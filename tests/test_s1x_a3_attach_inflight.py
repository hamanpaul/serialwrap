"""S1X-a-3：attach 進行中再呼叫 `device attach` 應回 ok＋session（0.3.2 行為）。

Stage 1 出口審查 A 指出 `attach_device` 只要 by_id 在 `_attach_ops`／`_preopened_ports`／
`_active_attach_ops`／`_attach_bridges` 任一表內就回 `SESSION_RECOVERING`；但
`_attach_bridges` 在整段 probe／login 期間都持有 entry，此時沒有任何 drain、state 也是
ATTACHING。0.3.2 回 `{"ok":true,"session":{...,"state":"ATTACHING"}}`。
`SESSION_RECOVERING` 只保留給 drain／release 進行中（SC-04）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import sw_core.session_manager as sm_mod
from stage0_fakes import FakePort, make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/X"


def _detached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    from sw_core.device_watcher import DeviceInfo
    import sw_core.uart_io as uart_io

    mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
    session = mgr.get_session("COM0")
    assert session is not None
    monkeypatch.setattr(uart_io, "open_serial_port", lambda *_a, **_k: FakePort())
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path=str(tmp_path / "fake-uart"))
    monkeypatch.setattr(mgr, "_probe_external_holder", lambda _path: {"pids": []})
    with mgr._lock:
        session.bridge = None
        session.state = "DETACHED"
    return mgr, session


def _wait(cond: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_second_attach_during_login_returns_ok_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一次 attach 回 ATTACHING 後立刻再呼叫一次：回 ok＋session（state=ATTACHING），非 SESSION_RECOVERING。"""
    mgr, session = _detached(tmp_path, monkeypatch)
    probe_started = threading.Event()
    probe_continue = threading.Event()

    def slow_probe(_bridge: Any, _profile: Any) -> tuple[bool, str]:
        probe_started.set()
        probe_continue.wait(3.0)
        return False, "PROMPT_TIMEOUT"

    monkeypatch.setattr(sm_mod, "probe_ready", slow_probe)
    try:
        first = mgr.attach_device("COM0")
        assert first["ok"] is True, first
        assert probe_started.wait(3.0), "attach worker 未進入 probe"
        assert BY_ID in mgr._attach_bridges or BY_ID in mgr._active_attach_ops

        second = mgr.attach_device("COM0")

        assert second.get("error_code") is None, second
        assert second["ok"] is True, second
        assert second["session"]["state"] == "ATTACHING"
        assert second["session"]["com"] == "COM0"
    finally:
        probe_continue.set()
        _wait(lambda: BY_ID not in mgr._attach_bridges and BY_ID not in mgr._active_attach_ops)
        with mgr._lock:
            bridge = session.bridge
        if bridge is not None:
            bridge.stop()


@pytest.mark.parametrize("table", ["_attach_ops", "_preopened_ports", "_active_attach_ops", "_attach_bridges"])
def test_inflight_attach_tables_without_drain_return_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    """四表任一命中、但無 pending drain／draining／release 請求且非 RELEASED：回 ok＋session。"""
    mgr, session = _detached(tmp_path, monkeypatch)
    spawned: list[str] = []
    monkeypatch.setattr(mgr, "_spawn_attach", lambda by_id: spawned.append(by_id))
    op = sm_mod._AttachOp(
        token=object(), session_id=session.session_id, by_id=BY_ID,
        bridge_generation=session.bridge_generation, real_path="fake",
    )
    with mgr._lock:
        session.state = "ATTACHING"
        getattr(mgr, table)[BY_ID] = op if table != "_attach_bridges" else object()

    result = mgr.attach_device("COM0")

    assert result.get("error_code") is None, result
    assert result["ok"] is True
    assert result["session"]["state"] == "ATTACHING"
    assert spawned == []


def test_inflight_attach_with_release_request_keeps_recovering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attach op 已帶 release 請求（release 進行中）：仍回 SESSION_RECOVERING（SC-04）。"""
    mgr, session = _detached(tmp_path, monkeypatch)
    op = sm_mod._AttachOp(
        token=object(), session_id=session.session_id, by_id=BY_ID,
        bridge_generation=session.bridge_generation, real_path="fake",
    )
    op.release_request = ("cli", None)
    with mgr._lock:
        session.state = "RECOVERING"
        mgr._active_attach_ops[BY_ID] = op

    result = mgr.attach_device("COM0")

    assert result["ok"] is False
    assert result["error_code"] == "SESSION_RECOVERING"


def test_inflight_attach_while_draining_keeps_recovering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """by_id 正在 drain：仍回 SESSION_RECOVERING。"""
    mgr, session = _detached(tmp_path, monkeypatch)
    with mgr._lock:
        session.state = "ATTACHING"
        mgr._attach_bridges[BY_ID] = object()
        mgr._draining_by_ids[BY_ID] = object()  # type: ignore[assignment]

    result = mgr.attach_device("COM0")

    assert result["ok"] is False
    assert result["error_code"] == "SESSION_RECOVERING"
