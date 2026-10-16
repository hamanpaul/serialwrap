"""tools/uart_stdio_bridge/tests/test_posix_pty_endpoint.py

POSIX PTY 端點 adapter `uart_stdio_bridge.endpoints.posix_pty.PosixPtyEndpoint` 的失敗測試
（P2-T3，TDD 紅燈；plan 03-stage0-test-infra.md Task 3 T3-1 表格）。

- 所有 `PosixPtyEndpoint` 相關 import 皆在測試函式內進行（P0 慣例），使 T3 實作前整套仍可收集。
- 「daemon 側」以測試自行 `os.open(link)` 模擬受測端 A；bridge 本身不持有端 A（§15.3）。
- 非 Linux 平台整檔以 `cap:posix_pty` 跳過。
"""
from __future__ import annotations

import os
import select
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_pty

if not sys.platform.startswith("linux"):
    pytest.skip("cap:posix_pty", allow_module_level=True)


# ---------------------------------------------------------------- 測試輔助


@pytest.fixture
def link(tmp_path: Path) -> Path:
    """by-id 模擬目錄下的 symlink 路徑（尚未建立）。"""
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    return by_id / "usb-SERIALWRAP_FAKE-if00"


def _open_peer(link: Path) -> int:
    """模擬受測端 A：經 by-id symlink 開啟 slave（O_NOCTTY，不成為控制終端）。"""
    return os.open(link, os.O_RDWR | os.O_NOCTTY)


def _poll_until(pred, timeout_s: float = 0.5, step_s: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step_s)


def _read_fd_n(fd: int, n: int, timeout_s: float) -> bytes:
    """從 peer fd 讀取至多 n 個位元組；逾時或對端 hangup（EIO／EOF）即回傳已讀到的內容。"""
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for _, ev in poller.poll(max(1, int(remaining * 1000))):
            if ev & (select.POLLIN | select.POLLHUP | select.POLLERR):
                try:
                    chunk = os.read(fd, n - len(buf))
                except OSError:  # EIO：master 已關閉
                    return bytes(buf)
                if not chunk:
                    return bytes(buf)
                buf.extend(chunk)
    return bytes(buf)


def _drain_fd_quiet(fd: int, quiet_s: float = 0.5, cap_s: float = 10.0) -> bytes:
    """讀到連續 quiet_s 無新資料為止（或達 cap_s）。"""
    buf = bytearray()
    deadline = time.monotonic() + cap_s
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while time.monotonic() < deadline:
        events = poller.poll(int(quiet_s * 1000))
        if not events:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _write_all_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    off = 0
    while off < len(data):
        off += os.write(fd, view[off:])


def _ep_read_n(ep, n: int, timeout_s: float) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    while len(buf) < n and time.monotonic() < deadline:
        buf.extend(ep.read(n - len(buf), 0.1))
    return bytes(buf)


# ---------------------------------------------------------------- T3-1 測試


