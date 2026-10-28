"""P7 Task 9（08-slice23-login-events.md）parity-amendments-2026-10-26 #117 的 Python 側特性測試（p7_gate:py_parity）。

鎖定 Rust `recover_p7::recover_resets_credentials_warned`／`recover_not_ready_non_force_returns_not_ready` 所承接的
Python 基線行為（`sw_core/session_manager.py` `recover_session`）。
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tests.stage0_fakes import make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_recover_not_ready_non_force_returns_not_ready(tmp_path: Path) -> None:
    """有 bridge、state 非 READY／ATTACHED 且非 force → SESSION_NOT_READY＋session，state 不變、不 probe。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "ATTACHING"
    s.bridge = mock.MagicMock()
    with mock.patch.object(mgr, "_probe_existing_bridge") as probe, mock.patch.object(mgr, "_force_recover") as force:
        r = mgr.recover_session("COM0", force=False)
    assert list(r.keys()) == ["ok", "error_code", "session"]
    assert r["ok"] is False and r["error_code"] == "SESSION_NOT_READY"
    assert s.state == "ATTACHING"
    probe.assert_not_called()
    force.assert_not_called()


def test_recover_resets_credentials_warned(tmp_path: Path) -> None:
    """手動 recover 為重新介入（#140）：通過 drain／RELEASED／FLASHING 早退後即 `credentials_warned = False`。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "ATTACHED"
    s.bridge = mock.MagicMock()
    s.credentials_warned = True
    seen: list[bool] = []

    def probe(sess, _bridge):
        seen.append(sess.credentials_warned)
        return {"ok": True, "session": sess.to_public_dict()}

    with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=probe):
        r = mgr.recover_session("COM0", force=False)
    assert seen == [False], "再探前 credentials_warned 已重置"
    assert r["action"] == "REPROBE"


def test_released_recover_does_not_reset_credentials_warned(tmp_path: Path) -> None:
    """RELEASED 早退（C2）不重置 credentials_warned 與 reprobe 進度。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "RELEASED"
    s.credentials_warned = True
    s.reprobe_attempts = 3
    r = mgr.recover_session("COM0", force=False)
    assert r["ok"] is True and r["released"] is True and r["recommended_action"] == "device_attach"
    assert s.credentials_warned is True
    assert s.reprobe_attempts == 3
