"""Slice 2.2 interactive lease 黑箱契約（07 Task 11；Python 基線與 Rust artifact 雙邊執行）。"""
from __future__ import annotations

import os
import time

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
    time.sleep(0.5)  # written() 讀完即返回，不會等；先等非同步寫出的機會
    assert len(target.written(0.2)) == before, "編碼拒絕在送出前（零 TX）"


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

    d, target = console_daemon
    iid = _open(d, timeout="1")["interactive_id"]
    time.sleep(1.05)
    before = len(target.written(0.2))
    first = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "x"})
    assert first["ok"] is False and first["error_code"] == "INTERACTIVE_EXPIRED", first
    # parity-amendments #54（ALIGN-TO-PYTHON）：lazy 到期後 tombstone 使後續 send／status／close 持續回 EXPIRED
    second = d.rpc("session.interactive_send", {"interactive_id": iid, "data": "x"})
    assert second["ok"] is False and second["error_code"] == "INTERACTIVE_EXPIRED", second
    st = d.rpc("session.interactive_status", {"interactive_id": iid})
    assert st["ok"] is False and st["error_code"] == "INTERACTIVE_EXPIRED", st
    cl = d.rpc("session.interactive_close", {"interactive_id": iid})
    assert cl["ok"] is False and cl["error_code"] == "INTERACTIVE_EXPIRED", cl
    time.sleep(0.5)
    assert b"x" not in target.written(0.2)[before:], "到期拒絕零 TX"


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


def test_open_rejections_zero_tx(console_daemon) -> None:
    import threading

    d, target = console_daemon
    # RECOVERING：agent `reboot` 進入 reboot recovery（`session_manager.py:6157-6165`），hold_reply 卡住其 prompt
    # 等待；`session recover --force` 在兩版都不對外暴露 RECOVERING（READY→ATTACHING），故改走 reboot。
    target.hold_reply(True)
    rec = threading.Thread(
        target=lambda: d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "reboot", "--cmd-timeout", "5"),
        daemon=True,
    )
    try:
        rec.start()
        assert wait_for(lambda: d._session("COM0")["state"] == "RECOVERING", 10.0, 0.02), d._session("COM0")
        before = len(target.written(0.2))
        r = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent", "command": "help"})
        assert r["ok"] is False and r["error_code"] == "SESSION_RECOVERING", r
        time.sleep(0.5)
        assert b"help" not in target.written(0.2)[before:], "RECOVERING 拒絕零 TX"
    finally:
        target.hold_reply(False)
        rec.join(timeout=30.0)
    d.wait_state("COM0", "READY", timeout=30.0)
    # 已有 lease → SESSION_INTERACTIVE_BUSY
    _open(d)
    before = len(target.written(0.2))
    busy = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent", "command": "help"})
    assert busy["ok"] is False and busy["error_code"] == "SESSION_INTERACTIVE_BUSY", busy
    time.sleep(0.5)
    assert b"help" not in target.written(0.2)[before:], "BUSY 拒絕零 TX"


def test_attached_rescue_boot_interrupt(unready_daemon) -> None:
    d, target = unready_daemon
    target.emit(b"Hit any key to stop autoboot:  3\r\n")
    time.sleep(0.3)
    r = d.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent", "--allow-attached")
    assert r["ok"] is True and r["recovery_mode"] is True and r.get("boot_interrupt") is True, r
    assert "login_required" not in r, r


def test_attached_rescue_login_required(unready_daemon) -> None:
    d, target = unready_daemon
    target.emit(b"\r\n(none) login: ")
    time.sleep(0.3)
    r = d.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent", "--allow-attached")
    assert r["ok"] is True and r["recovery_mode"] is True and r.get("login_required") is True, r
    assert "boot_interrupt" not in r, r


def test_attached_not_bootloader(unready_daemon) -> None:
    d, target = unready_daemon
    target.emit(b"random\r\n")
    time.sleep(0.3)
    before = len(target.written(0.2))
    r = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent", "allow_attached": True})
    assert r["ok"] is False and r["error_code"] == "SESSION_NOT_READY", r
    assert r.get("error_detail") == "NOT_BOOTLOADER", r
    time.sleep(0.3)
    assert len(target.written(0.2)) == before, "拒絕路徑零 TX"


def test_attached_without_allow_rejected(unready_daemon) -> None:
    d, target = unready_daemon
    before = len(target.written(0.2))
    r = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent"})
    assert r["ok"] is False and r["error_code"] == "SESSION_NOT_READY" and r["selector"] == "COM0", r
    time.sleep(0.3)
    assert len(target.written(0.2)) == before, "拒絕路徑零 TX"


@pytest.mark.rust_only
def test_stash_dropped_does_not_replay(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    o = _open(d)
    assert o.get("soft_preempted") is True, o
    fd1 = open_vtty(a1["vtty"])
    try:
        os.write(fd1, b"Q")
        time.sleep(0.3)
    finally:
        os.close(fd1)
    time.sleep(3.5)  # peer 消失超過 grace（D-16：stash 丟棄）
    assert d.rpc("session.interactive_close", {"interactive_id": o["interactive_id"]})["ok"] is True
    time.sleep(1.0)
    assert b"Q" not in target.written(0.2), "stash 已丟棄，close 後不回放"
