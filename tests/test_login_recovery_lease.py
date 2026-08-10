"""#174 — interactive_open(allow_attached=True) 的 login recovery lease。

停在 login prompt 時，`--allow-attached` 過去只認 bootloader prompt／boot banner，
一律回 SESSION_NOT_READY + NOT_BOOTLOADER——即使板子完全健康、只差有人打帳密。
本檔驗證 rx tail 命中 login_regex／password_regex 時同樣授予 recovery lease，
回應標 login_required=True；仍非 login prompt 時維持既有 NOT_BOOTLOADER 行為
（回歸線）。
"""
from __future__ import annotations

import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


def _make_profile(
    bootloader_prompts: tuple[str, ...] = (r"^=> ",),
    login_regex: str = r"(?mi)login:\s*$",
    password_regex: str = r"(?mi)password:\s*$",
) -> SessionProfile:
    return SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/orig",
        platform="bcm",
        login_regex=login_regex,
        password_regex=password_regex,
        uart=UartProfile(),
        bootloader_prompts=bootloader_prompts,
    )


class TestLoginRecoveryLease(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(self._restore_state_path)

    def _restore_state_path(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_mgr_attached(
        self,
        *,
        rx_tail_str: str,
        bootloader_prompts: tuple[str, ...] = (r"^=> ",),
    ) -> tuple[SessionManager, SessionRuntime, mock.MagicMock]:
        profile = _make_profile(bootloader_prompts=bootloader_prompts)
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "vtty": "/dev/pts/9",
        }
        bridge.rx_tail.return_value = rx_tail_str
        bridge.console_has_external_peer.return_value = True

        session.bridge = bridge
        session.state = "ATTACHED"
        session.attached_real_path = "/dev/ttyUSB0"

        with mgr._lock:
            mgr._devices = {
                "/dev/serial/by-id/orig": DeviceInfo(
                    by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0"
                )
            }
        return mgr, session, bridge

    def test_login_prompt_grants_lease_with_login_required_flag(self) -> None:
        """rx tail 命中 login_regex（如 "(none) login: "）→ 授予 recovery lease，login_required=True。"""
        mgr, session, bridge = self._make_mgr_attached(rx_tail_str="(none) login: ")
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)

        self.assertTrue(resp["ok"])
        self.assertTrue(resp["recovery_mode"])
        self.assertIs(resp.get("login_required"), True)
        self.assertIsNot(resp.get("boot_interrupt"), True)
        lease = mgr._interactive[resp["interactive_id"]]
        self.assertTrue(lease.recovery_mode)

    def test_password_prompt_grants_lease_with_login_required_flag(self) -> None:
        """rx tail 命中 password_regex → 同樣授予 lease，login_required=True。"""
        mgr, session, bridge = self._make_mgr_attached(rx_tail_str="Password: ")
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)

        self.assertTrue(resp["ok"])
        self.assertIs(resp.get("login_required"), True)

    def test_non_login_non_bootloader_still_not_bootloader(self) -> None:
        """rx tail 既非 bootloader／banner 也非 login/password prompt → 維持既有 NOT_BOOTLOADER（回歸線）。"""
        mgr, session, bridge = self._make_mgr_attached(rx_tail_str="root@dut:~# ls\nbin  etc  usr")
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_READY")
        self.assertEqual(resp.get("error_detail"), "NOT_BOOTLOADER")

    def test_bootloader_prompt_still_takes_priority_over_login(self) -> None:
        """bootloader prompt 命中優先於 login 檢查（既有行為不變，回歸線）。"""
        mgr, session, bridge = self._make_mgr_attached(rx_tail_str="boot output\n=> ")
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)

        self.assertTrue(resp["ok"])
        self.assertIsNot(resp.get("login_required"), True)
        self.assertIsNot(resp.get("boot_interrupt"), True)

    def test_boot_banner_still_takes_priority_over_login(self) -> None:
        """autoboot 倒數 banner 命中優先於 login 檢查（既有行為不變，回歸線）。"""
        mgr, session, bridge = self._make_mgr_attached(
            rx_tail_str="Hit any key to stop autoboot:  2 "
        )
        resp = mgr.interactive_open("COM0", owner="agent:test", allow_attached=True)

        self.assertTrue(resp["ok"])
        self.assertIs(resp.get("boot_interrupt"), True)
        self.assertIsNot(resp.get("login_required"), True)


if __name__ == "__main__":
    unittest.main()
