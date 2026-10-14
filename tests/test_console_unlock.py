"""Smart4 隱藏 console lock：被動靜默、憑證與 reboot 入口的回歸測試。"""

from __future__ import annotations

import dataclasses
import os
import pty
import re
import select
import threading
import time
import tty
from pathlib import Path

import pytest

from sw_core.auth import SessionAuth, resolve_session_auth
from sw_core.config import ProfileTemplate, SessionProfile, UartProfile, load_profiles
from sw_core.device_watcher import DeviceInfo
from sw_core.login_fsm import detect_boot_banner, ensure_ready
from sw_core.session_manager import SessionManager, SessionRuntime, _is_reboot_command
from sw_core.uart_io import UARTBridge


def profile(**overrides) -> SessionProfile:
    values = dict(
        profile_name="smart4R-profile", com="COM0", act_no=1, alias="smart4",
        device_by_id="/dev/serial/by-id/test-smart4", platform="shell",
        prompt_regex=r"(?m)^root@Speedport Smart 4 Typ [AB]:[^\r\n]*# ?$",
        login_regex="$^", password_regex="$^", console_unlock_env="SMART4R_CONSOLE_UNLOCK",
        console_unlock_idle_s=0.08, timeout_s=2.5, hard_timeout_s=3.0,
        bootloader_prompts=(r"(?m)^CFE>\s*$",),
    )
    values.update(overrides)
    return SessionProfile(**values)


def packaged_profile(**overrides) -> SessionProfile:
    """使用出貨模板的真實 regex，只縮短假板的等待秒數。"""
    asset_dir = Path(__file__).parents[1] / "sw_core/assets/profiles"
    template = next(tpl for tpl in load_profiles(str(asset_dir)).templates if tpl.profile_name == "smart4R-profile")
    values = {field.name: getattr(template, field.name) for field in dataclasses.fields(template)}
    values.update(com="COM0", act_no=1, alias="smart4", device_by_id="/dev/serial/by-id/test-smart4",
                  console_unlock_idle_s=0.08, timeout_s=2.5, hard_timeout_s=3.0)
    values.update(overrides)
    return SessionProfile(**values)


class Wal:
    def __init__(self):
        self.entries = []
        self.current_seq = 0

    def append(self, **kwargs):
        self.entries.append(kwargs)
        self.current_seq += 1


class Target:
    """真 PTY 假板：boot RX 後進入無 prompt lock，空行不會解鎖。"""

    def __init__(self, *, locked=True, bootloader=False, echo_secret=False):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.locked = locked
        self.bootloader = bootloader
        self.echo_secret = echo_secret
        self.execute_nonce = True
        self.async_after_prompt = False
        self.lines = []
        self.received = []
        self.last_boot = 0.0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def emit(self, data):
        os.write(self.master, data)

    def prompt(self):
        suffix = b"newWifiSensingStatus=Off\r\nwifiSensingStatus=Off\r\n" if self.async_after_prompt else b""
        # 同一 write：UART reader 不保證 prompt 與非同步 log 會被切成兩個 RX chunk。
        self.emit(b"\r\nroot@Speedport Smart 4 Typ B:/# " + suffix)

    def boot(self):
        self.locked = True
        for text in (b"CFE version 1.0\r\n", b"Booting Linux\r\n", b"console locked\r\n"):
            self.last_boot = time.monotonic()
            self.emit(text)
            time.sleep(0.035)

    def run(self):
        buf = b""
        while not self.stop.is_set():
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            data = os.read(self.master, 4096)
            self.received.append((time.monotonic(), data))
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.rstrip(b"\r")
                self.lines.append(line)
                if self.bootloader:
                    continue
                if self.locked:
                    if line == b"dummy-unlock":
                        self.locked = False
                        if self.echo_secret:
                            self.emit(line + b"\r\n")
                        self.prompt()
                    continue
                if line.startswith(b"echo __READY__"):
                    self.emit((line[5:] if self.execute_nonce else line) + b"\r\n")
                if line == b"echo __ST06_MARKER__":
                    self.emit(b"__ST06_BEGIN__\r\nVALUE\r\n__ST06_RC__=0\r\n__ST06_END__\r\n")
                self.prompt()

    def close(self):
        self.stop.set()
        self.thread.join(1)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def hardware():
    target = Target()
    wal = Wal()
    bridge = UARTBridge("COM0", target.path, UartProfile(), wal)
    bridge.start()
    try:
        yield target, bridge, wal
    finally:
        bridge.stop()
        target.close()


