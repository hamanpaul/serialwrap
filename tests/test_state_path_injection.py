"""#120：state/WAL 路徑注入與 def-time 凍結消除的單元測試。"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_walwriter_default_resolves_at_construction(tmp_path, monkeypatch):
    """WalWriter() 的 default wal_dir 須於建構時讀模組層 WAL_DIR，而非 def-time 凍結值。"""
    import sw_core.wal as wal_mod

    monkeypatch.setattr(wal_mod, "WAL_DIR", str(tmp_path / "patched-wal"))
    w = wal_mod.WalWriter()
    assert w.wal_path == str(tmp_path / "patched-wal" / "raw.wal.ndjson")
    assert (tmp_path / "patched-wal").is_dir()
