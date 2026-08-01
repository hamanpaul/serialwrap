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


class TestRxOffsetBoundedBuffer(unittest.TestCase):
    """#158：RX 視窗有界修剪不得破壞 offset 語意（絕對串流偏移記帳）。

    根因重演：`_rx_text` 觸頂修剪前端但不記帳 → `rx_snapshot_len()` 飽和後恆等於視窗上限
    → `wait_for_regex_from(pattern, pre_offset)` 切片永遠空字串 → prompt 永不匹配 →
    PROMPT_TIMEOUT、stdout 空、recovery 誤送 CTRL_D。本組測試把 `_rx_max_chars` 縮到 256
    保持毫秒級，直接餵 `_append_rx_text`（不 start()，不碰真實序列埠）。
    """

    RESPONSE = b"echo x\r\nx\r\nroot@prplOS:/# "

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )
        self._bridge._rx_max_chars = 256

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def _fill(self, n_chars: int) -> None:
        """灌 n_chars 個 filler 字元進 RX 緩衝。"""
        self._bridge._append_rx_text(b"." * n_chars)

    def test_prompt_match_survives_trim_crossing(self) -> None:
        """#158 精確重演：回應 append 恰好觸發視窗修剪（跨界），prompt 仍必須匹配得到。"""
        bridge = self._bridge
        self._fill(bridge._rx_max_chars - 10)
        pre = bridge.rx_snapshot_len()
        bridge._append_rx_text(self.RESPONSE)  # 跨界 → 觸發前端修剪
        self.assertTrue(
            bridge.wait_for_regex_from(r"root@prplOS", pre, 0.5),
            "跨界修剪後 prompt 匹配不到（#158 回歸：offset 語意被修剪破壞）",
        )
        self.assertEqual(bridge.rx_text_from(pre), self.RESPONSE.decode("utf-8"))

    def test_saturated_buffer_still_matches(self) -> None:
        """飽和態（對應實機失敗當下）：緩衝已在上限，pre 取於飽和後，回應仍必須匹配得到。"""
        bridge = self._bridge
        self._fill(bridge._rx_max_chars)  # 恰好觸頂
        pre = bridge.rx_snapshot_len()
        bridge._append_rx_text(self.RESPONSE)
        self.assertTrue(
            bridge.wait_for_regex_from(r"root@prplOS", pre, 0.5),
            "飽和緩衝下 prompt 匹配不到（#158 回歸）",
        )
        self.assertEqual(bridge.rx_text_from(pre), self.RESPONSE.decode("utf-8"))

    def test_snapshot_len_monotonic_after_saturation(self) -> None:
        """飽和後持續 append，rx_snapshot_len() 必須每次嚴格遞增（釘死靜默偵測誤判）。"""
        bridge = self._bridge
        self._fill(bridge._rx_max_chars)
        prev = bridge.rx_snapshot_len()
        for _ in range(5):
            bridge._append_rx_text(b"tick\r\n")
            cur = bridge.rx_snapshot_len()
            self.assertGreater(
                cur, prev,
                "飽和後 rx_snapshot_len 未嚴格遞增（#158 回歸：靜默偵測在飽和下恆真）",
            )
            prev = cur

    def test_offset_in_trimmed_head_returns_window(self) -> None:
        """offset 落在已修剪頭段：降級回傳現存全窗（非空、不拋例外），而非丟失回空。"""
        bridge = self._bridge
        pre = bridge.rx_snapshot_len()  # =0
        self._fill(2 * bridge._rx_max_chars)
        text = bridge.rx_text_from(pre)
        self.assertEqual(len(text), bridge._rx_max_chars)
        with bridge._rx_lock:
            self.assertEqual(text, bridge._rx_text)

    def test_clear_rx_buffer_keeps_offsets_monotonic(self) -> None:
        """clear_rx_buffer 後絕對偏移不得回退；clear 前的舊 offset 讀到 clear 後新資料。"""
        bridge = self._bridge
        self._fill(100)
        pre = bridge.rx_snapshot_len()
        bridge.clear_rx_buffer()
        self.assertGreaterEqual(bridge.rx_snapshot_len(), pre, "clear 後絕對偏移回退")
        bridge._append_rx_text(b"new")
        self.assertEqual(bridge.rx_text_from(pre), "new")

    def test_snapshot_exposes_rx_dropped_chars(self) -> None:
        """snapshot() 暴露 rx_dropped_chars（累計丟棄量）供鑑識；未修剪時為 0。"""
        bridge = self._bridge
        self.assertEqual(bridge.snapshot()["rx_dropped_chars"], 0)
        self._fill(bridge._rx_max_chars + 40)
        self.assertEqual(bridge.snapshot()["rx_dropped_chars"], 40)


