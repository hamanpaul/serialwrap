# tests/test_stage0_wal_rotate.py
"""P1 Task T04（BC-01c rotate／reset 於常駐 handle 下同步與 DF-03 歸檔後綴）紅燈測試。

依 02-stage0-python-bc.md Global Constraints：
本模組不在頂層 import 本 Phase 才新增的模組（如 `sw_core.wal_fs`），
一律於測試執行時以 `_load()` 延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。

marker 規則（P0 T14）：
資源 marker `memory_only` 放模組層；平台 marker 放函式層（同檔混合 core_portable 與 windows_native）。
"""
from __future__ import annotations

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


@pytest.mark.core_portable
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
    with open(wal_file, "rb") as reader:
        rec = w.append(com="COM0", direction="RX", source="device", payload=b"block")
        assert rec.get("rotation_failed") is True
        assert rec["seq"] == 6
    rec2 = w.append(com="COM0", direction="RX", source="device", payload=b"unblock")
    assert "rotation_failed" not in rec2
    assert rec2["seq"] == 7
    names = sorted(n for n in os.listdir(tmp_path) if n.startswith("raw.wal.ndjson."))
    assert len(names) >= 1
