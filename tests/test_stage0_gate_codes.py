from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from sw_core.arbiter import CommandArbiter
from sw_core.config import SessionProfile
from sw_core.service import SerialwrapService
from stage0_fakes import FakePort, make_bridge, wal_records

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _make_profile(
    name: str = "p",
    com: str = "COM0",
    by_id: str = "/dev/serial/by-id/fake-test",
    *,
    platform: str = "prpl",
) -> SessionProfile:
    """建立測試用 SessionProfile。"""
    return SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias=name,
        device_by_id=by_id,
        platform=platform,
    )


@pytest.fixture
def stage0_service(tmp_path):
    """Stage 0 測試用 SerialwrapService fixture。

    建立含單一 COM0 profile 的 service，回傳 (svc, sess)；
    teardown 時呼叫 svc.stop() 確保資源釋放。
    """
    state_file = str(tmp_path / "state.json")
    svc = SerialwrapService([_make_profile()], templates=[], state_path=state_file)
    sess = svc._sessions.get_session("COM0")
    assert sess is not None
    svc._arbiter.register_session(sess.session_id)
    try:
        yield svc, sess
    finally:
        svc.stop()


def _wait_for_terminal(arbiter: CommandArbiter, cmd_id: str) -> dict[str, object]:
    """等待命令進入終態，並回傳 command.get 的命令紀錄。"""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        result = arbiter.get(cmd_id)
        if result["ok"] and result["command"]["status"] in {"done", "error", "canceled"}:
            return result["command"]
        time.sleep(0.01)
    raise AssertionError(f"命令未在時限內結束：{cmd_id}")


@pytest.mark.parametrize(
    "state",
    ["RECOVERING", "FLASHING"],
)
def test_submit_time_state_codes(stage0_service, state):
    """SC-01 / SC-02：submit-time 狀態錯誤碼映射。

    RECOVERING 狀態回 SESSION_RECOVERING；FLASHING 狀態回 FLASHING_BUSY。
    """
    svc, sess = stage0_service
    sess.state = state
    expected_code = {
        "RECOVERING": "SESSION_RECOVERING",
        "FLASHING": "FLASHING_BUSY",
    }[state]
    r = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
    assert (
        r["ok"] is False
        and r["error_code"] == expected_code
        and r["session"]["state"] == state
    )


@pytest.mark.parametrize(
    "state",
    ["RECOVERING", "FLASHING"],
)
def test_interactive_open_state_codes(stage0_service, state):
    """SC-01 / SC-02：interactive_open 狀態錯誤碼映射。

    RECOVERING 狀態回 SESSION_RECOVERING；FLASHING 狀態回 FLASHING_BUSY。
    """
    svc, sess = stage0_service
    sess.state = state
    expected_code = {
        "RECOVERING": "SESSION_RECOVERING",
        "FLASHING": "FLASHING_BUSY",
    }[state]
    r = svc.rpc("session.interactive_open", {"selector": "COM0"})
    assert r["error_code"] == expected_code


def test_execute_time_quiet_has_retry_after(stage0_service):
    """SC-03：execute-time 的 AUTOBOOT_QUIET 拒絕回應必須附帶 retry_after_s。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.bridge = object()
    sess.ready_reconfirm_pending = True
    r = svc._sessions.execute_command(
        "p:COM0", "ls", "agent:x", "c1", timeout_s=5.0, mode="line"
    )
    assert r == {
        "ok": False,
        "error_code": "AUTOBOOT_QUIET",
        "selector": "COM0",
        "retry_after_s": 5.0,
    }


def test_rpc_human_source_is_gated_like_agent(stage0_service):
    """DF-02：RPC 傳入 human: 開頭的偽造 source 時，gate 一律以 agent 對待。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.ready_reconfirm_pending = True
    r = svc.rpc(
        "command.submit",
        {"selector": "COM0", "cmd": "ls", "source": "human:spoof"},
    )
    assert r["error_code"] == "AUTOBOOT_QUIET"


