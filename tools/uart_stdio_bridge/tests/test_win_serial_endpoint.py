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

    dsr = False  # pySerial dsr 預設低電位；子類可覆寫為 property 以模擬 peer 開啟

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

    @property
    def in_waiting(self) -> int:
        return len(self.read_data)

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


class _ControlledClock:
    """可明確推進的單調時鐘，供期限邊界測試避免毫秒級排程抖動。"""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, duration_s: float) -> None:
        with self._lock:
            self._now += max(0.0, duration_s)

    def sleep(self, duration_s: float) -> None:
        self.advance(duration_s)


def _patch_endpoint_clock(patch: pytest.MonkeyPatch, clock: _ControlledClock) -> None:
    """同時支援切換前後的 endpoint clock，讓期限案例固定在可重現時間線。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module

    patch.setattr(endpoint_module.time, "monotonic", clock)
    patch.setattr(endpoint_module, "_clock", clock, raising=False)


class _AdvancingLock:
    """持鎖前推進指定時間，確定性地模擬串行鎖等待。"""

    def __init__(self, inner: Any, clock: _ControlledClock, delays_s: list[float]) -> None:
        self._inner = inner
        self._clock = clock
        self._delays_s = list(delays_s)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._delays_s:
            self._clock.advance(self._delays_s.pop(0))
        if not blocking:
            return self._inner.acquire(False)
        if timeout == -1:
            return self._inner.acquire(True)
        return self._inner.acquire(True, timeout)

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    def __enter__(self) -> _AdvancingLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.release()
        return False


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


def test_io_does_not_reconfigure_unchanged_timeouts(fake_serial_module, monkeypatch):
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = FakeSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with pytest.MonkeyPatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            ep.read(8, 0.01)
            first_read_set_count = fake.timeout_assignments
            ep.read(8, 0.01)
            assert fake.timeout_assignments == first_read_set_count

            ep.write_some(b"x", 0.01)
            first_write_set_count = fake.write_timeout_assignments
            ep.write_some(b"x", 0.01)
            assert fake.write_timeout_assignments == first_write_set_count

            # 1 ms 是 Windows pySerial 可回報寫入長度的最短期限；受控時鐘避免系統計時粒度使期限先行到期。
            assert ep.write_some(b"x", 0) == 1
        assert fake.write_timeout == pytest.approx(0.001)
        assert fake.writes[-1] == b"x"
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


def test_disconnected_endpoint_waits_then_reconnects(fake_serial_module, monkeypatch):
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    serials: list[FakeSerial] = []

    def factory(**kwargs: Any) -> FakeSerial:
        fake = FakeSerial(**kwargs)
        serials.append(fake)
        return fake

    ep = WinSerialEndpoint("COMb", allow_peer_close=True, serial_factory=factory)
    ep.open()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(endpoint_module, "_clock", clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            ep.disconnect(remove_link=False)

            start = clock()
            assert ep.read(16, 0.05) == b""
            assert clock() - start == pytest.approx(0.05)

            start = clock()
            assert ep.write_some(b"x", 0.05) == 0
            assert clock() - start == pytest.approx(0.05)

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
            # 預留至少一個完整 pySerial 50 ms 量子，才能確定進入阻塞讀取。
            ep.read(1, 0.1)
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


class _ClosedAwareSerial(FakeSerial):
    """關閉後讀寫即拋例外的替身（近似 pySerial 對已關閉埠的行為）。"""

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("write on closed handle")
        return super().write(data)

    def read(self, size: int = 1) -> bytes:
        if self.closed:
            raise OSError("read on closed handle")
        return b""


class _HookedIoLock:
    """包裝串行鎖：首次取得前先執行 hook（於同一執行緒斷線），用以確定性地重現「快照後、取鎖前」的交錯。"""

    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook
        self._fired = False

    def _run_hook_once(self) -> None:
        if not self._fired:
            self._fired = True
            self._hook()

    def __enter__(self) -> _HookedIoLock:
        self._run_hook_once()
        self._inner.acquire()
        return self

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self._run_hook_once()
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()

    def __exit__(self, *_exc: object) -> bool:
        self._inner.release()
        return False


def test_read_does_not_touch_handle_closed_before_io_lock(fake_serial_module):
    """取得 handle 快照後、取得讀取串行鎖前若已斷線：不得對已關閉的 handle 讀取（應如無 handle 般等待至期限回空）。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    serials: list[_ClosedAwareSerial] = []

    def factory(**kwargs: Any) -> _ClosedAwareSerial:
        fake = _ClosedAwareSerial(**kwargs)
        serials.append(fake)
        return fake

    ep = WinSerialEndpoint("COMb", allow_peer_close=True, serial_factory=factory)
    ep.open()
    try:
        ep._read_io_lock = _HookedIoLock(ep._read_io_lock, lambda: ep.disconnect(remove_link=False))
        assert ep.read(16, 0.05) == b""
        assert serials[0].closed
    finally:
        ep.close()


def test_write_does_not_touch_handle_closed_before_io_lock(fake_serial_module):
    """取得 handle 快照後、取得寫入串行鎖前若已斷線：不得對已關閉的 handle 寫入（應回 0，不得拋例外）。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    serials: list[_ClosedAwareSerial] = []

    def factory(**kwargs: Any) -> _ClosedAwareSerial:
        fake = _ClosedAwareSerial(**kwargs)
        serials.append(fake)
        return fake

    ep = WinSerialEndpoint("COMb", allow_peer_close=True, serial_factory=factory)
    ep.open()
    try:
        ep._write_io_lock = _HookedIoLock(ep._write_io_lock, lambda: ep.disconnect(remove_link=False))
        assert ep.write_some(b"x", 0.05) == 0
        assert serials[0].closed
        assert serials[0].writes == []
    finally:
        ep.close()


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


# ---------------------------------------------------------------- peer gate（假 serial 純邏輯，Linux／hosted CI 皆跑）


class PeerFakeSerial(FakeSerial):
    """具 DSR 的替身：`dsr_on` 模擬對端 DTR 經 com0com 映射到本端 DSR（pySerial `dsr` 的 GetCommModemStatus 取值）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dsr_on = False

    @property
    def dsr(self) -> bool:
        return self.dsr_on

    def read(self, size: int = 1) -> bytes:
        if not self.read_data:
            # 對齊 pySerial 逾時等待語意：無資料時讓出時間片，避免讀取迴圈忙轉。
            time.sleep(0.01)
        return super().read(size)


