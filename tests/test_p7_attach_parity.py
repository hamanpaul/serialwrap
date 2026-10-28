"""P7 Task 17（08-slice23-login-events.md）`attach_state_result_mapping` 的 Python 側特性測試（p7_gate:py_parity）。

鎖定 Rust `session_core_login::attach_state_result_mapping` 所承接的基線：bridge 存在時 `attach_session` 對
FLASHING／RECOVERING（無 pending drain）走 `_attach_state_result`，回應鍵為 `ok, error_code, session`
（無 `selector`、無 drain 欄位）。
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tests.stage0_fakes import make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


@pytest.mark.parametrize("state,code", [("FLASHING", "FLASHING_BUSY"), ("RECOVERING", "SESSION_RECOVERING")])
def test_attach_with_bridge_state_result_shape(tmp_path: Path, state: str, code: str) -> None:
    from sw_core.device_watcher import DeviceInfo

    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    mgr._devices[s.profile.device_by_id] = DeviceInfo(by_id=s.profile.device_by_id, real_path="/dev/ttyUSB0")
    s.bridge = mock.MagicMock()
    s.state = state
    with mock.patch.object(mgr, "_probe_existing_bridge") as probe:
        r = mgr.attach_session("COM0")
    assert list(r.keys()) == ["ok", "error_code", "session"]
    assert r["ok"] is False and r["error_code"] == code
    assert s.state == state
    probe.assert_not_called()
