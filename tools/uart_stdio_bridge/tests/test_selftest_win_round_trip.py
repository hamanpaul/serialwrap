"""ST-03 win-serial 的回歸測試（假 COM 配對，不開真實序列埠）。

缺陷類別：`WinSerialEndpoint` 若在阻塞 I/O 期間持有單一狀態鎖，寫入阻塞（對端不讀、緩衝區滿）會連帶阻住讀取，
bridge 無法排空接收端，ST-03(a)「一次寫 2 MiB 且不讀，之後再讀」即死鎖（Windows 實機停在 stream_offset ≈ 1 MiB）。

本檔以有界緩衝的假配對重現同一機制（端點層級驗證，ST-03 本身維持原序列、不改寫）：
- `_BoundedPipe` 模擬 com0com 單向驅動緩衝區，滿時寫入阻塞、直到對端讀取。
- `_EchoPath` 取代 bridge＋DUT 的回聲路徑：COM21 收到的 bytes 原樣送回 COM20 的接收端。
"""

from __future__ import annotations

import random
import threading
import time

import pytest

_CAPACITY = 4096          # 每個方向的驅動緩衝區容量（遠小於 payload，必然填滿）
_PAYLOAD_SIZE = 64 * 1024


class SerialTimeoutException(Exception):
    """模擬 pySerial 的 SerialTimeoutException（端點以類別名稱辨識）。"""


