def test_config_roundtrip(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    rc = RuntimeConfig(tmp_path / "config.yaml")
    rc.set_mode("systemd-user", socket_path="/run/user/1000/serialwrap/serialwrapd.sock")
    rc2 = RuntimeConfig(tmp_path / "config.yaml")
    assert rc2.mode() == "systemd-user"
    assert rc2.socket_path().endswith("serialwrapd.sock")

def test_set_mode_preserves_existing_socket_path(tmp_path):
    """set_mode 不帶 socket_path 時，不可清掉先前已存的 socket_path（單一事實來源不可遺失）。"""
    from sw_core.runtime_config import RuntimeConfig
    RuntimeConfig(tmp_path / "config.yaml").set_mode("systemd-user", socket_path="/run/s.sock")
    RuntimeConfig(tmp_path / "config.yaml").set_mode("on-demand")  # 不帶 socket_path
    rc = RuntimeConfig(tmp_path / "config.yaml")
    assert rc.mode() == "on-demand"
    assert rc.socket_path() == "/run/s.sock"


def test_config_absent_returns_none(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    rc = RuntimeConfig(tmp_path / "missing.yaml")
    assert rc.mode() is None
    assert rc.socket_path() is None

def test_state_migrate_only_when_dest_empty(tmp_path):
    from sw_core.state_migrate import migrate_legacy_state
    legacy = tmp_path / "old"
    legacy.mkdir()
    (legacy / "state.json").write_text('{"x":1}')
    dest = tmp_path / "new" / "state.json"
    assert migrate_legacy_state(legacy / "state.json", dest) is True
    assert dest.read_text() == '{"x":1}'
    # 第二次：dest 已存在 → 不再搬，回 False
    assert migrate_legacy_state(legacy / "state.json", dest) is False

def test_state_migrate_noop_when_legacy_absent(tmp_path):
    from sw_core.state_migrate import migrate_legacy_state
    assert migrate_legacy_state(tmp_path / "nope.json", tmp_path / "d" / "state.json") is False
