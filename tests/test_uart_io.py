from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from sw_core.config import UartProfile
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


def _make_pty_bridge(com: str = "COM0") -> tuple[UARTBridge, int, int, tempfile.TemporaryDirectory]:
    """以 PTY slave 作 device_path 建立 UARTBridge 並回傳。

    供需要呼叫 start() 的測試使用。teardown 須 bridge.stop() + 關閉 PTY fd + tmpdir.cleanup()。
    回傳：(bridge, master_fd, slave_fd, tmpdir)
    """
    tmpdir = tempfile.TemporaryDirectory()
    wal = WalWriter(wal_dir=tmpdir.name)
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    bridge = UARTBridge(
        com=com,
        device_path=slave_path,
        profile=UartProfile(),
        wal=wal,
    )
    return bridge, master_fd, slave_fd, tmpdir


class TestUartBridgeConsoleCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def test_list_consoles_prunes_stale_non_primary_client(self) -> None:
        primary = self._bridge.attach_console(label="primary")
        stale = self._bridge.attach_console(label="stale")
        with self._bridge._state_lock:
            self._bridge._clients[stale["client_id"]].attached_at = time.time() - 10

        with mock.patch.object(self._bridge, "_client_has_external_peer_locked", return_value=False):
            consoles = self._bridge.list_consoles()

        client_ids = {row["client_id"] for row in consoles}
        self.assertIn(primary["client_id"], client_ids)
        self.assertNotIn(stale["client_id"], client_ids)
        self.assertNotIn(stale["client_id"], self._bridge._clients)

    def test_list_consoles_keeps_new_client_during_grace_window(self) -> None:
        self._bridge.attach_console(label="primary")
        fresh = self._bridge.attach_console(label="fresh")

        with mock.patch.object(self._bridge, "_client_has_external_peer_locked", return_value=False):
            consoles = self._bridge.list_consoles()

        client_ids = {row["client_id"] for row in consoles}
        self.assertIn(fresh["client_id"], client_ids)

    def test_list_consoles_never_prunes_primary_client(self) -> None:
        primary = self._bridge.attach_console(label="primary")
        with self._bridge._state_lock:
            self._bridge._clients[primary["client_id"]].attached_at = time.time() - 10

        with mock.patch.object(self._bridge, "_client_has_external_peer_locked", return_value=False):
            consoles = self._bridge.list_consoles()

        client_ids = {row["client_id"] for row in consoles}
        self.assertIn(primary["client_id"], client_ids)