class _BoundedPipe:
    """單向有界緩衝：滿時寫入阻塞，直到讀取端取走資料；write_timeout 為 None 時無限等待。"""

    def __init__(self, capacity: int = _CAPACITY) -> None:
        self._capacity = capacity
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False

    def put_some(self, data: bytes, timeout: float | None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while len(self._buf) >= self._capacity:
                if self._closed:
                    raise OSError("管線已關閉")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise SerialTimeoutException("寫入逾時")
                self._cond.wait(remaining)
            if self._closed:
                raise OSError("管線已關閉")
            accepted = min(len(data), self._capacity - len(self._buf))
            self._buf.extend(data[:accepted])
            self._cond.notify_all()
            return accepted

    def get_some(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._buf:
                remaining = deadline - time.monotonic()
                if self._closed or remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            out = bytes(self._buf[:size])
            del self._buf[:size]
            self._cond.notify_all()
            return out

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class _FakeSerialPort:
    """介面近似 pySerial 的假埠：write 寫入 tx 管線、read 從 rx 管線讀取。"""

    def __init__(self, tx: _BoundedPipe, rx: _BoundedPipe) -> None:
        self._tx = tx
        self._rx = rx
        self.timeout: float | None = None
        self.write_timeout: float | None = None

    def write(self, data: bytes) -> int:
        """與 pySerial 相同：在 write_timeout 內持續寫入直到全部接受，逾時拋出。"""
        deadline = None if self.write_timeout is None else time.monotonic() + self.write_timeout
        view = memoryview(bytes(data))
        sent = 0
        while sent < len(view):
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            sent += self._tx.put_some(bytes(view[sent:]), remaining)
        return sent

    def read(self, size: int) -> bytes:
        return self._rx.get_some(size, self.timeout if self.timeout is not None else 1.0)


class _EchoPath:
    """以執行緒模擬 bridge＋DUT 回聲：COM21 收到的每個位元組原樣寫回 COM20 的接收端。"""

    def __init__(self, a_to_b: _BoundedPipe, b_to_a: _BoundedPipe) -> None:
        self._a_to_b = a_to_b
        self._b_to_a = b_to_a
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                chunk = self._a_to_b.get_some(4096, 0.05)
                pending = memoryview(chunk)
                while pending:
                    # 回聲寫入無逾時（與 bridge 寫入 DUT 同樣會阻塞），才會重現緩衝區死鎖。
                    pending = pending[self._b_to_a.put_some(bytes(pending), None):]
        except OSError:
            return

    def stop(self) -> None:
        self._stop.set()


@pytest.fixture
def com_pair():
    """回傳以管線字典表示的假配對；結束時關閉管線，釋放所有仍阻塞中的 thread。"""
    a_to_b = _BoundedPipe()
    b_to_a = _BoundedPipe()
    yield {"a_to_b": a_to_b, "b_to_a": b_to_a}
    a_to_b.close()
    b_to_a.close()


def _payload(size: int, seed: int = 20261015) -> bytes:
    return random.Random(seed).randbytes(size)


def _endpoint(com_pair) -> tuple[object, _FakeSerialPort]:
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    port = _FakeSerialPort(com_pair["a_to_b"], com_pair["b_to_a"])
    ep = WinSerialEndpoint("COM-TEST", serial_factory=lambda **_kw: port)
    ep.open()
    return ep, port


def test_naive_write_then_read_deadlocks_when_buffers_fill(com_pair):
    """缺陷機制重現：無 write_timeout 的整段阻塞寫入，於緩衝區填滿後永遠無法前進（對端不讀）。"""
    port = _FakeSerialPort(com_pair["a_to_b"], com_pair["b_to_a"])
    echo = _EchoPath(com_pair["a_to_b"], com_pair["b_to_a"])
    echo.start()
    data = _payload(_PAYLOAD_SIZE)
    outcome: dict[str, object] = {}

    def _naive() -> None:
        try:
            port.write_timeout = None          # 無 write_timeout：單次寫入整段 payload（阻塞）
            port.write(data)
            outcome["read"] = bytes(port.read(len(data)))
        except BaseException as exc:  # pragma: no cover - 僅在缺陷修正後錯誤路徑才會到達
            outcome["error"] = exc

    worker = threading.Thread(target=_naive, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    echo.stop()
    assert worker.is_alive(), "預期無 write_timeout 的阻塞寫入在緩衝區填滿後死鎖"
    assert "read" not in outcome


def test_endpoint_read_is_not_blocked_by_blocked_write(com_pair):
    """端點層級：寫入阻塞（tx 滿、對端不讀）時，讀取必須仍可前進（不得共用阻塞期間的狀態鎖）。"""
    ep, _port = _endpoint(com_pair)
    com_pair["b_to_a"].put_some(b"hello", None)            # rx 端已有資料可取
    com_pair["a_to_b"].put_some(bytes(_CAPACITY), None)     # tx 端填滿，後續寫入必然阻塞
    write_result: dict[str, object] = {}

    def _writer() -> None:
        try:
            write_result["n"] = ep.write_some(b"z" * _CAPACITY, timeout_s=3.0)
        except BaseException as exc:
            write_result["err"] = exc

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    time.sleep(0.2)                                        # 讓寫入確實進入阻塞
    started = time.monotonic()
    got = ep.read(16, timeout_s=1.0)
    elapsed = time.monotonic() - started
    writer.join(timeout=10.0)
    assert got == b"hello"
    assert elapsed < 1.0, f"寫入阻塞期間讀取被延後 {elapsed:.2f} 秒（端點共用狀態鎖）"
    assert not writer.is_alive()


def test_endpoint_round_trip_completes_when_buffers_fill(com_pair):
    """端點層級往返：2×緩衝容量以上的 payload 經回聲路徑逐位元組全等回到讀取端，不死鎖。"""
    ep, _port = _endpoint(com_pair)
    echo = _EchoPath(com_pair["a_to_b"], com_pair["b_to_a"])
    echo.start()
    data = _payload(_PAYLOAD_SIZE)
    write_error: list[BaseException] = []

    def _send() -> None:
        try:
            off = 0
            while off < len(data):
                n = ep.write_some(data[off : off + 4096], timeout_s=10.0)
                if n <= 0:
                    raise OSError(f"寫入無進展（offset={off}）")
                off += n
        except BaseException as exc:
            write_error.append(exc)

    sender = threading.Thread(target=_send, daemon=True)
    sender.start()
    recvd = bytearray()
    deadline = time.monotonic() + 30.0
    while len(recvd) < len(data) and not write_error and time.monotonic() < deadline:
        recvd.extend(ep.read(min(65536, len(data) - len(recvd)), timeout_s=0.5))
    sender.join(timeout=5.0)
    echo.stop()
    assert not write_error, write_error
    assert len(recvd) == len(data), f"往返未完成：{len(recvd)}/{len(data)}"
    assert bytes(recvd) == data


def test_endpoint_write_fails_closed_when_dut_is_silent(com_pair):
    """DUT 不回聲、緩衝區滿時，寫入必須在期限內結束（回 0 或拋出例外），不得無限阻塞。"""
    from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain

    ep, _port = _endpoint(com_pair)
    com_pair["a_to_b"].put_some(bytes(_CAPACITY), None)
    started = time.monotonic()
    try:
        n = ep.write_some(b"s" * _CAPACITY, timeout_s=0.5)
    except EndpointWriteUncertain:
        n = None
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"靜默 DUT 的寫入超過期限（{elapsed:.2f} 秒）"
    assert n in (0, None)


def test_win_write_all_rejects_short_write_count():
    """ST-03(a) Windows 分支：寫入回報長度不足時必須失敗，不得當成功。"""
    from uart_stdio_bridge.selftest.checks import _win_write_all

    class _ShortPort:
        write_timeout = None

        def write(self, data: bytes) -> int:
            return len(data) - 1

    with pytest.raises(OSError, match="寫入不完整"):
        _win_write_all(_ShortPort(), b"x" * 16, timeout_s=1.0)


def test_win_read_exact_fails_closed_when_silent():
    """ST-03(a) Windows 分支：期限內未讀滿必須 TimeoutError，不得回傳部分資料當成功。"""
    from uart_stdio_bridge.selftest.checks import _win_read_exact

    class _SilentPort:
        timeout = None

        def read(self, size: int) -> bytes:
            time.sleep(0.05)
            return b""

    with pytest.raises(TimeoutError):
        _win_read_exact(_SilentPort(), 16, timeout_s=0.3)