def _wait_until(predicate: Any, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_peer_gate_no_write_before_open(fake_serial_module):
    """對應 POSIX `test_peer_gate_no_write_before_open`：peer 未開啟時寫入串流長度為 0；開啟後才寫入。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = PeerFakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        assert ep.peer_open() is False
        assert ep.write_some(b"BANNER\r\n", 0.2) == 0
        assert fake.writes == []

        fake.dsr_on = True
        assert _wait_until(lambda: ep.peer_open() is True, 1.0)
        assert ep.write_some(b"BANNER\r\n", 1.0) == len(b"BANNER\r\n")
        assert fake.writes == [b"BANNER\r\n"]
    finally:
        ep.close()


@pytest.mark.parametrize("timeout_path", ["reconfigure", "already-configured"])
def test_peer_gate_rechecks_dsr_after_waiting_for_write_lock(fake_serial_module, monkeypatch, timeout_path):
    """等待寫入鎖期間 peer 關閉時，已通過前置 DSR 檢查的 bytes 仍不得送出。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = PeerFakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    fake.dsr_on = True
    ep._peer_on_since = clock() - 0.25
    assert ep.peer_open() is True
    if timeout_path == "already-configured":
        fake.write_timeout = 0.5
        monkeypatch.setattr(ep, "_effective_write_timeout", lambda _requested, _remaining: 0.5)

    waiting_for_write_lock = threading.Event()
    original_acquire_until = ep._acquire_until

    def observe_write_lock_wait(lock, deadline):
        if lock is ep._write_io_lock:
            waiting_for_write_lock.set()
        return original_acquire_until(lock, deadline)

    monkeypatch.setattr(ep, "_acquire_until", observe_write_lock_wait)
    ep._write_io_lock.acquire()
    result: list[int] = []
    writer = threading.Thread(target=lambda: result.append(ep.write_some(b"B", 0.5)), daemon=True)
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            # _peer_on_since 與期限時鐘同源，避免 Windows 兩種時鐘原點差異影響 settle 判斷。
            ep._peer_on_since = clock() - 0.25
            assert ep.peer_open() is True
            writer.start()
            assert waiting_for_write_lock.wait(1.0)
            fake.dsr_on = False
            assert ep.peer_open() is False
            ep._write_io_lock.release()
            writer.join(2.0)
        assert not writer.is_alive()
        assert result == [0]
        assert fake.writes == []
        if timeout_path == "reconfigure":
            assert fake.write_timeout_assignments == 0, "DSR 已低時不應再改寫會重設 COM handle 的 timeout"
    finally:
        if ep._write_io_lock.locked():
            ep._write_io_lock.release()
        writer.join(1.0)
        ep.close()


def test_peer_gate_rechecks_dsr_after_timeout_setter(fake_serial_module, monkeypatch):
    """期限 setter 期間 peer 關閉時，setter 後的寫入閘仍須阻止 payload 進入 COM。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()

    class PeerClosesDuringTimeoutSetter(PeerFakeSerial):
        @property
        def write_timeout(self) -> float | None:
            return self._write_timeout

        @write_timeout.setter
        def write_timeout(self, value: float | None) -> None:
            FakeSerial.write_timeout.fset(self, value)
            self.dsr_on = False

    fake = PeerClosesDuringTimeoutSetter()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        fake.dsr_on = True
        ep._peer_on_since = clock() - 0.25
        with monkeypatch.context() as patch:
            patch.setattr(endpoint_module, "_clock", clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            assert ep.write_some(b"late", 0.5) == 0
        assert fake.write_timeout_assignments == 1
        assert fake.writes == []
    finally:
        ep.close()


def test_peer_gate_settle_window_before_first_write(fake_serial_module):
    """DSR 剛轉高時（pySerial open 的 PurgeComm 可能尚未執行）不立即寫入，須過 settle 窗口。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = PeerFakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        fake.dsr_on = True
        assert ep.write_some(b"x", 0.01) == 0
        assert fake.writes == []
        assert ep.write_some(b"x", 1.0) == 1
        assert fake.writes == [b"x"]
    finally:
        ep.close()


def test_peer_gate_disabled_writes_without_peer(fake_serial_module):
    """peer_gate=False（selftest 既有規格）時不閘控寫入；peer_open 不取樣而回 None（與 HEAD 一致）。

    回 False 會使 pumps 把寫入逾時縮為 50 ms，對端關閉時 fail-stop（HARNESS_WRITE_UNCERTAIN）。
    """
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = PeerFakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=False, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        assert ep.peer_open() is None
        assert ep.write_some(b"x", 0.2) == 1
        assert fake.writes == [b"x"]
    finally:
        ep.close()


def test_peer_gate_rejected_for_physical_usb_uart(fake_serial_module):
    """DSR 映射僅於 com0com 實測；physical_usb_uart 啟用 peer gate 須拒絕，未啟用則 peer_open 為 None。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    with pytest.raises(ValueError):
        WinSerialEndpoint(
            "COMb",
            transport_mode="physical_usb_uart",
            peer_gate=True,
            serial_factory=RecordingFactory(PeerFakeSerial()),
        )
    ep = WinSerialEndpoint(
        "COMb",
        transport_mode="physical_usb_uart",
        peer_gate=False,
        serial_factory=RecordingFactory(PeerFakeSerial()),
    )
    ep.open()
    try:
        assert ep.peer_open() is None
    finally:
        ep.close()


def test_peer_gate_rechecks_after_peer_close(fake_serial_module):
    """peer 關閉（DSR 轉低）後寫入回 0 且不進入埠；再次開啟後恢復寫入。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = PeerFakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        fake.dsr_on = True
        assert _wait_until(lambda: ep.peer_open() is True, 1.0)
        assert ep.write_some(b"A", 1.0) == 1

        fake.dsr_on = False
        assert ep.write_some(b"B", 0.1) == 0
        assert ep.peer_open() is False
        assert fake.writes == [b"A"]

        fake.dsr_on = True
        assert ep.write_some(b"C", 1.0) == 1
        assert fake.writes == [b"A", b"C"]
    finally:
        ep.close()


def test_peer_open_none_without_handle(fake_serial_module):
    """無 handle（未開啟或已關閉）時 peer_open 回 None，不回 False 以免被誤判為對端已關閉。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(PeerFakeSerial()))
    assert ep.peer_open() is None
    ep.open()
    assert ep.peer_open() is False
    ep.close()
    assert ep.peer_open() is None


def test_peer_gate_declares_peer_state_capability(fake_serial_module):
    """S1X 修訂（PENDING-OD-S1X-WIN-BANNER）：com0com 且 peer_gate 啟用時宣告 peer_state（DSR 已於 COM20:COM21 原生實測，
    g3-peer-gate/261023-061416-2646662）；transport_disconnect 與 by_id_link 仍為 False。
    舊斷言「peer_gate 不得宣告 peer_state」已依實測結果修訂。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(PeerFakeSerial()))
    ep.open()
    try:
        assert ep.capabilities() == {
            "transport_disconnect": False,
            "by_id_link": False,
            "peer_state": True,
        }
        assert ep.describe()["peer_gate"] is True
    finally:
        ep.close()