def test_config_inherits_and_overrides_unlock_fields(tmp_path):
    (tmp_path / "smart4.yaml").write_text("""profiles:
  smart4R-profile:
    console_unlock_env: SMART4R_CONSOLE_UNLOCK
    console_unlock_idle_s: 5
targets:
  - com: COM0
    profile: smart4R-profile
    device_by_id: /dev/serial/by-id/test-smart4
  - com: COM1
    profile: smart4R-profile
    device_by_id: /dev/serial/by-id/test-smart4b
    console_unlock_env: SECOND_UNLOCK
    console_unlock_idle_s: 7
""")
    loaded = load_profiles(str(tmp_path))
    assert loaded.templates[0].console_unlock_env == "SMART4R_CONSOLE_UNLOCK"
    assert loaded.profiles[0].console_unlock_idle_s == 5
    assert loaded.profiles[1].console_unlock_env == "SECOND_UNLOCK"
    assert loaded.profiles[1].console_unlock_idle_s == 7


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_idle_is_rejected(tmp_path, value):
    import yaml
    (tmp_path / "invalid.yaml").write_text(yaml.safe_dump({"console_unlock_idle_s": value}))
    with pytest.raises(ValueError, match="console_unlock_idle_s"):
        load_profiles(str(tmp_path))


def test_unlock_auth_only_resolves_and_repr_hides_secrets(monkeypatch):
    monkeypatch.setenv("SMART4R_CONSOLE_UNLOCK", "dummy-unlock")
    auth, resolution = resolve_session_auth(profile())
    assert auth.console_unlock == "dummy-unlock"
    assert resolution.reason == "ok"
    assert "dummy-unlock" not in repr(auth)
    assert "dummy-password" not in repr(SessionAuth(password="dummy-password"))


def test_missing_unlock_is_declared_terminal(monkeypatch):
    monkeypatch.delenv("SMART4R_CONSOLE_UNLOCK", raising=False)
    auth, resolution = resolve_session_auth(profile())
    assert auth.console_unlock is None
    assert resolution.reason == "key_absent"
    assert SessionManager._credentials_declared_but_unresolved(profile(), resolution)


def test_unlock_env_file_precedes_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "private.env"
    env_file.write_text("SMART4R_CONSOLE_UNLOCK=file-dummy\n")
    monkeypatch.setenv("SMART4R_CONSOLE_UNLOCK", "environment-dummy")
    auth, resolution = resolve_session_auth(profile(env_file=str(env_file)))
    assert auth.console_unlock == "file-dummy"
    assert resolution.reason == "ok"


def test_boot_rx_resets_idle_before_any_tx_and_unlock_is_not_logged(hardware):
    target, bridge, wal = hardware
    threading.Thread(target=target.boot, daemon=True).start()
    ok, err = ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock"))
    assert (ok, err) == (True, None)
    assert target.lines.count(b"dummy-unlock") == 1
    assert all(at >= target.last_boot + 0.075 for at, _ in target.received)
    assert not any(b"dummy-unlock" in item.get("payload", b"") for item in wal.entries)
    assert any(line.startswith(b"echo __READY__") for line in target.lines)


def test_existing_shell_prompt_skips_unlock(hardware):
    target, bridge, _ = hardware
    target.locked = False
    target.prompt()
    assert bridge.wait_for_regex(profile().prompt_regex, 0.5)
    assert ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock")) == (True, None)
    assert b"dummy-unlock" not in target.lines


def test_fresh_unlocked_attach_never_sends_secret_as_shell_command(hardware):
    target, bridge, _ = hardware
    target.locked = False
    assert ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock")) == (True, None)
    assert b"dummy-unlock" not in target.lines


