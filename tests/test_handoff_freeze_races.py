"""tests/test_handoff_freeze_races.py — SC-11 交接凍結與背景引擎競態（fail-closed）。

- (b) probe 於鎖外執行期間完成交接凍結：probe 結果不得把 session 推進到 READY，也不得清除 last_error。
- (c) reconcile tick 已通過頂部無鎖凍結檢查後才完成交接：DETACHED 不得變 ATTACHING、reprobe_attempts 不增加、
  lease 不被改動。
- (c2) lease 自癒區段在鎖外判定之後才完成交接：鎖內仍不得授予 human lease。
- (c3) 鎖內已把 DETACHED 標成 ATTACHING、交接於 spawn 前完成：attach 不執行，狀態須還原為 DETACHED。

測試只使用 in-process 替身（FakePort）與 tmp_path 隔離狀態；不觸碰任何真實裝置或正式 daemon。
"""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest import mock

import pytest

import sw_core.session_manager as sm
from sw_core.device_watcher import DeviceInfo
from stage0_fakes import FakePort, make_service

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _attach_device(mgr: Any, session: Any, tmp_path: Any) -> None:
    by_id = session.profile.device_by_id
    mgr._devices[by_id] = DeviceInfo(by_id=by_id, real_path=str(tmp_path / f"fake-{session.profile.com}"))


def test_probe_completing_after_handoff_freeze_does_not_mark_ready(tmp_path, monkeypatch):
    """(b) probe 於鎖外執行時完成交接凍結：即使 probe 成功，也不得把 ATTACHED 升為 READY。

    斷言：凍結後 state 仍為 ATTACHED、`_on_ready` 未被呼叫、last_error 不被清除，結果為零狀態改寫的拒絕。
    """
    svc = make_service(tmp_path, ready=["COM0"], port=FakePort())
    mgr = svc._sessions
    session = mgr.get_session("COM0")
    bridge = session.bridge
    with mgr._lock:
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"

    probe_started = threading.Event()
    release_probe = threading.Event()

    def slow_probe_ok(_bridge: Any, _profile: Any, *_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        probe_started.set()
        assert release_probe.wait(5.0), "測試未放行 probe"
        return True, None

    monkeypatch.setattr(sm, "probe_ready", slow_probe_ok)
    on_ready = mock.Mock()
    mgr._on_ready = on_ready
    out: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: out.update(mgr._probe_existing_bridge(session, bridge)), daemon=True,
    )
    worker.start()
    assert probe_started.wait(2.0), "probe 未開始"
    mgr.freeze_for_handoff()             # 無在途寫入：凍結立即成立，probe 仍在鎖外執行
    release_probe.set()
    worker.join(5.0)
    assert not worker.is_alive()
    assert mgr._handoff_frozen is True

    assert session.state == "ATTACHED", session.state
    assert session.last_error == "PROMPT_UNAVAILABLE", session.last_error
    on_ready.assert_not_called()
    assert out.get("ok") is False and out.get("error_code") == "FLUSHED_BY_SHUTDOWN", out
    assert out.get("reason") == "handoff_frozen", out
    svc.close_for_test()


def test_reconcile_tick_after_unlocked_check_handoff_freeze_changes_nothing(tmp_path, monkeypatch):
    """(c) tick 已通過頂部無鎖檢查後才完成交接：鎖內不得再改動任何 session 或 lease。

    斷言：DETACHED 不變 ATTACHING、reprobe_attempts 不增加、lease 不被到期收回、無 attach／probe 被拉起、零 TX。
    """
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0", "COM1", "COM2"], port=port)
    mgr = svc._sessions
    attached = mgr.get_session("COM0")   # 名義 ATTACHED＋prompt 錯誤：tick 會排 readiness probe
    leased = mgr.get_session("COM1")     # 持有已到期的 agent lease：tick 會收回
    detached = mgr.get_session("COM2")   # DETACHED＋PROMPT_TIMEOUT 且已有裝置：tick 會改為 ATTACHING
    for session in (attached, leased, detached):
        _attach_device(mgr, session, tmp_path)
    with mgr._lock:
        attached.state = "ATTACHED"
        attached.last_error = "PROMPT_UNAVAILABLE"
        detached.state = "DETACHED"
        detached.bridge = None
        detached.last_error = "PROMPT_TIMEOUT"
    opened = mgr.interactive_open("COM1", owner="agent:race", timeout_s=0.05)
    assert opened.get("ok") is True, opened
    lease = mgr._interactive[opened["interactive_id"]]
    time.sleep(0.1)                      # 使 lease 於 tick 時已到期
    attempts_before = (attached.reprobe_attempts, detached.reprobe_attempts)
    tx_before = list(port.writes)

    real_expire = mgr._expire_interactive_leases_tick

    def freeze_after_unlocked_check(*args: Any, **kwargs: Any) -> int:
        # 交接在 reconcile_readiness 頂部無鎖檢查之後、鎖內區段之前完成。
        mgr.freeze_for_handoff()
        return real_expire(*args, **kwargs)

    monkeypatch.setattr(mgr, "_expire_interactive_leases_tick", freeze_after_unlocked_check)
    spawn_attach = mock.Mock()
    spawn_probe = mock.Mock()
    monkeypatch.setattr(mgr, "_spawn_attach", spawn_attach)
    monkeypatch.setattr(mgr, "_spawn_reprobe_probe", spawn_probe)

    mgr.reconcile_readiness()

    assert detached.state == "DETACHED", detached.state
    assert detached.last_error == "PROMPT_TIMEOUT", detached.last_error
    assert (attached.reprobe_attempts, detached.reprobe_attempts) == attempts_before
    assert lease.status == "active", lease.status
    assert mgr.get_session("COM1").interactive_session_id == opened["interactive_id"]
    spawn_attach.assert_not_called()
    spawn_probe.assert_not_called()
    assert port.writes == tx_before, "tick 不得於凍結後送出 TX"
    svc.close_for_test()


