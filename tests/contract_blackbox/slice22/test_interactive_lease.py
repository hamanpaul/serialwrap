"""Slice 2.2 interactive lease 黑箱契約（07 Task 11；Python 基線與 Rust artifact 雙邊執行）。"""
from __future__ import annotations

import os

import pytest

from .console_helpers import open_vtty, tx_payload, wait_for, wait_rx_contains

pytestmark = pytest.mark.cap_posix_pty


def _open(d, owner: str = "agent", timeout: str | None = None) -> dict:
    args = ["session", "interactive-open", "--selector", "COM0", "--owner", owner]
    if timeout is not None:
        args += ["--timeout", timeout]
    return d.sw(*args)


def test_open_send_status_close_shapes(console_daemon) -> None:
    d, target = console_daemon
    o = _open(d)
    assert o["ok"] is True and o["recovery_mode"] is False and "soft_preempted" not in o, o
    iid = o["interactive_id"]
    s = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "echo I1\n"})
    assert s["ok"] is True and s["bytes"] == 8, s
    assert wait_rx_contains(target, b"echo I1\n", 3.0)
    assert wait_for(lambda: b"echo I1\n" in tx_payload(d.wal_records(), "agent"), 3.0)
    st = d.rpc("session.interactive_status", {"interactive_id": iid})
    assert st["ok"] is True and st["owner"] == "agent" and st["status"] == "active", st
    assert wait_for(lambda: "I1" in d.rpc("session.interactive_status", {"interactive_id": iid})["screen"], 3.0)
    assert d.rpc("session.interactive_close", {"interactive_id": iid})["ok"] is True
    gone = d.rpc("session.interactive_status", {"interactive_id": iid})
    assert gone["ok"] is False and gone["error_code"] == "INTERACTIVE_NOT_FOUND", gone


def test_open_twice_busy(console_daemon) -> None:
    d, target = console_daemon
    first = _open(d)
    before = len(target.written(0.2))
    second = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent", "command": "help"})
    assert second["ok"] is False and second["error_code"] == "SESSION_INTERACTIVE_BUSY", second
    assert second["interactive_session_id"] == first["interactive_id"]
    assert b"help" not in target.written(0.5)[before:], "拒絕零 TX"


def test_soft_preempt_restore_and_replay(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    o = _open(d)
    assert o["ok"] is True and o.get("soft_preempted") is True, o
    fd1 = open_vtty(a1["vtty"])
    try:
        os.write(fd1, b"Q")
        assert not wait_rx_contains(target, b"Q", 1.0), "preempt 期間 human 位元組進 deferred"
        assert d.rpc("session.interactive_close", {"interactive_id": o["interactive_id"]})["ok"] is True
        assert wait_rx_contains(target, b"Q", 3.0)
        rows = d.sw("session", "console-list", "--selector", "COM0")["consoles"]
        assert any(r["client_id"] == a1["client_id"] and r["interactive_owner"] for r in rows), rows
    finally:
        os.close(fd1)


def test_send_encodings(console_daemon) -> None:
    d, target = console_daemon
    iid = _open(d)["interactive_id"]
    r = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "ctrl-c", "encoding": "key"})
    assert r["ok"] is True and r["bytes"] == 1, r
    assert wait_rx_contains(target, b"\x03", 2.0)
    r = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "aGk=", "encoding": "base64"})
    assert r["ok"] is True and r["bytes"] == 2, r
    assert wait_rx_contains(target, b"hi", 2.0)
    before = len(target.written(0.3))
    r = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "00", "encoding": "hex"})
    assert r["ok"] is False and r["error_code"] == "EXCEPTION" and "version" not in r, r
    assert len(target.written(0.5)) == before, "編碼拒絕在送出前（零 TX）"


def test_expiry_tick_restores_without_access(console_daemon) -> None:
    import time

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    try:
        o = _open(d, timeout="1")
        assert o.get("soft_preempted") is True, o
        os.write(fd1, b"W")
        time.sleep(3.0)  # 期間不發任何 RPC：tick 到期還原並回放
        assert b"W" in target.written(0.5)
        iid = o["interactive_id"]
        for _ in range(2):
            st = d.rpc("session.interactive_status", {"interactive_id": iid})
            assert st["ok"] is False and st["error_code"] == "INTERACTIVE_EXPIRED", st
        cl = d.rpc("session.interactive_close", {"interactive_id": iid})
        assert cl["ok"] is False and cl["error_code"] == "INTERACTIVE_EXPIRED", cl
    finally:
        os.close(fd1)


def test_lazy_expiry_parity(console_daemon) -> None:
    import time

    d, _target = console_daemon
    iid = _open(d, timeout="1")["interactive_id"]
    time.sleep(1.05)
    first = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "x"})
    assert first["ok"] is False and first["error_code"] == "INTERACTIVE_EXPIRED", first
    second = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "x"})
    assert second["ok"] is False and second["error_code"] in ("INTERACTIVE_NOT_FOUND", "INTERACTIVE_EXPIRED"), second


def test_status_touch_extends(console_daemon) -> None:
    import time

    d, _target = console_daemon
    iid = _open(d, timeout="2")["interactive_id"]
    time.sleep(1.5)
    assert d.rpc("session.interactive_status", {"interactive_id": iid})["ok"] is True
    time.sleep(1.0)
    assert d.rpc("session.interactive_status", {"interactive_id": iid})["ok"] is True
    time.sleep(3.5)
    st = d.rpc("session.interactive_status", {"interactive_id": iid})
    assert st["ok"] is False and st["error_code"] == "INTERACTIVE_EXPIRED", st
