"""Issue #34 — session activity / heartbeat observability tests.

Covers SessionRuntime activity fields, classification, and the
integration with SessionManager._on_bridge_rx and _mark_session_tx.
"""
from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

from sw_core.config import SessionProfile
from sw_core.session_manager import SessionManager, SessionRuntime

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


def _make_profile(**overrides: Any) -> SessionProfile:
    defaults: dict[str, Any] = {
        "profile_name": "test",
        "platform": "shell",
        "com": "COM0",
        "act_no": 0,
        "alias": "t0",
        "device_by_id": "/dev/serial/by-id/test",
        "prompt_regex": r"[$#] $",
        "login_regex": r"(?mi)login:\s*$",
        "password_regex": r"(?mi)password:\s*$",
        "user_env": "U",
        "pass_env": "P",
        "ready_probe": "echo __READY__${nonce}",
        "timeout_s": 5.0,
    }
    defaults.update(overrides)
    return SessionProfile(**defaults)


def _make_session(state: str = "DETACHED") -> SessionRuntime:
    s = SessionRuntime(session_id="sid-1", profile=_make_profile())
    if state != "DETACHED":
        s.state = state
    return s


class TestSessionRuntimeActivityFields(unittest.TestCase):
    """SessionRuntime 新增的 activity 欄位與 helper 方法測試。"""

    def test_initial_state_is_detached_with_no_activity(self) -> None:
        s = _make_session()
        self.assertEqual(s.state, "DETACHED")
        self.assertIsNone(s.last_rx_at)
        self.assertIsNone(s.last_tx_at)
        self.assertIsNone(s.last_state_change_at)
        self.assertIsNone(s.last_probe_at)
        self.assertEqual(s.last_rx_mono, 0.0)
        self.assertEqual(s.last_tx_mono, 0.0)

    def test_state_change_auto_updates_last_state_change_at(self) -> None:
        s = _make_session()
        self.assertIsNone(s.last_state_change_at)
        s.state = "ATTACHING"
        first = s.last_state_change_at
        self.assertIsNotNone(first)
        s.state = "READY"
        second = s.last_state_change_at
        self.assertIsNotNone(second)
        # different transitions produce a fresh timestamp string
        self.assertNotEqual(first, second)

    def test_state_set_to_same_value_does_not_update_timestamp(self) -> None:
        s = _make_session()
        s.state = "READY"
        ts = s.last_state_change_at
        # set to the same state again — timestamp should NOT advance
        s.state = "READY"
        self.assertEqual(s.last_state_change_at, ts)

    def test_compute_idle_ms_returns_none_when_no_activity(self) -> None:
        s = _make_session("READY")
        self.assertIsNone(s.compute_idle_ms())

    def test_compute_idle_ms_uses_max_of_rx_tx(self) -> None:
        s = _make_session("READY")
        now = time.monotonic()
        s.last_rx_mono = now - 0.5  # 500ms ago
        s.last_tx_mono = now - 0.1  # 100ms ago (more recent)
        idle = s.compute_idle_ms()
        self.assertIsNotNone(idle)
        # use the most recent of rx/tx (~100ms)
        self.assertLess(idle, 200)

    def test_classify_offline_when_not_ready(self) -> None:
        for state in ("DETACHED", "ATTACHING", "RECOVERING", "ERROR"):
            s = _make_session(state)
            self.assertEqual(s.classify_activity(), "offline", f"state={state}")

    def test_classify_newly_attached_when_no_rx_tx_yet(self) -> None:
        s = _make_session("READY")
        self.assertEqual(s.classify_activity(), "newly-attached")

    def test_classify_active_when_recent_activity(self) -> None:
        s = _make_session("READY")
        s.last_rx_mono = time.monotonic()  # now
        self.assertEqual(s.classify_activity(), "active")

    def test_classify_idle_healthy_after_5s_silence(self) -> None:
        s = _make_session("READY")
        s.last_rx_mono = time.monotonic() - 10.0  # 10s ago
        self.assertEqual(s.classify_activity(), "idle-healthy")

    def test_classify_quiet_suspicious_after_60s_silence(self) -> None:
        s = _make_session("READY")
        s.last_rx_mono = time.monotonic() - 70.0  # 70s ago
        self.assertEqual(s.classify_activity(), "quiet-suspicious")

    def test_classify_active_for_attached_state(self) -> None:
        s = _make_session("ATTACHED")
        s.last_tx_mono = time.monotonic()
        self.assertEqual(s.classify_activity(), "active")


