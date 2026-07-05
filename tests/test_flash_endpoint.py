"""tests/test_flash_endpoint.py — FlashEndpoint 單元測試。

端點設計：一律沉默，只在 client 寫入（flasher sync）時走 flash 路徑。
支援家族清單改用 `serialwrap mcu patterns`，不經此 PTY（避免汙染 flasher）。
"""
import os
import sys
import time
import tempfile
import pytest
from sw_core.flash_endpoint import FlashEndpoint

# FlashEndpoint PTY 功能為 POSIX-only（#84 PORT-4）；Windows 上 start() 為 no-op，測試直接跳過。
pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only: PTY/symlink 功能在 Windows 不適用（#84 PORT-4）",
)


def _open_noctty(path: str, flags: int) -> int:
    """測試開 PTY slave 時避免取得 controlling terminal，防止 close 時收到 SIGHUP。"""
    return os.open(path, flags | getattr(os, "O_NOCTTY", 0))


def test_creates_pty_and_symlink():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link)
        ep.start()
        try:
            assert os.path.islink(link)
            assert os.path.exists(os.path.realpath(link))
        finally:
            ep.stop()
        assert not os.path.exists(link)  # stop 後清除 symlink


def test_endpoint_stays_silent_when_no_flash():
    """端點未在 bridge 時絕不主動寫 bytes——否則會汙染 flasher 的 SBL sync（真機實證）。"""
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, on_flash_open=None)
        ep.start()
        try:
            fd = _open_noctty(link, os.O_RDONLY | os.O_NONBLOCK)
            try:
                time.sleep(0.8)            # 等數個 loop 週期
                got = b""
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        got += chunk
                except BlockingIOError:
                    pass
                assert got == b""          # 完全沒有非請求的輸出
            finally:
                os.close(fd)
        finally:
            ep.stop()


def test_silent_even_after_client_write_when_no_match():
    """flasher 寫入但偵測未命中（on_flash_open=None）→ 端點仍不得回寫任何 bytes（no-match 沉默）。"""
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, on_flash_open=None)
        ep.start()
        try:
            fd = _open_noctty(link, os.O_RDWR | os.O_NONBLOCK)
            try:
                os.write(fd, b"\x55\x55")   # 模擬 flasher sync
                time.sleep(0.6)
                got = b""
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        got += chunk
                except BlockingIOError:
                    pass
                assert got == b""          # 端點保持沉默，flasher 走自身 retry/timeout
            finally:
                os.close(fd)
        finally:
            ep.stop()


def test_is_flashing_false_when_idle():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link)
        ep.start()
        try:
            assert ep.is_flashing() is False
        finally:
            ep.stop()


def test_endpoint_slave_is_raw_for_byte_transparency():
    """真實 flasher 開的就是這個 slave；必須是 raw，否則 SBL binary 會被 line discipline 汙染。"""
    import termios
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link)
        ep.start()
        try:
            fd = _open_noctty(link, os.O_RDWR | os.O_NONBLOCK)
            try:
                oflag, lflag = termios.tcgetattr(fd)[1], termios.tcgetattr(fd)[3]
                assert not (lflag & termios.ICANON)   # 非 canonical（不行緩衝）
                assert not (oflag & termios.OPOST)     # 無輸出處理（不會 LF→CRLF）
            finally:
                os.close(fd)
        finally:
            ep.stop()


def test_loop_survives_on_flash_open_exception():
    """on_flash_open 拋例外時，端點 _loop 執行緒須存活且清除 active flag（C1 _loop 半）。"""
    def boom(master_fd, slave_fd, first_bytes):
        raise RuntimeError("boom")

    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, on_flash_open=boom)
        ep.start()
        try:
            fd = _open_noctty(link, os.O_RDWR | os.O_NONBLOCK)
            try:
                os.write(fd, b"\x55\x55")        # 觸發 on_flash_open → 拋例外
                time.sleep(0.5)
                assert ep._thread.is_alive()      # 端點執行緒未被殺
                assert ep.is_flashing() is False  # active flag 已清
            finally:
                os.close(fd)
        finally:
            ep.stop()
