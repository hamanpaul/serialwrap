"""#131 Fix C：session payload 曝光 Windows TCP console 端點。

issue #131 點 3：`session list`/`self-test` 看不出 COM ↔ console TCP port
對應，使用者只能 netstat 反查。bridge 層已有 ``console_endpoint()``
（#84 PORT-2），本檔驗證 ``SessionRuntime.to_public_dict()``：

- 無 PTY 平台（Windows）：key **恆輸出**（bridge 未起時為 None），schema 跨
  session 狀態穩定，consumer 不需處理 key 忽有忽無；
- 有 PTY 平台（POSIX）：僅在端點非 None 時輸出（實務上恆 None → key 不存在，
  Linux JSON 輸出逐位元組不變；CLI 輸出 ``sort_keys=True``，恆輸出 null 會改變
  所有既有輸出）。
"""
from __future__ import annotations

from unittest import mock

import pytest

import sw_core.session_manager as sm
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


def test_console_endpoint_exposed_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_pty_available", lambda: False)
    payload = _session(_bridge("127.0.0.1:52085")).to_public_dict()
    assert payload["console_endpoint"] == "127.0.0.1:52085"


def test_key_always_present_on_non_pty_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows：bridge 未起（DETACHED/ATTACHING）也輸出 key=None → schema 穩定。"""
    monkeypatch.setattr(sm, "_pty_available", lambda: False)
    assert _session(None).to_public_dict()["console_endpoint"] is None
    assert _session(_bridge(None)).to_public_dict()["console_endpoint"] is None


def test_key_absent_on_pty_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX（PTY 平台）端點恆 None → 不加 key，Linux 輸出逐位元組不變。"""
    monkeypatch.setattr(sm, "_pty_available", lambda: True)
    assert "console_endpoint" not in _session(_bridge(None)).to_public_dict()
    assert "console_endpoint" not in _session(None).to_public_dict()
