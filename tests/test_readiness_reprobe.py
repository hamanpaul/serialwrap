import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sw_core import constants
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import InteractiveLease, SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class FakeBridge:
    def __init__(self, *, last_human_input_at: float | None = None) -> None:
        self.last_human_input_at = last_human_input_at
        self.interactive_owner: str | None = None

    def list_consoles(self) -> list[dict]:
        return []

    def snapshot(self) -> dict:
        return {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "interactive_owner": self.interactive_owner,
            "last_human_input_at": self.last_human_input_at,
            "vtty": "/tmp/fake-vtty",
        }

    def console_has_external_peer(self, _client_id: str) -> bool:
        return True

    def set_interactive_owner(self, owner: str | None) -> None:
        self.interactive_owner = owner


class TestReadinessReprobe(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        sm_mod.STATE_PATH = self._old_state_path

    def _make_profile(self) -> SessionProfile:
        return SessionProfile(
            profile_name="p",
            com="COM0",
            act_no=1,
            alias="lab+1",
            device_by_id="/dev/serial/by-id/fake",
            platform="prpl",
            login_regex="",
            ready_probe="echo __READY__${nonce}",
            uart=UartProfile(),
        )

    def _make_manager(self) -> tuple[SessionManager, sm_mod.SessionRuntime]:
        profile = self._make_profile()
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        self.assertIsNotNone(session)
        assert session is not None
        with mgr._lock:
            mgr._devices = {
                profile.device_by_id: DeviceInfo(
                    by_id=profile.device_by_id,
                    real_path="/dev/ttyFAKE0",
                )
            }
        return mgr, session

    def _make_attached_candidate(self) -> tuple[SessionManager, sm_mod.SessionRuntime]:
        mgr, session = self._make_manager()
        session.state = "ATTACHED"
        session.last_error = "PROMPT_UNAVAILABLE"
        session.bridge = FakeBridge()
        session.last_rx_mono = 100.0 - constants.REPROBE_RX_IDLE_S - 0.1
        session.reprobe_attempts = 2
        session.next_reprobe_at = 99.0
        return mgr, session

    def test_attached_prompt_unavailable_reprobe_success_resets_public_progress(self) -> None:
        """ATTACHED 且 RX 已閒置時重探成功，session 回 READY 並清空公開進度。"""
        mgr, session = self._make_attached_candidate()
        bridge = session.bridge

        def probe_success(_session: sm_mod.SessionRuntime, _bridge: FakeBridge) -> dict:
            session.state = "READY"
            session.last_error = None
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=probe_success) as probe:
                mgr.reconcile_readiness()

        probe.assert_called_once_with(session, bridge)
        self.assertEqual(session.state, "READY")
        public = session.to_public_dict()
        self.assertEqual(public["reprobe_attempts"], 0)
        self.assertIsNone(public["next_reprobe_at"])
        self.assertFalse(public["reprobe_exhausted"])

    def test_rx_not_idle_does_not_reprobe_or_increment_attempts(self) -> None:
        """boot log 仍在噴時不重探，也不累加 attempts。"""
        mgr, session = self._make_attached_candidate()
        session.last_rx_mono = 100.0 - (constants.REPROBE_RX_IDLE_S / 2)
        session.reprobe_attempts = 0
        session.next_reprobe_at = None

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_probe_existing_bridge") as probe:
                mgr.reconcile_readiness()

        probe.assert_not_called()
        self.assertEqual(session.reprobe_attempts, 0)
        self.assertIsNone(session.next_reprobe_at)

    def test_human_active_flashing_and_released_are_skipped(self) -> None:
        """human-active、FLASHING、RELEASED 均不得送 probe。"""
        cases = (
            ("human-active", "ATTACHED", "PROMPT_UNAVAILABLE", True),
            ("flashing", "FLASHING", "PROMPT_UNAVAILABLE", False),
            ("released", "RELEASED", "PROMPT_UNAVAILABLE", False),
        )
        for _label, state, last_error, human_active in cases:
            with self.subTest(_label):
                mgr, session = self._make_attached_candidate()
                session.state = state
                session.last_error = last_error
                session.reprobe_attempts = 0
                bridge = FakeBridge(last_human_input_at=99.0 if human_active else None)
                session.bridge = bridge
                if human_active:
                    bridge.interactive_owner = "human:console-1"
                    lease = InteractiveLease(
                        interactive_id="lease-1",
                        session_id=session.session_id,
                        owner="human:console-1",
                        created_at="now",
                        timeout_s=60.0,
                    )
                    session.interactive_session_id = lease.interactive_id
                    mgr._interactive[lease.interactive_id] = lease

                with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
                    with mock.patch.object(mgr, "_probe_existing_bridge") as probe:
                        with mock.patch.object(mgr, "_spawn_attach") as spawn:
                            mgr.reconcile_readiness()

                probe.assert_not_called()
                spawn.assert_not_called()
                self.assertEqual(session.reprobe_attempts, 0)

    def test_failed_reprobe_backs_off_and_exhausts_at_limit(self) -> None:
        """連續失敗會 backoff，達上限後標記 exhausted 並停手。"""
        mgr, session = self._make_attached_candidate()
        session.reprobe_attempts = 0
        session.next_reprobe_at = None

        def probe_failure(_session: sm_mod.SessionRuntime, _bridge: FakeBridge) -> dict:
            session.state = "ATTACHED"
            session.last_error = "PROMPT_UNAVAILABLE"
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=probe_failure) as probe:
            for attempt in range(1, constants.REPROBE_MAX_ATTEMPTS + 1):
                now = 100.0 + attempt
                session.last_rx_mono = now - constants.REPROBE_RX_IDLE_S - 0.1
                session.next_reprobe_at = 0.0
                with mock.patch.object(sm_mod.time, "monotonic", return_value=now):
                    mgr.reconcile_readiness()
                self.assertEqual(session.reprobe_attempts, attempt)

        self.assertEqual(probe.call_count, constants.REPROBE_MAX_ATTEMPTS)
        self.assertTrue(session.reprobe_exhausted)
        with mock.patch.object(mgr, "_probe_existing_bridge") as probe_after_limit:
            with mock.patch.object(sm_mod.time, "monotonic", return_value=999.0):
                mgr.reconcile_readiness()
        probe_after_limit.assert_not_called()

    def test_detached_prompt_timeout_spawns_reprobe_attach(self) -> None:
        """DETACHED 且 prompt timeout 類錯誤會走重新連線路徑。"""
        mgr, session = self._make_manager()
        session.state = "DETACHED"
        session.last_error = "LOGIN_PROMPT_TIMEOUT"
        session.last_rx_mono = 100.0 - constants.REPROBE_RX_IDLE_S - 0.1

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_spawn_attach") as spawn:
                mgr.reconcile_readiness()

        spawn.assert_called_once_with(session.profile.device_by_id)
        self.assertEqual(session.state, "ATTACHING")
        self.assertEqual(session.reprobe_attempts, 1)
        self.assertIsNotNone(session.next_reprobe_at)


if __name__ == "__main__":
    unittest.main()
