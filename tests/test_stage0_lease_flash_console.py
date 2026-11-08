"""Stage 0 P1-T12 測試：SC-13 lease tick＋tombstone、SC-14 flash probe 資格、SC-16 console 行上限。

依據：`docs/rust-refactor/plans/02-stage0-python-bc.md` Task T12；`spec-reconciliation.md` SC-13／SC-14／SC-16。

測試包含實作前的失敗案例與後續回歸防護。替身為本檔私有 helper；全部為記憶體內測試，
不開 PTY、不開 socket、不啟動子行程，平台條件為 core_portable。
"""
from __future__ import annotations

import threading
import logging
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest

from sw_core.config import SessionProfile, UartProfile
from sw_core.service import SerialwrapService
from sw_core.session_manager import SessionManager, SessionRuntime
from sw_core.uart_io import ConsoleClient, UARTBridge
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


class _FakePort:
    """記憶體內序列埠替身：只記錄寫入的 bytes，不開任何 fd。"""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(bytes(payload))

    def is_alive(self) -> bool:
        return True


def _profile(platform: str = "shell") -> SessionProfile:
    # passthrough 的 ready_probe 為空字串 → command_capable=False，才會進入 flash 認線候選
    ready_probe = "" if platform == "passthrough" else "echo __READY__${nonce}"
    return SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/fake-com0",
        platform=platform,
        ready_probe=ready_probe,
        uart=UartProfile(),
    )


def _make_manager(tmp_path, *, platform: str = "shell", wal: WalWriter | None = None) -> SessionManager:
    """建立只含單一 COM0 session 的 SessionManager；state.json 與 WAL 皆落在 tmp_path。"""
    return SessionManager(
        [_profile(platform)],
        wal if wal is not None else WalWriter(wal_dir=str(tmp_path)),
        on_ready=lambda _sid: None,
        on_detached=lambda _sid: None,
        state_path=str(tmp_path / "state.json"),
    )


def _ready_session(tmp_path) -> tuple[SessionManager, SessionRuntime, UARTBridge, _FakePort]:
    """建立 READY 狀態的 COM0 session，並掛上未 start 的記憶體 bridge。"""
    wal = WalWriter(wal_dir=str(tmp_path))
    mgr = _make_manager(tmp_path, wal=wal)
    session = mgr.get_session("COM0")
    assert session is not None
    port = _FakePort()
    bridge = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=wal)
    bridge._serial = port  # 直接掛替身：不 start()、不開序列埠或 PTY
    with mgr._lock:
        session.bridge = bridge
        session.state = "READY"
    return mgr, session, bridge, port


def _console_bridge(tmp_path) -> UARTBridge:
    return UARTBridge(
        com="COM0", device_path="fake", profile=UartProfile(), wal=WalWriter(wal_dir=str(tmp_path)),
    )


def _console_client(client_id: str = "c1") -> ConsoleClient:
    return ConsoleClient(
        client_id=client_id, label="x", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0,
    )


# ---- SC-13：lease tick＋tombstone ----


def test_tick_expires_agent_lease_and_keeps_tombstone(tmp_path) -> None:
    mgr, _session, _bridge, _port = _ready_session(tmp_path)
    r = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.05)
    assert r["ok"] is True
    lid = r["interactive_id"]
    time.sleep(0.1)
    assert mgr._expire_interactive_leases_tick() == 1
    assert mgr.get_session("COM0").interactive_session_id is None
    assert mgr.interactive_send(lid, data="ls")["error_code"] == "INTERACTIVE_EXPIRED"
    assert mgr.interactive_status(lid)["error_code"] == "INTERACTIVE_EXPIRED"
    assert mgr.interactive_close(lid)["error_code"] == "INTERACTIVE_EXPIRED"
    assert mgr.interactive_send(lid, data="ls")["error_code"] == "INTERACTIVE_EXPIRED"  # close 不刪 tombstone


