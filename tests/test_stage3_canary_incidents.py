# tests/test_stage3_canary_incidents.py
"""P9-T11 事故追蹤（tools/stage3/canary_incidents.py）的契約測試。

依 10-stage3-canary.md Task 11 Step 1 的測試表撰寫：自動偵測規則、note 抑制、已佐證主機重開、
安全不變量不可以環境原因結案、結案驗證與人工開案紀錄。政策取自 stage3_fakes.write_policy（frozen、
collector interval 30 s、gap 900 s、post-boot grace 300 s、reboot corroboration 60 s、不可達 3 次、
卡住 900 s）。快照的 `error` 欄位由 `_with_error` 補上，因 stage3_fakes.snapshot 不提供該參數。
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tools.stage3.reason_codes import HarnessError


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BASE = datetime(2026, 11, 3, 1, 0, 0, tzinfo=timezone.utc)


def _fakes():
    return importlib.import_module("stage3_fakes")


def _ci():
    return importlib.import_module("tools.stage3.canary_incidents")


def _jsonio():
    return importlib.import_module("tools.stage3.jsonio")


def _at(offset_s: int) -> str:
    """BASE（2026-11-03 01:00:00 UTC）起算 offset_s 秒的 ISO 時間。"""
    return (BASE + timedelta(seconds=offset_s)).isoformat()


@pytest.fixture
def pol(tmp_path):
    policy_mod = importlib.import_module("tools.stage3.canary_policy")
    return policy_mod.load_policy(_fakes().write_policy(tmp_path / "p.json"))


def _snap(t_wall: str, **kw: Any) -> dict[str, Any]:
    return _fakes().snapshot(t_wall, **kw)


def _with_error(snap: dict[str, Any], error: str) -> dict[str, Any]:
    out = dict(snap)
    out["error"] = error
    out["record_id"] = _jsonio().record_id(out)
    return out


def _sig(kind: str, *, t: str, seq: int = 1, host: str = "h1", com: str | None = "COM0",
         source: str = "agent:tester", detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "wal_signal",
        "kind": kind,
        "host_id": host,
        "platform": "linux",
        "com": com,
        "wall_ts": t,
        "seq": seq,
        "source": source,
        "detail": detail or {},
    }


def _anomaly(detail: str, t: str, host: str = "h1") -> dict[str, Any]:
    return {"type": "harvest_anomaly", "host_id": host, "platform": "linux", "detail": detail, "t_wall": t}


def _kinds(incidents) -> list[str]:
    return sorted(incident.kind for incident in incidents)


def test_supervisor_recovered_restart_is_incident(pol):
    s0 = _snap(_fakes().ts(3, 1, 0, 0), pid=100)
    s1 = _snap(_fakes().ts(3, 1, 0, 30), pid=101)
    inc = _ci().detect([s0, s1], [], [], pol)
    assert [i.kind for i in inc] == ["unexpected_restart"]
    assert inc[0].status == "open" and inc[0].detected_by == "auto"


def test_planned_note_suppresses(pol):
    s0 = _snap(_fakes().ts(3, 1, 0, 0), pid=100)
    s1 = _snap(_fakes().ts(3, 1, 0, 30), pid=101)
    note = {"type": "note", "kind": "planned_restart", "from": _fakes().ts(3, 0, 55),
            "to": _fakes().ts(3, 1, 5), "host_id": "h1"}
    assert _ci().detect([s0, s1], [], [note], pol) == []


def test_note_written_by_write_note_suppresses(tmp_path, pol):
    s0 = _snap(_at(0), pid=100)
    s1 = _snap(_at(30), pid=101)
    note = _ci().write_note(tmp_path, kind="planned_restart", start=_at(-300), end=_at(300),
                            reason="韌體升級", author="owner", host_id="h1", platform="linux")
    assert note["type"] == "note" and len(note["record_id"]) == 20
    assert _ci().detect([s0, s1], [], [note], pol) == []


def test_safety_violation_cannot_close_as_environment(tmp_path, pol):
    inc = _ci().open_manual(tmp_path, kind="safety_violation", platform="linux", host_id="h1", artifact_id="art-1",
                            summary="unauthorized TX", evidence=["wal:120"], now=_fakes().ts(3, 2))
    with pytest.raises(HarnessError) as ei:
        _ci().close(tmp_path, inc.incident_id, resolution="environment", issue_ref=None, note="x",
                    closed_by="owner", now=_fakes().ts(3, 3), known={inc.incident_id: inc})
    assert ei.value.code == "HARNESS_INCIDENT_INVALID_CLOSE"


def test_host_reboot_not_incident(pol):
    s0 = _snap(_at(0), boot="b1", pid=100)
    s1 = _snap(_at(120), boot="b2", pid=101)
    assert [i.kind for i in _ci().detect([s0, s1], [], [], pol)] == []


def test_boot_change_without_gap_is_incident(pol):
    s0 = _snap(_at(0), boot="b1", pid=100)
    s1 = _snap(_at(30), boot="b2", pid=101)
    assert _kinds(_ci().detect([s0, s1], [], [], pol)) == ["unexpected_restart"]


def test_boot_id_none_is_incident(pol):
    s0 = _snap(_at(0), boot=None, pid=100)
    s1 = _snap(_at(30), boot=None, pid=101)
    assert _kinds(_ci().detect([s0, s1], [], [], pol)) == ["unexpected_restart"]


def test_pid_change_across_down_polls(pol):
    s0 = _snap(_at(0), pid=100)
    d1 = _snap(_at(30), pid=None, ok=False, sessions=())
    d2 = _snap(_at(60), pid=None, ok=False, sessions=())
    s3 = _snap(_at(90), pid=101)
    assert _kinds(_ci().detect([s0, d1, d2, s3], [], [], pol)) == ["unexpected_restart"]

    # 20 分鐘內三次失聯（不可達）後 PID 變更：非預期重啟的 90 s 窗口已不適用。
    d_long = [_snap(_at(t), pid=None, ok=False, sessions=()) for t in (300, 600, 900)]
    s_late = _snap(_at(1200), pid=101)
    assert _kinds(_ci().detect([s0, *d_long, s_late], [], [], pol)) == ["daemon_unreachable", "unexplained_pid_change"]


def test_pid_reuse_after_not_running(pol):
    s0 = _snap(_at(0), pid=100)
    dead = _with_error(_snap(_at(30), pid=None, ok=False, sessions=()), "not_running")
    s2 = _snap(_at(60), pid=100)
    assert _kinds(_ci().detect([s0, dead, s2], [], [], pol)) == ["unexplained_pid_change"]


def test_harvest_anomaly_is_incident(pol):
    anomalies = [_anomaly("crc_mismatch:raw.wal.ndjson:120", _at(0))]
    inc = _ci().detect([], [], [], pol, anomalies=anomalies)
    assert _kinds(inc) == ["harvest_integrity"]
    assert inc[0].incident_id == _ci().detect([], [], [], pol, anomalies=anomalies)[0].incident_id


def test_non_integrity_anomaly_is_not_incident(pol):
    anomalies = [_anomaly("result_evicted:cmd-abc", _at(0))]
    assert _ci().detect([], [], [], pol, anomalies=anomalies) == []


def test_unauthorized_tx_auto_safety(pol):
    inc = _ci().detect([], [_sig("unauthorized_tx", t=_at(0), seq=7, source="agent:bad")], [], pol)
    assert _kinds(inc) == ["safety_violation"]
    assert inc[0].detected_by == "auto"


def test_resend_across_generation_is_safety(pol):
    snaps = [
        _snap(_at(0), sessions=(("COM0", "READY", 1),)),
        _snap(_at(30), sessions=(("COM0", "READY", 2),)),
        _snap(_at(60), sessions=(("COM0", "READY", 2),)),
    ]
    sig = _sig("resend_suspect", t=_at(60), seq=9, detail={"crc32": 1, "len": 10, "previous_wall_ts": _at(0)})
    assert _kinds(_ci().detect(snaps, [sig], [], pol)) == ["safety_violation"]


def test_resend_without_context_crossing_is_other(pol):
    snaps = [
        _snap(_at(0), sessions=(("COM0", "READY", 1),)),
        _snap(_at(30), sessions=(("COM0", "READY", 1),)),
        _snap(_at(60), sessions=(("COM0", "READY", 1),)),
    ]
    sig = _sig("resend_suspect", t=_at(60), seq=9, detail={"crc32": 1, "len": 10, "previous_wall_ts": _at(0)})
    assert _kinds(_ci().detect(snaps, [sig], [], pol)) == ["other"]


def test_resend_without_previous_wall_is_safety(pol):
    """前次時間不可得即無法排除跨上下文重送，保守歸為安全不變量（不可以環境原因結案）。"""
    snaps = [_snap(_at(0), sessions=(("COM0", "READY", 1),)), _snap(_at(30), sessions=(("COM0", "READY", 2),))]
    sig = _sig("resend_suspect", t=_at(30), seq=9, detail={"crc32": 1, "len": 10})
    assert _kinds(_ci().detect(snaps, [sig], [], pol)) == ["safety_violation"]


def test_resend_with_unknown_com_is_safety(pol):
    snaps = [_snap(_at(0), sessions=(("COM0", "READY", 1),)), _snap(_at(30), sessions=(("COM0", "READY", 2),))]
    sig = _sig("resend_suspect", t=_at(30), seq=9, com=None, detail={"crc32": 1, "len": 10, "previous_wall_ts": _at(0)})
    assert _kinds(_ci().detect(snaps, [sig], [], pol)) == ["safety_violation"]


def test_human_replay_without_previous_wall_is_other(pol):
    snaps = [_snap(_at(0), sessions=(("COM0", "READY", 1),)), _snap(_at(30), sessions=(("COM0", "READY", 1),))]
    sig = _sig("human_replay_suspect", t=_at(30), seq=11, detail={"crc32": 3, "len": 12})
    assert _kinds(_ci().detect(snaps, [sig], [], pol)) == ["other"]


def test_signal_artifact_follows_nearest_snapshot(pol):
    snaps = [_snap(_at(0), artifact="art-1", pid=100), _snap(_at(60), artifact="art-2", pid=100)]
    sigs = [
        _sig("unauthorized_tx", t=_at(30), seq=1, source="agent:bad"),
        _sig("unauthorized_tx", t=_at(90), seq=2, source="agent:bad"),
    ]
    inc = _ci().detect(snaps, sigs, [], pol)
    assert sorted((i.opened_at, i.artifact_id) for i in inc) == [(_at(30), "art-1"), (_at(90), "art-2")]


def test_human_tx_signal_is_not_incident(pol):
    assert _ci().detect([_snap(_at(0)), _snap(_at(30))], [_sig("human_tx", t=_at(10), source="human:x")], [], pol) == []


def test_down_inside_grace_stays_down_past_grace_is_unreachable(pol):
    """grace 內失聯、grace 結束後仍持續 8 分鐘：不可因區段起點位於 grace 內而整段遮蔽。"""
    s0 = _snap(_at(0), boot="b1", pid=100)
    s1 = _snap(_at(120), boot="b2", pid=200)
    downs = [_snap(_at(t), boot="b2", pid=None, ok=False, sessions=()) for t in range(150, 901, 30)]
    assert _kinds(_ci().detect([s0, s1, *downs], [], [], pol)) == ["daemon_unreachable"]


def test_down_after_grace_long_outage_is_unreachable(pol):
    """grace 內 daemon 正常、之後每 30 s 失聯共 2 小時 → 須開 daemon_unreachable（不得因 grace 被吞掉）。"""
    s0 = _snap(_at(0), boot="b1", pid=100)
    s1 = _snap(_at(120), boot="b2", pid=200)
    downs = [_snap(_at(t), boot="b2", pid=None, ok=False, sessions=()) for t in range(150, 7201, 30)]
    assert _kinds(_ci().detect([s0, s1, *downs], [], [], pol)) == ["daemon_unreachable"]


def test_down_entirely_inside_grace_is_not_unreachable(pol):
    s0 = _snap(_at(0), boot="b1", pid=100)
    s1 = _snap(_at(120), boot="b2", pid=200)
    downs = [_snap(_at(t), boot="b2", pid=None, ok=False, sessions=()) for t in range(150, 421, 30)]
    assert _ci().detect([s0, s1, *downs], [], [], pol) == []


def test_planned_downtime_ends_then_outage_continues_is_unreachable(pol):
    """計畫停機窗結束後 daemon 仍持續不可達：窗外的失聯須獨立開事故。"""
    s0 = _snap(_at(0), pid=100)
    note = {"type": "note", "kind": "planned_downtime", "from": _at(0), "to": _at(600), "host_id": "h1"}
    downs = [_snap(_at(t), pid=None, ok=False, sessions=()) for t in range(30, 1201, 30)]
    back = _snap(_at(1230), pid=100)
    assert _kinds(_ci().detect([s0, *downs, back], [], [note], pol)) == ["daemon_unreachable"]


def test_planned_downtime_partial_cover_still_incident(pol):
    s0 = _snap(_at(0), pid=100)
    note = {"type": "note", "kind": "planned_downtime", "from": _at(90), "to": _at(200), "host_id": "h1"}
    downs = [_snap(_at(t), pid=None, ok=False, sessions=()) for t in range(100, 3701, 30)]
    back = _snap(_at(3730), pid=100)
    assert _kinds(_ci().detect([s0, *downs, back], [], [note], pol)) == ["daemon_unreachable"]


def test_planned_downtime_full_cover_is_not_incident(pol):
    s0 = _snap(_at(0), pid=100)
    note = {"type": "note", "kind": "planned_downtime", "from": _at(90), "to": _at(3800), "host_id": "h1"}
    downs = [_snap(_at(t), pid=None, ok=False, sessions=()) for t in range(100, 3701, 30)]
    back = _snap(_at(3730), pid=100)
    assert _ci().detect([s0, *downs, back], [], [note], pol) == []


def test_later_boot_does_not_hide_supervisor_failure(pol):
    """b2 始終未恢復；之後 b3 的健康快照不能代表 b2 已成功啟動（兩次重開皆已佐證）。"""
    s0 = _snap(_at(0), boot="b1", pid=100)
    d1 = _snap(_at(120), boot="b2", pid=None, ok=False, sessions=())
    d2 = _snap(_at(150), boot="b2", pid=None, ok=False, sessions=())
    s3 = _snap(_at(240), boot="b3", pid=100)
    assert _kinds(_ci().detect([s0, d1, d2, s3], [], [], pol)) == ["supervisor_failed_to_start"]


def test_stuck_clock_survives_single_failed_poll(pol):
    """單次 daemon 不可達不重置卡住時鐘（同 pid、未確認 daemon 不存在）。"""
    snaps = [
        _snap(_at(t), pid=None, ok=False, sessions=()) if t == 900
        else _snap(_at(t), sessions=(("COM0", "ATTACHED", 1, {"command_capable": True}),))
        for t in range(0, 1801, 30)
    ]
    assert _kinds(_ci().detect(snaps, [], [], pol)) == ["stuck_attached"]


def test_stuck_clock_restarts_after_confirmed_dead_poll(pol):
    """daemon 確認不存在（not_running）後重新出現：PID 類事故成立，卡住時鐘不得跨越該斷點累計。"""
    snaps = [_snap(_at(t), sessions=(("COM0", "ATTACHED", 1, {"command_capable": True}),)) for t in range(0, 801, 30)]
    snaps.append(_with_error(_snap(_at(830), pid=None, ok=False, sessions=()), "not_running"))
    snaps += [_snap(_at(t), sessions=(("COM0", "ATTACHED", 1, {"command_capable": True}),)) for t in range(860, 1701, 30)]
    assert _kinds(_ci().detect(snaps, [], [], pol)) == ["unexplained_pid_change"]


def test_detect_is_order_independent_for_duplicate_keys(pol):
    early = _anomaly("crc_mismatch:raw.wal.ndjson:120", _at(10))
    late = _anomaly("crc_mismatch:raw.wal.ndjson:120", _at(100))
    forward = _ci().detect([], [], [], pol, anomalies=[early, late])
    backward = _ci().detect([], [], [], pol, anomalies=[late, early])
    assert forward == backward
    assert len(forward) == 1 and forward[0].opened_at == _at(10)


def test_write_note_rejects_start_not_before_end(tmp_path, pol):
    for start, end in ((_at(60), _at(60)), (_at(120), _at(60))):
        with pytest.raises(ValueError):
            _ci().write_note(tmp_path, kind="planned_restart", start=start, end=end, reason="韌體升級", author="owner",
                             host_id="h1", platform="linux")


def test_write_note_rejects_empty_reason(tmp_path, pol):
    with pytest.raises(ValueError):
        _ci().write_note(tmp_path, kind="planned_restart", start=_at(0), end=_at(60), reason="   ", author="owner",
                         host_id="h1", platform="linux")


def test_manual_records_reject_unknown_platform_and_empty_host(tmp_path, pol):
    with pytest.raises(ValueError):
        _ci().write_note(tmp_path, kind="planned_restart", start=_at(0), end=_at(60), reason="x", author="owner",
                         host_id="h1", platform="Linux")
    with pytest.raises(ValueError):
        _ci().open_manual(tmp_path, kind="multi_open", platform="Linux", host_id="h1", artifact_id=None,
                          summary="x", evidence=[], now=_at(0))
    with pytest.raises(ValueError):
        _ci().open_manual(tmp_path, kind="multi_open", platform="linux", host_id="", artifact_id=None,
                          summary="x", evidence=[], now=_at(0))
    assert not (tmp_path / "ledger.ndjson").exists()


def test_human_replay_across_recovering_is_safety(pol):
    snaps = [
        _snap(_at(0), sessions=(("COM0", "READY", 1),)),
        _snap(_at(30), sessions=(("COM0", "RECOVERING", 1),)),
        _snap(_at(60), sessions=(("COM0", "READY", 1),)),
    ]
    sig = _sig("human_replay_suspect", t=_at(60), seq=11, detail={"crc32": 3, "len": 12, "previous_wall_ts": _at(0)})
    assert _kinds(_ci().detect(snaps, [sig], [], pol)) == ["safety_violation"]


def test_human_replay_without_recovering_is_not_incident(pol):
    snaps = [
        _snap(_at(0), sessions=(("COM0", "READY", 1),)),
        _snap(_at(30), sessions=(("COM0", "READY", 1),)),
        _snap(_at(60), sessions=(("COM0", "READY", 1),)),
    ]
    sig = _sig("human_replay_suspect", t=_at(60), seq=11, detail={"crc32": 3, "len": 12, "previous_wall_ts": _at(0)})
    assert _ci().detect(snaps, [sig], [], pol) == []


def test_supervisor_failed_after_boot(pol):
    s0 = _snap(_at(0), boot="b1", pid=100)
    downs = [_snap(_at(t), boot="b2", pid=None, ok=False, sessions=()) for t in range(120, 481, 30)]
    assert _kinds(_ci().detect([s0, *downs], [], [], pol)) == ["supervisor_failed_to_start"]


def test_supervisor_recovered_after_boot_is_not_incident(pol):
    s0 = _snap(_at(0), boot="b1", pid=100)
    downs = [_snap(_at(t), boot="b2", pid=None, ok=False, sessions=()) for t in range(120, 241, 30)]
    back = _snap(_at(240), boot="b2", pid=200)
    later_down = [_snap(_at(t), boot="b2", pid=None, ok=False, sessions=()) for t in range(270, 481, 30)]
    assert _ci().detect([s0, *downs[:-1], back, *later_down], [], [], pol) == []


def test_gap_pid_change_unexplained(pol):
    s0 = _snap(_at(0), pid=100)
    s1 = _snap(_at(1200), pid=101)
    assert _kinds(_ci().detect([s0, s1], [], [], pol)) == ["collector_gap", "unexplained_pid_change"]


def test_unreachable_three_polls(pol):
    s0 = _snap(_at(0), pid=100)
    downs = [_snap(_at(t), pid=None, ok=False, sessions=()) for t in (30, 60, 90)]
    s_back = _snap(_at(120), pid=100)
    assert _kinds(_ci().detect([s0, *downs, s_back], [], [], pol)) == ["daemon_unreachable"]


def test_unreachable_two_polls_is_not_incident(pol):
    s0 = _snap(_at(0), pid=100)
    downs = [_snap(_at(t), pid=None, ok=False, sessions=()) for t in (30, 60)]
    s_back = _snap(_at(90), pid=100)
    assert _ci().detect([s0, *downs, s_back], [], [], pol) == []


def test_stuck_attached_900s(pol):
    snaps = [
        _snap(_at(t), sessions=(("COM0", "ATTACHED", 1, {"command_capable": True}),))
        for t in range(0, 901, 30)
    ]
    assert _kinds(_ci().detect(snaps, [], [], pol)) == ["stuck_attached"]


def test_stuck_attached_below_threshold_is_not_incident(pol):
    snaps = [
        _snap(_at(t), sessions=(("COM0", "ATTACHED", 1, {"command_capable": True}),))
        for t in range(0, 871, 30)
    ]
    assert _ci().detect(snaps, [], [], pol) == []


def test_released_for_a_day_is_not_stuck(pol):
    snaps = [_snap(_at(t), sessions=(("COM0", "RELEASED", 1),)) for t in range(0, 86401, 900)]
    assert _ci().detect(snaps, [], [], pol) == []


def test_passthrough_attached_for_a_day_is_not_stuck(pol):
    extra = {"command_capable": True, "platform": "passthrough", "profile": "lab-probe"}
    snaps = [_snap(_at(t), sessions=(("COM0", "ATTACHED", 1, extra),)) for t in range(0, 86401, 900)]
    assert _ci().detect(snaps, [], [], pol) == []


def test_wal_loss_signal(pol):
    loss = _sig("wal_loss", t=_at(0), seq=5)
    recreated = _sig("wal_recreated", t=_at(0), seq=5)
    assert _kinds(_ci().detect([], [loss], [], pol)) == ["wal_loss"]
    assert _kinds(_ci().detect([], [recreated], [], pol)) == ["wal_loss"]
    assert _kinds(_ci().detect([], [loss, recreated], [], pol)) == ["wal_loss"]


def test_wal_unhealthy_and_multi_open_segments(pol):
    snaps = [
        _snap(_at(0)),
        _snap(_at(30), wal_healthy=False),
        _snap(_at(60), wal_healthy=False),
        _snap(_at(90)),
        _snap(_at(120), multi_open=True),
        _snap(_at(150), multi_open=True),
    ]
    assert _kinds(_ci().detect(snaps, [], [], pol)) == ["multi_open", "wal_unhealthy"]


def test_detect_is_deterministic(pol):
    snaps = [_snap(_at(0), pid=100), _snap(_at(30), pid=101)]
    signals = [_sig("unauthorized_tx", t=_at(40), seq=3, source="agent:bad")]
    anomalies = [_anomaly("crc_mismatch:raw.wal.ndjson:9", _at(50))]
    first = _ci().detect(snaps, signals, [], pol, anomalies=anomalies)
    second = _ci().detect(snaps, signals, [], pol, anomalies=anomalies)
    assert [i.incident_id for i in first] == [i.incident_id for i in second]
    assert first == second


def test_close_requires_issue_for_fix(tmp_path, pol):
    inc = _ci().open_manual(tmp_path, kind="daemon_unreachable", platform="linux", host_id="h1", artifact_id="art-1",
                            summary="人工開案", evidence=["snapshot:x"], now=_at(0))
    with pytest.raises(HarnessError) as ei:
        _ci().close(tmp_path, inc.incident_id, resolution="fixed_in_artifact", issue_ref=None, note="修正",
                    closed_by="owner", now=_at(60), known={inc.incident_id: inc})
    assert ei.value.code == "HARNESS_INCIDENT_INVALID_CLOSE"


def test_close_rejects_bad_resolution_and_empty_note(tmp_path, pol):
    inc = _ci().open_manual(tmp_path, kind="daemon_unreachable", platform="linux", host_id="h1", artifact_id=None,
                            summary="人工開案", evidence=[], now=_at(0))
    known = {inc.incident_id: inc}
    with pytest.raises(HarnessError):
        _ci().close(tmp_path, inc.incident_id, resolution="bogus", issue_ref=None, note="x",
                    closed_by="owner", now=_at(60), known=known)
    with pytest.raises(HarnessError):
        _ci().close(tmp_path, inc.incident_id, resolution="environment", issue_ref=None, note="",
                    closed_by="owner", now=_at(60), known=known)


def test_safety_fixed_with_issue_closes(tmp_path, pol):
    inc = _ci().open_manual(tmp_path, kind="safety_violation", platform="linux", host_id="h1", artifact_id="art-1",
                            summary="未授權 TX", evidence=["wal:4"], now=_at(0))
    record = _ci().close(tmp_path, inc.incident_id, resolution="fixed_in_artifact", issue_ref="#999",
                         note="已於產物修正", closed_by="owner", now=_at(60), known={inc.incident_id: inc})
    assert record["type"] == "incident_close" and record["resolution"] == "fixed_in_artifact"


def test_double_close_rejected(tmp_path, pol):
    inc = _ci().open_manual(tmp_path, kind="daemon_unreachable", platform="linux", host_id="h1", artifact_id=None,
                            summary="人工開案", evidence=[], now=_at(0))
    _ci().close(tmp_path, inc.incident_id, resolution="environment", issue_ref=None, note="排程維護",
                closed_by="owner", now=_at(60), known={inc.incident_id: inc})
    with pytest.raises(HarnessError) as ei:
        _ci().close(tmp_path, inc.incident_id, resolution="environment", issue_ref=None, note="重複",
                    closed_by="owner", now=_at(120), known={inc.incident_id: inc})
    assert ei.value.code == "HARNESS_INCIDENT_INVALID_CLOSE"


def test_current_incidents_applies_close(tmp_path, pol):
    snaps = [_snap(_at(0), pid=100), _snap(_at(30), pid=101)]
    detected = _ci().detect(snaps, [], [], pol)
    inc = detected[0]
    _ci().close(tmp_path, inc.incident_id, resolution="environment", issue_ref=None, note="確認為主機排程重啟",
                closed_by="owner", now=_at(100), known={inc.incident_id: inc})
    records, malformed = _jsonio().read_ndjson(tmp_path / "ledger.ndjson")
    assert malformed == []
    current = _ci().current_incidents(records, detected)
    assert current[inc.incident_id].status == "closed"
    assert current[inc.incident_id].resolution == "environment"
    assert current[inc.incident_id].closed_by == "owner"


def test_manual_incident_roundtrip(tmp_path, pol):
    inc = _ci().open_manual(tmp_path, kind="multi_open", platform="windows", host_id="h2", artifact_id="art-2",
                            summary="人工發現多開", evidence=["snapshot:abc"], now=_at(0))
    records, _ = _jsonio().read_ndjson(tmp_path / "ledger.ndjson")
    assert [r["type"] for r in records] == ["incident_open"]
    current = _ci().current_incidents(records, [])
    assert current[inc.incident_id].status == "open"
    assert current[inc.incident_id].detected_by == "manual"
    assert current[inc.incident_id].platform == "windows"


def test_write_note_rejects_unknown_kind(tmp_path, pol):
    with pytest.raises(ValueError):
        _ci().write_note(tmp_path, kind="bogus", start=_at(0), end=_at(60), reason="x", author="owner",
                         host_id="h1", platform="linux")


def test_incident_kinds_contract():
    ci = _ci()
    assert len(ci.INCIDENT_KINDS) == 12
    assert ci.CLOSE_RESOLUTIONS == ("fixed_in_artifact", "not_product_defect", "environment", "duplicate")
    assert ci.SAFETY_ALLOWED_RESOLUTIONS == ("fixed_in_artifact", "duplicate")
    assert ci.NOTE_KINDS == ("planned_restart", "planned_downtime")
