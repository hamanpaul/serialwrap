"""#122 長跑分析器單測——吃合成快照/事件，不碰 live。

P2-T14 追加：lr-mixed 的 PID 來源（daemon status）、RPC 逾時不判死亡、連續 3 次且 pid 已死才判死亡。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from realhw.cases import longrun
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


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


def test_analyze_no_zero_duration_stuck_at_tail():
    # 最後一筆快照剛好非 READY（如 daemon death 後 sessions 清空）→不得產生 0 秒 stuck 段（Copilot #5）。
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY"}, "rss_kb": 1, "pid": 1},
        {"t": 300, "sessions": {"COM0": "READY"}, "rss_kb": 1, "pid": 1},
        {"t": 600, "sessions": {}, "rss_kb": 0, "pid": 0},  # 尾快照非 READY，open_from==last_t
    ]
    a = longrun.analyze(snapshots, [])
    assert a["stuck_attached"] == []
    assert all(s["duration_s"] > 0 for s in a["stuck_attached"])


def test_analyze_records_genuine_stuck_at_tail():
    # 尾端連續非 READY 且時長>0→仍須封口記錄（guard 不誤殺真實 stuck）。
    snapshots = [
        {"t": 0, "sessions": {"COM0": "READY"}, "rss_kb": 1, "pid": 1},
        {"t": 300, "sessions": {"COM0": "ATTACHED"}, "rss_kb": 1, "pid": 1},
        {"t": 600, "sessions": {"COM0": "ATTACHED"}, "rss_kb": 1, "pid": 1},
    ]
    a = longrun.analyze(snapshots, [])
    assert a["stuck_attached"] == [{"com": "COM0", "from_t": 300, "to_t": 600, "duration_s": 300}]


# --------------------------------------------------------------------------- #
# P2-T14 red：lr-mixed PID 與死亡判定（替身 sw／systemd，不碰 live）
# --------------------------------------------------------------------------- #
def _fast_uniform(a, b):
    """agent 的 uniform(0.5,2.0) → 0.02 s；human 的 uniform(120,300) → 0.2 s。"""
    return 0.02 if a < 10 else 0.2


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Systemd:
    """舊路徑 systemctl MainPID；新設計 lr-mixed 不得呼叫（calls 必須為 0）。"""

    def __init__(self):
        self.calls = 0

    def main_pid(self):
        self.calls += 1
        return 4242  # 非零：舊實作不得因 pid==0 提早判定 daemon death


class _FakeHuman:
    kind = "fake"

    def __init__(self):
        self.lines = []

    def open(self, com):
        pass

    def alive(self):
        return True

    def send_line(self, text):
        self.lines.append(text)

    def send_text(self, text):
        self.lines.append(text)

    def send_key(self, key):
        pass

    def capture(self):
        return ""

    def client_id(self):
        return None

    def close(self):
        pass

    def crash(self):
        pass


class _DaemonSw:
    """`daemon status` 依 pid_seq 逐次回應（"TIMEOUT"＝RPC 逾時）；其餘命令回成功空結果。"""

    def __init__(self, pid_seq):
        self._seq = list(pid_seq)
        self.daemon_calls = 0

    def submit_and_wait(self, com, cmd, *, cmd_timeout=12.0, settle_s=1.5, poll_s=0.5, source=None):
        marker = cmd.split(" ", 1)[1] if " " in cmd else cmd
        return {"status": "done", "stdout": marker + "\n"}

    def run(self, *args, **kwargs):
        if tuple(args) == ("daemon", "status"):
            self.daemon_calls += 1
            item = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
            if item == "TIMEOUT":
                return {"ok": False, "error_code": "TIMEOUT"}
            return {"ok": True, "running": True, "pid": item}
        if tuple(args[:2]) == ("cmd", "status"):
            return {"ok": True, "command": {"status": "done"}}
        if tuple(args[:2]) == ("session", "interactive-open"):
            return {"ok": True, "interactive_id": 7}
        return {"ok": True}

    def session(self, com):
        return {"state": "READY"}


def _ctx(tmp_path, sw, *, systemd=None, duration_s=2, snap=1):
    cfg = {
        "boards": [{"com": "COM0"}, {"com": "COM1"}],
        "longrun": {"agent_workers": 4, "snapshot_interval_s": snap},
        "duration_s": duration_s,
        "_version": "serialwrap 0.3.2",
        "_git": "abc1234",
    }
    return SimpleNamespace(
        cfg=cfg, report_dir=tmp_path, case_dir=tmp_path / "lr-mixed", sw=sw,
        tmux=None, usbipd=None, systemd=systemd or _Systemd(), win=None,
        human=lambda tag, role: _FakeHuman(),
    )


def test_lr_mixed_pid_from_daemon_status(tmp_path, monkeypatch):
    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    sw = _DaemonSw([77])
    systemd = _Systemd()
    ctx = _ctx(tmp_path, sw, systemd=systemd, duration_s=2, snap=1)

    longrun.lr_mixed(ctx)

    snaps = _read_jsonl(ctx.case_dir / "snapshots.ndjson")
    assert snaps
    assert all(s["pid"] == 77 for s in snaps)  # pid 取自 daemon status.pid
    assert systemd.calls == 0                  # 不得再呼叫 ctx.systemd.main_pid


def test_lr_mixed_rpc_timeout_not_death(tmp_path, monkeypatch):
    from realhw import procobs

    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    monkeypatch.setattr(procobs, "pid_alive", lambda pid: True)
    sw = _DaemonSw([77, "TIMEOUT", 77])  # 第 2 次 probe 逾時，之後恢復
    ctx = _ctx(tmp_path, sw, systemd=_Systemd(), duration_s=3, snap=1)

    t0 = time.monotonic()
    result = longrun.lr_mixed(ctx)
    elapsed = time.monotonic() - t0

    snaps = _read_jsonl(ctx.case_dir / "snapshots.ndjson")
    events = _read_jsonl(ctx.case_dir / "events.ndjson")
    assert snaps[0]["pid"] == 77
    assert snaps[1]["pid"] is None and "pid_probe_error" in snaps[1]  # 不得記 0
    assert not [e for e in events if e["kind"] == "major"]            # 無 major daemon_death
    assert elapsed >= 2.5                                             # 長跑未停
    analysis = longrun.analyze(snaps, events)
    assert analysis["pid_changes"] == 0
    assert analysis["daemon_death_at"] is None
    assert result.reason_code != "daemon_died"


def test_lr_mixed_death_requires_consecutive_and_dead_pid(tmp_path, monkeypatch):
    from realhw import procobs

    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    timeouts = [77] + ["TIMEOUT"] * 12

    # A：連續 3 次 probe 失敗且 pid 已死 → major daemon_death（第 3 次失敗的時刻 t=3）
    monkeypatch.setattr(procobs, "pid_alive", lambda pid: False)
    ctx_a = _ctx(tmp_path / "a", _DaemonSw(timeouts), systemd=_Systemd(), duration_s=6, snap=1)
    res_a = longrun.lr_mixed(ctx_a)
    majors_a = [e for e in _read_jsonl(ctx_a.case_dir / "events.ndjson") if e["kind"] == "major"]
    assert majors_a and majors_a[0]["detail"] == "daemon_death"
    assert majors_a[0]["t_at"] >= 3
    assert res_a.reason_code == "daemon_died"

    # B：同樣連續 3 次失敗，但 pid 仍活 → 不判死亡，長跑照常跑完
    monkeypatch.setattr(procobs, "pid_alive", lambda pid: True)
    ctx_b = _ctx(tmp_path / "b", _DaemonSw(timeouts), systemd=_Systemd(), duration_s=4, snap=1)
    res_b = longrun.lr_mixed(ctx_b)
    majors_b = [e for e in _read_jsonl(ctx_b.case_dir / "events.ndjson") if e["kind"] == "major"]
    assert majors_b == []
    assert res_b.reason_code != "daemon_died"
