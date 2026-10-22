"""P9-T12 Canary policy 凍結、帳本彙總與配額 gate 的紅燈測試。"""

from __future__ import annotations

import copy
import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

def _fakes():
    """延遲載入 Canary 合成資料，避免實作前的全套收集失敗。"""
    return importlib.import_module("fakes_canary")


def _base_fakes():
    return importlib.import_module("stage3_fakes")


def _ledger():
    """延遲載入待實作的帳本模組，使紅燈留在測試執行階段。"""
    return importlib.import_module("tools.stage3.canary_ledger")


def _policy_module():
    return importlib.import_module("tools.stage3.canary_policy")


def _pol(tmp_path, **over: Any):
    fakes = _base_fakes()
    return _policy_module().load_policy(fakes.write_policy(tmp_path / "p.json", **over))


def _tree() -> dict[str, str]:
    fakes = _fakes()
    return {"bundle_tree_sha256": fakes.BTREE, "runtime_tree_sha256": fakes.RTREE}


def _rehash(record: dict) -> dict:
    jsonio = importlib.import_module("tools.stage3.jsonio")
    record["record_id"] = jsonio.record_id(record)
    return record


def _pid_change_pair(day: int = 15) -> list[dict]:
    fakes = _base_fakes()
    return [
        fakes.snapshot(fakes.ts(day, 18, 0, 30), pid=100),
        fakes.snapshot(fakes.ts(day, 18, 1), pid=101),
    ]


def _split(records: list[dict], record_type: str) -> list[dict]:
    return [record for record in records if record["type"] == record_type]


def _record_time(record: dict) -> str:
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
    return str(record[fields[record["type"]]])


def _ledger_version(records: list[dict], platform: str) -> tuple[str | None, int]:
    jsonio = importlib.import_module("tools.stage3.jsonio")
    rows = [record for record in records if record.get("platform") == platform]
    if not rows:
        return None, 0
    latest = max((jsonio.parse_iso(_record_time(row)) for row in rows)).isoformat()
    return latest, len({row["record_id"] for row in rows})


def _append_anomaly(records: list[dict], *, day: int = 15) -> dict:
    fakes = _base_fakes()
    anomaly = _rehash(
        {
            "type": "harvest_anomaly",
            "host_id": "h1",
            "platform": "linux",
            "detail": "crc_mismatch:raw.wal.ndjson:120",
            "t_wall": fakes.ts(day, 18, 1),
        }
    )
    records.append(anomaly)
    return anomaly


def _close_detected(records: list[dict], tmp_path, *, resolution: str, issue_ref: str | None = None) -> dict:
    fakes = _base_fakes()
    incidents_module = importlib.import_module("tools.stage3.canary_incidents")
    policy = _policy_module().load_policy(fakes.write_policy(tmp_path / "incident-policy.json"))
    snapshots = _split(records, "snapshot")
    signals = _split(records, "wal_signal")
    notes = _split(records, "note")
    incidents = incidents_module.detect(snapshots, signals, notes, policy, anomalies=_split(records, "harvest_anomaly"))
    incident = next(item for item in incidents if item.kind in {"unexpected_restart", "unexplained_pid_change"})
    closed_at = fakes.ts(20, 19)
    close_record = incidents_module.close(
        tmp_path / "incident-ledger",
        incident.incident_id,
        resolution=resolution,
        issue_ref=issue_ref,
        note="依證據完成分診",
        closed_by="owner",
        now=closed_at,
        known={incident.incident_id: incident},
    )
    records.append(close_record)
    return close_record


