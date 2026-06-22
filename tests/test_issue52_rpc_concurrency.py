"""#52：bulk 傳輸（阻塞式 RPC handler）不得凍結其他 RPC（event loop 不被阻塞）。

根因：JsonRpcUnixServer 同步呼叫 handler，使 file.push/file.pull 這類長阻塞 handler
跑在 asyncio event loop 執行緒上，傳輸期間整個 daemon 的所有 RPC 全部凍結（真機實測
health.ping 被卡 19.8s）。修法（B-lite）：把指定的 blocking_methods 丟到 executor 執行，
event loop 於傳輸期間仍可服務其他連線。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest

from sw_core.client import rpc_call
from sw_core.rpc import JsonRpcUnixServer


class TestIssue52RpcNotBlockedByTransfer(unittest.TestCase):
    SLOW_S = 3.0

    def _handler(self, method: str, params: dict) -> dict:
        if method == "slow.blocking":
            time.sleep(self.SLOW_S)  # 模擬 file.push 的長阻塞同步 I/O
            return {"ok": True, "slept": True}
        return {"ok": True, "pong": True}

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sw52rpc-")
        self._sock = os.path.join(self._tmp, "t.sock")
        self._loop = asyncio.new_event_loop()
        # B-lite：宣告 slow.blocking 為 blocking method → 應 offload 到 executor，不卡 event loop
        self._server = JsonRpcUnixServer(
            self._sock, self._handler, blocking_methods={"slow.blocking"}
        )
        self._ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._server.start())
            self._ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        self.assertTrue(self._ready.wait(5.0), "server failed to start")

    def tearDown(self) -> None:
        async def _shutdown() -> None:
            await self._server.stop()

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fast_rpc_responsive_during_blocking_handler(self) -> None:
        """slow.blocking 進行中，並發的 health.ping 仍須即時回應（不被 event loop 阻塞）。"""
        slow_done: dict = {}

        def _fire_slow() -> None:
            slow_done["resp"] = rpc_call(self._sock, "slow.blocking", {}, timeout_s=10.0)

        slow_thread = threading.Thread(target=_fire_slow)
        slow_thread.start()
        time.sleep(0.3)  # 確保 slow handler 已進入

        t0 = time.monotonic()
        resp = rpc_call(self._sock, "health.ping", {}, timeout_s=5.0)
        elapsed = time.monotonic() - t0

        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("pong"))
        self.assertLess(
            elapsed,
            1.0,
            f"health.ping 在 slow handler 進行中被阻塞 {elapsed:.2f}s（event loop 被卡）",
        )
        slow_thread.join(timeout=10.0)
        self.assertTrue(slow_done.get("resp", {}).get("ok"))


class TestIssue52DaemonDeclaresTransferBlocking(unittest.TestCase):
    """daemon 必須把 file.push/file.pull 宣告為 blocking method（offload 到 executor）。"""

    def test_serialwrapd_declares_file_transfer_blocking(self) -> None:
        import serialwrapd

        self.assertIn("file.push", serialwrapd.BLOCKING_RPC_METHODS)
        self.assertIn("file.pull", serialwrapd.BLOCKING_RPC_METHODS)


if __name__ == "__main__":
    unittest.main()
