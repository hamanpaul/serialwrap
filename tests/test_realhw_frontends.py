"""P2-T14 red：realhw/frontends.py（HumanFrontend、Telnet frontend、能力式選擇）。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1。
實作前預期失敗：模組尚未存在（函式內 import），或行為斷言不成立。
"""
from __future__ import annotations

import signal
import socket
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.core_portable]

# 本檔的 tmux 字串只用於建構 TmuxMinicomFrontend／TmuxReaderFrontend 與比對工廠選擇，從不啟動 tmux。
RESOURCE_AUDIT_WAIVERS = {"interactive_desktop": "只檢查 tmux 前端的建構與工廠選擇，不啟動 tmux"}


def _wait_until(pred, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


class _FakeTelnetServer:
    """單一 client 的 loopback Telnet 替身（不做協商，只收送原始位元組）。"""

    def __init__(self) -> None:
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(1)
        self.port = self._listen.getsockname()[1]
        self._conn = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._accepted = threading.Event()
        self._closing = threading.Event()
        self.end_state: str | None = None  # "fin"（正常 FIN）或 "rst"（ConnectionResetError）
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._listen.accept()
        except OSError:
            return
        self._conn = conn
        self._accepted.set()
        conn.settimeout(0.2)
        while not self._closing.is_set():
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except ConnectionResetError:
                self.end_state = "rst"
                return
            except OSError:
                return
            if not data:
                self.end_state = "fin"
                return
            with self._lock:
                self._buf += data

    def received(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def wait_received(self, expected: bytes, timeout_s: float) -> bool:
        return _wait_until(lambda: expected in self.received(), timeout_s)

    def send(self, data: bytes) -> None:
        self._accepted.wait(2)
        self._conn.sendall(data)

    def close_client(self) -> None:
        """伺服端正常關閉（對 client 送 FIN）。"""
        self._accepted.wait(2)
        try:
            self._conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        self._closing.set()
        try:
            self._listen.close()
        except OSError:
            pass
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass


@pytest.fixture
def fake_telnet_server():
    srv = _FakeTelnetServer()
    try:
        yield srv
    finally:
        srv.close()


class _StubSw:
    """console-attach 等 RPC 替身：依 (argv[0], argv[1]) 查預設回應。"""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple] = []

    def run(self, *args, **kwargs) -> dict:
        self.calls.append(tuple(args))
        return dict(self.responses.get(tuple(args[:2]), {"ok": False, "error_code": "STUB_UNSET"}))


@pytest.fixture
def stub_sw() -> _StubSw:
    return _StubSw()


class _PaneTmux:
    def pane_pid(self, _session):
        return "4242"

    def name(self, tag):
        return tag

    def new(self, *_args):
        return None


def _endpoint_resp(port: int) -> dict:
    return {"ok": True, "client_id": None, "label": "realhw",
            "vtty": f"127.0.0.1:{port}", "endpoint": f"127.0.0.1:{port}",
            "transport": "tcp", "protocol": "telnet"}


@pytest.mark.process_rpc
def test_telnet_frontend_send_line_and_capture(fake_telnet_server, stub_sw):
    from realhw import frontends

    stub_sw.responses[("session", "console-attach")] = _endpoint_resp(fake_telnet_server.port)
    h = frontends.TelnetFrontend(stub_sw, "t1")
    h.open("COM0")
    try:
        h.send_line("echo HUMAN_TICK_1")
        h.send_key("Tab")
        fake_telnet_server.send(b"\x1b[32mroot@prplOS:/# \x1b[0mecho HUMAN_TICK_1\r\n")
        assert fake_telnet_server.wait_received(b"echo HUMAN_TICK_1\r\n\t", timeout_s=2)
        assert _wait_until(lambda: "root@prplOS:/# echo HUMAN_TICK_1" in h.capture(), 2)
        assert h.alive() is True
        fake_telnet_server.close_client()
        assert _wait_until(lambda: h.alive() is False, 2)
    finally:
        h.close()


@pytest.mark.memory_only
def test_select_frontend_kind():
    from realhw import frontends

    assert frontends.select_frontend_kind({"human_frontend": "auto"}, os_name="nt") == "telnet"
    assert frontends.select_frontend_kind({"human_frontend": "auto"}, os_name="posix") == "tmux"
    assert frontends.select_frontend_kind({"human_frontend": "telnet"}, os_name="posix") == "telnet"


@pytest.mark.memory_only
def test_factory_roles_tmux():
    from realhw import frontends

    factory = frontends.make_frontend_factory(
        {"human_frontend": "tmux"}, tmux=object(), sw=object(), os_name="posix")
    assert isinstance(factory("x", "minicom"), frontends.TmuxMinicomFrontend)
    assert isinstance(factory("x", "reader"), frontends.TmuxReaderFrontend)


@pytest.mark.memory_only
def test_telnet_requires_tcp_endpoint(stub_sw):
    from realhw import frontends

    # POSIX PTY 端點（無 transport 欄位）不得以 Telnet 連線
    stub_sw.responses[("session", "console-attach")] = {
        "ok": True, "client_id": None, "label": "realhw",
        "vtty": "/dev/pts/3", "endpoint": "/dev/pts/3"}
    h = frontends.TelnetFrontend(stub_sw, "t2")
    with pytest.raises(frontends.FrontendError):
        h.open("COM0")


@pytest.mark.process_rpc
def test_telnet_iac_escaped(fake_telnet_server, stub_sw):
    from realhw import frontends

    stub_sw.responses[("session", "console-attach")] = _endpoint_resp(fake_telnet_server.port)
    h = frontends.TelnetFrontend(stub_sw, "t3")
    h.open("COM0")
    try:
        # send_text 以 UTF-8 編碼：ÿ（U+00FF）→ C3 BF，線上不得出現裸 0xFF
        h.send_text("aÿ")
        assert fake_telnet_server.wait_received(b"a\xc3\xbf", timeout_s=2)
        # 內部原始位元組入口（假設名稱 _send_bytes；實作者若改名須同步此測試）：0xFF 必須加倍為 IAC IAC
        h._send_bytes(b"\xff")
        assert fake_telnet_server.wait_received(b"\xff\xff", timeout_s=2)
    finally:
        h.close()


@pytest.mark.process_rpc
def test_capture_bounded(fake_telnet_server, stub_sw):
    from realhw import frontends

    limit = 1_048_576
    stub_sw.responses[("session", "console-attach")] = _endpoint_resp(fake_telnet_server.port)
    h = frontends.TelnetFrontend(stub_sw, "t4", buffer_limit_bytes=limit)
    h.open("COM0")
    try:
        fake_telnet_server.send(b"A" * (3 * 1024 * 1024))
        assert _wait_until(lambda: len(h.capture()) >= limit - 4096, 10)
        text = h.capture()
        assert 0 < len(text) <= limit
    finally:
        h.close()


@pytest.mark.process_rpc
def test_frontend_crash_semantics(fake_telnet_server, stub_sw, monkeypatch):
    from realhw import frontends

    # tmux pane 的 PID 是 router；crash() 必須定位並終止其 minicom 子行程。
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(frontends.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(
        frontends.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="4242 1 bash serialwrap-minicom COM0\n4243 4242 minicom -D /dev/pts/3\n",
            stderr="",
        ),
    )
    mh = frontends.TmuxMinicomFrontend(_PaneTmux(), "t5")
    mh.open("COM0")
    assert mh.crash() == 4243
    assert (4243, getattr(signal, "SIGKILL", signal.SIGTERM)) in kills

    # 找不到 minicom child 必須明確失敗，不能靜默略過。
    monkeypatch.setattr(
        frontends.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="4242 1 bash serialwrap-minicom COM0\n", stderr=""
        ),
    )
    with pytest.raises(frontends.FrontendError, match="minicom"):
        mh.crash()

    # telnet 模式：crash() 前設 SO_LINGER(1,0)，對端收到 RST（ConnectionResetError），而非 FIN
    stub_sw.responses[("session", "console-attach")] = _endpoint_resp(fake_telnet_server.port)
    th = frontends.TelnetFrontend(stub_sw, "t6")
    th.open("COM0")
    th.crash()
    assert _wait_until(lambda: fake_telnet_server.end_state is not None, 2)
    assert fake_telnet_server.end_state == "rst"
