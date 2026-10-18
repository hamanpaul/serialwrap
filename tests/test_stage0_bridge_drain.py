"""P1 T07（SC-04a）：Bridge 兩段式 drain 與 DrainReceipt 的失敗測試（TDD 紅燈）。

依據：docs/rust-refactor/plans/02-stage0-python-bc.md 的 Task T07；規格 §5.2.3～§5.2.5。
本檔只定義行為斷言；產品碼（`UARTBridge.begin_drain` / `finish_drain` / `DrainReceipt`）於 B1 實作。

依 02 Global Constraints：本 Phase 新增的名稱（`DrainReceipt`、`TxIncompleteError`、`stage0_fakes`、
`sw_core.wal_fs`、`DRAIN_TIMEOUT_MS`）一律在測試函式內經 `_load()` 延遲匯入，
使 `python3 -m pytest -q tests/` 在任何階段都能完成 collection。

平台與資源標註（01 T14 規則 2）：本檔同時含可攜的記憶體替身測試與需要 POSIX PTY 的測試，
因此不放模組層 `pytestmark`，每個測試函式自帶一個平台 marker 與一個資源 marker。
"""

from __future__ import annotations

import errno
import importlib
import os
import threading
import time
from typing import Any

import pytest

import sw_core.uart_io as uio
from sw_core.config import UartProfile
from sw_core.wal import WalWriter

# 延遲匯入來源表（02 Global Constraints：測試模組頂層不得 import 本 Phase 才新增的名稱）。
_ORIGIN = {
    "FakePort": "stage0_fakes",
    "make_bridge": "stage0_fakes",
    "DrainReceipt": "sw_core.uart_io",
    "TxIncompleteError": "sw_core.uart_io",
    "PosixWalFs": "sw_core.wal_fs",
    "DRAIN_TIMEOUT_MS": "sw_core.constants",
}


def _load(*names: str) -> tuple[Any, ...]:
    """延遲匯入指定名稱；只在測試函式內呼叫，collection 階段不會觸發 ImportError。"""
    return tuple(getattr(importlib.import_module(_ORIGIN[name]), name) for name in names)


def _pty_bridge(tmp_path: Any) -> tuple[uio.UARTBridge, int, int]:
    """以 PTY slave 作 device_path 建立 UARTBridge（供需要 start() 的測試）。

    回傳 (bridge, master_fd, slave_fd)；呼叫端負責 stop() 與關閉兩個 fd。
    """
    master_fd, slave_fd = os.openpty()
    bridge = uio.UARTBridge(
        com="COM0",
        device_path=os.ttyname(slave_fd),
        profile=UartProfile(),
        wal=WalWriter(wal_dir=str(tmp_path / "wal")),
    )
    return bridge, master_fd, slave_fd


def _capture(errs: list[BaseException], bridge: uio.UARTBridge) -> None:
    """在背景執行緒送出一筆 TX，把例外收進 errs（供 drain 取消斷言）。"""
    try:
        bridge.send_bytes(b"zzz", source="agent:x")
    except BaseException as e:  # noqa: BLE001
        errs.append(e)


# ---- 行為規格 1、2：begin_drain 凍結新 TX（fence）與 idle 回條 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_begin_drain_freezes_new_tx(tmp_path):
    FakePort, make_bridge, TxIncompleteError = _load("FakePort", "make_bridge", "TxIncompleteError")
    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port)
    bridge.begin_drain(7, preserve_consoles=False)
    with pytest.raises(TxIncompleteError) as ei:
        bridge.send_bytes(b"x", source="agent:x")
    assert ei.value.result.outcome == "not_submitted" and ei.value.result.error == "draining"
    assert port.writes == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_idle_drain_returns_receipt_and_closes_once(tmp_path):
    FakePort, make_bridge, DrainReceipt = _load("FakePort", "make_bridge", "DrainReceipt")
    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port)
    bridge.begin_drain(7, preserve_consoles=False)
    r = bridge.finish_drain(1.0)
    assert isinstance(r, DrainReceipt)
    assert (r.epoch, r.handle_closed, r.writer_converged, r.reader_stopped) == (7, True, True, True)
    assert port.close_calls == 1 and bridge.drain_pending is False


