import importlib, os


def _reload(env, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("SERIALWRAP_", "XDG_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import sw_core.constants as c
    return importlib.reload(c)


def test_user_defaults_use_xdg_not_tmp(monkeypatch, tmp_path):
    xdg_run = str(tmp_path / "run")
    xdg_state = str(tmp_path / "state")
    xdg_cfg = str(tmp_path / "cfg")
    c = _reload({"XDG_RUNTIME_DIR": xdg_run,
                 "XDG_STATE_HOME": xdg_state,
                 "XDG_CONFIG_HOME": xdg_cfg}, monkeypatch)
    # SOCKET_PATH 必須落在 XDG_RUNTIME_DIR 之下（不是舊的 /tmp/serialwrap 硬編碼）
    assert c.SOCKET_PATH.startswith(xdg_run), f"SOCKET_PATH={c.SOCKET_PATH!r} 不在 {xdg_run!r} 下"
    assert c.STATE_PATH.startswith(xdg_state), f"STATE_PATH={c.STATE_PATH!r} 不在 {xdg_state!r} 下"
    assert c.PROFILE_DIR.startswith(xdg_cfg), f"PROFILE_DIR={c.PROFILE_DIR!r} 不在 {xdg_cfg!r} 下"


def test_runtime_falls_back_to_state_run_when_no_xdg_runtime(monkeypatch, tmp_path):
    xdg_state = str(tmp_path / "state")
    c = _reload({"XDG_STATE_HOME": xdg_state}, monkeypatch)
    # 無 XDG_RUNTIME_DIR 時，RUN_DIR 必須落在 STATE_DIR 之下（不是舊的 /tmp/serialwrap）
    assert c.RUN_DIR.startswith(xdg_state), f"RUN_DIR={c.RUN_DIR!r} 應在 {xdg_state!r} 下"


def test_env_override_wins(monkeypatch, tmp_path):
    c = _reload({"SERIALWRAP_RUN_DIR": str(tmp_path/"x")}, monkeypatch)
    assert c.SOCKET_PATH == str(tmp_path/"x"/"serialwrapd.sock")


def test_state_dir_override_pins_run_dir(monkeypatch, tmp_path):
    """向後相容：只設 SERIALWRAP_STATE_DIR 時 socket 必須落在 STATE_DIR 之下，即使 XDG_RUNTIME_DIR 存在。

    這是 throwaway-daemon / CI 隔離跑法的相容保證（舊行為 RUN_DIR 預設＝STATE_DIR）。
    """
    state = str(tmp_path / "state")
    xdg_run = str(tmp_path / "xdg_run")
    c = _reload({"SERIALWRAP_STATE_DIR": state, "XDG_RUNTIME_DIR": xdg_run}, monkeypatch)
    assert c.RUN_DIR == state, f"RUN_DIR={c.RUN_DIR!r} 應等於 STATE_DIR={state!r}（向後相容規則）"
    assert c.SOCKET_PATH.startswith(state)
    assert not c.SOCKET_PATH.startswith(xdg_run)


def teardown_module(module):
    import importlib, sw_core.constants as c
    importlib.reload(c)  # 還原預設，避免污染其他測試
