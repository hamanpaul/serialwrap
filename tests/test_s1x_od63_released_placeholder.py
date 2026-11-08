"""OD-63：template 已不存在的 RELEASED 紀錄以佔位 session 解除（S1X-a-8）。

擁有者裁決（OD-63，2026-10-29；審查 A 修正回報 OWNER-BATCH #2 方案 (A) 變形）：
- template 已不存在的 RELEASED 紀錄實體化為 RELEASED 佔位 session（passthrough、
  `command_capable:false`），by_id 仍被阻擋，跨重啟保留（DF-04）。
- `device attach COMx` 時佔位先移除自身並解除 by_id，再交 template 偵測建立正式 session
  （不降級 profile；無 template 可匹配時依現行 dynamic 偵測）。
- 不新增 RPC／CLI 表面。

修正前：佔位不會建立，`device attach COM3` 回 `SESSION_NOT_FOUND`，by_id 永久被阻擋。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sw_core.config import ProfileTemplate, SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/usb-Z"
SID = "gone-template:COM3"


def _templates() -> list[ProfileTemplate]:
    return [
        ProfileTemplate(
            profile_name="shell-template",
            platform="shell",
            prompt_regex=r"(?m)^# ",
            ready_probe="echo __READY__${nonce}",
            uart=UartProfile(),
        )
    ]


def _seed_state(tmp_path: Path, *, extra: dict[str, Any] | None = None) -> None:
    state = {
        "aliases": {},
        "bindings": {},
        "released": {
            SID: {
                "by_id": BY_ID,
                "released_by": "user",
                "released_at": "2026-10-29T00:00:00Z",
                "reason": "flash",
            }
        },
        "profile_pins": {},
        "profile_detected": {BY_ID: "gone-template"},
    }
    state.update(extra or {})
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _manager(
    tmp_path: Path,
    *,
    profiles: list[SessionProfile] | None = None,
    templates: list[ProfileTemplate] | None = None,
) -> SessionManager:
    return SessionManager(
        profiles or [],
        WalWriter(wal_dir=str(tmp_path / "wal")),
        on_ready=lambda _s: None,
        on_detached=lambda _s: None,
        templates=_templates() if templates is None else templates,
        state_path=str(tmp_path / "state.json"),
    )


def _persisted_released(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["released"]


class _FakePort:
    def __init__(self) -> None:
        self.closed = 0

    def open(self) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


def _plug(mgr: SessionManager) -> None:
    with mgr._lock:
        mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="/dev/ttyFAKE")


def _no_holder(monkeypatch: pytest.MonkeyPatch, mgr: SessionManager) -> None:
    monkeypatch.setattr(mgr, "_probe_external_holder", lambda _path: {"pids": []})


def test_orphan_released_record_materializes_placeholder(tmp_path: Path) -> None:
    """template 不存在的 RELEASED 紀錄：建立 RELEASED 佔位（passthrough、不可下命令），by_id 仍阻擋。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)

    s = mgr.get_session("COM3")
    assert s is not None, "佔位 session 未建立"
    assert s.session_id == SID
    assert s.state == "RELEASED"
    pub = s.to_public_dict()
    assert pub["platform"] == "passthrough"
    assert pub["command_capable"] is False
    assert pub["device_by_id"] == BY_ID
    assert s.released_by == "user"
    assert BY_ID in mgr._released_by_ids
    assert SID in _persisted_released(tmp_path)


def test_placeholder_persists_across_restarts(tmp_path: Path) -> None:
    """佔位跨重啟保留（DF-04）：再次載入仍為 RELEASED 佔位、紀錄仍在 state.json。"""
    _seed_state(tmp_path)
    _manager(tmp_path)
    mgr2 = _manager(tmp_path)

    s = mgr2.get_session("COM3")
    assert s is not None
    assert s.state == "RELEASED"
    assert BY_ID in mgr2._released_by_ids
    assert SID in _persisted_released(tmp_path)


def test_placeholder_rejects_commands(tmp_path: Path) -> None:
    """佔位不可下命令：檔案傳輸回 passthrough 錯誤、命令執行不得送出。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)

    push = mgr.file_push("COM3", local_path=str(tmp_path / "x"), remote_path="/tmp/x")
    assert push["ok"] is False
    assert push["error_code"] == "PROFILE_NOT_COMMAND_CAPABLE", push
    s = mgr.get_session("COM3")
    assert s is not None and s.bridge is None and s.state == "RELEASED"


def test_hotplug_keeps_placeholder_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """裝置熱插不會自行解除佔位阻擋（RELEASED 語意）。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    spawned: list[str] = []
    monkeypatch.setattr(mgr, "_attach_by_id", lambda by_id, **_kw: spawned.append(by_id))

    mgr.update_devices({BY_ID: DeviceInfo(by_id=BY_ID, real_path="/dev/ttyFAKE")})

    s = mgr.get_session("COM3")
    assert s is not None and s.state == "RELEASED"
    assert BY_ID in mgr._released_by_ids
    assert spawned == []


