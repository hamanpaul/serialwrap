from __future__ import annotations

import tempfile
import time
import unittest
from unittest import mock

from sw_core.config import UartProfile
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


class TestUartBridgeConsoleCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def test_list_consoles_prunes_stale_non_primary_client(self) -> None:
        primary = self._bridge.attach_console(label="primary")
        stale = self._bridge.attach_console(label="stale")
        with self._bridge._state_lock:
            self._bridge._clients[stale["client_id"]].attached_at = time.time() - 10

        with mock.patch.object(self._bridge, "_client_has_external_peer_locked", return_value=False):
            consoles = self._bridge.list_consoles()

        client_ids = {row["client_id"] for row in consoles}
        self.assertIn(primary["client_id"], client_ids)
        self.assertNotIn(stale["client_id"], client_ids)
        self.assertNotIn(stale["client_id"], self._bridge._clients)

    def test_list_consoles_keeps_new_client_during_grace_window(self) -> None:
        self._bridge.attach_console(label="primary")
        fresh = self._bridge.attach_console(label="fresh")

        with mock.patch.object(self._bridge, "_client_has_external_peer_locked", return_value=False):
            consoles = self._bridge.list_consoles()

        client_ids = {row["client_id"] for row in consoles}
        self.assertIn(fresh["client_id"], client_ids)

    def test_list_consoles_never_prunes_primary_client(self) -> None:
        primary = self._bridge.attach_console(label="primary")
        with self._bridge._state_lock:
            self._bridge._clients[primary["client_id"]].attached_at = time.time() - 10

        with mock.patch.object(self._bridge, "_client_has_external_peer_locked", return_value=False):
            consoles = self._bridge.list_consoles()

        client_ids = {row["client_id"] for row in consoles}
        self.assertIn(primary["client_id"], client_ids)


class TestUartBridgeSuspendResume(unittest.TestCase):
    """suspend_interactive / resume_interactive 的單元測試。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._sent: list[tuple[bytes, str]] = []

        def fake_send(data: bytes, *, source: str = "", cmd_id=None) -> None:
            self._sent.append((data, source))

        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )
        self._bridge.send_bytes = fake_send  # type: ignore[assignment]

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def test_suspend_switches_to_deferred_mode(self) -> None:
        """suspend 後 console input 從 suspended owner 應進 deferred buffer。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()

        with self._bridge._state_lock:
            self.assertTrue(self._bridge._agent_active)
            self.assertEqual(self._bridge._suspended_owner, f"human:{cid}")
            self.assertIsNone(self._bridge._interactive_owner)

        client = self._bridge._clients[cid]
        self._bridge._handle_console_rx(client, b"\x1b[A")

        with self._bridge._state_lock:
            buf = self._bridge._deferred_buffers.get(cid, bytearray())
        self.assertEqual(bytes(buf), b"\x1b[A")
        self.assertEqual(len(self._sent), 0, "deferred 不應 send_bytes")

    def test_resume_flushes_deferred_to_uart(self) -> None:
        """resume 應把 deferred buffer 一次性 flush 到 UART。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()
        client = self._bridge._clients[cid]
        self._bridge._handle_console_rx(client, b"echo hi\r")

        self._bridge.resume_interactive()

        self.assertTrue(any(data == b"echo hi\r" for data, _ in self._sent))
        with self._bridge._state_lock:
            self.assertFalse(self._bridge._agent_active)
            self.assertEqual(len(self._bridge._deferred_buffers), 0)

    def test_resume_restores_raw_mode(self) -> None:
        """resume 後 console input 應恢復 raw 透傳。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()
        self._bridge.resume_interactive()

        with self._bridge._state_lock:
            self.assertEqual(self._bridge._interactive_owner, f"human:{cid}")

        self._sent.clear()
        client = self._bridge._clients[cid]
        self._bridge._handle_console_rx(client, b"\x1b[B")
        self.assertTrue(any(data == b"\x1b[B" for data, _ in self._sent))

    def test_deferred_buffer_not_echoed_locally(self) -> None:
        """deferred 期間不做 local echo（human 打字不顯示）。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()
        client = self._bridge._clients[cid]

        with mock.patch.object(self._bridge, "_write_console_best_effort") as mock_write:
            self._bridge._handle_console_rx(client, b"hello")
            mock_write.assert_not_called()

    def test_non_suspended_console_stays_in_line_buffer(self) -> None:
        """agent active 時，非 suspended owner 的 console 仍走 line-buffer。"""
        owner_console = self._bridge.attach_console(label="owner")
        other_console = self._bridge.attach_console(label="other")
        owner_cid = owner_console["client_id"]
        other_cid = other_console["client_id"]
        self._bridge.set_interactive_owner(f"human:{owner_cid}")

        lines_received: list[str] = []
        self._bridge._on_console_line = lambda cid, line: lines_received.append(line)

        self._bridge.suspend_interactive()
        other_client = self._bridge._clients[other_cid]
        self._bridge._handle_console_rx(other_client, b"ls\n")

        self.assertEqual(lines_received, ["ls"])

    def test_suspend_when_no_interactive_is_noop(self) -> None:
        """沒有 interactive owner 時 suspend/resume 不崩潰。"""
        self._bridge.suspend_interactive()

        with self._bridge._state_lock:
            self.assertTrue(self._bridge._agent_active)
            self.assertIsNone(self._bridge._suspended_owner)

        self._bridge.resume_interactive()

        with self._bridge._state_lock:
            self.assertFalse(self._bridge._agent_active)
            self.assertIsNone(self._bridge._interactive_owner)
