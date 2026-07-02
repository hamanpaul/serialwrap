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


def _mk_manager(**kw):
    from sw_core.session_manager import SessionManager

    return SessionManager(
        [],
        MagicMock(),
        on_ready=lambda sid: None,
        on_detached=lambda sid: None,
        **kw,
    )


def test_injected_state_path_wins(tmp_path, monkeypatch):
    """注入 state_path 後，建構期的 _save_state 寫注入路徑、不碰模組層 STATE_PATH。"""
    import sw_core.session_manager as sm

    module_level = tmp_path / "module" / "state.json"
    monkeypatch.setattr(sm, "STATE_PATH", str(module_level))
    injected = tmp_path / "injected" / "state.json"
    _mk_manager(state_path=str(injected))
    assert injected.exists()
    assert not module_level.exists()


def test_default_falls_back_to_module_global(tmp_path, monkeypatch):
    """未注入時 fallback 讀模組層全域（於建構時）——既有 19 檔 setattr 隔離手法必須持續有效。"""
    import sw_core.session_manager as sm

    module_level = tmp_path / "module" / "state.json"
    monkeypatch.setattr(sm, "STATE_PATH", str(module_level))
    _mk_manager()
    assert module_level.exists()


def test_corrupt_backup_follows_injected_path(tmp_path, monkeypatch):
    """JSON 損毀備份（.corrupt）須跟隨注入路徑。"""
    import sw_core.session_manager as sm

    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "module" / "state.json"))
    injected = tmp_path / "injected" / "state.json"
    injected.parent.mkdir(parents=True)
    injected.write_text("{not-json", encoding="utf-8")
    _mk_manager(state_path=str(injected))
    assert (tmp_path / "injected" / "state.json.corrupt").exists()


def test_service_passthrough(tmp_path, monkeypatch):
    """SerialwrapService(state_path=...) 透傳到內部 SessionManager。"""
    import sw_core.service as svc_mod
    import sw_core.session_manager as sm
    import sw_core.wal as wal_mod

    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "module-state.json"))
    monkeypatch.setattr(wal_mod, "WAL_DIR", str(tmp_path / "wal"))
    monkeypatch.setattr(svc_mod, "EVENTS_DIR", str(tmp_path / "ev"))
    monkeypatch.setattr(svc_mod, "EVENTS_RUNTIME_DIR", str(tmp_path / "ev-rt"))
    monkeypatch.setattr(svc_mod, "EVENTS_LOG_PATH", str(tmp_path / "ev-rt" / "events.ndjson"))
    injected = tmp_path / "svc" / "state.json"
    svc_mod.SerialwrapService([], state_path=str(injected))
    assert injected.exists()
    assert not (tmp_path / "module-state.json").exists()
