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


def test_pyproject_declares_entrypoints_and_deps():
    import tomllib, pathlib
    data = tomllib.loads((pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts["serialwrap"] == "sw_core.cli:main"
    assert scripts["serialwrapd"] == "sw_core.daemon:main"
    assert any(d.lower().startswith("pyyaml") for d in data["project"]["dependencies"])
    assert data["project"]["requires-python"] == ">=3.10"


def test_reliability_dev_only_dist_contract():
    import importlib
    import pathlib
    import sys
    import tomllib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    data = tomllib.loads((repo_root / "reliability" / "pyproject.toml").read_text(encoding="utf-8"))

    build_system = data["build-system"]
    assert build_system["requires"] == ["hatchling"]
    assert build_system["build-backend"] == "hatchling.build"

    project = data["project"]
    assert project["name"] == "serialwrap-reliability"
    assert project["version"] == "0.1.0"
    assert project["requires-python"] == ">=3.10"
    assert project["dependencies"] == ["testpilot-core>=0.3.4,<1.0"]
    assert project["entry-points"]["testpilot.plugins"] == {
        "serialwrap_reliability": "serialwrap_reliability.plugin:Plugin"
    }
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "serialwrap_reliability"
    ]
    main_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    main_packages = main_data["tool"]["setuptools"]["packages"]
    assert "serialwrap_reliability" not in main_packages
    assert all("reliability" not in name for name in main_packages)
    assert not any("find" in str(key) for key in main_data["tool"]["setuptools"])

    sys.path.insert(0, str(repo_root / "reliability"))
    try:
        pkg = importlib.import_module("serialwrap_reliability")
        assert pkg.__version__ == "0.1.0"
        assert pkg.PLUGIN_API_VERSION == "1.1"
        plugin_py = repo_root / "reliability" / "serialwrap_reliability" / "plugin.py"
        assert plugin_py.is_file()
    finally:
        sys.path.remove(str(repo_root / "reliability"))
        sys.modules.pop("serialwrap_reliability", None)
