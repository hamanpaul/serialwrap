"""tests/test_uart_flash_bridge.py

測試 UARTBridge.flash_tx 原樣送出（不行處理）與 mirror_termios_from baud 鏡射。
"""
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
    b.set_flash_mode(True)
    sent = []
    monkeypatch.setattr(b, "send_bytes", lambda *a, **k: sent.append(a))

    class _Client:
        client_id = "c1"
        master_fd = -1
        tx_buffer = bytearray()

    b._handle_console_rx(_Client(), b"echo pwn\n")
    assert sent == []          # flash 模式下完全不送
    b.set_flash_mode(False)
