"""tests/test_serial_port.py — SerialPort port 抽象（#84 PORT-1）。

涵蓋：
- 跨平台：importability（Windows 不再因 top-level termios import 失敗）、factory 後端選擇、
  device 名稱正規化、SerialPort 介面契約。
- POSIX-only：以 PTY 當假 device 對 PosixSerialPort 做 open/configure/read/write/close 往返
  （驗證 termios 後端與 uart_io 原行為等價）。
- Windows 真機（env-gated）：對 SERIALWRAP_TEST_SERIAL_PORT 指向、且 TxRx 短接（loopback）的
  序列埠驗證 pyserial 後端真實往返。
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from sw_core.config import UartProfile
from sw_core import serial_port as sp
from sw_core.serial_port import SerialPort, open_serial_port


class TestImportability(unittest.TestCase):
    """#84 PORT-1 的核心目標：序列埠模組在無 termios/fcntl 的平台（Windows）仍可 import。"""

    def test_uart_io_and_serial_port_import(self) -> None:
        import sw_core.serial_port  # noqa: F401
        import sw_core.uart_io as u

        self.assertTrue(hasattr(u, "UARTBridge"))

    def test_no_toplevel_posix_only_imports_in_uart_io(self) -> None:
        # uart_io 不得在 top-level import termios/fcntl（否則 Windows import 即炸）。
        import sw_core.uart_io as u

        self.assertNotIn("termios", dir(u))
        self.assertNotIn("fcntl", dir(u))

    def test_uart_io_source_has_no_toplevel_posix_imports(self) -> None:
        # 比 dir() 更強：直接解析 AST，連 aliased（import termios as t）與 from-import 都擋。
        import ast
        import inspect

        import sw_core.uart_io as u

        tree = ast.parse(inspect.getsource(u))
        offenders: list[str] = []
        for node in tree.body:  # 僅檢查 top-level（函式內 lazy import 允許）
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name in ("termios", "fcntl")]
            elif isinstance(node, ast.ImportFrom) and node.module in ("termios", "fcntl"):
                offenders.append(node.module)
        self.assertEqual(offenders, [], f"uart_io top-level 不應 import POSIX-only 模組：{offenders}")

    def test_serial_port_top_level_imports_on_any_platform(self) -> None:
        # serial_port.py 的 termios/fcntl/_BAUD_MAP 須在 os.name=='posix' 守衛內，
        # 使非 POSIX 平台 import 不炸；此處驗證 import 成功即足（POSIX 上守衛為真亦不應炸）。
        import importlib

        mod = importlib.import_module("sw_core.serial_port")
        self.assertTrue(hasattr(mod, "open_serial_port"))


class TestWinDeviceName(unittest.TestCase):
    def test_bare_com_gets_prefix(self) -> None:
        self.assertEqual(sp._win_device_name("COM8"), r"\\.\COM8")
        self.assertEqual(sp._win_device_name("com10"), r"\\.\COM10")

    def test_already_prefixed_unchanged(self) -> None:
        self.assertEqual(sp._win_device_name(r"\\.\COM12"), r"\\.\COM12")

    def test_non_com_unchanged(self) -> None:
        self.assertEqual(sp._win_device_name("/dev/ttyUSB0"), "/dev/ttyUSB0")
        self.assertEqual(sp._win_device_name("loop://"), "loop://")


