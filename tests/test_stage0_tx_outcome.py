# tests/test_stage0_tx_outcome.py
"""Task P1-T6 紅燈測試：BC-03／SC-07 TX 三態、TX WAL 只記確認 bytes、可取消寫入、DF-15 精確 wal_seq。

涵蓋：
- BC-03 TX 三態（not_submitted、completed、unknown）與結果判定
- SC-07 TX WAL 只記 OS 已確認寫出的 bytes，順序等於實際寫入順序，在 _write_lock 內 append
- SC-04 前置：EAGAIN 期間可取消寫入
- DF-15：RX observer、background capture 改用該 chunk 的精確 WAL seq
- 凍結相容規則（legacy _write_all None 回傳、port.write 無 cancel 關鍵字、stub WAL None 回傳、舊 on_rx_data 簽章等）
- 分段傳輸與登入 FSM 在部分寫入後不自動重送

依 02-stage0-python-bc.md Global Constraints：
本模組不在頂層 import 本 Phase 才新增的模組或名稱（`TxResult`、`TxIncompleteError`、`stage0_fakes` 等），
一律於測試函式內以 `_load()` 延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。

marker 規則（P0 T14）：
資源 marker `memory_only` 放模組層；平台 marker 放函式層。
"""
from __future__ import annotations

import base64
import errno
import importlib
import threading
import time
from typing import Any

import pytest
import sw_core.uart_io as uio

pytestmark = [pytest.mark.memory_only]


def _load(module_name: str, *names: str) -> list[Any]:
    """延遲 import 模組並取出名稱；相依模組或名稱尚未實作時於測試函式內拋出 ImportError／ModuleNotFoundError。"""
    mod = importlib.import_module(module_name)
    out: list[Any] = []
    for name in names:
        if not hasattr(mod, name):
            raise ImportError(f"cannot import name {name!r} from {module_name!r}")
        out.append(getattr(mod, name))
    return out


def _posix_bridge(tmp_path, monkeypatch, actions):
    """建立帶有 ScriptedWrite 模擬 os.write 的 POSIX bridge 與 WAL。"""
    make_bridge, ScriptedWrite = _load("stage0_fakes", "make_bridge", "ScriptedWrite")
    bridge, wal = make_bridge(tmp_path)
    bridge._serial = object()
    bridge._serial_fd = 99
    sw = ScriptedWrite(actions)
    monkeypatch.setattr(uio.os, "write", sw)
    return bridge, wal, sw


@pytest.mark.core_portable
def test_partial_then_eio_records_only_confirmed_bytes(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    (wal_records,) = _load("stage0_fakes", "wal_records")
    bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [3, OSError(errno.EIO, "I/O error")])
    with pytest.raises(TxIncompleteError) as ei:
        bridge.send_bytes(b"hello", source="agent:x", cmd_id="c1")
    r = ei.value.result
    assert (r.outcome, r.confirmed, r.requested) == ("completed", 3, 5)
    tx = [x for x in wal_records(wal) if x["dir"] == "TX"]
    assert len(tx) == 1 and tx[0]["len"] == 3 and tx[0]["cmd_id"] == "c1"
    assert base64.b64decode(tx[0]["payload_b64"]) == b"hel"


@pytest.mark.core_portable
def test_first_write_error_records_no_tx(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    (wal_records,) = _load("stage0_fakes", "wal_records")
    bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [OSError(errno.EIO, "I/O error")])
    with pytest.raises(TxIncompleteError):
        bridge.send_bytes(b"hello", source="agent:x")
    assert [x for x in wal_records(wal) if x["dir"] == "TX"] == []


