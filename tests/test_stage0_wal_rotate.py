# tests/test_stage0_wal_rotate.py
"""P1 Task T04（BC-01c rotate／reset 於常駐 handle 下同步與 DF-03 歸檔後綴）紅燈測試。

依 02-stage0-python-bc.md Global Constraints：
本模組不在頂層 import 本 Phase 才新增的模組（如 `sw_core.wal_fs`），
一律於測試執行時以 `_load()` 延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。

marker 規則（P0 T14）：
資源 marker `memory_only` 放模組層；平台 marker 放函式層（同檔混合 core_portable 與 windows_native）。
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import shutil
from typing import Any

import pytest
import sw_core.wal as wal_mod
from sw_core.wal import WalWriter

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


def _make_ops_fs_cls():
    """延遲取得 PosixWalFs 並建立記錄操作的 OpsFs 子類別。"""
    (PosixWalFs,) = _load("sw_core.wal_fs", "PosixWalFs")

    class OpsFs(PosixWalFs):
        def __init__(self) -> None:
            super().__init__()
            self.ops: list[tuple[str, str]] = []
            self.rename_error: BaseException | None = None

        def open_append(self, path: str):
            self.ops.append(("open", os.path.basename(path)))
            inner = super().open_append(path)
            fs = self

            class W:
                def write_all_flush(self, d: bytes) -> None:
                    inner.write_all_flush(d)

                def sync(self) -> None:
                    fs.ops.append(("sync", os.path.basename(path)))
                    inner.sync()

                def identity(self):
                    return inner.identity()

                def unlinked(self) -> bool:
                    return inner.unlinked()

                def close(self) -> None:
                    fs.ops.append(("close", os.path.basename(path)))
                    inner.close()

            return W()

        def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str:
            if self.rename_error is not None:
                raise self.rename_error
            dst = super().rename_noreplace_or_suffix(src, base_dst)
            self.ops.append(("rename", os.path.basename(dst)))
            return dst

    return OpsFs


def _force_unsupported_atomic_rename(monkeypatch: pytest.MonkeyPatch, err_no: int) -> None:
    """讓 POSIX 改名 syscall 回報指定錯誤，實際驗證相容降級路徑。"""
    wal_fs_mod = importlib.import_module("sw_core.wal_fs")

    class FailedRename:
        argtypes: Any = None
        restype: Any = None

        def __call__(self, *_args: Any) -> int:
            return -1

    class FakeLibc:
        renameat2 = FailedRename()
        renamex_np = FailedRename()

    monkeypatch.setattr(wal_fs_mod.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(wal_fs_mod.ctypes, "get_errno", lambda: err_no)


@pytest.fixture
def fixed_ts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wal_mod.time, "strftime", lambda fmt, t=None: "20261015-030617")


def _fill(w: WalWriter, n: int) -> None:
    for _ in range(n):
        w.append(com="COM0", direction="RX", source="device", payload=b"y" * 40)


@pytest.mark.core_portable
def test_rotate_syncs_closes_then_renames_and_reopens(tmp_path, fixed_ts):
    # 規格 §8.2 順序：flush／sync → 關閉自身 handle → rename → 重開
    OpsFs = _make_ops_fs_cls()
    fs = OpsFs()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs)
    _fill(w, 10)
    wal_ops = [op for op in fs.ops if op[1].startswith("raw.wal.ndjson")]
    i = wal_ops.index(("sync", "raw.wal.ndjson"))
    assert wal_ops[i + 1] == ("close", "raw.wal.ndjson")
    assert wal_ops[i + 2][0] == "rename"
    assert wal_ops[i + 3] == ("open", "raw.wal.ndjson")


@pytest.mark.core_portable
def test_same_second_rotations_get_numeric_suffix(tmp_path, fixed_ts):
    OpsFs = _make_ops_fs_cls()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=OpsFs())
    _fill(w, 40)
    names = sorted(n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson."))
    assert "raw.wal.ndjson.20261015-030617" in names
    assert "raw.wal.ndjson.20261015-030617.1" in names
    seqs: list[int] = []
    for n in names + ["raw.wal.ndjson"]:
        seqs += [json.loads(l)["seq"] for l in open(tmp_path / n, encoding="utf-8") if l.strip()]
    assert sorted(seqs) == list(range(1, 41))  # 無覆蓋、無遺失


@pytest.mark.posix_only
def test_rotate_falls_back_when_atomic_rename_is_unsupported(tmp_path, fixed_ts, monkeypatch):
    _force_unsupported_atomic_rename(monkeypatch, errno.EINVAL)
    OpsFs = _make_ops_fs_cls()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=OpsFs())
    records = [
        w.append(com="COM0", direction="RX", source="device", payload=b"y" * 40)
        for _ in range(12)
    ]

    names = sorted(n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson."))
    assert "raw.wal.ndjson.20261015-030617" in names
    assert "raw.wal.ndjson.20261015-030617.1" in names
    assert all("rotation_failed" not in rec for rec in records)


@pytest.mark.posix_only
def test_reset_reports_suffix_when_atomic_rename_is_unsupported(tmp_path, fixed_ts, monkeypatch):
    _force_unsupported_atomic_rename(monkeypatch, errno.EINVAL)
    OpsFs = _make_ops_fs_cls()
    w = WalWriter(wal_dir=str(tmp_path), fs=OpsFs())
    _fill(w, 2)
    archive = tmp_path / "raw.wal.ndjson.20261015-030617"
    archive.write_text("old\n", encoding="utf-8")

    result = w.reset()

    assert result == {"ok": True, "previous_seq": 2, "rotated_suffix": "20261015-030617.1"}
    assert archive.read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / "raw.wal.ndjson.20261015-030617.1").exists()


@pytest.mark.posix_only
def test_rename_falls_back_when_hardlinks_are_unsupported(tmp_path, monkeypatch):
    _force_unsupported_atomic_rename(monkeypatch, errno.ENOSYS)
    (PosixWalFs,) = _load("sw_core.wal_fs", "PosixWalFs")
    source = tmp_path / "raw.wal.ndjson"
    base = tmp_path / "raw.wal.ndjson.20261015-030617"
    source.write_text("new archive\n", encoding="utf-8")
    original_exists = os.path.exists
    created = False

    def create_collision_after_probe(path):
        nonlocal created
        if os.fspath(path) == str(base) and not created:
            created = True
            base.write_text("external archive\n", encoding="utf-8")
            return False
        return original_exists(path)

    def unsupported_link(src, dst):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported", dst)

    monkeypatch.setattr(os.path, "exists", create_collision_after_probe)
    monkeypatch.setattr(os, "link", unsupported_link)

    archived = PosixWalFs().rename_noreplace_or_suffix(str(source), str(base))

    assert archived == f"{base}.1"
    assert base.read_text(encoding="utf-8") == "external archive\n"
    assert (tmp_path / "raw.wal.ndjson.20261015-030617.1").read_text(encoding="utf-8") == "new archive\n"


@pytest.mark.core_portable
def test_archive_created_after_existence_check_is_not_overwritten(tmp_path, monkeypatch):
    (PosixWalFs,) = _load("sw_core.wal_fs", "PosixWalFs")
    source = tmp_path / "raw.wal.ndjson"
    base = tmp_path / "raw.wal.ndjson.20261015-030617"
    source.write_text("new archive\n", encoding="utf-8")
    real_exists = os.path.exists
    injected = False

    def create_collision_after_probe(path):
        nonlocal injected
        if os.fspath(path) == str(base) and not injected:
            injected = True
            base.write_text("external archive\n", encoding="utf-8")
            return False
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", create_collision_after_probe)
    archived = PosixWalFs().rename_noreplace_or_suffix(str(source), str(base))

    assert archived == f"{base}.1"
    assert base.read_text(encoding="utf-8") == "external archive\n"
    assert (tmp_path / "raw.wal.ndjson.20261015-030617.1").read_text(encoding="utf-8") == "new archive\n"


@pytest.mark.core_portable
def test_reset_reports_actual_suffix_on_collision(tmp_path, fixed_ts):
    OpsFs = _make_ops_fs_cls()
    w = WalWriter(wal_dir=str(tmp_path), fs=OpsFs())
    _fill(w, 2)
    (tmp_path / "raw.wal.ndjson.20261015-030617").write_text("old\n", encoding="utf-8")
    r = w.reset()
    assert r == {"ok": True, "previous_seq": 2, "rotated_suffix": "20261015-030617.1"}
    assert (tmp_path / "raw.wal.ndjson.20261015-030617").read_text(encoding="utf-8") == "old\n"
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"z")
    assert rec["seq"] == 1


@pytest.mark.core_portable
def test_rotate_rename_failure_reopens_same_path_and_marks_record(tmp_path, fixed_ts):
    OpsFs = _make_ops_fs_cls()
    fs = OpsFs()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=100, fs=fs)
    _fill(w, 3)
    before = open(tmp_path / "raw.wal.ndjson", "rb").read()
    n = len(fs.ops)
    fs.rename_error = PermissionError(13, "Permission denied")
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"q")
    assert rec.get("rotation_failed") is True
    wal_ops = [op for op in fs.ops[n:] if op[1] == "raw.wal.ndjson"]
    assert wal_ops[:3] == [("sync", "raw.wal.ndjson"), ("close", "raw.wal.ndjson"), ("open", "raw.wal.ndjson")]
    after = open(tmp_path / "raw.wal.ndjson", "rb").read()
    assert after.startswith(before) and json.loads(after.splitlines()[-1])["seq"] == rec["seq"]  # 同檔繼續附加
    fs.rename_error = None
    rec2 = w.append(com="COM0", direction="RX", source="device", payload=b"r")
    assert "rotation_failed" not in rec2


@pytest.mark.core_portable
def test_rotate_threshold_is_ge_and_checked_before_write(tmp_path, fixed_ts):
    # rotate_bytes=R，檔案大小恰為 R 時下一筆先 rotate；大小 R-1 時不 rotate（WAL-D7）
    OpsFs = _make_ops_fs_cls()
    fs = OpsFs()
    R = 300
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=R, fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"init")
    wal_file = tmp_path / "raw.wal.ndjson"

    # 檔案大小恰為 R - 1：寫入前不 rotate
    with open(wal_file, "wb") as f:
        f.write(b"x" * (R - 1))
    assert os.path.getsize(wal_file) == R - 1

    fs.ops.clear()
    rec1 = w.append(com="COM0", direction="RX", source="device", payload=b"a")
    assert not any(op[0] == "rename" for op in fs.ops)
    assert not any(n.startswith("raw.wal.ndjson.") for n in os.listdir(tmp_path))

    # 檔案大小恰為 R：寫入前先 rotate
    with open(wal_file, "wb") as f:
        f.write(b"x" * R)
    assert os.path.getsize(wal_file) == R

    fs.ops.clear()
    rec2 = w.append(com="COM0", direction="RX", source="device", payload=b"b")
    assert any(op[0] == "rename" for op in fs.ops)
    archives = [n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson.")]
    assert "raw.wal.ndjson.20261015-030617" in archives


@pytest.mark.core_portable
def test_wal_and_mirror_rotate_independently(tmp_path, fixed_ts):
    # mirror 小於門檻時只產生 WAL 歸檔
    OpsFs = _make_ops_fs_cls()
    fs = OpsFs()
    # NDJSON 格式每筆包含 json 欄位（約 200 bytes），而 mirror 只有 payload bytes
    # 設定門檻使 WAL 達標而 mirror 未達標
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=250, fs=fs)
    w.append(com="COM0", direction="RX", source="device", payload=b"m10bytes..")
    w.append(com="COM0", direction="RX", source="device", payload=b"m10bytes..")
    # 觸發寫入，此時 WAL size >= 250，但 mirror size < 250
    w.append(com="COM0", direction="RX", source="device", payload=b"m10bytes..")

    wal_archives = [n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson.")]
    mirror_archives = [n for n in os.listdir(tmp_path) if n.startswith("raw.mirror.log.")]
    assert len(wal_archives) >= 1
    assert len(mirror_archives) == 0


@pytest.mark.core_portable
def test_reset_without_files_returns_ts_suffix(tmp_path, fixed_ts):
    # 無檔案時 rotated_suffix == "20261015-030617" 且 ok is True
    OpsFs = _make_ops_fs_cls()
    w = WalWriter(wal_dir=str(tmp_path), fs=OpsFs())
    r = w.reset()
    assert r == {"ok": True, "previous_seq": 0, "rotated_suffix": "20261015-030617"}


@pytest.mark.posix_only
def test_reset_directory_missing_raises_and_keeps_seq(tmp_path, fixed_ts):
    # rmtree 後 reset() 擲 OSError、current_seq 不變（盤點 04 §1.9）
    OpsFs = _make_ops_fs_cls()
    w = WalWriter(wal_dir=str(tmp_path), fs=OpsFs())
    _fill(w, 3)
    assert w.current_seq == 3
    shutil.rmtree(tmp_path)
    with pytest.raises(OSError):
        w.reset()
    assert w.current_seq == 3


@pytest.mark.windows_native
def test_native_rotate_with_open_handle_succeeds(tmp_path, fixed_ts):
    # Windows 上常駐 handle 存在時 rotate 產生歸檔、新檔 seq 連續
    (WindowsWalFs,) = _load("sw_core.wal_fs", "WindowsWalFs")
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=WindowsWalFs())
    _fill(w, 10)
    names = sorted(n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson."))
    assert len(names) >= 1
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"cont")
    assert rec["seq"] == 11


@pytest.mark.windows_native
def test_native_rotate_blocked_by_reader_degrades(tmp_path, fixed_ts):
    # 另一 handle 以 open(p,"rb") 持有時 record 帶 rotation_failed: true、重開原路徑後寫入持續、無遺失 seq；reader 關閉後下一筆 rotate 成功
    (WindowsWalFs,) = _load("sw_core.wal_fs", "WindowsWalFs")
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=WindowsWalFs())
    _fill(w, 5)
    wal_file = tmp_path / "raw.wal.ndjson"
    before_archives = {
        n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson.")
    }
    with open(wal_file, "rb") as reader:
        rec = w.append(com="COM0", direction="RX", source="device", payload=b"block")
        assert rec.get("rotation_failed") is True
        assert rec["seq"] == 6
    rec2 = w.append(com="COM0", direction="RX", source="device", payload=b"unblock")
    # OD-19 oracle 變更：reader 關閉後的首筆仍在退避窗內（10 ms）時不嘗試、不標示；
    # 輪替須於退避到期後以 append 或 tick() 非阻塞地「最終完成」，故改為有界等待。
    assert "rotation_failed" not in rec2
    assert rec2["seq"] == 7
    import time as _time

    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if {n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson.")} - before_archives:
            break
        _time.sleep(0.02)
        w.tick()
    after_archives = {
        n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson.")
    }
    assert len(after_archives) == len(before_archives) + 1
    assert len(after_archives - before_archives) == 1
    assert w.health()["deferred_delete_pending"] == 0


# ---------------------------------------------------------------------------
# OD-19（2026-10-23）：rotate 遇共用違規／存取拒絕的有界退避、延遲佇列與干擾計數。
# 防毒等外部程式暫時佔用檔案屬干擾而非資料錯誤：寫入不得阻塞、不得遺失紀錄，
# 輪替須最終完成，且干擾次數須以 health() 快照可觀測。
# ---------------------------------------------------------------------------


class _Clock:
    """可手動推進的注入時鐘（與 test_stage0_wal_unlink.Clock 同形，避免跨檔匯入）。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _sharing_fs(failures: int, *, err_no: int = errno.EACCES, winerror: int | None = None):
    """POSIX 替身：前 ``failures`` 次 rename 以指定的共用違規形態失敗（注入，不依賴防毒）。"""
    (PosixWalFs,) = _load("sw_core.wal_fs", "PosixWalFs")

    class SharingFs(PosixWalFs):
        def __init__(self) -> None:
            super().__init__()
            self.remaining = failures
            self.rename_attempts = 0

        def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str:
            self.rename_attempts += 1
            if self.remaining > 0:
                self.remaining -= 1
                exc = PermissionError(err_no, "injected sharing violation")
                if winerror is not None:
                    exc.winerror = winerror  # Windows 的 ERROR_SHARING_VIOLATION＝32
                raise exc
            return super().rename_noreplace_or_suffix(src, base_dst)

    return SharingFs()