def test_status_touch_still_extends(tmp_path) -> None:
    mgr, _session, _bridge, _port = _ready_session(tmp_path)
    r = mgr.interactive_open("COM0", owner="agent:x", timeout_s=1.0)
    assert r["ok"] is True
    lid = r["interactive_id"]
    for _ in range(4):
        time.sleep(0.3)
        assert mgr.interactive_status(lid)["ok"] is True
    assert mgr._expire_interactive_leases_tick() == 0
    assert mgr._interactive[lid].status == "active"


def test_reconcile_readiness_expires_agent_lease(tmp_path) -> None:
    mgr, session, _bridge, _port = _ready_session(tmp_path)
    r = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.05)
    assert r["ok"] is True
    lid = r["interactive_id"]

    time.sleep(0.1)
    mgr.reconcile_readiness()

    assert session.interactive_session_id is None
    assert mgr._interactive[lid].status == "expired"
    assert mgr.interactive_send(lid, data="ls")["error_code"] == "INTERACTIVE_EXPIRED"


def test_interactive_status_keeps_session_not_ready_precedence(tmp_path) -> None:
    mgr, session, _bridge, _port = _ready_session(tmp_path)
    r = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.05)
    assert r["ok"] is True
    lid = r["interactive_id"]
    lease = mgr._interactive[lid]
    lease.last_activity_at -= 1.0
    session.bridge = None

    result = mgr.interactive_status(lid)

    assert result["error_code"] == "SESSION_NOT_READY"
    assert lease.status == "active"


def test_tombstone_fifo_bound_16(tmp_path) -> None:
    mgr, session, _bridge, _port = _ready_session(tmp_path)
    lease_ids: list[str] = []
    for _ in range(17):
        r = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.001)
        assert r["ok"] is True
        lease_ids.append(r["interactive_id"])
        # Windows time.monotonic 解析度約 15.625 ms（GetTickCount64），5 ms sleep 可能讀到
        # 同一刻度而使 lease 尚未過期；sleep 拉到 50 ms（遠大於 timeout 0.001 與解析度）即與平台無關。
        time.sleep(0.05)
        assert mgr._expire_interactive_leases_tick() == 1
    tombstones = [
        lease for lease in mgr._interactive.values()
        if lease.session_id == session.session_id and lease.status == "expired"
    ]
    assert len(tombstones) == 16
    # 最舊者已被 FIFO 淘汰 → NOT_FOUND；最新者仍為 tombstone → EXPIRED
    assert mgr.interactive_send(lease_ids[0], data="ls")["error_code"] == "INTERACTIVE_NOT_FOUND"
    assert mgr.interactive_send(lease_ids[-1], data="ls")["error_code"] == "INTERACTIVE_EXPIRED"


def test_tick_restores_human_owner_after_soft_preempt_expiry(tmp_path, monkeypatch) -> None:
    mgr, session, bridge, port = _ready_session(tmp_path)
    # 測試替身：human console c1 視為仍有外部 peer，使還原分支成立
    monkeypatch.setattr(bridge, "console_has_external_peer", lambda _client_id: True)
    client = _console_client()
    client.attached_at = time.time()
    bridge._clients["c1"] = client
    human = mgr.interactive_open("COM0", owner="human:c1", timeout_s=60.0)
    assert human["ok"] is True
    agent = mgr.interactive_open("COM0", owner="agent:x", timeout_s=0.05)
    assert agent["ok"] is True
    assert agent.get("soft_preempted") is True
    # agent 軟搶佔期間 human 的鍵入進 deferred buffer；上下文未失效，應於還原後回放
    bridge._deferred_buffers["c1"] = bytearray(b"ls\r")
    time.sleep(0.1)
    assert mgr._expire_interactive_leases_tick() == 1
    assert session.interactive_session_id == human["interactive_id"]
    assert bridge.snapshot()["interactive_owner"] == "human:c1"
    assert port.writes == [b"ls\r"]