class TestRxStats(unittest.TestCase):
    """#153：UARTBridge RX 速率統計視窗（rx_stats）。

    不 start()、不碰真實序列埠；直接餵 _append_rx_text 並以可控時鐘驗證
    視窗剪枝、平均速率與筆數上限防呆。計量以 raw bytes（含 ANSI）為準。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=wal,
        )
        self._clock = {"t": 0.0}

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def _monotonic(self) -> float:
        return self._clock["t"]

    def test_window_prunes_entries_older_than_10s(self) -> None:
        """視窗只含最近 10s 的 bytes；超窗舊項剪除、rx_rate_bps 為視窗平均。"""
        import sw_core.uart_io as uart_io_mod

        bridge = self._bridge
        with mock.patch.object(uart_io_mod.time, "monotonic", side_effect=self._monotonic):
            self._clock["t"] = 0.0
            bridge._append_rx_text(b"a" * 1000)
            self._clock["t"] = 5.0
            bridge._append_rx_text(b"b" * 2000)
            self._clock["t"] = 12.0
            bridge._append_rx_text(b"c" * 4000)
            stats = bridge.rx_stats()
        # t=12：t=0 的 1000B 已超窗（age 12 > 10）剪除；剩 2000+4000。
        self.assertEqual(stats["rx_bytes_last_10s"], 6000)
        self.assertEqual(stats["rx_rate_bps"], 600)  # 6000 / 10s

    def test_raw_bytes_counted_including_ansi(self) -> None:
        """計量以 raw bytes 為準：ANSI/控制碼照算（對洪水最誠實）。"""
        import sw_core.uart_io as uart_io_mod

        bridge = self._bridge
        with mock.patch.object(uart_io_mod.time, "monotonic", side_effect=self._monotonic):
            bridge._append_rx_text(b"ab\x1b[0m")
            stats = bridge.rx_stats()
        self.assertEqual(stats["rx_bytes_last_10s"], 6)

    def test_stats_drop_to_zero_after_window_passes(self) -> None:
        """視窗滑過後 rx_stats 歸零（rx_stats 讀取時亦剪枝）。"""
        import sw_core.uart_io as uart_io_mod

        bridge = self._bridge
        with mock.patch.object(uart_io_mod.time, "monotonic", side_effect=self._monotonic):
            self._clock["t"] = 0.0
            bridge._append_rx_text(b"x" * 5000)
            self._clock["t"] = 30.0
            stats = bridge.rx_stats()
        self.assertEqual(stats["rx_bytes_last_10s"], 0)
        self.assertEqual(stats["rx_rate_bps"], 0)

    def test_window_entry_hard_cap(self) -> None:
        """同一瞬間灌超過 4096 筆：deque maxlen 防呆丟最舊，不無界成長。"""
        import sw_core.uart_io as uart_io_mod

        bridge = self._bridge
        with mock.patch.object(uart_io_mod.time, "monotonic", side_effect=self._monotonic):
            for _ in range(5000):
                bridge._append_rx_text(b"z")
            stats = bridge.rx_stats()
        self.assertEqual(len(bridge._rx_window), 4096)
        self.assertEqual(stats["rx_bytes_last_10s"], 4096)

    def test_rx_total_bytes_monotonic_and_clear_immune(self) -> None:
        """#150：rx_total_bytes 單調遞增、clear_rx_buffer 不歸零；snapshot 露出。"""
        bridge = self._bridge
        bridge._handle_serial_rx(b"ab\x1b[0m")
        self.assertEqual(bridge.rx_total_bytes(), 6)
        bridge.clear_rx_buffer()
        self.assertEqual(bridge.rx_total_bytes(), 6, "clear 後 raw RX 累計不得歸零")
        bridge._handle_serial_rx(b"cd")
        self.assertEqual(bridge.rx_total_bytes(), 8)
        self.assertEqual(bridge.snapshot()["rx_total_bytes"], 8)


