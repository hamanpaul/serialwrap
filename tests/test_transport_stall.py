"""#150 — transport stall（TRANSPORT_STALL）分類與觀測面單元測試。

涵蓋：transport_stall 純函式矩陣、UARTBridge raw RX 計數器、SessionManager
_refine_probe_failure 整合（翻轉、去重告警、重臂）、reprobe 資格。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from sw_core import constants, transport_stall
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter

AGE_OVER = constants.TRANSPORT_STALL_MIN_RX_AGE_S + 10.0
AGE_UNDER = constants.TRANSPORT_STALL_MIN_RX_AGE_S / 3


class TestClassifyProbeFailure(unittest.TestCase):
    """純分類函式矩陣。"""

    def _classify(self, err: str, *, rx_delta: int = 0,
                  last_rx_mono: float = 100.0, now: float | None = None):
        if now is None:
            now = 100.0 + AGE_OVER
        return transport_stall.classify_probe_failure(
            err, rx_delta=rx_delta, last_rx_mono=last_rx_mono, now=now)

    def test_prompt_unavailable_stall_flips(self) -> None:
        self.assertEqual(self._classify("PROMPT_UNAVAILABLE"), ("TRANSPORT_STALL", True))

    def test_positive_delta_keeps_original(self) -> None:
        """delta>0（#153 flood 特徵／有 echo）→ 原碼不變。"""
        self.assertEqual(
            self._classify("PROMPT_UNAVAILABLE", rx_delta=42),
            ("PROMPT_UNAVAILABLE", False))

    def test_never_had_rx_keeps_original(self) -> None:
        """last_rx_mono=0（從未活過的死線/空埠）→ 維持原語意。"""
        self.assertEqual(
            self._classify("PROMPT_UNAVAILABLE", last_rx_mono=0.0, now=1000.0),
            ("PROMPT_UNAVAILABLE", False))

    def test_age_under_threshold_keeps_original(self) -> None:
        self.assertEqual(
            self._classify("PROMPT_UNAVAILABLE", now=100.0 + AGE_UNDER),
            ("PROMPT_UNAVAILABLE", False))

    def test_prompt_timeout_family_flips(self) -> None:
        for err in ("LOGIN_PROMPT_TIMEOUT", "BCM_PROMPT_TIMEOUT",
                    "SHELL_PROMPT_TIMEOUT", "PRPL_PROMPT_TIMEOUT",
                    "READY_PROMPT_TIMEOUT"):
            with self.subTest(err):
                self.assertEqual(self._classify(err), ("TRANSPORT_STALL", True))

    def test_credentials_unresolved_never_flips(self) -> None:
        self.assertEqual(
            self._classify("CREDENTIALS_UNRESOLVED"),
            ("CREDENTIALS_UNRESOLVED", False))

    def test_rx_flood_never_flips(self) -> None:
        """#153 的 RX_FLOOD 不屬可精煉集合（兩案特徵互斥）。"""
        self.assertEqual(self._classify("RX_FLOOD"), ("RX_FLOOD", False))


class TestResolveUsbBusid(unittest.TestCase):
    def test_vhci_path_resolves_busid(self) -> None:
        target = "/sys/devices/platform/vhci_hcd.0/usb1/1-1/1-1:1.0/ttyUSB0"
        with mock.patch.object(os.path, "realpath", return_value=target):
            self.assertEqual(transport_stall.resolve_usb_busid("/dev/ttyUSB0"), "1-1")

    def test_nested_hub_busid(self) -> None:
        target = "/sys/devices/pci0000:00/usb3/3-2/3-2.1/3-2.1:1.0/ttyUSB1"
        with mock.patch.object(os.path, "realpath", return_value=target):
            self.assertEqual(transport_stall.resolve_usb_busid("/dev/ttyUSB1"), "3-2.1")

    def test_non_usb_path_returns_none(self) -> None:
        target = "/sys/devices/platform/serial8250/tty/ttyS0"
        with mock.patch.object(os.path, "realpath", return_value=target):
            self.assertIsNone(transport_stall.resolve_usb_busid("/dev/ttyS0"))

    def test_realpath_exception_returns_none(self) -> None:
        with mock.patch.object(os.path, "realpath", side_effect=OSError("boom")):
            self.assertIsNone(transport_stall.resolve_usb_busid("/dev/ttyUSB0"))

    def test_none_real_path_returns_none(self) -> None:
        self.assertIsNone(transport_stall.resolve_usb_busid(None))


