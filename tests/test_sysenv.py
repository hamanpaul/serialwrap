"""sw_core.sysenv 的單元測試。"""


def test_fake_effects_run_snapshots_cmd():
    """run() 須以 list(cmd) 快照記錄，呼叫端事後 mutate 原 list 不可污染 calls。"""
    from sw_core.sysenv import FakeEffects
    fx = FakeEffects()
    cmd = ["x"]
    fx.run(cmd)
    cmd.append("y")
    assert fx.calls[0] == ["x"]


def test_fake_effects_records_calls_and_answers_probes():
    from sw_core.sysenv import FakeEffects

    fx = FakeEffects(
        systemd=True,
        in_groups={"dialout"},
        wsl=False,
        which={"minicom": "/usr/bin/minicom"},
        commands={("systemctl", "--user", "is-active", "serialwrap"): (0, "active", "")},
    )
    assert fx.has_systemd() is True
    assert fx.user_in_group("dialout") is True
    assert fx.user_in_group("nope") is False
    assert fx.is_wsl() is False
    assert fx.which("minicom") == "/usr/bin/minicom"
    assert fx.which("absent") is None
    rc, out, err = fx.run(["systemctl", "--user", "is-active", "serialwrap"])
    assert (rc, out) == (0, "active")
    assert ["systemctl", "--user", "is-active", "serialwrap"] in fx.calls
    # unknown command → default (0,"","") and still recorded
    rc2, _, _ = fx.run(["echo", "hi"])
    assert rc2 == 0
    assert ["echo", "hi"] in fx.calls


def test_system_effects_run_captures_returncode():
    from sw_core.sysenv import SystemEffects

    fx = SystemEffects()
    rc, out, err = fx.run(["python3", "-c", "import sys;print('x');sys.exit(3)"])
    assert rc == 3
    assert "x" in out


def test_system_effects_which_and_groups_do_not_raise():
    from sw_core.sysenv import SystemEffects

    fx = SystemEffects()
    assert fx.which("definitely-not-a-binary-xyz") is None
    # should return a bool without raising even for a bogus group
    assert isinstance(fx.user_in_group("definitely-not-a-group-xyz"), bool)
    assert isinstance(fx.is_wsl(), bool)
    assert isinstance(fx.has_systemd(), bool)
