"""Slice 2.2 Telnet console 黑箱契約（07 Task 11）。"""
from __future__ import annotations

import os
import time

import pytest

from .console_helpers import console_kind, open_console, sock_read_for, sock_read_until, wait_for, wait_rx_contains, write_console

if os.name == "nt":
    pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com]
else:
    pytestmark = [pytest.mark.posix_only, pytest.mark.cap_posix_pty]
    pytestmark.append(pytest.mark.rust_only)

IAC, DONT, WILL = 0xFF, 0xFE, 0xFB
OPT_BINARY, OPT_NAWS = 0x00, 0x1F
# sw_core/telnet_console.py TELNET_GREETING：WILL ECHO、WILL SGA、DO SGA、WILL BINARY
GREETING = bytes([IAC, WILL, 0x01, IAC, WILL, 0x03, IAC, 0xFD, 0x03, IAC, WILL, OPT_BINARY])


def _consoles(d) -> list[dict]:
    return d.sw("session", "console-list", "--selector", "COM0")["consoles"]


def _connect(d, attach_resp: dict):
    n = len(_consoles(d))
    assert console_kind(attach_resp) == "tcp", attach_resp
    sock = open_console(attach_resp)
    assert hasattr(sock, "recv"), sock
    greeting = sock_read_until(sock, GREETING, 3.0)
    assert greeting.startswith(GREETING), greeting
    assert wait_for(lambda: len(_consoles(d)) == n + 1, 3.0), _consoles(d)
    return sock


def test_greeting_and_attach_shape(telnet_daemon) -> None:
    d, _target = telnet_daemon
    a = d.sw("session", "console-attach", "--selector", "COM0")
    assert a["ok"] is True and a["client_id"] is None, a
    assert console_kind(a) == "tcp", a
    assert a["transport"] == "tcp" and a["protocol"] == "telnet", a
    assert a["vtty"] == a["endpoint"] and a["endpoint"].startswith("127.0.0.1:"), a
    assert "interactive_session_id" not in a, a
    sock = open_console(a)
    try:
        got = sock_read_until(sock, GREETING, 3.0)
        assert got[:12] == GREETING, got
    finally:
        sock.close()


def test_first_raw_second_line(telnet_daemon) -> None:
    d, target = telnet_daemon
    attach = d.sw("session", "console-attach", "--selector", "COM0")
    t1 = _connect(d, attach)
    t2 = _connect(d, attach)
    try:
        rows = _consoles(d)
        assert sum(1 for r in rows if r["interactive_owner"]) == 1, rows
        write_console(t1, b"ab")
        assert wait_rx_contains(target, b"ab", 3.0)
        write_console(t2, b"ls\r\n")
        echo = sock_read_until(t2, b"ls\r", 3.0)
        assert b"ls\r" in echo, echo
        assert wait_rx_contains(target, b"ls\n", 3.0)
    finally:
        t1.close()
        t2.close()


def test_iac_escape_outbound(telnet_daemon) -> None:
    d, target = telnet_daemon
    attach = d.sw("session", "console-attach", "--selector", "COM0")
    t1 = _connect(d, attach)
    try:
        target.emit(b"\xff\x01")
        got = sock_read_until(t1, b"\xff\xff\x01", 3.0)
        assert b"\xff\xff\x01" in got, got
    finally:
        t1.close()


def test_inbound_nvt_and_binary_refusal(telnet_daemon) -> None:
    d, target = telnet_daemon
    attach = d.sw("session", "console-attach", "--selector", "COM0")
    t1 = _connect(d, attach)
    try:
        write_console(t1, bytes([IAC, IAC]))
        assert wait_rx_contains(target, b"\xff", 3.0)
        before = len(target.written(0.2))
        write_console(t1, bytes([IAC, WILL, OPT_BINARY]))
        got = sock_read_until(t1, bytes([IAC, DONT, OPT_BINARY]), 3.0)
        assert bytes([IAC, DONT, OPT_BINARY]) in got, got
        time.sleep(0.3)
        assert len(target.written(0.2)) == before, "協商 bytes 不得送到 DUT"
        write_console(t1, bytes([IAC]))
        time.sleep(0.1)
        write_console(t1, bytes([WILL, OPT_NAWS]))
        got = sock_read_until(t1, bytes([IAC, DONT, OPT_NAWS]), 3.0)
        assert bytes([IAC, DONT, OPT_NAWS]) in got, got
        time.sleep(0.3)
        assert len(target.written(0.2)) == before, "分段協商 bytes 不得送到 DUT"
    finally:
        t1.close()


def test_disconnect_removes_console(telnet_daemon) -> None:
    d, _target = telnet_daemon
    attach = d.sw("session", "console-attach", "--selector", "COM0")
    t1 = _connect(d, attach)
    t2 = _connect(d, attach)
    try:
        ids = {r["client_id"] for r in _consoles(d)}
        assert len(ids) == 2
        t2.close()
        assert wait_for(lambda: len(_consoles(d)) == 1, 1.0), _consoles(d)
        sock_read_for(t1, 0.05)
    finally:
        t1.close()


def test_session_public_has_console_endpoint(telnet_daemon) -> None:
    d, _target = telnet_daemon
    rows = d.sw("session", "list")["sessions"]
    assert rows and all("console_endpoint" in row for row in rows), rows
    assert rows[0]["console_endpoint"].startswith("127.0.0.1:"), rows
