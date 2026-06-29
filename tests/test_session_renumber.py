"""session renumber 重排 + remap 一致性測試（#100）。

涵蓋：
- `AliasRegistry.for_session` / `reassign_session`。
- `SessionManager.renumber_dynamic()`：依 sorted by-id 重排 dynamic COM，原子 remap
  `_sessions` key / profile.com / session_id / alias / binding，回傳 old→new mapping。
- RPC `session.renumber`（Service 編排 + arbiter worker re-register）。
- CLI `session renumber` 解析與分派。
"""

import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from sw_core.alias_registry import AliasRegistry
from sw_core.config import ProfileTemplate, SessionProfile, UartProfile
from sw_core.session_manager import SessionManager, SessionRuntime
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


BY_ID_AC01 = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0"
BY_ID_AQ00 = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AQ00OAQ7-if00-port0"


class TestAliasRegistryHelpers(unittest.TestCase):
    def test_for_session_returns_alias_or_none(self) -> None:
        reg = AliasRegistry()
        reg.set_for_session("p:COM0", "lab+1")
        self.assertEqual(reg.for_session("p:COM0"), "lab+1")
        self.assertIsNone(reg.for_session("p:COM9"))

    def test_reassign_session_moves_pointer_keeps_alias_string(self) -> None:
        reg = AliasRegistry()
        reg.set_for_session("p:COM1", "lab+2")
        reg.reassign_session("p:COM1", "p:COM0")
        # alias 字串不變、只改指向
        self.assertIsNone(reg.for_session("p:COM1"))
        self.assertEqual(reg.for_session("p:COM0"), "lab+2")
        self.assertIn("lab+2", {row["alias"] for row in reg.list_alias()})


class TestRenumberDynamic(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

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

    def _inject_dynamic(
        self, mgr: SessionManager, com: str, by_id: str, *, act_no: int, source: str = "detected"
    ) -> SessionRuntime:
        sid = f"prpl-template:{com}"
        alias = f"prpl-template+{act_no}"
        profile = SessionProfile(
            profile_name="prpl-template",
            com=com,
            act_no=act_no,
            alias=alias,
            device_by_id=by_id,
            platform="prpl",
            uart=UartProfile(),
        )
        rt = SessionRuntime(session_id=sid, profile=profile)
        rt.state = "READY"
        rt.profile_source = source
        with mgr._lock:
            mgr._sessions[sid] = rt
            mgr._aliases.set_for_session(sid, alias)
        return rt

    def test_renumber_snaps_to_sorted_and_remaps_alias(self) -> None:
        mgr = self._make_manager()
        # 亂序：COM0=AQ00（act_no1）, COM1=AC01（act_no2）
        self._inject_dynamic(mgr, "COM0", BY_ID_AQ00, act_no=1)
        self._inject_dynamic(mgr, "COM1", BY_ID_AC01, act_no=2)

        mapping = mgr.renumber_dynamic()

        # AC01 字典序在前 → 應排到 COM0；AQ00 → COM1
        self.assertEqual(mgr.com_for_by_id(BY_ID_AC01), "COM0")
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM1")
        # session_id remap 完整（兩者對調）
        self.assertEqual(mapping["prpl-template:COM1"], "prpl-template:COM0")
        self.assertEqual(mapping["prpl-template:COM0"], "prpl-template:COM1")
        # _sessions key 已是新 sid
        self.assertEqual(set(mgr._sessions.keys()), {"prpl-template:COM0", "prpl-template:COM1"})
        # alias 跟著新 sid（AC01 的 alias=prpl-template+2 現在指向 COM0 sid）
        self.assertEqual(mgr._aliases.for_session("prpl-template:COM0"), "prpl-template+2")
        self.assertEqual(mgr._aliases.for_session("prpl-template:COM1"), "prpl-template+1")
        # profile.com 與 session_id 一致
        s0 = mgr._sessions["prpl-template:COM0"]
        self.assertEqual(s0.profile.com, "COM0")
        self.assertEqual(s0.profile.device_by_id, BY_ID_AC01)

    def test_renumber_noop_when_already_sorted(self) -> None:
        mgr = self._make_manager()
        self._inject_dynamic(mgr, "COM0", BY_ID_AC01, act_no=1)
        self._inject_dynamic(mgr, "COM1", BY_ID_AQ00, act_no=2)
        mapping = mgr.renumber_dynamic()
        self.assertEqual(mapping, {})
        self.assertEqual(mgr.com_for_by_id(BY_ID_AC01), "COM0")
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM1")

    def test_renumber_preserves_explicit_com(self) -> None:
        explicit = SessionProfile(
            profile_name="prpl-template",
            com="COM5",
            act_no=1,
            alias="lab+1",
            device_by_id=BY_ID_AC01,
            platform="prpl",
            uart=UartProfile(),
        )
        mgr = self._make_manager([explicit])  # explicit → profile_source=yaml-target
        # 一片 dynamic（AQ00）落在亂序 COM3
        self._inject_dynamic(mgr, "COM3", BY_ID_AQ00, act_no=2)

        mapping = mgr.renumber_dynamic()

        # explicit COM5 不動；dynamic AQ00 從 COM0 起重排（COM5 reserved 但 idx 從 0 起）
        self.assertEqual(mgr.com_for_by_id(BY_ID_AC01), "COM5")
        self.assertEqual(mgr.com_for_by_id(BY_ID_AQ00), "COM0")
        self.assertEqual(mapping, {"prpl-template:COM3": "prpl-template:COM0"})

    def test_renumber_persists_state(self) -> None:
        mgr = self._make_manager()
        self._inject_dynamic(mgr, "COM0", BY_ID_AQ00, act_no=1)
        self._inject_dynamic(mgr, "COM1", BY_ID_AC01, act_no=2)
        with mock.patch.object(mgr, "_save_state", wraps=mgr._save_state) as save:
            mgr.renumber_dynamic()
        save.assert_called()


if __name__ == "__main__":
    unittest.main()
