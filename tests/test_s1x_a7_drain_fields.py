"""S1X-a-7：release／clear 的 drain 收尾後，session 對外欄位須與 0.3.2 相同。

Stage 1 出口審查 A 指出 `_complete_drain` 沒有沿用 0.3.2 的欄位更新：
- `session clear`（裝置在場）：0.3.2 為 `state:ATTACHING, last_error:null`；0.4.0 留下 `"CLEARED"`。
- `device release`：0.3.2 會更新 `detached_at` 並重置 `reprobe_attempts`／`reprobe_exhausted`；
  0.4.0 保留舊值。

oracle 取自 v0.3.2 `release_device`／`clear_session`／`_detach_session_locked` 的欄位寫入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage0_fakes import FakePort, make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/X"
OLD_TS = "2000-01-01T00:00:00Z"


def _ready(tmp_path: Path) -> tuple[Any, Any]:
    from sw_core.config import UartProfile
    from sw_core.device_watcher import DeviceInfo
    from sw_core.uart_io import UARTBridge

    mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
    s = mgr.get_session("COM0")
    assert s is not None
    bridge = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=mgr._wal)
    bridge._serial = FakePort()
    bridge._serial_fd = None
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="fake-uart")
    with mgr._lock:
        s.bridge = bridge
        s.state = "READY"
        s.bridge_generation = 4
        s.detached_at = OLD_TS
        s.reprobe_attempts = 2
        s.reprobe_exhausted = True
        s.last_error = "OLD_ERROR"
    return mgr, s


def test_release_refreshes_detached_at_and_resets_reprobe(tmp_path: Path) -> None:
    """release 收尾：detached_at 更新、reprobe 進度歸零、last_error=RELEASED（0.3.2 欄位）。"""
    mgr, s = _ready(tmp_path)

    result = mgr.release_device("COM0")

    assert result["ok"] is True, result
    public = mgr.get_session("COM0").to_public_dict()
    assert public["state"] == "RELEASED"
    assert public["detached_at"] not in (None, OLD_TS)
    assert public["reprobe_attempts"] == 0
    assert public["reprobe_exhausted"] is False
    assert public["last_error"] == "RELEASED"


def test_clear_with_device_present_clears_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear（裝置在場）收尾：state=ATTACHING、last_error=None（0.3.2 欄位）。"""
    mgr, s = _ready(tmp_path)
    spawned: list[str] = []
    monkeypatch.setattr(mgr, "_spawn_attach", lambda by_id: spawned.append(by_id))

    result = mgr.clear_session("COM0")

    assert result["ok"] is True, result
    public = mgr.get_session("COM0").to_public_dict()
    assert public["state"] == "ATTACHING"
    assert public["last_error"] is None
    assert public["detached_at"] not in (None, OLD_TS)
    assert public["reprobe_attempts"] == 0
    assert spawned == [BY_ID]


def test_clear_without_device_keeps_cleared(tmp_path: Path) -> None:
    """clear（裝置不在場）收尾：state=DETACHED、last_error=CLEARED（0.3.2 欄位）。"""
    mgr, s = _ready(tmp_path)
    mgr._devices.pop(BY_ID)

    result = mgr.clear_session("COM0")

    assert result["ok"] is True, result
    public = mgr.get_session("COM0").to_public_dict()
    assert public["state"] == "DETACHED"
    assert public["last_error"] == "CLEARED"