# ---- 行為規格 3：在途寫入使回條延後到收斂之後 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_inflight_write_delays_receipt_until_converged(tmp_path):
    FakePort, make_bridge, DrainReceipt = _load("FakePort", "make_bridge", "DrainReceipt")
    gate = threading.Event()
    port = FakePort(block=gate)
    bridge, _ = make_bridge(tmp_path, port=port)
    # daemon=True：紅燈階段若 begin_drain 缺失，writer 會卡在 gate 上，不得阻止行程結束。
    t = threading.Thread(target=lambda: bridge.send_bytes(b"abc", source="agent:x", cmd_id="c1"), daemon=True)
    t.start()
    time.sleep(0.05)
    late: list[DrainReceipt] = []
    got_late = threading.Event()
    bridge.begin_drain(3, preserve_consoles=False)
    r = bridge.finish_drain(0.1, on_late_receipt=lambda rc: (late.append(rc), got_late.set()))
    assert r is None and bridge.drain_pending is True and port.close_calls == 0
    with pytest.raises(RuntimeError):
        bridge.start()
    gate.set()
    t.join(1.0)
    assert got_late.wait(2.0)
    assert late[0].handle_closed is True and late[0].epoch == 3
    assert port.writes == [b"abc"] and port.close_calls == 1


# ---- 行為規格 1、2：EAGAIN 中的 writer 被 drain 取消 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_eagain_writer_is_cancelled_by_drain(tmp_path, monkeypatch):
    FakePort, make_bridge, TxIncompleteError = _load("FakePort", "make_bridge", "TxIncompleteError")
    bridge, _ = make_bridge(tmp_path)
    bridge._serial = FakePort()
    bridge._serial_fd = 99

    def _eagain(fd: int, data: Any) -> int:
        raise BlockingIOError(errno.EAGAIN, "again")

    monkeypatch.setattr(uio.os, "write", _eagain)
    errs: list[BaseException] = []
    # daemon=True：紅燈階段若 begin_drain 缺失，此執行緒會在 EAGAIN 迴圈中空轉，不得阻止行程結束。
    t = threading.Thread(target=lambda: _capture(errs, bridge), daemon=True)
    t.start()
    time.sleep(0.05)
    bridge.begin_drain(1, preserve_consoles=False)
    r = bridge.finish_drain(1.0)
    t.join(1.0)
    assert r is not None and r.writer_converged is True
    assert isinstance(errs[0], TxIncompleteError) and errs[0].result.error == "cancelled"


# ---- 行為規格 5：stop() 語意（基線：第二次回 None、port 只關一次）----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_stop_twice_closes_port_once(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port)
    bridge.stop()
    assert bridge.stop() is None  # 基線語意：第二次無 preserved
    assert port.close_calls == 1


# ---- 行為規格 4：回條的 unrecorded 列出未落盤的 TX ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_receipt_lists_unrecorded_when_wal_write_fails(tmp_path):
    FakePort, make_bridge, PosixWalFs = _load("FakePort", "make_bridge", "PosixWalFs")

    class _BrokenWalFs(PosixWalFs):
        """每次開檔都失敗的 WalFs，模擬權威 WAL 不可寫（T02 的 fs 注入點）。"""

        def open_append(self, path: str) -> Any:
            raise OSError(errno.EIO, "wal disk gone")

    wal = WalWriter(wal_dir=str(tmp_path / "wal"), fs=_BrokenWalFs())
    gate = threading.Event()
    port = FakePort(block=gate)
    bridge, _ = make_bridge(tmp_path, port=port, wal=wal)
    t = threading.Thread(
        target=lambda: bridge.send_bytes(b"abc", source="agent:x", cmd_id="c1"), daemon=True
    )
    t.start()
    time.sleep(0.05)
    bridge.begin_drain(5, preserve_consoles=False)
    threading.Timer(0.1, gate.set).start()
    r = bridge.finish_drain(2.0)
    t.join(1.0)
    assert r is not None and r.handle_closed is True
    assert any(s.startswith("TX cmd_id=c1 bytes=3") for s in r.unrecorded)


