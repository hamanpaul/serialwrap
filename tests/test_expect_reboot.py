"""``cmd submit --expect-reboot``：明示 reboot，不再靠猜命令字串。

涵蓋：旗標一路傳遞（CLI → RPC → submit-time gate → arbiter queue → execute）、
走 ``_handle_reboot_command``、串接命令的 prompt 等待、與 interactive／AUTOBOOT_QUIET
的互動、``command.get`` 回報，以及沒帶旗標時的非致命提示（不改變執行）。
"""

from __future__ import annotations

import io
import json
import os
import pty
import select
import threading
import time
import tty
from contextlib import redirect_stdout
from unittest import mock

import pytest

from sw_core.arbiter import CommandArbiter
from sw_core.cli import main
from sw_core.config import SessionProfile, UartProfile
from sw_core.expect_reboot import (
    ERROR_EXPECT_REBOOT_MODE,
    ERROR_INVALID_EXPECT_REBOOT,
    WARNING_CHAINED_REBOOT,
    expect_reboot_rejection,
    valid_expect_reboot,
)
from sw_core.service import SerialwrapService
from sw_core.session_manager import SessionManager, _is_reboot_command, find_chained_reboot_segment
from sw_core.uart_io import UARTBridge

CHAINED = "firstboot -y && reboot"


class Wal:
    def __init__(self):
        self.current_seq = 0
        self.entries = []

    def append(self, **kwargs):
        self.current_seq += 1
        self.entries.append(kwargs)


class RecordingBridge:
    """只實作 reboot 路徑與一般命令路徑會碰到的介面；記錄每次等 prompt 的上限秒數。"""

    def __init__(self, prompt_after_s: float | None = None):
        self.prompt_after_s = prompt_after_s
        self.waits: list[float] = []
        self.sent: list[tuple[str, str]] = []

    def rx_snapshot_len(self) -> int:
        return 0

    def send_command(self, cmd, *, source, cmd_id=None):
        self.sent.append((cmd, source))

    def wait_for_regex_from(self, _pattern, _offset, timeout):
        self.waits.append(timeout)
        return self.prompt_after_s is not None and timeout >= self.prompt_after_s

    def rx_text_from(self, _offset):
        return ""

    def clear_rx_buffer(self):
        pass


def _profile() -> SessionProfile:
    return SessionProfile("fake", "COM0", 1, "fake", "/dev/serial/by-id/fake", "prpl",
                          prompt_regex=r"(?m)^root@fake:.*# ", login_regex="$^", timeout_s=0.15)


@pytest.fixture
def mgr(tmp_path):
    wal = Wal()
    manager = SessionManager([_profile()], wal, on_ready=lambda _: None, on_detached=lambda _: None,
                             state_path=str(tmp_path / "state.json"))
    session = manager.get_session("COM0")
    assert session is not None
    session.state = "READY"
    return manager, session, wal


def _use(session, bridge):
    session.bridge = bridge
    return bridge


# --- 驗證函式 -------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, True, False])
def test_valid_values(value):
    assert valid_expect_reboot(value)


@pytest.mark.parametrize("value", ["false", "true", "", 0, 1, [], {}])
def test_non_bool_is_rejected_not_coerced(value):
    assert not valid_expect_reboot(value)
    assert expect_reboot_rejection(value, "line") == {"ok": False, "error_code": ERROR_INVALID_EXPECT_REBOOT}


def test_interactive_with_flag_is_rejected_but_false_is_not():
    rejected = expect_reboot_rejection(True, "interactive")
    assert rejected["error_code"] == ERROR_EXPECT_REBOOT_MODE and not rejected["ok"]
    assert expect_reboot_rejection(False, "interactive") is None
    assert expect_reboot_rejection(None, "interactive") is None
    assert expect_reboot_rejection(True, "line") is None
    assert expect_reboot_rejection(True, "background") is None


# --- 執行層：走 reboot 路徑 ------------------------------------------------------------


def test_flag_routes_chained_command_to_reboot_handler(mgr):
    manager, session, wal = mgr
    _use(session, RecordingBridge())
    with mock.patch.object(manager, "_handle_reboot_command", return_value={"ok": True, "recovery_action": "EXPECT_REBOOT"}) as reboot:
        result = manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", timeout_s=7.0,
                                         expected_duration_s=30.0, expect_reboot=True)
    assert result["recovery_action"] == "EXPECT_REBOOT"
    kwargs = reboot.call_args.kwargs
    assert kwargs["command"] == CHAINED
    # 串接命令的 prompt 何時回來由 --cmd-timeout／--expected-duration 界定，不是固定 2s
    assert kwargs["prompt_wait_s"] == 30.0
    assert any(e.get("meta", {}).get("event") == "command_expect_reboot" for e in wal.entries)


