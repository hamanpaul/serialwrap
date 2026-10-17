"""tests/test_rpc_blocking_offload.py — #80 阻塞型 RPC handler 丟 executor。

延伸 #52：把會同步阻塞 event loop 的 handler（recover/attach/self_test/console_attach 與
整檔讀取 command.result_tail/result.tail/log.tail_*/wal.range）納入 BLOCKING_RPC_METHODS，
offload 到 executor，避免單一慢 RPC 凍結全 daemon。機制本身（run_in_executor）由
test_issue52_rpc_concurrency 覆蓋；此處另以「實際 CLI 方法名 + 真實 BLOCKING set」驅動並發驗證。
"""
import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time

import pytest
import sw_core
from sw_core.client import rpc_call
from sw_core.daemon import BLOCKING_RPC_METHODS
from sw_core.rpc import JsonRpcUnixServer
from sw_core.rpc_posix import serve_connection

_EXPECTED = {
    "file.push", "file.pull",
    "session.recover", "session.attach", "session.self_test", "session.console_attach",
    "command.result_tail", "result.tail", "log.tail_raw", "log.tail_text", "wal.range",
}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_blocking_methods_include_known_blocking_handlers():
    assert _EXPECTED <= BLOCKING_RPC_METHODS


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_result_tail_method_is_offloaded():
    """CLI `cmd result-tail` 實際送 command.result_tail（非 legacy result.tail），須在 offload 清單。"""
    assert "command.result_tail" in BLOCKING_RPC_METHODS


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_blocking_methods_are_real_dispatch_strings():
    """防打錯方法名：每個 offload 的方法都應是 service.rpc 實際分派的字串。"""
    src = (pathlib.Path(sw_core.__file__).parent / "service.py").read_text(encoding="utf-8")
    for m in _EXPECTED - {"file.push", "file.pull"}:
        assert f'"{m}"' in src, f"{m} 不是 service.py 的分派方法（可能打錯名）"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only: Unix domain socket 在 Windows 不適用（#84 PORT-4）",
)
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_health_ping_not_blocked_by_slow_command_result_tail():
    """以真實 BLOCKING_RPC_METHODS 驅動：大 command.result_tail 進行中，health.ping 不得被卡。

    若 command.result_tail 不在 offload 清單（修正前），慢 handler 會在單執行緒 event loop 上
    同步執行，並發 health.ping 被卡 → 此測試失敗；納入清單後 health.ping 即時回應。
    """
    tmp = tempfile.mkdtemp(prefix="sw80rt-")
    sock = os.path.join(tmp, "t.sock")
    slow_s = 3.0

    def _handler(method: str, params: dict) -> dict:
        if method == "command.result_tail":
            time.sleep(slow_s)  # 模擬大 capture 讀取/切片/序列化
            return {"ok": True, "chunks": []}
        return {"ok": True, "pong": True}

    loop = asyncio.new_event_loop()
    # 關鍵：用 daemon 的真實 BLOCKING_RPC_METHODS，確保 command.result_tail 確實被宣告為 offload
    server = JsonRpcUnixServer(sock, _handler, blocking_methods=BLOCKING_RPC_METHODS)
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    assert ready.wait(5.0), "server failed to start"
    try:
        slow_done: dict = {}

        def _fire_slow() -> None:
            slow_done["resp"] = rpc_call(sock, "command.result_tail", {"cmd_id": "x"}, timeout_s=10.0)

        slow_thread = threading.Thread(target=_fire_slow)
        slow_thread.start()
        time.sleep(0.3)  # 確保慢 handler 已進入

        t0 = time.monotonic()
        resp = rpc_call(sock, "health.ping", {}, timeout_s=5.0)
        elapsed = time.monotonic() - t0

        assert resp.get("ok") and resp.get("pong")
        assert elapsed < 1.0, f"health.ping 在大 command.result_tail 進行中被阻塞 {elapsed:.2f}s"
        slow_thread.join(timeout=10.0)
        assert slow_done.get("resp", {}).get("ok")
    finally:
        fut = asyncio.run_coroutine_threadsafe(server.stop(), loop)
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=5.0)
        loop.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only: Unix domain socket 在 Windows 不適用（#84 PORT-4）",
)
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_release_rpc_offloaded_from_event_loop(tmp_path):
    """真實 release drain 阻塞時，production RPC server 仍須在 0.5 秒內回 health.ping。"""
    from stage0_fakes import FakePort, make_service
    from sw_core.daemon import make_rpc_handler

    release_write = threading.Event()
    write_started = threading.Event()

    class BlockingPort(FakePort):
        def write(self, payload, *, cancel=None, submit_guard=None):
            write_started.set()
            return super().write(payload, cancel=cancel, submit_guard=submit_guard)

    port = BlockingPort(block=release_write)
    service = make_service(tmp_path, ready=["COM0"], port=port)
    bridge = service._sessions.get_session("COM0").bridge
    drain_started = threading.Event()
    original_finish_drain = bridge.finish_drain

    def tracked_finish_drain(*args, **kwargs):
        drain_started.set()
        return original_finish_drain(*args, **kwargs)

    bridge.finish_drain = tracked_finish_drain
    sock = os.fspath(tmp_path / "t.sock")
    loop = asyncio.new_event_loop()
    server = JsonRpcUnixServer(
        sock,
        make_rpc_handler(service, lambda: None),
        blocking_methods=BLOCKING_RPC_METHODS,
    )
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    assert ready.wait(5.0), "server failed to start"
    command_result: dict = {}
    release_result: dict = {}
    try:
        submit_thread = threading.Thread(target=lambda: command_result.update(
            rpc_call(sock, "command.submit", {
                "selector": "COM0", "command": "echo drain", "source": "agent:test",
                "mode": "line", "timeout_s": 30.0,
            }, timeout_s=5.0)
        ))
        submit_thread.start()
        submit_thread.join(timeout=2.0)
        assert command_result.get("ok") is True
        assert write_started.wait(1.0), "command did not enter the blocked UART write"

        release_thread = threading.Thread(target=lambda: release_result.update(
            rpc_call(sock, "device.release", {"selector": "COM0"}, timeout_s=5.0)
        ))
        release_thread.start()
        assert drain_started.wait(3.0), f"release did not enter bridge.finish_drain: {release_result}"

        t0 = time.monotonic()
        resp = rpc_call(sock, "health.ping", {}, timeout_s=5.0)
        elapsed = time.monotonic() - t0

        assert resp.get("ok") and resp.get("pong")
        assert elapsed < 0.5, f"health.ping 在 device.release 進行中被阻塞 {elapsed:.2f}s"
        release_write.set()
        release_thread.join(timeout=5.0)
        assert not release_thread.is_alive()
        assert release_result.get("ok") is True
    finally:
        release_write.set()
        fut = asyncio.run_coroutine_threadsafe(server.stop(), loop)
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=5.0)
        loop.close()
        service.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_after_response_runs_when_rpc_write_fails():
    class Reader:
        sent = False

        async def readline(self) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b'{"id":1,"method":"daemon.handoff_stop","params":{}}\n'

    class Writer:
        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            raise ConnectionResetError("client disconnected before response drain")

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    stopped: list[bool] = []

    def handler(_method: str, _params: dict) -> dict:
        return {"ok": True, "_rpc_after_write": lambda: stopped.append(True)}

    with pytest.raises(ConnectionResetError):
        asyncio.run(serve_connection(Reader(), Writer(), handler, set()))
    assert stopped == [True]
