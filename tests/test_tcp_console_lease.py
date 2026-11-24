"""#275：Windows TCP console 的 raw 擁有權在 session 層補記鏡像 lease。

Windows TCP console（#84 PORT-2）在 accept 時由 bridge 直接授予 raw 擁有權，不經
SessionManager，session 因此沒有 lease：``human_attached``／``human_active`` 恆為 false、
人類打字時 reprobe 不會暫緩。本檔以 MagicMock bridge 驗 session 層的鏡像規則：

- 沒有 lease 且 bridge 回報 TCP console owner → 補記 ``bridge_owned`` lease；
- bridge 回報非字串（MagicMock 預設）或 None → 不補記（PTY console 一律經 console-attach）；
- owner 換人（斷線後新連線）或消失 → 只撤鏡像、**不**呼叫 ``set_interactive_owner``，
  避免清掉新連線的 raw 擁有權；
- ``human_active`` 依鏡像 lease 與最後鍵入時間判定。

真 socket 的端到端案例見 ``tests/test_serial_port.py::TestUARTBridgeTcpConsole``。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _profile() -> SessionProfile:
    return SessionProfile(
        profile_name="test-profile",
        com="COM0",
        act_no=0,
        alias="test",
        platform="passthrough",
        device_by_id="COM4",
        uart=UartProfile(),
        prompt_regex=r"(?m)^root@flybox:[^\r\n#]*#[ \t]*",
    )


class TestTcpConsoleLeaseMirror(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import sw_core.session_manager as sm_mod

        patcher = mock.patch.object(sm_mod, "STATE_PATH", str(Path(self._tmp.name) / "state.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = SessionManager(
            [_profile()],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None,
            on_detached=lambda _: None,
        )
        session = self.mgr.get_session("COM0")
        assert session is not None
        self.session = session
        self.bridge = mock.MagicMock()
        self.bridge.console_has_external_peer.return_value = True
        self.session.bridge = self.bridge
        self.session.state = "READY"

    def _refresh(self):
        with self.mgr._lock:
            lease, _post = self.mgr._refresh_interactive_locked(self.session)
        return lease

    def _set_owner(self, owner: str | None, *, last_input: float | None = None) -> None:
        self.bridge.tcp_console_owner.return_value = owner
        self.bridge.snapshot.return_value = {
            "interactive_owner": owner,
            "last_human_input_at": last_input,
        }

    def test_adopts_tcp_console_owner_as_bridge_owned_lease(self) -> None:
        self._set_owner("human:c1")
        lease = self._refresh()
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.owner, "human:c1")
        self.assertTrue(lease.bridge_owned)
        self.assertEqual(self.session.interactive_session_id, lease.interactive_id)
        self.bridge.set_interactive_owner.assert_not_called()
        # 再 refresh 沿用同一筆，不重複建立。
        self.assertIs(self._refresh(), lease)

    def test_non_string_owner_is_not_adopted(self) -> None:
        # MagicMock 預設回傳 MagicMock（PTY bridge 回 None）：不得誤補 lease。
        self.bridge.snapshot.return_value = {"interactive_owner": "human:c1"}
        self.assertIsNone(self._refresh())
        self.assertIsNone(self.session.interactive_session_id)

    def test_not_adopted_when_session_not_ready_or_handoff_frozen(self) -> None:
        self._set_owner("human:c1")
        self.session.state = "DETACHED"
        self.assertIsNone(self._refresh())
        self.session.state = "READY"
        self.mgr._handoff_frozen = True
        try:
            self.assertIsNone(self._refresh())
        finally:
            self.mgr._handoff_frozen = False

    def test_reconnect_drops_old_mirror_without_clearing_bridge_owner(self) -> None:
        self._set_owner("human:c1")
        old = self._refresh()
        assert old is not None
        # c1 斷線、c2 在 grace 窗內連入並取得 raw 擁有權。
        self._set_owner("human:c2")
        new = self._refresh()
        assert new is not None
        self.assertEqual(new.owner, "human:c2")
        self.assertTrue(new.bridge_owned)
        self.assertNotEqual(new.interactive_id, old.interactive_id)
        self.assertNotIn(old.interactive_id, self.mgr._interactive)
        # 一般 close 會 set_interactive_owner(None) 清掉 c2；鏡像 lease 不得這樣做。
        self.bridge.set_interactive_owner.assert_not_called()

    def test_disconnect_drops_mirror(self) -> None:
        self._set_owner("human:c1")
        lease = self._refresh()
        assert lease is not None
        self._set_owner(None)
        self.assertIsNone(self._refresh())
        self.assertIsNone(self.session.interactive_session_id)
        self.assertNotIn(lease.interactive_id, self.mgr._interactive)
        self.bridge.set_interactive_owner.assert_not_called()

    def test_lease_context_reports_human_attached_and_active(self) -> None:
        self._set_owner("human:c1", last_input=time.monotonic())
        lease = self._refresh()
        ctx = self.mgr._lease_context(lease, bridge=self.bridge)
        self.assertTrue(ctx["human_attached"])
        self.assertTrue(ctx["human_active"])

    def test_human_active_locked_adopts_before_judging(self) -> None:
        # reprobe 暫緩判定直接讀 session.interactive_session_id，須先補記鏡像。
        self._set_owner("human:c1", last_input=time.monotonic())
        with self.mgr._lock:
            self.assertTrue(self.mgr._human_active_locked(self.session, time.monotonic()))
        self.assertIsNotNone(self.session.interactive_session_id)

    def test_human_idle_is_not_active(self) -> None:
        self._set_owner("human:c1", last_input=time.monotonic() - 3600.0)
        with self.mgr._lock:
            self.assertFalse(self.mgr._human_active_locked(self.session, time.monotonic()))
        lease = self._refresh()
        ctx = self.mgr._lease_context(lease, bridge=self.bridge)
        self.assertTrue(ctx["human_attached"])
        self.assertFalse(ctx["human_active"])


class TestTcpConsoleLeasePublicPaths(TestTcpConsoleLeaseMirror):
    """公開路徑：self_test 回報、interactive_open 的 soft preempt／BUSY 與還原。"""

    def _set_owner(self, owner: str | None, *, last_input: float | None = None) -> None:
        self.bridge.tcp_console_owner.return_value = owner
        self.bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "127.0.0.1:50000",
            "interactive_owner": owner,
            "last_human_input_at": last_input,
        }
        self.bridge.detach_console.return_value = True
        self.bridge.vtty_path = "127.0.0.1:50000"

    def test_self_test_reports_tcp_console_human(self) -> None:
        self._set_owner("human:c1", last_input=time.monotonic())
        result = self.mgr.self_test("COM0")
        self.assertTrue(result["human_attached"], result)
        self.assertTrue(result["human_active"], result)

    def test_interactive_open_soft_preempts_idle_tcp_human(self) -> None:
        self._set_owner("human:c1", last_input=time.monotonic() - 3600.0)
        resp = self.mgr.interactive_open("COM0", owner="agent")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp.get("soft_preempted"))
        stash = self.session._stashed_human_lease
        self.assertIsNotNone(stash)
        assert stash is not None
        self.assertTrue(stash.bridge_owned)
        self.assertEqual(stash.owner, "human:c1")

        close = self.mgr.interactive_close(resp["interactive_id"])
        self.assertTrue(close["ok"], close)
        self.assertEqual(self.session.interactive_session_id, stash.interactive_id)
        self.bridge.set_interactive_owner.assert_any_call("human:c1")

    def test_interactive_open_busy_while_tcp_human_active(self) -> None:
        self._set_owner("human:c1", last_input=time.monotonic() - 1.0)
        resp = self.mgr.interactive_open("COM0", owner="agent")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_INTERACTIVE_BUSY")
