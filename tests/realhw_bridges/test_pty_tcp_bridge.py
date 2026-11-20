"""WSL PTY 到 localhost TCP 橋接器測試；只用暫存 PTY 與 loopback socket。"""

from __future__ import annotations

import os
import queue
import select
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest


pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]
ROOT = Path(__file__).resolve().parents[2]


def _pty_module():
    from realhw.bridge import pty_tcp_bridge

    return pty_tcp_bridge


class LocalPeer:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.accepted: queue.Queue[socket.socket] = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(0.1)
            self.accepted.put(conn)

    def next_connection(self, timeout: float = 2.0) -> socket.socket:
        return self.accepted.get(timeout=timeout)

    def close(self) -> None:
        self.stop_event.set()
        self.listener.close()
        self.thread.join(timeout=1)
        while True:
            try:
                self.accepted.get_nowait().close()
            except queue.Empty:
                break


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "等待 PTY 橋接器狀態逾時"


def _read_exact(fd: int, size: int) -> bytes:
    deadline = time.monotonic() + 2.0
    data = bytearray()
    while len(data) < size and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        try:
            chunk = os.read(fd, size - len(data))
        except BlockingIOError:
            continue
        if chunk:
            data.extend(chunk)
    assert len(data) == size, f"預期 PTY 收到 {size} bytes，實際收到 {len(data)}"
    return bytes(data)


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
    assert len(data) == size, f"預期 TCP 收到 {size} bytes，實際收到 {len(data)}"
    return bytes(data)


def _exercise_round(slave_fd: int, peer: socket.socket, suffix: bytes) -> None:
    to_tcp = b"from-pty:" + suffix + b"\x00\xff\r\n"
    os.write(slave_fd, to_tcp)
    assert _recv_exact(peer, len(to_tcp)) == to_tcp

    to_pty = b"from-tcp:" + suffix + b"\x00\xfe\r\n"
    peer.sendall(to_pty)
    assert _read_exact(slave_fd, len(to_pty)) == to_pty


def test_python_pty_bridge_keeps_the_same_pty_across_tcp_reconnect(tmp_path: Path) -> None:
    module = _pty_module()
    peer = LocalPeer()
    link = tmp_path / "isolated-by-id" / "opi-bridge"
    bridge = module.PtyTcpBridge(
        pty_link=link,
        host="127.0.0.1",
        port=peer.port,
        reconnect_interval_s=0.02,
        status_path=tmp_path / "pty-status.json",
    )
    bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    bridge_thread.start()
    slave_fd = -1
    first = second = None
    try:
        _wait_for(lambda: bridge.ready)
        assert link.is_symlink()
        first_target = os.readlink(link)
        slave_fd = os.open(link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        first = peer.next_connection()
        _wait_for(lambda: bridge.connected)
        _exercise_round(slave_fd, first, b"first")

        first.close()
        second = peer.next_connection()
        _wait_for(lambda: bridge.connected and bridge._status["accepted_connections"] == 2)
        assert os.readlink(link) == first_target, "TCP 重連不得更換 serialwrapd 持有的 PTY"
        _exercise_round(slave_fd, second, b"second")
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        if slave_fd >= 0:
            os.close(slave_fd)
        bridge.close()
        bridge_thread.join(timeout=2)
        peer.close()

    assert not bridge_thread.is_alive()
    assert not link.exists()
    assert bridge.bytes_pty_to_tcp == len(b"from-pty:first\x00\xff\r\nfrom-pty:second\x00\xff\r\n")
    assert bridge.bytes_tcp_to_pty == len(b"from-tcp:first\x00\xfe\r\nfrom-tcp:second\x00\xfe\r\n")


def test_launcher_uses_loopback_and_provides_pty_transport(tmp_path: Path) -> None:
    peer = LocalPeer()
    link = tmp_path / "launcher-by-id" / "opi-bridge"
    command = ["bash", str(ROOT / "realhw/bridge/start_pty_tcp_bridge.sh"), str(link), str(peer.port)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    slave_fd = -1
    connections: list[socket.socket] = []
    try:
        _wait_for(lambda: link.is_symlink(), timeout=3.0)
        slave_fd = os.open(link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        pty_target = os.readlink(link)
        first = peer.next_connection(timeout=3.0)
        connections.append(first)
        _exercise_round(slave_fd, first, b"launcher-first")

        first.close()
        second = peer.next_connection(timeout=3.0)
        connections.append(second)
        assert process.poll() is None, "TCP 斷線後 PTY bridge 必須保留行程與原 PTY"
        assert link.is_symlink() and os.readlink(link) == pty_target
        _exercise_round(slave_fd, second, b"launcher-reconnected")
    finally:
        for conn in connections:
            conn.close()
        if slave_fd >= 0:
            os.close(slave_fd)
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        peer.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    assert not process.poll() is None
