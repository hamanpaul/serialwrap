"""tests/test_service_ctl.py — service_ctl.service_action 單元測試（Task 9）。"""
from __future__ import annotations


def test_user_mode_restart_calls_systemctl_user():
    from sw_core.sysenv import FakeEffects
    from sw_core.service_ctl import service_action
    fx = FakeEffects(systemd=True)
    res = service_action("restart", mode="systemd-user", fx=fx)
    assert ["systemctl", "--user", "restart", "serialwrap"] in fx.calls
    assert res["ok"] is True


def test_system_mode_status_no_sudo():
    from sw_core.sysenv import FakeEffects
    from sw_core.service_ctl import service_action
    fx = FakeEffects(systemd=True)
    service_action("status", mode="systemd-system", fx=fx)
    assert ["systemctl", "status", "serialwrap"] in fx.calls  # status 唯讀，免 sudo


def test_system_mode_start_requires_sudo_optin():
    from sw_core.sysenv import FakeEffects
    from sw_core.service_ctl import service_action
    fx = FakeEffects(systemd=True)
    # 未帶 with_sudo：不可實際執行特權指令，只回 hint
    res = service_action("start", mode="systemd-system", fx=fx, with_sudo=False)
    assert res["ok"] is False
    assert "sudo systemctl start serialwrap" in res["hint"]
    assert not any(c and c[0] == "sudo" for c in fx.calls)
    assert ["systemctl", "start", "serialwrap"] not in fx.calls  # 沒帶 sudo 也不該直接跑
    # 帶 with_sudo：才真的跑 sudo systemctl
    fx2 = FakeEffects(systemd=True)
    res2 = service_action("start", mode="systemd-system", fx=fx2, with_sudo=True)
    assert ["sudo", "systemctl", "start", "serialwrap"] in fx2.calls


def test_on_demand_mode_returns_message_not_systemctl():
    from sw_core.sysenv import FakeEffects
    from sw_core.service_ctl import service_action
    fx = FakeEffects(systemd=False)
    res = service_action("start", mode="on-demand", fx=fx)
    assert res["ok"] is False
    assert not fx.calls  # on-demand 無 systemd，不呼叫 systemctl