@pytest.mark.parametrize("async_after_prompt", [False, True])
def test_prompt_arriving_during_second_idle_wait_skips_unlock(hardware, monkeypatch, async_after_prompt):
    """空行回應超過一秒，但在第二次靜默等待內到達，不可注入 secret。"""
    target, bridge, _ = hardware
    target.locked = False
    target.async_after_prompt = async_after_prompt
    original_prompt = target.prompt
    first_prompt = True

    def delayed_prompt():
        nonlocal first_prompt
        if first_prompt:
            first_prompt = False
            time.sleep(1.25)
        original_prompt()

    monkeypatch.setattr(target, "prompt", delayed_prompt)
    sp = packaged_profile(console_unlock_idle_s=0.5, timeout_s=5.0, hard_timeout_s=6.0)
    assert ensure_ready(bridge, sp, SessionAuth(console_unlock="dummy-unlock")) == (True, None)
    assert b"dummy-unlock" not in target.lines
    assert any(line.startswith(b"echo __READY__") for line in target.lines)


def test_history_prompt_before_boot_cannot_skip_unlock(hardware):
    target, bridge, _ = hardware
    target.prompt()
    assert bridge.wait_for_regex(profile().prompt_regex, 0.5)
    target.emit(b"\r\nCFE version 1.0\r\nLinux console locked\r\n")
    assert bridge.wait_for_regex("console locked", 0.5)
    assert ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock")) == (True, None)
    assert target.lines.count(b"dummy-unlock") == 1


def test_echoed_nonce_command_is_not_ready_evidence(hardware):
    target, bridge, _ = hardware
    target.locked = False
    target.execute_nonce = False
    assert ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock")) == (False, "READY_NONCE_TIMEOUT")


def test_packaged_nonce_accepts_prompt_and_async_log_in_same_write(hardware):
    target, bridge, _ = hardware
    target.locked = False
    target.async_after_prompt = True
    assert ensure_ready(bridge, packaged_profile(), SessionAuth(console_unlock="dummy-unlock")) == (True, None)
    assert b"dummy-unlock" not in target.lines


def test_packaged_marker_command_finishes_without_recovery_keys_on_async_log(hardware, tmp_path):
    target, bridge, wal = hardware
    target.locked = False
    target.async_after_prompt = True
    manager, session = make_manager(tmp_path, packaged_profile(), target, wal)
    session.bridge = bridge
    session.state = "READY"
    result = manager.execute_command(session.session_id, "echo __ST06_MARKER__", "agent:test", "async-prompt-case", timeout_s=0.15)
    assert result["ok"] and not result["partial"], result
    assert "__ST06_BEGIN__" in result["stdout"] and "__ST06_RC__=0" in result["stdout"] and "__ST06_END__" in result["stdout"]
    assert target.lines == [b"echo __ST06_MARKER__"]
    assert not any(b"\x03" in data or b"\x04" in data for _, data in target.received)


@pytest.mark.parametrize("text", [
    "log root@Speedport Smart 4 Typ B:~# ",
    "root@Speedport Smart 4 Typ C:~# ",
    "root@unrelated-host:~# ",
    "CFE> ",
])
def test_packaged_prompt_keeps_hostname_line_start_boundary(text):
    assert re.search(packaged_profile().prompt_regex, text) is None


def test_cfe_prompt_blocks_all_tx(hardware):
    target, bridge, _ = hardware
    target.bootloader = True
    target.emit(b"\r\nCFE> ")
    assert bridge.wait_for_regex(r"CFE>", 0.5)
    ok, err = ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock"))
    assert not ok and err == "CONSOLE_UNLOCK_BOOTLOADER"
    assert target.received == []


def test_missing_unlock_never_probes_or_sends(hardware):
    target, bridge, _ = hardware
    assert ensure_ready(bridge, profile(), SessionAuth()) == (False, "CREDENTIALS_UNRESOLVED")
    assert target.received == []


