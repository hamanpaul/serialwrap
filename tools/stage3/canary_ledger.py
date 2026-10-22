"""Canary 帳本彙總、分平台配額核算與 gate 判定器（stdlib only）。"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.stage3 import canary_classify, canary_incidents, canary_policy, jsonio
from tools.stage3.reason_codes import HarnessError


@dataclass(frozen=True)
class PlatformTally:
    platform: str
    artifact_id: str
    bundle_tree_sha256: str
    runtime_tree_sha256: str
    active_days: tuple[str, ...]
    submits: int
    tree_mismatch: int
    submits_unknown_result: int
    submits_result_evicted: int
    cycles_by_type: Mapping[str, int]
    cycles_total: int
    open_incidents: tuple[str, ...]
    failing_incidents: tuple[str, ...]
    pre_freeze_ignored: int
    policy_mismatch: int
    unreferenced: int
    carried_over: Mapping[str, int]
    first_counted_wall: str | None
    cycles_truncated: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CanaryGate:
    platform: str
    artifact_id: str
    bundle_tree_sha256: str
    runtime_tree_sha256: str
    result: str
    reasons: tuple[str, ...]
    tally: PlatformTally
    policy_sha256: str
    ledger_through: str | None
    ledger_records: int


def load_records(paths: Sequence[Path]) -> list[dict]:
    """載入並合併多個 host 帳本，依 record_id 去重；遇到損毀行或檔案不存在丟出 HARNESS_LEDGER_CORRUPT。"""
    records: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        path = Path(p)
        if not path.is_file():
            raise HarnessError("HARNESS_LEDGER_CORRUPT", f"ledger not found: {path}")
        rows, malformed = jsonio.read_ndjson(path)
        if malformed:
            raise HarnessError("HARNESS_LEDGER_CORRUPT", f"{path.name} malformed lines: {malformed[:5]}")
        for row in rows:
            rid = row.get("record_id")
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)
            records.append(row)
    return records


def _record_time_str(record: Mapping[str, Any]) -> str:
    fields = {
        "snapshot": "t_wall",
        "submit": "wall_ts",
        "submit_result": "t_wall",
        "harvest_anomaly": "t_wall",
        "note": "t_wall",
        "policy_ref": "t_wall",
        "wal_signal": "wall_ts",
        "incident_open": "opened_at",
        "incident_close": "closed_at",
        "carryover": "approved_at",
    }
    rtype = record.get("type")
    field = fields.get(str(rtype))
    if field and record.get(field):
        return str(record[field])
    for f in ("t_wall", "wall_ts", "opened_at", "closed_at", "approved_at"):
        val = record.get(f)
        if val is not None:
            return str(val)
    return "1970-01-01T00:00:00+00:00"


def _calc_ledger_version(records: Sequence[dict], platform: str) -> tuple[str | None, int]:
    rows = [r for r in records if r.get("platform") == platform]
    if not rows:
        return None, 0
    record_ids = {r["record_id"] for r in rows if "record_id" in r}
    latest_dt = max(jsonio.parse_iso(_record_time_str(r)).astimezone(timezone.utc) for r in rows)
    return latest_dt.isoformat(), len(record_ids)


def tally(
    records: Sequence[dict],
    policy: canary_policy.CanaryPolicy,
    *,
    platform: str,
    artifact_id: str,
    bundle_tree_sha256: str,
    runtime_tree_sha256: str,
    carryovers: Sequence[dict] = (),
) -> PlatformTally:
    """彙總特定平台與受測物在 Canary 帳本中的活躍日、有效提交、干擾循環與事故。"""
    platform_records = [r for r in records if r.get("platform") == platform]

    # 依 host 分組並按時間與優先順序排序
    records_by_host: dict[str, list[dict]] = defaultdict(list)
    for r in platform_records:
        records_by_host[str(r.get("host_id", ""))].append(r)

    priority_map = {"policy_ref": 0, "snapshot": 1, "submit": 2, "wal_signal": 3, "submit_result": 4}

    unreferenced = 0
    policy_mismatch = 0
    valid_records: list[dict] = []

    for host_id, rows in records_by_host.items():
        rows.sort(
            key=lambda item: (
                jsonio.parse_iso(_record_time_str(item)),
                priority_map.get(item.get("type"), 5),
                str(item.get("record_id", "")),
            )
        )
        current_policy_sha: str | None = None
        for r in rows:
            rtype = r.get("type")
            if rtype == "policy_ref":
                current_policy_sha = r.get("policy_sha256")
                continue
            if current_policy_sha is None:
                unreferenced += 1
                continue
            if current_policy_sha != policy.sha256:
                policy_mismatch += 1
                continue
            valid_records.append(r)

    valid_snapshots = [r for r in valid_records if r.get("type") == "snapshot"]
    valid_submits = [r for r in valid_records if r.get("type") == "submit"]
    valid_submit_results = [r for r in valid_records if r.get("type") == "submit_result"]
    valid_signals = [r for r in valid_records if r.get("type") == "wal_signal"]
    valid_notes = [r for r in valid_records if r.get("type") == "note"]
    valid_anomalies = [r for r in valid_records if r.get("type") == "harvest_anomaly"]

    snapshots_by_host: dict[str, list[dict]] = defaultdict(list)
    for snap in valid_snapshots:
        snapshots_by_host[str(snap.get("host_id", ""))].append(snap)
    for snaps in snapshots_by_host.values():
        snaps.sort(key=lambda s: jsonio.parse_iso(str(s["t_wall"])))

    results_by_key: dict[tuple[str, str], dict] = {}
    for res in valid_submit_results:
        key = (str(res.get("host_id", "")), str(res.get("cmd_id", "")))
        results_by_key[key] = res

    pre_freeze_ignored = 0
    submits_count = 0
    tree_mismatch = 0
    submits_unknown_result = 0
    submits_result_evicted = 0
    counted_walls: list[datetime] = []

    candidate_submits: list[dict] = []
    for s in valid_submits:
        cmd_id = s.get("cmd_id")
        source = s.get("source")
        if not isinstance(cmd_id, str) or re.fullmatch(policy.cmd_id_regex, cmd_id) is None:
            continue
        if not isinstance(source, str) or source.startswith(policy.exclude_source_prefixes):
            continue
        if policy.frozen_at is not None:
            if jsonio.parse_iso(str(s["wall_ts"])) < jsonio.parse_iso(policy.frozen_at):
                pre_freeze_ignored += 1
                continue
        candidate_submits.append(s)

    candidate_submits.sort(
        key=lambda item: (
            jsonio.parse_iso(str(item["wall_ts"])),
            str(item.get("host_id", "")),
            str(item["cmd_id"]),
        )
    )

    seen_submits: set[tuple[str, str]] = set()
    unique_submits: list[dict] = []
    for s in candidate_submits:
        k = (str(s.get("host_id", "")), str(s["cmd_id"]))
        if k in seen_submits:
            continue
        seen_submits.add(k)
        unique_submits.append(s)

    for s in unique_submits:
        host_id = str(s.get("host_id", ""))
        wall = str(s["wall_ts"])
        attribution = canary_classify.attribute_artifact(wall, snapshots_by_host.get(host_id, []))
        if attribution is None:
            continue
        if attribution.artifact_id != artifact_id:
            continue
        if (
            attribution.bundle_tree_sha256 != bundle_tree_sha256
            or attribution.runtime_tree_sha256 != runtime_tree_sha256
        ):
            tree_mismatch += 1
            continue

        res = results_by_key.get((host_id, str(s["cmd_id"])))
        status = res.get("status") if res else None
        if status in canary_classify.TERMINAL_RESULTS:
            submits_count += 1
            counted_walls.append(jsonio.parse_iso(wall))
        else:
            submits_unknown_result += 1
            if res and res.get("reason") == "CMD_NOT_FOUND":
                submits_result_evicted += 1

    active_snaps = valid_snapshots
    if policy.frozen_at is not None:
        active_snaps = [
            s for s in valid_snapshots if jsonio.parse_iso(str(s["t_wall"])) >= jsonio.parse_iso(policy.frozen_at)
        ]
    content_attr = canary_classify.Attribution(artifact_id, bundle_tree_sha256, runtime_tree_sha256)
    days_list = canary_classify.active_days(
        candidate_submits,
        active_snaps,
        policy,
        platform=platform,
        content=content_attr,
        results=valid_submit_results,
    )
    active_days = tuple(days_list)

    all_cycles = canary_classify.classify(
        valid_snapshots, candidate_submits, valid_signals, valid_notes, policy, platform=platform
    )
    cycles_by_type_dict = {t: 0 for t in canary_policy.CYCLE_TYPES}
    for cycle in all_cycles:
        if cycle.result != "recovered_ready":
            continue
        if (
            cycle.artifact_id,
            cycle.bundle_tree_sha256,
            cycle.runtime_tree_sha256,
        ) != (artifact_id, bundle_tree_sha256, runtime_tree_sha256):
            continue
        if policy.frozen_at is not None and jsonio.parse_iso(cycle.start_wall) < jsonio.parse_iso(policy.frozen_at):
            pre_freeze_ignored += 1
            continue
        cycles_by_type_dict[cycle.type] = cycles_by_type_dict.get(cycle.type, 0) + 1
        counted_walls.append(jsonio.parse_iso(cycle.start_wall))

    quota = policy.platforms[platform]
    cap = math.floor(round(quota.cycles_max_type_share * quota.cycles_total, 9))
    cycles_total = sum(min(cnt, cap) for cnt in cycles_by_type_dict.values())

    carried_over_dict: dict[str, int] = {}
    seen_carry: set[tuple[str, str]] = set()
    for carry in carryovers:
        if carry.get("type") != "carryover":
            continue
        if carry.get("to_artifact") != artifact_id:
            continue
        if carry.get("platform") != platform:
            continue
        approved_by = carry.get("approved_by")
        decision_ref = carry.get("decision_ref")
        if not approved_by or not decision_ref:
            continue
        from_art = carry.get("from_artifact")
        if not from_art or from_art == artifact_id:
            continue
        from_btree = carry.get("from_bundle_tree_sha256")
        from_rtree = carry.get("from_runtime_tree_sha256")
        if not from_btree or not from_rtree:
            from_trees = {
                (s.get("bundle_tree_sha256"), s.get("runtime_tree_sha256"))
                for s in platform_records
                if s.get("type") == "snapshot"
                and s.get("artifact_id") == from_art
                and s.get("artifact_check") == "verified"
                and s.get("bundle_tree_sha256")
                and s.get("runtime_tree_sha256")
            }
            if len(from_trees) != 1:
                continue
            from_btree, from_rtree = next(iter(from_trees))

        from_tally = tally(
            records,
            policy,
            platform=platform,
            artifact_id=from_art,
            bundle_tree_sha256=from_btree,
            runtime_tree_sha256=from_rtree,
            carryovers=(),
        )
        for c in carry.get("counters", []):
            if (str(from_art), str(c)) in seen_carry:
                continue
            seen_carry.add((str(from_art), str(c)))
            if c == "submits":
                val = from_tally.submits
                carried_over_dict["submits"] = carried_over_dict.get("submits", 0) + val
                submits_count += val
            elif c == "cycles":
                for t, val in from_tally.cycles_by_type.items():
                    cycles_by_type_dict[t] = cycles_by_type_dict.get(t, 0) + val
                carried_over_dict["cycles"] = carried_over_dict.get("cycles", 0) + from_tally.cycles_total
                cycles_total = sum(min(cnt, cap) for cnt in cycles_by_type_dict.values())
            elif c in cycles_by_type_dict:
                val = from_tally.cycles_by_type.get(c, 0)
                cycles_by_type_dict[c] = cycles_by_type_dict.get(c, 0) + val
                carried_over_dict[c] = carried_over_dict.get(c, 0) + val
                cycles_total = sum(min(cnt, cap) for cnt in cycles_by_type_dict.values())

    cycles_truncated_dict = {
        t: max(0, cnt - cap)
        for t, cnt in cycles_by_type_dict.items()
        if cnt > cap
    }

    detected = canary_incidents.detect(
        [r for r in platform_records if r.get("type") == "snapshot"],
        [r for r in platform_records if r.get("type") == "wal_signal"],
        valid_notes,
        policy,
        anomalies=[r for r in platform_records if r.get("type") == "harvest_anomaly"],
    )
    current = canary_incidents.current_incidents(records, detected)
    scoped_incidents = [
        inc
        for inc in current.values()
        if inc.platform == platform and (inc.artifact_id is None or inc.artifact_id == artifact_id)
    ]
    open_incidents = tuple(sorted(inc.incident_id for inc in scoped_incidents if inc.status == "open"))
    failing_incidents = tuple(
        sorted(
            inc.incident_id
            for inc in scoped_incidents
            if inc.kind == "safety_violation" or inc.resolution == "fixed_in_artifact"
        )
    )

    first_counted_wall = min(counted_walls).isoformat() if counted_walls else None

    return PlatformTally(
        platform=platform,
        artifact_id=artifact_id,
        bundle_tree_sha256=bundle_tree_sha256,
        runtime_tree_sha256=runtime_tree_sha256,
        active_days=active_days,
        submits=submits_count,
        tree_mismatch=tree_mismatch,
        submits_unknown_result=submits_unknown_result,
        submits_result_evicted=submits_result_evicted,
        cycles_by_type=cycles_by_type_dict,
        cycles_total=cycles_total,
        open_incidents=open_incidents,
        failing_incidents=failing_incidents,
        pre_freeze_ignored=pre_freeze_ignored,
        policy_mismatch=policy_mismatch,
        unreferenced=unreferenced,
        carried_over=carried_over_dict,
        first_counted_wall=first_counted_wall,
        cycles_truncated=cycles_truncated_dict,
    )


def gate(
    records: Sequence[dict],
    policy: canary_policy.CanaryPolicy,
    *,
    platform: str,
    artifact_id: str,
    bundle_tree_sha256: str,
    runtime_tree_sha256: str,
    carryovers: Sequence[dict] = (),
) -> CanaryGate:
    """依 Canary 政策判定特定平台與受測物的發版配額門檻。"""
    ledger_through, ledger_records = _calc_ledger_version(records, platform)

    tally_obj = tally(
        records,
        policy,
        platform=platform,
        artifact_id=artifact_id,
        bundle_tree_sha256=bundle_tree_sha256,
        runtime_tree_sha256=runtime_tree_sha256,
        carryovers=carryovers,
    )

    # 1. 政策凍結檢查
    if policy.status != "frozen":
        return CanaryGate(
            platform=platform,
            artifact_id=artifact_id,
            bundle_tree_sha256=bundle_tree_sha256,
            runtime_tree_sha256=runtime_tree_sha256,
            result="POLICY_NOT_FROZEN",
            reasons=(f"HARNESS_CANARY_POLICY_NOT_FROZEN: policy status is {policy.status}",),
            tally=tally_obj,
            policy_sha256=policy.sha256,
            ledger_through=ledger_through,
            ledger_records=ledger_records,
        )

    if tally_obj.first_counted_wall is not None:
        try:
            canary_policy.assert_frozen_before(policy, tally_obj.first_counted_wall)
        except HarnessError as exc:
            if exc.code == "HARNESS_CANARY_POLICY_NOT_FROZEN":
                return CanaryGate(
                    platform=platform,
                    artifact_id=artifact_id,
                    bundle_tree_sha256=bundle_tree_sha256,
                    runtime_tree_sha256=runtime_tree_sha256,
                    result="POLICY_NOT_FROZEN",
                    reasons=(f"HARNESS_CANARY_POLICY_NOT_FROZEN: {exc.detail}",),
                    tally=tally_obj,
                    policy_sha256=policy.sha256,
                    ledger_through=ledger_through,
                    ledger_records=ledger_records,
                )
            raise

    # 2. 政策變更檢查
    policy_mismatch = tally_obj.policy_mismatch
    if policy_mismatch > 0:
        return CanaryGate(
            platform=platform,
            artifact_id=artifact_id,
            bundle_tree_sha256=bundle_tree_sha256,
            runtime_tree_sha256=runtime_tree_sha256,
            result="POLICY_CHANGED",
            reasons=(f"HARNESS_CANARY_POLICY_CHANGED: {policy_mismatch} records",),
            tally=tally_obj,
            policy_sha256=policy.sha256,
            ledger_through=ledger_through,
            ledger_records=ledger_records,
        )

    # 3. 致命事故檢查
    failing_incidents = tally_obj.failing_incidents
    if failing_incidents:
        return CanaryGate(
            platform=platform,
            artifact_id=artifact_id,
            bundle_tree_sha256=bundle_tree_sha256,
            runtime_tree_sha256=runtime_tree_sha256,
            result="FAIL",
            reasons=(f"failing incidents: {', '.join(failing_incidents)}",),
            tally=tally_obj,
            policy_sha256=policy.sha256,
            ledger_through=ledger_through,
            ledger_records=ledger_records,
        )

    # 4. 配額完整性檢查
    quota = policy.platforms[platform]
    incomplete_reasons: list[str] = []
    open_incidents = tally_obj.open_incidents
    if open_incidents:
        incomplete_reasons.append(f"open incidents: {', '.join(open_incidents)}")
    active_days = tally_obj.active_days
    if len(active_days) < quota.active_days:
        incomplete_reasons.append(f"active days: {len(active_days)}/{quota.active_days}")
    submits = tally_obj.submits
    if submits < quota.submits:
        incomplete_reasons.append(f"submits: {submits}/{quota.submits}")
    cycles_total = tally_obj.cycles_total
    if cycles_total < quota.cycles_total:
        incomplete_reasons.append(f"cycles total: {cycles_total}/{quota.cycles_total}")
    cycles_by_type = tally_obj.cycles_by_type
    for cycle_type, min_count in quota.cycles_min_per_type.items():
        actual = cycles_by_type.get(cycle_type, 0)
        if actual < min_count:
            incomplete_reasons.append(f"cycle type {cycle_type}: {actual}/{min_count}")

    if incomplete_reasons:
        return CanaryGate(
            platform=platform,
            artifact_id=artifact_id,
            bundle_tree_sha256=bundle_tree_sha256,
            runtime_tree_sha256=runtime_tree_sha256,
            result="INCOMPLETE",
            reasons=tuple(incomplete_reasons),
            tally=tally_obj,
            policy_sha256=policy.sha256,
            ledger_through=ledger_through,
            ledger_records=ledger_records,
        )

    # 5. 通過
    return CanaryGate(
        platform=platform,
        artifact_id=artifact_id,
        bundle_tree_sha256=bundle_tree_sha256,
        runtime_tree_sha256=runtime_tree_sha256,
        result="PASS",
        reasons=(),
        tally=tally_obj,
        policy_sha256=policy.sha256,
        ledger_through=ledger_through,
        ledger_records=ledger_records,
    )


def report_md(gates: Sequence[CanaryGate]) -> str:
    """產出人類可讀的 Canary gate Markdown 報告。"""
    lines: list[str] = ["# Canary Verification Report", ""]
    for g in gates:
        lines.append(f"## Platform: {g.platform}")
        lines.append(f"- **Artifact ID**: {g.artifact_id}")
        lines.append(f"- **Result**: {g.result}")
        lines.append(f"- **Policy SHA256**: {g.policy_sha256}")
        lines.append(f"- **Bundle Tree SHA256**: {g.bundle_tree_sha256}")
        lines.append(f"- **Runtime Tree SHA256**: {g.runtime_tree_sha256}")
        lines.append(f"- **Ledger Through**: {g.ledger_through}")
        lines.append(f"- **Ledger Records**: {g.ledger_records}")
        lines.append("")
        if g.reasons:
            lines.append("### Reasons")
            for reason in g.reasons:
                lines.append(f"- {reason}")
            lines.append("")
        lines.append("### Tally Summary")
        lines.append(f"- Active Days: {len(g.tally.active_days)}")
        lines.append(f"- Submits: {g.tally.submits}")
        lines.append(
            f"- submits_unknown_result: {g.tally.submits_unknown_result} (evicted: {g.tally.submits_result_evicted})"
        )
        lines.append(f"- pre_freeze_ignored: {g.tally.pre_freeze_ignored}")
        lines.append(f"- unreferenced: {g.tally.unreferenced}")
        lines.append(f"- policy_mismatch: {g.tally.policy_mismatch}")
        if g.tally.tree_mismatch > 0:
            lines.append(f"- tree mismatch: {g.tally.tree_mismatch} submits on other bundle tree (not counted)")
        lines.append(f"- Cycles Total: {g.tally.cycles_total}")
        for ctype, count in sorted(g.tally.cycles_by_type.items()):
            lines.append(f"  - {ctype}: {count}")
        if g.tally.cycles_truncated:
            lines.append("- Cycles Truncated:")
            for ctype, count in sorted(g.tally.cycles_truncated.items()):
                lines.append(f"  - {ctype}: {count}")
        if g.tally.open_incidents:
            lines.append(f"- Open Incidents: {', '.join(g.tally.open_incidents)}")
        if g.tally.failing_incidents:
            lines.append(f"- Failing Incidents: {', '.join(g.tally.failing_incidents)}")
        if g.tally.carried_over:
            lines.append(f"- Carried Over: {g.tally.carried_over}")
        lines.append("")
    return "\n".join(lines)
