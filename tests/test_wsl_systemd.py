"""tests/test_wsl_systemd.py — #76 WSL systemd 自動啟用（ensure_wsl_systemd）。"""
import configparser

from sw_core.setup_cmd import _merge_wsl_conf_systemd, ensure_wsl_systemd
from sw_core.sysenv import FakeEffects


def _parse(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read_string(text)
    return cp


def test_merge_empty_adds_boot_systemd():
    cp = _parse(_merge_wsl_conf_systemd(""))
    assert cp.get("boot", "systemd") == "true"


def test_merge_preserves_existing_sections():
    existing = "[automount]\nenabled = true\n\n[network]\ngenerateResolvConf = true\n"
    cp = _parse(_merge_wsl_conf_systemd(existing))
    assert cp.get("boot", "systemd") == "true"
    assert cp.get("automount", "enabled") == "true"
    assert cp.get("network", "generateResolvConf") == "true"


def test_merge_existing_boot_section_keeps_other_keys():
    existing = "[boot]\ncommand = echo hi\n"
    cp = _parse(_merge_wsl_conf_systemd(existing))
    assert cp.get("boot", "systemd") == "true"
    assert cp.get("boot", "command") == "echo hi"


def test_ensure_non_wsl_is_noop():
    fx = FakeEffects(wsl=False, systemd=False)
    r = ensure_wsl_systemd(fx, "/home/x")
    assert r["wsl"] is False and r["needs_restart"] is False
    assert fx.calls == []


def test_ensure_wsl_with_systemd_already_enabled_noop(tmp_path):
    fx = FakeEffects(wsl=True, systemd=True)
    r = ensure_wsl_systemd(fx, tmp_path)
    assert r == {"wsl": True, "already": True, "enabled_now": False, "needs_restart": False}
    assert fx.calls == []


def test_ensure_wsl_without_systemd_writes_conf_and_needs_restart(tmp_path):
    """WSL 但 systemd 未啟用 → 寫 staging wsl.conf（含 [boot] systemd=true）+ sudo install + 需重啟。"""
    fx = FakeEffects(wsl=True, systemd=False)
    r = ensure_wsl_systemd(fx, tmp_path)
    assert r["enabled_now"] is True and r["needs_restart"] is True
    staging = tmp_path / ".local" / "share" / "serialwrap" / "wsl.conf"
    assert staging.is_file()
    assert _parse(staging.read_text(encoding="utf-8")).get("boot", "systemd") == "true"
    assert any(c[:2] == ["sudo", "install"] and c[-1] == "/etc/wsl.conf" for c in fx.calls)
