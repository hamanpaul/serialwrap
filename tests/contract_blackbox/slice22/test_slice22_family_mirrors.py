"""Slice 2.2 family mirror 黑箱案例（07 Task 11）。"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.cap_posix_pty


def test_f6_ping_during_long_op(console_daemon) -> None:
    d, _target = console_daemon
    sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 5", "--cmd-timeout", "10")
    assert sub["ok"] is True, sub
    time.sleep(0.5)
    t0 = time.monotonic()
    st = d.sw("daemon", "status")
    assert time.monotonic() - t0 < 1.0, "長命令期間 daemon status 往返 < 1 s"
    assert st.get("_rc") == 0, st


def test_f6_two_boards_no_starvation(two_board_daemon) -> None:
    d = two_board_daemon
    ids = []
    for n in range(10):
        for com in ("COM0", "COM1"):
            sub = d.sw("cmd", "submit", "--selector", com, "--cmd", f"echo {com}-{n}", "--cmd-timeout", "5")
            assert sub["ok"] is True, sub
            ids.append(sub["cmd_id"])
    for cid in ids:
        row = d.wait_command_done(cid, timeout=30.0)
        assert row.get("status") == "done", row
    for com in ("COM0", "COM1"):
        got = d.targets[com].written(0.2)
        for n in range(10):
            assert f"echo {com}-{n}\n".encode() in got, (com, n)
        other = "COM1" if com == "COM0" else "COM0"
        assert f"echo {other}-".encode() not in got, "命令不得送到另一塊板"


def test_f8_second_daemon_detected(console_daemon, tmp_path) -> None:
    from harness import start_daemon

    d, _target = console_daemon
    second = start_daemon(tmp_path / "second", profile_yaml="profiles: {}\ntargets: {}\n")
    try:
        deadline = time.monotonic() + 15.0
        st: dict = {}
        while time.monotonic() < deadline:
            st = d.sw("daemon", "status")
            pids = {row.get("pid") for row in (st.get("multi_open_detail") or {}).get("daemons", [])}
            if st.get("multi_open") is True and {d.proc.pid, second.proc.pid} <= pids:
                break
            time.sleep(0.5)
        assert st.get("multi_open") is True, st
        pids = {row.get("pid") for row in st["multi_open_detail"]["daemons"]}
        assert {d.proc.pid, second.proc.pid} <= pids, st
    finally:
        second.stop()


def test_f8_tty_holders_reported(console_daemon) -> None:
    d, target = console_daemon
    st = d.sw("daemon", "status")
    holders = st.get("foreign_holders")
    assert isinstance(holders, dict), st
    entry = holders.get(target.slave_path)
    # 形狀依 `service.py:696-705`：已知持有者為 pid；另有無法判定的外部 holder 時為
    # `{"device", "holder_unknown": true, "pids": [pid]}`（本 daemon 的 pid 仍須在內）
    if isinstance(entry, dict):
        assert entry.get("device") == target.slave_path and entry.get("holder_unknown") is True, entry
        assert entry.get("pids") == [d.proc.pid], (entry, d.proc.pid)
    else:
        assert entry == d.proc.pid, (target.slave_path, holders)