def test_without_flag_chained_command_keeps_old_behaviour(mgr):
    manager, session, wal = mgr
    assert not _is_reboot_command(CHAINED)  # 不默默擴大猜法
    _use(session, RecordingBridge())  # 永遠等不到 prompt
    with mock.patch.object(manager, "_handle_reboot_command") as reboot, \
         mock.patch.object(manager, "_recover_after_failure", return_value={"ok": False, "partial": True}) as recover:
        result = manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", timeout_s=0.1)
    reboot.assert_not_called()
    recover.assert_called_once()
    assert result == {"ok": False, "partial": True}
    assert not any(e.get("meta", {}).get("event") == "command_expect_reboot" for e in wal.entries)


def test_flag_with_plain_reboot_keeps_two_second_prompt_wait(mgr):
    manager, session, wal = mgr
    _use(session, RecordingBridge())
    with mock.patch.object(manager, "_handle_reboot_command", return_value={"ok": True}) as reboot:
        manager.execute_command(session.session_id, "reboot", "agent:t", "c1", timeout_s=10.0, expect_reboot=True)
    assert reboot.call_args.kwargs["prompt_wait_s"] is None


def test_plain_reboot_without_flag_is_unchanged(mgr):
    manager, session, wal = mgr
    _use(session, RecordingBridge())
    with mock.patch.object(manager, "_handle_reboot_command", return_value={"ok": True}) as reboot:
        manager.execute_command(session.session_id, "reboot", "agent:t", "c1", timeout_s=10.0)
    assert reboot.call_args.kwargs["prompt_wait_s"] is None
    assert not any(e.get("meta", {}).get("event") == "command_expect_reboot" for e in wal.entries)


def test_flag_in_interactive_mode_rejected_with_zero_tx(mgr):
    manager, session, wal = mgr
    bridge = _use(session, RecordingBridge())
    result = manager.execute_command(session.session_id, "reboot", "agent:t", "c1", mode="interactive", expect_reboot=True)
    assert result["error_code"] == ERROR_EXPECT_REBOOT_MODE
    assert bridge.sent == []


def test_invalid_flag_type_rejected_with_zero_tx(mgr):
    manager, session, wal = mgr
    bridge = _use(session, RecordingBridge())
    result = manager.execute_command(session.session_id, "reboot", "agent:t", "c1", expect_reboot="false")
    assert result == {"ok": False, "error_code": ERROR_INVALID_EXPECT_REBOOT}
    assert bridge.sent == []


def test_flag_does_not_bypass_autoboot_quiet_gate(mgr):
    manager, session, wal = mgr
    bridge = _use(session, RecordingBridge())
    session.arm_boot_quiet()  # 上一個 reboot 還在開機靜默窗
    result = manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", expect_reboot=True)
    assert result["error_code"] == "AUTOBOOT_QUIET"
    assert bridge.sent == []


def test_flag_wins_over_completion_regex(mgr):
    manager, session, wal = mgr
    _use(session, RecordingBridge())
    with mock.patch.object(manager, "_handle_reboot_command", return_value={"ok": True}) as reboot:
        manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", expect_reboot=True,
                                completion_regex=r"(?m)^DONE$")
    reboot.assert_called_once()
    assert "completion_regex" not in reboot.call_args.kwargs
    assert not any("completion_regex" in e.get("meta", {}) for e in wal.entries)


# --- 串接命令的 prompt 等待（先跑一段才重開）-------------------------------------------


