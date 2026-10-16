"""BC-01d：WAL 外部 unlink／替換／DeletePending 偵測、有界自癒、缺口 META、停止自動 TX（Stage 0 P1 T05）。

本檔為 TDD 紅燈階段的失敗測試，斷言下列行為（實作前應因行為缺失而失敗）：

1. 名稱消失（目錄不存在或檔案不存在）每筆即時偵測、不節流；identity／DeletePending 比對
   依 ``WAL_IDENTITY_CHECK_INTERVAL_S`` 節流。
2. 重建成功時先寫 ``WAL_RECREATED_AFTER_EXTERNAL_UNLINK`` 缺口 META（外層 ``loss_flag:true``），
   再寫原紀錄；META 欄位依 ``contracts/wal_meta_events.schema.json`` 的對應分支。
3. 遺失範圍未知時 ``potentially_lost_seq_range`` 為 ``None`` 且 ``range_known`` 為 ``False``，不猜測。
4. 重建重試有界、以 ``clock`` 判定退避、WalWriter 任何路徑都不睡眠。
5. 重建失敗或 WAL 寫入失敗時標示不健康，停止 ``system`` 來源的自動 TX；agent／human 來源不受限。

平台：核心行為以 POSIX 檔案系統與替身驗證（``core_portable``）。Windows 分享旗標與 DeletePending
語意以 ``windows_native`` 測試在原生 Windows 實跑；非 Windows 時 SKIP（SKIP 不計為 PASS，屬證據不足）。
新名稱（``sw_core.wal_fs``、``sw_core.win32_files``、``stage0_fakes``、``WalUnavailableError``）
一律於函式內延遲 import，使本檔在 Stage A 仍可被 collection。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from sw_core.wal import WalWriter

pytestmark = [pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
_NATIVE_SKIP = pytest.mark.skipif(
    os.name != "nt",
    reason="windows_native：請以 scripts/stage0/win-pytest.sh 執行（證據不足，非 PASS）",
)


def _load_posix_fs() -> Any:
    from sw_core.wal_fs import PosixWalFs

    return PosixWalFs


def _load_windows_fs() -> Any:
    from sw_core.wal_fs import WindowsWalFs

    return WindowsWalFs


def _load_w32() -> Any:
    from sw_core import win32_files

    return win32_files


class Clock:
    """可手動推進的注入時鐘（取代 time.monotonic）。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _lines(p: str | Path) -> list[dict[str, Any]]:
    with open(p, encoding="utf-8") as fp:
        return [json.loads(line) for line in fp if line.strip()]


def _w(tmp_path: str | Path, clock: Clock, fs: Any = None, **kwargs: Any) -> WalWriter:
    # 未指定 fs 時不載入 sw_core.wal_fs，由 WalWriter 預設值（POSIX 為 PosixWalFs）決定
    if fs is not None:
        kwargs["fs"] = fs
    return WalWriter(wal_dir=str(tmp_path), clock=clock, **kwargs)


def _rx(w: WalWriter, payload: bytes) -> dict[str, Any]:
    return w.append(com="COM0", direction="RX", source="device", payload=payload)


