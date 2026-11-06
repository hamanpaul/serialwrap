"""tests/test_od19_tick_wiring.py — OD-19 週期 tick 接線與延後輪替完成（服務層回歸）。

既有 ``tests/test_stage0_wal_rotate.py`` 直接呼叫 ``WalWriter.tick()``，只證明 tick 語意，無法證明 daemon 會呼叫它。
本檔改走 ``SerialwrapService`` 的 DeviceWatcher 回呼（``svc._watcher._on_tick``），驗證：

- 停止寫入（無後續 append）後，延後輪替仍由週期 tick 完成，``deferred_delete_pending`` 歸零、現行檔存在、
  ``WAL_MISSING`` 不觸發（tick 驅動輪替的連續性 META 維持健康判定）；
- 上述 tick 完成的輪替於重啟後 seq 續接（N+1），不與歸檔段重複；
- ``WalWriter.tick`` 與 ``reconcile_readiness`` 互不遮蔽：任一拋例外，另一項仍執行。

全部使用注入式 fs／時鐘與 tmp_path 隔離狀態，不觸碰真實裝置、正式 daemon 或防毒產品。
"""
from __future__ import annotations

import errno
import json
import os

import pytest

import sw_core.wal as wal_mod
from sw_core.wal import WalWriter
from sw_core.wal_fs import PosixWalFs
from tests.stage0_fakes import make_service

pytestmark = [pytest.mark.memory_only]


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class _SharingFs(PosixWalFs):
    """前 ``failures`` 次 rename 注入共用違規（注入式干擾，不依賴防毒產品）。"""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining = failures
        self.rename_attempts = 0

    def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str:
        self.rename_attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise PermissionError(errno.EACCES, "injected sharing violation")
        return super().rename_noreplace_or_suffix(src, base_dst)


@pytest.fixture
def fixed_ts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wal_mod.time, "strftime", lambda fmt, t=None: "20261015-030617")


def _append_rx(w: WalWriter) -> dict:
    return w.append(com="COM0", direction="RX", source="device", payload=b"")


def _drive_to_deferred(wal: WalWriter, clk: _Clock) -> None:
    """與 test_stage0_wal_rotate 同序列：首次＋三段退避皆失敗 → 轉入延遲佇列。"""
    _append_rx(wal)
    _append_rx(wal)
    for gap in (0.011, 0.051, 0.201):
        clk.advance(gap)
        _append_rx(wal)
    assert wal.health()["deferred_delete_pending"] == 1


@pytest.mark.core_portable
def test_idle_watcher_tick_completes_deferred_rotation_without_new_append(tmp_path, fixed_ts):
    svc = make_service(tmp_path)
    assert svc._watcher._on_tick == svc._on_watcher_tick  # 接線本身：DeviceWatcher 週期回呼即服務方法
    clk = _Clock()
    fs = _SharingFs(4)
    svc._wal.close()
    svc._wal = WalWriter(wal_dir=str(tmp_path / "wal"), rotate_bytes=200, fs=fs, clock=clk)
    wal = svc._wal
    _drive_to_deferred(wal, clk)
    assert fs.rename_attempts == 4

    # 停止寫入：只靠 DeviceWatcher 的週期回呼推進（不直接呼叫 wal.tick）。
    clk.advance(1.01)
    svc._watcher._on_tick()
    assert fs.rename_attempts == 5
    h = wal.health()
    assert h["deferred_delete_pending"] == 0
    assert h["wal_file_exists"] is True
    assert h["healthy"] is True
    assert svc._wal_missing_response(h) is None


@pytest.mark.core_portable
def test_idle_tick_rotation_keeps_seq_continuous_across_restart(tmp_path, fixed_ts):
    svc = make_service(tmp_path)
    clk = _Clock()
    fs = _SharingFs(4)
    wal_dir = str(tmp_path / "wal")
    svc._wal.close()
    svc._wal = WalWriter(wal_dir=wal_dir, rotate_bytes=200, fs=fs, clock=clk)
    wal = svc._wal
    _drive_to_deferred(wal, clk)
    clk.advance(1.01)
    svc._watcher._on_tick()
    last_seq = wal.health()["current_seq"]
    wal.close()

    with open(os.path.join(wal_dir, "raw.wal.ndjson"), encoding="utf-8") as fp:
        rows = [json.loads(line) for line in fp if line.strip()]
    assert rows[0]["dir"] == "META"
    assert rows[0]["meta"]["event"] == "WAL_ROTATED_AFTER_DEFERRAL"
    assert rows[0]["seq"] == last_seq  # 現行檔首筆即 tick 補寫的連續性 META

    restarted = WalWriter(wal_dir=wal_dir, rotate_bytes=10**9, clock=clk)
    assert _append_rx(restarted)["seq"] == last_seq + 1
    restarted.close()


@pytest.mark.core_portable
def test_watcher_tick_runs_reconcile_even_when_wal_tick_raises(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    calls: list[str] = []

    def _boom() -> bool:
        raise RuntimeError("wal tick boom")

    monkeypatch.setattr(svc._wal, "tick", _boom)
    monkeypatch.setattr(svc._sessions, "reconcile_readiness", lambda: calls.append("reconcile"))
    with pytest.raises(RuntimeError, match="wal tick boom"):
        svc._on_watcher_tick()
    assert calls == ["reconcile"]


@pytest.mark.core_portable
def test_watcher_tick_runs_wal_tick_even_when_reconcile_raises(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    calls: list[str] = []

    def _boom() -> None:
        raise RuntimeError("reconcile boom")

    monkeypatch.setattr(svc._wal, "tick", lambda: calls.append("tick") or False)
    monkeypatch.setattr(svc._sessions, "reconcile_readiness", _boom)
    with pytest.raises(RuntimeError, match="reconcile boom"):
        svc._on_watcher_tick()
    assert calls == ["tick"]