def test_peer_state_undeclared_without_peer_gate(fake_serial_module):
    """未啟用 peer gate 時 DSR 不被取樣，故不得宣告 peer_state（對外 status.peer_open 維持 null）。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(PeerFakeSerial()))
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


def test_peer_state_undeclared_for_physical_usb_uart(fake_serial_module):
    """physical_usb_uart 無 DSR 映射實測，peer_gate 於建構時被拒絕，故 peer_state 不得宣告。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    ep = WinSerialEndpoint(
        "COMb",
        transport_mode="physical_usb_uart",
        serial_factory=RecordingFactory(PeerFakeSerial()),
    )
    ep.open()
    try:
        assert ep.capabilities()["peer_state"] is False
        assert ep.capabilities()["transport_disconnect"] is False
    finally:
        ep.close()


def test_peer_state_capability_matches_peer_open_sampling(fake_serial_module):
    """宣告 peer_state 時 peer_open 必為布林值（DSR 低為 False、高為 True），不得回 None。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = PeerFakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        assert ep.capabilities()["peer_state"] is True
        assert ep.peer_open() is False
        fake.dsr_on = True
        assert ep.peer_open() is True
    finally:
        ep.close()


def test_open_endpoint_forwards_peer_gate(fake_serial_module):
    from uart_stdio_bridge.endpoints import open_endpoint

    fake_serial_module.Serial = RecordingFactory(PeerFakeSerial())
    gated = open_endpoint({"kind": "win-serial", "com": "COMb", "peer_gate": True})
    ungated = open_endpoint({"kind": "win-serial", "com": "COMb"})
    try:
        assert gated.peer_gate is True
        assert ungated.peer_gate is False
    finally:
        gated.close()
        ungated.close()


# ---------------------------------------------------------------- 並行期限與雙向靜止（G9：sol ca0106f Medium，failing-first）


def test_endpoint_deadlines_use_high_resolution_counter(fake_serial_module):
    """Windows 的期限時鐘必須使用 QPC-backed perf_counter，避免 monotonic 的粗粒度跳變。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module

    assert endpoint_module._clock is time.perf_counter


class TimedSerial(FakeSerial):
    """記錄每次 I/O 實際使用期限的替身，並偵測「I/O 進行中」時呼叫期限 setter 的情形。

    pySerial 的期限 setter 會重設整個 COM handle，故只能在讀寫雙向皆靜止時呼叫；`violations` 記錄任何
    違反此條件的 setter 呼叫。`read_gate`／`write_gate` 可使 I/O 阻塞於 pySerial 呼叫中，以確定性地重現交錯。
    """

    def __init__(self, **kwargs: Any) -> None:
        # RecordingFactory 會回傳同一個替身而不套用 open() 傳入的參數，故預設值與 `_open_locked` 的初始值一致。
        kwargs.setdefault("timeout", 0.05)
        kwargs.setdefault("write_timeout", None)
        super().__init__(**kwargs)
        self._guard = threading.Lock()
        self._active: set[str] = set()
        self.read_timeouts_at_call: list[float | None] = []
        self.write_timeouts_at_call: list[float | None] = []
        self.violations: list[str] = []
        self.read_gate: threading.Event | None = None
        self.write_gate: threading.Event | None = None
        self.read_entered = threading.Event()
        self.write_entered = threading.Event()

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self._note_setter("timeout")
        self.timeout_assignments += 1
        self._timeout = value

    @property
    def write_timeout(self) -> float | None:
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value: float | None) -> None:
        self._note_setter("write_timeout")
        self.write_timeout_assignments += 1
        self._write_timeout = value

    def _note_setter(self, name: str) -> None:
        with self._guard:
            if self._active:
                self.violations.append(f"{name} during {sorted(self._active)}")

    def read(self, size: int = 1) -> bytes:
        with self._guard:
            self._active.add("read")
            self.read_timeouts_at_call.append(self._timeout)
        try:
            self.read_entered.set()
            if self.read_gate is not None:
                self.read_gate.wait(5)
            return super().read(size)
        finally:
            with self._guard:
                self._active.discard("read")

    def write(self, data: bytes) -> int:
        with self._guard:
            self._active.add("write")
            self.write_timeouts_at_call.append(self._write_timeout)
        try:
            self.write_entered.set()
            if self.write_gate is not None:
                self.write_gate.wait(5)
            return super().write(data)
        finally:
            with self._guard:
                self._active.discard("write")