# ---- 行為規格 1：重複 begin_drain 去重 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_begin_drain_twice_is_noop(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port)
    bridge.begin_drain(7, preserve_consoles=False)
    bridge.begin_drain(9, preserve_consoles=False)
    r = bridge.finish_drain(1.0)
    assert r is not None and r.epoch == 7


# ---- 行為規格 3：preserve_consoles 的 console 進入回條且 fd 不關閉（POSIX PTY）----


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_preserve_consoles_returned_in_receipt(tmp_path):
    bridge, master_fd, slave_fd = _pty_bridge(tmp_path)
    preserved_clients: list[Any] = []
    try:
        bridge.start()
        cid = bridge.attach_console(label="extra")["client_id"]
        bridge.begin_drain(11, preserve_consoles=True)
        r = bridge.finish_drain(1.0)
        assert r is not None and r.preserved is not None
        preserved_clients = list(r.preserved.clients.values())
        assert r.preserved.has_client(cid)
        client = r.preserved.clients[cid]
        os.fstat(client.master_fd)  # fd 未關閉：若已關閉此處擲 OSError
        os.fstat(client.slave_fd)
    finally:
        for client in preserved_clients:
            uio._close_console_client_fds(client)
        bridge.stop()
        os.close(master_fd)
        os.close(slave_fd)


# ---- 行為規格 3：port.close() 失敗時回條 handle_closed=False，且不清除 drain_pending ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_port_close_error_reports_handle_not_closed(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    port = FakePort()

    def _boom() -> None:
        raise OSError(errno.EIO, "close failed")

    port.close = _boom  # type: ignore[method-assign]
    bridge, _ = make_bridge(tmp_path, port=port)
    bridge.begin_drain(2, preserve_consoles=False)
    r = bridge.finish_drain(1.0)
    assert r is not None and r.handle_closed is False and r.error
    assert bridge.drain_pending is True


# ---- 行為規格 3：reader 執行緒在 poll 週期內停止（POSIX PTY）----


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_reader_thread_stops_within_poll(tmp_path):
    bridge, master_fd, slave_fd = _pty_bridge(tmp_path)
    try:
        bridge.start()
        t0 = time.monotonic()
        bridge.begin_drain(4, preserve_consoles=False)
        r = bridge.finish_drain(1.0)
        elapsed = time.monotonic() - t0
        assert r is not None and r.reader_stopped is True
        assert elapsed < 1.0
    finally:
        bridge.stop()
        os.close(master_fd)
        os.close(slave_fd)


