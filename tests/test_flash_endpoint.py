"""tests/test_flash_endpoint.py — FlashEndpoint 單元測試（Task 4）。"""
import os
import time
import tempfile
from sw_core.mcu_patterns import McuPatternRegistry
from sw_core.flash_endpoint import FlashEndpoint


def _read_available(path, wait=0.6):
    """非阻塞開啟 + 等 daemon 至少寫一次 listing，讀出當下可得 bytes（不依賴 EOF）。"""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        time.sleep(wait)
        chunks = []
        try:
            while True:
                b = os.read(fd, 4096)
                if not b:
                    break
                chunks.append(b)
        except BlockingIOError:
            pass
        return b"".join(chunks)
    finally:
        os.close(fd)


def test_creates_pty_and_symlink():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [])
        ep.start()
        try:
            assert os.path.islink(link)
            assert os.path.exists(os.path.realpath(link))
        finally:
            ep.stop()
        assert not os.path.exists(link)  # stop 後清除 symlink


def test_readonly_open_returns_support_list():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [], idle_list_interval=0.2)
        ep.start()
        try:
            data = _read_available(link, wait=0.6)
            assert b"ti-cc26xx" in data
        finally:
            ep.stop()


def test_is_flashing_false_when_idle():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [])
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
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [])
        ep.start()
        try:
            fd = os.open(link, os.O_RDWR | os.O_NONBLOCK)
            try:
                oflag, lflag = termios.tcgetattr(fd)[1], termios.tcgetattr(fd)[3]
                assert not (lflag & termios.ICANON)   # 非 canonical（不行緩衝）
                assert not (oflag & termios.OPOST)     # 無輸出處理（不會 LF→CRLF）
            finally:
                os.close(fd)
        finally:
            ep.stop()


def test_no_idle_list_during_flasher_cooldown():
    """flasher 寫入後的 cool-down 內，端點不得寫支援清單（no-match SHALL 沉默）（I1）。"""
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "dev", "ttyMCU")
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [], idle_list_interval=0.1,
                           client_cooldown=5.0)
        ep.start()
        try:
            fd = os.open(link, os.O_RDWR | os.O_NONBLOCK)
            try:
                os.write(fd, b"\x55\x55")   # 模擬 flasher sync 寫入
                time.sleep(0.6)
                data = b""
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        data += chunk
                except BlockingIOError:
                    pass
                assert b"ti-cc26xx" not in data   # cool-down 內保持沉默
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
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [], on_flash_open=boom,
                           idle_list_interval=0.1, client_cooldown=0.1)
        ep.start()
        try:
            fd = os.open(link, os.O_RDWR | os.O_NONBLOCK)
            try:
                os.write(fd, b"\x55\x55")        # 觸發 on_flash_open → 拋例外
                time.sleep(0.5)
                assert ep._thread.is_alive()      # 端點執行緒未被殺
                assert ep.is_flashing() is False  # active flag 已清
            finally:
                os.close(fd)
        finally:
            ep.stop()
