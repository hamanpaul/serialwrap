"""Human console raw interactive mode 整合測試。

驗證方案 2（minicom 預設 raw interactive ownership）的核心行為：
- READY 狀態 console-attach 自動授予 interactive
- ESC 序列（方向鍵）在 raw mode 下正確透傳
- Agent 命令暫時掛起 human interactive 後執行
- Human 輸入在 agent 執行期間 deferred → 完成後 flush
- Console detach 釋放 interactive ownership
"""
from __future__ import annotations

import os
import pty
import select
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import InteractiveLease, SessionManager
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


class FakeTarget:
    """簡易 PTY 模擬 target：收集 TX 資料，可注入 RX 回應。"""

    def __init__(self) -> None:
        master, slave = pty.openpty()
        self.master_fd = master
        self.slave_fd = slave
        self.slave_path = os.ttyname(slave)
        self.received: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            rlist, _, _ = select.select([self.master_fd], [], [], 0.05)
            if rlist:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        self.received.append(data)
                except OSError:
                    break

    def emit(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    def collected(self) -> bytes:
        return b"".join(self.received)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        os.close(self.master_fd)
        os.close(self.slave_fd)


def _make_profile(com: str = "COM0", alias: str = "test") -> SessionProfile:
    return SessionProfile(
        profile_name="test-profile",
        com=com,
        act_no=0,
        alias=alias,
        platform="shell",
        device_by_id="/dev/serial/by-id/test",
        uart=UartProfile(),
        prompt_regex=r"(?m)^root@.*[#$]\s*$",
    )


class TestAttachConsoleInReadyGrantsInteractive(unittest.TestCase):
    """READY 狀態下 console-attach 應自動授予 interactive ownership。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from sw_core import session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        from sw_core import session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def test_attach_console_in_ready_grants_interactive(self) -> None:
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.attach_console.return_value = {
            "client_id": "cid-1", "label": "minicom:1", "vtty": "/dev/pts/99",
        }
        session.bridge.console_has_external_peer.return_value = True
        session.bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        session.state = "READY"

        resp = mgr.attach_console("COM0", label="minicom:1")

        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("interactive_owner"))
        self.assertIsNotNone(resp.get("interactive_session_id"))
        self.assertIsNotNone(session.interactive_session_id)

    def test_second_console_does_not_get_interactive(self) -> None:
        """已有 interactive lease 時，第二個 console 不應取得 ownership。"""
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.attach_console.side_effect = [
            {"client_id": "cid-1", "label": "first", "vtty": "/dev/pts/10"},
            {"client_id": "cid-2", "label": "second", "vtty": "/dev/pts/11"},
        ]
        session.bridge.console_has_external_peer.return_value = True
        session.bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        session.state = "READY"

        resp1 = mgr.attach_console("COM0", label="first")
        self.assertTrue(resp1.get("interactive_owner"))

        resp2 = mgr.attach_console("COM0", label="second")
        self.assertFalse(resp2.get("interactive_owner", False))

    def test_detach_console_releases_interactive(self) -> None:
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.attach_console.return_value = {
            "client_id": "cid-1", "label": "minicom:1", "vtty": "/dev/pts/99",
        }
        session.bridge.console_has_external_peer.return_value = True
        session.bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        session.bridge.detach_console.return_value = True
        session.bridge.vtty_path = "/dev/pts/99"
        session.state = "READY"

        mgr.attach_console("COM0", label="minicom:1")
        self.assertIsNotNone(session.interactive_session_id)

        mgr.detach_console("COM0", "cid-1")
        self.assertIsNone(session.interactive_session_id)


class TestEscapeSequencesPassThrough(unittest.TestCase):
    """方向鍵 / Tab 在 raw interactive mode 下正確透傳到 UART。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._target = FakeTarget()
        self.addCleanup(self._target.close)
        wal = WalWriter(wal_dir=self._tmp.name)
        self._bridge = UARTBridge(
            com="COM0",
            device_path=self._target.slave_path,
            profile=UartProfile(),
            wal=wal,
        )
        self._bridge.start()
        time.sleep(0.1)

    def tearDown(self) -> None:
        self._bridge.stop()

    def test_arrow_keys_pass_through_raw(self) -> None:
        """ESC [ A/B/C/D 在 raw mode 下完整送到 target。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        vtty_fd = os.open(console["vtty"], os.O_RDWR | os.O_NOCTTY)
        try:
            for seq in [b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"]:
                os.write(vtty_fd, seq)
            time.sleep(0.3)
        finally:
            os.close(vtty_fd)

        collected = self._target.collected()
        for seq in [b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"]:
            self.assertIn(seq, collected, f"target 應收到 {seq!r}")

    def test_tab_passes_through_raw(self) -> None:
        """Tab (0x09) 在 raw mode 下立即送到 target，不等 Enter。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        vtty_fd = os.open(console["vtty"], os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(vtty_fd, b"\x09")
            time.sleep(0.2)
        finally:
            os.close(vtty_fd)

        collected = self._target.collected()
        self.assertIn(b"\x09", collected, "target 應收到 Tab")


class TestAgentSuspendsHumanInteractive(unittest.TestCase):
    """Agent 命令到達時 suspend human interactive，完成後 resume。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from sw_core import session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        from sw_core import session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def test_agent_command_suspends_and_resumes(self) -> None:
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.console_has_external_peer.return_value = True
        bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        bridge.vtty_path = "/dev/pts/9"
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        bridge.rx_text_from.return_value = "hello\nroot@host:~# "
        session.bridge = bridge
        session.state = "READY"

        lease = InteractiveLease(
            interactive_id="lease-1",
            session_id=session.session_id,
            owner="human:cid-1",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        resp = mgr.execute_command("test-profile:COM0", "echo hello", "agent:1", "cmd-1", timeout_s=5.0)

        self.assertTrue(resp["ok"])
        bridge.suspend_interactive.assert_called_once()
        bridge.resume_interactive.assert_called_once()
        self.assertEqual(session.interactive_session_id, "lease-1",
                         "interactive lease 不應被關閉，只是暫時掛起")

    def test_human_input_deferred_during_agent_then_flushed(self) -> None:
        """Agent 執行期間 human 輸入進 deferred buffer，完成後 flush。"""
        tmp = self._tmp
        target = FakeTarget()
        self.addCleanup(target.close)
        wal = WalWriter(wal_dir=tmp.name)
        bridge = UARTBridge(
            com="COM0",
            device_path=target.slave_path,
            profile=UartProfile(),
            wal=wal,
        )
        bridge.start()
        self.addCleanup(bridge.stop)
        time.sleep(0.1)

        console = bridge.attach_console(label="human")
        cid = console["client_id"]
        bridge.set_interactive_owner(f"human:{cid}")

        bridge.suspend_interactive()

        vtty_fd = os.open(console["vtty"], os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(vtty_fd, b"deferred input\r")
            time.sleep(0.2)
        finally:
            os.close(vtty_fd)

        with bridge._state_lock:
            buf = bridge._deferred_buffers.get(cid, bytearray())
        self.assertIn(b"deferred input", bytes(buf))

        bridge.resume_interactive()
        time.sleep(0.3)

        collected = target.collected()
        self.assertIn(b"deferred input", collected, "flush 後 target 應收到 deferred 的內容")

    def test_interactive_restored_after_agent_command(self) -> None:
        """Agent 命令完成後 human console 恢復 raw mode。"""
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None

        suspend_calls: list[str] = []
        resume_calls: list[str] = []

        bridge = mock.MagicMock()
        bridge.console_has_external_peer.return_value = True
        bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        bridge.vtty_path = "/dev/pts/9"
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        bridge.rx_text_from.return_value = "output\nroot@host:~# "
        bridge.suspend_interactive.side_effect = lambda: suspend_calls.append("suspend")
        bridge.resume_interactive.side_effect = lambda: resume_calls.append("resume")
        session.bridge = bridge
        session.state = "READY"

        lease = InteractiveLease(
            interactive_id="lease-r",
            session_id=session.session_id,
            owner="human:cid-1",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        mgr.execute_command("test-profile:COM0", "ls", "agent:1", "cmd-r", timeout_s=5.0)

        self.assertEqual(suspend_calls, ["suspend"])
        self.assertEqual(resume_calls, ["resume"])