class TestBackendSelection(unittest.TestCase):
    def test_explicit_posix(self) -> None:
        port = open_serial_port("/dev/null", UartProfile(), backend="posix")
        self.assertEqual(type(port).__name__, "_PosixSerialPort")
        self.assertIsInstance(port, SerialPort)

    def test_explicit_pyserial(self) -> None:
        port = open_serial_port("COM8", UartProfile(), backend="pyserial")
        self.assertEqual(type(port).__name__, "_PySerialPort")
        self.assertIsInstance(port, SerialPort)
        # 尚未 open → 無可多工 fd。
        self.assertIsNone(port.fileno())

    def test_env_override(self) -> None:
        old = os.environ.get("SERIALWRAP_SERIAL_BACKEND")
        try:
            os.environ["SERIALWRAP_SERIAL_BACKEND"] = "pyserial"
            self.assertEqual(sp._select_backend(None), "pyserial")
            os.environ["SERIALWRAP_SERIAL_BACKEND"] = "posix"
            self.assertEqual(sp._select_backend(None), "posix")
        finally:
            if old is None:
                os.environ.pop("SERIALWRAP_SERIAL_BACKEND", None)
            else:
                os.environ["SERIALWRAP_SERIAL_BACKEND"] = old

    def test_explicit_arg_beats_env(self) -> None:
        old = os.environ.get("SERIALWRAP_SERIAL_BACKEND")
        try:
            os.environ["SERIALWRAP_SERIAL_BACKEND"] = "pyserial"
            self.assertEqual(sp._select_backend("posix"), "posix")
        finally:
            if old is None:
                os.environ.pop("SERIALWRAP_SERIAL_BACKEND", None)
            else:
                os.environ["SERIALWRAP_SERIAL_BACKEND"] = old

    def test_auto_default_is_posix_on_posix(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX 平台才驗 auto→posix")
        old = os.environ.pop("SERIALWRAP_SERIAL_BACKEND", None)
        try:
            self.assertEqual(sp._select_backend(None), "posix")
        finally:
            if old is not None:
                os.environ["SERIALWRAP_SERIAL_BACKEND"] = old


def _read_until(port: SerialPort, deadline_s: float) -> bytes:
    """非阻塞重讀直到有資料或逾時（POSIX 後端 read 在無資料時拋 BlockingIOError）。"""
    buf = bytearray()
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            chunk = port.read(256)
        except BlockingIOError:
            chunk = b""
        if chunk:
            buf.extend(chunk)
            if buf.endswith(b"\n"):
                break
        else:
            time.sleep(0.01)
    return bytes(buf)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"), "PosixSerialPort 需 POSIX + PTY")
class TestPosixSerialPortRoundtrip(unittest.TestCase):
    """以 PTY 當假 device 驗證 termios 後端 open/configure/read/write/close。"""

    def setUp(self) -> None:
        self._master, self._slave = os.openpty()
        self._dev = os.ttyname(self._slave)
        self._port = open_serial_port(self._dev, UartProfile(), backend="posix")

    def tearDown(self) -> None:
        try:
            self._port.close()
        except Exception:  # noqa: BLE001
            pass
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_open_configure_alive_fileno(self) -> None:
        self._port.open()
        self._port.configure(UartProfile())
        self.assertIsInstance(self._port.fileno(), int)
        self.assertTrue(self._port.is_alive())

    def test_master_to_port_read(self) -> None:
        self._port.open()
        self._port.configure(UartProfile())
        os.write(self._master, b"hello-rx\n")
        data = _read_until(self._port, 1.0)
        self.assertIn(b"hello-rx", data)

    def test_port_to_master_write(self) -> None:
        self._port.open()
        self._port.configure(UartProfile())
        self._port.write(b"hello-tx\n")
        time.sleep(0.05)
        got = os.read(self._master, 256)
        self.assertIn(b"hello-tx", got)

    def test_close_marks_not_alive(self) -> None:
        self._port.open()
        self._port.configure(UartProfile())
        self._port.close()
        self.assertFalse(self._port.is_alive())
        self.assertIsNone(self._port.fileno())

    def test_configure_various_profiles_runs(self) -> None:
        # 覆蓋 configure 對 data_bits/parity/stop_bits/flow_control/xonxoff 各分支的程式碼路徑
        # （#84 review #5）。PTY 不保證回讀 cflag，故只驗「套用不拋例外」；termios cflag/baud
        # 真值驗證交給 env-gated 真機測試（TestPySerialLoopback）。
        self._port.open()
        for prof in (
            UartProfile(baud=9600, data_bits=7, parity="E", stop_bits=2, flow_control="rtscts"),
            UartProfile(baud=115200, data_bits=8, parity="O", stop_bits=1),
            UartProfile(baud=57600, data_bits=8, parity="N", stop_bits=1, xonxoff=True),
        ):
            self._port.configure(prof)
            self.assertTrue(self._port.is_alive())

    def test_set_baud_runs(self) -> None:
        self._port.open()
        self._port.configure(UartProfile(baud=115200))
        self._port.set_baud(9600)  # 不拋例外；真值（B9600）驗證交給 env-gated 真機 test_set_baud
        self.assertTrue(self._port.is_alive())


@unittest.skipUnless(
    os.environ.get("SERIALWRAP_TEST_SERIAL_PORT"),
    "需設 SERIALWRAP_TEST_SERIAL_PORT 指向 TxRx 短接的真機序列埠",
)
class TestPySerialLoopback(unittest.TestCase):
    """真機 loopback：對 SERIALWRAP_TEST_SERIAL_PORT（TxRx 短接）驗 pyserial 後端往返。"""

    def setUp(self) -> None:
        self._dev = os.environ["SERIALWRAP_TEST_SERIAL_PORT"]
        self._port = open_serial_port(self._dev, UartProfile(), backend="pyserial")
        self._port.open()
        self._port.configure(UartProfile())

    def tearDown(self) -> None:
        self._port.close()

    def test_fileno_is_none(self) -> None:
        self.assertIsNone(self._port.fileno())

    def test_alive(self) -> None:
        self.assertTrue(self._port.is_alive())

    def test_loopback_roundtrip(self) -> None:
        msg = b"serialport-loopback-0123456789\n"
        self._port.write(msg)
        got = bytearray()
        end = time.monotonic() + 2.0
        while len(got) < len(msg) and time.monotonic() < end:
            got.extend(self._port.read(256))
        self.assertEqual(bytes(got[: len(msg)]), msg)

    def test_idle_read_returns_empty(self) -> None:
        # drain 後 idle read 應在 read timeout 內回 b""（不拋例外、不永久阻塞）。
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not self._port.read(256):
                break
        t0 = time.monotonic()
        out = self._port.read(256)
        dt = time.monotonic() - t0
        self.assertEqual(out, b"")
        self.assertLess(dt, 1.0)

    def test_set_baud(self) -> None:
        self._port.set_baud(9600)
        self._port.set_baud(115200)
        self._port.write(b"after-baud\n")
        got = bytearray()
        end = time.monotonic() + 2.0
        while b"\n" not in got and time.monotonic() < end:
            got.extend(self._port.read(256))
        self.assertIn(b"after-baud", bytes(got))


@unittest.skipUnless(
    os.environ.get("SERIALWRAP_TEST_SERIAL_PORT"),
    "需設 SERIALWRAP_TEST_SERIAL_PORT 指向 TxRx 短接的真機序列埠",
)
class TestUARTBridgeLoopback(unittest.TestCase):
    """真機 loopback：以完整 UARTBridge 驗證 Windows/pyserial 路徑的 start/RX/TX/WAL/stop。"""

    def setUp(self) -> None:
        import tempfile

        from sw_core.uart_io import UARTBridge
        from sw_core.wal import WalWriter

        self._dev = os.environ["SERIALWRAP_TEST_SERIAL_PORT"]
        self._tmp = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmp.name)
        self._bridge = UARTBridge(self._dev, self._dev, UartProfile(), wal)
        self._bridge.start()

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmp.cleanup()

    def test_snapshot_alive_running(self) -> None:
        snap = self._bridge.snapshot()
        self.assertTrue(snap["serial_alive"])
        self.assertTrue(snap["running"])

    def test_command_echo_via_wait_for_regex(self) -> None:
        self._bridge.clear_rx_buffer()
        self._bridge.send_command("PING-12345", source="agent")
        self.assertTrue(self._bridge.wait_for_regex("PING-12345", timeout_s=2.0))

    def test_binary_byte_exact_echo(self) -> None:
        self._bridge.clear_rx_buffer()
        self._bridge.send_bytes(bytes([0x55, 0x00, 0xAA, 0x41, 0x42, 0x43, 0x0A]), source="agent")
        time.sleep(0.4)
        self.assertIn("ABC", self._bridge.rx_tail(64))

    def test_clean_stop_is_prompt(self) -> None:
        t0 = time.monotonic()
        self._bridge.stop()
        self.assertLess(time.monotonic() - t0, 2.5)


