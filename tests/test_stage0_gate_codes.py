from __future__ import annotations

import json
import os
import threading
import time
from unittest import mock

import pytest

from sw_core.arbiter import CommandArbiter
from sw_core.config import SessionProfile
from sw_core.service import SerialwrapService

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


@pytest.mark.parametrize(
    "state,code",
    [("RECOVERING", "SESSION_RECOVERING"), ("FLASHING", "FLASHING_BUSY")],
)
def test_submit_time_state_codes(stage0_service, state, code):
    """SC-01 / SC-02：submit-time 狀態錯誤碼映射。

    RECOVERING 狀態回 SESSION_RECOVERING；FLASHING 狀態回 FLASHING_BUSY。
    """
    svc, sess = stage0_service
    sess.state = state
    r = svc.rpc("command.submit", {"selector": "COM0", "cmd": "ls"})
    assert r["ok"] is False and r["error_code"] == code and r["session"]["state"] == state


@pytest.mark.parametrize(
    "state,code",
    [("RECOVERING", "SESSION_RECOVERING"), ("FLASHING", "FLASHING_BUSY")],
)
def test_interactive_open_state_codes(stage0_service, state, code):
    """SC-01 / SC-02：interactive_open 狀態錯誤碼映射。

    RECOVERING 狀態回 SESSION_RECOVERING；FLASHING 狀態回 FLASHING_BUSY。
    """
    svc, sess = stage0_service
    sess.state = state
    r = svc.rpc("session.interactive_open", {"selector": "COM0"})
    assert r["error_code"] == code


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


def test_gate_reject_zero_side_effects(stage0_service):
    """零副作用驗證：SESSION_RECOVERING / FLASHING_BUSY / AUTOBOOT_QUIET 拒絕時零 TX。

    替身 port writes 為空、WAL 無新 TX 紀錄；submit-time 拒絕未建命令紀錄，
    execute-time 拒絕命令紀錄 status="error" 且 error_code="AUTOBOOT_QUIET"。
    """
    svc, sess = stage0_service
    fake_port = mock.MagicMock()
    fake_port.writes = []
    fake_bridge = mock.MagicMock()
    fake_bridge.port = fake_port
    fake_bridge.writes = fake_port.writes
    fake_bridge.is_flash_mode.return_value = False
    fake_bridge.snapshot.return_value = {
        "running": True,
        "serial_alive": True,
        "vtty_alive": True,
    }
    sess.bridge = fake_bridge

    def tx_count() -> int:
        wal_path = svc._wal.wal_path
        if not os.path.exists(wal_path):
            return 0
        with open(wal_path, "r", encoding="utf-8") as f:
            return sum(
                1
                for line in f
                if line.strip() and json.loads(line).get("source") == "tx"
            )

    initial_tx = tx_count()

    # 1. Submit-time 拒絕：RECOVERING
    sess.state = "RECOVERING"
    r1 = svc.rpc("command.submit", {"selector": "COM0", "cmd": "cmd1"})
    assert r1["ok"] is False and r1["error_code"] == "SESSION_RECOVERING"
    assert fake_port.writes == []
    assert tx_count() == initial_tx
    assert not any(
        rec.get("command") == "cmd1" for rec in svc._arbiter._commands.values()
    )

    # 2. Submit-time 拒絕：FLASHING
    sess.state = "FLASHING"
    r2 = svc.rpc("command.submit", {"selector": "COM0", "cmd": "cmd2"})
    assert r2["ok"] is False and r2["error_code"] == "FLASHING_BUSY"
    assert fake_port.writes == []
    assert tx_count() == initial_tx
    assert not any(
        rec.get("command") == "cmd2" for rec in svc._arbiter._commands.values()
    )

    # 3. Submit-time 拒絕：AUTOBOOT_QUIET
    sess.state = "READY"
    sess.ready_reconfirm_pending = True
    r3 = svc.rpc("command.submit", {"selector": "COM0", "cmd": "cmd3"})
    assert r3["ok"] is False and r3["error_code"] == "AUTOBOOT_QUIET"
    assert fake_port.writes == []
    assert tx_count() == initial_tx
    assert not any(
        rec.get("command") == "cmd3" for rec in svc._arbiter._commands.values()
    )

    # 4. Execute-time 拒絕：AUTOBOOT_QUIET
    sess.ready_reconfirm_pending = False
    sub = svc._arbiter.submit(
        session_id="p:COM0", command="cmd4", source="agent", mode="fg", timeout_s=5.0
    )
    assert sub["ok"] is True
    cmd_id = sub["cmd_id"]
    sess.ready_reconfirm_pending = True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        info = svc._arbiter.get(cmd_id)
        if info["ok"] and info["command"]["status"] in ("done", "error"):
            break
        time.sleep(0.01)
    info = svc._arbiter.get(cmd_id)
    assert info["ok"] is True
    assert info["command"]["status"] == "error"
    assert info["command"]["error_code"] == "AUTOBOOT_QUIET"
    assert fake_port.writes == []
    assert tx_count() == initial_tx