class TestTransportStallHint(unittest.TestCase):
    def test_hint_mentions_urb_stopped(self) -> None:
        hint = transport_stall.transport_stall_hint(None, 45.0)
        self.assertIn("urb stopped", hint)
        self.assertIn("dmesg", hint)

    def test_hint_with_busid_has_authorized_toggle(self) -> None:
        with mock.patch.object(transport_stall, "resolve_usb_busid", return_value="1-1"):
            hint = transport_stall.transport_stall_hint("/dev/ttyUSB0", 45.0)
        self.assertIn("/sys/bus/usb/devices/1-1/authorized", hint)

    def test_hint_without_busid_is_generic(self) -> None:
        with mock.patch.object(transport_stall, "resolve_usb_busid", return_value=None):
            hint = transport_stall.transport_stall_hint("/dev/ttyUSB0", 45.0)
        self.assertNotIn("/sys/bus/usb/devices/None", hint)
        self.assertIn("authorized", hint)


class TestBridgeRawRxCounter(unittest.TestCase):
    """raw RX 計數器（uart_io）：raw bytes（含 ANSI）、clear 不歸零、snapshot 露出。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        wal = WalWriter(wal_dir=self._tmpdir.name)
        self._bridge = UARTBridge(
            com="COM0", device_path="/dev/null", profile=UartProfile(), wal=wal)
        self.addCleanup(self._bridge.stop)

    def test_counts_raw_bytes_and_survives_clear(self) -> None:
        self._bridge._handle_serial_rx(b"ab\x1b[0m")
        self.assertEqual(self._bridge.rx_total_bytes(), 6)
        self._bridge.clear_rx_buffer()
        self.assertEqual(self._bridge.rx_total_bytes(), 6)
        self.assertIn("rx_total_bytes", self._bridge.snapshot())


class StallFakeBridge:
    """SessionManager 整合用 fake：rx_total 可控（delta 由測試決定）。"""

    def __init__(self) -> None:
        self.rx_total = 1000
        self.device_path = "/dev/ttyFAKE0"
        self.interactive_owner: str | None = None

    def list_consoles(self) -> list[dict]:
        return []

    def console_endpoint(self) -> str | None:
        return None

    def snapshot(self) -> dict:
        return {
            "running": True, "serial_alive": True, "vtty_alive": True,
            "interactive_owner": self.interactive_owner,
            "last_human_input_at": None, "vtty": "/tmp/fake-vtty",
        }

    def console_has_external_peer(self, _client_id: str) -> bool:
        return True

    def set_interactive_owner(self, owner: str | None) -> None:
        self.interactive_owner = owner

    def _enumerate_all_held_paths(self):
        return None

    def reap_stale_consoles(self, *, held_slave_paths=None):
        return []

    def rx_total_bytes(self) -> int:
        return self.rx_total


def _make_profile(**overrides: Any) -> SessionProfile:
    defaults: dict[str, Any] = {
        "profile_name": "p",
        "com": "COM0",
        "act_no": 1,
        "alias": "lab+1",
        "device_by_id": "/dev/serial/by-id/fake",
        "platform": "prpl",
        "prompt_regex": r"root@prplOS.*# ",
        "login_regex": "",
        "ready_probe": "echo __READY__${nonce}",
        "timeout_s": 0.01,
        "uart": UartProfile(),
    }
    defaults.update(overrides)
    return SessionProfile(**defaults)


class TestRefineProbeFailureIntegration(unittest.TestCase):
    """_probe_existing_bridge 接線：翻轉、detail、一次性 WAL 告警與重臂。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self) -> tuple[SessionManager, SessionRuntime, StallFakeBridge]:
        profile = _make_profile()
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
                    by_id=profile.device_by_id, real_path="/dev/ttyFAKE0")
            }
        bridge = StallFakeBridge()
        session.bridge = bridge  # type: ignore[assignment]
        session.attached_real_path = "/dev/ttyFAKE0"
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"
        session.last_rx_mono = time.monotonic() - AGE_OVER
        return mgr, session, bridge

    def _count_stall_meta(self) -> int:
        count = 0
        for path in Path(self._tmp.name).glob("raw.wal*.ndjson"):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("meta") or {}).get("event") == "transport_stall_suspected":
                    count += 1
        return count

    def test_zero_delta_probe_failure_flips_and_warns_once(self) -> None:
        mgr, session, bridge = self._make_manager()

        with mock.patch.object(sm_mod, "probe_ready",
                               return_value=(False, "PROMPT_UNAVAILABLE")):
            mgr._probe_existing_bridge(session, bridge)  # type: ignore[arg-type]

        self.assertEqual(session.last_error, "TRANSPORT_STALL")
        self.assertIsNotNone(session.last_error_detail)
        assert session.last_error_detail is not None
        self.assertIn("urb stopped", session.last_error_detail)
        self.assertTrue(session.transport_stall_warned)
        self.assertEqual(self._count_stall_meta(), 1)

        # 第二輪 reprobe：warned 去重 → 不重複告警。
        with mock.patch.object(sm_mod, "probe_ready",
                               return_value=(False, "PROMPT_UNAVAILABLE")):
            mgr._probe_existing_bridge(session, bridge)  # type: ignore[arg-type]
        self.assertEqual(self._count_stall_meta(), 1)
        self.assertEqual(session.last_error, "TRANSPORT_STALL")

    def test_positive_delta_keeps_prompt_unavailable(self) -> None:
        mgr, session, bridge = self._make_manager()

        def probe_with_rx(_bridge: object, _sp: object) -> tuple[bool, str]:
            bridge.rx_total += 128  # probe 期間有 RX（echo/雜訊）
            return False, "PROMPT_UNAVAILABLE"

        with mock.patch.object(sm_mod, "probe_ready", side_effect=probe_with_rx):
            mgr._probe_existing_bridge(session, bridge)  # type: ignore[arg-type]

        self.assertEqual(session.last_error, "PROMPT_UNAVAILABLE")
        self.assertIsNone(session.last_error_detail)
        self.assertEqual(self._count_stall_meta(), 0)

    def test_mark_session_rx_rearms_warning(self) -> None:
        mgr, session, _ = self._make_manager()
        session.transport_stall_warned = True
        mgr._mark_session_rx(session)
        self.assertFalse(session.transport_stall_warned)

    def test_reprobe_eligibility_keeps_transport_stall(self) -> None:
        mgr, _, _ = self._make_manager()
        self.assertTrue(mgr._is_reprobe_prompt_error("ATTACHED", "TRANSPORT_STALL"))
        self.assertTrue(mgr._is_reprobe_prompt_error("DETACHED", "TRANSPORT_STALL"))

    def test_success_clears_stall_detail(self) -> None:
        """probe 成功 → last_error=None，setter 連動清 detail（不殘留 stale hint）。"""
        mgr, session, bridge = self._make_manager()
        session.last_error = "TRANSPORT_STALL"
        session.last_error_detail = "stale hint"
        with mock.patch.object(sm_mod, "probe_ready", return_value=(True, None)):
            mgr._probe_existing_bridge(session, bridge)  # type: ignore[arg-type]
        self.assertEqual(session.state, "READY")
        self.assertIsNone(session.last_error)
        self.assertIsNone(session.last_error_detail)


if __name__ == "__main__":
    unittest.main()