def _append_rx(w: WalWriter) -> dict:
    # 空 payload：只讓現行 WAL 越過門檻，mirror 不成長，rename 嘗試次數只反映 WAL 路徑。
    return w.append(com="COM0", direction="RX", source="device", payload=b"")


@pytest.mark.core_portable
def test_od19_sharing_violation_retries_with_bounded_backoff_then_completes(
    tmp_path, fixed_ts, monkeypatch
):
    import time as _time

    monkeypatch.setattr(
        _time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("寫入路徑不得睡眠"))
    )
    fs = _sharing_fs(2, winerror=32)
    clk = _Clock()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs, clock=clk)

    _append_rx(w)  # 先寫入一筆，使現行檔越過門檻
    first = _append_rx(w)  # 首次輪替嘗試失敗：本筆標示 rotation_failed
    assert first.get("rotation_failed") is True
    assert fs.rename_attempts == 1
    assert w.health()["rotate_retry"] == 1 and w.health()["deferred_delete_pending"] == 0

    _append_rx(w)  # 退避未到期：不嘗試，續寫既有檔
    assert fs.rename_attempts == 1

    clk.advance(0.011)  # 第一段退避 10 ms 到期 → 第二次嘗試仍失敗
    _append_rx(w)
    assert fs.rename_attempts == 2

    clk.advance(0.051)  # 第二段退避 50 ms 到期 → 第三次嘗試成功（最終完成）
    _append_rx(w)
    assert fs.rename_attempts == 3
    assert any(n.startswith("raw.wal.ndjson.") for n in os.listdir(tmp_path))
    h = w.health()
    assert h["rotate_retry"] == 2 and h["rotate_deferred"] == 0 and h["deferred_delete_pending"] == 0