def test_canceled_but_running_counts_toward_pending():
    """SC-06：pending 計數必須納入已 cancel 但 worker 仍在執行的命令。"""
    gate = threading.Event()
    arb = CommandArbiter(
        send_cb=lambda *a, **k: (gate.wait(5), {"ok": True, "stdout": ""})[1]
    )
    arb.register_session("s")
    try:
        first = arb.submit(
            session_id="s", command="c0", source="t", mode="fg", timeout_s=1.0
        )
        deadline = time.monotonic() + 2.0
        running = False
        while time.monotonic() < deadline:
            with arb._lock:
                running = arb._running.get("s") == first["cmd_id"]
            if running:
                break
            time.sleep(0.01)
        assert running, "worker 未在 2 s 內取件"
        arb.cancel(first["cmd_id"])
        with arb._lock:
            assert arb._count_pending_locked("s") == 1
    finally:
        gate.set()
        arb.unregister_session("s")


def test_canceled_running_record_survives_cross_session_history_eviction(monkeypatch):
    """SC-06：其他 session 的終態歷史淘汰不得移除仍在 worker 執行的 canceled 命令。"""
    import sw_core.arbiter as arbiter_module

    monkeypatch.setattr(arbiter_module, "CMD_HISTORY_MAX", 4)
    running_gate = threading.Event()

    def send_cb(session_id: str, *args, **kwargs) -> dict[str, object]:
        if session_id == "a":
            running_gate.wait(5.0)
        return {"ok": True, "stdout": "done"}

    arbiter = CommandArbiter(send_cb=send_cb)
    arbiter.register_session("a")
    arbiter.register_session("b")
    try:
        canceled = arbiter.submit(
            session_id="a", command="blocked", source="test", mode="fg", timeout_s=5.0
        )
        assert canceled["ok"] is True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with arbiter._lock:
                is_running = arbiter._running.get("a") == canceled["cmd_id"]
            if is_running:
                break
            time.sleep(0.01)
        assert is_running, "session a worker 未在 2 s 內進入執行"
        assert arbiter.cancel(canceled["cmd_id"])["ok"] is True

        for index in range(8):
            completed = arbiter.submit(
                session_id="b",
                command=f"done-{index}",
                source="test",
                mode="fg",
                timeout_s=1.0,
            )
            assert completed["ok"] is True
            _wait_for_terminal(arbiter, completed["cmd_id"])

        still_running = arbiter.get(canceled["cmd_id"])
        assert still_running["ok"] is True
        with arbiter._lock:
            assert arbiter._count_pending_locked("a") == 1
    finally:
        running_gate.set()
        arbiter.unregister_session("a")
        arbiter.unregister_session("b")


def test_file_push_quiet_has_retry_after(stage0_service):
    """SC-03：SessionManager.file_push 遇 quiet 拒絕時必須附帶 retry_after_s。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.bridge = object()
    sess.ready_reconfirm_pending = True
    r = svc._sessions.file_push(
        "COM0", local_path="/tmp/fake", remote_path="/tmp/remote"
    )
    assert r["ok"] is False
    assert r["error_code"] == "AUTOBOOT_QUIET"
    assert "retry_after_s" in r
    assert r["retry_after_s"] == 5.0


@pytest.mark.parametrize("operation", ["execute_command", "file_push"])
def test_manager_quiet_window_returns_remaining_retry_after(stage0_service, operation):
    """SC-03：quiet 窗口分支回傳剩餘秒數，不可誤用 pending-only 的 5.0。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.bridge = object()
    sess.ready_reconfirm_pending = False
    sess.boot_quiet_until = time.monotonic() + 30.0

    if operation == "execute_command":
        result = svc._sessions.execute_command(
            "p:COM0", "ls", "agent:x", "quiet-window", timeout_s=5.0, mode="line"
        )
    else:
        result = svc._sessions.file_push(
            "COM0", local_path="/tmp/fake", remote_path="/tmp/remote"
        )

    assert result["error_code"] == "AUTOBOOT_QUIET"
    assert 0 < result["retry_after_s"] <= 30.0
    assert result["retry_after_s"] != 5.0


