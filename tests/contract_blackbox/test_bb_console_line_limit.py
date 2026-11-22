"""`cmd submit` 遵守 profile 的 `max_console_line_chars`（console 單行上限）。

外部 CLI＋FakeTarget 觀測；Python 基線與 Rust（`SERIALWRAP_AB_LABEL=rust-*`）皆須通過（同一組斷言即 A/B）。
超過上限回 `CMD_TOO_LONG`（`limit` 為 profile 的值、`cmd_length` 為 UTF-8 bytes），DUT 零 TX；等於上限照常執行。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import time

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

HINT = ("Command exceeds this console's line limit (profile max_console_line_chars) and would be truncated by the "
        "target. Shorten it, or push a script with file push and run that instead.")


def _wait_done(p7, cmd_id: str) -> dict:
    deadline = time.monotonic() + 20.0
    rec = None
    while time.monotonic() < deadline:
        _, st = p7.cli("cmd", "status", "--cmd-id", cmd_id)
        rec = (st or {}).get("command")
        if isinstance(rec, dict) and rec.get("done_at"):
            return rec
        time.sleep(0.3)
    raise AssertionError(f"命令未在期限內結案：{rec}")


def test_submit_over_profile_line_limit_rejected_zero_tx(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell_line_limit", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    mark = tgt.rx_mark()
    cmd = "echo " + "中" * 6   # 5 + 18 = 23 UTF-8 bytes
    rc, j = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", cmd)
    assert rc == 2, (rc, j)
    assert {k: j.get(k) for k in ("ok", "error_code", "cmd_length", "limit", "hint")} == {
        "ok": False, "error_code": "CMD_TOO_LONG", "cmd_length": 23, "limit": 20, "hint": HINT}, j
    time.sleep(1.0)
    assert tgt.rx_log_since(mark) == b"", "被拒的命令不得有任何 TX"
    rc, j = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "echo " + "A" * 15)   # 恰 20 bytes
    assert rc == 0 and j and j.get("ok") is True, (rc, j)
    rec = _wait_done(p7, j["cmd_id"])
    assert (rec["status"], rec["error_code"]) == ("done", None), rec
    assert tgt.rx_log_since(mark).startswith(b"echo " + b"A" * 15 + b"\n"), tgt.rx_log_since(mark)
