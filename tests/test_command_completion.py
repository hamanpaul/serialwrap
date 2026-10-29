"""每命令完成 regex：CLI/RPC 仲裁、PTY producer 證據與零 TX gate。"""

from __future__ import annotations

import io
import json
import os
try:  # POSIX-only：原生 Windows 無 pty 模組；僅 POSIX PTY 測試（posix_only + posix_pty）使用
    import pty
except ImportError:  # pragma: no cover -- 原生 Windows
    pty = None
import select
import threading
import time
try:  # POSIX-only：原生 Windows 無 tty 模組；僅 POSIX PTY 測試（posix_only + posix_pty）使用
    import tty
except ImportError:  # pragma: no cover -- 原生 Windows
    tty = None
from contextlib import redirect_stdout
from unittest import mock

import pytest

from sw_core.arbiter import CommandArbiter
from sw_core.cli import main
from sw_core.config import SessionProfile, UartProfile
from sw_core.service import SerialwrapService
from sw_core.session_manager import SessionManager
from sw_core.uart_io import UARTBridge


COMPLETION = r"(?m)^__RC_case1__=[0-9]+\r?\n__END_case1__\r?$"
COMMAND = "printf '__RC_case1__=%s\\n__END_case1__\\n' \"$?\""


class Wal:
    def __init__(self):
        self.current_seq = 0
        self.entries = []

    def append(self, **kwargs):
        self.current_seq += 1
        self.entries.append(kwargs)


class Producer:
    """先回顯命令，再輸出 RC/END；producer 完成後故意不給 shell prompt。"""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.stop = threading.Event()
        self.produce = True
        self.give_prompt = False
        self.prefix_prompt = False
        self.delay = 0.08
        self.tx = []
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
                prefix = b"root@fake:/# " if self.prefix_prompt else b""
                os.write(self.master, prefix + line + b"\r\n")
                if self.produce:
                    time.sleep(self.delay)
                    os.write(self.master, b"__BEGIN_case1__\r\nVALUE\r\n__RC_case1__=0\r\n__END_case1__\r\n")
                if self.give_prompt:
                    os.write(self.master, b"root@fake:/# ")

    def close(self):
        self.stop.set()
        self.thread.join(1)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def device(tmp_path):
    producer = Producer()
    wal = Wal()
    profile = SessionProfile("fake", "COM0", 1, "fake", "/dev/serial/by-id/fake", "shell",
                             prompt_regex=r"(?m)^root@fake:.*# ", login_regex="$^", timeout_s=0.15)
    bridge = UARTBridge("COM0", producer.path, UartProfile(), wal)
    bridge.start()
    manager = SessionManager([profile], wal, on_ready=lambda _: None, on_detached=lambda _: None,
                             state_path=str(tmp_path / "state.json"))
    session = manager.get_session("COM0")
    assert session is not None
    session.bridge = bridge
    session.state = "READY"
    try:
        yield producer, bridge, manager, session, wal
    finally:
        bridge.stop()
        producer.close()


@pytest.mark.parametrize("mode", ["line", "background"])
@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_producer_completion_finishes_without_prompt_and_preserves_stdout(device, mode):
    producer, bridge, manager, session, wal = device
    started = time.monotonic()
    result = manager.execute_command(session.session_id, COMMAND, "agent:test", "completion-case", timeout_s=0.3,
                                     mode=mode, completion_regex=COMPLETION)
    assert result["ok"] and not result["partial"], result
    assert time.monotonic() - started >= producer.delay
    assert "__BEGIN_case1__" in result["stdout"]
    assert "VALUE" in result["stdout"] and "__RC_case1__=0" in result["stdout"]
    assert "__END_case1__" in result["stdout"]
    assert not any(b"\x03" in item or b"\x04" in item for item in producer.tx)
    assert any(entry.get("meta", {}).get("completion_regex") == COMPLETION for entry in wal.entries)


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_echo_only_cannot_satisfy_numeric_rc_end_producer_regex(device):
    producer, bridge, manager, session, wal = device
    producer.produce = False
    result = manager.execute_command(session.session_id, COMMAND, "agent:test", "echo-only", timeout_s=0.1,
                                     completion_regex=COMPLETION)
    assert not result["ok"] and result["partial"]


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_historical_marker_cannot_complete_new_command(device):
    producer, bridge, manager, session, wal = device
    producer.produce = False
    bridge._append_rx_text(b"__RC_case1__=0\r\n__END_case1__\r\n")
    result = manager.execute_command(session.session_id, COMMAND, "agent:test", "old-marker", timeout_s=0.1,
                                     completion_regex=COMPLETION)
    assert not result["ok"] and result["partial"]


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_prompt_fallback_without_completion_is_unchanged(device):
    producer, bridge, manager, session, wal = device
    producer.give_prompt = True
    result = manager.execute_command(session.session_id, COMMAND, "agent:test", "default-case", timeout_s=0.3)
    assert result["ok"] and not result["partial"]
    assert "__END_case1__" in result["stdout"]
    assert not any("completion_regex" in entry.get("meta", {}) for entry in wal.entries)


@pytest.mark.parametrize("trailing_prompt", [False, True])
@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_prompt_before_producer_cannot_truncate_completed_stdout(device, trailing_prompt):
    producer, bridge, manager, session, wal = device
    producer.prefix_prompt = True
    producer.give_prompt = trailing_prompt
    result = manager.execute_command(session.session_id, COMMAND, "agent:test", "prefix-prompt", timeout_s=0.3,
                                     completion_regex=COMPLETION)
    assert result["ok"] and not result["partial"], result
    assert result["stdout"] == "__BEGIN_case1__\nVALUE\n__RC_case1__=0\n__END_case1__"


