import os
import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

    def _make_profile(self, name: str, com: str, alias: str, by_id: str) -> SessionProfile:
        return SessionProfile(
            profile_name=name, com=com, act_no=1, alias=alias,
            device_by_id=by_id, platform="prpl", uart=UartProfile(),
        )

    def _mgr(self, profiles):
        return SessionManager(
            profiles, WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _s: None, on_detached=lambda _s: None,
        )


import time
import unittest.mock as mock


class TestReleasedStateFields(_Base):
    def test_session_has_released_fields_and_set(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        self.assertIsNone(session.released_by)
        self.assertIsNone(session.released_at)
        self.assertIsNone(session.released_reason)
        self.assertEqual(mgr._released_by_ids, set())


class TestSpawnAttachGuard(_Base):
    def test_spawn_attach_skips_released_by_id(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        with mgr._lock:
            mgr._released_by_ids.add(by_id)
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            mgr._spawn_attach(by_id)
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertNotIn(by_id, mgr._attach_inflight)

    def test_spawn_attach_runs_for_non_released_by_id(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            mgr._spawn_attach(by_id)
            time.sleep(0.1)
        attach_by_id.assert_called_once_with(by_id)


class TestDropConsolesDetach(_Base):
    def test_detach_drop_consoles_closes_and_does_not_stash(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
        bridge.stop.assert_called_once_with(preserve_consoles=False)
        self.assertIsNone(session.retained_consoles)
        self.assertIsNone(session.bridge)

    def test_detach_default_preserves_consoles(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._detach_session_locked(session, reason="X")
        bridge.stop.assert_called_once_with(preserve_consoles=True)


class TestClearSessionReleasedGuard(_Base):
    def test_clear_on_released_session_is_noop(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.clear_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("released"))
        self.assertEqual(session.state, "RELEASED")
        spawn_attach.assert_not_called()


class TestReleaseDevice(_Base):
    def test_release_clean_slate_and_provenance(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        bridge.list_consoles.return_value = [{"client_id": "c1"}]
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}

        resp = mgr.release_device("COM0", source="agent:flash", reason="flash CC2674")

        self.assertTrue(resp["ok"])
        self.assertEqual(session.state, "RELEASED")
        self.assertEqual(session.released_by, "agent:flash")
        self.assertIsNotNone(session.released_at)
        self.assertEqual(session.released_reason, "flash CC2674")
        self.assertIn(by_id, mgr._released_by_ids)
        self.assertIsNone(session.retained_consoles)
        bridge.stop.assert_called_once_with(preserve_consoles=False)

    def test_release_idempotent(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            session.state = "RELEASED"
            mgr._released_by_ids.add(by_id)
        resp = mgr.release_device("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("already_released"))

    def test_release_session_not_found(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        resp = mgr.release_device("COM9")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_FOUND")


class TestProbeExternalHolder(_Base):
    def test_probe_detects_holder_in_fake_proc(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB9", proc / "1234" / "fd" / "5")
        res = mgr._probe_external_holder("/dev/ttyUSB9", _proc_root=str(proc))
        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_probe_no_holder(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB0", proc / "1234" / "fd" / "5")
        res = mgr._probe_external_holder("/dev/ttyUSB9", _proc_root=str(proc))
        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])


class TestAttachDevice(_Base):
    def _released_mgr(self, by_id="/dev/serial/by-id/orig", real="/dev/ttyUSB0"):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path=real)}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
            session.released_by = "agent:flash"
            session.released_at = "now"
        return mgr, session, by_id

    def test_attach_reclaims_when_free(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [], "holder": None}), \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0")
        self.assertTrue(resp["ok"])
        self.assertNotIn(by_id, mgr._released_by_ids)
        self.assertIsNone(session.released_by)
        self.assertEqual(session.state, "ATTACHING")
        spawn_attach.assert_called_once_with(by_id)

    def test_attach_refuses_when_externally_held(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [4321], "holder": 4321}), \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_STILL_HELD")
        self.assertEqual(resp["pids"], [4321])
        self.assertEqual(session.state, "RELEASED")
        spawn_attach.assert_not_called()

    def test_attach_force_bypasses_holder_check(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [4321], "holder": 4321}) as probe, \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0", force=True)
        self.assertTrue(resp["ok"])
        probe.assert_not_called()
        spawn_attach.assert_called_once_with(by_id)

    def test_attach_device_not_present(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        with mgr._lock:
            session.state = "RELEASED"
            mgr._released_by_ids.add("/dev/serial/by-id/orig")
        resp = mgr.attach_device("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_NOT_PRESENT")


class TestReleasedPersistence(_Base):
    def test_released_survives_restart_and_bootstrap_skips(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        profiles = [self._make_profile("p", "COM0", "lab+1", by_id)]
        mgr = self._mgr(profiles)
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        bridge.list_consoles.return_value = []
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0", source="agent:flash", reason="flash")

        # 模擬 daemon 重啟：同一 STATE_PATH、同 profiles 重建 SessionManager
        mgr2 = self._mgr(profiles)
        s2 = mgr2.get_session("COM0")
        assert s2 is not None
        self.assertEqual(s2.state, "RELEASED")
        self.assertEqual(s2.released_by, "agent:flash")
        self.assertIn(by_id, mgr2._released_by_ids)

        with mgr2._lock:
            mgr2._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        with mock.patch.object(mgr2, "_attach_by_id") as attach_by_id:
            mgr2.bootstrap_attach()
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertEqual(s2.state, "RELEASED")


class TestSelfTestReleased(_Base):
    def _released(self, holder_pids):
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            session.state = "RELEASED"
            session.released_by = "agent:flash"
            session.released_at = "now"
            session.released_reason = "flash CC2674"
        mgr._probe_external_holder = mock.MagicMock(
            return_value={"pids": holder_pids, "holder": (holder_pids[0] if holder_pids else None)}
        )
        return mgr

    def test_self_test_released_with_holder(self) -> None:
        resp = self._released([4321]).self_test("COM0")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "RELEASED")
        self.assertEqual(resp["external_holder"], [4321])
        self.assertFalse(resp["reclaimable"])
        self.assertEqual(resp["recommended_action"], "wait_external_flash")
        self.assertEqual(resp["released_by"], "agent:flash")

    def test_self_test_released_reclaimable(self) -> None:
        resp = self._released([]).self_test("COM0")
        self.assertEqual(resp["classification"], "RELEASED")
        self.assertEqual(resp["external_holder"], "none")
        self.assertTrue(resp["reclaimable"])
        self.assertEqual(resp["recommended_action"], "device_attach")

    def test_public_dict_has_released_fields(self) -> None:
        mgr = self._released([])
        pub = mgr.get_session("COM0").to_public_dict()
        self.assertEqual(pub["released_by"], "agent:flash")
        self.assertEqual(pub["released_at"], "now")


class TestDeviceRpc(_Base):
    def _service(self):
        from sw_core.service import SerialwrapService
        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        return SerialwrapService(profiles)

    def test_rpc_release_and_attach_dispatch(self) -> None:
        svc = self._service()
        svc._sessions.release_device = mock.MagicMock(return_value={"ok": True})
        svc._sessions.attach_device = mock.MagicMock(return_value={"ok": True})

        r1 = svc.rpc("device.release", {"selector": "COM0", "source": "agent:x", "reason": "flash"})
        self.assertTrue(r1["ok"])
        svc._sessions.release_device.assert_called_once_with("COM0", source="agent:x", reason="flash")

        r2 = svc.rpc("device.attach", {"selector": "COM0", "force": True})
        self.assertTrue(r2["ok"])
        svc._sessions.attach_device.assert_called_once_with("COM0", force=True)

    def test_rpc_release_requires_selector(self) -> None:
        svc = self._service()
        resp = svc.rpc("device.release", {})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")


class TestDeviceCli(_Base):
    def test_cli_parses_release_and_attach(self) -> None:
        from sw_core.cli import build_parser
        parser = build_parser()
        a = parser.parse_args(["device", "release", "--selector", "COM0", "--source", "agent:x", "--reason", "flash"])
        self.assertEqual(a.device_cmd, "release")
        self.assertEqual(a.selector, "COM0")
        self.assertEqual(a.source, "agent:x")
        self.assertEqual(a.reason, "flash")
        b = parser.parse_args(["device", "attach", "--selector", "COM0", "--force"])
        self.assertEqual(b.device_cmd, "attach")
        self.assertTrue(b.force)
        c = parser.parse_args(["device", "list"])
        self.assertEqual(c.device_cmd, "list")


import threading


class TestAdversarial(_Base):
    def test_update_devices_does_not_steal_released(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock(); bridge.stop.return_value = None; bridge.list_consoles.return_value = []
        session.bridge = bridge; session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0")
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            # USB realpath 變動（重插）
            mgr.update_devices({by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB3")})
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertEqual(session.state, "RELEASED")

    def test_concurrent_clear_and_attach_keep_invariant(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock(); bridge.stop.return_value = None; bridge.list_consoles.return_value = []
        session.bridge = bridge; session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0")
        with mock.patch.object(mgr, "_spawn_attach"):
            threads = [threading.Thread(target=mgr.clear_session, args=("COM0",)) for _ in range(8)]
            for t in threads: t.start()
            for t in threads: t.join()
        # released 不變、集合不漂移
        self.assertEqual(session.state, "RELEASED")
        self.assertIn(by_id, mgr._released_by_ids)


class TestAttachByIdReleasedBackstop(_Base):
    """C1：飛行中的 attach 在 probe 窗口期間遇到 release，
    最終 commit 區必須 RELEASED backstop（關掉剛開的 FD、不打回非 RELEASED）。
    嚴禁 mock 掉被測的 attach commit 路徑——只 mock UARTBridge（避免開真 FD）
    與 probe_ready（人為製造 probe 窗口阻塞）。"""

    def _no_login_profile(self, com: str, by_id: str) -> SessionProfile:
        # login_regex="" → _attach_by_id 走最單純的 probe_ready 分支
        return SessionProfile(
            profile_name="p", com=com, act_no=1, alias="lab+1",
            device_by_id=by_id, platform="prpl", login_regex="",
            uart=UartProfile(),
        )

    def test_inflight_attach_aborts_on_release_and_closes_fd(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._no_login_profile("COM0", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}

        in_probe = threading.Event()
        release_done = threading.Event()

        def _blocking_probe(bridge, profile, *a, **kw):
            # 進入 probe 窗口 → 通知主執行緒 → 等 release 完成後才回頭 commit
            in_probe.set()
            assert release_done.wait(timeout=5.0)
            return True, None

        with mock.patch("sw_core.session_manager.UARTBridge") as bridge_cls, \
             mock.patch("sw_core.session_manager.probe_ready", side_effect=_blocking_probe):
            bridge_cls.return_value.stop.return_value = None
            bridge_cls.return_value.vtty_path = "/dev/pts/9"

            t = threading.Thread(target=mgr._attach_by_id, args=(by_id,))
            t.start()
            assert in_probe.wait(timeout=5.0), "attach 未進入 probe 窗口"

            # release 落在 attach 飛行窗口內
            resp = mgr.release_device("COM0", source="agent:flash", reason="flash")
            self.assertTrue(resp["ok"])

            release_done.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "attach thread 未結束")

            # backstop：RELEASED 不被打回、FD 被關、集合一致
            self.assertEqual(session.state, "RELEASED")
            self.assertIsNone(session.bridge)
            self.assertIsNone(session.attached_real_path)
            self.assertIn(by_id, mgr._released_by_ids)
            bridge_cls.return_value.stop.assert_called_with(preserve_consoles=False)

    def test_attach_by_id_early_returns_when_already_released(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._no_login_profile("COM0", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        with mock.patch("sw_core.session_manager.UARTBridge") as bridge_cls:
            mgr._attach_by_id(by_id)
        # 早退：根本不應實例化 bridge（不開 FD），state 仍 RELEASED
        bridge_cls.assert_not_called()
        self.assertEqual(session.state, "RELEASED")
        self.assertIsNone(session.bridge)
        self.assertIn(by_id, mgr._released_by_ids)


class TestReleasedGuardAttachRecover(_Base):
    """C2：attach_session / recover_session 對 RELEASED session 必須早退，
    不得改 state、不得 spawn、不得動集合——否則下一次 _save_state 會把
    released map 寫空，重啟後 RELEASED 保護全失。"""

    def _released_mgr(self, by_id="/dev/serial/by-id/orig", real="/dev/ttyUSB0"):
        profiles = [self._make_profile("p", "COM0", "lab+1", by_id)]
        mgr = self._mgr(profiles)
        session = mgr.get_session("COM0")
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path=real)}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
            session.released_by = "agent:flash"
            session.released_at = "now"
            session.released_reason = "flash CC2674"
        return mgr, session, by_id, profiles

    def _assert_released_persists(self, mgr, by_id, profiles):
        # 關鍵斷言：_save_state 後重建 SessionManager（同 STATE_PATH、同 profiles）
        # 仍須 RELEASED；未修前 released map 被寫空 → 重建後 state 非 RELEASED 而 FAIL。
        mgr._save_state()
        mgr2 = self._mgr(profiles)
        s2 = mgr2.get_session("COM0")
        assert s2 is not None
        self.assertEqual(s2.state, "RELEASED")
        self.assertIn(by_id, mgr2._released_by_ids)

    def test_attach_session_on_released_early_returns_and_persists(self) -> None:
        mgr, session, by_id, profiles = self._released_mgr()
        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("released"))
        self.assertEqual(resp.get("recommended_action"), "device_attach")
        spawn_attach.assert_not_called()
        self.assertEqual(session.state, "RELEASED")
        self.assertIn(by_id, mgr._released_by_ids)
        self._assert_released_persists(mgr, by_id, profiles)

    def test_recover_session_on_released_early_returns_and_persists(self) -> None:
        mgr, session, by_id, profiles = self._released_mgr()
        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.recover_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("released"))
        self.assertEqual(resp.get("recommended_action"), "device_attach")
        spawn_attach.assert_not_called()
        self.assertEqual(session.state, "RELEASED")
        self.assertIn(by_id, mgr._released_by_ids)
        self._assert_released_persists(mgr, by_id, profiles)
