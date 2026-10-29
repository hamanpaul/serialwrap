"""OD-33 R2(b)：0.3.2 → 0.4.0 升級驗證的判定純函式（容器內 driver 與 pytest 共用）。

只用標準函式庫：容器內 driver 以系統 python3 執行，不依賴 repo 套件。
每個檢查列為 {"id", "verdict": PASS|FAIL|INSUFFICIENT, "reason", "detail"}；整體 verdict 規則見 overall_verdict。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

# 每種監管模式都必須產出的檢查（順序即 driver 執行順序）。缺任何一項 → INSUFFICIENT。
REQUIRED_CHECKS: tuple[str, ...] = (
    "old_install_0_3_2",
    "seed_user_data",
    "doctor_green_old",
    "upgrade_with_old_daemon_running",
    "legacy_handoff_direct_setup",
    "upgrade_after_stop",
    "entries_replaced",
    "config_preserved_upgrade",
    "new_daemon_started",
    "supervision_reconciled",
    "state_compatible_upgrade",
    "doctor_green_new",
    "rollback_command",
    "downgrade_manual",
    "config_preserved_downgrade",
    "state_compatible_downgrade",
    "doctor_green_downgrade",
    "reupgrade",
    "no_two_reader",
)

# OD-48 SPEC-GAP #10：systemd-system 只驗「0.3.2 daemon 執行中直接 setup」成功路徑（rollback、手動降級與重新升級
# 屬完整流程，只在 systemd-user／on-demand 跑），必要檢查為完整集合的子集。
REQUIRED_CHECKS_SYSTEMD_SYSTEM: tuple[str, ...] = (
    "old_install_0_3_2",
    "seed_user_data",
    "doctor_green_old",
    "upgrade_with_old_daemon_running",
    "legacy_handoff_direct_setup",
    "upgrade_after_stop",
    "entries_replaced",
    "config_preserved_upgrade",
    "new_daemon_started",
    "supervision_reconciled",
    "state_compatible_upgrade",
    "doctor_green_new",
    "no_two_reader",
)

# 容器內沒有序列硬體（OD-15／OD-16 禁止把真實裝置帶進容器），doctor 的 devices advisory 必然為 0；其餘 advisory 須全綠。
ALLOWED_ADVISORY_FAILURES = frozenset({"devices"})

_DAEMON_NAMES = frozenset({"serialwrapd", "serialwrapd.exe", "serialwrapd.py"})

# 升級前寫入的使用者資料（driver phase_seed 建立）：基線必須真的含這些，相容比對才不會空轉。
SEED_ALIASES = frozenset({"upgbench", "pinned"})
SEED_BINDINGS = {"shellboard:COM7": "/dev/serial/by-id/usb-UPGRADE_REBOUND-if00-port0"}
SEED_CONFIG_FILES = frozenset({"profiles/bench.yaml"})

# 規格上預期的拒絕原因（release-0.4.0-plan.md §6）：現場未改動之外，拒絕原因也必須相符。
# OD-48 起只用於「非 legacy 的不支援交接」daemon；0.3.x daemon 必須改走舊版停止路徑，拒絕即 FAIL。
REFUSAL_LEGACY_HANDOFF = {"error_code": "METHOD_NOT_FOUND", "probe": "daemon.handoff_stop"}
LEGACY_STOP_METHODS = frozenset({"systemctl_stop", "rpc_daemon_stop"})
REFUSAL_ROLLBACK_UNAVAILABLE = {"error_code": "INVALID_ARGS", "reason": "rollback_unavailable"}


def row(check_id: str, verdict: str, reason: str | None = None, **detail: Any) -> dict[str, Any]:
    return {"id": check_id, "verdict": verdict, "reason": reason, "detail": detail}


def is_daemon_argv(argv: list[str]) -> bool:
    """argv 是否為 serialwrapd 行程本身（launcher、python 直譯器執行 serialwrapd(.py) 或 -m sw_core.daemon）。

    只看 argv[0] 與直譯器的第一個非選項參數；shell、runuser、grep 等命令字串中出現 serialwrapd 不算。
    """
    if not argv:
        return False
    base0 = os.path.basename(argv[0])
    if base0 in _DAEMON_NAMES:
        return True
    if not base0.startswith("python"):
        return False
    args = iter(argv[1:])
    for arg in args:
        if arg == "-m":
            return next(args, "") == "sw_core.daemon"
        if arg in ("-c", "-"):
            return False
        if arg.startswith("-"):
            continue
        return os.path.basename(arg) in _DAEMON_NAMES
    return False


def _read_argv(pid_dir: Path) -> list[str] | None:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _read_ppid(pid_dir: Path) -> int | None:
    try:
        text = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm 以括號包住且可能含空白：取最後一個 ')' 之後的欄位
    fields = text[text.rfind(")") + 1:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def scan_daemon_instances(proc_root: Path = Path("/proc")) -> list[int]:
    """回傳 daemon 實例的 PID（排序）。祖先鏈上另有 daemon 行程者（launcher 的子行程）併入祖先，不另計。"""
    daemons: dict[int, int | None] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        argv = _read_argv(entry)
        if argv and is_daemon_argv(argv):
            daemons[int(entry.name)] = _read_ppid(entry)
    roots = []
    for pid in daemons:
        seen = set()
        parent = daemons.get(pid)
        nested = False
        while parent and parent not in seen and parent != pid:
            if parent in daemons:
                nested = True
                break
            seen.add(parent)
            parent = _read_ppid(proc_root / str(parent))
        if not nested:
            roots.append(pid)
    return sorted(roots)


def judge_two_reader(samples: Iterable[tuple[float, list[int]]], *, min_samples: int) -> dict[str, Any]:
    """任何取樣同時有兩個以上 daemon 實例 → FAIL；取樣數不足 → INSUFFICIENT。"""
    samples = list(samples)
    overlaps = [{"t": t, "pids": sorted(pids)} for t, pids in samples if len(pids) > 1]
    seen: list[int] = []
    for _, pids in samples:
        for pid in pids:
            if pid not in seen:
                seen.append(pid)
    detail = {"samples": len(samples), "overlaps": overlaps[:20], "overlap_count": len(overlaps),
              "instances_seen": seen}
    if overlaps:
        return row("no_two_reader", "FAIL", "同一時間出現兩個以上 serialwrapd 實例（two-reader）", **detail)
    if len(samples) < min_samples:
        return row("no_two_reader", "INSUFFICIENT", f"取樣數 {len(samples)} < {min_samples}", **detail)
    return row("no_two_reader", "PASS", None, **detail)


def snapshot_files(root: Path) -> dict[str, str]:
    """root 下所有一般檔案的相對路徑 → sha256（不跟隨 symlink 目錄）。"""
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def judge_files_preserved(check_id: str, before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    missing = sorted(set(before) - set(after))
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    added = sorted(set(after) - set(before))
    if missing or changed:
        return row(check_id, "FAIL", "既有設定或 profile 遺失／被改寫", missing=missing, changed=changed, added=added)
    return row(check_id, "PASS", None, files=len(before), added=added)


def _alias_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(entry.get("alias")), str(entry.get("session_id") or ""), str(entry.get("device_by_id") or ""),
            str(entry.get("profile") or ""))


def judge_state_compatible(base: dict[str, Any], now: dict[str, Any], check_id: str = "state_compatible") -> dict[str, Any]:
    """alias（名稱、session_id、by-id、profile；不含時間戳）與 session 的 by-id 綁定須與基線相同。"""
    base_aliases = {_alias_key(a) for a in base.get("aliases") or []}
    now_aliases = {_alias_key(a) for a in now.get("aliases") or []}
    base_bind = {s.get("session_id"): s.get("device_by_id") for s in base.get("sessions") or []}
    now_bind = {s.get("session_id"): s.get("device_by_id") for s in now.get("sessions") or []}
    aliases_missing = sorted(base_aliases - now_aliases)
    bindings_changed = sorted(str(sid) for sid, dev in base_bind.items() if now_bind.get(sid) != dev)
    detail = {"aliases_missing": [list(a) for a in aliases_missing], "bindings_changed": bindings_changed,
              "aliases_added": [list(a) for a in sorted(now_aliases - base_aliases)]}
    if aliases_missing or bindings_changed:
        return row(check_id, "FAIL", "alias 或 binding 與升級前不一致", **detail)
    return row(check_id, "PASS", None, **detail)


def judge_doctor(check_id: str, doctor: dict[str, Any] | None, *, require_pairing: bool) -> dict[str, Any]:
    if not isinstance(doctor, dict) or not isinstance(doctor.get("checks"), list):
        return row(check_id, "FAIL", "doctor 輸出不可解析")
    checks = [c for c in doctor["checks"] if isinstance(c, dict)]
    hard = sorted(c.get("check") for c in checks if not c.get("ok") and not c.get("advisory"))
    advisory = sorted(c.get("check") for c in checks if not c.get("ok") and c.get("advisory"))
    extra_adv = sorted(set(advisory) - ALLOWED_ADVISORY_FAILURES)
    pairing = next((c for c in checks if c.get("check") == "version_pairing"), None)
    detail = {"ok": doctor.get("ok"), "failed": hard, "advisory_failed": advisory,
              "version_pairing": None if pairing is None else pairing.get("ok")}
    if hard or doctor.get("ok") is not True:
        return row(check_id, "FAIL", "doctor 有非 advisory 檢查失敗", **detail)
    if extra_adv:
        return row(check_id, "FAIL", f"doctor advisory 失敗超出容器允許集合 {sorted(ALLOWED_ADVISORY_FAILURES)}", **detail)
    if require_pairing and (pairing is None or pairing.get("ok") is not True):
        return row(check_id, "FAIL", "release 安裝的 doctor 缺 version_pairing 或未通過", **detail)
    return row(check_id, "PASS", None, **detail)


def judge_refusal_untouched(check_id: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """setup 拒絕時：daemon PID、入口 symlink 目標與設定檔雜湊都必須與拒絕前相同。"""
    diffs = [key for key in ("daemon_pids", "entries", "files") if before.get(key) != after.get(key)]
    if not before.get("daemon_pids"):
        return row(check_id, "INSUFFICIENT", "拒絕前沒有執行中的舊 daemon，無法驗證", before=before)
    if diffs:
        return row(check_id, "FAIL", "setup 拒絕後安裝現場已被改動", changed=diffs, before=before, after=after)
    return row(check_id, "PASS", None, kept=["daemon_pids", "entries", "files"])


def judge_refusal(check_id: str, before: dict[str, Any], after: dict[str, Any], body: dict[str, Any] | None,
                  expected: dict[str, str]) -> dict[str, Any]:
    """拒絕型檢查：現場須未改動（judge_refusal_untouched），且回應欄位須逐一等於 expected，否則 FAIL。"""
    body = body if isinstance(body, dict) else {}
    result = judge_refusal_untouched(check_id, before, after)
    got = {key: body.get(key) for key in expected}
    result["detail"]["refusal"] = got
    if result["verdict"] != "PASS":
        return result
    mismatched = sorted(key for key, value in expected.items() if got[key] != value)
    if mismatched:
        return row(check_id, "FAIL", "拒絕原因與規格不符", expected=dict(expected), refusal=got, mismatched=mismatched)
    return result


def judge_upgrade_running_refusal(check_id: str, before: dict[str, Any], after: dict[str, Any],
                                  body: dict[str, Any] | None, *, legacy: bool) -> dict[str, Any]:
    """舊 daemon 執行中直接 setup 卻被拒絕：0.3.x（legacy）daemon 依 OD-48 不得再拒絕 → FAIL；
    其他不支援交接的 daemon 才沿用 REFUSAL_LEGACY_HANDOFF 的拒絕判定。"""
    if legacy:
        body = body if isinstance(body, dict) else {}
        return row(check_id, "FAIL", "OD-48：0.3.x daemon 執行中的 setup 必須改走舊版停止路徑，不得拒絕",
                   outcome="refused", refusal={k: body.get(k) for k in ("error_code", "probe", "reason", "phase")})
    return judge_refusal(check_id, before, after, body, REFUSAL_LEGACY_HANDOFF)


def _step_detail(body: dict[str, Any], name: str) -> dict[str, Any]:
    report = body.get("install") or body.get("update") or {}
    step = next((s for s in report.get("steps") or [] if isinstance(s, dict) and s.get("step") == name), {})
    detail = step.get("detail")
    return detail if isinstance(detail, dict) else {}


def judge_legacy_direct_setup(check_id: str, *, setup_rc: int, body: dict[str, Any] | None, old_pids: list[int],
                              old_alive_after: list[int], status_after: dict[str, Any] | None, instances_after: list[int],
                              cfg_before: dict[str, str], cfg_after: dict[str, str], overlaps: list[dict[str, Any]],
                              new_version: str = "0.4.0", old_version: str = "0.3.2") -> dict[str, Any]:
    """OD-48：0.3.2 daemon 執行中直接 setup → 成功切換、舊 daemon 行程樹結束、新 daemon 由 setup 自己啟動
    （判定在 driver 任何啟動動作之前取樣；setup 沒啟動就 FAIL）、config／profiles 逐位元組保留、無兩個 daemon 重疊。"""
    body = body if isinstance(body, dict) else {}
    report = body.get("install") or body.get("update") or {}
    legacy = _step_detail(body, "stop_supervisor").get("legacy_stop") or {}
    started = _step_detail(body, "start_and_verify").get("started")
    status = status_after if isinstance(status_after, dict) else {}
    preserved = judge_files_preserved(check_id, cfg_before, cfg_after)
    detail = {"setup_rc": setup_rc, "op": report.get("op"), "to_version": report.get("to_version"),
              "legacy_stop": legacy, "started_by_setup": started, "old_pids": list(old_pids),
              "old_alive_after": list(old_alive_after), "daemon_version_after": status.get("version"),
              "daemon_pid_after": status.get("pid"), "instances_after": list(instances_after),
              "overlaps": overlaps[:20], "config": preserved["detail"], "error_code": body.get("error_code"),
              "refusal_reason": body.get("reason")}
    if not old_pids:
        return row(check_id, "INSUFFICIENT", "setup 前沒有執行中的 0.3.2 daemon，無法驗證", **detail)
    if setup_rc != 0 or body.get("ok") is not True or report.get("to_version") != new_version:
        return row(check_id, "FAIL", "0.3.2 daemon 執行中直接 setup 未成功切換", **detail)
    if legacy.get("method") not in LEGACY_STOP_METHODS or legacy.get("from_version") != old_version:
        return row(check_id, "FAIL", "報告缺少 legacy_stop 或停止手段／來源版本不符", **detail)
    if old_alive_after:
        return row(check_id, "FAIL", "舊 daemon 行程樹在 setup 完成後仍存在", **detail)
    if started is not True or status.get("version") != new_version or instances_after != [status.get("pid")]:
        return row(check_id, "FAIL", "新 daemon 不是由 setup 啟動（driver 不得代啟）或未唯一執行", **detail)
    if preserved["verdict"] != "PASS":
        return row(check_id, "FAIL", "既有設定或 profile 遺失／被改寫", **detail)
    if overlaps:
        return row(check_id, "FAIL", "升級期間同一時間出現兩個以上 serialwrapd 實例（two-reader）", **detail)
    return row(check_id, "PASS", None, **detail)


def judge_seed(check_id: str, *, rcs: list[int], baseline: dict[str, Any], config_files: Iterable[str]) -> dict[str, Any]:
    """基線須含 SEED_ALIASES、SEED_BINDINGS 已生效（session list 的 by-id 為重綁值）與 SEED_CONFIG_FILES。"""
    names = {a.get("alias") for a in baseline.get("aliases") or []}
    sessions = list(baseline.get("sessions") or [])
    bound = {s.get("session_id"): s.get("device_by_id") for s in sessions}
    files = set(config_files)
    missing_aliases = sorted(SEED_ALIASES - names)
    bindings_missing = sorted(sid for sid, dev in SEED_BINDINGS.items() if bound.get(sid) != dev)
    missing_files = sorted(SEED_CONFIG_FILES - files)
    detail = {"rcs": list(rcs), "aliases": sorted(map(str, names)), "sessions": sessions,
              "config_files": sorted(files)}
    if any(rc != 0 for rc in rcs) or missing_aliases or bindings_missing or missing_files:
        return row(check_id, "FAIL", "無法建立 alias／binding／profile 基線", missing_aliases=missing_aliases,
                   bindings_missing=bindings_missing, missing_files=missing_files, **detail)
    return row(check_id, "PASS", None, **detail)


def judge_rollback_success(check_id: str, *, cli_version: str | None, daemon_version: str | None,
                           cfg_before: dict[str, str], cfg_after: dict[str, str], old_version: str = "0.3.2") -> dict[str, Any]:
    """setup --rollback 成功路徑：CLI 與 daemon 都回到 old_version，且設定逐位元組保留。"""
    preserved = judge_files_preserved(check_id, cfg_before, cfg_after)
    detail = {"outcome": "rolled_back", "cli_version": cli_version, "daemon_version": daemon_version,
              "config": preserved["detail"]}
    ok = cli_version == f"serialwrap {old_version}" and daemon_version == old_version and preserved["verdict"] == "PASS"
    return row(check_id, "PASS" if ok else "FAIL", None if ok else f"setup --rollback 未完整回到 {old_version}", **detail)


def overall_verdict(rows: list[dict[str, Any]], required: tuple[str, ...] = REQUIRED_CHECKS) -> str:
    """任一 FAIL 或檢查 id 重複 → FAIL；缺必要檢查或任一 INSUFFICIENT → INSUFFICIENT；否則 PASS。"""
    ids = [r.get("id") for r in rows]
    if len(ids) != len(set(ids)) or any(r.get("verdict") == "FAIL" for r in rows):
        return "FAIL"
    if set(required) - set(ids) or any(r.get("verdict") != "PASS" for r in rows):
        return "INSUFFICIENT"
    return "PASS"