def test_read_recomputes_buffer_only_budget_after_read_lock_wait(fake_serial_module, monkeypatch):
    """70 ms 讀取鎖等待後只剩 30 ms 時，不得再進入固定 50 ms 阻塞讀取。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()

    class SlowEmptyReadSerial(FakeSerial):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.read_sizes: list[int] = []

        def read(self, size: int = 1) -> bytes:
            self.read_sizes.append(size)
            clock.advance(float(self.timeout or 0.05))
            return super().read(size)

    fake = SlowEmptyReadSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    inner = ep._read_io_lock
    ep._read_io_lock = _AdvancingLock(inner, clock, [0.07])
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            assert ep.read(16, 0.1) == b""
        assert fake.read_sizes == [], "鎖等待後剩餘期限小於 50 ms，必須只檢查 driver buffer"
    finally:
        ep._read_io_lock = inner
        ep.close()


def test_write_recomputes_fast_path_budget_after_write_lock_wait(fake_serial_module, monkeypatch):
    """200 ms 寫入等鎖 150 ms 後，pySerial write_timeout 只能使用剩下的 50 ms。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake._write_timeout = 0.2
    lock_waiting = threading.Event()
    original_acquire_until = ep._acquire_until

    def observe_wait(lock: Any, deadline: float) -> bool:
        if lock is ep._write_io_lock:
            lock_waiting.set()
        return original_acquire_until(lock, deadline)

    monkeypatch.setattr(ep, "_acquire_until", observe_wait)
    ep._write_io_lock.acquire()
    result: list[int] = []
    writer = threading.Thread(target=lambda: result.append(ep.write_some(b"B", 0.2)), daemon=True)
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            writer.start()
            assert lock_waiting.wait(1.0)
            clock.advance(0.15)
            ep._write_io_lock.release()
            writer.join(2.0)
        assert not writer.is_alive()
        assert result == [1]
        assert fake.write_timeouts_at_call == pytest.approx([0.05], abs=0.001)
        assert fake.writes == [b"B"]
    finally:
        if ep._write_io_lock.locked():
            ep._write_io_lock.release()
        writer.join(1.0)
        ep.close()


def test_write_reconfiguration_recomputes_budget_after_read_lock_wait(fake_serial_module, monkeypatch):
    """寫入期限設定等讀取鎖 50 ms 後，setter 必須採用縮短後的 150 ms。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    inner = ep._read_io_lock
    ep._read_io_lock = _AdvancingLock(inner, clock, [0.05])
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            assert ep.write_some(b"W", 0.2) == 1
        assert fake.write_timeouts_at_call == pytest.approx([0.15], abs=0.001)
        assert fake.writes == [b"W"]
    finally:
        ep._read_io_lock = inner
        ep.close()


def test_expired_deadline_after_write_timeout_setter_does_not_send(fake_serial_module, monkeypatch):
    """pySerial setter 推進時間跨過 deadline 後，不得送出任何 bytes。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()

    class AdvancingSetterSerial(TimedSerial):
        @property
        def write_timeout(self) -> float | None:
            return self._write_timeout

        @write_timeout.setter
        def write_timeout(self, value: float | None) -> None:
            TimedSerial.write_timeout.fset(self, value)
            clock.advance(0.101)

    fake = AdvancingSetterSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            assert ep.write_some(b"late", 0.1) == 0
        assert fake.writes == []
    finally:
        ep.close()


def test_partial_setter_deadline_consumption_does_not_send_with_stale_timeout(fake_serial_module, monkeypatch):
    """setter 消耗 150/200 ms 後，剩餘期限不足以沿用原 200 ms write_timeout。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()

    class PartialBudgetSetterSerial(TimedSerial):
        @property
        def write_timeout(self) -> float | None:
            return self._write_timeout

        @write_timeout.setter
        def write_timeout(self, value: float | None) -> None:
            TimedSerial.write_timeout.fset(self, value)
            clock.advance(0.15)

    fake = PartialBudgetSetterSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            assert ep.write_some(b"late", 0.2) == 0
        assert clock() == pytest.approx(0.15)
        assert fake.write_timeout == pytest.approx(0.2)
        assert fake.writes == [], "setter 後剩 50 ms，不可使用舊的 200 ms timeout 傳送 payload"
    finally:
        ep.close()


def test_write_timeout_quantization_prevents_setter_churn(fake_serial_module, monkeypatch):
    """期限剩餘量的亞毫秒抖動量化後不應每次寫入都重設 COM timeout。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake._write_timeout = 4.997  # 前一次合法設定；每次鎖等待後仍有 4.997 秒以上預算。
    setter_count = fake.write_timeout_assignments
    inner = ep._write_io_lock
    ep._write_io_lock = _AdvancingLock(inner, clock, [0.0021, 0.0024, 0.0022, 0.0023, 0.0021, 0.0024])
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            for _ in range(6):
                assert ep.write_some(b"x", 5.0) == 1
        assert fake.write_timeout_assignments == setter_count
        assert fake.write_timeouts_at_call == pytest.approx([4.997] * 6)
        assert fake.writes == [b"x"] * 6
    finally:
        ep._write_io_lock = inner
        ep.close()