def test_tick_blocks_new_lease_until_owner_restore_finishes(tmp_path, monkeypatch) -> None:
    mgr, _session, bridge, _port = _ready_session(tmp_path)
    human = mgr.interactive_open("COM0", owner="human:c1", timeout_s=60.0)
    assert human["ok"] is True
    agent = mgr.interactive_open("COM0", owner="agent:old", timeout_s=0.05)
    assert agent["ok"] is True and agent.get("soft_preempted") is True
    mgr._interactive[agent["interactive_id"]].last_activity_at -= 1.0

    resumed = threading.Event()
    allow_finish = threading.Event()
    original_resume = bridge.resume_interactive

    def delayed_resume() -> None:
        original_resume()
        resumed.set()
        assert allow_finish.wait(2.0), "測試未及時釋放 lease 收尾"

    monkeypatch.setattr(bridge, "resume_interactive", delayed_resume)
    tick_results: list[int] = []
    tick_thread = threading.Thread(target=lambda: tick_results.append(mgr._expire_interactive_leases_tick()))
    tick_thread.start()
    try:
        assert resumed.wait(2.0), "tick 未進入鎖外 ownership 還原"
        during_close = mgr.interactive_open("COM0", owner="agent:new", timeout_s=10.0)
        console_during_close = mgr.attach_console("COM0")
    finally:
        allow_finish.set()
        tick_thread.join(2.0)

    assert not tick_thread.is_alive()
    assert tick_results == [1]
    assert during_close["error_code"] == "SESSION_INTERACTIVE_BUSY"
    assert console_during_close["error_code"] == "SESSION_INTERACTIVE_BUSY"
    opened = mgr.interactive_open("COM0", owner="agent:new", timeout_s=10.0)
    assert opened["ok"] is True
    assert bridge.snapshot()["interactive_owner"] == "agent:new"


def test_tick_continues_after_one_session_replay_failure(tmp_path, caplog, monkeypatch) -> None:
    from sw_core.serial_port import TxResult
    from sw_core.uart_io import TxUnknownError

    second_profile = replace(
        _profile(), com="COM1", act_no=2, alias="lab+1-2", device_by_id="/dev/serial/by-id/fake-com1",
    )
    wal = WalWriter(wal_dir=str(tmp_path))
    mgr = SessionManager(
        [_profile(), second_profile], wal, on_ready=lambda _sid: None, on_detached=lambda _sid: None,
        state_path=str(tmp_path / "state.json"),
    )
    sessions: list[tuple[SessionRuntime, UARTBridge, _FakePort]] = []
    for com in ("COM0", "COM1"):
        session = mgr.get_session(com)
        assert session is not None
        port = _FakePort()
        bridge = UARTBridge(com=com, device_path="fake", profile=UartProfile(), wal=wal)
        bridge._serial = port
        bridge._serial_fd = None
        with mgr._lock:
            session.bridge = bridge
            session.state = "READY"
        human = mgr.interactive_open(com, owner="human:c1", timeout_s=60.0)
        assert human["ok"] is True
        agent = mgr.interactive_open(com, owner="agent:x", timeout_s=0.05)
        assert agent["ok"] is True and agent.get("soft_preempted") is True
        mgr._interactive[agent["interactive_id"]].last_activity_at -= 1.0
        bridge._deferred_buffers["c1"] = bytearray(b"human\r")
        client = _console_client()
        client.attached_at = time.time()
        bridge._clients["c1"] = client
        sessions.append((session, bridge, port))

    first_bridge = sessions[0][1]
    replay_attempts: list[bytes] = []

    def uncertain_replay(payload: bytes, **_kwargs) -> None:
        replay_attempts.append(bytes(payload))
        raise TxUnknownError(TxResult("unknown", 0, len(payload), "injected uncertainty"))

    monkeypatch.setattr(first_bridge, "console_has_external_peer", lambda _client_id: False)
    monkeypatch.setattr(sessions[1][1], "console_has_external_peer", lambda _client_id: False)
    monkeypatch.setattr(first_bridge, "send_bytes", uncertain_replay)
    with caplog.at_level(logging.WARNING, logger="serialwrap"):
        assert mgr._expire_interactive_leases_tick() == 2

    assert replay_attempts == [b"human\r"]
    assert sessions[0][1]._suspend_depth == 0
    assert sessions[0][1].snapshot()["interactive_owner"] is None
    assert sessions[1][2].writes == [b"human\r"]
    assert sessions[1][1]._suspend_depth == 0
    assert "TxUnknownError" in caplog.text


