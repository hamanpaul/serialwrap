"""explicit `targets` device_by_id 排他性回歸測試（#186）。

Bench 實測（TI XDS110 探棒，兩個 CDC-ACM port）：`00-com2-prpl.yaml` 把 COM2 綁到一顆
**未插著**的 CH340 by-id；另一份 profile 檔把 COM3/COM4 explicit 綁到 XDS110 的
`-if00`/`-if03` by-id，兩者都確實在線。`systemctl restart serialwrap` 後：

    COM2 com2-prpl        prpl-template        ATTACHED  /dev/ttyACM0   <- 佔用了沒綁給它的裝置
    COM3 cc2745-console   xds110-passthrough   DETACHED  None           <- 自己的裝置被搶走
    COM4 cc2745-aux       xds110-passthrough   DETACHED  None

把 XDS110 的 profile 檔改名成排序上先於 `00-com2-prpl.yaml` 載入後，結果整個反過來
（COM2 DETACHED、COM3/COM4 正確 ATTACHED）——即目前的裝置指派是
**first-come-by-load-order**：`self._sessions` 的 insertion order（由 profile 檔案
載入順序決定）在裝置 by-id 出現「一個以上 session 同時宣稱擁有」時，替 `next()` 的
exact-match 做了不該存在的 tiebreak。

根因在 `SessionManager.__init__`：`_binding_overrides`（`session.bind` 或舊版
`_attach_by_id` DETACHED-rebind fallback 留下的持久化紀錄，存在 `state.json`）套用
時完全沒檢查是否與「另一個 explicit target 自己在 YAML 宣告」的 device_by_id 衝突。
在這支探棒的真實時間線上：COM3/COM4 的 explicit target 檔案是**後來才加進來**的；
在那之前，`_attach_by_id` 的 DETACHED-rebind fallback（#100，這條路徑本身是刻意
設計給「placeholder 裝置自動綁定」用的，見 `test_session_bind.py::
test_auto_bind_on_device_attach`，非本次修復對象）已經把當時「還沒有任何 target
認領」的 XDS110 -if00 借給了 COM2、寫進 `state.json` 的 `bindings`。等 COM3 的
explicit target 加入、daemon 重啟後，COM2 的持久化 override 與 COM3 自己宣告的
device_by_id 撞成同一個值，`__init__` 沒有偵測這個衝突，兩個 session 都以
`device_by_id == if00` 存在於 `self._sessions`，`_attach_by_id` 的 exact-match
`next()` 就只能靠 insertion order 亂猜贏家——這正是「改檔名讀取順序」會翻轉結果的
原因。
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter

# 對應 bench 實測的兩個 by-id（CH340 未插 / XDS110 -if00 已插）。
ABSENT_BY_ID = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
PRESENT_BY_ID = (
    "/dev/serial/by-id/usb-Texas_Instruments_XDS110__03.00.00.43__"
    "Embed_with_CMSIS-DAP_LT4704QJ-if00"
)


class TestExplicitTargetDeviceOwnership(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = str(Path(self._tmp.name) / "state.json")
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = self._state_path
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

    def _make_profile(self, name: str, com: str, alias: str, by_id: str, act_no: int) -> SessionProfile:
        return SessionProfile(
            profile_name=name,
            com=com,
            act_no=act_no,
            alias=alias,
            device_by_id=by_id,
            platform="passthrough",
            login_regex="",
            ready_probe="",
            uart=UartProfile(),
        )

    def _mgr(self, profiles: list[SessionProfile]) -> SessionManager:
        return SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
            state_path=self._state_path,
        )

    def test_restart_does_not_let_stale_override_steal_another_targets_device(self) -> None:
        """重現 bench 上「改檔名載入順序」才會翻轉結果的那個 restart 情境，並驗證
        修復後結果與 profile 檔案（`self._sessions` insertion order）順序無關：
        COM3（真正宣告 XDS110 -if00 的 target）必須拿到它，COM2 必須退回自己
        設定的（缺席的）CH340、維持 DETACHED。
        """
        target_absent = self._make_profile("com2-prpl", "COM2", "com2-prpl+3", ABSENT_BY_ID, act_no=3)
        target_present = self._make_profile(
            "cc2745-console", "COM3", "cc2745-console+4", PRESENT_BY_ID, act_no=4
        )

        # Step 1：只有 COM2 存在時，XDS110 -if00 上線但尚無任何 target 宣告它——
        # `_attach_by_id` 的 DETACHED-rebind fallback（#100，非本次修復對象）把它
        # 借給 COM2 並持久化。直接重建這個持久化結果（等價於該 fallback 真的跑過
        # 一次），聚焦驗證下面 Step 2 的 restart 行為。
        mgr1 = self._mgr([target_absent])
        with mgr1._lock:
            com2_v1 = mgr1.get_session("COM2")
            assert com2_v1 is not None
            com2_v1.profile = dataclasses.replace(com2_v1.profile, device_by_id=PRESENT_BY_ID)
            mgr1._binding_overrides[com2_v1.session_id] = PRESENT_BY_ID
            mgr1._save_state()

        # Step 2：加入 COM3 的 explicit target（對應「第二份 profile 檔案後來才
        # 加進來」），以同一份 state.json 模擬 `systemctl restart serialwrap`。
        mgr2 = self._mgr([target_absent, target_present])
        with mgr2._lock:
            mgr2._devices = {PRESENT_BY_ID: DeviceInfo(by_id=PRESENT_BY_ID, real_path="/dev/ttyACM0")}
        with mock.patch("sw_core.session_manager.UARTBridge") as MockBridge:
            MockBridge.return_value.start.return_value = None
            mgr2._attach_by_id(PRESENT_BY_ID)

        com2 = mgr2.get_session("COM2")
        com3 = mgr2.get_session("COM3")
        assert com2 is not None and com3 is not None
        self.assertEqual(
            com3.profile.device_by_id, PRESENT_BY_ID,
            "COM3 自己宣告的裝置必須歸自己",
        )
        self.assertEqual(com3.state, "ATTACHED")
        self.assertEqual(
            com2.profile.device_by_id, ABSENT_BY_ID,
            "COM2 必須退回自己設定的裝置，不得繼續頂著過期 override 佔用 COM3 的裝置",
        )
        self.assertEqual(com2.state, "DETACHED")

    def test_restart_outcome_is_independent_of_profile_file_load_order(self) -> None:
        """同一組 target，profiles 傳入順序（對應 profile 檔案載入順序）互換後，
        結果必須一致——修復前這正是 bench 上「改檔名」才能繞過問題的根因
        （`self._sessions` insertion order 決定 exact-match 的 tiebreak）。
        """
        target_absent = self._make_profile("com2-prpl", "COM2", "com2-prpl+3", ABSENT_BY_ID, act_no=3)
        target_present = self._make_profile(
            "cc2745-console", "COM3", "cc2745-console+4", PRESENT_BY_ID, act_no=4
        )

        mgr1 = self._mgr([target_absent])
        with mgr1._lock:
            com2_v1 = mgr1.get_session("COM2")
            assert com2_v1 is not None
            com2_v1.profile = dataclasses.replace(com2_v1.profile, device_by_id=PRESENT_BY_ID)
            mgr1._binding_overrides[com2_v1.session_id] = PRESENT_BY_ID
            mgr1._save_state()

        # 順序反過來：COM3（present target）先於 COM2 出現在 profiles 列表。
        mgr2 = self._mgr([target_present, target_absent])
        with mgr2._lock:
            mgr2._devices = {PRESENT_BY_ID: DeviceInfo(by_id=PRESENT_BY_ID, real_path="/dev/ttyACM0")}
        with mock.patch("sw_core.session_manager.UARTBridge") as MockBridge:
            MockBridge.return_value.start.return_value = None
            mgr2._attach_by_id(PRESENT_BY_ID)

        com2 = mgr2.get_session("COM2")
        com3 = mgr2.get_session("COM3")
        assert com2 is not None and com3 is not None
        self.assertEqual(com3.profile.device_by_id, PRESENT_BY_ID)
        self.assertEqual(com3.state, "ATTACHED")
        self.assertEqual(com2.profile.device_by_id, ABSENT_BY_ID)
        self.assertEqual(com2.state, "DETACHED")


if __name__ == "__main__":
    unittest.main()
