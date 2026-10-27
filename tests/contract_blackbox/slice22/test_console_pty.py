"""Slice 2.2 PTY console 黑箱契約（07 Task 11；Python 基線與 Rust artifact 雙邊執行）。"""
from __future__ import annotations

import os
import re

import pytest

from .console_helpers import open_vtty, read_until, tx_payload, wait_for, wait_rx_contains

pytestmark = pytest.mark.cap_posix_pty


def _consoles(d) -> list[dict]:
    return d.sw("session", "console-list", "--selector", "COM0")["consoles"]


def test_first_console_raw_second_line_buffered(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    assert a1["ok"] is True, a1
    assert re.fullmatch(r"[0-9a-f]{12}", a1["client_id"])
    assert re.fullmatch(r"/dev/pts/\d+", a1["vtty"])
    assert re.fullmatch(r"[0-9a-f]{32}", a1["interactive_session_id"]) and a1["interactive_owner"] is True
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    assert "interactive_session_id" not in a2 and "interactive_owner" not in a2
    rows = _consoles(d)
    assert [r["label"] for r in rows] == sorted(r["label"] for r in rows)
    assert [r["client_id"] for r in rows if r["interactive_owner"]] == [a1["client_id"]]
    fd1 = open_vtty(a1["vtty"])
    try:
        os.write(fd1, b"\x1b[A\t")
        assert wait_rx_contains(target, b"\x1b[A\t", 2.0)
        assert wait_for(lambda: tx_payload(d.wal_records(), f"human:{a1['client_id']}") == b"\x1b[A\t", 2.0)
    finally:
        os.close(fd1)


def test_rx_fanned_out_to_all_consoles(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    fd1, fd2 = open_vtty(a1["vtty"]), open_vtty(a2["vtty"])
    try:
        target.emit(b"FANOUT\r\n")
        assert b"FANOUT" in read_until(fd1, b"FANOUT", 3.0)
        assert b"FANOUT" in read_until(fd2, b"FANOUT", 3.0)
    finally:
        os.close(fd1)
        os.close(fd2)


def test_second_console_line_runs_after_owner_detached(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    assert d.sw("session", "console-detach", "--selector", "COM0", "--client-id", a1["client_id"])["ok"] is True
    fd2 = open_vtty(a2["vtty"])
    try:
        os.write(fd2, b"echo L2\r")
        echo = read_until(fd2, b"echo L2\r", 3.0)
        assert b"echo L2\r" in echo, echo
        assert wait_rx_contains(target, b"echo L2\n", 5.0)
        rows = [r for r in d.wal_records() if r.get("dir") == "TX" and r.get("source") == f"human:{a2['client_id']}"]
        assert rows and all(r.get("cmd_id") for r in rows), rows
    finally:
        os.close(fd2)


def test_backspace_local_echo(console_daemon) -> None:
    d, target = console_daemon
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    d.sw("session", "console-detach", "--selector", "COM0", "--client-id", _consoles(d)[0]["client_id"])
    fd2 = open_vtty(a2["vtty"])
    try:
        os.write(fd2, b"ab\x7fc\r")
        echo = read_until(fd2, b"c\r", 3.0)
        assert b"ab\x08 \x08c\r" in echo, echo
        assert wait_rx_contains(target, b"ac\n", 5.0)
    finally:
        os.close(fd2)


def test_detach_releases_owner(console_daemon) -> None:
    d, _target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    r = d.sw("session", "console-detach", "--selector", "COM0", "--client-id", a1["client_id"])
    assert r["ok"] is True and r["session"]["interactive_session_id"] is None, r
    assert not any(row["interactive_owner"] for row in _consoles(d))


def test_detach_unknown_client(console_daemon) -> None:
    d, _target = console_daemon
    r = d.rpc("session.console_detach", {"selector": "COM0", "client_id": "nope"})
    assert {k: r[k] for k in ("ok", "error_code", "client_id")} == {
        "ok": False,
        "error_code": "CONSOLE_NOT_FOUND",
        "client_id": "nope",
    }
    assert "version" in r


def test_invalid_args_and_alias_selector(console_daemon) -> None:
    d, target = console_daemon
    before = len(target.written(0.2))
    r = d.rpc("session.console_attach", {})
    assert r["ok"] is False and r["error_code"] == "INVALID_ARGS"
    assert d.rpc("session.console_attach", {"com": "COM0"})["ok"] is True
    r = d.rpc("session.console_attach", {"selector": "COM9"})
    assert r["ok"] is False and r["error_code"] == "SESSION_NOT_READY" and r["selector"] == "COM9"
    assert len(target.written(0.2)) == before, "拒絕路徑零 TX"


def test_session_list_has_no_console_endpoint(console_daemon) -> None:
    d, _target = console_daemon
    rows = d.sw("session", "list")["sessions"]
    assert rows and all("console_endpoint" not in row for row in rows)


def test_session_vtty_cached(console_daemon) -> None:
    d, _target = console_daemon
    first = d._session("COM0")["vtty"]
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    assert d._session("COM0")["vtty"] == first
