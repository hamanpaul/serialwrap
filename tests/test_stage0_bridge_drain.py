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
