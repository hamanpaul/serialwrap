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