@pytest.mark.parametrize("invalid", ["(", "", "a" * 1025, "x" * 512 + "界" * 200, 12, ".*", "\ud800", "(" * 1000],
                         ids=["syntax", "empty", "bytes", "unicode-bytes", "type", "matches-empty", "invalid-unicode", "parser-recursion"])
@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_invalid_regex_direct_execute_rejects_before_any_tx(device, invalid):
    producer, bridge, manager, session, wal = device
    result = manager.execute_command(session.session_id, COMMAND, "agent:test", "invalid-case", completion_regex=invalid)
    assert result == {"ok": False, "error_code": "INVALID_COMPLETION_REGEX"}
    assert producer.tx == []


# OD-67（擁有者裁決）／P5 第 6 輪 SPEC-GAP #3：明確 ids，沿用 pytest 9.0.3 的自動 id 形式（pytest 9.2 起過長字串參數
# 改以 `invalid2` 呈現、nodeid 隨版本變動）；已凍結的 nodeid 全數不變（舊 id → 新 id 為恆等），無須移除。
_INVALID_RPC_CASES = ["(", "", "a" * 1025, 12, ".*"]


@pytest.mark.parametrize("invalid", _INVALID_RPC_CASES, ids=[str(case) for case in _INVALID_RPC_CASES])
@pytest.mark.core_portable
@pytest.mark.memory_only
def test_invalid_regex_rpc_and_arbiter_reject_without_enqueue(invalid):
    service = SerialwrapService([])
    with mock.patch.object(service, "_resolve_session_id") as resolve:
        result = service.rpc("command.submit", {"selector": "COM0", "cmd": COMMAND, "completion_regex": invalid})
        assert not result["ok"] and result["error_code"] == "INVALID_COMPLETION_REGEX"
        resolve.assert_not_called()
    callback = mock.Mock()
    arbiter = CommandArbiter(callback)
    result = arbiter.submit(session_id="missing", command=COMMAND, source="agent:test", mode="line", timeout_s=1,
                            completion_regex=invalid)
    assert result == {"ok": False, "error_code": "INVALID_COMPLETION_REGEX"}
    callback.assert_not_called()
    assert arbiter.snapshot() == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_transmits_completion_regex():
    with mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}) as rpc, redirect_stdout(io.StringIO()):
        assert main(["--socket", "/tmp/nonexistent-completion.sock", "cmd", "submit", "--selector", "COM0", "--cmd", COMMAND,
                     "--completion-regex", COMPLETION]) == 0
    assert rpc.call_args.args[1] == "command.submit"
    assert rpc.call_args.args[2]["completion_regex"] == COMPLETION


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_rejects_invalid_regex_before_rpc():
    output = io.StringIO()
    with mock.patch("sw_core.cli.rpc_call") as rpc, redirect_stdout(output):
        assert main(["cmd", "submit", "--selector", "COM0", "--cmd", COMMAND, "--completion-regex", "("]) == 1
    assert json.loads(output.getvalue())["error_code"] == "INVALID_COMPLETION_REGEX"
    rpc.assert_not_called()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rpc_queue_worker_execute_and_command_record_propagate_regex():
    service = SerialwrapService([])
    session_id = "fake:COM0"
    service._arbiter.register_session(session_id)
    try:
        with mock.patch.object(service, "_resolve_session_id", return_value=(session_id, None)), \
             mock.patch.object(service._sessions, "execute_command", return_value={"ok": True, "stdout": "complete", "partial": False}) as execute:
            submitted = service.rpc("command.submit", {"selector": "COM0", "cmd": COMMAND, "completion_regex": COMPLETION})
            assert submitted["ok"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                record = service.rpc("command.get", {"cmd_id": submitted["cmd_id"]})["command"]
                if record["done_at"]:
                    break
                time.sleep(0.01)
            assert record["status"] == "done", record
            assert record["completion_regex"] == COMPLETION
            assert execute.call_args.kwargs["completion_regex"] == COMPLETION
    finally:
        service._arbiter.unregister_session(session_id)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_legacy_seven_argument_arbiter_callback_is_unchanged():
    callback = mock.Mock(return_value={"ok": True})
    arbiter = CommandArbiter(callback)
    arbiter.register_session("fake")
    try:
        submitted = arbiter.submit(session_id="fake", command="echo ok", source="agent:test", mode="line", timeout_s=1)
        deadline = time.monotonic() + 2
        while not arbiter.get(submitted["cmd_id"])["command"]["done_at"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(callback.call_args.args) == 7
        assert callback.call_args.kwargs == {}
    finally:
        arbiter.unregister_session("fake")


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_completion_override_does_not_replace_reboot_recovery(device):
    producer, bridge, manager, session, wal = device
    with mock.patch.object(manager, "_handle_reboot_command", return_value={"ok": True, "recovery_action": "EXPECT_REBOOT"}) as reboot:
        result = manager.execute_command(session.session_id, "reboot", "agent:test", "reboot-case", completion_regex=COMPLETION)
    assert result["recovery_action"] == "EXPECT_REBOOT"
    assert reboot.call_args.kwargs["command"] == "reboot"
    assert "completion_regex" not in reboot.call_args.kwargs
    assert producer.tx == []
    assert not any("completion_regex" in entry.get("meta", {}) for entry in wal.entries)