@pytest.mark.parametrize(
    ("rpc_method", "manager_method", "params"),
    [
        (
            "file.push",
            "file_push",
            {"selector": "COM0", "local_path": "/tmp/fake", "remote_path": "/tmp/remote"},
        ),
        ("file.pull", "file_pull", {"selector": "COM0", "remote_path": "/tmp/remote"}),
    ],
)
def test_file_rpc_rechecks_spoofed_human_as_agent(
    stage0_service, monkeypatch, rpc_method, manager_method, params
):
    """DF-02：file RPC 在 submit 後進入 quiet 時，偽造 human source 仍由 manager gate 拒絕。"""
    import sw_core.file_transfer as file_transfer

    svc, sess = stage0_service
    sess.state = "READY"
    sess.bridge = mock.MagicMock()
    sess.bridge.list_consoles.return_value = []
    original_resolve = svc._resolve_session_id
    original_manager_call = getattr(svc._sessions, manager_method)
    monkeypatch.setattr(file_transfer, "push_file", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(file_transfer, "pull_file", lambda *args, **kwargs: {"ok": True})

    def resolve_then_arm_quiet(selector: str, **kwargs):
        resolved = original_resolve(selector, **kwargs)
        if resolved[0] is not None and resolved[1] is None:
            sess.ready_reconfirm_pending = True
        return resolved

    def manager_call_then_gate(selector: str, **kwargs):
        assert kwargs.get("trusted_human") is False
        return original_manager_call(selector, **kwargs)

    monkeypatch.setattr(svc, "_resolve_session_id", resolve_then_arm_quiet)
    monkeypatch.setattr(svc._sessions, manager_method, manager_call_then_gate)
    result = svc.rpc(rpc_method, {**params, "source": "human:spoof"})

    assert result["error_code"] == "AUTOBOOT_QUIET"
    assert result["retry_after_s"] == 5.0


def test_execute_time_unconfirmed_has_no_retry_after(stage0_service):
    """SC-03：ready_reconfirm_failed 落終態回 READY_UNCONFIRMED，無 retry_after_s。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.bridge = object()
    sess.ready_reconfirm_failed = True
    r = svc._sessions.execute_command(
        "p:COM0", "ls", "agent:x", "c1", timeout_s=5.0, mode="line"
    )
    assert r["ok"] is False
    assert r["error_code"] == "READY_UNCONFIRMED"
    assert "retry_after_s" not in r


def test_rpc_human_source_not_promoted_on_timeout(stage0_service):
    """DF-02：source="human:x" 的 RPC 命令逾時不得升格為 PROMOTE_HUMAN_INTERACTIVE。"""
    svc, sess = stage0_service
    fake_bridge = mock.MagicMock()
    fake_bridge.rx_snapshot_len.return_value = 0
    fake_bridge.wait_for_regex_from.return_value = False
    fake_bridge.rx_text_from.return_value = ""
    fake_bridge.snapshot.return_value = {
        "running": True,
        "serial_alive": True,
        "vtty_alive": True,
    }
    fake_bridge.is_flash_mode.return_value = False
    fake_bridge.boot_quiet_active.return_value = False
    sess.bridge = fake_bridge
    sess.state = "READY"
    r = svc._send_cb(
        "p:COM0", "sleep 10", "human:spoof", "c1", timeout_s=0.01, mode="line"
    )
    assert r.get("recovery_action") != "PROMOTE_HUMAN_INTERACTIVE"
    assert r.get("status") != "interactive"


def test_console_line_trusted_human_bypasses_quiet(stage0_service, monkeypatch):
    """SC-03 / DF-02：_on_console_line 送出的命令 trusted_human=True，quiet 中仍入列。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.ready_reconfirm_pending = True
    captured: dict[str, object] = {}
    orig_submit = svc._arbiter.submit

    def fake_submit(*a, **kw):
        captured.update(kw)
        return orig_submit(*a, **kw)

    monkeypatch.setattr(svc._arbiter, "submit", fake_submit)
    svc._on_console_line("p:COM0", "client-1", "ls")
    assert captured.get("trusted_human") is True


def test_record_source_string_unchanged_for_spoof(stage0_service):
    """DF-02：偽造的 human: 來源在命令紀錄中保持原字串，只改 gate 授權判定。"""
    svc, sess = stage0_service
    sess.state = "READY"
    res = svc.rpc(
        "command.submit",
        {"selector": "COM0", "cmd": "echo 1", "source": "human:spoof"},
    )
    assert res["ok"] is True
    rec = svc._arbiter.get(res["cmd_id"])
    assert rec["ok"] is True
    assert rec["command"]["source"] == "human:spoof"


def test_queue_full_counts_canceled_running():
    """SC-06：255 accepted 加上 1 canceled-running 達到上限，下一筆拒絕 SESSION_QUEUE_FULL。"""
    gate = threading.Event()
    arb = CommandArbiter(
        send_cb=lambda *a, **k: (gate.wait(5), {"ok": True, "stdout": ""})[1]
    )
    arb.register_session("s")
    try:
        first = arb.submit(
            session_id="s", command="c0", source="t", mode="fg", timeout_s=10.0
        )
        assert first["ok"] is True
        deadline = time.monotonic() + 2.0
        running = False
        while time.monotonic() < deadline:
            with arb._lock:
                rec = arb._commands.get(first["cmd_id"])
                if rec and rec.get("started_at"):
                    running = True
                    break
            time.sleep(0.01)
        assert running, "worker 未在 2 s 內取件"
        arb.cancel(first["cmd_id"])
        for i in range(255):
            res = arb.submit(
                session_id="s",
                command=f"c{i+1}",
                source="t",
                mode="fg",
                timeout_s=10.0,
            )
            assert res["ok"] is True, f"Failed at {i}: {res}"
        overflow = arb.submit(
            session_id="s", command="overflow", source="t", mode="fg", timeout_s=10.0
        )
        assert overflow["ok"] is False
        assert overflow["error_code"] == "SESSION_QUEUE_FULL"
    finally:
        gate.set()
        arb.unregister_session("s")


def test_released_still_session_not_ready(stage0_service):
    """回歸防護：RELEASED 下 submit 回 SESSION_NOT_READY（規格 §5.3 RELEASED 列）。"""
    svc, sess = stage0_service
    sess.state = "RELEASED"
    r = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
    assert r["ok"] is False
    assert r["error_code"] == "SESSION_NOT_READY"


def test_direct_manager_human_source_keeps_legacy_semantics(stage0_service):
    """回歸防護：直接呼叫 manager.execute_command（未傳 trusted_human）沿用既有 human 語意。"""
    svc, sess = stage0_service
    sess.state = "READY"
    sess.ready_reconfirm_pending = True
    fake_bridge = mock.MagicMock()
    fake_bridge.is_flash_mode.return_value = False
    fake_bridge.write_command.return_value = b"hi\r\n"
    fake_bridge.read_until_prompt.return_value = (b"hi\r\n", True)
    fake_bridge.wait_for_regex_from.return_value = True
    fake_bridge.rx_snapshot_len.return_value = 0
    fake_bridge.rx_text_from.return_value = "echo hi\r\nhi\r\nroot@prplOS:~# "
    fake_bridge.snapshot.return_value = {
        "running": True,
        "serial_alive": True,
        "vtty_alive": True,
    }
    sess.bridge = fake_bridge
    r = svc._sessions.execute_command(
        "p:COM0", "echo hi", "human:tester", "c1", timeout_s=1.0
    )
    assert r.get("error_code") != "AUTOBOOT_QUIET"


def test_worker_omits_trusted_human_kwarg_for_rpc():
    """相容性驗證：以固定 7 參數的 send_cb 替身建 arbiter，RPC 來源命令不擲 TypeError。"""
    calls: list[str] = []

    def fake_send(
        session_id: str,
        command: str,
        source: str,
        cmd_id: str,
        timeout_s: float,
        mode: str,
        expected_duration_s: float | None = None,
    ) -> dict[str, object]:
        calls.append(cmd_id)
        return {"ok": True, "stdout": "done"}

    arb = CommandArbiter(send_cb=fake_send)
    arb.register_session("s")
    try:
        res = arb.submit(
            session_id="s", command="ls", source="agent", mode="fg", timeout_s=5.0
        )
        assert res["ok"] is True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            info = arb.get(res["cmd_id"])
            if info["ok"] and info["command"]["status"] in ("done", "error"):
                break
            time.sleep(0.01)
        info = arb.get(res["cmd_id"])
        assert info["ok"] is True
        assert info["command"]["status"] == "done"
        assert calls == [res["cmd_id"]]
    finally:
        arb.unregister_session("s")


def test_worker_passes_trusted_human_kwarg_when_enabled():
    """DF-02：worker 必須把內部 console 的明確信任旗標傳給 send callback。"""
    captured: list[dict[str, object]] = []

    def send_cb(*args, **kwargs):
        captured.append(kwargs)
        return {"ok": True, "stdout": "done"}

    arbiter = CommandArbiter(send_cb=send_cb)
    arbiter.register_session("s")
    try:
        submitted = arbiter.submit(
            session_id="s",
            command="ls",
            source="human:client-1",
            mode="line",
            timeout_s=5.0,
            trusted_human=True,
        )
        assert submitted["ok"] is True
        _wait_for_terminal(arbiter, submitted["cmd_id"])
        assert captured == [{"trusted_human": True}]
    finally:
        arbiter.unregister_session("s")


def test_console_line_reaches_uart_during_quiet_with_real_bridge(stage0_service, tmp_path):
    """DF-02：內部 human console 在 quiet 中仍經 worker 寫入 UART。"""
    svc, sess = stage0_service
    bridge_ref: dict[str, object] = {}

    def answer_prompt(data: bytes) -> None:
        if data == b"ls\n":
            bridge_ref["bridge"]._handle_serial_rx(b"root@prplOS:~# ")

    port = FakePort(on_write=answer_prompt)
    port.opened = True
    bridge, wal = make_bridge(tmp_path, port=port, wal=svc._wal)
    bridge_ref["bridge"] = bridge
    sess.state = "READY"
    sess.bridge = bridge
    sess.ready_reconfirm_pending = True
    svc._on_console_line(sess.session_id, "client-1", "ls")
    deadline = time.monotonic() + 2.0
    record = None
    while time.monotonic() < deadline:
        matches = [
            item for item in svc._arbiter.snapshot()
            if item.get("source") == "human:client-1" and item.get("command") == "ls"
        ]
        if matches and matches[-1].get("status") in {"done", "error"}:
            record = matches[-1]
            break
        time.sleep(0.01)

    assert record is not None, record
    assert record["status"] == "done", record
    assert port.writes == [b"ls\n"]
    tx_rows = [row for row in wal_records(wal) if row.get("dir") == "TX"]
    assert tx_rows and tx_rows[-1]["source"] == "human:client-1"


def test_gate_reject_zero_side_effects(stage0_service, tmp_path):
    """零副作用驗證：SESSION_RECOVERING / FLASHING_BUSY / AUTOBOOT_QUIET 拒絕時零 TX。

    替身 port writes 為空、WAL 無新 TX 紀錄；submit-time 拒絕未建命令紀錄，
    execute-time 拒絕命令紀錄 status="error" 且 error_code="AUTOBOOT_QUIET"。
    """
    svc, sess = stage0_service
    fake_port = FakePort()
    bridge, wal = make_bridge(tmp_path, port=fake_port, wal=svc._wal)
    sess.bridge = bridge
    initial_tx_rows = [row for row in wal_records(wal) if row.get("dir") == "TX"]

    def assert_zero_tx() -> None:
        assert fake_port.writes == []
        assert [row for row in wal_records(wal) if row.get("dir") == "TX"] == initial_tx_rows

    # 1. Submit-time 拒絕：RECOVERING
    sess.state = "RECOVERING"
    r1 = svc.rpc("command.submit", {"selector": "COM0", "cmd": "cmd1"})
    assert r1["ok"] is False and r1["error_code"] == "SESSION_RECOVERING"
    assert_zero_tx()
    assert not any(
        rec.get("command") == "cmd1" for rec in svc._arbiter._commands.values()
    )
    opened_recovering = svc.rpc("session.interactive_open", {"selector": "COM0"})
    assert opened_recovering["error_code"] == "SESSION_RECOVERING"
    assert_zero_tx()
    for method, params in (
        ("file.push", {"selector": "COM0", "local_path": "/tmp/fake", "remote_path": "/tmp/remote"}),
        ("file.pull", {"selector": "COM0", "remote_path": "/tmp/remote"}),
    ):
        rejected = svc.rpc(method, params)
        assert rejected["error_code"] == "SESSION_RECOVERING"
        assert_zero_tx()

    # 2. Submit-time 拒絕：FLASHING
    sess.state = "FLASHING"
    r2 = svc.rpc("command.submit", {"selector": "COM0", "cmd": "cmd2"})
    assert r2["ok"] is False and r2["error_code"] == "FLASHING_BUSY"
    assert_zero_tx()
    assert not any(
        rec.get("command") == "cmd2" for rec in svc._arbiter._commands.values()
    )
    opened_flashing = svc.rpc("session.interactive_open", {"selector": "COM0"})
    assert opened_flashing["error_code"] == "FLASHING_BUSY"
    assert_zero_tx()
    for method, params in (
        ("file.push", {"selector": "COM0", "local_path": "/tmp/fake", "remote_path": "/tmp/remote"}),
        ("file.pull", {"selector": "COM0", "remote_path": "/tmp/remote"}),
    ):
        rejected = svc.rpc(method, params)
        assert rejected["error_code"] == "FLASHING_BUSY"
        assert_zero_tx()

    # 3. Submit-time 拒絕：AUTOBOOT_QUIET
    sess.state = "READY"
    sess.ready_reconfirm_pending = True
    r3 = svc.rpc("command.submit", {"selector": "COM0", "cmd": "cmd3"})
    assert r3["ok"] is False and r3["error_code"] == "AUTOBOOT_QUIET"
    assert_zero_tx()
    assert not any(
        rec.get("command") == "cmd3" for rec in svc._arbiter._commands.values()
    )



def test_execute_time_quiet_command_record_has_no_retry_after(stage0_service, tmp_path, monkeypatch):
    """SC-03／DF-02：RPC 偽造 human source 的 execute-time quiet 記錄不含 retry 欄且零 TX。"""
    svc, sess = stage0_service
    fake_port = FakePort()
    bridge, wal = make_bridge(tmp_path, port=fake_port, wal=svc._wal)
    sess.bridge = bridge
    sess.state = "READY"
    initial_tx_rows = [row for row in wal_records(wal) if row.get("dir") == "TX"]
    original_gate = svc._sessions._agent_gate_reject_locked

    def arm_quiet_before_execute(session):
        session.boot_quiet_until = time.monotonic() + 30.0
        return original_gate(session)

    monkeypatch.setattr(svc._sessions, "_agent_gate_reject_locked", arm_quiet_before_execute)
    submitted = svc.rpc(
        "command.submit",
        {"selector": "COM0", "cmd": "cmd4", "source": "human:spoof"},
    )
    assert submitted["ok"] is True
    record = _wait_for_terminal(svc._arbiter, submitted["cmd_id"])

    assert record["status"] == "error"
    assert record["error_code"] == "AUTOBOOT_QUIET"
    assert record["source"] == "human:spoof"
    assert "retry_after_s" not in record
    assert fake_port.writes == []
    assert [row for row in wal_records(wal) if row.get("dir") == "TX"] == initial_tx_rows
