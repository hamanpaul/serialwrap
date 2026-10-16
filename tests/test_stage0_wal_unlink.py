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
    from sw_core.wal_fs import default_wal_fs

    # core_portable 測試使用當前平台的 WAL 後端，Windows 才能驗證 share-delete 語意。
    return type(default_wal_fs())


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


def _replace_open_wal(w: WalWriter, content: bytes) -> None:
    wal_path = Path(w.wal_path)
    if os.name == "nt":
        os.rename(w.wal_path, f"{w.wal_path}.old")
        wal_path.write_bytes(content)
        return
    replacement = wal_path.with_name("replacement")
    replacement.write_bytes(content)
    os.replace(replacement, wal_path)


@pytest.mark.core_portable
def test_external_unlink_recreates_and_writes_meta_first(tmp_path, caplog):
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
    assert w.health()["last_write_error"].startswith("FileNotFoundError: [Errno 2] WAL 檔案路徑消失:")
    assert any(
        "WAL 目錄 " + str(tmp_path) + " 消失，已重建並續寫" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.core_portable
def test_second_unlink_range_starts_at_first_meta_seq(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"before first unlink")
    os.unlink(w.wal_path)
    clk.advance(0.06)
    _rx(w, b"after first unlink")
    first_meta = next(row for row in w.tail_raw() if row["dir"] == "META")

    os.unlink(w.wal_path)
    clk.advance(0.06)
    _rx(w, b"after second unlink")

    second_meta = next(row for row in w.tail_raw() if row["dir"] == "META")
    assert second_meta["meta"]["potentially_lost_seq_range"] == [
        first_meta["seq"], first_meta["seq"] + 1
    ]


@pytest.mark.core_portable
def test_replaced_file_detected(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"a")
    _replace_open_wal(w, b"")
    clk.advance(0.06)
    _rx(w, b"b")
    metas = [line for line in _lines(w.wal_path) if line["dir"] == "META"]
    assert metas[0]["meta"]["reason"] == "replaced"


@pytest.mark.parametrize("replacement_size", [600, 601])
@pytest.mark.core_portable
def test_replaced_file_at_rotate_threshold_is_detected_before_rotation(
    tmp_path, replacement_size
):
    clk = Clock()
    w = _w(tmp_path, clk, rotate_bytes=600)
    _rx(w, b"before replacement")
    _replace_open_wal(w, b"x" * replacement_size)

    record = _rx(w, b"after replacement")

    rows = w.tail_raw()
    metas = [row for row in rows if row["dir"] == "META"]
    assert len(metas) == 1
    assert metas[0]["meta"]["reason"] == "replaced"
    assert metas[0]["meta"]["potentially_lost_seq_range"] == [1, 1]
    assert record["seq"] == metas[0]["seq"] + 1


@pytest.mark.core_portable
def test_replaced_unterminated_file_keeps_meta_on_a_parseable_line(tmp_path):
    clk = Clock()
    w = _w(tmp_path, clk)
    _rx(w, b"before replacement")
    _replace_open_wal(w, b"partial")
    clk.advance(0.06)

    record = _rx(w, b"after replacement")

    physical_lines = Path(w.wal_path).read_bytes().splitlines()
    meta_line = next(line for line in physical_lines if b'"dir":"META"' in line)
    meta = json.loads(meta_line)
    assert meta["meta"]["event"] == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"
    assert [row["dir"] for row in w.tail_raw()] == ["META", "RX"]
    assert w.tail_raw()[-1]["seq"] == record["seq"]


@pytest.mark.core_portable
def test_replacement_tail_inspection_failure_stays_unhealthy_until_retry(tmp_path):
    WalFs = _load_posix_fs()

    class F(WalFs):
        fail_tail_inspection = True

        def tail_needs_termination(self, path):
            if self.fail_tail_inspection:
                self.fail_tail_inspection = False
                raise OSError(5, "tail inspection unavailable")
            return super().tail_needs_termination(path)

    clk = Clock()
    fs = F()
    w = _w(tmp_path, clk, fs=fs)
    _rx(w, b"before replacement")
    _replace_open_wal(w, b"partial")
    clk.advance(0.06)

    failed = _rx(w, b"first recreate attempt")
    assert failed["loss_flag"] is True
    assert w.unhealthy is True
    assert w.health()["unhealthy_reason"] == "recreate_failed"
    assert w.tail_raw() == []

    clk.advance(0.011)
    _rx(w, b"retry succeeds")
    rows = w.tail_raw()
    meta = next(row for row in rows if row["dir"] == "META")
    assert meta["meta"]["potentially_lost_seq_range"] == [1, failed["seq"]]
    assert rows[-1]["dir"] == "RX"
    assert w.unhealthy is False


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


@pytest.mark.parametrize("identity_mode", ["zero_sentinel", "same_identity_unlinked"])
@pytest.mark.core_portable
def test_zero_identity_sentinel_and_unlinked_handle_trigger_recreation(
    tmp_path, identity_mode
):
    WalFs = _load_posix_fs()
    wal_path = str(tmp_path / "raw.wal.ndjson")

    class F(WalFs):
        def path_identity(self, path):
            if identity_mode == "zero_sentinel" and path == wal_path:
                return (0, 0)
            return super().path_identity(path)

        def open_append(self, path):
            inner = super().open_append(path)
            if identity_mode != "same_identity_unlinked" or path != wal_path:
                return inner
            fs = self

            class Wrapped:
                def write_all_flush(self, data):
                    inner.write_all_flush(data)

                def sync(self):
                    inner.sync()

                def identity(self):
                    return inner.identity()

                def unlinked(self):
                    return True

                def close(self):
                    inner.close()

            return Wrapped()

    clk = Clock()
    w = _w(tmp_path, clk, fs=F())
    _rx(w, b"before")
    clk.advance(0.06)
    _rx(w, b"after")

    metas = [row for row in w.tail_raw() if row["dir"] == "META"]
    assert len(metas) == 1
    assert metas[0]["meta"]["reason"] == "external_unlink"


@pytest.mark.parametrize("failing_query", ["path_identity", "identity", "unlinked"])
@pytest.mark.core_portable
def test_identity_query_oserror_is_unknown_and_append_continues(tmp_path, failing_query):
    PosixWalFs = _load_posix_fs()
    wal_path = str(tmp_path / "raw.wal.ndjson")

    class F(PosixWalFs):
        def path_identity(self, path):
            if failing_query == "path_identity" and path == wal_path:
                raise OSError(5, "identity unavailable")
            return super().path_identity(path)

        def open_append(self, path):
            handle = super().open_append(path)
            if path != wal_path:
                return handle
            fs = self

            class Wrapped:
                def write_all_flush(self, data):
                    handle.write_all_flush(data)

                def sync(self):
                    handle.sync()

                def identity(self):
                    if failing_query == "identity":
                        raise OSError(5, "identity unavailable")
                    return handle.identity()

                def unlinked(self):
                    if failing_query == "unlinked":
                        raise OSError(5, "identity unavailable")
                    return handle.unlinked()

                def close(self):
                    handle.close()

            return Wrapped()

    clk = Clock()
    w = _w(tmp_path, clk, fs=F())
    _rx(w, b"before")
    clk.advance(0.06)
    record = _rx(w, b"after")

    rows = _lines(w.wal_path)
    assert record["loss_flag"] is False
    assert [row["seq"] for row in rows] == [1, 2]
    assert all(row["dir"] != "META" for row in rows)
    assert w.unhealthy is False


@pytest.mark.parametrize("failing_query", ["path_identity", "identity", "unlinked"])
@pytest.mark.core_portable
def test_identity_query_oserror_at_rotate_threshold_keeps_current_file(
    tmp_path, failing_query
):
    PosixWalFs = _load_posix_fs()
    wal_path = str(tmp_path / "raw.wal.ndjson")

    class F(PosixWalFs):
        fail_identity_query = False

        def path_identity(self, path):
            if self.fail_identity_query and failing_query == "path_identity" and path == wal_path:
                raise OSError(5, "identity unavailable")
            return super().path_identity(path)

        def open_append(self, path):
            handle = super().open_append(path)
            if path != wal_path:
                return handle
            fs = self

            class Wrapped:
                def write_all_flush(self, data):
                    handle.write_all_flush(data)

                def sync(self):
                    handle.sync()

                def identity(self):
                    if fs.fail_identity_query and failing_query == "identity":
                        raise OSError(5, "identity unavailable")
                    return handle.identity()

                def unlinked(self):
                    if fs.fail_identity_query and failing_query == "unlinked":
                        raise OSError(5, "identity unavailable")
                    return handle.unlinked()

                def close(self):
                    handle.close()

            return Wrapped()

    clk = Clock()
    fs = F()
    w = _w(tmp_path, clk, rotate_bytes=1, fs=fs)
    _rx(w, b"before")
    current_handle = w._wal_fp
    fs.fail_identity_query = True

    record = _rx(w, b"after")

    rows = _lines(w.wal_path)
    assert w._wal_fp is current_handle
    assert [row["seq"] for row in rows] == [1, 2]
    assert all(row["dir"] != "META" for row in rows)
    assert not list(Path(w.wal_path).parent.glob("raw.wal.ndjson.*"))
    assert record["loss_flag"] is False and w.unhealthy is False


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
def test_recreate_retry_schedule_checks_each_gap_and_caps_at_five_seconds(tmp_path):
    from sw_core.constants import WAL_RECREATE_BACKOFF_S, WAL_RECREATE_RETRY_INTERVALS_S

    WalFs = _load_posix_fs()

    class Broken(WalFs):
        broken = False
        attempts = 0

        def open_append(self, path):
            if self.broken and path.endswith("raw.wal.ndjson"):
                self.attempts += 1
                raise PermissionError(13, "Access is denied")
            return super().open_append(path)

    retry_gaps = [
        *WAL_RECREATE_BACKOFF_S,
        *WAL_RECREATE_RETRY_INTERVALS_S,
        WAL_RECREATE_RETRY_INTERVALS_S[-1],
    ]
    assert retry_gaps == [0.01, 0.05, 0.2, 1.0, 2.0, 5.0, 5.0]

    fs = Broken()
    clk = Clock()
    w = _w(tmp_path, clk, fs=fs)
    _rx(w, b"before unlink")
    os.unlink(w.wal_path)
    fs.broken = True
    clk.advance(0.06)
    _rx(w, b"first failed recreate")
    assert fs.attempts == 1

    for attempt, gap in enumerate(retry_gaps, start=2):
        clk.advance(gap - 0.001)
        _rx(w, b"before retry deadline")
        assert fs.attempts == attempt - 1
        clk.advance(0.0011)
        _rx(w, b"at retry deadline")
        assert fs.attempts == attempt


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


@pytest.mark.core_portable
def test_flash_gate_precedes_wal_unhealthy_rejection(tmp_path):
    from stage0_fakes import FakePort, make_bridge

    class UnhealthyWal:
        unhealthy = True

        def append(self, **kwargs):
            raise AssertionError("flash gate 已丟棄的資料不得寫入 WAL")

    port = FakePort()
    bridge, _ = make_bridge(tmp_path, port=port, wal=UnhealthyWal())
    bridge.set_flash_mode(True)

    assert bridge.send_bytes(b"probe\n", source="system:self_test") is None
    assert port.writes == []


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