@pytest.mark.core_portable
def test_od19_exhausted_immediate_retries_enter_deferred_queue_and_complete(tmp_path, fixed_ts):
    fs = _sharing_fs(4, winerror=5)  # 初次＋3 次立即退避皆失敗 → 轉入延遲佇列
    clk = _Clock()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs, clock=clk)
    _append_rx(w)  # 先寫入一筆，使現行檔越過門檻
    _append_rx(w)  # 第 1 次嘗試失敗
    for gap in (0.011, 0.051, 0.201):  # 第 2、3、4 次嘗試（第 4 次失敗後即轉入延遲）
        clk.advance(gap)
        _append_rx(w)
    assert fs.rename_attempts == 4
    h = w.health()
    assert h["rotate_deferred"] == 1 and h["deferred_delete_pending"] == 1 and h["rotate_retry"] == 4

    clk.advance(0.5)  # 延遲間隔（1 s）未到：不嘗試
    _append_rx(w)
    assert fs.rename_attempts == 4

    clk.advance(0.51)  # 延遲間隔到期：第 5 次嘗試成功，佇列清空
    _append_rx(w)
    assert fs.rename_attempts == 5
    h = w.health()
    assert h["deferred_delete_pending"] == 0 and h["rotate_deferred"] == 1
    assert any(n.startswith("raw.wal.ndjson.") for n in os.listdir(tmp_path))