@pytest.mark.core_portable
def test_eagain_wait_is_cancellable(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (make_bridge,) = _load("stage0_fakes", "make_bridge")
    bridge, wal = make_bridge(tmp_path)

    def always_eagain(fd, data):
        raise BlockingIOError(errno.EAGAIN, "again")

    monkeypatch.setattr(uio.os, "write", always_eagain)
    cancel = threading.Event()
    threading.Timer(0.05, cancel.set).start()
    t0 = time.monotonic()
    r = bridge._write_all(99, b"abc", cancel=cancel)
    assert time.monotonic() - t0 < 1.0
    assert r == TxResult("completed", 0, 3, "cancelled")


@pytest.mark.core_portable
def test_pyserial_timeout_is_unknown_and_writes_meta(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    FakePort, make_bridge, wal_records = _load("stage0_fakes", "FakePort", "make_bridge", "wal_records")
    port = FakePort(write_results=[TxResult("unknown", 0, 5, "SerialTimeoutException: Write timeout")])
    bridge, wal = make_bridge(tmp_path, port=port)
    with pytest.raises(TxIncompleteError) as ei:
        bridge.send_bytes(b"hello", source="agent:x", cmd_id="c9")
    assert ei.value.result.outcome == "unknown"
    recs = wal_records(wal)
    assert [x for x in recs if x["dir"] == "TX"] == []
    meta = [x for x in recs if x["dir"] == "META"]
    assert meta[0]["meta"] == {
        "event": "tx_outcome_unknown",
        "requested_bytes": 5,
        "confirmed_bytes": 0,
        "unknown_range": [0, 5],
        "error": "SerialTimeoutException: Write timeout",
    }
    assert meta[0]["cmd_id"] == "c9" and meta[0]["source"] == "agent:x"


@pytest.mark.core_portable
def test_pyserial_partial_then_timeout_keeps_confirmed_prefix(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxUnknownError,) = _load("sw_core.uart_io", "TxUnknownError")
    FakePort, make_bridge, wal_records = _load("stage0_fakes", "FakePort", "make_bridge", "wal_records")
    port = FakePort(
        write_results=[TxUnknownError(TxResult("unknown", 2, 5, "SerialTimeoutException: t"), errno.ETIMEDOUT)]
    )
    bridge, wal = make_bridge(tmp_path, port=port)
    with pytest.raises(TxUnknownError) as ei:
        bridge.send_bytes(b"hello", source="agent:x", cmd_id="c8")
    assert (ei.value.result.confirmed, ei.value.result.requested) == (2, 5)
    tx = [x for x in wal_records(wal) if x["dir"] == "TX"]
    assert len(tx) == 1 and base64.b64decode(tx[0]["payload_b64"]) == b"he"
    assert bridge.tx_account("c8") == (2, True)


@pytest.mark.core_portable
def test_partial_writes_from_two_sources_do_not_interleave(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    make_bridge, wal_records = _load("stage0_fakes", "make_bridge", "wal_records")
    bridge, wal = make_bridge(tmp_path)
    bridge._serial = object()
    bridge._serial_fd = 99
    seen: list[bytes] = []
    gate = threading.Event()

    def w(fd, data):
        seen.append(bytes(data))
        if bytes(data) == b"AAAAA":
            gate.set()
            time.sleep(0.05)
            return 2  # A 只寫出 2 bytes
        return len(data)

    monkeypatch.setattr(uio.os, "write", w)
    ta = threading.Thread(target=bridge.send_bytes, args=(b"AAAAA",), kwargs={"source": "agent:a"})
    ta.start()
    gate.wait(1.0)
    tb = threading.Thread(target=bridge.send_bytes, args=(b"BB",), kwargs={"source": "agent:b"})
    tb.start()
    ta.join()
    tb.join()
    assert seen == [b"AAAAA", b"AAA", b"BB"]  # OS 寫入序列：A[:2]、A[2:]、B
    assert [base64.b64decode(x["payload_b64"]) for x in wal_records(wal) if x["dir"] == "TX"] == [
        b"AAAAA",
        b"BB",
    ]


@pytest.mark.core_portable
def test_flash_gate_returns_not_submitted_silently(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    FakePort, make_bridge, wal_records = _load("stage0_fakes", "FakePort", "make_bridge", "wal_records")
    port = FakePort()
    bridge, wal = make_bridge(tmp_path, port=port)
    bridge.set_flash_mode(True)
    r = bridge.send_bytes(b"x", source="agent:x")
    assert r == TxResult("not_submitted", 0, 1, None)
    assert port.writes == [] and wal_records(wal) == []


@pytest.mark.core_portable
def test_tx_wal_order_equals_os_write_order(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    make_bridge, wal_records = _load("stage0_fakes", "make_bridge", "wal_records")
    bridge, wal = make_bridge(tmp_path)
    bridge._serial = object()
    bridge._serial_fd = 99
    order: list[bytes] = []

    def slow_write(fd, data):
        order.append(bytes(data))
        time.sleep(0.02)
        return len(data)

    monkeypatch.setattr(uio.os, "write", slow_write)
    ts = [
        threading.Thread(target=bridge.send_bytes, args=(p,), kwargs={"source": "agent:x"})
        for p in (b"AAAA", b"BBBB", b"CCCC")
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    logged = [base64.b64decode(x["payload_b64"]) for x in wal_records(wal) if x["dir"] == "TX"]
    assert logged == order


@pytest.mark.core_portable
def test_rx_observer_receives_exact_chunk_seq(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    make_bridge, wal_records = _load("stage0_fakes", "make_bridge", "wal_records")
    got: list[int | None] = []
    bridge, wal = make_bridge(tmp_path)
    bridge._on_rx_data = lambda data, wal_seq=None: got.append(wal_seq)
    wal.append(com="COM1", direction="RX", source="device", payload=b"other")
    bridge._handle_serial_rx(b"mine")
    mine = [x for x in wal_records(wal) if x["com"] == "COM0"][0]
    assert got == [mine["seq"]]


@pytest.mark.core_portable
def test_tx_account_accumulates_confirmed_and_unknown(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [5, 2, OSError(errno.EIO, "x")])
    bridge.send_bytes(b"hello", source="agent:x", cmd_id="c1")
    with pytest.raises(TxIncompleteError):
        bridge.send_bytes(b"abcd", source="agent:x", cmd_id="c1")
    assert bridge.tx_account("c1") == (7, False)
    assert bridge.tx_account("c1") == (0, False)  # 讀後移除


@pytest.mark.core_portable
def test_tx_account_settled_on_completion_and_active_never_evicted(tmp_path, monkeypatch):  # R1-F8
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [1] * 2000)
    bridge.tx_set_active("fg")
    bridge.send_bytes(b"f", source="agent:x", cmd_id="fg")  # 最早插入的在途前景命令
    for i in range(600):  # > 512：已完成命令逐一結算，不累積
        bridge.send_bytes(b"x", source="agent:x", cmd_id=f"c{i}")
        bridge.tx_settle(f"c{i}")
    assert set(bridge._tx_accounts) == {"fg"}
    for i in range(600):  # 漏結算（洩漏）時 FIFO 上限仍成立，但跳過 active
        bridge.send_bytes(b"x", source="agent:x", cmd_id=f"leak{i}")
    assert len(bridge._tx_accounts) == 512 and "fg" in bridge._tx_accounts
    assert bridge.tx_account("fg") == (1, False)  # drain 映射仍拿得到在途證據
    bridge.tx_settle("nope")  # 未知 cmd_id：no-op


@pytest.mark.core_portable
def test_write_returns_zero_is_short_write(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    (wal_records,) = _load("stage0_fakes", "wal_records")
    bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [2, 0])
    with pytest.raises(TxIncompleteError) as ei:
        bridge.send_bytes(b"hello", source="agent:x", cmd_id="c_zero")
    assert ei.value.result == TxResult("completed", 2, 5, "short_write")
    tx = [x for x in wal_records(wal) if x["dir"] == "TX"]
    assert len(tx) == 1 and tx[0]["len"] == 2
    assert base64.b64decode(tx[0]["payload_b64"]) == b"he"


@pytest.mark.core_portable
def test_serial_not_ready_raises_runtimeerror_subclass(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    SerialNotReady, TxIncompleteError = _load("sw_core.uart_io", "SerialNotReady", "TxIncompleteError")
    (make_bridge,) = _load("stage0_fakes", "make_bridge")
    bridge, wal = make_bridge(tmp_path)
    bridge._serial = None
    bridge._serial_fd = None
    with pytest.raises(RuntimeError) as ei:
        bridge.send_bytes(b"abc", source="agent:x")
    exc = ei.value
    assert isinstance(exc, TxIncompleteError)
    assert isinstance(exc, OSError)


@pytest.mark.core_portable
def test_secret_not_logged_even_on_partial(tmp_path, monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    (wal_records,) = _load("stage0_fakes", "wal_records")
    bridge, wal, _ = _posix_bridge(tmp_path, monkeypatch, [3, OSError(errno.EIO, "io error")])
    with pytest.raises(TxIncompleteError):
        bridge.send_secret("my_secret_pass")
    assert [x for x in wal_records(wal) if x["dir"] == "TX"] == []


@pytest.mark.core_portable
def test_pyserial_port_write_maps_timeout_to_unknown():
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxUnknownError,) = _load("sw_core.uart_io", "TxUnknownError")
    from sw_core.config import UartProfile
    from sw_core.serial_port import _PySerialPort
    import serial

    class FakeSer:
        is_open = True

        def __init__(self):
            self.calls = 0

        def write(self, data):
            self.calls += 1
            if self.calls == 1:
                return 2
            raise serial.SerialTimeoutException("Write timeout")

    port = _PySerialPort("COM1", UartProfile())
    port._ser = FakeSer()
    with pytest.raises(TxUnknownError) as ei:
        port.write(b"hello")
    e = ei.value
    assert isinstance(e, OSError)
    assert e.errno == errno.ETIMEDOUT
    assert e.result == TxResult("unknown", 2, 5, "SerialTimeoutException: Write timeout")


@pytest.mark.posix_only
def test_posix_port_write_returns_txresult(monkeypatch):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (ScriptedWrite,) = _load("stage0_fakes", "ScriptedWrite")
    from sw_core.config import UartProfile
    from sw_core.serial_port import _PosixSerialPort

    port = _PosixSerialPort("/dev/fake", UartProfile())
    port._fd = 99
    sw = ScriptedWrite([3, OSError(errno.EIO, "I/O error")])
    monkeypatch.setattr("sw_core.serial_port.os.write", sw)
    r = port.write(b"hello")
    assert r.outcome == "completed" and r.confirmed == 3 and r.requested == 5
    assert "EIO" in (r.error or "") or "OSError" in (r.error or "")


@pytest.mark.core_portable
def test_echo_paced_partial_logs_confirmed_slices_only(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    FakePort, make_bridge, wal_records = _load("stage0_fakes", "FakePort", "make_bridge", "wal_records")
    port = FakePort()
    bridge, wal = make_bridge(tmp_path, port=port)

    order = []

    def custom_write(payload, **kwargs):
        order.append(payload)
        if payload == b"slice1":
            bridge._handle_serial_rx(b"slice1")
        return TxResult("completed", len(payload), len(payload))

    port.write = custom_write

    def send_second():
        time.sleep(0.01)
        bridge.send_bytes(b"SECOND", source="agent:b")

    t = threading.Thread(target=send_second)
    t.start()
    res = bridge.send_command_echo_paced(
        "slice1slice2",
        source="agent:a",
        slice_size=6,
        echo_timeout_s=0.05,
    )
    t.join()
    assert res["ok"] is False
    tx = [x for x in wal_records(wal) if x["dir"] == "TX"]
    assert len(tx) == 2
    assert base64.b64decode(tx[0]["payload_b64"]) == b"slice1slice2"
    assert tx[0]["source"] == "agent:a"
    assert base64.b64decode(tx[1]["payload_b64"]) == b"SECOND"
    assert tx[1]["source"] == "agent:b"


@pytest.mark.core_portable
def test_write_all_legacy_none_return_is_completed(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (make_bridge,) = _load("stage0_fakes", "make_bridge")
    bridge, wal = make_bridge(tmp_path)
    bridge._serial = object()
    bridge._serial_fd = 99
    bridge._write_all = lambda fd, payload: None
    r = bridge.send_bytes(b"hello", source="agent:x")
    assert r == TxResult("completed", 5, 5, None)


@pytest.mark.core_portable
def test_port_without_cancel_kwarg_still_works(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (make_bridge,) = _load("stage0_fakes", "make_bridge")
    written = []

    class LegacyPort:
        def write(self, payload: bytes) -> None:
            written.append(payload)

        def is_alive(self) -> bool:
            return True

        def fileno(self) -> int | None:
            return None

        def close(self) -> None:
            pass

    bridge, wal = make_bridge(tmp_path, port=LegacyPort())
    r = bridge.send_bytes(b"test", source="agent:x")
    assert r == TxResult("completed", 4, 4, None)
    assert written == [b"test"]


@pytest.mark.core_portable
def test_stub_wal_returning_none_does_not_break_rx(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (make_bridge,) = _load("stage0_fakes", "make_bridge")

    class StubWal:
        unhealthy = False

        def append(self, **kwargs):
            return None

    bridge, _ = make_bridge(tmp_path, wal=StubWal())
    got = []
    bridge._on_rx_data = lambda data, wal_seq=None: got.append((data, wal_seq))
    bridge._handle_serial_rx(b"hello")
    assert got == [(b"hello", None)]


@pytest.mark.core_portable
def test_no_auto_resend_after_partial_in_file_transfer(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    FakePort, make_bridge = _load("stage0_fakes", "FakePort", "make_bridge")
    from sw_core.file_transfer import push_file

    test_file = tmp_path / "data.bin"
    test_file.write_bytes(b"12345678901234567890")

    port = FakePort()
    bridge, wal = make_bridge(tmp_path, port=port)

    chunk_writes = []

    def custom_write(payload, **kwargs):
        if b"SW_XFER_DECODER" in payload:
            bridge._handle_serial_rx(b"SW_XFER_DECODER_B64_42\r\nroot@box:~# ")
            return TxResult("completed", len(payload), len(payload))
        if b"SW_XFER_CHUNK_" in payload:
            chunk_writes.append(payload)
            if len(chunk_writes) == 1:
                bridge._handle_serial_rx(b"SW_XFER_CHUNK_42\r\nroot@box:~# ")
                return TxResult("completed", len(payload), len(payload))
            raise TxIncompleteError(TxResult("completed", 2, len(payload), "short_write"))
        return TxResult("completed", len(payload), len(payload))

    port.write = custom_write
    with pytest.raises(Exception):
        res = push_file(
            bridge,
            str(test_file),
            "/tmp/dest.bin",
            prompt_regex=r"root@box:~#\s",
            chunk_size=10,
            ack_mode="none",
        )
        if isinstance(res, dict) and not res.get("ok"):
            raise RuntimeError("push_file failed as expected")

    assert len(chunk_writes) == 2


@pytest.mark.core_portable
def test_no_auto_resend_after_partial_in_login_fsm(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (TxIncompleteError,) = _load("sw_core.uart_io", "TxIncompleteError")
    FakePort, make_bridge = _load("stage0_fakes", "FakePort", "make_bridge")
    from sw_core.auth import SessionAuth
    from sw_core.config import SessionProfile
    from sw_core.login_fsm import ensure_ready

    user_writes = []

    def custom_write(payload, **kwargs):
        if b"admin" in payload:
            user_writes.append(payload)
            raise TxIncompleteError(TxResult("completed", 2, len(payload), "short_write"))
        return TxResult("completed", len(payload), len(payload))

    port = FakePort()
    port.write = custom_write
    bridge, wal = make_bridge(tmp_path, port=port)
    sp = SessionProfile(
        name="test_p",
        com="COM0",
        device_by_id="/dev/null",
        user_env="TEST_USER",
        pass_env="TEST_PASS",
        prompt_regex=r"root@box:~#\s",
        login_regex=r"login:\s",
        password_regex=r"Password:\s",
        timeout_s=0.1,
    )
    auth = SessionAuth(user="admin", password="password")
    bridge._handle_serial_rx(b"\r\nlogin: ")

    with pytest.raises(Exception):
        ok, _ = ensure_ready(bridge, sp, auth)
        if not ok:
            raise RuntimeError("login failed as expected")

    assert len(user_writes) == 1


@pytest.mark.core_portable
def test_legacy_on_rx_data_single_arg_still_supported(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    (make_bridge,) = _load("stage0_fakes", "make_bridge")
    bridge, wal = make_bridge(tmp_path)
    received = []
    bridge._on_rx_data = lambda d: received.append(d)
    bridge._handle_serial_rx(b"legacy chunk")
    assert received == [b"legacy chunk"]


@pytest.mark.core_portable
def test_background_capture_from_seq_uses_exact_seq(tmp_path):
    (TxResult,) = _load("sw_core.serial_port", "TxResult")
    make_manager, make_profile, wal_records = _load(
        "stage0_fakes", "make_manager", "make_profile", "wal_records"
    )
    from sw_core.session_manager import BackgroundCapture

    p0 = make_profile("p0", com="COM0")
    mgr = make_manager(tmp_path, [p0])
    session = mgr.get_session("COM0")
    assert session is not None
    cap = BackgroundCapture(
        cmd_id="bg1",
        session_id=session.session_id,
        from_seq=1,
        quiet_window_s=1.0,
        created_at="2026-10-16T00:00:00Z",
        last_seq=0,
    )
    mgr._background["bg1"] = cap
    session.background_cmd_ids.append("bg1")

    rec0 = mgr._wal.append(com="COM0", direction="RX", source="device", payload=b"chunk1")
    seq0 = rec0["seq"]
    mgr._wal.append(com="COM1", direction="RX", source="device", payload=b"other_com_chunk")

    mgr._on_bridge_rx(session.session_id, b"chunk1", wal_seq=seq0)
    assert cap.last_seq == seq0
