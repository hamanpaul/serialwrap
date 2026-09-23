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


def test_setup_preserves_existing_remote_tcp_socket_path(tmp_path, monkeypatch, capsys):
    """#222：既有 config.yaml 的 socket_path 若已是遠端（非 loopback）tcp:// endpoint，
    重跑 setup（未顯式帶 --socket/--endpoint）不得把它打回本機預設路徑——否則
    reverse SSH tunnel 拓樸下，每次 install.sh/setup 都會讓後續指令連到一個
    根本沒有 daemon 監聽的本機 socket（SOCKET_ERROR）。"""
    import yaml

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = _reload()

    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump({"supervision_mode": "on-demand", "socket_path": "tcp://203.0.113.5:48701"}),
        encoding="utf-8",
    )

    assert cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()

    from sw_core.runtime_config import RuntimeConfig

    rc = RuntimeConfig(str(cfg_dir / "config.yaml"))
    assert rc.socket_path() == "tcp://203.0.113.5:48701"


def test_setup_preserves_loopback_tcp_socket_path_from_reverse_tunnel(tmp_path, monkeypatch, capsys):
    """#222 迴歸重點：reverse SSH tunnel 的本質就是把遠端 daemon 映成本機
    loopback 位址（如 `tcp://127.0.0.1:48701`）——不能用「是否 loopback」
    排除這個最常見的真實案例，只要既有值是 tcp:// 就該保留。"""
    import yaml

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = _reload()

    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump({"supervision_mode": "on-demand", "socket_path": "tcp://127.0.0.1:48701"}),
        encoding="utf-8",
    )

    assert cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()

    from sw_core.runtime_config import RuntimeConfig

    rc = RuntimeConfig(str(cfg_dir / "config.yaml"))
    assert rc.socket_path() == "tcp://127.0.0.1:48701"


def test_setup_still_defaults_local_socket_when_no_existing_remote_endpoint(tmp_path, monkeypatch, capsys):
    """#222 反向案例：沒有既有遠端 socket_path 時，setup 仍照舊寫入本機預設路徑。"""
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = _reload()

    assert cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()

    from sw_core.runtime_config import RuntimeConfig

    rc = RuntimeConfig(str(cfg_dir / "config.yaml"))
    assert rc.socket_path() == cli.SOCKET_PATH


def test_setup_explicit_socket_skips_remote_preservation(tmp_path, monkeypatch, capsys):
    """#222：使用者顯式帶 --socket 時，跳過「保留既有遠端 socket_path」的例外邏輯，
    落回既有預設行為（effective_socket 本就不採用 args.socket 的值寫回 config，
    這是修復前既有行為，本次修復範圍不變更此點，只確保不誤觸保留邏輯）。"""
    import yaml

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = _reload()

    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump({"supervision_mode": "on-demand", "socket_path": "tcp://203.0.113.5:48701"}),
        encoding="utf-8",
    )

    explicit_socket = str(tmp_path / "explicit.sock")
    assert cli.main(["--socket", explicit_socket, "setup", "--on-demand"]) == 0
    capsys.readouterr()

    from sw_core.runtime_config import RuntimeConfig

    rc = RuntimeConfig(str(cfg_dir / "config.yaml"))
    assert rc.socket_path() == cli.SOCKET_PATH


def test_setup_prints_console_hint_for_serialwrap_minicom(tmp_path, monkeypatch, capsys):
    """#149：setup 完成後主動提示 human console 入口指令，避免 operator 誤敲未走
    broker 的裸 `minicom -D /dev/ttyUSBx`（two-reader）。"""
    import json

    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = _reload()

    assert cli.main(["setup", "--on-demand"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "console_hint" in payload
    assert "serialwrap-minicom" in payload["console_hint"]


def teardown_module(module):
    import sw_core.constants
    import sw_core.cli
    importlib.reload(sw_core.constants)
    importlib.reload(sw_core.cli)