class _FakeSerialPort(SerialPort):
    """測試替身：模擬「無 selectable fd」的後端（Windows/pyserial 語意，fileno()=None）。

    loopback=True 時 write() 把 bytes 回灌入 read 佇列（模擬 TxRx 短接），供 TCP console
    端到端測試在 Linux CI（無實機）驗 console→UART→回波→console。執行緒安全。
    """

    def __init__(self, *, loopback: bool = False) -> None:
        self._loopback = loopback
        self._lock = threading.Lock()
        self.opened = False
        self.configured = False
        self.closed = False
        self.written = bytearray()
        self.baud: int | None = None
        self._alive = False
        self._rx = bytearray()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)

    def open(self) -> None:
        self.opened = True
        self._alive = True

    def configure(self, profile: UartProfile) -> None:
        self.configured = True

    def read(self, max_bytes: int = 8192) -> bytes:
        with self._lock:
            if self._rx:
                chunk = bytes(self._rx[:max_bytes])
                del self._rx[:max_bytes]
                return chunk
        time.sleep(0.02)  # 模擬 read timeout（讓 _loop 可檢查 stop_event）
        return b""

    def write(self, payload: bytes) -> None:
        self.written.extend(payload)
        if self._loopback:
            with self._lock:
                self._rx.extend(payload)

    def close(self) -> None:
        self.closed = True
        self._alive = False

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def is_alive(self) -> bool:
        return self._alive

    def fileno(self) -> int | None:
        return None


