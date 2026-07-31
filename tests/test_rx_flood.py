"""#153 — RX 洪水分類（RX_FLOOD）單元測試。

涵蓋：login_fsm 的洪水反分類 choke point（probe_ready/ensure_ready 出口）、
reprobe 資格（_is_reprobe_prompt_error／_prepare_reprobe_locked）、self-test 的
RX_FLOOD 分類、to_public_dict 的 RX 指標露出（getattr 防禦回歸線）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from sw_core import constants
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.login_fsm import ensure_ready, probe_ready
from sw_core.session_manager import SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter

OVER = constants.RX_FLOOD_BYTES_PER_10S  # 剛好達閾＝超閾（>=）
UNDER = constants.RX_FLOOD_BYTES_PER_10S - 1


class FloodProbeBridge:
    """login_fsm 用 fake bridge：可控 wait 結果、rx_tail 快照與 rx_stats 讀數。"""

    def __init__(
        self,
        *,
        rx_bytes: int = 0,
        tail: str = "",
        wait_results: list[bool] | None = None,
    ) -> None:
        self.rx_bytes = rx_bytes
        self.tail = tail
        self._wait_results = list(wait_results or [])
        self.sent: list[str] = []

    def clear_rx_buffer(self) -> None:
        pass

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        self.sent.append(cmd)

    def send_secret(self, secret: str) -> None:
        self.sent.append("<secret>")

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        if self._wait_results:
            return self._wait_results.pop(0)
        return False

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self.tail

    def rx_stats(self) -> dict[str, Any]:
        return {
            "rx_bytes_last_10s": self.rx_bytes,
            "rx_rate_bps": self.rx_bytes // 10,
        }


def _make_profile(**overrides: Any) -> SessionProfile:
    defaults: dict[str, Any] = {
        "profile_name": "p",
        "com": "COM0",
        "act_no": 1,
        "alias": "lab+1",
        "device_by_id": "/dev/serial/by-id/fake",
        "platform": "prpl",
        "prompt_regex": r"root@prplOS.*# ",
        "login_regex": "(?!)",  # 永不匹配（空字串會 re.search 恆真、誤判 LOGIN_REQUIRED）
        "password_regex": r"(?mi)^password:\s*$",
        "ready_probe": "echo __READY__${nonce}",
        "timeout_s": 0.01,
        "uart": UartProfile(),
    }
    defaults.update(overrides)
    return SessionProfile(**defaults)


class TestFloodReclassify(unittest.TestCase):
    """login_fsm 兩個公開出口的 RX 洪水反分類。"""

    def test_probe_fail_over_threshold_returns_rx_flood(self) -> None:
        bridge = FloodProbeBridge(rx_bytes=OVER, tail="boot noise " * 5)
        ok, err = probe_ready(bridge, _make_profile())
        self.assertFalse(ok)
        self.assertEqual(err, "RX_FLOOD")

    def test_probe_fail_under_threshold_keeps_prompt_unavailable(self) -> None:
        """未超閾 → 原碼不變（無行為變更防線）。"""
        bridge = FloodProbeBridge(rx_bytes=UNDER, tail="quiet")
        ok, err = probe_ready(bridge, _make_profile())
        self.assertFalse(ok)
        self.assertEqual(err, "PROMPT_UNAVAILABLE")

    def test_login_required_never_masked_by_flood(self) -> None:
        """login prompt 可見＝可行動，優先於 flood，不得被遮蔽。"""
        bridge = FloodProbeBridge(rx_bytes=OVER, tail="orangepi3 login: ")
        profile = _make_profile(login_regex=r"(?mi)^.*login:\s*$")
        ok, err = probe_ready(bridge, profile)
        self.assertFalse(ok)
        self.assertEqual(err, "LOGIN_REQUIRED")

    def test_ensure_ready_login_prompt_timeout_over_threshold(self) -> None:
        """有帳密路徑死在 login_regex 等待（LOGIN_PROMPT_TIMEOUT）→ 超閾反分類。"""
        bridge = FloodProbeBridge(rx_bytes=OVER)
        profile = _make_profile(
            login_regex=r"(?mi)^.*login:\s*$",
            user_env="SW_FLOOD_U",
        )
        with mock.patch.dict("os.environ", {"SW_FLOOD_U": "u"}, clear=False):
            ok, err = ensure_ready(bridge, profile)
        self.assertFalse(ok)
        self.assertEqual(err, "RX_FLOOD")

    def test_ensure_ready_prpl_prompt_timeout_over_threshold(self) -> None:
        """無帳密、login 不需要 → _prompt_timeout_error（PRPL_PROMPT_TIMEOUT）→ 反分類。"""
        bridge = FloodProbeBridge(rx_bytes=OVER)
        profile = _make_profile(login_regex=r"(?mi)^.*login:\s*$")
        ok, err = ensure_ready(bridge, profile)
        self.assertFalse(ok)
        self.assertEqual(err, "RX_FLOOD")

    def test_pass_env_missing_never_masked(self) -> None:
        """帳密解析類錯誤（PASS_ENV_MISSING）永不被遮蔽。"""
        bridge = FloodProbeBridge(
            rx_bytes=OVER,
            wait_results=[False, True, True],  # probe 失敗、login prompt、password prompt
        )
        profile = _make_profile(
            login_regex=r"(?mi)^.*login:\s*$",
            user_env="SW_FLOOD_U",
            pass_env="SW_FLOOD_P_ABSENT",
        )
        with mock.patch.dict("os.environ", {"SW_FLOOD_U": "u"}, clear=False):
            ok, err = ensure_ready(bridge, profile)
        self.assertFalse(ok)
        self.assertEqual(err, "PASS_ENV_MISSING")

    def test_bridge_without_rx_stats_passes_through(self) -> None:
        """不支援 rx_stats 的 bridge（舊 fake）一律原樣直通，不炸。"""
        bridge = FloodProbeBridge(rx_bytes=OVER, tail="")
        del FloodProbeBridge.rx_stats  # type: ignore[misc]
        try:
            ok, err = probe_ready(bridge, _make_profile())
        finally:
            FloodProbeBridge.rx_stats = (  # type: ignore[method-assign]
                lambda self: {
                    "rx_bytes_last_10s": self.rx_bytes,
                    "rx_rate_bps": self.rx_bytes // 10,
                }
            )
        self.assertFalse(ok)
        self.assertEqual(err, "PROMPT_UNAVAILABLE")


class ManagerFakeBridge:
    """SessionManager 用 fake bridge（沿 test_readiness_reprobe 手法＋RX 指標）。"""

    def __init__(self, *, rx_bytes: int = 0, tail: str = "") -> None:
        self.rx_bytes = rx_bytes
        self.tail = tail
        self.device_path = "/dev/ttyFAKE0"
        self.interactive_owner: str | None = None
        self.sent: list[str] = []

    def list_consoles(self) -> list[dict]:
        return []

    def console_endpoint(self) -> str | None:
        return None

    def snapshot(self) -> dict:
        return {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "interactive_owner": self.interactive_owner,
            "last_human_input_at": None,
            "vtty": "/tmp/fake-vtty",
        }

    def console_has_external_peer(self, _client_id: str) -> bool:
        return True

    def set_interactive_owner(self, owner: str | None) -> None:
        self.interactive_owner = owner

    def _enumerate_all_held_paths(self):
        return None

    def reap_stale_consoles(self, *, held_slave_paths=None):
        return []

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self.tail

    def rx_stats(self) -> dict[str, Any]:
        return {
            "rx_bytes_last_10s": self.rx_bytes,
            "rx_rate_bps": self.rx_bytes // 10,
        }

    def rx_total_bytes(self) -> int:
        return 0

    def rx_snapshot_len(self) -> int:
        return 0

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        self.sent.append(cmd)

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        return False  # nonce probe 恆失敗（洪水淹沒）

    def suspend_interactive(self) -> None:
        pass

    def resume_interactive(self) -> None:
        pass


class _ManagerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(self._restore_state_path)

    def _restore_state_path(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, **profile_overrides: Any) -> tuple[SessionManager, SessionRuntime]:
        profile = _make_profile(**profile_overrides)
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {
                profile.device_by_id: DeviceInfo(
                    by_id=profile.device_by_id,
                    real_path="/dev/ttyFAKE0",
                )
            }
        return mgr, session


class TestReprobeAcceptsRxFlood(_ManagerTestBase):
    """D 項回歸線：RX_FLOOD 不得喪失自動重探資格（否則卡死 ATTACHED，比原病更糟）。"""

    def test_is_reprobe_prompt_error_accepts_rx_flood(self) -> None:
        mgr, _ = self._make_manager()
        self.assertTrue(mgr._is_reprobe_prompt_error("ATTACHED", "RX_FLOOD"))
        self.assertTrue(mgr._is_reprobe_prompt_error("DETACHED", "RX_FLOOD"))
        self.assertFalse(mgr._is_reprobe_prompt_error("READY", "RX_FLOOD"))

    def test_prepare_reprobe_returns_probe_job_after_drain(self) -> None:
        """ATTACHED＋RX_FLOOD＋RX 已閒置 → 排 probe job（排空後自動接手）。"""
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        session.last_error = "RX_FLOOD"
        session.bridge = ManagerFakeBridge()
        session.last_rx_mono = 100.0 - constants.REPROBE_RX_IDLE_S - 0.1
        with mgr._lock:
            job = mgr._prepare_reprobe_locked(session, 100.0)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job[0], "probe")

    def test_prepare_reprobe_backs_off_while_flooding(self) -> None:
        """洪水中（last_rx_mono 新鮮）→ _rx_idle_enough 天然退避、不排 job。"""
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        session.last_error = "RX_FLOOD"
        session.bridge = ManagerFakeBridge()
        session.last_rx_mono = 100.0 - (constants.REPROBE_RX_IDLE_S / 2)
        with mgr._lock:
            job = mgr._prepare_reprobe_locked(session, 100.0)
        self.assertIsNone(job)


class TestSelfTestRxFlood(_ManagerTestBase):
    """E 項：self-test 於洪水中回 RX_FLOOD＋wait，而非誤導性分類。"""

    def _attach(self, mgr: SessionManager, session: SessionRuntime,
                bridge: ManagerFakeBridge, state: str) -> None:
        session.bridge = bridge
        session.attached_real_path = "/dev/ttyFAKE0"
        session.state = state

    def test_attached_with_rx_flood_last_error(self) -> None:
        mgr, session = self._make_manager()
        bridge = ManagerFakeBridge(rx_bytes=UNDER)  # 指標已退、但 last_error 仍標洪水
        self._attach(mgr, session, bridge, "ATTACHED")
        session.last_error = "RX_FLOOD"
        result = mgr.self_test("COM0")
        self.assertEqual(result["classification"], "RX_FLOOD")
        self.assertEqual(result["recommended_action"], "wait")
        self.assertIn("rx_bytes_last_10s", result)

    def test_attached_with_live_flood_metric(self) -> None:
        """last_error 尚未標洪水、但當下指標超閾 → 亦判 RX_FLOOD。"""
        mgr, session = self._make_manager()
        bridge = ManagerFakeBridge(rx_bytes=OVER)
        self._attach(mgr, session, bridge, "ATTACHED")
        session.last_error = "PROMPT_UNAVAILABLE"
        result = mgr.self_test("COM0")
        self.assertEqual(result["classification"], "RX_FLOOD")
        self.assertEqual(result["rx_bytes_last_10s"], OVER)

    def test_ready_nonce_timeout_over_threshold_is_rx_flood(self) -> None:
        mgr, session = self._make_manager()
        bridge = ManagerFakeBridge(rx_bytes=OVER)
        self._attach(mgr, session, bridge, "READY")
        result = mgr.self_test("COM0", timeout_s=0.01)
        self.assertEqual(result["classification"], "RX_FLOOD")
        self.assertEqual(result["recommended_action"], "wait")
        self.assertFalse(result["probe_ok"])
        self.assertEqual(result["rx_bytes_last_10s"], OVER)

    def test_ready_nonce_timeout_under_threshold_stays_unresponsive(self) -> None:
        mgr, session = self._make_manager()
        bridge = ManagerFakeBridge(rx_bytes=UNDER)
        self._attach(mgr, session, bridge, "READY")
        result = mgr.self_test("COM0", timeout_s=0.01)
        self.assertEqual(result["classification"], "TARGET_UNRESPONSIVE")
        self.assertEqual(result["recommended_action"], "recover")

    def test_bootloader_priority_over_flood(self) -> None:
        """bootloader tail 匹配時即使超閾仍判 BOOTLOADER（優先序不變）。"""
        mgr, session = self._make_manager(bootloader_prompts=("=> ",))
        bridge = ManagerFakeBridge(rx_bytes=OVER, tail="\r\n=> ")
        self._attach(mgr, session, bridge, "ATTACHED")
        session.last_error = "PROMPT_UNAVAILABLE"
        result = mgr.self_test("COM0")
        self.assertEqual(result["classification"], "BOOTLOADER")


class TestPublicDictRxMetrics(unittest.TestCase):
    """F 項：to_public_dict 的 RX 指標露出與 getattr 防禦回歸線。"""

    def test_no_bridge_yields_none(self) -> None:
        session = SessionRuntime(session_id="sid", profile=_make_profile())
        d = session.to_public_dict()
        self.assertIn("rx_bytes_last_10s", d)
        self.assertIsNone(d["rx_bytes_last_10s"])
        self.assertIsNone(d["rx_rate_bps"])

    def test_bridge_with_rx_stats_exposes_values(self) -> None:
        session = SessionRuntime(session_id="sid", profile=_make_profile())
        session.bridge = ManagerFakeBridge(rx_bytes=1230)  # type: ignore[assignment]
        d = session.to_public_dict()
        self.assertEqual(d["rx_bytes_last_10s"], 1230)
        self.assertEqual(d["rx_rate_bps"], 123)

    def test_bridge_without_rx_stats_does_not_crash(self) -> None:
        """舊 fake bridge（無 rx_stats）→ None、不拋例外（getattr 防禦）。"""

        class LegacyBridge:
            def list_consoles(self) -> list:
                return []

            def console_endpoint(self) -> None:
                return None

        session = SessionRuntime(session_id="sid", profile=_make_profile())
        session.bridge = LegacyBridge()  # type: ignore[assignment]
        d = session.to_public_dict()
        self.assertIsNone(d["rx_bytes_last_10s"])
        self.assertIsNone(d["rx_rate_bps"])


if __name__ == "__main__":
    unittest.main()
