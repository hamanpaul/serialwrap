"""tests/test_uart_flash_bridge.py

測試 UARTBridge.flash_tx 原樣送出（不行處理）與 mirror_termios_from baud 鏡射。
"""
import threading
import time

from sw_core.uart_io import UARTBridge
from sw_core.config import UartProfile


class _RecordingWal:
    def __init__(self): self.records = []
    def append(self, **kw): self.records.append(kw)


def _bridge():
    prof = UartProfile(baud=115200, data_bits=8, parity="N", stop_bits=1,
                       flow_control="none", xonxoff=False)
    return UARTBridge("COM0", "/dev/null", prof, _RecordingWal())


def test_flash_tx_is_byte_exact(monkeypatch):
    b = _bridge()
    sent = bytearray()
    monkeypatch.setattr(b, "send_bytes",
                        lambda payload, **kw: sent.extend(payload))
    payload = bytes([0x08, 0x0A, 0x0D, 0x7F, 0x55, 0x00])
    b.flash_tx(payload)                       # 新 API：flash 模式 TX
    assert bytes(sent) == payload             # 無退格/斷行/行組合汙染


def test_flash_mode_blocks_console_injection(monkeypatch):
    """FLASHING 期間 console→device 注入須被丟棄，避免汙染 SBL binary（C2）。"""
    b = _bridge()
    # 讓 human-owner raw 送出路徑可達（否則測試空轉、移掉 fix 也會過）。
    b._interactive_owner = "human:c1"
    sent = []
    monkeypatch.setattr(b, "send_bytes", lambda *a, **k: sent.append(a))

    class _Client:
        client_id = "c1"
        master_fd = -1
        tx_buffer = bytearray()

    # flash OFF：human owner 輸入應送達 → 證明本測試非空轉。
    b.set_flash_mode(False)
    b._handle_console_rx(_Client(), b"hello\n")
    assert len(sent) == 1

    # flash ON：注入應被完全丟棄。
    sent.clear()
    b.set_flash_mode(True)
    b._handle_console_rx(_Client(), b"echo pwn\n")
    assert sent == []
    b.set_flash_mode(False)


def test_flash_mode_drops_non_flash_send_bytes(monkeypatch):
    """FLASHING 期間，非 flasher 來源（system probe / reconcile 自動重探等）的 send_bytes
    必須在寫入 choke point 被丟棄，避免競態下汙染 SBL binary 串流（C2，#69 Finding 1）。"""
    b = _bridge()
    written = []
    monkeypatch.setattr(b, "_write_all", lambda fd, payload: written.append(payload))
    b._serial_fd = 1  # 假 fd，讓非 flash 路徑能走到 _write_all（已被 monkeypatch）

    b.set_flash_mode(True)
    # system probe 寫入（login_fsm.probe_ready/ensure_ready 走的就是 source="system"）→ 應被丟棄
    b.send_bytes(b"\rprobe\n", source="system")
    assert written == []
    # flasher 自身仍可寫
    b.flash_tx(bytes([0x80, 0x55, 0x00]))
    assert written == [bytes([0x80, 0x55, 0x00])]

    # flash OFF 後，system 寫入恢復正常
    b.set_flash_mode(False)
    b.send_bytes(b"cmd\n", source="system")
    assert written == [bytes([0x80, 0x55, 0x00]), b"cmd\n"]


def test_set_flash_mode_serializes_with_in_flight_write(monkeypatch):
    """set_flash_mode 須與寫入序列化（共用 _write_lock），使 flash 開啟不會插進
    『檢查 flash_mode → 實際寫入』的空隙、讓非 flash byte 在 flash 開始後仍寫出（#69 Finding 2 round2）。"""
    b = _bridge()
    b._serial_fd = 1
    monkeypatch.setattr(b, "_write_all", lambda fd, payload: None)

    b._write_lock.acquire()  # 模擬一筆寫入正持有 _write_lock
    done = []

    def flip():
        b.set_flash_mode(True)
        done.append(True)

    t = threading.Thread(target=flip)
    t.start()
    time.sleep(0.15)
    assert done == []              # set_flash_mode 被 _write_lock 擋住，旗標尚未翻轉
    assert b._flash_mode is False
    b._write_lock.release()
    t.join(2.0)
    assert done == [True]
    assert b._flash_mode is True
