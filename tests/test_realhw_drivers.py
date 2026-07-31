"""#122 drivers 純函式單測（不碰 live）。"""
from __future__ import annotations

from types import SimpleNamespace

from realhw import drivers

USBIPD_LIST = """Connected:
BUSID  VID:PID    DEVICE                        STATE
8-1    0403:6001  USB Serial Converter          Attached
8-2    0403:6001  USB Serial Converter          Attached

Persisted:
GUID  DEVICE
"""


def test_parse_usbipd_list_maps_busids():
    got = drivers.parse_usbipd_list(USBIPD_LIST)
    assert got == ["8-1", "8-2"]


def test_swcli_exe_injectable(monkeypatch):
    """#155：SwCli 可注入執行檔路徑（#154 pin 防線）；未注入時維持裸 PATH 行為。"""
    import subprocess

    captured: dict[str, list[str]] = {}

    def fake_run(argv, timeout=30.0):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(drivers, "_run", fake_run)
    drivers.SwCli(exe="/opt/bin/serialwrap").run("doctor")
    assert captured["argv"][0] == "/opt/bin/serialwrap"
    drivers.SwCli().run("doctor")
    assert captured["argv"][0] == "serialwrap"


def test_strip_ansi():
    assert drivers.strip_ansi("a\x1b[31mred\x1b[0mb\x1b(B") == "aredb"


def test_find_marker_ignores_ansi_and_wraps():
    pane = "prompt$ echo MARK_42\r\n\x1b[1mMARK_42\x1b[0m\r\nprompt$"
    assert drivers.find_marker(pane, "MARK_42")
    assert not drivers.find_marker(pane, "MARK_99")


WIN_SESSIONS = {
    "ok": True,
    "sessions": [
        {"com": "COM3", "state": "READY", "device_by_id": "USB\\VID_0403+PID_6001+AQ00OAQ7A"},
        {"com": "COM4", "state": "DETACHED", "device_by_id": ""},
        {"com": "COM5", "state": "ATTACHED", "device_by_id": ""},
        {"com": "COM6", "state": "RELEASED", "device_by_id": ""},
    ],
}


def test_parse_win_held_excludes_detached_and_released():
    held = drivers.parse_win_held(WIN_SESSIONS)
    assert [h["com"] for h in held] == ["COM3", "COM5"]
    assert held[0]["state"] == "READY"


def test_match_held_for_serial_exact_hit():
    held = drivers.parse_win_held(WIN_SESSIONS)
    hit = drivers.match_held_for_serial(held, "AQ00OAQ7")
    assert hit is not None and hit["com"] == "COM3"


def test_match_held_for_serial_fallback_and_miss():
    held = [{"com": "COM7", "state": "ATTACHED", "device_by_id": ""}]
    assert drivers.match_held_for_serial(held, "AC01QZT0") == held[0]
    held2 = [{"com": "COM8", "state": "READY", "device_by_id": "OTHER_SERIAL"}]
    assert drivers.match_held_for_serial(held2, "AC01QZT0") is None
    assert drivers.match_held_for_serial([], "AC01QZT0") is None


def test_plan_hp_rescue_release_then_retry_when_held():
    assert drivers.plan_hp_rescue(True, "COM3", 0) == ("win_release:COM3", "attach_retry")
    assert drivers.plan_hp_rescue(True, "COM3", 1) == ("win_release:COM3", "attach_retry")


def test_plan_hp_rescue_bare_retry_when_not_held_or_no_win():
    assert drivers.plan_hp_rescue(False, None, 0) == ("attach_retry",)
    assert drivers.plan_hp_rescue(True, None, 0) == ("attach_retry",)


def test_plan_hp_rescue_exhausted_is_fail_attended():
    assert drivers.plan_hp_rescue(True, "COM3", 2) == ("fail_attended",)
    assert drivers.plan_hp_rescue(False, None, 2) == ("fail_attended",)
    assert drivers.plan_hp_rescue(True, None, 5) == ("fail_attended",)


def test_usbipd_attach_returns_rc(monkeypatch):
    monkeypatch.setattr(drivers, "_run",
                        lambda argv, timeout=30.0: SimpleNamespace(returncode=17, stdout="", stderr=""))
    assert drivers.Usbipd("/x").attach("8-1") == 17


def test_classify_topology_run_pass_and_skip():
    assert drivers.classify_topology_run(0, "[serialwrap] === 拓樸 1／direct：PASS ===") == \
        ("PASS", "", "", "")
    v, cat, code, _ = drivers.classify_topology_run(
        0, "[serialwrap] SKIP：docker daemon 不可連（docker info 失敗）…")
    assert (v, cat, code) == ("SKIP", "environment", "docker_unavailable")


def test_classify_topology_run_environment_signals():
    v, cat, code, reason = drivers.classify_topology_run(1, "[serialwrap] FAIL: docker build 失敗")
    assert (v, cat, code) == ("FAIL", "environment", "docker_build_failed")
    assert "docker build" in reason
    v, cat, code, _ = drivers.classify_topology_run(
        1, "[serialwrap] FAIL: sw-rt-uart1-9：uart harness（fake target + serialwrapd）逾時未就緒")
    assert (v, cat, code) == ("FAIL", "environment", "harness_not_ready")


def test_classify_topology_run_assertion_failure_is_test():
    tail = "[serialwrap] FAIL: assertion⑤：sw-rt-agent1 port 7777 bind 位址非 loopback：0.0.0.0:7777"
    v, cat, code, reason = drivers.classify_topology_run(1, tail)
    assert (v, cat, code) == ("FAIL", "test", "tunnel_assertion_failed")
    assert "assertion⑤" in reason
    v, cat, code, reason = drivers.classify_topology_run(124, "…被截斷的輸出…")
    assert (v, cat, code) == ("FAIL", "test", "tunnel_assertion_failed")
    assert "rc=124" in reason


def test_classify_topology_run_python_timeout_is_environment():
    v, cat, code, reason = drivers.classify_topology_run(-1, "…（逾時 1800s 遭終止）")
    assert (v, cat, code) == ("FAIL", "environment", "harness_timeout")
    assert "逾時" in reason


def test_remote_state_dir_resolution_order(tmp_path):
    assert drivers.remote_state_dir({"SERIALWRAP_RUN_DIR": str(tmp_path)}) == tmp_path / "remote"
    assert drivers.remote_state_dir({"SERIALWRAP_STATE_DIR": str(tmp_path)}) == tmp_path / "remote"
    assert drivers.remote_state_dir({"XDG_RUNTIME_DIR": str(tmp_path)}) == \
        tmp_path / "serialwrap" / "remote"
    got = drivers.remote_state_dir({})
    assert str(got).endswith(".local/state/serialwrap/run/remote")
