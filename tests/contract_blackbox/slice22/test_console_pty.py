"""Slice 2.2 PTY console 黑箱契約（07 Task 11；Python 基線與 Rust artifact 雙邊執行）。"""
from __future__ import annotations

import json
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
    d, target = console_daemon
    before = len(target.written(0.2))
    r = d.rpc("session.console_detach", {"selector": "COM0", "client_id": "nope"})
    assert {k: r[k] for k in ("ok", "error_code", "client_id")} == {
        "ok": False,
        "error_code": "CONSOLE_NOT_FOUND",
        "client_id": "nope",
    }
    assert "version" in r
    assert len(target.written(0.2)) == before, "拒絕路徑零 TX"


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
    assert first, "READY 後 session.vtty_path 已是 bridge primary（session_manager.py:4832）"
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    assert a1["ok"] is True and a2["ok"] is True, (a1, a2)
    for path in (first, a1["vtty"], a2["vtty"]):
        assert re.fullmatch(r"/dev/pts/\d+", path), path
    assert first not in (a1["vtty"], a2["vtty"])
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
        # console_daemon 的 FakeTarget 腳本：`sleep 2` 於 2 s 後回 `sleep 2: ok`（07 Task 11 fixture）
        seq_ok = min(r["seq"] for r in recs if r["dir"] == "RX" and b"sleep 2: ok" in b64_payload(r))
        assert seq_cmd < seq_ok < seq_x, (seq_cmd, seq_ok, seq_x)
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
    # 直接走既有 RPC 減少受限 runner 上 CLI 子行程排隊期間的未連入窗口。
    a1 = d.rpc("session.console_attach", {"selector": "COM0", "label": "h1"})
    # 先持有兩個 PTY，避免慢 runner 在明確 detach 前回收尚未連入的 console。
    fd1 = open_vtty(a1["vtty"])
    fd2 = None
    try:
        a2 = d.rpc("session.console_attach", {"selector": "COM0", "label": "h2"})
        fd2 = open_vtty(a2["vtty"])
        detached = d.rpc("session.console_detach", {"selector": "COM0", "client_id": a1["client_id"]})
        assert detached["ok"] is True, detached
        precondition = _consoles(d)
        assert any(row["client_id"] == a2["client_id"] for row in precondition), precondition
        assert not any(row["interactive_owner"] for row in precondition), precondition
        target_before = len(target.written(0.2))
        payload = b"a" * 16390 + b"\r"
        view = memoryview(payload)
        ownership_trace = []
        while view:
            try:
                n = os.write(fd2, view[:4096])
            except BlockingIOError:
                read_until(fd2, b"\x00", 0.05)
                continue
            view = view[n:]
            read_until(fd2, b"\x00", 0.01)
            ownership_trace.append(_consoles(d))
        sent = wait_rx_contains(target, b"aaaa", 2.0)
        tx_delta = target.written(0.2)[target_before:]
        ownership_violation = any(
            row["interactive_owner"]
            for snapshot in ownership_trace
            for row in snapshot
        )
        diagnostic = {
            "h1_attach": a1,
            "h2_attach": a2,
            "h1_client_id": a1["client_id"],
            "h2_client_id": a2["client_id"],
            "detach_response": detached,
            "detached_client_id": a1["client_id"],
            "initial_consoles": precondition,
            "ownership_trace": ownership_trace,
            "ownership_violation": ownership_violation,
            "stderr": d._stderr_tail(),
            "tx_delta_len": len(tx_delta),
            "tx_delta_hex": tx_delta.hex(),
            "wal_tx": [
                row for row in d.wal_records()
                if row.get("dir") == "TX" and row.get("source") == f"human:{a2['client_id']}"
            ],
        }
        warning_seen = wait_for(lambda: a2["client_id"] in d._stderr_tail(20000), 3.0)
        diagnostic["warning_seen"] = warning_seen
        print(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True))
        assert not diagnostic["ownership_violation"], diagnostic
        assert not sent, diagnostic
        assert warning_seen, diagnostic
    finally:
        if fd2 is not None:
            os.close(fd2)
        os.close(fd1)


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_line_overflow_owner_change_keeps_full_diagnostics(monkeypatch, capsys) -> None:
    class FakeDaemon:
        def __init__(self) -> None:
            self.attach_count = 0
            self.stderr_reads = 0
            self.wal_reads = 0

        def rpc(self, method, _params):
            if method == "session.console_attach":
                self.attach_count += 1
                client_id = f"client-{self.attach_count}"
                return {"ok": True, "client_id": client_id, "vtty": f"/dev/pts/{self.attach_count}"}
            return {"ok": True}

        def _stderr_tail(self, _limit=None):
            self.stderr_reads += 1
            return "client-2 overflow warning"

        def wal_records(self):
            self.wal_reads += 1
            return [{"dir": "TX", "source": "human:client-2", "payload_b64": "YWFhYQ=="}]

    class FakeTarget:
        def __init__(self) -> None:
            self.written_reads = 0

        def written(self, _wait_s=0.0):
            self.written_reads += 1
            return b""

    import sys

    module = sys.modules[__name__]
    daemon = FakeDaemon()
    target = FakeTarget()
    writes: list[bytes] = []
    console_snapshots = 0
    opened_fds = iter((51, 52))
    real_write = os.write
    real_close = os.close

    def fake_write(fd: int, payload) -> int:
        if fd == 52:
            chunk = bytes(payload)
            writes.append(chunk)
            return len(chunk)
        return real_write(fd, payload)

    def fake_close(fd: int) -> None:
        if fd not in (51, 52):
            real_close(fd)

    def fake_consoles(_daemon) -> list[dict]:
        nonlocal console_snapshots
        console_snapshots += 1
        return [{"client_id": "client-2", "interactive_owner": console_snapshots > 1}]

    monkeypatch.setattr(module, "open_vtty", lambda _path: next(opened_fds))
    monkeypatch.setattr(module, "read_until", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(module, "_consoles", fake_consoles)
    monkeypatch.setattr(module, "wait_rx_contains", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "wait_for", lambda predicate, *_args, **_kwargs: predicate())
    monkeypatch.setattr(module.os, "write", fake_write)
    monkeypatch.setattr(module.os, "close", fake_close)

    with pytest.raises(AssertionError) as failure:
        test_line_overflow_warning((daemon, target))

    assert target.written_reads >= 2, target.written_reads
    assert daemon.wal_reads == 1, daemon.wal_reads
    assert daemon.stderr_reads >= 1, daemon.stderr_reads
    assert writes and sum(map(len, writes)) == 16391, sum(map(len, writes))
    diagnostic = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert diagnostic["h1_client_id"] == "client-1", diagnostic
    assert diagnostic["h2_client_id"] == "client-2", diagnostic
    assert diagnostic["ownership_trace"][-1][0]["interactive_owner"] is True, diagnostic
    assert diagnostic["ownership_violation"] is True, diagnostic
    assert diagnostic["tx_delta_hex"] == "", diagnostic
    assert diagnostic["wal_tx"], diagnostic
    assert diagnostic["warning_seen"] is True, diagnostic
    assert "assert not True" in str(failure.value), failure.value


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

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    os.write(fd1, b"a")  # human active
    time.sleep(0.3)
    os.close(fd1)
    before = len(target.written(0.2))
    busy = d.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent"})
    assert busy["ok"] is False and busy["error_code"] == "SESSION_INTERACTIVE_BUSY", busy
    assert len(target.written(0.2)) == before, "拒絕路徑零 TX"
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
