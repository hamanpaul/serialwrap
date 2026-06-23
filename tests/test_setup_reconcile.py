"""Task 11 — 監管模式 reconciler 的單元測試。

驗證重點：
- 模式轉換時「先停舊、再起新」的嚴格順序（避免 /dev/ttyUSB* 兩個 reader）。
- flash 進行中除非 force，否則拒絕轉換（不動任何東西）。
- 同模式為冪等刷新，不可 stop 正在跑的 daemon。
- system 模式無 with_sudo 時不可實際跑 sudo，改記 pending_sudo。
- force 跳過 flash 護欄。
"""


def _cmds(fx):
    return [" ".join(c) for c in fx.calls]


def test_transition_stops_old_before_starting_new(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    res = reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx,
                    home=tmp_path, daemon_running=True, any_flashing=False)
    cmds = _cmds(fx)
    stop_idx = next(i for i, c in enumerate(cmds) if "stop" in c)
    start_idx = next(i for i, c in enumerate(cmds) if "start" in c)
    assert stop_idx < start_idx, f"stop 必須在 start 之前: {cmds}"
    assert res["mode"] == "systemd-user"
    assert res["transitioned"] is True
    # user unit 應已寫出
    assert (tmp_path / ".config" / "systemd" / "user" / "serialwrap.service").is_file()


def test_transition_aborts_when_flashing(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile, FlashingBusy
    fx = FakeEffects(systemd=True)
    try:
        reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx,
                  home=tmp_path, daemon_running=True, any_flashing=True)
        assert False, "應拋 FlashingBusy"
    except FlashingBusy:
        pass
    assert fx.calls == []  # flash 中不可動任何東西


def test_idempotent_same_mode_no_stop(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    reconcile(old_mode="systemd-user", target_mode="systemd-user", fx=fx,
              home=tmp_path, daemon_running=True, any_flashing=False)
    assert not any("stop" in c for c in _cmds(fx)), f"同模式不可 stop: {_cmds(fx)}"


def test_system_mode_without_sudo_records_pending_not_run(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    res = reconcile(old_mode="on-demand", target_mode="systemd-system", fx=fx,
                    home=tmp_path, daemon_running=False, any_flashing=False, with_sudo=False)
    # 未帶 with_sudo：不可實際跑 sudo 指令，改記 pending
    assert not any(c and c[0] == "sudo" for c in fx.calls)
    assert res.get("pending_sudo"), "system 模式無 sudo 應回 pending_sudo 清單"


def test_force_overrides_flashing(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    res = reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx,
                    home=tmp_path, daemon_running=True, any_flashing=True, force=True)
    assert res["mode"] == "systemd-user"  # force 跳過 flash 護欄


def test_config_is_written_with_custom_path_and_socket(tmp_path):
    """轉換實際套用時，config 寫到自訂 config_path 且 socket_path round-trip（M-2/M-1）。"""
    from sw_core.sysenv import FakeEffects
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    cfgp = tmp_path / "custom" / "config.yaml"
    res = reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx, home=tmp_path,
                    daemon_running=False, config_path=cfgp, socket_path="/run/x.sock")
    assert res["applied"] is True
    rc = RuntimeConfig(cfgp)
    assert rc.mode() == "systemd-user"
    assert rc.socket_path() == "/run/x.sock"


def test_system_no_sudo_does_not_write_config_or_diverge(tmp_path):
    """I-1：system 模式無 sudo → 不寫 config（不謊報 systemd-system）、不停舊，pending 含真實 install。"""
    from sw_core.sysenv import FakeEffects
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    cfgp = tmp_path / "config.yaml"
    res = reconcile(old_mode="on-demand", target_mode="systemd-system", fx=fx, home=tmp_path,
                    daemon_running=True, with_sudo=False, config_path=cfgp)
    assert res["applied"] is False
    assert RuntimeConfig(cfgp).mode() is None        # config 未寫 → 不分歧
    assert not any("stop" in " ".join(c) for c in fx.calls)  # 不白停舊 daemon
    assert any("install" in " ".join(c) for c in res["pending_sudo"])


def test_system_with_sudo_installs_real_unit_content(tmp_path):
    """I-2：system 模式帶 sudo → staging 寫出真實 unit 內容（非空檔），並以 sudo install 安裝。"""
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    reconcile(old_mode="on-demand", target_mode="systemd-system", fx=fx, home=tmp_path,
              daemon_running=False, with_sudo=True)
    staging = tmp_path / ".local" / "share" / "serialwrap" / "serialwrap.service"
    assert staging.is_file()
    content = staging.read_text(encoding="utf-8")
    assert "User=serialwrap" in content and "ExecStart=" in content  # 真實 system unit
    assert any(c[:2] == ["sudo", "install"] for c in fx.calls)


def test_transition_out_of_system_without_sudo_blocks_no_two_reader(tmp_path):
    """CRITICAL #4：systemd-system → user 未帶 sudo，不可停不了舊卻起新（two-reader）。"""
    from sw_core.sysenv import FakeEffects
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    cfgp = tmp_path / "config.yaml"
    res = reconcile(old_mode="systemd-system", target_mode="systemd-user", fx=fx, home=tmp_path,
                    daemon_running=True, with_sudo=False, config_path=cfgp)
    assert res["applied"] is False
    assert res["mode"] == "systemd-system"          # 模式未變
    assert fx.calls == []                            # 什麼都沒跑（沒停舊、沒起新）
    assert RuntimeConfig(cfgp).mode() is None        # config 沒被改寫
    assert any("stop" in " ".join(c) for c in res["pending_sudo"])  # 停舊列為待辦
    # 新 user unit 不可被寫出（沒起新）
    assert not (tmp_path / ".config" / "systemd" / "user" / "serialwrap.service").exists()


def test_transition_out_of_system_with_sudo_stops_before_starts(tmp_path):
    """systemd-system → user 帶 sudo：先停舊 system service，再起新 user unit。"""
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    res = reconcile(old_mode="systemd-system", target_mode="systemd-user", fx=fx, home=tmp_path,
                    daemon_running=True, with_sudo=True)
    cmds = [" ".join(c) for c in fx.calls]
    stop_idx = next(i for i, c in enumerate(cmds) if "stop" in c)
    start_idx = next(i for i, c in enumerate(cmds) if "start" in c)
    assert stop_idx < start_idx, f"停舊須在起新之前: {cmds}"
    assert res["applied"] is True and res["mode"] == "systemd-user"