class TestSendCommandEchoPaced(unittest.TestCase):
    """#161：send_command_echo_paced／_await_echo_progress／cancel_input_line。

    不 start()、不碰真實序列埠：monkeypatch send_bytes 記錄送出 bytes，並以可控的
    echo 函式把回顯餵進 _append_rx_text，模擬板端逐段 echo／插噪音／停滯。
    """

    CMD = "printf '%s' 'QUJDREVGRw==' | base64 -d >> /tmp/.sw_upload_0123456789ab && echo done-0123"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._wal = mock.Mock()
        self._bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=UartProfile(),
            wal=self._wal,
        )
        self._sent: list[bytes] = []

    def tearDown(self) -> None:
        self._bridge.stop()
        self._tmpdir.cleanup()

    def _install_send(self, echo_fn) -> None:
        """monkeypatch send_bytes：記錄送出 bytes，echo_fn(data)->bytes|None 為板端回顯。"""

        def fake_send(data: bytes, *, source: str = "", cmd_id=None, log: bool = True, **_kw) -> None:
            self._sent.append(bytes(data))
            echo = echo_fn(bytes(data))
            if echo:
                self._bridge._append_rx_text(echo)

        self._bridge.send_bytes = fake_send  # type: ignore[assignment]

    def test_paced_send_all_slices_acked_then_newline(self) -> None:
        """正常逐段確認：每段 echo 同步到達 → ok=True，換行最後單獨送出。"""
        self._install_send(lambda data: data)  # 板端原樣 echo
        result = self._bridge.send_command_echo_paced(
            self.CMD, source="file_transfer", cmd_id="ft-1", slice_size=16)
        self.assertTrue(result["ok"])
        self.assertEqual(result["acked_chars"], len(self.CMD))
        self.assertEqual(result["sent_chars"], len(self.CMD))
        self.assertEqual(self._sent[-1], b"\n", "換行必須在全段確認後單獨送出")
        self.assertEqual(b"".join(self._sent), self.CMD.encode() + b"\n")
        for piece in self._sent[:-1]:
            self.assertLessEqual(len(piece), 16)

    def test_noise_between_slices_still_matches(self) -> None:
        """slice 間插入 printk 噪音與 ANSI 殘字：移動起點 find 仍逐段比對成功。"""

        def noisy_echo(data: bytes) -> bytes:
            return b"\r\n[  12.345678] printk noise\x1b[0m\r\n" + data

        self._install_send(noisy_echo)
        result = self._bridge.send_command_echo_paced(
            self.CMD, source="file_transfer", slice_size=16)
        self.assertTrue(result["ok"], f"噪音插入不應造成停滯：{result}")
        self.assertEqual(result["acked_chars"], len(self.CMD))

    def test_echo_crlf_normalized(self) -> None:
        """板端 echo 帶 CR/LF 折行：去 CR/LF 正規化後仍比對成功。"""
        self._install_send(lambda data: data.replace(b" ", b" \r\n"))
        result = self._bridge.send_command_echo_paced(
            self.CMD, source="file_transfer", slice_size=16)
        self.assertTrue(result["ok"])

    def test_echo_stall_returns_not_ok_without_newline(self) -> None:
        """echo 停滯：第二段起無 echo → ok=False、acked 停在第一段、絕不送換行。"""
        state = {"count": 0}

        def stalling_echo(data: bytes) -> bytes | None:
            state["count"] += 1
            return data if state["count"] == 1 else None  # 只 echo 第一段

        self._install_send(stalling_echo)
        result = self._bridge.send_command_echo_paced(
            self.CMD, source="file_transfer", slice_size=16, echo_timeout_s=0.05)
        self.assertFalse(result["ok"])
        self.assertEqual(result["acked_chars"], 16)
        self.assertEqual(result["sent_chars"], 32, "停滯後不得再送後續 slice")
        self.assertNotIn(b"\n", b"".join(self._sent), "停滯時換行不得送出（命令不得執行）")

    def test_wal_single_record_per_command(self) -> None:
        """WAL 一命令一筆 TX：成功記全文＋換行；停滯記實際已送出的部分。"""
        self._install_send(lambda data: data)
        self._bridge.send_command_echo_paced(
            self.CMD, source="file_transfer", cmd_id="ft-9", slice_size=16)
        self._wal.append.assert_called_once()
        kwargs = self._wal.append.call_args.kwargs
        self.assertEqual(kwargs["payload"], self.CMD.encode() + b"\n")
        self.assertEqual(kwargs["direction"], "TX")
        self.assertEqual(kwargs["cmd_id"], "ft-9")

        self._wal.append.reset_mock()
        self._sent.clear()
        self._install_send(lambda data: None)  # 全程無 echo
        result = self._bridge.send_command_echo_paced(
            self.CMD, source="file_transfer", slice_size=16, echo_timeout_s=0.05)
        self.assertFalse(result["ok"])
        self._wal.append.assert_called_once()
        partial = self._wal.append.call_args.kwargs["payload"]
        self.assertEqual(partial, self.CMD.encode()[:16], "停滯時 WAL 記實際送出的部分")

    def test_crlf_terminated_command_strips_cr_too(self) -> None:
        """CRLF 結尾的命令：``\\r`` 必須與 ``\\n`` 一起去掉（Copilot review）。

        只 rstrip("\\n") 會把 ``\\r`` 留在 body 尾端當命令本文送出：
        (1) 板端多半直接把它當換行執行命令——早於本函式自己送的 ``\\n``，
            破壞「全段確認才送換行＝停滯時命令未執行＝可安全重試」的核心不變量；
        (2) `_await_echo_progress` 的比對已把 RX 的 CR/LF 正規化掉，該字元永遠
            ack 不到，必然變成假性 stall。
        """
        self._install_send(lambda data: data)

        result = self._bridge.send_command_echo_paced(
            self.CMD + "\r\n", source="file_transfer", slice_size=16)

        self.assertTrue(result["ok"], f"CRLF 結尾不得造成假性 stall：{result}")
        self.assertEqual(result["sent_chars"], len(self.CMD), "\\r 不得計入命令本文")
        self.assertEqual(b"".join(self._sent), self.CMD.encode() + b"\n")
        self.assertNotIn(b"\r", b"".join(self._sent), "\\r 絕不得送上 UART")

    def test_lf_only_termination_unchanged(self) -> None:
        """反向斷言：既有 LF 結尾行為逐字不變（rstrip 放寬不得改動正常路徑）。"""
        self._install_send(lambda data: data)

        result = self._bridge.send_command_echo_paced(
            self.CMD + "\n", source="file_transfer", slice_size=16)

        self.assertTrue(result["ok"])
        self.assertEqual(b"".join(self._sent), self.CMD.encode() + b"\n")

    def test_cancel_input_line_sends_ctrl_u_newline(self) -> None:
        """cancel_input_line：送 \\x15＋\\n（Ctrl-U 清行＋換行重取 prompt）。"""
        self._install_send(lambda data: None)
        self._bridge.cancel_input_line(source="file_transfer")
        self.assertEqual(self._sent, [b"\x15\n"])