def test_concurrent_writes_each_use_own_deadline(fake_serial_module, monkeypatch):
    """第一筆寫入（5 s）進行中，第二筆（0.2 s）並行呼叫：第二筆不得在第一筆 I/O 進行中改寫期限；
    兩筆實際送進 pySerial 的 write_timeout 各自為自己的期限。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    clock = _ControlledClock()
    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    results: dict[str, int] = {}
    second_waiting_for_write_lock = threading.Event()
    original_acquire_until = ep._acquire_until

    def observe_second_writer_wait(lock: Any, deadline: float) -> bool:
        if lock is ep._write_io_lock and threading.current_thread().name == "second-writer":
            second_waiting_for_write_lock.set()
        return original_acquire_until(lock, deadline)

    monkeypatch.setattr(ep, "_acquire_until", observe_second_writer_wait)
    fake.write_gate = threading.Event()
    first = threading.Thread(target=lambda: results.__setitem__("A", ep.write_some(b"AAAA", 5.0)), name="first-writer")
    second = threading.Thread(target=lambda: results.__setitem__("B", ep.write_some(b"B", 0.2)), name="second-writer")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(endpoint_module, "_clock", clock)
            first.start()
            assert fake.write_entered.wait(2)
            second.start()
            assert second_waiting_for_write_lock.wait(2)
            assert fake.violations == [], "第一筆寫入進行中不得改寫期限"
            fake.write_gate.set()
            first.join(5)
            second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert results == {"A": 4, "B": 1}
        assert fake.write_timeouts_at_call == pytest.approx([5.0, 0.2])
        assert fake.writes == [b"AAAA", b"B"]
        assert fake.violations == []
    finally:
        fake.write_gate.set()
        first.join(1)
        second.join(1)
        ep.close()


def test_write_timeout_is_reduced_by_time_spent_waiting_for_write_lock(fake_serial_module, monkeypatch):
    """200 ms 呼叫先等寫入鎖 150 ms，pySerial 只能再等剩餘期限，不可重新取得完整 200 ms。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()

    class BudgetSerial(TimedSerial):
        def write(self, data: bytes) -> int:
            if data == b"B":
                with self._guard:
                    self._active.add("write")
                    self.write_timeouts_at_call.append(self.write_timeout)
                self.write_entered.set()
                try:
                    clock.advance(float(self.write_timeout or 0))
                    raise fake_serial_module.SerialTimeoutException("write timeout")
                finally:
                    with self._guard:
                        self._active.discard("write")
            return super().write(data)

    fake = BudgetSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    inner = ep._write_io_lock
    ep._write_io_lock = _AdvancingLock(inner, clock, [0.15])
    try:
        with monkeypatch.context() as patch:
            patch.setattr(endpoint_module, "_clock", clock)
            with pytest.raises(EndpointWriteUncertain):
                ep.write_some(b"B", 0.2)
        assert clock() == pytest.approx(0.2)
        assert fake.write_timeouts_at_call == pytest.approx([0.05])
    finally:
        ep._write_io_lock = inner
        ep.close()


def test_peer_open_does_not_wait_for_close_blocked_by_write(fake_serial_module, monkeypatch):
    """DSR 取樣不得等待控制面 close 因在途 write 而持有狀態鎖。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    class PeerSerial(TimedSerial):
        dsr = True

    fake = PeerSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep.open()
    fake.write_gate = threading.Event()
    writer = threading.Thread(target=lambda: ep.write_some(b"A", 1.0))
    closer = threading.Thread(target=ep.close)
    peer_result: dict[str, bool | None] = {}
    peer_done = threading.Event()

    def sample_peer() -> None:
        peer_result["open"] = ep.peer_open()
        peer_done.set()

    peer = threading.Thread(target=sample_peer)
    try:
        with monkeypatch.context() as patch:
            # settle 起點與端點期限使用同一受控時鐘，避開 Windows monotonic/perf_counter 的不同原點。
            patch.setattr(endpoint_module, "_clock", clock)
            ep._peer_on_since = endpoint_module._clock() - 1.0
            writer.start()
            assert fake.write_entered.wait(2)
            closer.start()
            deadline = time.monotonic() + 2
            while not ep._closed and time.monotonic() < deadline:
                time.sleep(0.001)
            assert ep._closed, "close 未進入等待在途 I/O 的區段"
            peer.start()
            assert peer_done.wait(0.1), "peer_open 不得等待阻塞寫入完成"
            assert peer_result == {"open": True}
            assert not fake.write_gate.is_set()
            fake.write_gate.set()
            writer.join(2)
            closer.join(2)
            peer.join(2)
    finally:
        fake.write_gate.set()
        if writer.ident is not None:
            writer.join(2)
        if closer.ident is not None:
            closer.join(2)
        if peer.ident is not None:
            peer.join(2)
        ep.close()


def test_write_expired_while_waiting_for_lock_is_not_sent(fake_serial_module):
    """取得寫入鎖時期限已過，必須回 0 且不得送出任何 bytes。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    results: dict[str, int] = {}
    second_done = threading.Event()
    fake.write_gate = threading.Event()
    first = threading.Thread(target=lambda: results.__setitem__("A", ep.write_some(b"A", 1.0)))
    def _write_second() -> None:
        results["B"] = ep.write_some(b"B", 0.05)
        second_done.set()

    second = threading.Thread(target=_write_second)
    try:
        first.start()
        assert fake.write_entered.wait(2)
        second.start()
        assert second_done.wait(0.2), "期限到期後第二筆寫入必須在第一筆釋放鎖前返回"
        assert fake.write_gate.is_set() is False
        fake.write_gate.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert results == {"A": 1, "B": 0}
        assert fake.writes == [b"A"]
    finally:
        fake.write_gate.set()
        first.join(1)
        second.join(1)
        ep.close()


def test_write_deadline_survives_interleaved_call_before_io_lock(fake_serial_module, monkeypatch):
    """第一筆（5 s）已取得期限快照、尚未取得寫入串行鎖時，另一筆（1 ms）完整執行：
    第一筆仍須以自己的期限寫入，不得沿用被改寫的期限（sol ca0106f 之原始交錯）。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    interleaved: dict[str, int] = {}
    ep._write_io_lock = _HookedIoLock(ep._write_io_lock, lambda: interleaved.__setitem__("B", ep.write_some(b"B", 0.001)))
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            assert ep.write_some(b"AAAA", 5.0) == 4
        assert interleaved == {"B": 1}
        assert fake.write_timeouts_at_call == [0.001, 5.0]
        assert fake.writes == [b"B", b"AAAA"]
        assert fake.violations == []
    finally:
        ep.close()


def test_concurrent_reads_each_use_own_deadline(fake_serial_module):
    """短期限讀取不得等候較早的讀取鎖；讀取 timeout 固定 50 ms，由呼叫端累計期限。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    results: dict[str, bytes] = {}
    elapsed: dict[str, float] = {}
    fake.read_gate = threading.Event()
    second_done = threading.Event()

    def _read(label: str, timeout: float, done: threading.Event | None = None) -> None:
        started = time.monotonic()
        results[label] = ep.read(16, timeout)
        elapsed[label] = time.monotonic() - started
        if done is not None:
            done.set()

    first = threading.Thread(target=lambda: _read("A", 0.2))
    second = threading.Thread(target=lambda: _read("B", 0.01, second_done))
    try:
        first.start()
        assert fake.read_entered.wait(2)
        second.start()
        assert second_done.wait(0.1), "短期限讀取在等待讀取鎖時超過自己的期限"
        assert fake.violations == [], "第一筆讀取進行中不得改寫期限"
        fake.read_gate.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert results == {"A": b"", "B": b""}
        assert elapsed["B"] < 0.1 and 0.15 <= elapsed["A"] < 0.4
        assert fake.read_timeouts_at_call and all(value == 0.05 for value in fake.read_timeouts_at_call)
        assert fake.violations == []
    finally:
        fake.read_gate.set()
        first.join(1)
        second.join(1)
        ep.close()