def test_open_creates_link_and_releases_slave(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    try:
        assert link.is_symlink()
        assert os.path.realpath(link) == os.path.realpath(ep.slave_path)
        slave_real = os.path.realpath(ep.slave_path)
        # bridge 不持有端 A：程序內沒有任何 fd 指向 slave
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                continue
            assert target != slave_real
    finally:
        ep.close()


def test_peer_open_tracks_opener(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    try:
        assert ep.peer_open() is False
        fd = _open_peer(link)
        try:
            assert _poll_until(lambda: ep.peer_open() is True, 0.5)
        finally:
            os.close(fd)
        assert _poll_until(lambda: ep.peer_open() is False, 0.5)
    finally:
        ep.close()


def test_full_byte_range_both_directions(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    fd = _open_peer(link)
    try:
        data = bytes(range(256)) * 16

        # peer → endpoint：peer 寫入（背景執行緒），endpoint 讀取累積
        writer = threading.Thread(target=_write_all_fd, args=(fd, data), daemon=True)
        writer.start()
        got = _ep_read_n(ep, len(data), 10.0)
        writer.join(5)
        assert got == data

        # endpoint → peer：endpoint 寫入，peer 讀取（背景執行緒）
        result: dict[str, bytes] = {}
        reader = threading.Thread(
            target=lambda: result.update(data=_read_fd_n(fd, len(data), 10.0)), daemon=True
        )
        reader.start()
        sent = 0
        deadline = time.monotonic() + 10.0
        while sent < len(data) and time.monotonic() < deadline:
            sent += ep.write_some(data[sent:], 0.5)
        reader.join(15)
        assert result.get("data") == data
    finally:
        os.close(fd)
        ep.close()


def test_peer_gate_blocks_write_without_opener(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link, peer_gate=True)
    ep.open()
    try:
        t0 = time.monotonic()
        n = ep.write_some(b"x", 0.2)
        dt = time.monotonic() - t0
        assert n == 0
        assert dt >= 0.2

        # 之後 peer 才開啟：0.2 s 內讀不到任何 byte（未交給下一個 opener）
        fd = _open_peer(link)
        try:
            assert _read_fd_n(fd, 1, 0.2) == b""
        finally:
            os.close(fd)
    finally:
        ep.close()


def test_peer_gate_disabled_writes_queue(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link, peer_gate=False)
    ep.open()
    try:
        assert ep.write_some(b"x", 0.2) == 1
    finally:
        ep.close()


def test_partial_write_reports_accepted_len(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    fd = _open_peer(link)
    try:
        assert _poll_until(lambda: ep.peer_open() is True, 0.5)
        accepted = 0
        partial = False
        for _ in range(2000):
            n = ep.write_some(b"\x55" * 65536, 0.05)
            accepted += n
            if n < 65536:
                partial = True
                break
        assert partial, "反覆寫入未出現部分寫入（回傳 < 要求長度）"
        received = _drain_fd_quiet(fd)
        assert len(received) == accepted
    finally:
        os.close(fd)
        ep.close()


def test_read_timeout_returns_empty(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    fd = _open_peer(link)
    try:
        t0 = time.monotonic()
        assert ep.read(4096, 0.1) == b""
        assert time.monotonic() - t0 >= 0.1
    finally:
        os.close(fd)
        ep.close()


def test_disconnect_removes_link_and_peer_sees_hangup(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    fd = _open_peer(link)
    try:
        ep.disconnect(remove_link=True)
        assert not os.path.lexists(link)

        # 對端應觀察到 hangup（以 poll 設逾時，避免 read 在 master 未關閉時無限阻塞）
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP)
        assert poller.poll(1000)
        try:
            chunk = os.read(fd, 16)
        except OSError:  # EIO
            chunk = b""
        assert chunk == b""
    finally:
        os.close(fd)
        ep.close()


def test_reconnect_increments_generation_and_new_slave(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    try:
        old_slave = ep.slave_path
        assert ep.generation == 1
        assert ep.reconnect() == 2
        assert ep.generation == 2
        assert ep.slave_path != old_slave
        assert os.path.realpath(link) == os.path.realpath(ep.slave_path)
    finally:
        ep.close()


def test_link_remove_add_keeps_pty(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    fd = _open_peer(link)
    try:
        slave = ep.slave_path
        ep.link_remove()
        assert not os.path.lexists(link)

        # 無 link 時，已開啟的 peer 仍可雙向傳輸
        _write_all_fd(fd, b"ping")
        assert _ep_read_n(ep, 4, 2.0) == b"ping"
        assert ep.write_some(b"pong", 1.0) == 4
        assert _read_fd_n(fd, 4, 2.0) == b"pong"

        ep.link_add()
        assert os.path.islink(link)
        assert os.path.realpath(link) == os.path.realpath(slave)
        assert ep.slave_path == slave
    finally:
        os.close(fd)
        ep.close()


def test_close_idempotent(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link)
    ep.open()
    ep.close()
    ep.close()  # 第二次不得 raise


def test_describe_fields(link: Path):
    from uart_stdio_bridge.endpoints.posix_pty import PosixPtyEndpoint

    ep = PosixPtyEndpoint(link, peer_gate=True)
    ep.open()
    try:
        desc = ep.describe()
        assert desc["kind"] == "posix-pty"
        assert desc["transport_mode"] == "virtual_pty"
        assert str(desc["by_id_link"]) == str(link)
        assert desc["slave_path"] == ep.slave_path
        assert desc["generation"] == 1
        assert desc["peer_gate"] is True
    finally:
        ep.close()