def test_continuous_rx_timeout_never_transmits(hardware):
    target, bridge, _ = hardware
    done = threading.Event()
    def flood():
        while not done.is_set():
            target.emit(b"boot in progress\r\n")
            time.sleep(0.02)
    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    try:
        ok, err = ensure_ready(bridge, profile(timeout_s=0.2), SessionAuth(console_unlock="dummy-unlock"))
        assert not ok and err == "CONSOLE_UNLOCK_IDLE_TIMEOUT"
        assert target.received == []
    finally:
        done.set()
        thread.join(1)


def test_smart4_unlock_can_pass_boot_quiet_but_other_profiles_cannot():
    smart4 = SessionRuntime("smart4R-profile:COM0", profile())
    smart4.arm_boot_quiet()
    assert not SessionManager._boot_quiet_blocks_probe(smart4)
    generic = SessionRuntime("generic:COM0", dataclasses.replace(profile(), console_unlock_env=None))
    generic.arm_boot_quiet()
    assert SessionManager._boot_quiet_blocks_probe(generic)


def make_manager(tmp_path, sp, target, wal):
    manager = SessionManager(
        [sp], wal, on_ready=lambda _: None, on_detached=lambda _: None,
        state_path=str(tmp_path / "state.json"),
    )
    manager._devices[sp.device_by_id] = DeviceInfo(by_id=sp.device_by_id, real_path=target.path)
    session = manager.get_session("COM0")
    assert session is not None
    return manager, session


@pytest.mark.parametrize("operation", ["attach", "recover"])
def test_existing_bridge_attach_and_recover_unlock_inside_boot_quiet(hardware, tmp_path, monkeypatch, operation):
    target, bridge, wal = hardware
    monkeypatch.setenv("SMART4R_CONSOLE_UNLOCK", "dummy-unlock")
    manager, session = make_manager(tmp_path, profile(), target, wal)
    session.bridge = bridge
    session.attached_real_path = target.path
    session.state = "ATTACHED"
    session.arm_boot_quiet()
    result = manager.attach_session("COM0") if operation == "attach" else manager.recover_session("COM0")
    assert result["ok"] and result["session"]["state"] == "READY"
    assert not session.boot_quiet_active()
    assert not session.ready_reconfirm_pending
    assert target.lines.count(b"dummy-unlock") == 1


def test_initial_attach_unlocks_after_cfe_boot_without_generic_cr(tmp_path, monkeypatch):
    target = Target()
    wal = Wal()
    monkeypatch.setenv("SMART4R_CONSOLE_UNLOCK", "dummy-unlock")
    manager, session = make_manager(tmp_path, profile(), target, wal)
    session.arm_boot_quiet()
    boot = threading.Thread(target=target.boot, daemon=True)
    boot.start()
    try:
        manager._attach_by_id(session.profile.device_by_id)
        assert session.state == "READY", session.last_error
        assert target.lines.count(b"dummy-unlock") == 1
        assert all(at >= target.last_boot + 0.075 for at, _ in target.received)
    finally:
        boot.join(1)
        if session.bridge is not None:
            session.bridge.stop()
        target.close()


def test_reboot_recovery_uses_unlock_inside_boot_quiet(hardware, tmp_path, monkeypatch):
    target, bridge, wal = hardware
    monkeypatch.setenv("SMART4R_CONSOLE_UNLOCK", "dummy-unlock")
    manager, session = make_manager(tmp_path, profile(), target, wal)
    session.bridge = bridge
    session.state = "RECOVERING"
    session.recovering = True
    session.arm_boot_quiet()
    bridge._on_rx_data = lambda data: manager._on_bridge_rx(session.session_id, data)
    boot = threading.Thread(target=target.boot, daemon=True)
    boot.start()
    manager._spawn_reboot_recovery(session.session_id, 2.0)
    deadline = time.monotonic() + 3
    while session.state == "RECOVERING" and time.monotonic() < deadline:
        time.sleep(0.025)
    boot.join(1)
    assert session.state == "READY", session.last_error
    assert target.lines.count(b"dummy-unlock") == 1
    assert all(at >= target.last_boot + 0.075 for at, _ in target.received)