def test_read_deadline_survives_interleaved_call_before_io_lock(fake_serial_module):
    """讀取端交錯版本：先執行的短呼叫不改寫固定 50 ms pySerial timeout，也不改變外層期限。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    interleaved: dict[str, bytes] = {}
    ep._read_io_lock = _HookedIoLock(ep._read_io_lock, lambda: interleaved.__setitem__("B", ep.read(16, 0.01)))
    try:
        started = time.monotonic()
        assert ep.read(16, 0.2) == b""
        elapsed = time.monotonic() - started
        assert interleaved == {"B": b""}
        assert 0.15 <= elapsed < 0.4
        assert fake.read_timeouts_at_call and all(value == 0.05 for value in fake.read_timeouts_at_call)
        assert fake.violations == []
    finally:
        ep.close()


def test_read_timeout_setter_waits_for_inflight_write(fake_serial_module):
    """寫入進行中（阻塞於 pySerial write）時，短讀取仍能排空 bytes，且不呼叫期限 setter。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake.write_gate = threading.Event()
    fake.read_data.extend(b"R")
    writer = threading.Thread(target=lambda: ep.write_some(b"W", 5.0))
    result: dict[str, bytes] = {}
    reader = threading.Thread(target=lambda: result.__setitem__("data", ep.read(16, 0.2)))
    try:
        writer.start()
        assert fake.write_entered.wait(2)
        reader.start()
        reader.join(0.5)
        assert not reader.is_alive(), "讀取不得等待被阻塞的寫入鎖"
        assert fake.violations == [], "寫入進行中不得改寫讀取期限"
        assert result == {"data": b"R"}
        assert fake.read_timeouts_at_call == [0.05]
        assert writer.is_alive(), "讀取結果不得以解除寫入阻塞為前提"
        fake.write_gate.set()
        writer.join(5)
        reader.join(5)
        assert not writer.is_alive() and not reader.is_alive()
        assert fake.violations == []
        assert fake.writes == [b"W"]
    finally:
        fake.write_gate.set()
        writer.join(1)
        reader.join(1)
        ep.close()


def test_write_timeout_setter_waits_for_inflight_read(fake_serial_module):
    """讀取進行中（阻塞於 pySerial read）時，寫入端需改期限者不得呼叫 setter（對稱版本）。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake.read_gate = threading.Event()
    # 預留至少一個完整 pySerial 50 ms 量子，才能確定 setter 與讀取 I/O 重疊。
    reader = threading.Thread(target=lambda: ep.read(16, 0.1))
    writer = threading.Thread(target=lambda: ep.write_some(b"W", 1.0))
    try:
        reader.start()
        assert fake.read_entered.wait(2)
        writer.start()
        time.sleep(0.05)
        assert fake.violations == [], "讀取進行中不得改寫寫入期限"
        fake.read_gate.set()
        reader.join(5)
        writer.join(5)
        assert not reader.is_alive() and not writer.is_alive()
        assert fake.violations == []
        assert fake.writes == [b"W"]
    finally:
        fake.read_gate.set()
        reader.join(1)
        writer.join(1)
        ep.close()


def test_blocked_write_does_not_stall_default_read(fake_serial_module):
    """寫入阻塞（對端不讀）時，長期限讀取仍須以固定短讀取期限排空資料（ST-03(a) 雙向獨立）。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake.write_gate = threading.Event()
    writer = threading.Thread(target=lambda: ep.write_some(b"W", 5.0))
    try:
        writer.start()
        assert fake.write_entered.wait(2)
        fake.read_data.extend(b"R")
        start = time.monotonic()
        assert ep.read(16, 0.5) == b"R"
        assert time.monotonic() - start < 0.25
        assert fake.read_timeouts_at_call == [0.05], "read 的呼叫期限不得重設 pySerial 的 COM 期限"
        assert fake.violations == []
    finally:
        fake.write_gate.set()
        writer.join(5)
        ep.close()


def test_long_read_deadline_uses_fixed_short_pyserial_timeout(fake_serial_module):
    """長讀取期限由外層輪詢累計；pySerial 使用固定 50 ms timeout，不等待寫入期限 setter。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake.write_gate = threading.Event()
    writer = threading.Thread(target=lambda: ep.write_some(b"W", 5.0))
    reader = threading.Thread(target=lambda: ep.read(16, 0.2))
    try:
        writer.start()
        assert fake.write_entered.wait(2)
        started = time.monotonic()
        reader.start()
        reader.join(0.5)
        elapsed = time.monotonic() - started
        assert not reader.is_alive() and 0.15 <= elapsed < 0.4
        assert fake.read_timeouts_at_call and all(value == 0.05 for value in fake.read_timeouts_at_call)
        assert fake.violations == []
        assert writer.is_alive(), "讀取期限不得等待寫入結束"
    finally:
        fake.write_gate.set()
        writer.join(5)
        reader.join(5)
        ep.close()


def test_first_write_reconfig_not_starved_by_busy_reader(fake_serial_module):
    """讀取端緊密迴圈時，首次寫入期限設定仍須成功送出並於合理時間內完成。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = TimedSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    stop = threading.Event()

    def spin_read() -> None:
        while not stop.is_set():
            ep.read(16, 0.1)

    reader = threading.Thread(target=spin_read, daemon=True)
    reader.start()
    try:
        started = time.monotonic()
        assert ep.write_some(b"W", 1.0) == 1
        assert time.monotonic() - started < 2.0
        assert fake.writes == [b"W"]
        assert fake.violations == []
    finally:
        stop.set()
        reader.join(5)
        ep.close()


