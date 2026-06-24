"""tests/test_state_persistence_atomic.py — #82 state.json 原子寫入 + 損毀備份。

驗證 _save_state 採 temp+fsync+os.replace 原子置換（崩潰/ENOSPC 中途失敗不破壞既有檔），
_load_state 對損毀檔保留備份並告警而非靜默全棄——避免 RELEASED 交接遺失致重啟 two-reader。
"""
import json
import os

import pytest

import sw_core.session_manager as sm_mod
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter


def _mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(sm_mod, "STATE_PATH", str(tmp_path / "state.json"))
    return SessionManager([], WalWriter(wal_dir=str(tmp_path)),
                          on_ready=lambda _s: None, on_detached=lambda _s: None)


def test_save_state_atomic_valid_and_no_tmp_residue(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._binding_overrides = {"prpl:COM0": "by-id-abc"}
    mgr._save_state()
    obj = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert obj["bindings"]["prpl:COM0"] == "by-id-abc"
    assert not list(tmp_path.glob("state.json.tmp*"))  # 無殘留 temp


def test_save_state_failure_preserves_existing(tmp_path, monkeypatch):
    """寫入中途失敗（模擬 fsync ENOSPC）時，原 state.json 須完好、無殘留 temp。"""
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._binding_overrides = {"a:COM0": "good"}
    mgr._save_state()
    good = (tmp_path / "state.json").read_text(encoding="utf-8")

    def _boom(*_a, **_k):
        raise OSError("ENOSPC")

    monkeypatch.setattr(os, "fsync", _boom)
    mgr._binding_overrides = {"b:COM1": "bad"}
    with pytest.raises(OSError):
        mgr._save_state()
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == good  # 原檔未被截斷/覆寫
    assert not list(tmp_path.glob("state.json.tmp*"))                     # 半寫 temp 已清


def test_load_state_corrupt_backed_up_not_silently_dropped(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    sp = tmp_path / "state.json"
    sp.write_text("{ this is not valid json :::", encoding="utf-8")
    mgr._load_state()  # 不得拋例外
    assert not sp.exists()                                  # active 路徑已清出
    assert (tmp_path / "state.json.corrupt").exists()       # 損毀檔保留為證據（非靜默全棄）


def test_save_then_load_roundtrip_preserves_bindings(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._binding_overrides = {"prpl:COM0": "by-id-1", "prpl:COM1": "by-id-2"}
    mgr._save_state()
    mgr2 = _mgr(tmp_path, monkeypatch)  # 重新載入同一 STATE_PATH
    mgr2._load_state()
    assert mgr2._binding_overrides == {"prpl:COM0": "by-id-1", "prpl:COM1": "by-id-2"}
