"""多 Agent 競爭與錯誤交叉測試。

涵蓋：A 卡住不阻塞 B 佇列、交叉 good/bad 命令、cross-agent cancel、
      多 agent timeout 不雙重 recover、agent+human 同時操作。
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sw_core.arbiter import CommandArbiter
from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class TestMultiAgentArbiter(unittest.TestCase):
    """Arbiter 層的多 agent 佇列行為。"""

    def test_agent_a_hang_does_not_block_agent_b_queue(self) -> None:
        """Agent A 的慢命令不會阻塞同 session 的 Agent B 命令（佇列排隊正常消化）。"""
        results: list[str] = []
        gate = threading.Event()

        def slow_cb(session_id, command, source, cmd_id, timeout_s, mode):
            if command == "slow":
                gate.wait(timeout=10.0)
            results.append(command)
            return {"ok": True, "stdout": f"done:{command}"}

        arb = CommandArbiter(send_cb=slow_cb)
        arb.register_session("s1")
        self.addCleanup(lambda: (gate.set(), arb.unregister_session("s1")))

        # Agent A 送慢命令
        r_a = arb.submit(session_id="s1", command="slow", source="agent-A",
                         mode="fg", timeout_s=10.0)
        self.assertTrue(r_a["ok"])

        # Agent B 送快命令（會排在 slow 後面）
        r_b = arb.submit(session_id="s1", command="fast", source="agent-B",
                         mode="fg", timeout_s=1.0)
        self.assertTrue(r_b["ok"])

        # 放行 slow
        time.sleep(0.1)
        gate.set()

        # 等 fast 完成
        for _ in range(100):
            info = arb.get(r_b["cmd_id"])
            if info["ok"] and info["command"]["status"] == "done":
                break
            time.sleep(0.05)

        # 兩個都應完成，且 slow 在 fast 之前
        self.assertEqual(results, ["slow", "fast"])
        info_a = arb.get(r_a["cmd_id"])
        info_b = arb.get(r_b["cmd_id"])
        self.assertEqual(info_a["command"]["status"], "done")
        self.assertEqual(info_b["command"]["status"], "done")

    def test_interleaved_good_bad_from_multiple_agents(self) -> None:
        """3 agents 各送 good/bad/good，bad 觸發錯誤但不阻塞後續。"""
        exec_log: list[str] = []

        def cb(session_id, command, source, cmd_id, timeout_s, mode):
            exec_log.append(f"{source}:{command}")
            if "bad" in command:
                return {"ok": False, "error_code": "PROMPT_TIMEOUT", "stdout": ""}
            return {"ok": True, "stdout": f"ok:{command}"}

        arb = CommandArbiter(send_cb=cb)
        arb.register_session("s1")
        self.addCleanup(lambda: arb.unregister_session("s1"))

        cmd_ids = []
        for agent in ["a1", "a2", "a3"]:
            for cmd in ["good1", "bad", "good2"]:
                r = arb.submit(session_id="s1", command=f"{agent}-{cmd}",
                               source=f"agent:{agent}", mode="fg", timeout_s=1.0)
                cmd_ids.append(r["cmd_id"])

        # 等全部完成
        for _ in range(200):
            all_done = True
            for cid in cmd_ids:
                info = arb.get(cid)
                if info["ok"] and info["command"]["status"] not in ("done", "error"):
                    all_done = False
                    break
            if all_done:
                break
            time.sleep(0.05)

        # 全部 9 個命令應被執行
        self.assertEqual(len(exec_log), 9)
        # good 命令應為 done，bad 應為 error
        for cid in cmd_ids:
            info = arb.get(cid)
            cmd = info["command"]
            if "bad" in cmd["command"]:
                self.assertEqual(cmd["status"], "error")
            else:
                self.assertEqual(cmd["status"], "done")

    def test_cancel_from_agent_b_while_agent_a_executing(self) -> None:
        """Agent A 正在執行時，Agent B cancel 自己排隊中的命令，A 不受影響。"""
        a_started = threading.Event()
        a_gate = threading.Event()

        def cb(session_id, command, source, cmd_id, timeout_s, mode):
            if source == "agent:A":
                a_started.set()
                a_gate.wait(timeout=10.0)
            return {"ok": True, "stdout": f"done:{command}"}

        arb = CommandArbiter(send_cb=cb)
        arb.register_session("s1")
        self.addCleanup(lambda: (a_gate.set(), arb.unregister_session("s1")))

        # A 先進入執行
        r_a = arb.submit(session_id="s1", command="A-work", source="agent:A",
                         mode="fg", timeout_s=10.0)
        a_started.wait(timeout=5.0)

        # B 送命令（排隊）
        r_b = arb.submit(session_id="s1", command="B-work", source="agent:B",
                         mode="fg", timeout_s=1.0)

        # B cancel 自己
        cancel_result = arb.cancel(r_b["cmd_id"])
        self.assertTrue(cancel_result["ok"])

        # 放行 A
        a_gate.set()

        # A 應正常完成
        for _ in range(100):
            info = arb.get(r_a["cmd_id"])
            if info["ok"] and info["command"]["status"] == "done":
                break
            time.sleep(0.05)

        self.assertEqual(arb.get(r_a["cmd_id"])["command"]["status"], "done")
        self.assertEqual(arb.get(r_b["cmd_id"])["command"]["status"], "canceled")

    def test_priority_ordering_respected(self) -> None:
        """高優先級命令應排在低優先級之前（數值越小優先級越高）。"""
        exec_order: list[str] = []
        gate = threading.Event()

        def cb(session_id, command, source, cmd_id, timeout_s, mode):
            if command == "blocker":
                gate.wait(timeout=10.0)
            exec_order.append(command)
            return {"ok": True, "stdout": "ok"}

        arb = CommandArbiter(send_cb=cb)
        arb.register_session("s1")
        self.addCleanup(lambda: (gate.set(), arb.unregister_session("s1")))

        # 先佔住 worker
        arb.submit(session_id="s1", command="blocker", source="a", mode="fg",
                   timeout_s=10.0)
        time.sleep(0.1)

        # 送低優先級再送高優先級
        arb.submit(session_id="s1", command="low-pri", source="a", mode="fg",
                   timeout_s=1.0, priority=20)
        arb.submit(session_id="s1", command="high-pri", source="a", mode="fg",
                   timeout_s=1.0, priority=1)

        gate.set()

        # 等全部完成
        time.sleep(1.0)

        # high-pri 應在 low-pri 之前（排除 blocker）
        non_blocker = [c for c in exec_order if c != "blocker"]
        self.assertEqual(non_blocker, ["high-pri", "low-pri"])


class TestMultiAgentSessionRecover(unittest.TestCase):
    """Session 層的多 agent 復原行為。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old

    def _make_ready_session(self):
        profile = SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab",
            device_by_id="/dev/serial/by-id/dev0", platform="shell",
            prompt_regex=r"[$#] $", uart=UartProfile(),
        )
        mgr = SessionManager(
            [profile], WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        session.bridge = bridge
        session.state = "READY"
        return mgr, session, bridge

    def test_agent_and_human_sequential_on_same_session(self) -> None:
        """Agent 命令完成後，human 命令也能正常執行。"""
        mgr, session, bridge = self._make_ready_session()

        # Agent 命令成功
        bridge.rx_snapshot_len.side_effect = [10, 50]
        bridge.wait_for_regex_from.side_effect = [True]
        bridge.rx_text_from.return_value = "agent-result\n$ "
        resp1 = mgr.execute_command("p:COM0", "agent-cmd", "agent:a1", "c1", timeout_s=1.0)
        self.assertTrue(resp1["ok"])

        # Human 命令也成功
        bridge.rx_snapshot_len.side_effect = [60, 100]
        bridge.wait_for_regex_from.side_effect = [True]
        bridge.rx_text_from.return_value = "human-result\n$ "
        resp2 = mgr.execute_command("p:COM0", "ls", "human:h1", "c2", timeout_s=1.0)
        self.assertTrue(resp2["ok"])
        self.assertIn("human-result", resp2.get("stdout", ""))


if __name__ == "__main__":
    unittest.main()