@pytest.mark.core_portable
def test_od19_deferred_rotate_failure_warns_again_at_limited_interval(tmp_path, fixed_ts, caplog):
    import logging

    fs = _sharing_fs(10, winerror=5)
    clk = _Clock()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs, clock=clk)
    with caplog.at_level(logging.WARNING, logger="serialwrap"):
        _append_rx(w)
        _append_rx(w)  # 首次失敗
        for gap in (0.011, 0.051, 0.201):
            clk.advance(gap)
            _append_rx(w)
        def rotate_warnings() -> list[logging.LogRecord]:
            return [rec for rec in caplog.records if "WAL 輪替失敗" in rec.getMessage()]

        assert len(rotate_warnings()) == 2  # 首次失敗、轉入延遲佇列
        clk.advance(1.01)
        w.tick()
        assert len(rotate_warnings()) == 2  # 延遲重試未到限頻間隔
        clk.advance(59.0)
        w.tick()
        assert len(rotate_warnings()) == 3  # 長時間持續失敗仍有受限頻率的訊號


@pytest.mark.core_portable
def test_od19_non_transient_rotate_error_keeps_legacy_degrade(tmp_path, fixed_ts):
    fs = _sharing_fs(99, err_no=errno.EIO)  # EIO 非共用違規：不排程重試，維持既有 degrade
    clk = _Clock()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs, clock=clk)
    _append_rx(w)  # 先寫入一筆，使現行檔越過門檻
    rec = _append_rx(w)  # 第 1 次嘗試失敗
    assert rec.get("rotation_failed") is True
    _append_rx(w)  # 第 2 次嘗試：非暫時性錯誤不排程退避，每筆仍嘗試（舊語意）
    assert fs.rename_attempts == 2
    h = w.health()
    assert h["rotate_retry"] == 0 and h["deferred_delete_pending"] == 0


