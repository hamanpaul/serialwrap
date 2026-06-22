"""sw_core.setup_cmd.materialize_assets 測試。

測試策略：monkeypatch XDG 環境變數並傳入 home=tmp_path，
驗證 profiles/skill symlink/minicom wrappers 均正確物化。
"""

from __future__ import annotations


def test_materialize_copies_profiles_and_symlinks_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