def test_chained_command_prompt_after_pre_segment_is_not_treated_as_recovery(mgr):
    """前段耗時 5s（> 舊的固定 2s）：prompt 回來＝非同步 reboot 的 prompt 回顯，
    與單獨 reboot 同一條路徑（不開 recovery 迴圈、不 confirm_ready、gate 保留）。"""
    manager, session, wal = mgr
    bridge = _use(session, RecordingBridge(prompt_after_s=5.0))
    with mock.patch.object(manager, "_spawn_reboot_recovery") as spawn:
        result = manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", timeout_s=10.0, expect_reboot=True)
    assert result["ok"] and result["partial"] is False and "status" not in result
    spawn.assert_not_called()
    assert bridge.waits == [10.0]
    assert session.boot_quiet_until == 0.0  # TX 靜默維度解除
    assert session.ready_reconfirm_pending  # 但不是 READY 實證
    follow = manager.execute_command(session.session_id, "echo hi", "agent:t", "c2")
    assert follow["error_code"] == "AUTOBOOT_QUIET"


def test_fixed_two_second_wait_would_have_entered_recovery(mgr):
    """對照組：沿用固定 2s（不傳 prompt_wait_s）時，同一段 5s 的前段會誤入 recovery 分支。"""
    manager, session, wal = mgr
    bridge = _use(session, RecordingBridge(prompt_after_s=5.0))
    with mock.patch.object(manager, "_spawn_reboot_recovery") as spawn, mock.patch.object(manager, "_on_detached"):
        result = manager._handle_reboot_command(session, bridge, command=CHAINED, source="agent:t", cmd_id="c1",
                                                timeout_s=10.0, execution_mode="line")
    assert result["status"] == "recovering"
    spawn.assert_called_once()
    assert bridge.waits == [2.0]


def test_chained_command_without_prompt_still_falls_to_recovery(mgr):
    """前段後 shell 不再回 prompt（同步重開、sysupgrade）：等滿 --cmd-timeout 後走既有 recovery。"""
    manager, session, wal = mgr
    bridge = _use(session, RecordingBridge(prompt_after_s=None))
    with mock.patch.object(manager, "_spawn_reboot_recovery") as spawn, mock.patch.object(manager, "_on_detached"):
        result = manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", timeout_s=3.0, expect_reboot=True)
    assert result["status"] == "recovering" and result["recovery_action"] == "EXPECT_REBOOT"
    spawn.assert_called_once()
    assert bridge.waits == [3.0]
    assert session.boot_quiet_until > time.monotonic()  # 送出當下就 arm


# --- PTY：真 bridge 的 e2e ------------------------------------------------------------


class Target:
    """先回顯命令，等 ``pre_segment_s`` 模擬前段執行，再重印 prompt（非同步 reboot）。"""

    def __init__(self, pre_segment_s: float):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.pre_segment_s = pre_segment_s
        self.stop = threading.Event()
        self.tx: list[bytes] = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        buf = b""
        while not self.stop.is_set():
            if not select.select([self.master], [], [], 0.02)[0]:
                continue
            data = os.read(self.master, 4096)
            self.tx.append(data)
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                os.write(self.master, line + b"\r\n")
                time.sleep(self.pre_segment_s)
                os.write(self.master, b"root@fake:/# ")

    def close(self):
        self.stop.set()
        self.thread.join(1)
        os.close(self.master)
        os.close(self.slave)


@pytest.mark.parametrize("pre_segment_s", [0.2, 2.4])
def test_pty_chained_reboot_with_flag(tmp_path, pre_segment_s):
    target = Target(pre_segment_s)
    wal = Wal()
    manager = SessionManager([_profile()], wal, on_ready=lambda _: None, on_detached=lambda _: None,
                             state_path=str(tmp_path / "state.json"))
    session = manager.get_session("COM0")
    # 與正式組裝一致：RX 回呼接到 manager，prompt 才會解除 boot quiet（_update_boot_quiet_locked）
    bridge = UARTBridge("COM0", target.path, UartProfile(), wal,
                        on_rx_data=lambda data, sid=session.session_id: manager._on_bridge_rx(sid, data))
    bridge.start()
    session.bridge = bridge
    session.state = "READY"
    try:
        with mock.patch.object(manager, "_spawn_reboot_recovery") as spawn:
            result = manager.execute_command(session.session_id, CHAINED, "agent:t", "c1", timeout_s=6.0, expect_reboot=True)
        assert result["ok"] and result["partial"] is False, result
        spawn.assert_not_called()
        assert session.ready_reconfirm_pending and session.boot_quiet_until == 0.0
        # 前段後 prompt 回來不是 READY 實證：下一條 agent 命令被擋，零 TX
        tx_before = list(target.tx)
        follow = manager.execute_command(session.session_id, "echo hi", "agent:t", "c2", timeout_s=1.0)
        assert follow["error_code"] == "AUTOBOOT_QUIET"
        assert target.tx == tx_before
        assert not any(b"\x03" in item or b"\x04" in item for item in target.tx)
        assert any(CHAINED.encode() in item for item in target.tx)
    finally:
        bridge.stop()
        target.close()


