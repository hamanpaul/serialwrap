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


def test_second_console_blocked_while_human_lease(console_daemon) -> None:
    d, target = console_daemon
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    fd2 = open_vtty(a2["vtty"])
    try:
        os.write(fd2, b"ls\r")
        assert not wait_rx_contains(target, b"ls\n", 3.0), "h1 持 lease 時 h2 的行不得送到 DUT"
        assert tx_payload(d.wal_records(), f"human:{a2['client_id']}") == b""
    finally:
        os.close(fd2)


def test_deferred_bytes_flushed_after_agent_command(console_daemon) -> None:
    import time

    from .console_helpers import b64_payload

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0")
    fd1 = open_vtty(a1["vtty"])
    try:
        sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 2", "--cmd-timeout", "8")
        assert sub["ok"] is True, sub
        time.sleep(0.5)
        os.write(fd1, b"X")
        time.sleep(0.5)
        assert b"X" not in target.written(0.1), "agent 命令期間 human 位元組必須進 deferred"
        assert wait_rx_contains(target, b"X", 6.0)
        recs = [r for r in d.wal_records() if r["dir"] in ("TX", "RX")]
        seq_cmd = min(r["seq"] for r in recs if r["dir"] == "TX" and r.get("cmd_id") == sub["cmd_id"])
        seq_x = min(r["seq"] for r in recs if r["dir"] == "TX" and r["source"] == f"human:{a1['client_id']}")
        seq_prompt = min(
            r["seq"] for r in recs if r["dir"] == "RX" and r["seq"] > seq_cmd and b"# " in b64_payload(r)
        )
        assert seq_cmd < seq_prompt < seq_x, (seq_cmd, seq_prompt, seq_x)
    finally:
        os.close(fd1)


def test_raw_ownership_survives_agent_rounds(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    # human 的 minicom 一直開著（否則逾 2 s 無 peer 會被 list prune，與 ownership 無關）。
    fd1 = open_vtty(a1["vtty"])
    try:
        for n in range(5):
            sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo R{n}", "--cmd-timeout", "5")
            assert sub["ok"] is True, sub
            d.wait_command_done(sub["cmd_id"])
            # done_at 之後 resume 才還原 owner（Python `finally` 先 resume 再結束前景），短暫輪詢。
            assert wait_for(
                lambda: [r["client_id"] for r in _consoles(d) if r["interactive_owner"]] == [a1["client_id"]], 2.0
            ), _consoles(d)
        os.write(fd1, b"\t")
        assert wait_rx_contains(target, b"\t", 2.0)
    finally:
        os.close(fd1)


def test_peer_gone_console_pruned(console_daemon) -> None:
    import time

    d, _target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    fd1 = open_vtty(a1["vtty"])
    try:
        time.sleep(2.5)
        ids = [r["client_id"] for r in _consoles(d)]
        assert a2["client_id"] not in ids, ids
        assert a1["client_id"] in ids, ids
    finally:
        os.close(fd1)


def test_human_line_interactive_mode_opens_lease(console_daemon) -> None:
    d, _target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    d.sw("session", "console-detach", "--selector", "COM0", "--client-id", a1["client_id"])
    fd2 = open_vtty(a2["vtty"])
    try:
        os.write(fd2, b"vim\r")
        assert wait_for(
            lambda: any(r["client_id"] == a2["client_id"] and r["interactive_owner"] for r in _consoles(d)), 5.0
        ), _consoles(d)
    finally:
        os.close(fd2)


def test_line_overflow_warning(console_daemon) -> None:
    d, target = console_daemon
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    d.sw("session", "console-detach", "--selector", "COM0", "--client-id", _consoles(d)[0]["client_id"])
    fd2 = open_vtty(a2["vtty"])
    try:
        payload = b"a" * 16390 + b"\r"
        view = memoryview(payload)
        while view:
            try:
                n = os.write(fd2, view[:4096])
            except BlockingIOError:
                read_until(fd2, b"\x00", 0.05)
                continue
            view = view[n:]
            read_until(fd2, b"\x00", 0.01)
        assert not wait_rx_contains(target, b"aaaa", 2.0)
        assert wait_for(lambda: a2["client_id"] in d._stderr_tail(20000), 3.0), d._stderr_tail()
    finally:
        os.close(fd2)


def test_last_line_before_disconnect_submitted(console_daemon) -> None:
    d, target = console_daemon
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    d.sw("session", "console-detach", "--selector", "COM0", "--client-id", _consoles(d)[0]["client_id"])
    fd2 = open_vtty(a2["vtty"])
    os.write(fd2, b"echo LAST\r")
    os.close(fd2)
    assert wait_rx_contains(target, b"echo LAST\n", 5.0)


def test_peer_loss_grace_then_teardown(console_daemon) -> None:
    import time

    d, _target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    os.write(fd1, b"a")  # human active
    time.sleep(0.3)
    os.close(fd1)
    busy = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent"})
    assert busy["ok"] is False and busy["error_code"] == "SESSION_INTERACTIVE_BUSY", busy
    time.sleep(3.5)
    ok = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent"})
    assert ok["ok"] is True and "soft_preempted" not in ok, ok
    assert a1["client_id"] not in [r["client_id"] for r in _consoles(d)]


def test_vtty_stable_across_recover(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    try:
        d.sw("session", "clear", "--selector", "COM0")
        d.wait_state("COM0", "READY")
        row = next(r for r in _consoles(d) if r["client_id"] == a1["client_id"])
        assert row["vtty"] == a1["vtty"]
        target.emit(b"AFTER-CLEAR\r\n")
        assert b"AFTER-CLEAR" in read_until(fd1, b"AFTER-CLEAR", 3.0)
    finally:
        os.close(fd1)


def test_release_closes_consoles_before_reply(console_daemon) -> None:
    d, _target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    try:
        count = d._session("COM0")["console_count"]
        r = d.sw("device", "release", "--selector", "COM0")
        assert r["ok"] is True and r["closed_consoles"] == count, r
        try:
            data = os.read(fd1, 1024)
            assert data == b"", data
        except BlockingIOError:
            raise AssertionError("release 回覆後 console 仍未關閉") from None
        except OSError:
            pass  # EIO：master 已關
        assert d._session("COM0")["state"] == "RELEASED"
    finally:
        os.close(fd1)


def test_unread_console_does_not_block(console_daemon) -> None:
    d, target = console_daemon
    d.sw("session", "console-attach", "--selector", "COM0", "--label", "h3")  # 從不讀取
    chunk = b"y" * 65536
    for _ in range(32):  # 2 MiB
        target.emit(chunk)
    sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo OK", "--cmd-timeout", "5")
    assert sub["ok"] is True, sub
    done = d.wait_command_done(sub["cmd_id"], timeout=5.0)
    assert done["status"] == "done", done