class TestSessionPublicDict(unittest.TestCase):
    """to_public_dict 新欄位 expose 測試。"""

    def test_public_dict_contains_activity_fields(self) -> None:
        s = _make_session("READY")
        d = s.to_public_dict()
        for key in (
            "last_state_change_at",
            "last_rx_at",
            "last_tx_at",
            "last_probe_at",
            "idle_for_ms",
            "outstanding_commands",
            "activity_classification",
        ):
            self.assertIn(key, d, f"missing field {key}")

    def test_public_dict_state_unchanged_after_property_refactor(self) -> None:
        s = _make_session()
        s.state = "READY"
        d = s.to_public_dict()
        self.assertEqual(d["state"], "READY")

    def test_outstanding_commands_counts_background_and_foreground(self) -> None:
        s = _make_session("READY")
        # only foreground
        s.foreground_busy = True
        self.assertEqual(s.to_public_dict()["outstanding_commands"], 1)
        # add 2 background
        s.background_cmd_ids.extend(["cmd-1", "cmd-2"])
        self.assertEqual(s.to_public_dict()["outstanding_commands"], 3)
        # only background
        s.foreground_busy = False
        self.assertEqual(s.to_public_dict()["outstanding_commands"], 2)
        # none
        s.background_cmd_ids.clear()
        self.assertEqual(s.to_public_dict()["outstanding_commands"], 0)

    def test_idle_for_ms_in_public_dict(self) -> None:
        s = _make_session("READY")
        # no activity → None
        self.assertIsNone(s.to_public_dict()["idle_for_ms"])
        # with activity → small int
        s.last_rx_mono = time.monotonic()
        self.assertIsNotNone(s.to_public_dict()["idle_for_ms"])
        self.assertLess(s.to_public_dict()["idle_for_ms"], 100)


class TestSessionRxTxAgeFields(unittest.TestCase):
    """#150 — to_public_dict 的 RX/TX 單邊年齡與 last_error_detail 欄位。"""

    def test_public_dict_contains_age_and_detail_fields(self) -> None:
        s = _make_session("READY")
        d = s.to_public_dict()
        for key in ("last_rx_age_s", "last_tx_age_s", "last_error_detail"):
            self.assertIn(key, d, f"missing field {key}")

    def test_age_none_when_never_active(self) -> None:
        s = _make_session("READY")
        d = s.to_public_dict()
        self.assertIsNone(d["last_rx_age_s"])
        self.assertIsNone(d["last_tx_age_s"])

    def test_age_reflects_monotonic_distance(self) -> None:
        s = _make_session("READY")
        s.last_rx_mono = time.monotonic() - 40.0
        s.last_tx_mono = time.monotonic() - 1.0
        d = s.to_public_dict()
        self.assertAlmostEqual(d["last_rx_age_s"], 40.0, delta=1.0)
        self.assertAlmostEqual(d["last_tx_age_s"], 1.0, delta=1.0)

    def test_last_error_setter_clears_detail_on_change(self) -> None:
        """last_error 值變更 → detail 自動清空（stale detail 根除線）。"""
        s = _make_session()
        s.last_error = "TRANSPORT_STALL"
        s.last_error_detail = "hint"
        s.last_error = "PROMPT_UNAVAILABLE"
        self.assertIsNone(s.last_error_detail)

    def test_last_error_same_value_keeps_detail(self) -> None:
        """同值重設不清 detail（逐輪 reprobe 同碼時 detail 保留）。"""
        s = _make_session()
        s.last_error = "TRANSPORT_STALL"
        s.last_error_detail = "hint"
        s.last_error = "TRANSPORT_STALL"
        self.assertEqual(s.last_error_detail, "hint")


class TestSessionManagerActivityHooks(unittest.TestCase):
    """SessionManager 內 RX / TX 標記助手在實際路徑被呼叫的整合測試。"""

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)  # #120 per-file 隔離（unittest 不載 conftest）

    def _make_manager(self) -> SessionManager:
        wal = mock.MagicMock()
        wal.current_seq = 0
        mgr = SessionManager(
            profiles=[_make_profile()],
            wal=wal,
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        return mgr

    def test_mark_session_rx_updates_timestamps(self) -> None:
        mgr = self._make_manager()
        s = _make_session("READY")
        mgr._mark_session_rx(s)
        self.assertIsNotNone(s.last_rx_at)
        self.assertGreater(s.last_rx_mono, 0.0)

    def test_mark_session_tx_updates_timestamps(self) -> None:
        mgr = self._make_manager()
        s = _make_session("READY")
        mgr._mark_session_tx(s)
        self.assertIsNotNone(s.last_tx_at)
        self.assertGreater(s.last_tx_mono, 0.0)

    def test_on_bridge_rx_updates_session_rx_activity(self) -> None:
        mgr = self._make_manager()
        # inject a session into the manager
        s = SessionRuntime(session_id="sid-rx", profile=_make_profile())
        s.state = "READY"
        mgr._sessions["sid-rx"] = s
        mgr._on_bridge_rx("sid-rx", b"hello\n")
        self.assertIsNotNone(s.last_rx_at)
        self.assertGreater(s.last_rx_mono, 0.0)

    def test_on_bridge_rx_marks_rx_even_when_foreground_busy(self) -> None:
        """RX activity tracking should NOT be gated by foreground_busy —
        the data is still being received from the UART."""
        mgr = self._make_manager()
        s = SessionRuntime(session_id="sid-busy", profile=_make_profile())
        s.state = "READY"
        s.foreground_busy = True
        mgr._sessions["sid-busy"] = s
        mgr._on_bridge_rx("sid-busy", b"chunk during fg\n")
        self.assertIsNotNone(s.last_rx_at)


if __name__ == "__main__":
    unittest.main()
