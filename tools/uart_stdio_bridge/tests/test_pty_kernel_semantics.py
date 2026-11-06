"""tools/uart_stdio_bridge/tests/test_pty_kernel_semantics.py

POSIX PTY 核心語意的事實測試（P2-T3，plan 03-stage0-test-infra.md Task 3 T3-1）。

這些測試不依賴 `uart_stdio_bridge` 的任何實作，直接釘住 WSL2 Linux 6.6 上的核心行為：
- slave 無 opener 時 master 的 poll 回 POLLHUP、read 得 EIO，但 write 到 master 仍成功，
  bytes 交給下一個 opener；
- tty.setraw(slave) 後關閉 slave，raw termios 狀態在 pty 存續期間保留給下一個 opener。

預期結果：本檔在 T3-2 即應 PASS（事實測試）；若 FAIL 代表核心語意與計畫假設不同，須停止並回報。
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.posix_pty

if not sys.platform.startswith("linux"):
    pytest.skip("cap:posix_pty", allow_module_level=True)

import os  # noqa: E402
import pty  # noqa: E402
import select  # noqa: E402
import termios  # noqa: E402
import tty  # noqa: E402


def test_master_pollhup_and_write_when_no_slave_opener():
    m, s = pty.openpty()
    try:
        tty.setraw(s)
        path = os.ttyname(s)
        os.close(s)
        s = -1

        p = select.poll()
        p.register(m, select.POLLIN | select.POLLHUP)
        assert any(ev & select.POLLHUP for _, ev in p.poll(100))
        assert os.write(m, b"abc") == 3  # 無 opener 仍接受寫入

        s2 = os.open(path, os.O_RDWR | os.O_NOCTTY)
        try:
            assert os.read(s2, 10) == b"abc"  # 交給下一個 opener
            attrs = termios.tcgetattr(s2)
            assert not (attrs[3] & termios.ECHO) and not (attrs[3] & termios.ICANON)  # raw 保留
        finally:
            os.close(s2)
    finally:
        if s != -1:
            os.close(s)
        os.close(m)
