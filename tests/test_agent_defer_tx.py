"""測試 UARTBridge agent 命令執行期間暫存 minicom TX，命令完成後排程送出。"""
from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


class TestAgentDeferHumanTx(unittest.TestCase):
    """驗證 begin/end_agent_cmd + _loop() TX buffering 行為。"""

    def _make_bridge(self):
        """建立 UARTBridge（不開啟真實 serial，只測 flag/queue 邏輯）。"""
        from sw_core.uart_io import UARTBridge
        from sw_core.config import UartProfile
        profile = UartProfile()
        wal = MagicMock()
        bridge = UARTBridge(com="COM0", device_path="/dev/null", profile=profile, wal=wal)
        return bridge

    # ── begin / end_agent_cmd ────────────────────────────────────────────

    def test_begin_sets_flag(self):
        bridge = self._make_bridge()
        self.assertFalse(bridge._agent_active.is_set())
        bridge.begin_agent_cmd()
        self.assertTrue(bridge._agent_active.is_set())

    def test_end_clears_flag(self):
        bridge = self._make_bridge()
        bridge.begin_agent_cmd()
        bridge._serial_fd = None  # 已停止，讓 end_agent_cmd 直接清 queue
        bridge.end_agent_cmd()
        self.assertFalse(bridge._agent_active.is_set())

    def test_end_flushes_queue_to_serial(self):
        bridge = self._make_bridge()
        # 模擬 _serial_fd 為 fake fd；攔截 _write_all
        bridge._serial_fd = 99
        written = []
        bridge._write_all = lambda fd, data: written.append(data)

        # 塞入兩筆暫存資料
        bridge._agent_active.set()
        bridge._human_tx_queue.put(b"hello\n")
        bridge._human_tx_queue.put(b"world\n")

        bridge.end_agent_cmd()

        self.assertFalse(bridge._agent_active.is_set())
        self.assertEqual(written, [b"hello\n", b"world\n"])
        self.assertTrue(bridge._human_tx_queue.empty())

    def test_end_discards_queue_when_serial_closed(self):
        bridge = self._make_bridge()
        bridge._serial_fd = None  # serial 已關閉
        bridge._agent_active.set()
        bridge._human_tx_queue.put(b"orphan\n")

        bridge.end_agent_cmd()

        # flag 清除，queue 清空（資料丟棄，不 raise）
        self.assertFalse(bridge._agent_active.is_set())
        self.assertTrue(bridge._human_tx_queue.empty())

    # ── rx_snapshot_len / wait_for_regex_from ───────────────────────────

    def test_rx_snapshot_len(self):
        bridge = self._make_bridge()
        bridge._rx_text = "abc"
        self.assertEqual(bridge.rx_snapshot_len(), 3)

    def test_wait_for_regex_from_ignores_old_data(self):
        bridge = self._make_bridge()
        # 在 offset=0 之前就有 prompt，但 from_offset=10 之後才看
        bridge._rx_text = "root@box:~# \n" + "cmd output"
        # offset=10 之後沒有 prompt → should timeout quickly
        result = bridge.wait_for_regex_from(r".*# $", from_offset=10, timeout_s=0.1)
        self.assertFalse(result)

    def test_wait_for_regex_from_sees_new_prompt(self):
        bridge = self._make_bridge()
        bridge._rx_text = "old stuff"
        pre = bridge.rx_snapshot_len()

        # 背景執行緒模擬 target 在 0.1s 後送回 prompt
        def inject():
            time.sleep(0.1)
            with bridge._rx_lock:
                bridge._rx_text += "\nroot@box:~# "

        t = threading.Thread(target=inject, daemon=True)
        t.start()

        result = bridge.wait_for_regex_from(r"# $", from_offset=pre, timeout_s=1.0)
        self.assertTrue(result)

    # ── _loop() TX 暫存行為（透過 _agent_active + queue 驗證）───────────

    def test_loop_buffers_pty_tx_during_agent_cmd(self):
        """_loop() 讀到 PTY TX 時若 _agent_active，不送 serial 而是放 queue。"""
        import os
        import select as _select
        from sw_core.uart_io import UARTBridge
        from sw_core.config import UartProfile

        wal = MagicMock()
        bridge = UARTBridge(
            com="COM0", device_path="/dev/null",
            profile=UartProfile(baud=115200), wal=wal
        )

        # 建立 PTY pair + fake serial pipe
        pty_master, pty_slave = os.openpty()
        serial_r, serial_w = os.pipe()

        bridge._serial_fd = serial_r
        bridge._pty_master = pty_master
        bridge._pty_slave = pty_slave
        bridge._pty_slave_path = os.ttyname(pty_slave)

        # 讓 _write_all 對 serial_r 的寫入不實際觸發，攔截 serial 寫入
        serial_written: list[bytes] = []
        orig_write_all = bridge._write_all

        def mock_write_all(fd, data):
            if fd == serial_r:
                serial_written.append(data)
            else:
                orig_write_all(fd, data)

        bridge._write_all = mock_write_all

        # 啟動 _loop()
        bridge._stop_event.clear()
        t = threading.Thread(target=bridge._loop, daemon=True)
        t.start()

        # 先等 loop 起來，然後 set agent_active
        time.sleep(0.05)
        bridge.begin_agent_cmd()

        # 模擬 minicom 使用者輸入（寫到 pty_slave → _loop 從 pty_master 讀到）
        os.write(pty_slave, b"hello\n")
        time.sleep(0.15)

        # agent_active 期間：不應送到 serial，但應在 queue 裡
        self.assertEqual(serial_written, [], "agent_active 期間不應直接寫 serial")
        self.assertFalse(bridge._human_tx_queue.empty(), "應有暫存資料在 queue")

        # 結束 agent_cmd → 暫存資料應排程送出
        bridge.end_agent_cmd()
        time.sleep(0.05)
        self.assertEqual(serial_written, [b"hello\r\n"], "end_agent_cmd 後應排程送出")

        # 清理
        bridge._stop_event.set()
        t.join(timeout=1)
        for fd in (pty_master, pty_slave, serial_r, serial_w):
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