def test_first_write_reconfig_preempts_second_reader(fake_serial_module, monkeypatch):
    """期限設定已等待讀取鎖時，第二個 reader 必須讓路到 setter 完成。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    class BlockingSetterSerial(TimedSerial):
        def __init__(self):
            super().__init__()
            self.setter_entered = threading.Event()
            self.setter_gate = threading.Event()

        @property
        def write_timeout(self):
            return self._write_timeout

        @write_timeout.setter
        def write_timeout(self, value):
            if value != self._write_timeout:
                self.setter_entered.set()
                self.setter_gate.wait(5)
            TimedSerial.write_timeout.fset(self, value)

    fake = BlockingSetterSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    fake.read_gate = threading.Event()
    first_reader = threading.Thread(target=lambda: ep.read(16, 1.0), name="first-reader")
    writer = threading.Thread(target=lambda: ep.write_some(b"W", 1.0), name="writer")
    second_read_once = threading.Event()
    original_read_once = ep._read_once

    def tracked_read_once(*args, **kwargs):
        if threading.current_thread().name == "second-reader":
            second_read_once.set()
        return original_read_once(*args, **kwargs)

    monkeypatch.setattr(ep, "_read_once", tracked_read_once)
    second_reader = threading.Thread(target=lambda: ep.read(16, 0.5), name="second-reader")
    try:
        first_reader.start()
        assert fake.read_entered.wait(2)
        writer.start()
        deadline = time.monotonic() + 2
        while ep._reconfig_waiters != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert ep._reconfig_waiters == 1, "writer 未進入雙向靜止期限設定等待"
        fake.read_gate.set()
        assert fake.setter_entered.wait(2), "writer 未取得讀取鎖以設定 write_timeout"
        second_reader.start()
        time.sleep(0.05)
        assert second_read_once.is_set() is False, "期限設定期間第二個 reader 不得進入 pySerial read"
        fake.setter_gate.set()
        first_reader.join(2)
        second_reader.join(2)
        writer.join(2)
        assert not first_reader.is_alive() and not second_reader.is_alive() and not writer.is_alive()
        assert second_read_once.is_set()
        assert fake.violations == []
    finally:
        fake.read_gate.set()
        fake.setter_gate.set()
        first_reader.join(1)
        if second_reader.ident is not None:
            second_reader.join(1)
        writer.join(1)
        ep.close()


def test_short_read_timeout_does_not_block_for_fixed_pyserial_quantum(fake_serial_module, monkeypatch):
    """10 ms read timeout 不得因固定 50 ms pySerial timeout 而多阻塞 40 ms。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()

    class SlowEmptyReadSerial(TimedSerial):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.read_sizes: list[int] = []

        def read(self, size: int = 1) -> bytes:
            self.read_sizes.append(size)
            time.sleep(float(self.timeout or 0))
            return super().read(size)

    fake = SlowEmptyReadSerial()
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(endpoint_module, "_clock", clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            assert ep.read(8, 0.01) == b""
        assert clock() == pytest.approx(0.01)
        assert fake.read_sizes == [], "10 ms read 不得呼叫會等待固定 50 ms 的 pySerial read"
    finally:
        ep.close()


class PySerialReadSemanticsSerial(FakeSerial):
    """模擬 pySerial：read(size) 不完整時等待至設定期限，並記錄實際要求的 size。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.read_sizes: list[int] = []

    def read(self, size: int = 1) -> bytes:
        self.read_sizes.append(size)
        if len(self.read_data) < size:
            time.sleep(float(self.timeout or 0))
        return super().read(size)


def test_short_read_with_partial_buffer_requests_only_available_bytes(fake_serial_module):
    """短期限且 buffer 有部分資料時，read 不等待 pySerial 補滿呼叫者要求的 bytes。"""
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = PySerialReadSemanticsSerial()
    fake._timeout = 0.05
    fake.read_data.extend(b"abc")
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        assert ep.read(8, 0.01) == b"abc"
        assert fake.read_sizes == [3]
    finally:
        ep.close()


def test_zero_timeout_read_returns_buffered_bytes(fake_serial_module, monkeypatch):
    """零期限可讀取已緩衝資料；空 buffer 也由受控時鐘保證在 1 ms 內回傳。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock = _ControlledClock()
    fake = PySerialReadSemanticsSerial()
    fake._timeout = 0.05
    fake.read_data.extend(b"abc")
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, clock)
            patch.setattr(endpoint_module.time, "sleep", clock.sleep)
            assert ep.read(8, 0) == b"abc"
        assert fake.read_sizes == [3]
    finally:
        ep.close()

    empty_clock = _ControlledClock()
    empty_fake = PySerialReadSemanticsSerial()
    empty_fake._timeout = 0.05
    empty_ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(empty_fake))
    empty_ep.open()
    try:
        with monkeypatch.context() as patch:
            _patch_endpoint_clock(patch, empty_clock)
            patch.setattr(endpoint_module.time, "sleep", empty_clock.sleep)
            assert empty_ep.read(8, 0) == b""
        assert empty_clock() == pytest.approx(0.001)
        assert empty_fake.read_sizes == []
    finally:
        empty_ep.close()


def test_read_uses_available_only_after_remaining_deadline_falls_below_quantum(fake_serial_module):
    """70 ms 呼叫只得進行一次固定 50 ms 阻塞讀取，尾端不得再進一個 50 ms 量子。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    clock_now = [0.0]

    class OneQuantumSerial(FakeSerial):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.read_sizes: list[int] = []

        def read(self, size: int = 1) -> bytes:
            self.read_sizes.append(size)
            if len(self.read_data) < size:
                clock_now[0] += float(self.timeout or 0)
            return super().read(size)

    fake = OneQuantumSerial()
    fake._timeout = 0.05
    ep = WinSerialEndpoint("COMb", serial_factory=RecordingFactory(fake))
    ep.open()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(endpoint_module.time, "monotonic", lambda: clock_now[0])
            patch.setattr(endpoint_module, "_clock", lambda: clock_now[0], raising=False)
            patch.setattr(endpoint_module.time, "sleep", lambda duration: clock_now.__setitem__(0, clock_now[0] + duration))
            assert ep.read(8, 0.07) == b""
        assert fake.read_sizes == [8]
    finally:
        ep.close()


def test_acquire_until_rejects_lock_granted_at_deadline(fake_serial_module, monkeypatch):
    """鎖成功但取得時呼叫期限已到，必須放回鎖並回報失敗。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    class GrantedLock:
        released = False

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            assert timeout == 10.0
            return True

        def release(self) -> None:
            self.released = True

    clock_values = iter((0.0, 10.0))
    lock = GrantedLock()
    with monkeypatch.context() as patch:
        patch.setattr(endpoint_module.time, "monotonic", lambda: next(clock_values))
        patch.setattr(endpoint_module, "_clock", lambda: next(clock_values), raising=False)
        assert not WinSerialEndpoint._acquire_until(lock, 10.0)
    assert lock.released


