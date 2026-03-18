from __future__ import annotations

import os
import pty
import select
import subprocess
import tempfile
import threading
import time
import unittest

from sw_core.config import UartProfile
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


class FakeTarget:
    def __init__(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_path = os.ttyname(self.slave_fd)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.received: list[bytes] = []

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def emit(self, payload: bytes) -> None:
        os.write(self.master_fd, payload)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rlist, _, _ = select.select([self.master_fd], [], [], 0.1)
            except OSError:
                return
            if self.master_fd not in rlist:
                continue
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                return
            if chunk:
                self.received.append(chunk)


class TestUARTBridgeConsoles(unittest.TestCase):
    def _make_target(self) -> FakeTarget:
        try:
            return FakeTarget()
        except OSError as exc:
            self.skipTest(f"pty not available in current environment: {exc}")

    def test_console_attach_creates_unique_vtty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = self._make_target()
            target.start()
            self.addCleanup(target.stop)

            bridge = UARTBridge("COM0", target.slave_path, UartProfile(), WalWriter(wal_dir=td))
            bridge.start()
            self.addCleanup(bridge.stop)

            primary = bridge.vtty_path
            attached = bridge.attach_console(label="observer")

            self.assertIsNotNone(primary)
            self.assertNotEqual(primary, attached["vtty"])
            self.assertEqual(len(bridge.list_consoles()), 2)

    def test_console_input_is_line_buffered_for_broker_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = self._make_target()
            target.start()
            self.addCleanup(target.stop)

            captured: list[tuple[str, str]] = []
            bridge = UARTBridge(
                "COM0",
                target.slave_path,
                UartProfile(),
                WalWriter(wal_dir=td),
                on_console_line=lambda client_id, line: captured.append((client_id, line)),
            )
            bridge.start()
            self.addCleanup(bridge.stop)

            primary = bridge.vtty_path
            assert primary is not None
            fd = os.open(primary, os.O_RDWR | os.O_NOCTTY)
            try:
                os.write(fd, b"ifconfig\n")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not captured:
                    time.sleep(0.05)
            finally:
                os.close(fd)

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0][1], "ifconfig")
            self.assertEqual(target.received, [])

    def test_console_input_supports_backspace_and_local_echo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = self._make_target()
            target.start()
            self.addCleanup(target.stop)

            captured: list[tuple[str, str]] = []
            bridge = UARTBridge(
                "COM0",
                target.slave_path,
                UartProfile(),
                WalWriter(wal_dir=td),
                on_console_line=lambda client_id, line: captured.append((client_id, line)),
            )
            bridge.start()
            self.addCleanup(bridge.stop)

            primary = bridge.vtty_path
            assert primary is not None
            fd = os.open(primary, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            echoed = b""
            try:
                os.write(fd, b"abc\x7fd\n")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and (not captured or b"abc\x08 \x08d\r\n" not in echoed):
                    try:
                        echoed += os.read(fd, 4096)
                    except BlockingIOError:
                        pass
                    time.sleep(0.05)
            finally:
                os.close(fd)

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0][1], "abd")
            self.assertIn(b"abc\x08 \x08d\r\n", echoed)
            self.assertEqual(target.received, [])

    def test_rx_is_fanned_out_to_all_consoles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = self._make_target()
            target.start()
            self.addCleanup(target.stop)

            bridge = UARTBridge("COM0", target.slave_path, UartProfile(), WalWriter(wal_dir=td))
            bridge.start()
            self.addCleanup(bridge.stop)

            attached = bridge.attach_console(label="observer")
            primary = bridge.vtty_path
            assert primary is not None

            fd_primary = os.open(primary, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            fd_second = os.open(attached["vtty"], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                target.emit(b"hello\r\n# ")
                deadline = time.monotonic() + 2.0
                primary_data = b""
                second_data = b""
                while time.monotonic() < deadline and (not primary_data or not second_data):
                    try:
                        primary_data += os.read(fd_primary, 4096)
                    except BlockingIOError:
                        pass
                    try:
                        second_data += os.read(fd_second, 4096)
                    except BlockingIOError:
                        pass
                    time.sleep(0.05)
            finally:
                os.close(fd_primary)
                os.close(fd_second)

            self.assertIn(b"hello", primary_data)
            self.assertIn(b"hello", second_data)

    def test_unread_console_does_not_block_human_line_submission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = self._make_target()
            target.start()
            self.addCleanup(target.stop)

            captured: list[tuple[str, str]] = []
            bridge = UARTBridge(
                "COM0",
                target.slave_path,
                UartProfile(),
                WalWriter(wal_dir=td),
                on_console_line=lambda client_id, line: captured.append((client_id, line)),
            )
            bridge.start()
            self.addCleanup(bridge.stop)

            # Leave all console slaves unread, then push enough RX data to
            # pressure the PTY buffers. Human line input must still reach the
            # callback instead of being blocked behind fan-out writes.
            bridge.attach_console(label="idle-observer")
            for _ in range(32):
                target.emit(b"x" * 4096)
                time.sleep(0.01)

            primary = bridge.vtty_path
            assert primary is not None
            fd = os.open(primary, os.O_RDWR | os.O_NOCTTY)
            try:
                os.write(fd, b"echo still-works\n")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not captured:
                    time.sleep(0.05)
            finally:
                os.close(fd)

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0][1], "echo still-works")

    def test_console_external_peer_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = self._make_target()
            target.start()
            self.addCleanup(target.stop)

            bridge = UARTBridge("COM0", target.slave_path, UartProfile(), WalWriter(wal_dir=td))
            bridge.start()
            self.addCleanup(bridge.stop)

            attached = bridge.attach_console(label="observer")
            client_id = attached["client_id"]

            self.assertFalse(bridge.console_has_external_peer(client_id))

            proc = subprocess.Popen(
                [
                    os.environ.get("PYTHON", "python3"),
                    "-c",
                    "import os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY); time.sleep(1.5); os.close(fd)",
                    attached["vtty"],
                ]
            )
            try:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not bridge.console_has_external_peer(client_id):
                    time.sleep(0.05)
                self.assertTrue(bridge.console_has_external_peer(client_id))
            finally:
                proc.wait(timeout=3.0)

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and bridge.console_has_external_peer(client_id):
                time.sleep(0.05)
            self.assertFalse(bridge.console_has_external_peer(client_id))


if __name__ == "__main__":
    unittest.main()