def _drain_sock(sock: socket.socket, deadline_s: float = 0.3) -> None:
    sock.settimeout(0.1)
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            if not sock.recv(4096):
                break
        except (socket.timeout, OSError):
            break


def _recv_until(sock: socket.socket, needle: bytes, timeout_s: float = 2.0) -> bytes:
    sock.settimeout(0.2)
    got = b""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        got += chunk
        if needle in got:
            break
    return got


class TestUARTBridgeWindowsPath(unittest.TestCase):
    """以 fake SerialPort（fileno()=None）驗證 UARTBridge 的 Windows/無-PTY 路徑（免 pyserial/實機）。"""

    def setUp(self) -> None:
        import sw_core.uart_io as uart_io
        from sw_core.wal import WalWriter

        self._uart_io = uart_io
        self._tmp = tempfile.TemporaryDirectory()
        self._wal = WalWriter(wal_dir=self._tmp.name)
        self._fake = _FakeSerialPort()
        # 模擬 Windows：注入 fake 後端、且平台無 PTY。
        self._patches = [
            mock.patch.object(uart_io, "open_serial_port", return_value=self._fake),
            mock.patch.object(uart_io, "_pty_available", return_value=False),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _bridge(self):
        from sw_core.uart_io import UARTBridge

        return UARTBridge("COM9", "COM9", UartProfile(), self._wal)

    def test_start_skips_console_and_has_no_selectable_fd(self) -> None:
        b = self._bridge()
        b.start()
        try:
            self.assertIs(b._serial, self._fake)
            self.assertIsNone(b._serial_fd)  # 無 selectable fd → _loop 走 Windows 分支
            self.assertEqual(b._clients, {})  # 無 PTY → 不建 human console
            self.assertTrue(self._fake.opened)
            self.assertTrue(self._fake.configured)
        finally:
            b.stop()
        self.assertTrue(self._fake.closed)  # stop() 經 port.close()

    def test_loop_windows_branch_routes_rx(self) -> None:
        b = self._bridge()
        b.start()
        try:
            self._fake.feed(b"hello-windows-rx\n")
            self.assertTrue(b.wait_for_regex("hello-windows-rx", timeout_s=2.0))
        finally:
            b.stop()

    def test_send_bytes_uses_port_write_and_preserves_flash_gate(self) -> None:
        b = self._bridge()
        b.start()
        try:
            b.send_bytes(b"cmd\n", source="agent")
            self.assertEqual(bytes(self._fake.written), b"cmd\n")
            # #69 flash gate 在 Windows 寫入路徑亦須生效：FLASHING 期間非 flash 來源被丟棄。
            b.set_flash_mode(True)
            b.send_bytes(b"pwn\n", source="system")
            self.assertEqual(bytes(self._fake.written), b"cmd\n")  # 未新增
            b.flash_tx(b"\x55")  # flasher 內部能力仍可寫
            self.assertEqual(bytes(self._fake.written), b"cmd\n\x55")
            b.set_flash_mode(False)
        finally:
            b.stop()

    def test_snapshot_uses_port_is_alive(self) -> None:
        b = self._bridge()
        b.start()
        try:
            snap = b.snapshot()
            self.assertTrue(snap["serial_alive"])  # serial_fd is None → 走 port.is_alive()
            self.assertIsNone(snap["vtty"])
            self.assertEqual(snap["consoles"], [])
        finally:
            b.stop()


def _pyserial_available() -> bool:
    try:
        import serial  # noqa: F401

        return True
    except ImportError:
        return False


@unittest.skipUnless(_pyserial_available(), "需安裝 pyserial 才能驗 _PySerialPort 內部行為")
class TestPySerialPortUnit(unittest.TestCase):
    """以 fake pyserial Serial 物件驗 _PySerialPort 內部邏輯（read drain / 例外轉譯 / set_baud），免實機。"""

    def _port(self):
        return open_serial_port("COM9", UartProfile(), backend="pyserial")

    def test_read_drains_in_waiting(self) -> None:
        class FakeSer:
            is_open = True

            def __init__(self) -> None:
                self._buf = bytearray(b"ABCDEF")

            def read(self, n):
                chunk = bytes(self._buf[:n])
                del self._buf[:n]
                return chunk

            @property
            def in_waiting(self):
                return len(self._buf)

        p = self._port()
        p._ser = FakeSer()
        self.assertEqual(p.read(8192), b"ABCDEF")  # read(1)='A' 再依 in_waiting drain 'BCDEF'

    def test_read_idle_returns_empty(self) -> None:
        class FakeSer:
            is_open = True

            def read(self, n):
                return b""

            @property
            def in_waiting(self):
                return 0

        p = self._port()
        p._ser = FakeSer()
        self.assertEqual(p.read(8192), b"")

    def test_write_timeout_maps_to_oserror_etimedout(self) -> None:
        import errno

        import serial

        class FakeSer:
            is_open = True

            def write(self, data):
                raise serial.SerialTimeoutException("timeout")

        p = self._port()
        p._ser = FakeSer()
        with self.assertRaises(OSError) as cm:
            p.write(b"x")
        self.assertEqual(cm.exception.errno, errno.ETIMEDOUT)

    def test_write_serialexception_maps_to_oserror(self) -> None:
        import serial

        class FakeSer:
            is_open = True

            def write(self, data):
                raise serial.SerialException("boom")

        p = self._port()
        p._ser = FakeSer()
        with self.assertRaises(OSError):
            p.write(b"x")

    def test_read_serialexception_maps_to_oserror(self) -> None:
        import serial

        class FakeSer:
            is_open = True

            def read(self, n):
                raise serial.SerialException("disconnected")

            @property
            def in_waiting(self):
                return 0

        p = self._port()
        p._ser = FakeSer()
        with self.assertRaises(OSError):
            p.read(8192)

    def test_is_alive_reflects_is_open(self) -> None:
        class FakeSer:
            def __init__(self) -> None:
                self.is_open = True

        p = self._port()
        p._ser = FakeSer()
        self.assertTrue(p.is_alive())
        p._ser.is_open = False
        self.assertFalse(p.is_alive())

    def test_set_baud_updates_baudrate_and_rebuilds_full_profile(self) -> None:
        class FakeSer:
            is_open = True
            baudrate = 115200

        p = self._port()
        p._ser = FakeSer()
        p.set_baud(9600)
        self.assertEqual(p._ser.baudrate, 9600)
        # profile 重建須保留所有欄位（不漏 xonxoff/data_bits 等）。
        self.assertEqual(p._profile.baud, 9600)
        self.assertEqual(p._profile.data_bits, 8)
        self.assertEqual(p._profile.parity, "N")
        self.assertFalse(p._profile.xonxoff)


class TestUARTBridgeTcpConsole(unittest.TestCase):
    """Windows TCP human console（#84 PORT-2）端到端：loopback fake serial + 真 localhost socket，
    免 PTY/pyserial/實機，可於 Linux CI 執行。驗 raw ownership、雙向透傳、agent coexistence
    （suspend defer / resume flush，連線不斷）、斷線偵測。
    """

    def setUp(self) -> None:
        import sw_core.uart_io as uart_io
        from sw_core.uart_io import UARTBridge
        from sw_core.wal import WalWriter

        self._uart_io = uart_io
        self._tmp = tempfile.TemporaryDirectory()
        self._fake = _FakeSerialPort(loopback=True)
        self._patches = [
            mock.patch.object(uart_io, "open_serial_port", return_value=self._fake),
            mock.patch.object(uart_io, "_pty_available", return_value=False),
        ]
        for p in self._patches:
            p.start()
        self._bridge = UARTBridge("COM9", "COM9", UartProfile(), WalWriter(wal_dir=self._tmp.name))
        self._bridge.start()

    def tearDown(self) -> None:
        try:
            self._bridge.stop()
        except Exception:  # noqa: BLE001
            pass
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _connect(self) -> socket.socket:
        ep = self._bridge.console_endpoint()
        host, port = ep.rsplit(":", 1)
        s = socket.create_connection((host, int(port)), timeout=2)
        time.sleep(0.2)  # 待 accept + raw ownership 授予
        return s

    def test_endpoint_advertised(self) -> None:
        ep = self._bridge.console_endpoint()
        self.assertTrue(ep and ep.startswith("127.0.0.1:"))
        self.assertEqual(self._bridge.snapshot()["console_endpoint"], ep)

    def test_first_console_gets_raw_ownership(self) -> None:
        s = self._connect()
        try:
            owner = self._bridge.snapshot()["interactive_owner"]
            self.assertTrue(owner and owner.startswith("human:"))
        finally:
            s.close()

    def test_socket_to_uart_loopback_echo(self) -> None:
        s = self._connect()
        try:
            _drain_sock(s)
            s.sendall(b"PING-CON\r")
            self.assertIn(b"PING-CON", _recv_until(s, b"PING-CON", 2.0))
        finally:
            s.close()

    def test_agent_coexistence_defer_and_flush(self) -> None:
        s = self._connect()
        try:
            _drain_sock(s)
            self._bridge.suspend_interactive()  # 模擬 agent 命令開始
            s.sendall(b"during-agent\r")  # 應被 defer
            time.sleep(0.2)
            self._bridge.send_command("ACMD", source="agent")
            self.assertTrue(self._bridge.wait_for_regex("ACMD", timeout_s=2.0))
            self._bridge.resume_interactive()  # flush deferred → UART → loopback → socket
            self.assertIn(b"during-agent", _recv_until(s, b"during-agent", 2.0))
            # 連線全程保持
            s.sendall(b"still-alive\r")
        finally:
            s.close()

    def test_disconnect_drops_console(self) -> None:
        s = self._connect()
        self.assertEqual(len(self._bridge.snapshot()["consoles"]), 1)
        s.close()
        dropped = False
        for _ in range(40):
            if self._bridge.snapshot()["consoles"] == []:
                dropped = True
                break
            time.sleep(0.05)
        self.assertTrue(dropped)


if __name__ == "__main__":
    unittest.main()
