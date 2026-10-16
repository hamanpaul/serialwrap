"""tools/uart_stdio_bridge/tests/test_win_serial_endpoint.py

Windows pySerial 端點 adapter `uart_stdio_bridge.endpoints.win_serial.WinSerialEndpoint` 的失敗測試
（P2-T3，TDD 紅燈；plan 03-stage0-test-infra.md Task 3 T3-1 表格）。

- 不需 Windows：以注入的 `serial_factory`（FakeSerial）與假 `serial` 模組（`sys.modules` 替身，
  僅提供 `SerialException`、`SerialTimeoutException`）驗 adapter 邏輯；Linux 與 Windows hosted CI 皆可跑。
  這些 fake 結果**不得**計為 `windows_com` 證據（OD-15、§11.2）。
- `test_cap_windows_com__*` 需真實 COM pair（`SW_TEST_COM_PAIR=COMa:COMb`，擁有者 host 實跑）；
  未設時由 conftest 以 `cap:windows_com 未提供（證據不足，非 PASS）` 顯示 skipped。
- 所有 `WinSerialEndpoint` 相關 import 皆在測試函式內進行（P0 慣例）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
from typing import Any

import pytest


# ---------------------------------------------------------------- 測試替身


class FakeSerial:
    """最小 pySerial 替身：記錄 write、可注入回傳長度或例外。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self._timeout = kwargs.get("timeout")
        self._write_timeout = kwargs.get("write_timeout")
        self.timeout_assignments = 0
        self.write_timeout_assignments = 0
        self.write_return: int | None = None  # None → 回傳 len(data)
        self.write_exc: BaseException | None = None
        self.writes: list[bytes] = []
        self.read_data = bytearray()
        self.closed = False

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self.timeout_assignments += 1
        self._timeout = value

    @property
    def write_timeout(self) -> float | None:
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value: float | None) -> None:
        self.write_timeout_assignments += 1
        self._write_timeout = value

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        if self.write_exc is not None:
            raise self.write_exc
        return len(data) if self.write_return is None else self.write_return

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self.read_data[:size])
        del self.read_data[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


class RecordingFactory:
    """`serial_factory` 替身：記錄每次呼叫的 keyword 參數並回傳同一個 FakeSerial。"""

    def __init__(self, fake: FakeSerial) -> None:
        self.fake = fake
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeSerial:
        self.calls.append(kwargs)
        return self.fake


@pytest.fixture
def fake_serial_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """注入假 `serial` 模組，使 adapter 可取得 SerialTimeoutException 等例外類別（不依賴真 pyserial）。"""
    mod = types.ModuleType("serial")

    class SerialException(OSError):
        pass

    class SerialTimeoutException(SerialException):
        pass

    mod.SerialException = SerialException
    mod.SerialTimeoutException = SerialTimeoutException
    monkeypatch.setitem(sys.modules, "serial", mod)
    return mod


def _com_pair() -> tuple[str, str]:
    """解析 SW_TEST_COM_PAIR=COMa:COMb；COMa 為 pySerial 測試端，COMb 為 endpoint。"""
    raw = os.environ["SW_TEST_COM_PAIR"]
    a, b = raw.split(":", 1)
    return a.strip(), b.strip()


def _read_serial_n(ser: Any, n: int, timeout_s: float) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    while len(buf) < n and time.monotonic() < deadline:
        buf.extend(ser.read(n - len(buf)))
    return bytes(buf)


# ---------------------------------------------------------------- 假 serial 測試（Linux／hosted CI 皆跑）


def test_open_uses_exact_params(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    factory = RecordingFactory(fake)
    ep = WinSerialEndpoint("COMb", serial_factory=factory)
    ep.open()
    try:
        assert len(factory.calls) == 1
        kw = factory.calls[0]
        assert kw["port"] == "COMb"
        assert kw["baudrate"] == 115200
        assert kw["bytesize"] == 8
        assert kw["parity"] == "N"
        assert kw["stopbits"] == 1
        assert isinstance(kw["timeout"], (int, float))
    finally:
        ep.close()


def test_write_some_returns_accepted(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    fake.write_return = 3  # OS 只接受 3 bytes
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        assert ep.write_some(b"abcde", 1.0) == 3
    finally:
        ep.close()


@pytest.mark.parametrize("accepted", [-1, 6])
def test_write_some_rejects_impossible_accepted_lengths(fake_serial_module, accepted: int):
    from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    fake.write_return = accepted
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with pytest.raises(EndpointWriteUncertain) as exc_info:
            ep.write_some(b"abcde", 0.1)
        assert exc_info.value.requested_len == 5
    finally:
        ep.close()


def test_io_does_not_reconfigure_unchanged_timeouts(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        ep.read(8, 0.01)
        first_read_set_count = fake.timeout_assignments
        ep.read(8, 0.01)
        assert fake.timeout_assignments == first_read_set_count

        ep.write_some(b"x", 0.01)
        first_write_set_count = fake.write_timeout_assignments
        ep.write_some(b"x", 0.01)
        assert fake.write_timeout_assignments == first_write_set_count

        ep.write_some(b"x", 0)
        assert fake.write_timeout == pytest.approx(0.001)
    finally:
        ep.close()


def test_write_timeout_is_uncertain(fake_serial_module):
    from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    fake.write_exc = fake_serial_module.SerialTimeoutException("write timeout")
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with pytest.raises(EndpointWriteUncertain) as exc_info:
            ep.write_some(b"abcde", 1.0)
        assert exc_info.value.requested_len == 5
    finally:
        ep.close()


def test_capabilities_no_transport_disconnect(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(FakeSerial()))
    ep.open()
    try:
        assert ep.capabilities() == {
            "transport_disconnect": False,
            "by_id_link": False,
            "peer_state": False,
        }
        assert ep.peer_open() is None
    finally:
        ep.close()


def test_disconnect_refused_without_peer_close(fake_serial_module):
    from uart_stdio_bridge.endpoints.base import EndpointError
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint("COMb", allow_peer_close=False, serial_factory=RecordingFactory(FakeSerial()))
    ep.open()
    try:
        with pytest.raises(EndpointError) as exc_info:
            ep.disconnect(remove_link=False)
        assert "HARNESS_CAPABILITY_UNAVAILABLE" in str(exc_info.value)
    finally:
        ep.close()


def test_peer_close_labelled_not_transport_disconnect(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    ep = WinSerialEndpoint("COMb", allow_peer_close=True, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        ep.disconnect(remove_link=False)
        assert fake.closed  # allow_peer_close=True 時關閉 COM-B
        assert ep.describe()["last_fault"] == "peer_close"
    finally:
        ep.close()


def test_reconnect_requires_peer_close_permission_for_open_handle(fake_serial_module):
    from uart_stdio_bridge.endpoints.base import EndpointError
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with pytest.raises(EndpointError) as exc_info:
            ep.reconnect()
        assert "HARNESS_CAPABILITY_UNAVAILABLE" in str(exc_info.value)
        assert not fake.closed
        assert ep.generation == 1
    finally:
        ep.close()


def test_disconnected_endpoint_waits_then_reconnects(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    serials: list[FakeSerial] = []

    def factory(**kwargs: Any) -> FakeSerial:
        fake = FakeSerial(**kwargs)
        serials.append(fake)
        return fake

    ep = WinSerialEndpoint("COMb", allow_peer_close=True, serial_factory=factory)
    ep.open()
    try:
        ep.disconnect(remove_link=False)

        start = time.monotonic()
        assert ep.read(16, 0.05) == b""
        assert time.monotonic() - start >= 0.05

        start = time.monotonic()
        assert ep.write_some(b"x", 0.05) == 0
        assert time.monotonic() - start >= 0.05

        assert ep.reconnect() == 2
        assert len(serials) == 2
        assert not serials[-1].closed
        assert ep.write_some(b"new", 0.1) == 3
    finally:
        ep.close()


def test_io_and_reconnect_are_serialized(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    read_started = threading.Event()
    read_release = threading.Event()
    failures: list[BaseException] = []

    class BlockingSerial(FakeSerial):
        def read(self, size: int = 1) -> bytes:
            read_started.set()
            if not read_release.wait(2):
                raise TimeoutError("test read was not released")
            if self.closed:
                raise OSError("serial object closed during read")
            return b""

    serials: list[BlockingSerial] = []

    def factory(**kwargs: Any) -> BlockingSerial:
        fake = BlockingSerial(**kwargs)
        serials.append(fake)
        return fake

    ep = WinSerialEndpoint("COMb", allow_peer_close=True, serial_factory=factory)
    ep.open()

    def read_one() -> None:
        try:
            ep.read(1, 0.01)
        except BaseException as exc:
            failures.append(exc)

    def reconnect() -> None:
        try:
            ep.reconnect()
        except BaseException as exc:
            failures.append(exc)

    reader = threading.Thread(target=read_one)
    reconnector = threading.Thread(target=reconnect)
    reader.start()
    second_handle_open = False
    try:
        assert read_started.wait(1)
        reconnector.start()
        time.sleep(0.02)
        assert reconnector.is_alive()
    finally:
        read_release.set()
        reader.join(2)
        if reconnector.ident is not None:
            reconnector.join(2)
        second_handle_open = len(serials) > 1 and not serials[1].closed
        ep.close()

    assert not reader.is_alive()
    assert not reconnector.is_alive()
    assert failures == []
    assert ep.generation == 2
    assert serials[0].closed
    assert second_handle_open


def test_open_endpoint_dispatches_and_opens_win_serial(fake_serial_module):
    from uart_stdio_bridge.endpoints import open_endpoint

    fake = FakeSerial()
    factory = RecordingFactory(fake)
    fake_serial_module.Serial = factory
    ep = open_endpoint(
        {
            "kind": "win-serial",
            "com": "COMb",
            "baud": 57600,
            "transport_mode": "virtual_com0com",
            "allow_peer_close": True,
        }
    )
    try:
        assert ep.generation == 1
        assert ep.com == "COMb"
        assert ep.baud == 57600
        assert ep.allow_peer_close is True
        assert factory.calls[0]["port"] == "COMb"
        assert factory.calls[0]["baudrate"] == 57600
    finally:
        ep.close()

    for invalid in (None, {}, {"kind": "win-serial"}, {"kind": "unknown"}):
        with pytest.raises(ValueError):
            open_endpoint(invalid)


def test_link_ops_unavailable(fake_serial_module):
    from uart_stdio_bridge.endpoints.base import EndpointError
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(FakeSerial()))
    ep.open()
    try:
        with pytest.raises(EndpointError) as exc_info:
            ep.link_remove()
        assert "HARNESS_CAPABILITY_UNAVAILABLE" in str(exc_info.value)
        with pytest.raises(EndpointError) as exc_info:
            ep.link_add()
        assert "HARNESS_CAPABILITY_UNAVAILABLE" in str(exc_info.value)
    finally:
        ep.close()


def test_transport_mode_validated(fake_serial_module):
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    with pytest.raises(ValueError):
        WinSerialEndpoint("COMb", transport_mode="virtual_pty", serial_factory=RecordingFactory(FakeSerial()))


# ---------------------------------------------------------------- 真實 COM pair 測試（擁有者 host，OD-15）


@pytest.mark.windows_com
def test_cap_windows_com__roundtrip_full_byte_range():
    import serial

    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    com_a, com_b = _com_pair()
    peer = serial.Serial(port=com_a, baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.05)
    ep = WinSerialEndpoint(com_b, baud=115200, transport_mode="virtual_com0com")
    ep.open()
    try:
        data = bytes(range(256)) * 16

        # peer → endpoint：pySerial 寫入（背景執行緒），endpoint 讀取累積
        writer = threading.Thread(target=peer.write, args=(data,), daemon=True)
        writer.start()
        got = bytearray()
        deadline = time.monotonic() + 15.0
        while len(got) < len(data) and time.monotonic() < deadline:
            got.extend(ep.read(4096, 0.1))
        writer.join(5)
        assert bytes(got) == data

        # endpoint → peer：endpoint 寫入，pySerial 讀取（背景執行緒）
        result: dict[str, bytes] = {}
        reader = threading.Thread(
            target=lambda: result.update(data=_read_serial_n(peer, len(data), 15.0)), daemon=True
        )
        reader.start()
        sent = 0
        deadline = time.monotonic() + 15.0
        while sent < len(data) and time.monotonic() < deadline:
            sent += ep.write_some(data[sent:], 1.0)
        reader.join(20)
        assert result.get("data") == data
    finally:
        ep.close()
        peer.close()


@pytest.mark.windows_com
def test_cap_windows_com__exclusive_second_open_fails():
    import serial

    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    _com_a, com_b = _com_pair()
    ep = WinSerialEndpoint(com_b, baud=115200, transport_mode="virtual_com0com")
    ep.open()
    try:
        # Windows 通訊裝置獨占：endpoint 已開啟 COMb 時，第二次開啟必定失敗
        with pytest.raises(serial.SerialException):
            serial.Serial(port=com_b, baudrate=115200, timeout=0.05)
    finally:
        ep.close()
