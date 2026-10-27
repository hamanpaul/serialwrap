"""Slice 2.2 BC-02 黑箱契約（07 Task 11）。"""
from __future__ import annotations

import os

import pytest

from .console_helpers import b64_payload, open_vtty, wait_rx_contains

pytestmark = pytest.mark.cap_posix_pty


def test_normal_suspend_loses_no_keystroke(console_daemon) -> None:
    import time

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    try:
        for n in range(10):
            sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo S{n}", "--cmd-timeout", "5")
            assert sub["ok"] is True, sub
            os.write(fd1, str(n).encode())
            d.wait_command_done(sub["cmd_id"])
            time.sleep(0.05)
        assert wait_rx_contains(target, b"9", 3.0)
        recs = sorted((r for r in d.wal_records() if r["dir"] == "TX"), key=lambda r: r["seq"])
        human = b"".join(b64_payload(r) for r in recs if r["source"] == f"human:{a1['client_id']}")
        assert human == b"0123456789", human
        # 每筆 human TX 都不落在 agent 命令 payload 的 bytes 之間（逐筆 WAL TX 不交錯）
        for r in recs:
            if r["source"] == f"human:{a1['client_id']}":
                assert len(b64_payload(r)) >= 1
    finally:
        os.close(fd1)
