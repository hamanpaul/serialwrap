"""serialwrap setup CLI 接線測試（I-1 回歸：config writer 路徑須等於 reader 路徑）。

用 ``--on-demand`` 避免在測試機跑真實 systemctl（--user/--system 會驅動真 SystemEffects）。
"""
from __future__ import annotations

import importlib


def _reload():
    import sw_core.constants
    import sw_core.cli
    importlib.reload(sw_core.constants)
    importlib.reload(sw_core.cli)
    return sw_core.cli


def test_setup_writes_config_to_xdg_config_dir_readable_by_supervision_mode(tmp_path, monkeypatch, capsys):
    """自訂 SERIALWRAP_CONFIG_DIR 下，setup 寫的 config 必須被 supervision-mode 讀回（單一事實來源）。"""
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = _reload()

    assert cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()
    # SERIALWRAP_CONFIG_DIR 本身即 CONFIG_DIR；config 必須落在這裡（reader 端），而非寫死的 ~/.config
    assert (cfg_dir / "config.yaml").is_file()
    # 同一路徑被 supervision-mode 讀回
    assert cli.main(["supervision-mode"]) == 0
    assert capsys.readouterr().out.strip() == "on-demand"


def teardown_module(module):
    import sw_core.constants
    import sw_core.cli
    importlib.reload(sw_core.constants)
    importlib.reload(sw_core.cli)