@pytest.mark.core_portable
def test_od19_tick_is_non_blocking_and_services_due_deferred_rotation(tmp_path, fixed_ts):
    fs = _sharing_fs(4)
    clk = _Clock()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, fs=fs, clock=clk)
    _append_rx(w)  # 先寫入一筆，使現行檔越過門檻
    _append_rx(w)  # 第 1 次嘗試失敗
    for gap in (0.011, 0.051, 0.201):
        clk.advance(gap)
        _append_rx(w)
    assert w.health()["deferred_delete_pending"] == 1

    w._lock.acquire()  # 寫鎖被佔用時 tick 必須立即返回，不得等待
    try:
        assert w.tick() is False
    finally:
        w._lock.release()
    assert fs.rename_attempts == 4

    clk.advance(1.01)
    w.tick()  # 延遲佇列到期：tick 可獨立完成輪替，不需等待下一筆寫入
    assert fs.rename_attempts == 5
    assert w.health()["deferred_delete_pending"] == 0


@pytest.mark.posix_only
def test_od19_unlink_fallback_permission_error_is_retried(tmp_path, fixed_ts, monkeypatch):
    # POSIX 無 renameat2 時走 link＋unlink 降級；unlink 來源名稱遇 EACCES（防毒佔用）即屬干擾。
    _force_unsupported_atomic_rename(monkeypatch, errno.EINVAL)
    real_unlink = os.unlink
    state = {"failed": False}

    def flaky_unlink(path, *args, **kwargs):
        if not state["failed"] and str(path).endswith("raw.wal.ndjson"):
            state["failed"] = True
            raise PermissionError(errno.EACCES, "injected: source name held by scanner")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    clk = _Clock()
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=200, clock=clk)
    _append_rx(w)  # 先寫入一筆，使現行檔越過門檻
    first = _append_rx(w)
    assert first.get("rotation_failed") is True and state["failed"] is True
    assert w.health()["rotate_retry"] == 1
    clk.advance(0.011)
    _append_rx(w)
    assert any(n.startswith("raw.wal.ndjson.") for n in os.listdir(tmp_path))
    assert w.health()["deferred_delete_pending"] == 0


@pytest.mark.core_portable
def test_od19_health_exposes_interference_counters_on_clean_path(tmp_path):
    w = WalWriter(wal_dir=str(tmp_path))
    h = w.health()
    assert h["rotate_retry"] == 0
    assert h["rotate_deferred"] == 0
    assert h["deferred_delete_pending"] == 0
    assert h["healthy"] is True
