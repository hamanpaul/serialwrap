"""#131 Fix C：session payload 曝光 Windows TCP console 端點。

issue #131 點 3：`session list`/`self-test` 看不出 COM ↔ console TCP port
對應，使用者只能 netstat 反查。bridge 層已有 ``console_endpoint()``
（#84 PORT-2），本檔驗證 ``SessionRuntime.to_public_dict()``：

- bridge 有 TCP console 端點 → payload 帶 ``console_endpoint``；
- 端點為 None（POSIX PTY 平台）或無 bridge → **key 不存在**（CLI 輸出
  ``sort_keys=True``，恆輸出 null 會改變所有 Linux JSON 輸出）。
"""
from __future__ import annotations

from unittest import mock

from sw_core.session_manager import SessionRuntime


def _session(bridge) -> SessionRuntime:
    profile = mock.Mock()
    profile.profile_name = "prpl-template"
    profile.com = "COM0"
    profile.alias = None
    profile.act_no = None
    profile.device_by_id = None
    profile.platform = "prpl"
    profile.command_capable = True
    session = SessionRuntime(session_id="s-131", profile=profile)
    session.bridge = bridge
    return session


def _bridge(endpoint: str | None) -> mock.Mock:
    bridge = mock.Mock()
    bridge.list_consoles.return_value = []
    bridge.console_endpoint.return_value = endpoint
    return bridge


def test_console_endpoint_exposed_when_present() -> None:
    payload = _session(_bridge("127.0.0.1:52085")).to_public_dict()
    assert payload["console_endpoint"] == "127.0.0.1:52085"


def test_key_absent_when_bridge_has_no_endpoint() -> None:
    """POSIX（PTY 平台）bridge 的 console_endpoint() 恆 None → 不加 key。"""
    payload = _session(_bridge(None)).to_public_dict()
    assert "console_endpoint" not in payload


def test_key_absent_when_no_bridge() -> None:
    payload = _session(None).to_public_dict()
    assert "console_endpoint" not in payload
