"""Windows COM 到 localhost TCP 橋接器的替身測試，不開啟任何真實 COM。"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def _bridge_module():
    from realhw.bridge import com_tcp_bridge

    return com_tcp_bridge


class FakeSerial:
    def __init__(self) -> None:
        self._incoming: queue.Queue[bytes] = queue.Queue()
        self._pending = bytearray()
        self._written_lock = threading.Lock()
        self._written_bytes = bytearray()
        self.close_calls = 0

    def read(self, size: int) -> bytes:
        if not self._pending:
            try:
                self._pending.extend(self._incoming.get(timeout=0.02))
            except queue.Empty:
                return b""
        data = bytes(self._pending[:size])
        del self._pending[:size]
        return data

    def write(self, data: bytes) -> int:
        value = bytes(data)
        with self._written_lock:
            self._written_bytes.extend(value)
        return len(value)

    def feed_rx(self, data: bytes) -> None:
        self._incoming.put(bytes(data))

    def written(self) -> bytes:
        with self._written_lock:
            return bytes(self._written_bytes)

    def close(self) -> None:
        self.close_calls += 1


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except PermissionError:
            pass
        time.sleep(0.01)
    assert predicate(), "等待橋接器狀態逾時"


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    deadline = time.monotonic() + 2.0
    data = bytearray()
    while len(data) < size and time.monotonic() < deadline:
        try:
            chunk = sock.recv(size - len(data))
        except TimeoutError:
            continue
        if not chunk:
            break
        data.extend(chunk)
    assert len(data) == size, f"預期 {size} bytes，實際收到 {len(data)}"
    return bytes(data)


def test_tcp_bridge_transfers_bytes_rejects_second_client_and_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bridge_module()
    serial_port = FakeSerial()
    status_path = tmp_path / "bridge-status.json"
    bridge = module.TcpSerialBridge(
        serial_port,
        serial_port_name="COM20",
        status_path=status_path,
        port=0,
    )
    host, port = bridge.start()
    server_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    server_thread.start()
    original_read_text = Path.read_text
    permission_errors = 0

    def flaky_read_text(path: Path, *args, **kwargs):
        nonlocal permission_errors
        if path == status_path and permission_errors == 0:
            permission_errors += 1
            raise PermissionError(13, "原子替換窗口", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    first = socket.create_connection((host, port), timeout=1)
    first.settimeout(0.1)
    try:
        _wait_for(lambda: json.loads(status_path.read_text(encoding="utf-8"))["connected"])
        first.sendall(b"\x00\x01agent\r\n\xff")
        _wait_for(lambda: serial_port.written() == b"\x00\x01agent\r\n\xff")
        serial_port.feed_rx(b"\x00console\r\n\xfe")
        assert _recv_exact(first, len(b"\x00console\r\n\xfe")) == b"\x00console\r\n\xfe"

        second = socket.create_connection((host, port), timeout=1)
        second.settimeout(1)
        try:
            assert second.recv(1) == b"", "第二個同時連線必須被拒絕"
        finally:
            second.close()
        _wait_for(lambda: json.loads(status_path.read_text(encoding="utf-8"))["rejected_connections"] == 1)

        first.close()
        _wait_for(lambda: not json.loads(status_path.read_text(encoding="utf-8"))["connected"])
        assert serial_port.close_calls == 0, "TCP 中斷時不得關閉 COM handle"

        reconnected = socket.create_connection((host, port), timeout=1)
        reconnected.settimeout(0.1)
        try:
            _wait_for(lambda: json.loads(status_path.read_text(encoding="utf-8"))["connected"])
            reconnected.sendall(b"reconnect\x00")
            _wait_for(lambda: b"reconnect\x00" in serial_port.written())
            serial_port.feed_rx(b"again\xff")
            assert _recv_exact(reconnected, len(b"again\xff")) == b"again\xff"
        finally:
            reconnected.close()
    finally:
        first.close()
        bridge.close()
        server_thread.join(timeout=2)

    assert not server_thread.is_alive()
    assert serial_port.close_calls == 1
    report = json.loads(status_path.read_text(encoding="utf-8"))
    assert report["pid"] > 0
    assert report["serial_port"] == "COM20"
    assert report["serial_opened_at_utc"]
    assert report["bytes_tcp_to_serial"] == len(b"\x00\x01agent\r\n\xffreconnect\x00")
    assert report["bytes_serial_to_tcp"] == len(b"\x00console\r\n\xfeagain\xff")
    assert report["accepted_connections"] == 2
    assert report["rejected_connections"] == 1
    assert "serialwrapd" not in report["process_name"].lower()
    assert permission_errors == 1


def test_tcp_bridge_refuses_non_loopback_listener(tmp_path: Path) -> None:
    module = _bridge_module()
    with pytest.raises(ValueError, match="127.0.0.1"):
        module.TcpSerialBridge(
            FakeSerial(), serial_port_name="COM20", status_path=tmp_path / "unused.json",
            host="0.0.0.0",
        )


def test_windows_serial_configuration_is_115200_8n1_and_no_flow_control() -> None:
    module = _bridge_module()
    calls: list[dict[str, object]] = []
    fake_api = SimpleNamespace(
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
        Serial=lambda **kwargs: calls.append(kwargs) or object(),
    )

    module.open_windows_serial("COM20", serial_module=fake_api)

    assert calls == [{
        "port": "COM20",
        "baudrate": 115200,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 0.1,
        "write_timeout": 2.0,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }]
