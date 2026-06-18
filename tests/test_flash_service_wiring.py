"""tests/test_flash_service_wiring.py — SerialwrapService flash 接線 smoke tests。

無真實裝置；驗證：
  1. _on_flash_open 在無候選時靜默結束（不 raise、不進入 flashing 狀態）。
  2. _flash_rx_observer 正確維護 _flash_rx_buffers（probe 用）。
  3. _flash_rx_observer 在 active flash 期間把 bytes 寫入 master_fd。
  4. _BridgeProbe.probe 在無 session 時回傳 False（不 raise）。
"""
import os
import pty
import threading
import time

from sw_core.service import SerialwrapService


def test_on_flash_open_no_candidates_is_silent():
    """沒有任何候選 → detect_mcu_line 回 'none' → 不應 raise、不應進入 flashing 狀態。"""
    svc = SerialwrapService([])
    master, slave = pty.openpty()
    try:
        # 應靜默結束，不 raise
        svc._on_flash_open(master, slave, b"\x55\x55")
        assert svc._flash_endpoint.is_flashing() is False
    finally:
        os.close(master)
        os.close(slave)


def test_flash_rx_observer_fills_probe_buffer():
    """_flash_rx_observer 應把 RX bytes 累積到 _flash_rx_buffers[com]。"""
    svc = SerialwrapService([])
    com = "/dev/ttyUSB0"
    # 先建立 buffer entry（模擬 _BridgeProbe.probe 初始化的動作）
    with svc._flash_lock:
        svc._flash_rx_buffers[com] = bytearray()
    svc._flash_rx_observer(com, b"\xAA\xBB", wal_seq=1)
    svc._flash_rx_observer(com, b"\xCC", wal_seq=2)
    with svc._flash_lock:
        buf = bytes(svc._flash_rx_buffers[com])
    assert buf == b"\xAA\xBB\xCC"


def test_flash_rx_observer_ignores_unregistered_com():
    """未在 _flash_rx_buffers 中的 com 不應 raise，也不應建立 entry（不記錄多餘狀態）。"""
    svc = SerialwrapService([])
    # 這個 com 沒有預先建立 buffer entry
    svc._flash_rx_observer("/dev/ttyUSB99", b"\x00", wal_seq=0)
    with svc._flash_lock:
        assert "/dev/ttyUSB99" not in svc._flash_rx_buffers


def test_flash_rx_observer_writes_to_master_when_active():
    """active flash 期間，_flash_rx_observer 應把 RX bytes 寫入 _flash_master_fd。

    PTY 方向：master 寫入 → slave 讀出。
    需將 slave 設 raw mode 避免 line discipline 丟棄特殊字元。
    """
    import tty
    svc = SerialwrapService([])
    master, slave = pty.openpty()
    tty.setraw(slave)                 # raw mode：避免 line discipline 干擾
    os.set_blocking(slave, False)
    com = "/dev/ttyUSB0"
    try:
        with svc._flash_lock:
            svc._flash_active_com = com
            svc._flash_master_fd = master
        svc._flash_rx_observer(com, b"\xDE\xAD", wal_seq=5)
        time.sleep(0.15)
        data = os.read(slave, 16)
        assert data == b"\xDE\xAD", f"device RX 應透過 master_fd 送給 flasher，got {data!r}"
    finally:
        with svc._flash_lock:
            svc._flash_active_com = None
            svc._flash_master_fd = None
        os.close(master)
        os.close(slave)


def test_bridge_probe_returns_false_when_no_session():
    """_BridgeProbe.probe 在 by_id 無對應 session 時回傳 False，不 raise。"""
    from sw_core.service import _BridgeProbe
    svc = SerialwrapService([])
    probe = _BridgeProbe(svc, {"usb-id-x": "/dev/ttyUSB0"})
    result = probe.probe("usb-id-x", b"\x00", b"\xAA", timeout_ms=100)
    assert result is False


def test_flash_rx_buffer_capped_at_4096():
    """_flash_rx_buffers 超過 4096 bytes 時，應截斷保留最後 4096 bytes。"""
    svc = SerialwrapService([])
    com = "/dev/ttyUSB0"
    with svc._flash_lock:
        svc._flash_rx_buffers[com] = bytearray()
    # 送入 5000 bytes
    svc._flash_rx_observer(com, bytes(5000), wal_seq=1)
    with svc._flash_lock:
        buf = svc._flash_rx_buffers[com]
    assert len(buf) <= 4096, f"buffer 應被截斷至 4096，實際 {len(buf)}"