def test_peer_gate_uses_captured_settle_time_if_dsr_clears_state(fake_serial_module, monkeypatch):
    """取樣後 DSR 狀態可被另一執行緒清除；本次判定只使用鎖內複製的時間值。"""
    import uart_stdio_bridge.endpoints.win_serial as endpoint_module
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    fake = FakeSerial()
    ep = WinSerialEndpoint("COMb", peer_gate=True, serial_factory=RecordingFactory(fake))
    ep._peer_on_since = 0.0

    def clear_after_capture() -> float:
        ep._peer_on_since = None
        return 1000.0

    with monkeypatch.context() as patch:
        patch.setattr(endpoint_module.time, "monotonic", clear_after_capture)
        patch.setattr(endpoint_module, "_clock", clear_after_capture, raising=False)
        assert ep._peer_writable_locked(type("Peer", (), {"dsr": True})())
    assert ep._peer_on_since is None


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


@pytest.mark.windows_native
@pytest.mark.windows_com
def test_cap_windows_com__peer_gate_no_write_before_peer_open():
    """原生 pySerial：COM-A（serialwrap 端）未開啟時 peer_open 為 False 且寫入回 0；開啟後才寫入，且開啟前的 bytes 不出現。"""
    import serial

    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    com_a, com_b = _com_pair()
    ep = WinSerialEndpoint(com_b, baud=115200, transport_mode="virtual_com0com", peer_gate=True)
    ep.open()
    peer = None
    try:
        assert ep.peer_open() is False
        assert ep.write_some(b"BEFORE-PEER-OPEN\r\n", 0.3) == 0

        peer = serial.Serial(port=com_a, baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.05)
        assert _wait_until(lambda: ep.peer_open() is True, 5.0)

        payload = b"AFTER-PEER-OPEN\r\n"
        sent = 0
        deadline = time.monotonic() + 5.0
        while sent < len(payload) and time.monotonic() < deadline:
            sent += ep.write_some(payload[sent:], 0.5)
        assert sent == len(payload)
        assert _read_serial_n(peer, len(payload), 5.0) == payload
    finally:
        ep.close()
        if peer is not None:
            peer.close()


@pytest.mark.windows_native
@pytest.mark.windows_com
def test_cap_windows_com__peer_gate_rechecks_after_peer_close():
    """原生 pySerial（G3 close 邊緣）：peer 關閉後 DSR 須於 1 s 內拉低（peer_open 為 False），寫入回 0 且不拋例外；
    重新開啟後恢復寫入，且關閉期間寫入的 bytes 不得到達 peer。"""
    import serial

    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint

    com_a, com_b = _com_pair()
    ep = WinSerialEndpoint(com_b, baud=115200, transport_mode="virtual_com0com", peer_gate=True)
    ep.open()
    peer = None
    try:
        peer = serial.Serial(port=com_a, baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.05)
        assert _wait_until(lambda: ep.peer_open() is True, 5.0)
        first = b"OPEN-1\r\n"
        assert ep.write_some(first, 1.0) == len(first)
        assert _read_serial_n(peer, len(first), 5.0) == first

        peer.close()
        peer = None
        assert _wait_until(lambda: ep.peer_open() is False, 1.0), "peer 關閉後 DSR 未於 1 s 內拉低"
        assert ep.write_some(b"CLOSED-GAP\r\n", 0.3) == 0

        peer = serial.Serial(port=com_a, baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.05)
        assert _wait_until(lambda: ep.peer_open() is True, 5.0)
        second = b"OPEN-2\r\n"
        sent = 0
        deadline = time.monotonic() + 5.0
        while sent < len(second) and time.monotonic() < deadline:
            sent += ep.write_some(second[sent:], 0.5)
        assert sent == len(second)
        # 關閉期間寫入的 CLOSED-GAP 不得出現在重開後的讀取中
        assert _read_serial_n(peer, len(second), 5.0) == second
    finally:
        ep.close()
        if peer is not None:
            peer.close()


@pytest.mark.windows_native
@pytest.mark.windows_com
def test_cap_windows_com__bridge_banner_held_until_peer_open(tmp_path):
    """重現 G3 的 PENDING-OD-S1X-WIN-BANNER：DUT 開機 banner 於 peer 開啟前產生，bridge 須握住直到 peer 開啟後才寫入。"""
    import serial

    from helpers import StubDutProcess

    from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
    from uart_stdio_bridge.endpoints.win_serial import WinSerialEndpoint
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    com_a, com_b = _com_pair()
    banner = b"=== BOOT BANNER ===\r\n"
    ep = WinSerialEndpoint(com_b, baud=115200, transport_mode="virtual_com0com", peer_gate=True)
    dut = StubDutProcess()
    dut.stdout_buf.extend(banner)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    writer = EvidenceWriter(
        evidence_dir / "wire.ndjson", run_id="native-banner", case_id="banner", clock_id="native"
    )
    cfg = BridgeConfig(run_id="native-banner", case_id="banner", port_id="COM0", evidence_dir=evidence_dir)
    bridge = Bridge(cfg, ep, dut, writer, dut_control=None)
    bridge.start()
    peer = None
    try:
        time.sleep(0.5)
        peer = serial.Serial(port=com_a, baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=0.05)
        assert _read_serial_n(peer, len(banner), 10.0) == banner
    finally:
        bridge.handle_action("shutdown", {}, "act_shutdown")
        bridge.wait()
        if peer is not None:
            peer.close()
