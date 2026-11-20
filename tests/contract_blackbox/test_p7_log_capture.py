"""P7 Task 18（08-slice23-login-events.md）：agent log capture 黑箱（reader 端 observer，DF-15）。

`session log-start` 之後的背景 RX 進 capture 檔；前景命令期間的輸出（含命令回顯）不進 capture
（Python `_on_bridge_rx`：foreground 進行中不寫）。外部 CLI＋FakeTarget＋檔案觀測。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]


def test_capture_skips_foreground(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    rc, j = p7.cli("session", "log-start", "--selector", "COM0")
    assert rc == 0 and j["ok"] is True, (rc, j)
    path = Path(j["log_path"])
    tgt.emit("bg-line-before\r\n")
    time.sleep(0.5)
    rc, sub = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "echo fg-output-marker")
    assert rc == 0 and sub["ok"] is True, (rc, sub)
    deadline = time.monotonic() + 15.0
    while True:
        _, st = p7.cli("cmd", "status", "--cmd-id", sub["cmd_id"])
        if (st or {}).get("command", {}).get("done_at"):
            break
        assert time.monotonic() < deadline, st
        time.sleep(0.2)
    assert st["command"]["status"] == "done" and "fg-output-marker" in st["command"]["stdout"], st
    tgt.emit("bg-line-after\r\n")
    time.sleep(0.5)
    rc, status = p7.cli("session", "log-status", "--selector", "COM0")
    assert rc == 0 and status["active"] is True, (rc, status)
    rc, j = p7.cli("session", "log-stop", "--selector", "COM0")
    assert rc == 0 and j["ok"] is True, (rc, j)
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "bg-line-before" in text and "bg-line-after" in text, text
    assert "fg-output-marker" not in text, f"前景命令輸出不得進 capture：{text!r}"
    lines = [ln for ln in text.splitlines() if ln]
    assert lines == ["bg-line-before", "bg-line-after"], lines
    assert (status["line_count"], status["byte_count"]) == (2, len("bg-line-before\nbg-line-after\n")), status
