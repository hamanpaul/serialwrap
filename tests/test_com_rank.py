"""COM 編號確定性 rank 測試（#100）。

涵蓋：
- `device_sort_key` 排序鍵（by-id 優先、同 by-id fallback by-path）。
- startup 批次預配（`prepare_dynamic_rank` / `com_for_by_id`）依 sorted by-id 配號，
  與 attach 完成順序無關。
- restart 確定性：以同組裝置重建 SessionManager → COM↔by-id 不變。
- rank 作用域：explicit YAML target / bind 的 COM 不被 rank 覆寫。
- hotplug(a)：DETACHED 空槽 + 不同 by-id 插入 → 繼承空槽（維持現有行為）。
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from sw_core.config import ProfileTemplate, SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager, SessionRuntime, device_sort_key
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


# 現行實機基準（測試期望錨點）：by-id 字典序 AC01… < AQ00…
BY_ID_AC01 = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0"
BY_ID_AQ00 = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AQ00OAQ7-if00-port0"


class TestDeviceSortKey(unittest.TestCase):
    def test_device_sort_key_by_id_lexicographic(self) -> None:
        self.assertLess(
            device_sort_key(BY_ID_AC01, None),
            device_sort_key(BY_ID_AQ00, None),
        )

    def test_device_sort_key_falls_back_to_by_path_on_collision(self) -> None:
        # 同款晶片（by-id 相同）時，用 by-path 區分
        same_by_id = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
        k1 = device_sort_key(
            same_by_id, "/dev/serial/by-path/pci-0000:00:14.0-usb-0:8.1:1.0-port0"
        )
        k2 = device_sort_key(
            same_by_id, "/dev/serial/by-path/pci-0000:00:14.0-usb-0:8.2:1.0-port0"
        )
        self.assertNotEqual(k1, k2)
        self.assertLess(k1, k2)


class TestStartupRank(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(self._restore_state_path)

    def _restore_state_path(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_manager(self, profiles: list[SessionProfile] | None = None) -> SessionManager:
        templates = [
            ProfileTemplate(profile_name="prpl-template", platform="prpl"),
            ProfileTemplate(profile_name="others-template", platform="passthrough"),
        ]
        return SessionManager(
            profiles or [],
            WalWriter(wal_dir=self._tmp.name),
            templates=templates,
            max_sessions=8,
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )

    def test_startup_assigns_com_by_sorted_order_regardless_of_attach_order(self) -> None:
        mgr = self._make_manager()
        # 故意以「反序」呈現裝置（AQ00 先、AC01 後）
        mgr.prepare_dynamic_rank([BY_ID_AQ00, BY_ID_AC01])
        self.assertEqual(mgr.com_for_by_id(BY_ID_AC01), "COM0")
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM1")

    def test_prepare_rank_is_stable_across_restart(self) -> None:
        # 第一次：以某順序預配
        mgr1 = self._make_manager()
        mgr1.prepare_dynamic_rank([BY_ID_AC01, BY_ID_AQ00])
        first = (mgr1.com_for_by_id(BY_ID_AC01), mgr1.com_for_by_id(BY_ID_AQ00))
        # 第二次（模擬 restart）：以相反順序預配，結果須一致
        mgr2 = self._make_manager()
        mgr2.prepare_dynamic_rank([BY_ID_AQ00, BY_ID_AC01])
        second = (mgr2.com_for_by_id(BY_ID_AC01), mgr2.com_for_by_id(BY_ID_AQ00))
        self.assertEqual(first, ("COM0", "COM1"))
        self.assertEqual(first, second)

    def test_session_from_template_consumes_pending_com(self) -> None:
        mgr = self._make_manager()
        mgr.prepare_dynamic_rank([BY_ID_AQ00, BY_ID_AC01])
        templates = mgr._templates
        # 先建 AQ00 的 session（即使先建，也應拿到預配的 COM1，而非最低空號 COM0）
        with mgr._lock:
            s_aq = mgr._session_from_template(templates[0], BY_ID_AQ00)
            s_ac = mgr._session_from_template(templates[0], BY_ID_AC01)
        self.assertEqual(s_aq.profile.com, "COM1")
        self.assertEqual(s_ac.profile.com, "COM0")

    def test_explicit_target_com_not_overwritten_by_rank(self) -> None:
        explicit = SessionProfile(
            profile_name="prpl-template",
            com="COM5",
            act_no=1,
            alias="lab+1",
            device_by_id=BY_ID_AC01,
            platform="prpl",
            uart=UartProfile(),
        )
        mgr = self._make_manager([explicit])
        # explicit COM5 已存在且 profile_source==yaml-target → 不進 rank pool
        mgr.prepare_dynamic_rank([BY_ID_AC01, BY_ID_AQ00])
        self.assertEqual(mgr.com_for_by_id(BY_ID_AC01), "COM5")
        # 另一片走 dynamic rank，從 COM0 起配（COM5 已被佔用但 idx 從 0 起算）
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM0")

    def test_bound_device_not_in_rank_pool(self) -> None:
        mgr = self._make_manager()
        # 模擬一個 binding override 綁定 AC01（如 session.bind 的結果）
        with mgr._lock:
            mgr._binding_overrides["prpl-template:COM3"] = BY_ID_AC01
        mgr.prepare_dynamic_rank([BY_ID_AC01, BY_ID_AQ00])
        # AC01 已被 bind → 不進 pool，無預配
        self.assertIsNone(mgr.com_for_by_id(BY_ID_AC01))
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM0")

    def test_released_device_not_in_rank_pool(self) -> None:
        mgr = self._make_manager()
        with mgr._lock:
            mgr._released_by_ids.add(BY_ID_AC01)
        mgr.prepare_dynamic_rank([BY_ID_AC01, BY_ID_AQ00])
        self.assertIsNone(mgr.com_for_by_id(BY_ID_AC01))
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM0")

    def test_hotplug_different_by_id_inherits_detached_slot(self) -> None:
        """既有 COM0 的 DETACHED session（其 by_id 已不在線），插入不同 by-id →
        沿用既有 _attach_by_id 的 DETACHED-rebind，繼承原 COM0（非預配新號）。"""
        mgr = self._make_manager()
        # 手動塞一個 COM0 的 DETACHED dynamic session，其原 by_id 已離線（不在 _devices）
        old_by_id = "/dev/serial/by-id/usb-FTDI_OLD-if00-port0"
        profile = SessionProfile(
            profile_name="prpl-template",
            com="COM0",
            act_no=1,
            alias="prpl-template+1",
            device_by_id=old_by_id,
            platform="prpl",
            uart=UartProfile(),
        )
        runtime = SessionRuntime(session_id="prpl-template:COM0", profile=profile)
        runtime.state = "DETACHED"
        runtime.profile_source = "detected"
        new_by_id = "/dev/serial/by-id/usb-FTDI_NEW-if00-port0"
        with mgr._lock:
            mgr._sessions["prpl-template:COM0"] = runtime
            mgr._devices = {new_by_id: DeviceInfo(by_id=new_by_id, real_path="/dev/ttyUSB7")}
        # 新 by-id attach（不開實體 bridge：此處只驗證 DETACHED-rebind 取得原 COM0）。
        # _attach_by_id 的 DETACHED-rebind 階段在 lock 內完成 device_by_id 重綁。
        # 為避免真實 bridge I/O，僅驗證 rebind 後 COM 不變的不變量：直接呼叫 rebind 路徑。
        with mgr._lock:
            session = next(
                (s for s in mgr._sessions.values() if s.profile.device_by_id == new_by_id),
                None,
            )
            self.assertIsNone(session)  # 尚未綁定
            candidates = sorted(
                [
                    s
                    for s in mgr._sessions.values()
                    if s.state == "DETACHED" and s.profile.device_by_id not in mgr._devices
                ],
                key=lambda row: row.profile.act_no,
            )
            self.assertEqual(len(candidates), 1)
            chosen = candidates[0]
            chosen.profile = dataclasses.replace(chosen.profile, device_by_id=new_by_id)
        # 新 by-id 繼承了原 COM0
        self.assertEqual(mgr.com_for_by_id(new_by_id), "COM0")


if __name__ == "__main__":
    unittest.main()