# --- 傳遞：CLI → RPC → submit-time gate → arbiter → execute ----------------------------


def test_cli_transmits_flag_only_when_given():
    with mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}) as rpc, redirect_stdout(io.StringIO()):
        assert main(["--socket", "/tmp/nonexistent-expect-reboot.sock", "cmd", "submit", "--selector", "COM0",
                     "--cmd", CHAINED, "--expect-reboot"]) == 0
        assert rpc.call_args.args[1] == "command.submit"
        assert rpc.call_args.args[2]["expect_reboot"] is True
        assert main(["--socket", "/tmp/nonexistent-expect-reboot.sock", "cmd", "submit", "--selector", "COM0",
                     "--cmd", "ifconfig"]) == 0
        assert "expect_reboot" not in rpc.call_args.args[2]


def test_cli_rejects_interactive_with_flag_before_rpc():
    output = io.StringIO()
    with mock.patch("sw_core.cli.rpc_call") as rpc, redirect_stdout(output):
        assert main(["cmd", "submit", "--selector", "COM0", "--cmd", "reboot", "--mode", "interactive",
                     "--expect-reboot"]) == 1
    assert json.loads(output.getvalue())["error_code"] == ERROR_EXPECT_REBOOT_MODE
    rpc.assert_not_called()


def _wait_done(service, cmd_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = service.rpc("command.get", {"cmd_id": cmd_id})["command"]
        if record["done_at"]:
            return record
        time.sleep(0.01)
    raise AssertionError("command did not finish")


def test_rpc_queue_worker_execute_and_command_get_propagate_flag():
    service = SerialwrapService([])
    session_id = "fake:COM0"
    service._arbiter.register_session(session_id)
    try:
        with mock.patch.object(service, "_resolve_session_id", return_value=(session_id, None)), \
             mock.patch.object(service._sessions, "execute_command", return_value={"ok": True, "stdout": "", "partial": True}) as execute:
            submitted = service.rpc("command.submit", {"selector": "COM0", "cmd": CHAINED, "expect_reboot": True})
            assert submitted["ok"] and submitted["expect_reboot"] is True
            record = _wait_done(service, submitted["cmd_id"])
            assert record["status"] == "done"
            assert record["expect_reboot"] is True  # command.get 回報
            assert execute.call_args.kwargs["expect_reboot"] is True

            plain = service.rpc("command.submit", {"selector": "COM0", "cmd": "ifconfig"})
            assert "expect_reboot" not in plain
            record = _wait_done(service, plain["cmd_id"])
            assert record["expect_reboot"] is False
            assert "expect_reboot" not in execute.call_args.kwargs  # 沒帶旗標：callback 契約不變
    finally:
        service._arbiter.unregister_session(session_id)


def test_arbiter_callback_contract_only_extends_when_flag_is_set():
    callback = mock.Mock(return_value={"ok": True})
    arbiter = CommandArbiter(callback)
    arbiter.register_session("fake")
    try:
        for kwargs, expected in (
            ({}, {}),
            ({"expect_reboot": False}, {}),
            ({"expect_reboot": True}, {"expect_reboot": True}),
            ({"expect_reboot": True, "completion_regex": r"(?m)^X$"}, {"expect_reboot": True, "completion_regex": r"(?m)^X$"}),
        ):
            submitted = arbiter.submit(session_id="fake", command="echo ok", source="agent:t", mode="line", timeout_s=1, **kwargs)
            deadline = time.monotonic() + 2
            while not arbiter.get(submitted["cmd_id"])["command"]["done_at"] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(callback.call_args.args) == 7
            assert callback.call_args.kwargs == expected
    finally:
        arbiter.unregister_session("fake")


@pytest.mark.parametrize("bad", ["false", "true", 1, 0, ""])
def test_rpc_and_arbiter_reject_non_bool_without_enqueue(bad):
    service = SerialwrapService([])
    with mock.patch.object(service, "_resolve_session_id") as resolve:
        result = service.rpc("command.submit", {"selector": "COM0", "cmd": CHAINED, "expect_reboot": bad})
        assert not result["ok"] and result["error_code"] == ERROR_INVALID_EXPECT_REBOOT
        resolve.assert_not_called()
    callback = mock.Mock()
    arbiter = CommandArbiter(callback)
    result = arbiter.submit(session_id="missing", command=CHAINED, source="agent:t", mode="line", timeout_s=1, expect_reboot=bad)
    assert result == {"ok": False, "error_code": ERROR_INVALID_EXPECT_REBOOT}
    callback.assert_not_called()
    assert arbiter.snapshot() == []


def test_rpc_rejects_interactive_with_flag_before_session_resolution():
    service = SerialwrapService([])
    with mock.patch.object(service, "_resolve_session_id") as resolve:
        result = service.rpc("command.submit", {"selector": "COM0", "cmd": "reboot", "mode": "interactive", "expect_reboot": True})
    assert result["error_code"] == ERROR_EXPECT_REBOOT_MODE
    resolve.assert_not_called()


def test_rpc_flag_does_not_bypass_submit_time_quiet_gate():
    service = SerialwrapService([])
    gate = {"ok": False, "error_code": "AUTOBOOT_QUIET", "retry_after_s": 5.0}
    with mock.patch.object(service, "_resolve_session_id", return_value=(None, gate)), \
         mock.patch.object(service._arbiter, "submit") as submit:
        result = service.rpc("command.submit", {"selector": "COM0", "cmd": CHAINED, "expect_reboot": True})
    assert result["error_code"] == "AUTOBOOT_QUIET" and result["retry_after_s"] == 5.0
    submit.assert_not_called()


# --- 非致命提示 -----------------------------------------------------------------------


def _submit(cmd, **params):
    service = SerialwrapService([])
    session_id = "fake:COM0"
    service._arbiter.register_session(session_id)
    try:
        with mock.patch.object(service, "_resolve_session_id", return_value=(session_id, None)), \
             mock.patch.object(service._sessions, "execute_command", return_value={"ok": True, "stdout": ""}):
            result = service.rpc("command.submit", {"selector": "COM0", "cmd": cmd, **params})
            if result.get("cmd_id"):
                _wait_done(service, result["cmd_id"])
            return result
    finally:
        service._arbiter.unregister_session(session_id)


@pytest.mark.parametrize("cmd,segment", [
    ("firstboot -y && reboot", "reboot"),
    ("bcm_bootstate 3 && reboot", "reboot"),
    ("sync; systemctl reboot", "systemctl reboot"),
    ("false || shutdown -r now", "shutdown -r now"),
    ("sleep 1 & reboot -f", "reboot -f"),
    ("sync && echo b > /proc/sysrq-trigger", "echo b > /proc/sysrq-trigger"),
])
def test_chained_reboot_without_flag_gets_non_fatal_warning(cmd, segment):
    result = _submit(cmd)
    assert result["ok"] and result["cmd_id"]  # 命令照常入列
    assert result["warning"]["code"] == WARNING_CHAINED_REBOOT
    assert result["warning"]["segment"] == segment
    assert "--expect-reboot" in result["warning"]["hint"]
    assert result["warnings"] == [result["warning"]]


@pytest.mark.parametrize("cmd", [
    "reboot",  # 已能認出，走 reboot 路徑
    "ifconfig",
    "echo 'a && reboot'",  # 引號內不是串接
    "echo reboot && ls",  # reboot 只是參數
    "a && shutdown -h now",
    "a && 'unterminated",
])
def test_no_warning_for_commands_without_chained_reboot(cmd):
    result = _submit(cmd)
    assert result["ok"]
    assert "warning" not in result and "warnings" not in result


def test_no_warning_when_flag_given_or_interactive():
    assert "warning" not in _submit(CHAINED, expect_reboot=True)
    assert "warning" not in _submit(CHAINED, mode="interactive")


def test_length_warning_keeps_its_slot_and_hint_is_added_to_list():
    result = _submit(CHAINED + " # " + "x" * 5000)
    assert result["warning"]["code"] == "CMD_LENGTH_WARNING"
    assert [w["code"] for w in result["warnings"]] == ["CMD_LENGTH_WARNING", WARNING_CHAINED_REBOOT]


def test_guess_is_not_silently_widened():
    assert not _is_reboot_command(CHAINED)
    assert not _is_reboot_command("sync; systemctl reboot")
    assert find_chained_reboot_segment("reboot") is None
