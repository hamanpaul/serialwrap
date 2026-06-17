"""tests/test_flash_pump.py — pump/sink 輔助函式 byte-perfect 單元測試。

PTY 注意事項：
  - slave fd 需設 raw mode（tty.setraw），否則 PTY line discipline 會轉換控制字元
    （例如 0x0A → 0x0D 0x0A），導致 byte-perfect 驗證失敗。
  - master fd 設 non-blocking，以便 pump 使用 select 多路等待。
"""
import os
import pty
import threading
import time
import tty

from sw_core.flash_endpoint import pump_endpoint_to_sink, make_rx_to_endpoint_writer


class _Sink:
    def __init__(self):
        self.got = bytearray()

    def flash_tx(self, payload):
        self.got.extend(payload)


def test_pump_forwards_bytes_byte_perfect_and_stops_on_eof():
    """pump_endpoint_to_sink 應完整轉送所有 bytes（含控制字元），並在 EOF 後自行結束。"""
    master, slave = pty.openpty()
    # 設 raw mode 避免 PTY line discipline 轉換控制字元（byte-perfect 核心要求）
    tty.setraw(slave)
    os.set_blocking(master, False)
    sink = _Sink()
    stop = threading.Event()
    t = threading.Thread(
        target=pump_endpoint_to_sink,
        args=(master, sink, stop),
        kwargs={"first_bytes": b"\x55\x55"},
    )
    t.start()
    # 含 NUL、控制字元，驗證 byte-perfect 轉送
    payload = bytes([0x08, 0x0A, 0x0D, 0x7F, 0x00, 0xCC, 0xAB])
    os.write(slave, payload)          # 模擬 flasher 從 slave 端寫入
    time.sleep(0.4)
    os.close(slave)                   # EOF → pump 應結束
    t.join(timeout=2.0)
    assert not t.is_alive(), "pump 執行緒應在 EOF 後結束"
    assert bytes(sink.got) == b"\x55\x55" + payload, (
        f"轉送內容必須 byte-perfect（含 first_bytes）\n"
        f"  got: {bytes(sink.got)!r}\n"
        f"  exp: {b'\\x55\\x55' + payload!r}"
    )
    stop.set()
    os.close(master)


def test_pump_stops_on_stop_event():
    """stop_event 設定後，pump 應在下個 select timeout 結束，不需要 EOF。"""
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    sink = _Sink()
    stop = threading.Event()
    t = threading.Thread(target=pump_endpoint_to_sink, args=(master, sink, stop))
    t.start()
    time.sleep(0.05)
    stop.set()                        # 觸發停止，不送 EOF
    t.join(timeout=1.5)
    assert not t.is_alive(), "stop_event 設定後 pump 應在 select timeout 內結束"
    os.close(master)
    os.close(slave)


def test_pump_no_first_bytes():
    """first_bytes 為空時，pump 仍正常運作（不額外送空 bytes）。"""
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    sink = _Sink()
    stop = threading.Event()
    t = threading.Thread(
        target=pump_endpoint_to_sink,
        args=(master, sink, stop),
        kwargs={"first_bytes": b""},
    )
    t.start()
    os.write(slave, b"\xDE\xAD\xBE\xEF")
    time.sleep(0.3)
    os.close(slave)
    t.join(timeout=2.0)
    assert bytes(sink.got) == b"\xDE\xAD\xBE\xEF"
    stop.set()
    os.close(master)


def test_rx_writer_writes_to_master():
    """make_rx_to_endpoint_writer 產生的 callback 應把 bytes 寫入 master fd（device RX → endpoint）。

    方向：callback 寫入 master → 從 slave 讀出（PTY 標準方向）。
    需先設 slave raw mode 避免 line discipline 截斷。
    """
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(slave, False)
    w = make_rx_to_endpoint_writer(master)
    w(b"\x00\xcc")
    time.sleep(0.15)                  # 等 PTY 緩衝區把 bytes 送到 slave 端
    data = os.read(slave, 16)
    assert data == b"\x00\xcc", f"callback 寫入的 bytes 應在 slave 端可讀出，got {data!r}"
    os.close(master)
    os.close(slave)


def test_rx_writer_tolerates_closed_fd():
    """master fd 已關閉時，callback 應靜默吞掉 OSError，不 raise。"""
    master, slave = pty.openpty()
    w = make_rx_to_endpoint_writer(master)
    os.close(master)
    os.close(slave)
    # 不應 raise
    w(b"\xFF\xFF\xFF")