def test_human_lease_not_expired_by_tick(tmp_path) -> None:
    mgr, session, _bridge, _port = _ready_session(tmp_path)
    r = mgr.interactive_open("COM0", owner="human:c1", timeout_s=0.001)
    assert r["ok"] is True
    lid = r["interactive_id"]
    time.sleep(0.01)
    # 以 DeviceWatcher 的 1 s tick 實際入口驅動；human lease 不得因 timeout 被收回
    mgr.reconcile_readiness()
    lease = mgr._interactive.get(lid)
    assert lease is not None and lease.status == "active"
    assert session.interactive_session_id == lid


# ---- SC-14：flash probe 認線資格 ----


def test_flash_candidates_exclude_busy_and_draining(tmp_path) -> None:
    mgr = _make_manager(tmp_path, platform="passthrough")
    s = mgr.get_session("COM0")
    assert s is not None and not s.profile.command_capable  # 前置條件：原本應為候選
    s.state = "ATTACHED"
    s.foreground_busy = True
    assert mgr.collect_flash_candidates_and_mark() == []


def test_flash_candidates_exclude_pending_drain(tmp_path) -> None:
    mgr = _make_manager(tmp_path, platform="passthrough")
    s = mgr.get_session("COM0")
    assert s is not None and not s.profile.command_capable  # 前置條件：原本應為候選
    s.state = "ATTACHED"
    s._pending_drain = object()  # 模擬 release／drain 進行中（T08 欄位）
    assert mgr.collect_flash_candidates_and_mark() == []


@pytest.mark.parametrize("condition", ["draining_by_id", "foreground_operation"])
def test_flash_candidates_exclude_draining_by_id_and_foreground_operation(tmp_path, condition: str) -> None:
    mgr = _make_manager(tmp_path, platform="passthrough")
    session = mgr.get_session("COM0")
    assert session is not None and session.profile.device_by_id is not None
    session.state = "ATTACHED"
    if condition == "draining_by_id":
        mgr._draining_by_ids[session.profile.device_by_id] = object()
    else:
        session._foreground_operation = object()

    assert mgr.collect_flash_candidates_and_mark() == []


@pytest.mark.parametrize(
    "state,code",
    [("RELEASED", "SESSION_NOT_READY"), ("RECOVERING", "SESSION_RECOVERING")],
)
def test_enter_flashing_guard(tmp_path, state: str, code: str) -> None:
    mgr = _make_manager(tmp_path)
    session = mgr.get_session("COM0")
    assert session is not None
    session.state = state
    r = mgr.enter_flashing("COM0")
    assert r["ok"] is False and r["error_code"] == code


@pytest.mark.parametrize("gate", ["pending_drain", "draining_by_id"])
def test_enter_flashing_rejects_active_drain(tmp_path, gate: str) -> None:
    mgr, session, _bridge, _port = _ready_session(tmp_path)
    if gate == "pending_drain":
        session._pending_drain = object()
    else:
        assert session.profile.device_by_id is not None
        mgr._draining_by_ids[session.profile.device_by_id] = object()

    result = mgr.enter_flashing("COM0")

    assert result["ok"] is False
    assert result["error_code"] == "SESSION_RECOVERING"
    assert session.state == "READY"


