"""`tests/contract_blackbox/bb_daemon.PyDaemon.stop` 的量測與歸因紀錄（S1X Y3）。

出口 run `s1exit-20261028T050554` 的 A/B 兩輪各出現一次 teardown「Daemon process 於 stop 後仍存活」，
當時無任何階段耗時或行程狀態可供歸因。stop 改為：每次 stop 以 `$SW_AB_STOPLOG` 記錄 RPC／升級階段與總耗時；
需升級時記錄升級前各 thread 的排程狀態與 wchan；仍存活時錯誤訊息附上 thread 狀態。判定與逾時不變。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "contract_blackbox"))

import bb_daemon  # noqa: E402


def _daemon_like(tmp_path: Path, script: str) -> bb_daemon.PyDaemon:
    d = bb_daemon.PyDaemon({}, profile_dir=tmp_path, socket=str(tmp_path / "none.sock"), lock=tmp_path / "x.lock")
    d.proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    d._pids = [d.proc.pid]
    return d


def test_stop_escalation_is_logged_with_thread_states(tmp_path, monkeypatch):
    """忽略 SIGTERM、RPC 不回應的行程：stop 依原序列升級到 SIGKILL 並成功回收；紀錄含升級前各 thread 狀態。"""
    log = tmp_path / "stop.ndjson"
    monkeypatch.setenv("SW_AB_STOPLOG", str(log))
    d = _daemon_like(tmp_path, "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
    d.stop(timeout_s=0.5)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["escalation"] == "sigkill"
    assert row["rpc_ok"] is False
    assert row["returncode"] == -9
    assert row["diag_before_sigterm"] and row["diag_before_sigkill"]
    assert {"tid", "state"} <= set(row["diag_before_sigkill"][0])
    assert row["diag_before_sigkill"][0]["state"] in {"S", "R", "D"}


def test_stop_without_stoplog_env_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SW_AB_STOPLOG", raising=False)
    d = _daemon_like(tmp_path, "import time\ntime.sleep(60)\n")
    d.stop(timeout_s=0.2)
    assert not list(tmp_path.glob("*.ndjson"))


def test_proc_diag_reads_states_and_handles_missing_pid():
    diag = bb_daemon._proc_diag(__import__("os").getpid())
    assert diag and all("state" in row for row in diag)
    assert bb_daemon._proc_diag(2 ** 22 + 12345) == []


def test_ab_blackbox_sets_stoplog_for_both_rounds():
    text = (ROOT / "scripts" / "rust" / "ab_blackbox.sh").read_text(encoding="utf-8")
    assert 'SW_AB_STOPLOG="$RUN_DIR/a.stoplog.ndjson"' in text
    assert 'SW_AB_STOPLOG="$RUN_DIR/b.stoplog.ndjson"' in text
