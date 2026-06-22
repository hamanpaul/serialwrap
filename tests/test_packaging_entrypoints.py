import importlib


def test_daemon_main_importable_from_package():
    mod = importlib.import_module("sw_core.daemon")
    assert callable(mod.main)
    assert {"file.push", "file.pull"} <= set(mod.BLOCKING_RPC_METHODS)


def test_bundled_assets_readable_via_resources():
    from sw_core import assets
    names = assets.list_profile_files()
    assert any(n.endswith(".yaml") for n in names)
    tools = assets.list_tool_files()
    assert {"minicom_router.sh", "minicom-broker.sh", "minicom-raw.sh"} <= set(tools)


def test_copy_tree_materializes_profiles(tmp_path):
    """copy_tree 是 T10/T11 materialize 的基礎，需確認可把套件資產複製出來。"""
    from sw_core import assets
    dest = tmp_path / "profiles"
    assets.copy_tree("profiles", dest)
    copied = sorted(p.name for p in dest.iterdir() if p.is_file())
    assert copied == assets.list_profile_files()
    assert any(n.endswith(".yaml") for n in copied)


def test_copy_tree_handles_nested_skill_dir(tmp_path):
    """skill 子目錄含巢狀檔案（SKILL.md），copy_tree 需遞迴正確。"""
    from sw_core import assets
    dest = tmp_path / "skill"
    assets.copy_tree("skill", dest)
    assert (dest / "SKILL.md").is_file()
