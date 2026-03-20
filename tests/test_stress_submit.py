"""高頻命令壓力測試。

涵蓋：單 agent 連續 50 命令、多 agent 並發 100 命令、
      佇列 backpressure 200 命令不死鎖、大量命令後 WAL 完整。
使用 arbiter 層級直接測試（不依賴 PTY / daemon），確保快速執行。
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sw_core.arbiter import CommandArbiter
from sw_core.wal import WalWriter


class TestStressSubmit(unittest.TestCase):
    """Arbiter 高頻壓力測試。"""

    def _make_arbiter(
        self,
        *,
        delay: float = 0.0,
        wal: WalWriter | None = None,
    ) -> CommandArbiter:
        """建立 arbiter，send_cb 帶可選延遲。"""
        counter = {"n": 0}
        lock = threading.Lock()

        def cb(session_id: str, command: str, source: str,
               cmd_id: str, timeout_s: float, mode: str) -> dict[str, Any]:
            if delay > 0:
                time.sleep(delay)
            with lock:
                counter["n"] += 1
            if wal:
                wal.append(com="COM0", direction="TX", cmd_id=cmd_id,
                           source=source, payload=command.encode())
            return {"ok": True, "stdout": f"done:{counter['n']}"}

        self._exec_counter = counter
        arb = CommandArbiter(send_cb=cb)
        return arb

    # ---- test 1: 50 sequential commands ----
    def test_rapid_50_commands_sequential(self) -> None:
        """單 agent 連續提交 50 命令，全部 done、無死鎖。"""
        arb = self._make_arbiter(delay=0.001)
        arb.register_session("s1")
        self.addCleanup(lambda: arb.unregister_session("s1"))

        cmd_ids: list[str] = []
        for i in range(50):
            r = arb.submit(session_id="s1", command=f"cmd-{i}",
                           source="agent:stress", mode="fg", timeout_s=5.0)
            self.assertTrue(r["ok"], f"submit #{i} failed")
            cmd_ids.append(r["cmd_id"])

        # 等全部完成（最多 30 秒）
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            all_done = all(
                arb.get(cid)["ok"] and arb.get(cid)["command"]["status"] == "done"
                for cid in cmd_ids
            )
            if all_done:
                break
            time.sleep(0.05)

        for i, cid in enumerate(cmd_ids):
            status = arb.get(cid)["command"]["status"]
            self.assertEqual(status, "done", f"cmd-{i} status={status}")

        self.assertEqual(self._exec_counter["n"], 50)

    # ---- test 2: 10 agents × 10 commands concurrent ----
    def test_10_agents_10_commands_concurrent(self) -> None:
        """10 agents 同時各送 10 命令（共 100），全部完成、無死鎖。"""
        arb = self._make_arbiter(delay=0.001)
        arb.register_session("s1")
        self.addCleanup(lambda: arb.unregister_session("s1"))

        all_ids: list[str] = []
        lock = threading.Lock()

        def submit_batch(agent_idx: int) -> list[str]:
            ids = []
            for j in range(10):
                r = arb.submit(session_id="s1",
                               command=f"a{agent_idx}-c{j}",
                               source=f"agent:{agent_idx}",
                               mode="fg", timeout_s=10.0)
                ids.append(r["cmd_id"])
            return ids

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(submit_batch, i): i for i in range(10)}
            for f in as_completed(futs):
                with lock:
                    all_ids.extend(f.result())

        self.assertEqual(len(all_ids), 100)

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            statuses = [arb.get(cid)["command"]["status"] for cid in all_ids]
            if all(s == "done" for s in statuses):
                break
            time.sleep(0.1)

        for cid in all_ids:
            self.assertEqual(arb.get(cid)["command"]["status"], "done")

        self.assertEqual(self._exec_counter["n"], 100)

    # ---- test 3: queue backpressure 200 commands ----
    def test_queue_backpressure_200_commands(self) -> None:
        """一次提交 200 命令，佇列不爆、全部最終完成。"""
        arb = self._make_arbiter(delay=0.0)
        arb.register_session("s1")
        self.addCleanup(lambda: arb.unregister_session("s1"))

        cmd_ids: list[str] = []
        for i in range(200):
            r = arb.submit(session_id="s1", command=f"bp-{i}",
                           source="agent:bp", mode="fg", timeout_s=30.0)
            self.assertTrue(r["ok"])
            cmd_ids.append(r["cmd_id"])

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            done_count = sum(
                1 for cid in cmd_ids
                if arb.get(cid)["command"]["status"] == "done"
            )
            if done_count == 200:
                break
            time.sleep(0.1)

        self.assertEqual(
            sum(1 for cid in cmd_ids if arb.get(cid)["command"]["status"] == "done"),
            200,
        )

    # ---- test 4: 50 commands with WAL integrity ----
    def test_stress_with_wal_all_recorded(self) -> None:
        """50 命令搭配 WAL，驗證 WAL 記錄完整 50 筆。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        wal = WalWriter(wal_dir=tmp.name)
        arb = self._make_arbiter(delay=0.001, wal=wal)
        arb.register_session("s1")
        self.addCleanup(lambda: arb.unregister_session("s1"))

        cmd_ids: list[str] = []
        for i in range(50):
            r = arb.submit(session_id="s1", command=f"wal-{i}",
                           source="agent:wal", mode="fg", timeout_s=5.0)
            cmd_ids.append(r["cmd_id"])

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if all(arb.get(c)["command"]["status"] == "done" for c in cmd_ids):
                break
            time.sleep(0.05)

        # WAL 檔案應有 50 行
        wal_path = Path(tmp.name) / "raw.wal.ndjson"
        self.assertTrue(wal_path.exists(), "WAL file missing")
        lines = [l for l in wal_path.read_text().strip().split("\n") if l.strip()]
        self.assertEqual(len(lines), 50, f"WAL has {len(lines)} lines, expected 50")


if __name__ == "__main__":
    unittest.main()
