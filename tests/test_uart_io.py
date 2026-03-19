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
