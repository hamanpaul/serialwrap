"""OD-63 佔位 session 的 `session bind` 路徑（sol 增量審查 5b05626..9990b18 B 段）。

- S1-9990b18-1：`session bind` 對佔位走既有 RELEASED 解除路徑，丟棄阻擋、寫 binding
  override 後以佔位的 passthrough profile 真實 attach。依 OD-63，佔位只能經
  `device attach` 解除後交 template 偵測；bind 比照 `session attach` 的 RELEASED 早退，
  不解除阻擋、不寫 override、不開埠。

修正前：bind 回 `ok:true` 並以 passthrough 佔位 profile 開埠。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sw_core.device_watcher import DeviceInfo
from test_s1x_od63_released_placeholder import (
    BY_ID,
    SID,
    _manager,
    _no_holder,
    _persisted_released,
    _plug,
    _seed_state,
)

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

OTHER_BY_ID = "/dev/serial/by-id/usb-NEW"


class _CountingPort:
    """記錄 open 次數；``error`` 不為 None 時 open 拋出該例外。"""

    def __init__(self, opens: list[str], path: str, error: BaseException | None) -> None:
        self._opens = opens
        self._path = path
        self._error = error

    def open(self) -> None:
        self._opens.append(self._path)
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        return None


def _patch_open(
    monkeypatch: pytest.MonkeyPatch, opens: list[str], error: BaseException | None = None
) -> None:
    import sw_core.uart_io as uart_io_mod

    monkeypatch.setattr(
        uart_io_mod,
        "open_serial_port",
        lambda path, *_a, **_k: _CountingPort(opens, path, error),
    )


def _join_attach_workers(mgr: Any) -> None:
    for worker in list(mgr._attach_threads.values()):
        worker.join(timeout=5)


@pytest.mark.parametrize("target", [BY_ID, OTHER_BY_ID], ids=["same_by_id", "other_by_id"])
def test_session_bind_on_placeholder_keeps_block_without_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """S1-9990b18-1：對佔位 session bind 比照 RELEASED 早退，不解除阻擋、不寫 override、零開埠。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    with mgr._lock:
        mgr._devices[OTHER_BY_ID] = DeviceInfo(by_id=OTHER_BY_ID, real_path="/dev/ttyFAKE2")
    _no_holder(monkeypatch, mgr)
    opens: list[str] = []
    _patch_open(monkeypatch, opens)

    result = mgr.bind_session("COM3", target)
    _join_attach_workers(mgr)

    assert result["ok"] is True, result
    assert result.get("released") is True, result
    assert result.get("recommended_action") == "device_attach", result
    assert result["session"]["state"] == "RELEASED", result
    assert opens == [], "佔位不得被 bind 以 passthrough profile 開埠"
    s = mgr.get_session("COM3")
    assert s is not None and s.session_id == SID
    assert s.released_placeholder is True
    assert s.state == "RELEASED"
    assert s.bridge is None
    assert s.profile.device_by_id == BY_ID
    assert BY_ID in mgr._released_by_ids
    assert SID not in mgr._binding_overrides
    assert SID in _persisted_released(tmp_path)


def test_session_bind_on_placeholder_leaves_device_attach_handoff_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bind 早退後，`device attach COM3` 仍照 OD-63 交 template 偵測（保留 COM3）。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    opens: list[str] = []
    _patch_open(monkeypatch, opens)
    dynamic_calls: list[str] = []
    monkeypatch.setattr(
        mgr, "_attach_by_id_dynamic", lambda by_id, **_kw: dynamic_calls.append(by_id)
    )

    assert mgr.bind_session("COM3", BY_ID)["ok"] is True
    result = mgr.attach_device("COM3")
    _join_attach_workers(mgr)

    assert result["ok"] is True, result
    assert dynamic_calls == [BY_ID]
    assert SID not in mgr._sessions
    assert BY_ID not in mgr._released_by_ids
    assert mgr._pending_com.get(BY_ID) == "COM3"


def test_attach_by_id_never_builds_bridge_for_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """防線：即使阻擋已被解除且狀態非 RELEASED，`_attach_by_id` 也不得以佔位 profile 開埠。"""
    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    opens: list[str] = []
    _patch_open(monkeypatch, opens)
    with mgr._lock:
        s = mgr._sessions[SID]
        mgr._released_by_ids.discard(BY_ID)
        s.state = "DETACHED"

    mgr._attach_by_id(BY_ID)

    assert opens == []
    assert mgr._sessions[SID].bridge is None
    assert BY_ID not in mgr._attach_bridges