def test_device_attach_placeholder_hands_off_to_template_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """device attach COM3：佔位移除自身、解除 by_id，交 template 偵測（保留 COM3），回 ok＋session。"""
    import sw_core.uart_io as uart_io_mod

    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    port = _FakePort()
    monkeypatch.setattr(uart_io_mod, "open_serial_port", lambda *_a, **_k: port)
    dynamic_calls: list[tuple[str, Any]] = []

    def fake_dynamic(by_id: str, *, preopened_op: Any = None) -> None:
        dynamic_calls.append((by_id, preopened_op))
        with mgr._lock:
            s = mgr._session_from_template(_templates()[0], by_id)
            s.profile_source = "detected"

    monkeypatch.setattr(mgr, "_attach_by_id_dynamic", fake_dynamic)

    result = mgr.attach_device("COM3")

    assert result["ok"] is True, result
    assert result["session"]["com"] == "COM3"
    for worker in list(mgr._attach_threads.values()):
        worker.join(timeout=5)
    assert SID not in mgr._sessions, "佔位必須移除自身"
    assert BY_ID not in mgr._released_by_ids
    assert SID not in mgr._loaded_released
    assert SID not in _persisted_released(tmp_path)
    assert [call[0] for call in dynamic_calls] == [BY_ID]
    assert dynamic_calls[0][1] is not None and dynamic_calls[0][1].port is port
    formal = mgr.get_session("COM3")
    assert formal is not None
    assert formal.profile.profile_name == "shell-template"
    assert formal.profile.command_capable is True


def test_handoff_skips_detached_slot_rebind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """佔位交接一律走 template 偵測，不把 by_id 綁進其他 DETACHED 空槽（不降級 profile）。"""
    import sw_core.uart_io as uart_io_mod

    _seed_state(tmp_path)
    other = SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/absent",
        platform="prpl",
        uart=UartProfile(),
    )
    mgr = _manager(tmp_path, profiles=[other])
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    monkeypatch.setattr(uart_io_mod, "open_serial_port", lambda *_a, **_k: _FakePort())
    dynamic_calls: list[str] = []
    monkeypatch.setattr(
        mgr, "_attach_by_id_dynamic", lambda by_id, **_kw: dynamic_calls.append(by_id)
    )

    result = mgr.attach_device("COM3")

    assert result["ok"] is True, result
    for worker in list(mgr._attach_threads.values()):
        worker.join(timeout=5)
    assert dynamic_calls == [BY_ID]
    slot = mgr.get_session("COM0")
    assert slot is not None and slot.profile.device_by_id == "/dev/serial/by-id/absent"


def test_device_attach_placeholder_without_device_keeps_block(tmp_path: Path) -> None:
    """裝置不在線：回 DEVICE_NOT_PRESENT，佔位與阻擋保留。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)

    result = mgr.attach_device("COM3")

    assert result["ok"] is False
    assert result["error_code"] == "DEVICE_NOT_PRESENT", result
    assert SID in mgr._sessions
    assert BY_ID in mgr._released_by_ids


def test_device_attach_placeholder_held_keeps_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """外部持有者存在：回 DEVICE_STILL_HELD，佔位與阻擋保留。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    monkeypatch.setattr(mgr, "_probe_external_holder", lambda _path: {"pids": [4242]})

    result = mgr.attach_device("COM3")

    assert result["ok"] is False
    assert result["error_code"] == "DEVICE_STILL_HELD", result
    assert SID in mgr._sessions
    assert BY_ID in mgr._released_by_ids


def test_restart_after_handoff_no_longer_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """交接後重啟：不再出現佔位，by_id 不再被阻擋。"""
    import sw_core.uart_io as uart_io_mod

    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    monkeypatch.setattr(uart_io_mod, "open_serial_port", lambda *_a, **_k: _FakePort())
    monkeypatch.setattr(mgr, "_attach_by_id_dynamic", lambda by_id, **_kw: None)
    assert mgr.attach_device("COM3")["ok"] is True
    for worker in list(mgr._attach_threads.values()):
        worker.join(timeout=5)

    mgr2 = _manager(tmp_path)
    assert SID not in mgr2._sessions
    assert BY_ID not in mgr2._released_by_ids
