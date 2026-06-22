"""Task 7: auto-spawn gate 測試。

驗證 should_auto_spawn() 邏輯與 supervision-mode CLI 指令。
"""
from __future__ import annotations


def test_should_auto_spawn_false_in_systemd_mode(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.cli import should_auto_spawn
    rc = RuntimeConfig(tmp_path / "config.yaml"); rc.set_mode("systemd-user")
    assert should_auto_spawn(rc) is False


def test_should_auto_spawn_false_in_systemd_system_mode(tmp_path):
    """systemd-system 也必須擋下 auto-spawn（不可只比對 systemd-user 字串）。"""
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.cli import should_auto_spawn
    rc = RuntimeConfig(tmp_path / "config.yaml"); rc.set_mode("systemd-system")
    assert should_auto_spawn(rc) is False


def test_should_auto_spawn_true_in_on_demand_mode(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.cli import should_auto_spawn
    rc = RuntimeConfig(tmp_path / "config.yaml"); rc.set_mode("on-demand")
    assert should_auto_spawn(rc) is True


def test_should_auto_spawn_true_when_unset(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.cli import should_auto_spawn
    rc = RuntimeConfig(tmp_path / "config.yaml")  # no mode set
    assert should_auto_spawn(rc) is True


def test_supervision_mode_command_prints_effective_mode(tmp_path, capsys, monkeypatch):
    # 指向 tmp config，驗證 CLI 印出有效模式（未設→on-demand）
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(tmp_path))
    import importlib, sw_core.constants, sw_core.cli
    importlib.reload(sw_core.constants); importlib.reload(sw_core.cli)
    rc_path = tmp_path / "config.yaml"
    from sw_core.runtime_config import RuntimeConfig
    # 未設定 → 印 on-demand
    assert sw_core.cli.main(["supervision-mode"]) == 0
    assert capsys.readouterr().out.strip() == "on-demand"
    # 設成 systemd-user → 印 systemd-user
    RuntimeConfig(rc_path).set_mode("systemd-user")
    importlib.reload(sw_core.cli)
    assert sw_core.cli.main(["supervision-mode"]) == 0
    assert capsys.readouterr().out.strip() == "systemd-user"


def teardown_module(module):
    import importlib, sw_core.constants, sw_core.cli
    importlib.reload(sw_core.constants); importlib.reload(sw_core.cli)