def test_reprobe_can_schedule_passive_unlock_inside_boot_quiet(hardware, tmp_path):
    target, bridge, wal = hardware
    manager, session = make_manager(tmp_path, profile(), target, wal)
    session.bridge = bridge
    session.state = "ATTACHED"
    session.last_error = "PROMPT_UNAVAILABLE"
    session.arm_boot_quiet()
    with manager._lock:
        job = manager._prepare_reprobe_locked(session, time.monotonic())
        assert job is not None
        assert manager._reprobe_target_still_valid_locked(session, bridge, time.monotonic())


def test_template_materialization_preserves_unlock_settings(tmp_path):
    template = ProfileTemplate("smart4R-profile", console_unlock_env="SMART4R_CONSOLE_UNLOCK", console_unlock_idle_s=7)
    manager = SessionManager([], Wal(), templates=[template], on_ready=lambda _: None, on_detached=lambda _: None,
                             state_path=str(tmp_path / "state.json"))
    with manager._lock:
        session = manager._session_from_template(template, "/dev/serial/by-id/test")
        assert session.profile.console_unlock_env == "SMART4R_CONSOLE_UNLOCK"
        changed = dataclasses.replace(template, console_unlock_env="SECOND_UNLOCK", console_unlock_idle_s=9)
        manager._rematerialize_profile_locked(session, changed, "pin")
        assert session.profile.console_unlock_env == "SECOND_UNLOCK"
        assert session.profile.console_unlock_idle_s == 9


def test_dynamic_detection_skips_unlock_template():
    from unittest import mock
    from sw_core.login_fsm import detect_template
    bridge = mock.MagicMock()
    bridge.rx_tail.return_value = "root@Speedport Smart 4 Typ B:/# "
    template = ProfileTemplate("smart4R-profile", prompt_regex=profile().prompt_regex, console_unlock_env="SMART4R_CONSOLE_UNLOCK")
    assert detect_template(bridge, [template], probe_timeout_s=0.001) is None


def test_cfe_split_chunks_arm_boot_quiet(hardware, tmp_path):
    target, bridge, wal = hardware
    manager, session = make_manager(tmp_path, profile(), target, wal)
    session.bridge = bridge
    session.state = "ATTACHED"
    manager._on_bridge_rx(session.session_id, b"CF")
    assert not session.boot_quiet_active()
    manager._on_bridge_rx(session.session_id, b"E version 1.0\r\n")
    assert session.boot_quiet_active()


def test_unlock_timing_log_contains_only_safe_metadata(hardware, caplog):
    target, bridge, _ = hardware
    with caplog.at_level("INFO", logger="serialwrap"):
        assert ensure_ready(bridge, profile(), SessionAuth(console_unlock="dummy-unlock")) == (True, None)
    text = caplog.text
    assert "console unlock attempt com=COM0 rx_idle_s=" in text
    assert "required_idle_s=0.080" in text
    assert "dummy-unlock" not in text


@pytest.mark.parametrize("command", ["echo b > /proc/sysrq-trigger", "echo 'b' >/proc/sysrq-trigger"])
def test_exact_sysrq_reboot_is_recovery_command(command):
    assert _is_reboot_command(command)


@pytest.mark.parametrize("command", ["echo b > /tmp/sysrq-trigger", "echo c > /proc/sysrq-trigger", "sync; echo b > /proc/sysrq-trigger", "sync; reboot"])
def test_other_commands_do_not_broaden_reboot_detection(command):
    assert not _is_reboot_command(command)


def test_cfe_boot_banner_is_detected():
    assert detect_boot_banner("CFE version 1.0.38-164.255.3")


def test_packaged_smart4_profile_declares_no_literal_secret():
    asset = Path(__file__).parents[1] / "sw_core/assets/profiles/default.yaml"
    loaded = load_profiles(str(asset.parent))
    import yaml
    obj = yaml.safe_load(asset.read_text())
    sp = obj["profiles"]["smart4R-profile"]
    assert sp["console_unlock_env"] == "SMART4R_CONSOLE_UNLOCK"
    assert sp["console_unlock_idle_s"] == 5
    assert "console_unlock" not in sp
    assert "prpl-template" in {template.profile_name for template in loaded.templates}
