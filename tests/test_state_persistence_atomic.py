"""tests/test_state_persistence_atomic.py — #82 state.json 原子寫入 + 損毀備份。

驗證 _save_state 採 temp+fsync+os.replace 原子置換（崩潰/ENOSPC 中途失敗不破壞既有檔），
_load_state 對損毀檔保留備份並告警而非靜默全棄——避免 RELEASED 交接遺失致重啟 two-reader。
"""
import builtins
import json
import os

import pytest

import sw_core.session_manager as sm_mod
from sw_core.session_manager import SessionManager, StateLoadError
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


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


def _block_open_for(monkeypatch, target_path, exc):
    """讓對 target_path 的 open() 拋 exc，其餘路徑照常（用於模擬 state.json 讀取失敗）。"""
    real_open = builtins.open

    def _guard(path, *a, **k):
        if str(path) == str(target_path):
            raise exc
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _guard)


def test_load_state_ioerror_fail_closed_raises_and_preserves(tmp_path, monkeypatch):
    """讀取失敗（PermissionError 等，非 JSON 損毀）須 fail closed：拋 StateLoadError、保留原檔、不備份。"""
    sp = tmp_path / "state.json"
    monkeypatch.setattr(sm_mod, "STATE_PATH", str(sp))
    mgr = _mgr(tmp_path, monkeypatch)  # 先用無狀態建構成功
    payload = json.dumps({"aliases": {}, "bindings": {}, "released": {"prpl:COM0": {"by_id": "X"}}})
    sp.write_text(payload, encoding="utf-8")

    _block_open_for(monkeypatch, sp, PermissionError("EACCES"))
    with pytest.raises(StateLoadError):
        mgr._load_state()
    # 原檔完好、未被誤判損毀而備份/清空
    assert sp.exists()
    assert not (tmp_path / "state.json.corrupt").exists()


def test_init_with_unreadable_state_does_not_clobber_released(tmp_path, monkeypatch):
    """核心 two-reader 回歸：既有 state.json 讀不到時，建構 daemon 須拒絕啟動且絕不覆寫/丟失 RELEASED。"""
    sp = tmp_path / "state.json"
    monkeypatch.setattr(sm_mod, "STATE_PATH", str(sp))
    payload = json.dumps(
        {"aliases": {}, "bindings": {}, "released": {"prpl:COM0": {"by_id": "handed-off"}}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n"
    sp.write_text(payload, encoding="utf-8")

    _block_open_for(monkeypatch, sp, PermissionError("EACCES"))
    with pytest.raises(StateLoadError):
        SessionManager([], WalWriter(wal_dir=str(tmp_path)),
                       on_ready=lambda _s: None, on_detached=lambda _s: None)
    # __init__ 尾段的 _save_state() 不得被觸及——RELEASED 交接原封不動，無 .corrupt 殘留
    assert sp.read_text(encoding="utf-8") == payload
    assert not (tmp_path / "state.json.corrupt").exists()


def test_save_then_load_roundtrip_preserves_bindings(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._binding_overrides = {"prpl:COM0": "by-id-1", "prpl:COM1": "by-id-2"}
    mgr._save_state()
    mgr2 = _mgr(tmp_path, monkeypatch)  # 重新載入同一 STATE_PATH
    mgr2._load_state()
    assert mgr2._binding_overrides == {"prpl:COM0": "by-id-1", "prpl:COM1": "by-id-2"}


# ---------------------------------------------------------------------------
# P5-T15 特性測試（#81，Python 基線 oracle；Rust state_store 對應測試以此為準）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "exc_type", "msg"),
    [
        (1, TypeError, "'int' object is not iterable"),
        (None, TypeError, "'NoneType' object is not iterable"),
        ("ab", ValueError, "dictionary update sequence element #0 has length 1; 2 is required"),
        ([1], TypeError, "cannot convert dictionary update sequence element #0 to a sequence"),
        ([[[1], 2]], TypeError, "unhashable type: 'list'"),
    ],
    ids=["int", "none", "str", "list-int", "unhashable-key"],
)
def test_load_state_alias_row_not_dict_raises_uncaught(tmp_path, monkeypatch, row, exc_type, msg):
    """#12（B-06，KEEP-fail）：aliases 列無法 dict(v) → _load_state 拋未捕捉例外（非 StateLoadError），原檔不動。"""
    sp = tmp_path / "state.json"
    mgr = _mgr(tmp_path, monkeypatch)
    body = json.dumps({"aliases": {"x": row}})
    sp.write_text(body, encoding="utf-8")
    with pytest.raises(exc_type) as ei:
        mgr._load_state()
    assert not isinstance(ei.value, StateLoadError)
    assert str(ei.value) == msg
    assert sp.read_text(encoding="utf-8") == body
    assert not (tmp_path / "state.json.corrupt").exists()


@pytest.mark.parametrize("body", ["[1,2]", "1", '"x"', "null"], ids=["list", "int", "str", "null"])
def test_load_state_non_dict_json_not_backed_up(tmp_path, monkeypatch, body):
    """#95（B-04）：合法 JSON 但非 object → 不備份、不改原檔、狀態維持空。"""
    sp = tmp_path / "state.json"
    mgr = _mgr(tmp_path, monkeypatch)
    sp.write_text(body, encoding="utf-8")
    mgr._load_state()
    assert sp.read_text(encoding="utf-8") == body
    assert sorted(p.name for p in tmp_path.glob("state.json*")) == ["state.json"]
    assert mgr._aliases.dump() == {} and mgr._binding_overrides == {} and mgr._loaded_released == {}