@pytest.mark.core_portable
def test_external_unlink_recreates_and_writes_meta_first(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    for _ in range(3):
        _rx(w, b"a")
    st = os.stat(w.wal_path)
    os.unlink(w.wal_path)
    clk.advance(0.06)
    rec = _rx(w, b"b")
    lines = _lines(w.wal_path)
    meta = lines[0]
    assert meta["dir"] == "META" and meta["com"] == "WAL" and meta["source"] == "system"
    assert meta["loss_flag"] is True and meta["seq"] == 4
    assert meta["meta"] == {
        "event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK", "reason": "external_unlink",
        "previous_identity": f"{st.st_dev}:{st.st_ino}",
        "potentially_lost_seq_range": [1, 3], "range_known": True,
    }
    assert lines[1]["seq"] == rec["seq"] == 5
    assert w.health()["recreated_count"] == 1 and w.health()["healthy"] is True


@pytest.mark.core_portable
def test_replaced_file_detected(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    tmp = tmp_path / "other"
    tmp.write_text("", encoding="utf-8")
    os.replace(tmp, w.wal_path)
    clk.advance(0.06)
    _rx(w, b"b")
    metas = [line for line in _lines(w.wal_path) if line["dir"] == "META"]
    assert metas[0]["meta"]["reason"] == "replaced"


@pytest.mark.core_portable
def test_dir_missing_reason(tmp_path):
    d = tmp_path / "wal"
    clk = Clock()
    w = _w(d, clk)
    _rx(w, b"a")
    shutil.rmtree(d)
    clk.advance(0.06)
    _rx(w, b"b")
    assert _lines(w.wal_path)[0]["meta"]["reason"] == "dir_missing"


@pytest.mark.core_portable
def test_check_is_throttled_to_interval(tmp_path):
    PosixWalFs = _load_posix_fs()
    calls: list[str] = []

    class F(PosixWalFs):
        def path_identity(self, p):
            calls.append(p)
            return super().path_identity(p)

    clk = Clock()
    w = _w(tmp_path, clk, fs=F())
    for _ in range(10):
        _rx(w, b"a")
    wal_calls = [c for c in calls if c == w.wal_path]
    assert len(wal_calls) <= 1
    clk.advance(0.06)   # 不用 0.05：1000.0+0.05-1000.0 在浮點下 < 0.05
    _rx(w, b"a")
    assert len([c for c in calls if c == w.wal_path]) == len(wal_calls) + 1


@pytest.mark.core_portable
def test_unknown_range_when_first_seq_unknown(tmp_path):
    (tmp_path / "raw.wal.ndjson").write_text(
        "garbage-not-json\n" + json.dumps({"seq": 7}) + "\n", encoding="utf-8"
    )
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    os.unlink(w.wal_path)
    clk.advance(0.06)
    _rx(w, b"b")
    meta = _lines(w.wal_path)[0]["meta"]
    assert meta["potentially_lost_seq_range"] is None and meta["range_known"] is False


@pytest.mark.core_portable
def test_bounded_retry_then_unhealthy_and_auto_tx_stops(tmp_path, monkeypatch):
    import time as _time

    PosixWalFs = _load_posix_fs()
    monkeypatch.setattr(_time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("WalWriter 不得睡眠")))

    class Broken(PosixWalFs):
        broken = False
        attempts = 0

        def open_append(self, p):
            if self.broken and p.endswith("raw.wal.ndjson"):
                self.attempts += 1
                raise PermissionError(13, "Access is denied")
            return super().open_append(p)

    fs = Broken()
    clk = Clock()
    w = _w(tmp_path, clk, fs=fs)
    _rx(w, b"a")
    os.unlink(w.wal_path)
    fs.broken = True
    clk.advance(0.06)
    rec = _rx(w, b"b")
    assert rec["loss_flag"] is True and fs.attempts == 1          # 每筆最多一次嘗試
    h = w.health()
    assert h["healthy"] is False and h["unhealthy_reason"] == "recreate_failed" and w.unhealthy is True
    _rx(w, b"b2")                                                  # 未到退避時間：不嘗試
    assert fs.attempts == 1
    clk.advance(0.011)
    _rx(w, b"b3")
    assert fs.attempts == 2                                        # 第 1 次失敗後 WAL_RECREATE_BACKOFF_S[0]=0.01 s
    from stage0_fakes import FakePort, make_bridge
    from sw_core.uart_io import WalUnavailableError

    port = FakePort()
    bridge, _ = make_bridge(tmp_path / "b", port=port, wal=w)
    with pytest.raises(WalUnavailableError):
        bridge.send_bytes(b"\x03", source="system:recover")
    assert port.writes == []
    bridge.send_bytes(b"ls\n", source="agent:x")
    assert port.writes == [b"ls\n"]
    fs.broken = False
    clk.advance(0.06)
    _rx(w, b"c")
    meta = [line for line in _lines(w.wal_path) if line["dir"] == "META"][0]["meta"]
    assert meta["potentially_lost_seq_range"][0] == 1
    assert w.unhealthy is False


@pytest.mark.core_portable
def test_mirror_unlink_reopens_without_meta(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    os.unlink(w.mirror_path)
    clk.advance(0.06)
    _rx(w, b"b")
    assert os.path.exists(w.mirror_path)
    assert open(w.mirror_path, "rb").read() == b"b"
    assert [line for line in _lines(w.wal_path) if line["dir"] == "META"] == []


@pytest.mark.core_portable
def test_first_seq_after_rotate_used_for_range(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk, rotate_bytes=600)
    for _ in range(10):
        w.append(com="COM0", direction="RX", source="device", payload=b"y" * 40)
    first = _lines(w.wal_path)[0]["seq"]
    assert first > 1   # 確認確實發生輪替，現行檔第一筆 seq 為 N
    os.unlink(w.wal_path)
    clk.advance(0.06)
    _rx(w, b"z")
    meta = [line for line in _lines(w.wal_path) if line["dir"] == "META"][0]["meta"]
    assert meta["potentially_lost_seq_range"] == [first, 10] and meta["range_known"] is True


@pytest.mark.core_portable
def test_meta_conforms_to_contract_schema(tmp_path):
    import jsonschema

    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    os.unlink(w.wal_path)
    clk.advance(0.06)
    _rx(w, b"b")
    meta_row = [line for line in _lines(w.wal_path) if line["dir"] == "META"][0]
    schema = json.loads((REPO_ROOT / "contracts" / "wal_meta_events.schema.json").read_text(encoding="utf-8"))
    branch = next(
        b for b in schema["oneOf"]
        if b.get("properties", {}).get("event", {}).get("const") == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"
    )
    jsonschema.validate(meta_row["meta"], branch)
    for key, value in branch["x-outer-record"].items():
        assert meta_row[key] == value


@pytest.mark.core_portable
def test_health_unhealthy_reason_null_when_healthy(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    assert w.health()["unhealthy_reason"] is None


@pytest.mark.core_portable
def test_unthrottled_name_check_writes_meta_immediately(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    os.unlink(w.wal_path)
    _rx(w, b"b")   # 不推進 clock：名稱檢查不受 50 ms 節流
    lines = _lines(w.wal_path)
    assert lines[0]["dir"] == "META" and lines[0]["meta"]["reason"] == "external_unlink"


@pytest.mark.core_portable
def test_write_failed_also_blocks_auto_tx(tmp_path):
    PosixWalFs = _load_posix_fs()

    class FailingWalFs(PosixWalFs):
        broken = False

        def open_append(self, p):
            inner = super().open_append(p)
            if not p.endswith("raw.wal.ndjson"):
                return inner
            fs = self

            class Wrapped:
                def write_all_flush(self, data):
                    if fs.broken:
                        raise OSError(28, "No space left on device")
                    inner.write_all_flush(data)

                def sync(self):
                    inner.sync()

                def identity(self):
                    return inner.identity()

                def unlinked(self):
                    return inner.unlinked()

                def close(self):
                    inner.close()

            return Wrapped()

    fs = FailingWalFs()
    clk = Clock()
    w = _w(tmp_path, clk, fs=fs)
    _rx(w, b"a")
    fs.broken = True
    _rx(w, b"b")
    assert w.unhealthy is True and w.health()["unhealthy_reason"] == "write_failed"
    from stage0_fakes import FakePort, make_bridge
    from sw_core.uart_io import WalUnavailableError

    port = FakePort()
    bridge, _ = make_bridge(tmp_path / "bridge", port=port, wal=w)
    with pytest.raises(WalUnavailableError):
        bridge.send_bytes(b"login\n", source="system:login")
    assert port.writes == []


@pytest.mark.core_portable
def test_bridge_with_stub_wal_without_unhealthy_attr(tmp_path):
    from stage0_fakes import FakePort, make_bridge

    class StubWal:
        """無 ``unhealthy`` 屬性的既有式 stub WAL（既有測試慣例）。"""

        def __init__(self) -> None:
            self.records: list[dict[str, Any]] = []

        def append(self, **kwargs: Any) -> None:
            self.records.append(kwargs)

    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port, wal=StubWal())
    bridge.send_bytes(b"x", source="system:x")
    assert port.writes == [b"x"]


@pytest.mark.windows_native
@_NATIVE_SKIP
def test_native_classic_delete_pending_with_external_reader(tmp_path):
    w32 = _load_w32()
    WindowsWalFs = _load_windows_fs()
    clk = Clock()
    w = _w(tmp_path, clk, fs=WindowsWalFs())
    _rx(w, b"a")
    wal_path = w.wal_path
    external = w32.open_shared_append(wal_path)   # 外部 share-all handle 持有檔案
    try:
        w32.delete_classic(wal_path)
        assert w32.path_identity(wal_path) == (0, 0)   # classic 語意已生效，否則 FAIL
        assert w._wal_fp.unlinked() is True
        clk.advance(0.06)
        _rx(w, b"b")                                    # 重建因 access denied 失敗
        assert w.unhealthy is True and w.health()["unhealthy_reason"] == "recreate_failed"
    finally:
        external.close()
    clk.advance(5.0)   # 大於 WAL_RECREATE_RETRY_INTERVALS_S 任一退避值，確保已到嘗試時間
    _rx(w, b"c")
    metas = [line for line in _lines(w.wal_path) if line["dir"] == "META"]
    assert len(metas) == 1 and metas[0]["meta"]["reason"] == "external_unlink"
    assert w.unhealthy is False


@pytest.mark.windows_native
@_NATIVE_SKIP
def test_native_posix_semantics_delete(tmp_path):
    w32 = _load_w32()
    WindowsWalFs = _load_windows_fs()
    clk = Clock()
    w = _w(tmp_path, clk, fs=WindowsWalFs())
    _rx(w, b"a")
    w32.delete_posix_semantics(w.wal_path)
    assert w32.path_identity(w.wal_path) is None   # 名稱立即消失
    assert not os.path.exists(w.wal_path)
    clk.advance(0.06)
    _rx(w, b"b")
    assert os.path.exists(w.wal_path)              # 重建成功
    metas = [line for line in _lines(w.wal_path) if line["dir"] == "META"]
    assert len(metas) == 1 and metas[0]["meta"]["reason"] == "external_unlink"
    assert w.unhealthy is False