class TestUartBridgeSuspendResume(unittest.TestCase):
    """suspend_interactive / resume_interactive 的單元測試。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._sent: list[tuple[bytes, str]] = []

        def fake_send(data: bytes, *, source: str = "", cmd_id=None) -> None:
            self._sent.append((data, source))

        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )
        self._bridge.send_bytes = fake_send  # type: ignore[assignment]

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def test_suspend_switches_to_deferred_mode(self) -> None:
        """suspend 後 console input 從 suspended owner 應進 deferred buffer。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()

        with self._bridge._state_lock:
            self.assertTrue(self._bridge._agent_active)
            self.assertEqual(self._bridge._suspended_owner, f"human:{cid}")
            self.assertIsNone(self._bridge._interactive_owner)

        client = self._bridge._clients[cid]
        self._bridge._handle_console_rx(client, b"\x1b[A")

        with self._bridge._state_lock:
            buf = self._bridge._deferred_buffers.get(cid, bytearray())
        self.assertEqual(bytes(buf), b"\x1b[A")
        self.assertEqual(len(self._sent), 0, "deferred 不應 send_bytes")

    def test_resume_flushes_deferred_to_uart(self) -> None:
        """resume 應把 deferred buffer 一次性 flush 到 UART。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()
        client = self._bridge._clients[cid]
        self._bridge._handle_console_rx(client, b"echo hi\r")

        self._bridge.resume_interactive()

        self.assertTrue(any(data == b"echo hi\r" for data, _ in self._sent))
        with self._bridge._state_lock:
            self.assertFalse(self._bridge._agent_active)
            self.assertEqual(len(self._bridge._deferred_buffers), 0)

    def test_resume_restores_raw_mode(self) -> None:
        """resume 後 console input 應恢復 raw 透傳。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()
        self._bridge.resume_interactive()

        with self._bridge._state_lock:
            self.assertEqual(self._bridge._interactive_owner, f"human:{cid}")

        self._sent.clear()
        client = self._bridge._clients[cid]
        self._bridge._handle_console_rx(client, b"\x1b[B")
        self.assertTrue(any(data == b"\x1b[B" for data, _ in self._sent))

    def test_deferred_buffer_not_echoed_locally(self) -> None:
        """deferred 期間不做 local echo（human 打字不顯示）。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        self._bridge.suspend_interactive()
        client = self._bridge._clients[cid]

        with mock.patch.object(self._bridge, "_write_console_best_effort") as mock_write:
            self._bridge._handle_console_rx(client, b"hello")
            mock_write.assert_not_called()

    def test_non_suspended_console_stays_in_line_buffer(self) -> None:
        """agent active 時，非 suspended owner 的 console 仍走 line-buffer。"""
        owner_console = self._bridge.attach_console(label="owner")
        other_console = self._bridge.attach_console(label="other")
        owner_cid = owner_console["client_id"]
        other_cid = other_console["client_id"]
        self._bridge.set_interactive_owner(f"human:{owner_cid}")

        lines_received: list[str] = []
        self._bridge._on_console_line = lambda cid, line: lines_received.append(line)

        self._bridge.suspend_interactive()
        other_client = self._bridge._clients[other_cid]
        self._bridge._handle_console_rx(other_client, b"ls\n")

        self.assertEqual(lines_received, ["ls"])

    def test_suspend_when_no_interactive_is_noop(self) -> None:
        """沒有 interactive owner 時 suspend/resume 不崩潰。"""
        self._bridge.suspend_interactive()

        with self._bridge._state_lock:
            self.assertTrue(self._bridge._agent_active)
            self.assertIsNone(self._bridge._suspended_owner)

        self._bridge.resume_interactive()

        with self._bridge._state_lock:
            self.assertFalse(self._bridge._agent_active)
            self.assertIsNone(self._bridge._interactive_owner)


class TestConsoleClientInternalFlag(unittest.TestCase):
    """Task 2：ConsoleClient.internal 旗標——哨兵 primary vs 真實 console。"""

    def test_start_primary_is_internal_sentinel(self) -> None:
        """start() 建立的哨兵 primary 應標記 internal=True。"""
        bridge, master_fd, slave_fd, tmpdir = _make_pty_bridge()
        try:
            bridge.start()
            with bridge._state_lock:
                primary = bridge._clients[bridge._primary_client_id]
            self.assertTrue(primary.internal, "start() 建的哨兵 primary 應 internal=True")
        finally:
            bridge.stop()
            os.close(master_fd)
            os.close(slave_fd)
            tmpdir.cleanup()

    def test_attach_console_client_is_not_internal(self) -> None:
        """attach_console() 建的真實 console 應標記 internal=False。"""
        bridge, master_fd, slave_fd, tmpdir = _make_pty_bridge()
        try:
            bridge.start()
            info = bridge.attach_console(label="minicom-test")
            with bridge._state_lock:
                client = bridge._clients[info["client_id"]]
            self.assertFalse(client.internal, "attach_console 建的真實 console 應 internal=False")
        finally:
            bridge.stop()
            os.close(master_fd)
            os.close(slave_fd)
            tmpdir.cleanup()


class TestReapStaleConsoles(unittest.TestCase):
    """Task 3：UARTBridge.reap_stale_consoles() 主動回收孤兒 console。"""

    def test_reap_drops_orphan_non_primary(self) -> None:
        """孤兒（過 grace、無外部 reader）非 internal console 應被 reaper 回收。"""
        bridge, master_fd, slave_fd, tmpdir = _make_pty_bridge()
        try:
            bridge.start()
            info = bridge.attach_console(label="orphan")
            cid = info["client_id"]
            with bridge._state_lock:
                bridge._clients[cid].attached_at -= 10.0  # 強制過 grace
            reaped = bridge.reap_stale_consoles(held_slave_paths=set())  # 模擬「無人持有任何 slave」
            self.assertIn(cid, [c.client_id for c in reaped])
            with bridge._state_lock:
                self.assertNotIn(cid, bridge._clients)
        finally:
            bridge.stop()
            os.close(master_fd)
            os.close(slave_fd)
            tmpdir.cleanup()

    def test_reap_never_touches_internal_sentinel(self) -> None:
        """internal=True 的哨兵 primary 即使過 grace 也絕不可被回收。"""
        bridge, master_fd, slave_fd, tmpdir = _make_pty_bridge()
        try:
            bridge.start()
            pid = bridge._primary_client_id
            with bridge._state_lock:
                bridge._clients[pid].attached_at -= 10.0
            bridge.reap_stale_consoles(held_slave_paths=set())
            with bridge._state_lock:
                self.assertIn(pid, bridge._clients, "internal 哨兵 primary 不得被回收")
        finally:
            bridge.stop()
            os.close(master_fd)
            os.close(slave_fd)
            tmpdir.cleanup()

    def test_reap_skips_owner_and_suspended_owner(self) -> None:
        """reaper 不可回收當前 owner 與 suspended owner。"""
        bridge, master_fd, slave_fd, tmpdir = _make_pty_bridge()
        try:
            bridge.start()
            info = bridge.attach_console(label="owner")
            cid = info["client_id"]
            bridge.set_interactive_owner(f"human:{cid}")
            with bridge._state_lock:
                bridge._clients[cid].attached_at -= 10.0
            bridge.reap_stale_consoles(held_slave_paths=set())
            with bridge._state_lock:
                self.assertIn(cid, bridge._clients, "當前 owner 不得被 reaper 回收")
            # suspended owner（agent 命令中）
            bridge.suspend_interactive()
            bridge.reap_stale_consoles(held_slave_paths=set())
            with bridge._state_lock:
                self.assertIn(cid, bridge._clients, "suspended owner 不得被 reaper 回收")
                self.assertEqual(bridge._suspended_owner, f"human:{cid}")
            bridge.resume_interactive()
        finally:
            bridge.stop()
            os.close(master_fd)
            os.close(slave_fd)
            tmpdir.cleanup()

    def test_reap_conservative_when_procfs_unavailable(self) -> None:
        """procfs 不可用時，reaper 保守不回收活著的 console（_scan_held_slave_paths 回全集）。"""
        bridge, master_fd, slave_fd, tmpdir = _make_pty_bridge()
        real_listdir = os.listdir

        def fake_listdir(path):
            if path == "/proc":
                raise OSError("procfs unavailable")
            return real_listdir(path)

        try:
            bridge.start()
            info = bridge.attach_console(label="live")
            cid = info["client_id"]
            with bridge._state_lock:
                bridge._clients[cid].attached_at -= 10.0  # 過 grace，否則不進候選
            # 不傳 held_slave_paths → 觸發 _scan_held_slave_paths → /proc 掃描失敗 → 保守回全集
            with mock.patch("sw_core.uart_io.os.listdir", side_effect=fake_listdir):
                reaped = bridge.reap_stale_consoles()
            self.assertEqual(reaped, [], "procfs 不可用時不得回收任何 console")
            with bridge._state_lock:
                self.assertIn(cid, bridge._clients, "procfs 不可用時活 console 須保留")
        finally:
            bridge.stop()
            os.close(master_fd)
            os.close(slave_fd)
            tmpdir.cleanup()


class TestSnapshotDecisionFieldsAndAtomicGrant(unittest.TestCase):
    """Task 6：snapshot() 擴充決策欄位 + try_grant_interactive_if_idle 原子 grant（Codex finding-2）。"""

    def _make_idle_bridge(self) -> tuple[UARTBridge, tempfile.TemporaryDirectory]:
        tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=tmpdir.name)
        bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )
        return bridge, tmpdir

    def test_snapshot_exposes_decision_fields(self) -> None:
        """snapshot() 必須包含 agent_active / suspended_owner / flash_mode / primary_client_id 四個決策欄位。"""
        bridge, tmpdir = self._make_idle_bridge()
        try:
            snap = bridge.snapshot()
            for key in ("agent_active", "suspended_owner", "flash_mode", "primary_client_id"):
                self.assertIn(key, snap, f"snapshot() 缺少欄位：{key}")
            # 確認初始值
            self.assertFalse(snap["agent_active"])
            self.assertIsNone(snap["suspended_owner"])
            self.assertFalse(snap["flash_mode"])
            self.assertIsNone(snap["primary_client_id"])
        finally:
            bridge.stop()
            tmpdir.cleanup()

    def test_try_grant_if_idle_succeeds_when_idle(self) -> None:
        """完全 idle bridge（無 owner/suspended/agent/flash）→ grant 回 True，owner 被設定。"""
        bridge, tmpdir = self._make_idle_bridge()
        try:
            result = bridge.try_grant_interactive_if_idle("human:test-client")
            self.assertTrue(result, "idle bridge 應成功 grant")
            with bridge._state_lock:
                self.assertEqual(bridge._interactive_owner, "human:test-client")
        finally:
            bridge.stop()
            tmpdir.cleanup()

    def test_try_grant_if_idle_fails_when_owner_set(self) -> None:
        """已有 interactive owner → grant 回 False，owner 不變。"""
        bridge, tmpdir = self._make_idle_bridge()
        try:
            bridge.set_interactive_owner("human:existing-owner")
            result = bridge.try_grant_interactive_if_idle("human:new-client")
            self.assertFalse(result, "有 owner 時不得 grant")
            with bridge._state_lock:
                self.assertEqual(bridge._interactive_owner, "human:existing-owner")
        finally:
            bridge.stop()
            tmpdir.cleanup()

    def test_try_grant_if_idle_fails_when_agent_active(self) -> None:
        """agent 執行中（suspend_interactive 後 _agent_active=True）→ grant 回 False。"""
        bridge, tmpdir = self._make_idle_bridge()
        try:
            # 先設 owner，再 suspend → _suspended_owner 非 None、_agent_active=True、_interactive_owner=None
            bridge.set_interactive_owner("human:original-owner")
            bridge.suspend_interactive()
            with bridge._state_lock:
                self.assertTrue(bridge._agent_active)
                self.assertEqual(bridge._suspended_owner, "human:original-owner")
                self.assertIsNone(bridge._interactive_owner)
            result = bridge.try_grant_interactive_if_idle("human:new-client")
            self.assertFalse(result, "agent 執行中（suspended）不得 grant")
            with bridge._state_lock:
                self.assertIsNone(bridge._interactive_owner, "grant 失敗後 owner 仍應為 None")
        finally:
            bridge.resume_interactive()
            bridge.stop()
            tmpdir.cleanup()
