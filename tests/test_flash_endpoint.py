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
