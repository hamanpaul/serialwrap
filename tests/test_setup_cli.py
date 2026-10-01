"""setup 的隔離接線測試：真實 config/reconcile，RPC 與系統效果一律使用替身。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """不連任何真實 endpoint、不執行 systemctl 或 daemon stop；檔案只寫 tmp。"""
    from sw_core import cli, sysenv
    from sw_core.runtime_config import RuntimeConfig

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "posix")
    monkeypatch.setattr(cli, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(cli, "SOCKET_PATH", str(tmp_path / "run" / "serialwrap.sock"))
    fx = sysenv.FakeEffects(systemd=True)
    monkeypatch.setattr(sysenv, "SystemEffects", lambda: fx)
    # resolver 對 Unix／win TCP 的 connect 探測也須隔離，不能只 mock 最後 RPC。
    monkeypatch.setattr(cli, "_endpoint_alive", lambda endpoint: True)
    calls = []
    state = {"reachable": True, "flashing": False}

    def rpc(endpoint, method, params, **kwargs):
        calls.append((endpoint, method))
        assert method in {"health.ping", "mcu.status", "session.list"}
        if not state["reachable"]:
            raise OSError("隔離測試模擬 tunnel 暫時斷線")
        return {"ok": True, "flashing": state["flashing"], "sessions": []}

    monkeypatch.setattr(cli, "rpc_call", rpc)
    config = cfg_dir / "config.yaml"

    def write(socket_path=None, mode="on-demand"):
        cfg_dir.mkdir(parents=True, exist_ok=True)
        data = {"supervision_mode": mode, "custom_setting": "保留其他設定"}
        if socket_path is not None:
            data["socket_path"] = socket_path
        config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    yield SimpleNamespace(
        cli=cli, fx=fx, config=config, write=write, calls=calls, state=state,
        read=lambda: RuntimeConfig(config), home=tmp_path,
    )
    assert all(method in {"health.ping", "mcu.status", "session.list"} for _, method in calls)


def test_setup_writes_config_to_xdg_config_dir_readable_by_supervision_mode(setup_env, capsys):
    """自訂 SERIALWRAP_CONFIG_DIR 的 writer 與 supervision-mode reader 一致。"""
    env = setup_env
    assert env.cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()
    assert env.config.is_file()
    assert env.cli.main(["supervision-mode"]) == 0
    assert capsys.readouterr().out.strip() == "on-demand"
    assert env.fx.calls == []


@pytest.mark.parametrize("endpoint", [
    "tcp://203.0.113.5:48701", "tcp://127.0.0.1:48701",
    "tcp://localhost:48701", "tcp://[::1]:48701",
])
@pytest.mark.parametrize("reachable", [True, False], ids=["reachable", "offline"])
def test_setup_preserves_tcp_and_default_rpc_routing_on_repeated_refresh(setup_env, capsys, endpoint, reachable):
    """同模式重跑兩次仍保留 endpoint；一般 RPC 路由不變，且不停、不啟 daemon。"""
    env = setup_env
    env.write(endpoint)
    env.state["reachable"] = reachable
    for _ in range(2):
        assert env.cli.main(["setup", "--on-demand"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["setup"]["transitioned"] is False
        assert env.read().socket_path() == endpoint
        assert yaml.safe_load(env.config.read_text())["custom_setting"] == "保留其他設定"
        # 使用真實 resolver 與 CLI 接線、只在 RPC 傳輸邊界攔截。
        env.state["reachable"] = True
        assert env.cli.main(["session", "list"]) == 0
        capsys.readouterr()
        assert env.calls[-1] == (endpoint, "session.list")
        env.state["reachable"] = reachable
    assert {ep for ep, _ in env.calls} == {endpoint}
    assert env.fx.calls == []
    assert not (env.home / ".config/systemd/user/serialwrap.service").exists()


@pytest.mark.parametrize("existing", [
    None, "", "/tmp/previous.sock", "unix:///tmp/previous.sock", 42, ["tcp://localhost:48701"],
    "tcp://", "tcp://localhost", "tcp://localhost:not-a-port", "tcp://localhost:99999",
    "tcp://localhost:48701/path", "tcp://localhost:48701?query=1", "tcp://[broken:48701",
    "udp://localhost:48701",
])
def test_setup_defaults_local_for_missing_non_tcp_or_malformed_endpoint(setup_env, capsys, existing):
    """缺值、非字串、Unix、格式錯誤及其他協定不觸發 TCP 保存例外。"""
    env = setup_env
    env.write(existing)
    assert env.cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()
    assert env.read().socket_path() == env.cli.SOCKET_PATH
    assert env.fx.calls == []


@pytest.mark.parametrize("flag", ["--socket", "--endpoint"])
def test_setup_explicit_endpoint_flag_retains_canonical_write_contract(setup_env, capsys, flag):
    """顯式值供 probe 使用；setup 沿用寫回本機預設的契約，不假稱持久化顯式值。"""
    env = setup_env
    env.write("tcp://127.0.0.1:48701")
    explicit = "tcp://203.0.113.10:48702"
    assert env.cli.main([flag, explicit, "setup", "--on-demand"]) == 0
    capsys.readouterr()
    assert env.read().socket_path() == env.cli.SOCKET_PATH
    assert {ep for ep, _ in env.calls} == {explicit}
    assert env.fx.calls == []


def test_setup_empty_endpoint_retains_existing_absent_value_semantics(setup_env, capsys):
    """resolver 既有契約將空 --endpoint 視為缺值，setup 仍保存 TCP。"""
    env = setup_env
    endpoint = "tcp://127.0.0.1:48701"
    env.write(endpoint)
    assert env.cli.main(["--endpoint", "", "setup", "--on-demand"]) == 0
    capsys.readouterr()
    assert env.read().socket_path() == endpoint
    assert {ep for ep, _ in env.calls} == {endpoint}
    assert env.fx.calls == []


def test_setup_explicit_empty_socket_is_still_an_override(setup_env, capsys):
    """--socket 有傳即明確，即使空字串也不能誤判為未指定。"""
    env = setup_env
    env.write("tcp://127.0.0.1:48701")
    assert env.cli.main(["--socket", "", "setup", "--on-demand"]) == 0
    capsys.readouterr()
    assert env.read().socket_path() == env.cli.SOCKET_PATH
    assert env.fx.calls == []


@pytest.mark.parametrize("mode,flags", [
    ("systemd-user", ["--user"]),
    ("systemd-system", ["--system", "--with-sudo"]),
])
def test_setup_systemd_refresh_keeps_canonical_socket(setup_env, capsys, mode, flags):
    """本機 systemd unit 刷新不混入保存的 TCP client endpoint；全部 effects 為替身。"""
    env = setup_env
    env.write("tcp://127.0.0.1:48701", mode=mode)
    assert env.cli.main(["setup", *flags]) == 0
    capsys.readouterr()
    expected = env.cli.SYSTEM_SOCKET if mode == "systemd-system" else env.cli.SOCKET_PATH
    assert env.read().socket_path() == expected
    assert not any("stop" in cmd or "start" in cmd for cmd in env.fx.calls)


@pytest.mark.parametrize("old,flags", [
    ("on-demand", []),  # FakeEffects 有 systemd：install.sh 未傳模式時同此 auto 路徑。
    ("systemd-user", ["--on-demand"]),
])
@pytest.mark.parametrize("reachable", [True, False])
def test_setup_mode_transition_does_not_preserve_tcp_client_endpoint(setup_env, capsys, old, flags, reachable):
    """保存規則不延伸到模式轉換；不把本機 daemon 與舊 TCP 設定混在一起。"""
    env = setup_env
    env.write("tcp://127.0.0.1:48701", mode=old)
    env.state["reachable"] = reachable
    assert env.cli.main(["setup", *flags]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["setup"]["transitioned"] is True
    assert env.read().socket_path() == env.cli.SOCKET_PATH
    if old == "on-demand" and reachable:
        # 既有風險：可達的 TCP client 模式轉本機 systemd 時，仍會先發 daemon stop。
        stop = env.fx.calls.index(["serialwrap", "daemon", "stop"])
        start = env.fx.calls.index(["systemctl", "--user", "start", "serialwrap"])
        assert stop < start
        assert {ep for ep, _ in env.calls} == {"tcp://127.0.0.1:48701"}


def test_setup_win_backend_does_not_apply_posix_preservation(setup_env, capsys, monkeypatch):
    """模擬 win backend 的排除邊界，不宣稱替代 Windows 原生端到端驗證。"""
    env = setup_env
    env.write("tcp://127.0.0.1:48701")
    monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
    assert env.cli.main(["setup", "--on-demand"]) == 0
    capsys.readouterr()
    assert env.read().socket_path() == env.cli.SOCKET_PATH
    assert env.fx.calls == []


def test_setup_force_preserves_tcp_and_flash_guard_still_precedes_writes(setup_env, capsys):
    """force 只沿用 profiles／flash 語意，不是重設 endpoint 的新開關。"""
    env = setup_env
    endpoint = "tcp://127.0.0.1:48701"
    env.write(endpoint)
    before = env.config.read_bytes()
    env.state["flashing"] = True
    assert env.cli.main(["setup", "--on-demand"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "FLASHING_BUSY"
    assert env.config.read_bytes() == before
    assert not (env.home / "data").exists()
    assert env.cli.main(["setup", "--on-demand", "--force"]) == 0
    capsys.readouterr()
    assert env.read().socket_path() == endpoint
    assert env.fx.calls == []


def test_setup_prints_console_hint_for_serialwrap_minicom(setup_env, capsys):
    """setup 完成後仍提示 human console 的 broker 入口。"""
    assert setup_env.cli.main(["setup", "--on-demand"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "serialwrap-minicom" in payload["console_hint"]
