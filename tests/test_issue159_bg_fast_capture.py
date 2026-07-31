"""#159：background 模式快速完成命令的 result_tail 完整性測試。

根因（複合）：
1. `_execute_command_inner` 對 fg/bg 一視同仁設 `foreground_busy`；
2. `_on_bridge_rx` 用同一個 `foreground_busy` gate 連坐擋掉 background capture；
3. `BackgroundCapture` 在 prompt 比對成功「之後」才回溯建立——快速完成的命令
   全部輸出都落在建立之前，capture 從頭到尾空的，卻回 `lost: False` 假保證。

修法：capture 於命令送出前掛好、`_on_bridge_rx` 的 background 累積迴圈移到
`foreground_busy` gate 之前、`_set_terminal_capture_locked` 僅新建分支回填 chunks。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class TestIssue159BgFastCapture(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_profile(self) -> SessionProfile:
        return SessionProfile(
            profile_name="p", com="COM0", act_no=1, alias="lab",
            device_by_id="/dev/serial/by-id/dev0",
            platform="shell",
            prompt_regex=r"[$#] $",
            uart=UartProfile(),
        )

    def _setup_ready_session(self):
        """建立一個 READY session 並回傳 (mgr, session, bridge)。"""
        profiles = [self._make_profile()]
        mgr = SessionManager(
            profiles, WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None, on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        session.bridge = bridge
        session.state = "READY"
        return mgr, session, bridge

    def test_background_fast_command_result_tail_gets_full_output(self) -> None:
        """#159：background 模式的快速完成命令，result_tail 必須拿到完整輸出，
        不得因 capture 掛載時序落後於命令完成而整段吞掉。"""
        mgr, session, bridge = self._setup_ready_session()

        output = "".join(f"L{i}\n" for i in range(1, 51))
        full_rx = output + "$ "

        def fake_wait(pattern, from_offset, timeout_s):
            # 模擬：命令的完整輸出在「prompt 比對成功」這一刻之前，
            # 就已經整批經由 bridge reader thread 送達（典型快速完成命令）。
            # 此時 foreground_busy 仍為 True——修復前這段 RX 會被 gate 掉，
            # 且 capture 根本尚未建立。
            mgr._on_bridge_rx(session.session_id, full_rx.encode("utf-8"))
            return True

        bridge.wait_for_regex_from.side_effect = fake_wait
        bridge.rx_snapshot_len.return_value = 0
        bridge.rx_text_from.return_value = full_rx

        result = mgr.execute_command(
            "p:COM0", "for i in $(seq 1 50); do echo L$i; done",
            "agent:test", "cid-bg-fast", timeout_s=5.0, mode="bg",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("background_capture_id"), "cid-bg-fast")

        tail = mgr.get_background_result("cid-bg-fast", from_chunk=0)
        self.assertTrue(tail["ok"])
        combined = "".join(tail["chunks"])
        for i in range(1, 51):
            self.assertIn(f"L{i}\n", combined, f"L{i} missing — #159 regression")
        self.assertEqual(tail["dropped_chunks"], 0)
        self.assertFalse(tail["lost"])

    def test_background_timeout_recovery_does_not_duplicate_chunks(self) -> None:
        """#159 review：background 命令逾時走 CTRL_C 復原路徑時，
        `_set_terminal_capture_locked` 不得對已即時累積過的 capture 重複回填相同內容。"""
        mgr, session, bridge = self._setup_ready_session()

        calls = {"n": 0}

        def fake_wait(pattern, from_offset, timeout_s):
            calls["n"] += 1
            if calls["n"] == 1:
                # 等待迴圈第一輪：部分輸出即時送達（經 _on_bridge_rx 進 capture），
                # 但 prompt 未出現 → False。
                mgr._on_bridge_rx(session.session_id, b"partial-marker\n")
                return False
            # CTRL_C 復原後 prompt 回來。
            return True

        # 呼叫序：pre_offset(10)、等待迴圈 pre_rx(10)、silence check(10)→靜默 break、
        # 進入 recover：CTRL_C offset(20)。
        bridge.rx_snapshot_len.side_effect = [10, 10, 10, 20]
        bridge.wait_for_regex_from.side_effect = fake_wait
        # 復原路徑 rx_text_from(pre_offset) 全量重讀：同一段輸出 + prompt。
        bridge.rx_text_from.return_value = "partial-marker\n$ "

        result = mgr.execute_command(
            "p:COM0", "sleep 999", "agent:test", "cid-bg-slow",
            timeout_s=0.1, mode="bg",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["error_code"], "PROMPT_TIMEOUT_RECOVERED")

        tail = mgr.get_background_result("cid-bg-slow", from_chunk=0)
        self.assertTrue(tail["ok"])
        self.assertEqual(tail["status"], "error")
        self.assertEqual(tail["error_code"], "PROMPT_TIMEOUT_RECOVERED")
        combined = "".join(tail["chunks"])
        # 即時累積的內容必須在（誠實性），且不得因復原路徑全量重讀而重複。
        self.assertEqual(
            combined.count("partial-marker"), 1,
            f"partial-marker 應恰出現一次，實得 chunks={tail['chunks']!r}",
        )
        self.assertEqual(tail["dropped_chunks"], 0)
        self.assertFalse(tail["lost"])

    def test_line_mode_timeout_backfill_unchanged(self) -> None:
        """改動 3 的 else 分支回歸：line 模式逾時（capture 先前未掛載）仍走
        「新建＋回填」，行為與修復前相同。"""
        mgr, session, bridge = self._setup_ready_session()

        # 呼叫序同上，但全程無 RX 即時送達（無 _on_bridge_rx）。
        bridge.rx_snapshot_len.side_effect = [10, 10, 10, 20]
        bridge.wait_for_regex_from.side_effect = [False, True]
        bridge.rx_text_from.return_value = "partial line\n$ "

        resp = mgr.execute_command(
            "p:COM0", "cat", "agent:test", "cid-line-timeout", timeout_s=0.1,
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["error_code"], "PROMPT_TIMEOUT_RECOVERED")

        tail = mgr.get_background_result("cid-line-timeout")
        self.assertTrue(tail["ok"])
        self.assertEqual(tail["status"], "error")
        self.assertEqual(tail["chunks"], ["partial line"])


if __name__ == "__main__":
    unittest.main()