def test_on_flash_open_failed_enter_releases_probe_resources(monkeypatch) -> None:
    import sw_core.service as service_module

    candidate = {"com": "COM0", "by_id": "fake-by-id", "real_path": "fake", "command_capable": False}
    calls: dict[str, list] = {"released": [], "unmarked": [], "mirrored": []}

    class _Sessions:
        def collect_flash_candidates_and_mark(self):
            return [candidate]

        def get_session(self, _selector):
            bridge = SimpleNamespace(
                mirror_termios_from=lambda *_args, **_kwargs: calls["mirrored"].append(True),
            )
            return SimpleNamespace(bridge=bridge)

        def enter_flashing(self, _com):
            return {"ok": False, "error_code": "SESSION_RECOVERING"}

        def unmark_flash_critical(self, com):
            calls["unmarked"].append(com)

    class _Probe:
        def __init__(self, _service, _mapping, *, sync_bytes):
            assert sync_bytes == b"sync"
            self.acks = {"fake-by-id": b"ack"}

        def release_held(self, **kwargs):
            calls["released"].append(kwargs)

    svc = SerialwrapService([])
    monkeypatch.setattr(svc, "_sessions", _Sessions())
    monkeypatch.setattr(service_module, "_BridgeProbe", _Probe)
    monkeypatch.setattr(
        service_module,
        "detect_mcu_line",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="matched", by_id="fake-by-id", family="unknown", hits=["fake-by-id"],
        ),
    )
    pump = mock.Mock()
    monkeypatch.setattr(service_module, "pump_endpoint_to_sink", pump)

    svc._on_flash_open(-1, -1, b"sync")

    assert calls["released"] == [{}]
    assert calls["unmarked"] == ["COM0"]
    assert calls["mirrored"] == []
    pump.assert_not_called()


# ---- SC-16：console 行上限與拒絕診斷 ----


def test_console_line_over_limit_dropped_and_logged(tmp_path, caplog) -> None:
    bridge = _console_bridge(tmp_path)
    client = _console_client()
    with caplog.at_level(logging.WARNING, logger="serialwrap"):
        lines, _echo = bridge._consume_console_input(client, b"a" * 16386 + b"\r")
    assert lines == []
    assert "console 行超過 16385 bytes" in caplog.text
    lines, _echo = bridge._consume_console_input(client, b"ok\r")
    assert lines == ["ok"]


def test_line_exactly_16385_bytes_is_emitted_then_rejected_logged(tmp_path, caplog) -> None:
    bridge = _console_bridge(tmp_path)
    lines, _echo = bridge._consume_console_input(_console_client(), b"a" * 16385 + b"\r")
    assert len(lines) == 1
    assert len(lines[0].encode("utf-8")) == 16385
    svc = SerialwrapService([], state_path=str(tmp_path / "state.json"))
    real_submit = svc._arbiter.submit
    results: list[dict] = []

    def spy(**kwargs):
        result = real_submit(**kwargs)
        results.append(result)
        return result

    with caplog.at_level(logging.WARNING, logger="serialwrap"), \
            mock.patch.object(svc._arbiter, "submit", side_effect=spy):
        svc._on_console_line("p:COM0", "c1", lines[0])
    assert results and results[0]["error_code"] == "CMD_TOO_LONG"
    assert "console 行被拒" in caplog.text
    assert "error_code=CMD_TOO_LONG" in caplog.text


def test_console_line_rejected_not_ready_logged(tmp_path, caplog) -> None:
    svc = SerialwrapService([], state_path=str(tmp_path / "state.json"))
    # 前置條件：session 尚未 register 佇列（ATTACHED 無 queue）→ arbiter 回 SESSION_NOT_READY
    pre = svc._arbiter.submit(
        session_id="p:COM0", command="ls", source="human:c1", mode="line", timeout_s=30.0,
    )
    assert pre["error_code"] == "SESSION_NOT_READY"
    with caplog.at_level(logging.WARNING, logger="serialwrap"):
        svc._on_console_line("p:COM0", "c1", "ls")
    assert "error_code=SESSION_NOT_READY" in caplog.text