# ---- R3-F3：drain 共同期限自 begin_drain 起算，不從 finish_drain 重新計時 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_finish_drain_deadline_counts_from_begin(tmp_path):
    FakePort, make_bridge, DRAIN_TIMEOUT_MS = _load("FakePort", "make_bridge", "DRAIN_TIMEOUT_MS")
    gate = threading.Event()
    port = FakePort(block=gate)
    bridge, _ = make_bridge(tmp_path, port=port)
    writer = threading.Thread(
        target=lambda: bridge.send_bytes(b"abc", source="agent:x", cmd_id="c1"), daemon=True
    )
    writer.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    bridge.begin_drain(3, preserve_consoles=False, deadline=t0 + 0.3)
    time.sleep(0.25)
    late: list[Any] = []
    got_late = threading.Event()
    r = bridge.finish_drain(1.0, on_late_receipt=lambda rc: (late.append(rc), got_late.set()))
    elapsed = time.monotonic() - t0
    assert r is None
    assert elapsed < 0.45  # 舊實作從 finish 起算會等到 t0+1.25 s
    assert bridge.drain_pending is True
    assert port.close_calls == 0
    gate.set()
    writer.join(1.0)
    assert got_late.wait(2.0)
    assert late[0].handle_closed is True
    assert port.close_calls == 1

    # 未帶 deadline：期限為 begin_drain 呼叫當下起算的 DRAIN_TIMEOUT_MS
    bridge2, _ = make_bridge(tmp_path, port=FakePort())
    t_call = time.monotonic()
    bridge2.begin_drain(4, preserve_consoles=False)
    t_ret = time.monotonic()
    assert t_call + DRAIN_TIMEOUT_MS / 1000 <= bridge2._drain_deadline <= t_ret + DRAIN_TIMEOUT_MS / 1000


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_reader_callback_can_stop_and_receive_preserved_consoles(tmp_path):
    bridge, master_fd, slave_fd = _pty_bridge(tmp_path)
    returned: list[Any] = []
    callback_done = threading.Event()
    bridge._on_rx_data = lambda _data: (returned.append(bridge.stop(preserve_consoles=True)), callback_done.set())
    preserved = None
    try:
        bridge.start()
        os.write(master_fd, b"rx")
        assert callback_done.wait(1.0)
        preserved = returned[0]
        assert isinstance(preserved, uio.PreservedConsoles)
        assert preserved.clients
        for client in preserved.clients.values():
            os.fstat(client.master_fd)
            os.fstat(client.slave_fd)
        assert bridge._thread is not None
        bridge._thread.join(1.0)
        assert not bridge._thread.is_alive()
    finally:
        if preserved is None:
            receipt = bridge.finish_drain(1.0)
            preserved = receipt.preserved if receipt is not None else None
        if preserved is not None:
            for client in preserved.clients.values():
                uio._close_console_client_fds(client)
        bridge.stop()
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_drain_timeout_closes_preserved_consoles_that_cannot_be_returned(tmp_path, monkeypatch):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    constants = importlib.import_module("sw_core.constants")
    monkeypatch.setattr(constants, "DRAIN_TIMEOUT_MS", 60)
    gate = threading.Event()
    write_entered = threading.Event()
    port = FakePort(block=gate)
    bridge, _ = make_bridge(tmp_path, port=port)
    original_write = port.write

    def _blocked_write(payload: bytes, **kwargs: Any) -> Any:
        write_entered.set()
        return original_write(payload, **kwargs)

    port.write = _blocked_write  # type: ignore[method-assign]
    client = bridge._create_console_client("preserved")
    bridge._clients[client.client_id] = client
    bridge._primary_client_id = client.client_id
    writer = threading.Thread(
        target=lambda: bridge.send_bytes(b"abc", source="agent:x"), daemon=True
    )
    writer.start()
    try:
        assert write_entered.wait(1.0)
        started = time.monotonic()
        assert bridge.stop(preserve_consoles=True) is None
        assert time.monotonic() - started < 0.5
        os.fstat(client.master_fd)
        gate.set()
        deadline = time.monotonic() + 1.0
        while port.close_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert port.close_calls == 1
        with pytest.raises(OSError):
            os.fstat(client.master_fd)
    finally:
        gate.set()
        writer.join(1.0)
        uio._close_console_client_fds(client)


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_unsafe_stop_receipt_closes_preserved_consoles(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port)
    client = bridge._create_console_client("preserved")
    bridge._clients[client.client_id] = client
    bridge._primary_client_id = client.client_id
    port.close = lambda: (_ for _ in ()).throw(OSError("close failed"))  # type: ignore[method-assign]
    try:
        assert bridge.stop(preserve_consoles=True) is None
        with pytest.raises(OSError):
            os.fstat(client.master_fd)
    finally:
        uio._close_console_client_fds(client)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_stop_reads_drain_timeout_constant_at_call_time(tmp_path, monkeypatch):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    constants = importlib.import_module("sw_core.constants")
    monkeypatch.setattr(constants, "DRAIN_TIMEOUT_MS", 80)
    gate = threading.Event()
    write_entered = threading.Event()
    port = FakePort(block=gate)
    bridge, _ = make_bridge(tmp_path, port=port)
    original_write = port.write

    def _blocked_write(payload: bytes, **kwargs: Any) -> Any:
        write_entered.set()
        return original_write(payload, **kwargs)

    port.write = _blocked_write  # type: ignore[method-assign]
    writer = threading.Thread(
        target=lambda: bridge.send_bytes(b"abc", source="agent:x"), daemon=True
    )
    writer.start()
    try:
        assert write_entered.wait(1.0)
        started = time.monotonic()
        assert bridge.stop() is None
        assert time.monotonic() - started < 0.5
    finally:
        gate.set()
        writer.join(1.0)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_begin_drain_invalidation_runs_inside_state_lock(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    bridge, _ = make_bridge(tmp_path, port=FakePort())
    entered = threading.Event()
    release = threading.Event()
    acquired: list[bool] = []

    invalidate_locked = bridge._invalidate_deferred_locked

    def _invalidate(reason: str) -> int:
        entered.set()
        release.wait(1.0)
        assert bridge._tx_fence_lock.locked()
        return invalidate_locked(reason)

    bridge._invalidate_deferred_locked = _invalidate
    drain_thread = threading.Thread(
        target=lambda: bridge.begin_drain(9, preserve_consoles=False), daemon=True
    )
    drain_thread.start()
    try:
        assert entered.wait(1.0)

        def _probe_state_lock() -> None:
            got_lock = bridge._state_lock.acquire(timeout=0.1)
            acquired.append(got_lock)
            if got_lock:
                bridge._state_lock.release()

        probe = threading.Thread(target=_probe_state_lock, daemon=True)
        probe.start()
        probe.join(0.5)
        assert acquired == [False]
    finally:
        release.set()
        drain_thread.join(1.0)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_start_open_does_not_block_drain_and_aborts_if_drain_started(tmp_path, monkeypatch):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    port = FakePort()
    bridge, _ = make_bridge(tmp_path)
    open_entered = threading.Event()
    open_release = threading.Event()
    original_open = port.open
    start_errors: list[BaseException] = []
    drain_done = threading.Event()

    def _blocked_open() -> None:
        open_entered.set()
        open_release.wait(1.0)
        original_open()

    def _start() -> None:
        try:
            bridge.start()
        except BaseException as exc:  # noqa: BLE001 — 收集預期的 drain 拒絕
            start_errors.append(exc)

    port.open = _blocked_open  # type: ignore[method-assign]
    monkeypatch.setattr(uio, "open_serial_port", lambda *_args: port)
    monkeypatch.setattr(uio, "_pty_available", lambda: False)
    monkeypatch.setattr(bridge, "_start_console_listener", lambda: None)
    starter = threading.Thread(target=_start, daemon=True)
    starter.start()
    try:
        assert open_entered.wait(1.0)
        drainer = threading.Thread(
            target=lambda: (bridge.begin_drain(10, preserve_consoles=False), drain_done.set()),
            daemon=True,
        )
        drainer.start()
        drain_not_blocked = drain_done.wait(0.2)
        open_release.set()
        starter.join(1.0)
        drainer.join(1.0)
        assert drain_not_blocked
        assert len(start_errors) == 1
        assert isinstance(start_errors[0], RuntimeError)
        assert str(start_errors[0]) == "drain in progress"
        assert port.close_calls == 1
        assert bridge._serial is None
    finally:
        open_release.set()
        starter.join(1.0)
        bridge.stop()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_late_receipt_callback_survives_restart_race(tmp_path, monkeypatch):
    FakePort, make_bridge, DrainReceipt = _load("FakePort", "make_bridge", "DrainReceipt")
    gate = threading.Event()
    port = FakePort(block=gate)
    bridge, _ = make_bridge(tmp_path, port=port)
    write_entered = threading.Event()
    original_write = port.write

    def _blocked_write(payload: bytes, **kwargs: Any) -> Any:
        write_entered.set()
        return original_write(payload, **kwargs)

    port.write = _blocked_write  # type: ignore[method-assign]
    writer = threading.Thread(
        target=lambda: bridge.send_bytes(b"abc", source="agent:x"), daemon=True
    )
    writer.start()
    assert write_entered.wait(1.0)
    callback_done = threading.Event()
    late_receipts: list[DrainReceipt] = []
    completion_published = threading.Event()
    resume_worker = threading.Event()
    original_complete = bridge._complete_drain

    def _pause_after_completion(*args: Any, **kwargs: Any) -> Any:
        receipt = original_complete(*args, **kwargs)
        if kwargs.get("wait_forever") and receipt is not None:
            completion_published.set()
            resume_worker.wait(1.0)
        return receipt

    bridge._complete_drain = _pause_after_completion  # type: ignore[method-assign]
    try:
        bridge.begin_drain(12, preserve_consoles=False)
        assert bridge.finish_drain(
            0.02,
            on_late_receipt=lambda receipt: (late_receipts.append(receipt), callback_done.set()),
        ) is None
        gate.set()
        assert completion_published.wait(1.0)
        restarted_port = FakePort()
        monkeypatch.setattr(uio, "open_serial_port", lambda *_args: restarted_port)
        monkeypatch.setattr(uio, "_pty_available", lambda: False)
        monkeypatch.setattr(bridge, "_start_console_listener", lambda: None)
        bridge.start()
        resume_worker.set()
        assert callback_done.wait(1.0)
        assert len(late_receipts) == 1
        assert late_receipts[0].handle_closed
    finally:
        gate.set()
        resume_worker.set()
        writer.join(1.0)
        bridge.stop()


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_concurrent_stop_delivers_preserved_consoles_once(tmp_path):
    bridge, master_fd, slave_fd = _pty_bridge(tmp_path)
    close_entered = threading.Event()
    close_release = threading.Event()
    first_result: list[Any] = []
    second_result: list[Any] = []
    close_calls: list[bool] = []
    preserved = []
    try:
        bridge.start()
        client_id = bridge.attach_console(label="extra")["client_id"]
        port = bridge._serial
        original_close = port.close

        def _blocked_close() -> None:
            close_entered.set()
            close_release.wait(1.0)
            close_calls.append(True)
            original_close()

        port.close = _blocked_close  # type: ignore[method-assign]
        first = threading.Thread(
            target=lambda: first_result.append(bridge.stop(preserve_consoles=True)), daemon=True
        )
        first.start()
        assert close_entered.wait(1.0)
        second_done = threading.Event()
        second_started = threading.Event()

        def _second_stop() -> None:
            second_started.set()
            second_result.append(bridge.stop(preserve_consoles=True))
            second_done.set()

        second = threading.Thread(target=_second_stop, daemon=True)
        second.start()
        assert second_started.wait(1.0)
        second_finished_while_close_blocked = second_done.wait(0.5)
        close_release.set()
        first.join(1.0)
        second.join(1.0)
        assert second_done.is_set()
        assert second_finished_while_close_blocked
        delivered = [item for item in (first_result + second_result) if item is not None]
        assert len(delivered) == 1
        preserved = list(delivered[0].clients.values())
        assert delivered[0].has_client(client_id)
        assert len(close_calls) == 1
    finally:
        close_release.set()
        for client in preserved:
            uio._close_console_client_fds(client)
        bridge.stop()
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_drain_gate_follows_flash_gate(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    bridge, _ = make_bridge(tmp_path, port=FakePort())
    bridge._flash_mode = True
    bridge.begin_drain(13, preserve_consoles=False)
    result = bridge.send_bytes(b"x", source="agent:x")
    assert result.outcome == "not_submitted"
    assert result.error is None


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_drain_gate_follows_wal_unhealthy_gate(tmp_path):
    FakePort, make_bridge = _load("FakePort", "make_bridge")
    bridge, _ = make_bridge(tmp_path, port=FakePort())
    bridge.wal = type("UnhealthyWal", (), {"unhealthy": True})()
    bridge.begin_drain(14, preserve_consoles=False)
    with pytest.raises(uio.WalUnavailableError):
        bridge.send_bytes(b"x", source="system:login")