def test_unfrozen_policy_never_passes(tmp_path):
    pol = _pol(
        tmp_path,
        status="draft",
        frozen_at=None,
        frozen_by=None,
        decision_ref=None,
        **{"platforms.linux.evidence_owner": None, "platforms.windows.evidence_owner": None},
    )
    gate = _ledger().gate([], pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "POLICY_NOT_FROZEN"


def test_quota_complete_passes(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={
            "handoff_release_attach": 10,
            "hotplug_reattach": 5,
            "dut_reboot": 10,
            "recovery": 10,
            "human_agent_coexist": 15,
        },
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "PASS", gate.reasons
    assert gate.tally.submits == 5040 and len(gate.tally.active_days) == 14 and gate.tally.cycles_total == 50
    assert (gate.bundle_tree_sha256, gate.runtime_tree_sha256) == (fakes.BTREE, fakes.RTREE)
    assert gate.tally.tree_mismatch == 0


def test_policy_edit_after_freeze_detected(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    edited = _pol(tmp_path, **{"active_day.min_distinct_submits": 5})
    gate = _ledger().gate(recs, edited, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "POLICY_CHANGED"
    assert any("HARNESS_CANARY_POLICY_CHANGED" in reason for reason in gate.reasons)


def test_other_artifact_not_merged(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 50},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-2", **_tree())
    assert gate.result == "INCOMPLETE" and gate.tally.submits == 0


def test_sideloaded_install_same_manifest_not_counted(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    side = {"bundle_tree": "e" * 64, "runtime_tree": "f" * 64}
    cycles = {
        "handoff_release_attach": 10,
        "hotplug_reattach": 5,
        "dut_reboot": 10,
        "recovery": 10,
        "human_agent_coexist": 15,
    }
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles=cycles,
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
        **side,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "INCOMPLETE"
    assert gate.tally.submits == 0 and gate.tally.cycles_total == 0 and gate.tally.active_days == ()
    assert gate.tally.tree_mismatch == 5040

    clean = fakes.synthetic_canary(
        days=7,
        submits_per_day=360,
        cycles=cycles,
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    dirty = fakes.synthetic_canary(
        days=7,
        submits_per_day=360,
        cycles=cycles,
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
        start_day=9,
        host="h2",
        **side,
    )
    mixed = _ledger().gate(clean + dirty, pol, platform="linux", artifact_id="art-1", **_tree())
    assert mixed.result == "INCOMPLETE"
    assert len(mixed.tally.active_days) == 7 and mixed.tally.submits == 2520
    assert mixed.tally.tree_mismatch == 2520

    side_gate = _ledger().gate(
        recs,
        pol,
        platform="linux",
        artifact_id="art-1",
        bundle_tree_sha256="e" * 64,
        runtime_tree_sha256="f" * 64,
    )
    assert side_gate.result == "PASS" and side_gate.bundle_tree_sha256 == "e" * 64


def test_per_type_minimum_blocks(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 50},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "INCOMPLETE"
    assert "cycle type handoff_release_attach: 0/10" in gate.reasons
    assert gate.tally.cycles_total == 20


def test_type_share_cap(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 40, "dut_reboot": 10, "handoff_release_attach": 10},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.tally.cycles_total == 40


def test_unknown_result_not_counted(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=1,
        submits_per_day=30,
        cycles={},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    baseline = _ledger().tally(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    results = sorted(_split(recs, "submit_result"), key=lambda row: row["cmd_id"])
    for result, reason in zip(results[:2], ("CMD_NOT_FOUND", "STALE_NON_TERMINAL")):
        row = copy.deepcopy(result)
        row["status"] = "unknown"
        row["reason"] = reason
        _rehash(row)
        recs[recs.index(result)] = row
    tally = _ledger().tally(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert tally.submits == baseline.submits - 2
    assert tally.submits_unknown_result == 2
    assert tally.submits_result_evicted == 1


def test_unreferenced_records_not_counted(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=1,
        submits_per_day=30,
        cycles={},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    recs = [record for record in recs if record["type"] != "policy_ref"]
    tally = _ledger().tally(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert tally.submits == 0 and tally.unreferenced > 0


def test_open_incident_blocks(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    recs.extend(_pid_change_pair())
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "INCOMPLETE"
    assert any(reason.startswith("open incidents:") for reason in gate.reasons)


def test_harvest_anomaly_blocks(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    _append_anomaly(recs)
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "INCOMPLETE"
    assert any(reason.startswith("open incidents:") for reason in gate.reasons)


def test_closed_not_product_defect_unblocks(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    recs.extend(_pid_change_pair())
    _close_detected(recs, tmp_path, resolution="environment")
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "PASS"


def test_gate_reports_ledger_version(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert (gate.ledger_through, gate.ledger_records) == _ledger_version(recs, "linux")

    recs.extend(_pid_change_pair())
    later_gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    earlier_time, earlier_records = gate.ledger_through, gate.ledger_records
    assert later_gate.ledger_through > earlier_time
    assert later_gate.ledger_records == earlier_records + 2
    assert later_gate.result == "INCOMPLETE"

    windows = _rehash(
        {
            "type": "snapshot",
            **{
                key: value
                for key, value in fakes.snapshot(
                    fakes.ts(30, 12), host="win-host", platform="windows"
                ).items()
                if key != "record_id"
            },
        }
    )
    recs.append(windows)
    windows_gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert (windows_gate.ledger_through, windows_gate.ledger_records) == (later_gate.ledger_through, later_gate.ledger_records)

    close_record = _close_detected(recs, tmp_path, resolution="environment")
    closed_gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert closed_gate.ledger_through == close_record["closed_at"]
    assert closed_gate.ledger_records == later_gate.ledger_records + 1

    empty_gate = _ledger().gate([], pol, platform="linux", artifact_id="art-1", **_tree())
    assert empty_gate.ledger_through is None and empty_gate.ledger_records == 0


def test_fixed_in_artifact_fails_same_artifact(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    recs.extend(_pid_change_pair())
    _close_detected(recs, tmp_path, resolution="fixed_in_artifact", issue_ref="issue-123")
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "FAIL"


def test_safety_violation_fails(tmp_path):
    fakes = _fakes()
    base_fakes = _base_fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    recs.append(
        _rehash(
            {
                "type": "wal_signal",
                "host_id": "h1",
                "platform": "linux",
                "kind": "unauthorized_tx",
                "com": "COM0",
                "wall_ts": base_fakes.ts(10, 12),
                "seq": 99999,
                "source": "agent:unauthorized",
                "detail": {},
            }
        )
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "FAIL"


def test_pre_freeze_records_ignored(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path, frozen_at=fakes.ts(6, 0))
    recs = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    gate = _ledger().gate(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "INCOMPLETE"
    assert gate.tally.pre_freeze_ignored > 0
    assert len(gate.tally.active_days) == 10


def test_gate_asserts_frozen_before_first_counted(tmp_path, monkeypatch):
    fakes = _fakes()
    policy_module = _policy_module()
    ledger = _ledger()
    pol = _pol(tmp_path)
    first_counted = fakes.ts(2, 0)
    calls: list[str] = []
    original = policy_module.assert_frozen_before

    def spy(policy, first_counted_wall_ts):
        calls.append(first_counted_wall_ts)
        return original(policy, first_counted_wall_ts)

    monkeypatch.setattr(ledger, "tally", lambda *args, **kwargs: SimpleNamespace(first_counted_wall=first_counted))
    monkeypatch.setattr(policy_module, "assert_frozen_before", spy)
    gate = ledger.gate([], pol, platform="linux", artifact_id="art-1", **_tree())
    assert gate.result == "POLICY_NOT_FROZEN"
    assert calls == [first_counted]


def test_carryover_excludes_active_days(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=1,
        submits_per_day=30,
        cycles={},
        platform="linux",
        artifact="art-old",
        policy_sha256=pol.sha256,
    )
    carryover = {
        "type": "carryover",
        "host_id": "h1",
        "platform": "linux",
        "from_artifact": "art-old",
        "to_artifact": "art-new",
        "counters": ["submits"],
        "approved_by": "owner",
        "decision_ref": "OD-10-C2",
        "approved_at": fakes.ts(3, 0),
    }
    carryover = _rehash(carryover)
    without = _ledger().gate(recs, pol, platform="linux", artifact_id="art-new", **_tree())
    with_carryover = _ledger().gate(
        recs,
        pol,
        platform="linux",
        artifact_id="art-new",
        carryovers=[carryover],
        **_tree(),
    )
    assert with_carryover.tally.submits > without.tally.submits
    assert with_carryover.tally.active_days == without.tally.active_days


def test_carryover_without_approval_ignored(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=1,
        submits_per_day=30,
        cycles={},
        platform="linux",
        artifact="art-old",
        policy_sha256=pol.sha256,
    )
    carryover = _rehash(
        {
            "type": "carryover",
            "host_id": "h1",
            "platform": "linux",
            "from_artifact": "art-old",
            "to_artifact": "art-new",
            "counters": ["submits"],
            "approved_by": "",
            "decision_ref": "OD-10-C2",
            "approved_at": fakes.ts(3, 0),
        }
    )
    without = _ledger().gate(recs, pol, platform="linux", artifact_id="art-new", **_tree())
    ignored = _ledger().gate(
        recs,
        pol,
        platform="linux",
        artifact_id="art-new",
        carryovers=[carryover],
        **_tree(),
    )
    assert ignored.tally.submits == without.tally.submits
    assert ignored.tally.carried_over == {}


def test_windows_independent(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    linux = fakes.synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    linux_gate = _ledger().gate(linux, pol, platform="linux", artifact_id="art-1", **_tree())
    windows_gate = _ledger().gate([], pol, platform="windows", artifact_id="art-1", **_tree())
    assert linux_gate.result == "PASS"
    assert windows_gate.result == "INCOMPLETE"


def test_gate_tree_args_required(tmp_path):
    pol = _pol(tmp_path)
    with pytest.raises(TypeError):
        _ledger().gate([], pol, platform="linux", artifact_id="art-1")


def test_tree_change_mid_episode_not_counted(tmp_path):
    fakes = _fakes()
    base_fakes = _base_fakes()
    pol = _pol(tmp_path)
    recs = [
        record
        for record in fakes.synthetic_canary(
            days=1,
            submits_per_day=0,
            cycles={},
            platform="linux",
            artifact="art-1",
            policy_sha256=pol.sha256,
        )
        if record["type"] == "policy_ref"
    ]
    recs.extend(
        [
            base_fakes.snapshot(base_fakes.ts(2, 6, 0), sessions=(("COM0", "READY", 1),)),
            base_fakes.snapshot(
                base_fakes.ts(2, 6, 0, 30),
                sessions=(("COM0", "RELEASED", 1),),
                bundle_tree=None,
                runtime_tree=None,
            ),
            base_fakes.snapshot(
                base_fakes.ts(2, 6, 1),
                sessions=(("COM0", "ATTACHING", 1),),
                bundle_tree="e" * 64,
                runtime_tree="f" * 64,
            ),
            base_fakes.snapshot(
                base_fakes.ts(2, 6, 1, 30),
                sessions=(("COM0", "READY", 1),),
                bundle_tree="e" * 64,
                runtime_tree="f" * 64,
            ),
        ]
    )
    cmd_id = base_fakes.uuid_cmd_id(1)
    recs.extend(
        [
            _rehash(
                {
                    "type": "submit",
                    "host_id": "h1",
                    "platform": "linux",
                    "cmd_id": cmd_id,
                    "com": "COM0",
                    "source": "agent:synthetic",
                    "wall_ts": base_fakes.ts(2, 6, 0, 45),
                    "seq": 1,
                    "wal_file": "raw.wal.ndjson",
                }
            ),
            _rehash(
                {
                    "type": "submit_result",
                    "host_id": "h1",
                    "platform": "linux",
                    "cmd_id": cmd_id,
                    "status": "done",
                    "error_code": None,
                    "t_wall": base_fakes.ts(2, 6, 0, 46),
                }
            ),
        ]
    )
    tally = _ledger().tally(recs, pol, platform="linux", artifact_id="art-1", **_tree())
    assert tally.cycles_by_type.get("handoff_release_attach", 0) == 0
    assert tally.submits == 0 and tally.tree_mismatch == 0


def test_merge_two_hosts_dedup(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    recs = fakes.synthetic_canary(
        days=1,
        submits_per_day=10,
        cycles={},
        platform="linux",
        artifact="art-1",
        policy_sha256=pol.sha256,
    )
    path = tmp_path / "host.ndjson"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in recs), encoding="utf-8")
    loaded = _ledger().load_records([path, path])
    assert len(loaded) == len({row["record_id"] for row in recs})


def test_corrupt_ledger_rejected(tmp_path):
    from tools.stage3.reason_codes import HarnessError

    path = tmp_path / "corrupt.ndjson"
    path.write_text("{bad\n", encoding="utf-8")
    with pytest.raises(HarnessError) as error:
        _ledger().load_records([path])
    assert error.value.code == "HARNESS_LEDGER_CORRUPT"


def test_report_md_lists_reasons(tmp_path):
    fakes = _fakes()
    pol = _pol(tmp_path)
    gate = _ledger().gate([], pol, platform="linux", artifact_id="art-1", **_tree())
    report = _ledger().report_md([gate])
    assert "linux" in report and "art-1" in report
    for reason in gate.reasons:
        assert reason in report
    assert "submits_unknown_result" in report


def test_freeze_policy_writes_and_appends(tmp_path):
    fakes = _base_fakes()
    policy_module = _policy_module()
    policy_path = fakes.write_policy(
        tmp_path / "policy.json",
        status="draft",
        frozen_at=None,
        frozen_by=None,
        decision_ref=None,
        **{"platforms.linux.evidence_owner": None, "platforms.windows.evidence_owner": None},
    )
    addenda = tmp_path / "owner-decisions-addenda.md"
    addenda.write_text("| OD-09-C1 | 既有決策 | 2026-10-01 | 已凍結 |\n", encoding="utf-8")
    before_rows = [line for line in addenda.read_text(encoding="utf-8").splitlines() if line.startswith("|")]
    result = policy_module.freeze_policy(
        policy_path,
        decision_id="OD-10-C1",
        frozen_by="owner",
        evidence_owners={"linux": "owner-linux", "windows": "owner-windows"},
        addenda_path=addenda,
        now=fakes.ts(2, 0),
    )
    document = json.loads(policy_path.read_text(encoding="utf-8"))
    assert document["status"] == "frozen" and document["decision_ref"] == "OD-10-C1"
    assert document["platforms"]["linux"]["evidence_owner"] == "owner-linux"
    assert document["platforms"]["windows"]["evidence_owner"] == "owner-windows"
    rows = [line for line in addenda.read_text(encoding="utf-8").splitlines() if line.startswith("|")]
    assert len(rows) == len(before_rows) + 1
    added = [line for line in rows if line.startswith("| OD-10-C1 |")]
    assert len(added) == 1 and f"policy_sha256={result['policy_sha256']}" in added[0]
    loaded = policy_module.load_policy(policy_path, addenda_path=addenda)
    assert loaded.sha256 == result["policy_sha256"]
    assert result["decision_ref"] == "OD-10-C1" and result["frozen_at"] == fakes.ts(2, 0)


def test_freeze_policy_refuses_refreeze(tmp_path):
    from tools.stage3.reason_codes import HarnessError

    fakes = _base_fakes()
    policy_module = _policy_module()
    policy_path = fakes.write_policy(tmp_path / "policy.json")
    with pytest.raises(HarnessError) as error:
        policy_module.freeze_policy(
            policy_path,
            decision_id="OD-10-C1",
            frozen_by="owner",
            evidence_owners={"linux": "owner-linux", "windows": "owner-windows"},
            addenda_path=tmp_path / "owner-decisions-addenda.md",
            now=fakes.ts(2, 0),
        )
    assert error.value.code == "HARNESS_CANARY_POLICY_INVALID"