def test_lease_self_heal_after_handoff_freeze_grants_nothing(tmp_path, monkeypatch):
    """(c2) 自癒區段：外部 peer 判定（鎖外）之後才完成交接凍結，鎖內仍不得授予 human lease。"""
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    mgr = svc._sessions
    session = mgr.get_session("COM0")
    bridge = session.bridge
    mgr._last_console_reap_at = -1e9     # 允許本 tick 執行 console 回收與自癒區段

    monkeypatch.setattr(bridge, "_enumerate_all_held_paths", lambda: None)
    monkeypatch.setattr(bridge, "reap_stale_consoles", lambda **_kwargs: None)
    monkeypatch.setattr(bridge, "snapshot", lambda: {
        "interactive_owner": None,
        "suspended_owner": None,
        "agent_active": False,
        "flash_mode": False,
        "primary_client_id": "race-primary",
        "vtty": None,
    })

    def peer_seen_then_freeze(_client_id: str) -> bool:
        mgr.freeze_for_handoff()         # 鎖外判定期間完成交接凍結
        return True

    monkeypatch.setattr(bridge, "console_has_external_peer", peer_seen_then_freeze)
    grant = mock.Mock(return_value=True)
    monkeypatch.setattr(bridge, "try_grant_interactive_if_idle", grant)

    mgr.reconcile_readiness()

    grant.assert_not_called()
    assert session.interactive_session_id is None
    assert port.writes == []
    svc.close_for_test()


def test_attaching_marked_before_freeze_reverts_to_detached_without_attach(tmp_path):
    """(c3) 鎖內已標記 ATTACHING，交接於 spawn 前完成：attach 不執行，狀態還原為 DETACHED。"""
    svc = make_service(tmp_path, ready=["COM2"], port=FakePort())
    mgr = svc._sessions
    detached = mgr.get_session("COM2")
    _attach_device(mgr, detached, tmp_path)
    by_id = detached.profile.device_by_id
    with mgr._lock:
        detached.state = "DETACHED"
        detached.bridge = None
        detached.last_error = "PROMPT_TIMEOUT"
        job = mgr._prepare_reprobe_locked(detached, time.monotonic())
    assert job is not None and job[0] == "attach", job
    assert detached.state == "ATTACHING"

    mgr.freeze_for_handoff()
    mgr._spawn_attach(by_id)

    assert detached.state == "DETACHED", detached.state
    assert by_id not in mgr._attach_threads
    assert by_id not in mgr._attach_inflight
    svc.close_for_test()


def test_attach_body_after_handoff_freeze_reverts_pre_marked_attaching(tmp_path):
    """(c3 補) attach 執行緒啟動後才遇到交接凍結：`_attach_by_id` 早退時同樣還原 ATTACHING。"""
    svc = make_service(tmp_path, ready=["COM2"], port=FakePort())
    mgr = svc._sessions
    detached = mgr.get_session("COM2")
    _attach_device(mgr, detached, tmp_path)
    by_id = detached.profile.device_by_id
    with mgr._lock:
        detached.state = "ATTACHING"
        detached.bridge = None
    mgr.freeze_for_handoff()

    mgr._attach_by_id(by_id)

    assert detached.state == "DETACHED", detached.state
    assert detached.bridge is None
    svc.close_for_test()
