"""Canary 政策（OD-10）的載入、驗證與時區換算（stdlib only）。

政策檔 `tests/acceptance/canary_policy.json` 的 `status` 為 `draft` 時可修改；凍結（`frozen`）後
不得改寫，且凍結只能經 `canary policy freeze` 執行（BO-P9-06）。本模組只負責讀取與驗證，
不寫入任何政策檔。時區以固定 `utc_offset_minutes` 加上預先列出的 `utc_offset_changes` 表達，
不依賴 `zoneinfo`（Windows 無 tzdata）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Mapping

from tools.stage3 import jsonio
from tools.stage3.reason_codes import HarnessError

# OD-10 配額下限，不可下修。
QUOTA_FLOOR: Mapping[str, int] = {"active_days": 14, "submits": 5000, "cycles_total": 50}
CYCLE_TYPES: tuple[str, ...] = ("handoff_release_attach", "hotplug_reattach", "dut_reboot", "recovery",
                                "human_agent_coexist", "flash", "planned_restart")
# §12.2「不能全部以同一種容易的 Recovery 取代」：至少 3 種類型有配額。
MIN_TYPES_WITH_QUOTA: int = 3
# sw_core/arbiter.py 以 uuid4().hex 產生 cmd_id；排除 file_transfer 的 "ft-<8hex>-<suffix>"。
ARBITER_CMD_ID_REGEX: str = "^[0-9a-f]{32}$"
PLATFORMS: tuple[str, ...] = ("linux", "windows")
_OFFSET_RANGE = (-720, 840)


@dataclass(frozen=True)
class PlatformQuota:
    active_days: int
    submits: int
    cycles_total: int
    cycles_min_per_type: Mapping[str, int]
    cycles_max_type_share: float       # 每種類型計入 cycles_total 的上限比例（0 < x ≤ 0.5）
    evidence_owner: str | None
    utc_offset_minutes: int
    utc_offset_changes: tuple[tuple[str, int], ...]   # [(from_iso, offset_minutes)]，依 from 升冪


@dataclass(frozen=True)
class CanaryPolicy:
    status: str                       # "draft" | "frozen"
    frozen_by: str | None
    frozen_at: str | None
    decision_ref: str | None
    platforms: Mapping[str, PlatformQuota]   # 鍵恰為 {"linux","windows"}
    active_day_min_distinct_submits: int
    active_day_min_snapshot_coverage_s: int
    exclude_source_prefixes: tuple[str, ...]
    cmd_id_regex: str                 # 必須等於 ARBITER_CMD_ID_REGEX
    result_poll_max_per_cycle: int    # 每次 collect 最多查幾個新 cmd_id 的 `cmd status`
    collector_interval_s: int
    gap_incident_s: int
    stuck_incident_s: int
    coexist_bucket_s: int
    coexist_cap_per_day: int
    episode_max_gap_factor: int
    recover_cluster_s: int
    post_boot_grace_s: int
    unreachable_incident_polls: int
    reboot_corroboration_s: int       # boot 身分變更須有 ≥ 此秒數的 collector 缺口才算主機重開
    allowed_tx_source_regex: str      # 與 acceptance 檔的 safety 段同語意；Canary 為真實使用，值由擁有者凍結
    resend_window_s: int
    human_replay_min_len: int
    sha256: str                       # 檔案 bytes 的 sha256


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_CANARY_POLICY_INVALID", detail)


def offset_at(quota: PlatformQuota, ts: str) -> int:
    """回傳 `ts` 時刻適用的 UTC 偏移（分鐘）：from ≤ ts 的最後一筆變更，否則預設值。"""
    when = jsonio.parse_iso(ts)
    offset = quota.utc_offset_minutes
    for frm, minutes in quota.utc_offset_changes:
        if jsonio.parse_iso(frm) <= when:
            offset = minutes
        else:
            break
    return offset


def local_date(quota: PlatformQuota, ts: str) -> str:
    """依平台的本地時區換算日期，格式 `YYYY-MM-DD`。"""
    local = jsonio.parse_iso(ts).astimezone(timezone.utc) + timedelta(minutes=offset_at(quota, ts))
    return local.date().isoformat()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _req_int(d: Mapping, key: str, where: str, *, minimum: int = 0) -> int:
    value = d.get(key)
    if not _is_int(value) or value < minimum:
        raise _invalid(f"{where}.{key} must be integer >= {minimum}")
    return value


def _req_str(d: Mapping, key: str, where: str) -> str:
    value = d.get(key)
    if not isinstance(value, str):
        raise _invalid(f"{where}.{key} must be string")
    return value


def _opt_str(d: Mapping, key: str, where: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"{where}.{key} must be string or null")
    return value


def _opt_nonblank_str(d: Mapping, key: str, where: str) -> str | None:
    """凍結身分欄位：缺省（null）或非空白字串；空字串與純空白視為未填，一律拒絕（與 schema minLength 1 一致）。"""
    value = _opt_str(d, key, where)
    if value is not None and not value.strip():
        raise _invalid(f"{where}.{key} must not be blank")
    return value


def _section(d: Mapping, key: str, where: str) -> Mapping:
    value = d.get(key)
    if not isinstance(value, dict):
        raise _invalid(f"{where}.{key} must be object")
    return value


def _parse_quota(platform: str, raw: Mapping, evidence_owner: str | None) -> PlatformQuota:
    where = f"platforms.{platform}"
    quotas = _section(raw, "quotas", where)
    active_days = _req_int(quotas, "active_days", f"{where}.quotas")
    submits = _req_int(quotas, "submits", f"{where}.quotas")
    cycles_total = _req_int(quotas, "cycles_total", f"{where}.quotas")
    for key, floor in QUOTA_FLOOR.items():
        value = {"active_days": active_days, "submits": submits, "cycles_total": cycles_total}[key]
        if value < floor:
            raise _invalid(f"{where}.quotas.{key} below OD-10 floor {floor}")

    per_type_raw = _section(quotas, "cycles_min_per_type", f"{where}.quotas")
    per_type: dict[str, int] = {}
    for name, value in per_type_raw.items():
        if name not in CYCLE_TYPES:
            raise _invalid(f"{where}.cycles_min_per_type has unknown type {name!r}")
        if not _is_int(value) or value < 0:
            raise _invalid(f"{where}.cycles_min_per_type.{name} must be integer >= 0")
        per_type[name] = value
    if sum(per_type.values()) > cycles_total:
        raise _invalid(f"{where}: sum(cycles_min_per_type) exceeds cycles_total")
    if sum(1 for v in per_type.values() if v >= 1) < MIN_TYPES_WITH_QUOTA:
        raise _invalid(f"{where}: fewer than {MIN_TYPES_WITH_QUOTA} cycle types carry a quota")

    share = quotas.get("cycles_max_type_share")
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not (0 < share <= 0.5):
        raise _invalid(f"{where}.cycles_max_type_share must be in (0, 0.5]")
    cap = math.floor(round(share * cycles_total, 9))
    for name, value in per_type.items():
        if value > cap:
            raise _invalid(f"{where}: cycles_min_per_type.{name} exceeds max type share cap {cap}")

    if platform == "windows" and per_type.get("flash", 0) > 0:
        raise _invalid("windows cycles_min_per_type.flash must be 0 (no /dev/ttyMCU on Windows)")

    offset = raw.get("utc_offset_minutes")
    if not _is_int(offset) or not (_OFFSET_RANGE[0] <= offset <= _OFFSET_RANGE[1]):
        raise _invalid(f"{where}.utc_offset_minutes out of range")
    changes_raw = raw.get("utc_offset_changes")
    if not isinstance(changes_raw, list):
        raise _invalid(f"{where}.utc_offset_changes must be array")
    changes: list[tuple[str, int]] = []
    prev = None
    for item in changes_raw:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and _is_int(item[1])):
            raise _invalid(f"{where}.utc_offset_changes item must be [iso, minutes]")
        frm, minutes = item
        try:
            when = jsonio.parse_iso(frm)
        except ValueError as exc:
            raise _invalid(f"{where}.utc_offset_changes from must be tz-aware ISO: {exc}") from exc
        if not (_OFFSET_RANGE[0] <= minutes <= _OFFSET_RANGE[1]):
            raise _invalid(f"{where}.utc_offset_changes offset out of range")
        if prev is not None and when <= prev:
            raise _invalid(f"{where}.utc_offset_changes must be strictly ascending by from")
        prev = when
        changes.append((frm, minutes))

    return PlatformQuota(
        active_days=active_days,
        submits=submits,
        cycles_total=cycles_total,
        cycles_min_per_type=per_type,
        cycles_max_type_share=float(share),
        evidence_owner=evidence_owner,
        utc_offset_minutes=offset,
        utc_offset_changes=tuple(changes),
    )


def _parse(doc: Mapping, *, sha256: str) -> CanaryPolicy:
    if _req_int(doc, "schema_version", "root", minimum=1) != 1:
        raise _invalid("unsupported schema_version")
    status = _req_str(doc, "status", "root")
    if status not in ("draft", "frozen"):
        raise _invalid("status must be draft or frozen")
    frozen_by = _opt_nonblank_str(doc, "frozen_by", "root")
    frozen_at = _opt_nonblank_str(doc, "frozen_at", "root")
    decision_ref = _opt_nonblank_str(doc, "decision_ref", "root")
    if frozen_at is not None:
        try:
            jsonio.parse_iso(frozen_at)
        except ValueError as exc:
            raise _invalid(f"frozen_at must be tz-aware ISO: {exc}") from exc

    active_day = _section(doc, "active_day", "root")
    collector = _section(doc, "collector", "root")
    cycles = _section(doc, "cycles", "root")
    incidents = _section(doc, "incidents", "root")
    safety = _section(doc, "safety", "root")
    submit_counting = _section(doc, "submit_counting", "root")

    cmd_id_regex = _req_str(submit_counting, "cmd_id_regex", "submit_counting")
    if cmd_id_regex != ARBITER_CMD_ID_REGEX:
        raise _invalid("submit_counting.cmd_id_regex must equal ARBITER_CMD_ID_REGEX")
    tx_regex = _req_str(safety, "allowed_tx_source_regex", "safety")
    try:
        re.compile(tx_regex)
    except re.error as exc:
        raise _invalid(f"safety.allowed_tx_source_regex does not compile: {exc}") from exc
    excludes = submit_counting.get("exclude_source_prefixes")
    if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
        raise _invalid("submit_counting.exclude_source_prefixes must be string array")
    # 計數口徑是工具的唯一實作（Task 8）：政策只能宣告同一口徑，不得宣告放寬後的值（凍結後不可更正）
    if submit_counting.get("source") != "wal_tx_cmd_id":
        raise _invalid("submit_counting.source must be wal_tx_cmd_id")
    if submit_counting.get("dedup_key") != ["host_id", "cmd_id"]:
        raise _invalid("submit_counting.dedup_key must be [host_id, cmd_id]")
    if submit_counting.get("require_terminal_result") is not True:
        raise _invalid("submit_counting.require_terminal_result must be true")
    if not {"human:", "system", "flash"} <= set(excludes):
        raise _invalid("submit_counting.exclude_source_prefixes must keep human:, system and flash")
    if re.fullmatch(tx_regex, "unauthorized-source-sentinel") is not None:
        raise _invalid("safety.allowed_tx_source_regex must reject unknown sources")

    platforms_raw = _section(doc, "platforms", "root")
    if set(platforms_raw) != set(PLATFORMS):
        raise _invalid("platforms keys must be exactly linux and windows")
    platforms: dict[str, PlatformQuota] = {}
    for name in PLATFORMS:
        raw = _section(platforms_raw, name, "platforms")
        owner = _opt_nonblank_str(raw, "evidence_owner", f"platforms.{name}")
        if status == "frozen" and owner is None:
            raise _invalid(f"platforms.{name}.evidence_owner required when frozen")
        platforms[name] = _parse_quota(name, raw, owner)

    if status == "frozen":
        if frozen_by is None or frozen_at is None or decision_ref is None:
            raise _invalid("frozen policy requires frozen_by, frozen_at and decision_ref")

    return CanaryPolicy(
        status=status,
        frozen_by=frozen_by,
        frozen_at=frozen_at,
        decision_ref=decision_ref,
        platforms=platforms,
        active_day_min_distinct_submits=_req_int(active_day, "min_distinct_submits", "active_day", minimum=1),
        active_day_min_snapshot_coverage_s=_req_int(active_day, "min_snapshot_coverage_s", "active_day"),
        exclude_source_prefixes=tuple(excludes),
        cmd_id_regex=cmd_id_regex,
        result_poll_max_per_cycle=_req_int(collector, "result_poll_max_per_cycle", "collector", minimum=1),
        collector_interval_s=_req_int(collector, "interval_s", "collector", minimum=1),
        gap_incident_s=_req_int(collector, "gap_incident_s", "collector", minimum=1),
        stuck_incident_s=_req_int(incidents, "stuck_incident_s", "incidents", minimum=1),
        coexist_bucket_s=_req_int(cycles, "coexist_bucket_s", "cycles", minimum=1),
        coexist_cap_per_day=_req_int(cycles, "coexist_cap_per_day", "cycles", minimum=1),
        episode_max_gap_factor=_req_int(collector, "episode_max_gap_factor", "collector", minimum=1),
        recover_cluster_s=_req_int(cycles, "recover_cluster_s", "cycles", minimum=1),
        post_boot_grace_s=_req_int(collector, "post_boot_grace_s", "collector"),
        unreachable_incident_polls=_req_int(collector, "unreachable_incident_polls", "collector", minimum=1),
        reboot_corroboration_s=_req_int(collector, "reboot_corroboration_s", "collector"),
        allowed_tx_source_regex=tx_regex,
        resend_window_s=_req_int(safety, "resend_window_s", "safety", minimum=1),
        human_replay_min_len=_req_int(safety, "human_replay_min_len", "safety", minimum=1),
        sha256=sha256,
    )


def load_policy(path: Path, *, addenda_path: Path | None = None) -> CanaryPolicy:
    """載入並驗證 Canary 政策。

    違規一律丟出 `HARNESS_CANARY_POLICY_INVALID`。凍結政策若給定 `addenda_path`，
    附加式決策紀錄檔必須有以 `| <decision_ref> |` 開頭的列，否則同一錯誤碼。
    """
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:   # 含 UnicodeDecodeError、整數位數上限與過深巢狀，一律為 POLICY_INVALID
        raise _invalid(f"policy is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise _invalid("policy root must be object")
    policy = _parse(doc, sha256=jsonio.sha256_bytes(raw))
    if policy.status == "frozen" and addenda_path is not None:
        try:
            text = Path(addenda_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise _invalid(f"addenda unreadable: {exc}") from exc
        prefix = f"| {policy.decision_ref} |"
        if not any(line.startswith(prefix) for line in text.splitlines()):
            raise _invalid("decision_ref not in addenda")
    return policy


def assert_frozen_before(policy: CanaryPolicy, first_counted_wall_ts: str) -> None:
    """確認政策已凍結且凍結時間不晚於第一筆計數紀錄；否則丟出 `HARNESS_CANARY_POLICY_NOT_FROZEN`。"""
    if policy.status != "frozen" or policy.frozen_at is None:
        raise HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN", "policy is draft")
    if jsonio.parse_iso(policy.frozen_at) > jsonio.parse_iso(first_counted_wall_ts):
        raise HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN", "frozen_at is later than first counted record")


def freeze_policy(
    policy_path: Path,
    *,
    decision_id: str,
    frozen_by: str,
    evidence_owners: Mapping[str, str],
    addenda_path: Path,
    now: str,
) -> dict[str, str]:
    """將 draft Canary 政策凍結並附加記錄至 owner-decisions-addenda.md。"""
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise _invalid("decision_id must not be blank")
    if not isinstance(frozen_by, str) or not frozen_by.strip():
        raise _invalid("frozen_by must not be blank")
    try:
        when = jsonio.parse_iso(now)
    except Exception as exc:
        raise _invalid(f"now must be tz-aware ISO: {exc}") from exc

    if not isinstance(evidence_owners, Mapping) or set(evidence_owners) != set(PLATFORMS):
        raise _invalid("evidence_owners keys must be exactly linux and windows")
    for plat in PLATFORMS:
        owner = evidence_owners.get(plat)
        if not isinstance(owner, str) or not owner.strip():
            raise _invalid(f"evidence_owners.{plat} must not be blank")

    current_policy = load_policy(policy_path)
    if current_policy.status != "draft":
        raise _invalid("policy already frozen")

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", decision_id) is None:
        raise _invalid("decision_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    for label, value in (("frozen_by", frozen_by), *((f"evidence_owners.{p}", evidence_owners[p]) for p in PLATFORMS)):
        if any(ch in value for ch in "\r\n|"):
            raise _invalid(f"{label} must not contain CR, LF or '|'")

    addenda = Path(addenda_path)
    if not addenda.is_file():
        raise _invalid("addenda file not found (append-only record owned by P2 Task 16)")
    original_addenda = addenda.read_text(encoding="utf-8")
    if any(line.startswith(f"| {decision_id} |") for line in original_addenda.splitlines()):
        raise _invalid("decision_id already recorded in addenda")

    policy_raw = Path(policy_path).read_text(encoding="utf-8")
    try:
        doc = json.loads(policy_raw)
    except Exception as exc:
        raise _invalid(f"policy is not valid JSON: {exc}") from exc

    doc["status"] = "frozen"
    doc["frozen_by"] = frozen_by
    doc["frozen_at"] = now
    doc["decision_ref"] = decision_id
    doc["platforms"]["linux"]["evidence_owner"] = evidence_owners["linux"]
    doc["platforms"]["windows"]["evidence_owner"] = evidence_owners["windows"]

    new_text = jsonio.dumps_report(doc)
    new_sha256 = jsonio.sha256_bytes(new_text.encode("utf-8"))

    date_str = when.date().isoformat()
    row = (
        f"| {decision_id} | Canary policy 凍結 | {date_str} | "
        f"frozen_by={frozen_by}；policy_sha256={new_sha256}；"
        f"evidence_owner linux={evidence_owners['linux']}、windows={evidence_owners['windows']} |\n"
    )

    prefix = "" if (original_addenda.endswith("\n") or not original_addenda) else "\n"
    try:
        jsonio.atomic_write_text(addenda, original_addenda + prefix + row)
        try:
            jsonio.atomic_write_text(Path(policy_path), new_text)
        except BaseException:
            jsonio.atomic_write_text(addenda, original_addenda)
            raise
    except OSError as exc:
        raise _invalid(f"freeze aborted, nothing changed: {exc}") from exc

    load_policy(Path(policy_path), addenda_path=addenda)

    return {
        "policy_sha256": new_sha256,
        "frozen_at": now,
        "decision_ref": decision_id,
    }

