"""#122 長跑分析器單測——吃合成快照/事件，不碰 live。"""
from __future__ import annotations

from realhw.cases import longrun


def test_analyze_counts_and_stuck_attached():
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY", "COM1": "READY"}, "rss_kb": 50000, "pid": 1},
        {"t": 300, "sessions": {"COM0": "ATTACHED", "COM1": "READY"}, "rss_kb": 51000, "pid": 1},
        {"t": 600, "sessions": {"COM0": "ATTACHED", "COM1": "READY"}, "rss_kb": 52000, "pid": 1},
        {"t": 900, "sessions": {"COM0": "READY", "COM1": "READY"}, "rss_kb": 52000, "pid": 1},
    ]
    events = [
        {"t": 10, "source": "agent:rhw1", "kind": "submit"},
        {"t": 11, "source": "agent:rhw1", "kind": "done"},
        {"t": 20, "source": "agent:rhw2", "kind": "submit"},
        {"t": 30, "source": "agent:rhw2", "kind": "error", "detail": "SESSION_NOT_READY"},
    ]
    a = longrun.analyze(snapshots, events)
    assert a["per_source"]["agent:rhw1"] == {"submit": 1, "done": 1, "error": 0}
    assert a["per_source"]["agent:rhw2"]["error"] == 1
    assert a["stuck_attached"] == [{"com": "COM0", "from_t": 300, "to_t": 900, "duration_s": 600}]
    assert a["pid_changes"] == 0


def test_analyze_flags_daemon_death():
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY"}, "rss_kb": 1, "pid": 1},
        {"t": 300, "sessions": {}, "rss_kb": 0, "pid": 0},
    ]
    a = longrun.analyze(snapshots, [])
    assert a["daemon_death_at"] == 300


def test_analyze_rss_trend_and_pid_restart():
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY"}, "rss_kb": 40000, "pid": 100},
        {"t": 300, "sessions": {"COM0": "READY"}, "rss_kb": 45000, "pid": 100},
        {"t": 600, "sessions": {"COM0": "READY"}, "rss_kb": 48000, "pid": 200},  # 重啟
    ]
    a = longrun.analyze(snapshots, [])
    assert a["rss_trend"] == {"first_kb": 40000, "last_kb": 48000, "delta_kb": 8000}
    assert a["pid_changes"] == 1  # 100→200 一次重啟（非死亡）
    assert a["daemon_death_at"] is None
    assert a["stuck_attached"] == []  # 全程 READY


def test_analyze_empty_inputs():
    a = longrun.analyze([], [])
    assert a["per_source"] == {}
    assert a["stuck_attached"] == []
    assert a["pid_changes"] == 0
    assert a["daemon_death_at"] is None
    assert a["rss_trend"] == {"first_kb": 0, "last_kb": 0, "delta_kb": 0}
