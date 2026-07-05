"""sw_core.setup_cmd.materialize_assets 測試。

測試策略：monkeypatch XDG 環境變數並傳入 home=tmp_path，
驗證 profiles/skill symlink/minicom wrappers 均正確物化。
"""

from __future__ import annotations


def test_materialize_copies_profiles_and_symlinks_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SERIALWRAP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SERIALWRAP_DATA_DIR", raising=False)
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)
    assert (tmp_path / "cfg" / "serialwrap" / "profiles" / "default.yaml").is_file()
    link = tmp_path / ".agents" / "skills" / "serialwrap"
    assert link.is_symlink()
    assert (link / "SKILL.md").is_file()  # symlink 指向已物化的 skill
    assert (tmp_path / ".local" / "bin" / "serialwrap-minicom").is_file()


def test_materialize_does_not_overwrite_existing_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SERIALWRAP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SERIALWRAP_DATA_DIR", raising=False)
    prof = tmp_path / "cfg" / "serialwrap" / "profiles" / "default.yaml"
    prof.parent.mkdir(parents=True)
    prof.write_text("MINE", encoding="utf-8")
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)            # 無 force → 不覆蓋
    assert prof.read_text(encoding="utf-8") == "MINE"
    materialize_assets(home=tmp_path, force=True)  # force → 覆蓋
    assert prof.read_text(encoding="utf-8") != "MINE"


def test_materialize_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)
    materialize_assets(home=tmp_path)  # 第二次不可炸（symlink 已存在要能覆蓋）
    assert (tmp_path / ".agents" / "skills" / "serialwrap").is_symlink()


def test_materialize_replaces_stale_real_directory_at_link(tmp_path, monkeypatch):
    """若 ~/.agents/skills/serialwrap 是真實目錄（非 symlink），須能取代而非丟 IsADirectoryError。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    stale = tmp_path / ".agents" / "skills" / "serialwrap"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("old", encoding="utf-8")
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)  # 不可炸
    assert stale.is_symlink()
    assert (stale / "SKILL.md").is_file()


def test_materialize_removes_legacy_serialwrap_mcp_symlink(tmp_path, monkeypatch):
    """新版 skill 已改名為 serialwrap；setup 須清掉舊 serialwrap-mcp symlink 避免雙載入。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    legacy_target = tmp_path / "custom-skills" / "serialwrap-mcp"
    legacy_target.mkdir(parents=True)
    (legacy_target / "SKILL.md").write_text("name: serialwrap-mcp\n", encoding="utf-8")
    legacy_link = tmp_path / ".agents" / "skills" / "serialwrap-mcp"
    legacy_link.parent.mkdir(parents=True)
    legacy_link.symlink_to(legacy_target)
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)
    assert not legacy_link.exists()
    assert (tmp_path / ".agents" / "skills" / "serialwrap").is_symlink()


def test_materialize_keeps_real_legacy_named_directory(tmp_path, monkeypatch):
    """清理只針對 symlink，避免刪除使用者手動放置的同名資料夾。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    legacy_dir = tmp_path / ".agents" / "skills" / "serialwrap-mcp"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "keep.txt").write_text("manual", encoding="utf-8")
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)
    assert legacy_dir.is_dir()
    assert (legacy_dir / "keep.txt").read_text(encoding="utf-8") == "manual"


def test_materialize_honors_serialwrap_config_dir_matching_daemon_profile_dir(tmp_path, monkeypatch):
    """I-B：只設 SERIALWRAP_CONFIG_DIR 時，setup 物化的 profiles 必須等於 daemon 讀的 PROFILE_DIR。"""
    import importlib
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SERIALWRAP_PROFILE_DIR", raising=False)
    from sw_core.setup_cmd import materialize_assets
    res = materialize_assets(home=tmp_path)
    assert (tmp_path / "cc" / "profiles" / "default.yaml").is_file()
    import sw_core.constants as c
    importlib.reload(c)
    assert res["profiles"] == c.PROFILE_DIR  # writer == reader（daemon 的 --profile-dir 預設）


def teardown_module(module):
    import importlib
    import sw_core.constants as c
    importlib.reload(c)  # 還原 constants（上面測試 reload 過），避免污染其他測試
