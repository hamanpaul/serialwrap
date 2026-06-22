import tempfile
import threading
import time
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
                mgr.join_reprobe_workers(2.0)

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
                mgr.join_reprobe_workers(2.0)

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
                            mgr.join_reprobe_workers(2.0)

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
                    mgr.join_reprobe_workers(2.0)
                self.assertEqual(session.reprobe_attempts, attempt)

        self.assertEqual(probe.call_count, constants.REPROBE_MAX_ATTEMPTS)
        self.assertTrue(session.reprobe_exhausted)
        with mock.patch.object(mgr, "_probe_existing_bridge") as probe_after_limit:
            with mock.patch.object(sm_mod.time, "monotonic", return_value=999.0):
                mgr.reconcile_readiness()
                mgr.join_reprobe_workers(2.0)
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

    # --- adversarial-review 回歸（#69 codex review）-------------------------

    def test_reprobe_probe_runs_off_watcher_tick(self) -> None:
        """Finding 3：reconcile 不得同步阻塞在 probe 上；probe 須在背景 worker 執行，
        否則 DeviceWatcher tick 會被 probe 的 timeout（最長數十秒）卡住、延誤裝置偵測。"""
        mgr, session = self._make_attached_candidate()
        session.reprobe_attempts = 0
        session.next_reprobe_at = None

        started = threading.Event()
        release = threading.Event()

        def slow_probe(_session: sm_mod.SessionRuntime, _bridge: FakeBridge) -> dict:
            started.set()
            release.wait(2.0)
            session.state = "READY"
            session.last_error = None
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=slow_probe):
                mgr.reconcile_readiness()  # 必須立即返回，不等 probe 完成
                self.assertTrue(started.wait(1.0), "probe worker 未在背景啟動")
                self.assertEqual(session.state, "ATTACHED")  # probe 仍卡住，尚未回 READY
                release.set()
                mgr.join_reprobe_workers(2.0)

        self.assertEqual(session.state, "READY")

    def test_reprobe_is_single_flight_per_session(self) -> None:
        """Finding 2：同一 session 進行中的 readiness probe 不得被第二次 tick 重入，
        避免兩個 probe 在同一 bridge 上互相清 RX / 交錯 nonce / 重複寫 bytes。"""
        mgr, session = self._make_attached_candidate()
        session.reprobe_attempts = 0
        session.next_reprobe_at = None

        calls: list[int] = []
        no_overlap: list[bool] = []
        active = threading.Lock()
        first_in = threading.Event()
        let_go = threading.Event()

        def probe(_session: sm_mod.SessionRuntime, _bridge: FakeBridge) -> dict:
            no_overlap.append(active.acquire(blocking=False))
            calls.append(1)
            first_in.set()
            let_go.wait(2.0)
            try:
                if no_overlap[-1]:
                    active.release()
            except RuntimeError:
                pass
            session.state = "ATTACHED"
            session.last_error = "PROMPT_UNAVAILABLE"
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=probe):
                mgr.reconcile_readiness()        # spawn worker 1
                self.assertTrue(first_in.wait(1.0), "第一個 probe worker 未啟動")
                session.next_reprobe_at = 0.0    # 強制通過 backoff gate
                mgr.reconcile_readiness()        # worker 1 仍在跑 → 不應再 spawn
                let_go.set()
                mgr.join_reprobe_workers(2.0)

        self.assertEqual(len(calls), 1, "in-flight 期間不該重入第二個 probe")
        self.assertTrue(all(no_overlap), "probe 發生重疊")

    def test_manual_recover_rearms_exhausted_attached_session(self) -> None:
        """Finding 4：已 exhausted 的 ATTACHED session，手動 recover（顯式人工介入）即使
        當下 probe 仍失敗，也必須清掉 reprobe 上限/進度，讓之後自動重探能重新接手。"""
        mgr, session = self._make_attached_candidate()
        session.reprobe_attempts = constants.REPROBE_MAX_ATTEMPTS
        session.reprobe_exhausted = True
        session.next_reprobe_at = None

        def probe_fail(_session: sm_mod.SessionRuntime, _bridge: FakeBridge) -> dict:
            session.state = "ATTACHED"
            session.last_error = "PROMPT_UNAVAILABLE"
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=probe_fail):
            result = mgr.recover_session("COM0")

        self.assertTrue(result.get("ok"))
        self.assertFalse(session.reprobe_exhausted, "手動 recover 後 exhausted 應被清除（re-arm）")
        self.assertEqual(session.reprobe_attempts, 0, "手動 recover 後 attempts 應歸零")

    def test_reprobe_skips_when_state_flips_to_flashing_before_probe(self) -> None:
        """Finding 1：job 收集後、worker 實際 probe 前若 session 轉入 FLASHING，
        worker 必須在寫入前重新驗證並放棄，不得對燒錄中的 bridge 送 probe bytes。"""
        mgr, session = self._make_attached_candidate()
        session.reprobe_attempts = 0
        session.next_reprobe_at = None

        # 攔截 prepare：在 job 已收集（state 仍 ATTACHED）後，把 session 翻成 FLASHING，
        # 模擬 flash broker 在 lock 釋放後搶進 FLASHING 的競態。
        orig_prepare = mgr._prepare_reprobe_locked

        def prepare_then_flip(sess, now):
            job = orig_prepare(sess, now)
            if job is not None:
                sess.state = "FLASHING"
            return job

        with mock.patch.object(sm_mod.time, "monotonic", return_value=100.0):
            with mock.patch.object(mgr, "_prepare_reprobe_locked", side_effect=prepare_then_flip):
                with mock.patch.object(mgr, "_probe_existing_bridge") as probe:
                    mgr.reconcile_readiness()
                    mgr.join_reprobe_workers(2.0)

        probe.assert_not_called()

    def test_probe_result_does_not_clobber_flashing_or_released_state(self) -> None:
        """Finding 1 round2：probe 在 lock 外阻塞期間 session 被搶進 FLASHING/RELEASED 時，
        probe 結果不得覆寫狀態——否則 FLASHING 被打回 ATTACHED，flasher 結束時 exit_flashing
        提早 return、bridge 永久卡在 flash 模式並靜默丟棄非 flash 寫入。"""
        mgr, session = self._make_attached_candidate()
        bridge = session.bridge

        for clobber_state in ("FLASHING", "RELEASED"):
            with self.subTest(clobber_state):
                session.state = "ATTACHED"
                session.last_error = "PROMPT_UNAVAILABLE"

                def fake_probe(_bridge: object, _sp: object) -> tuple[bool, None]:
                    # 模擬 probe 進行中 flash broker / device release 搶進改了狀態。
                    session.state = clobber_state
                    return True, None

                with mock.patch.object(sm_mod, "probe_ready", side_effect=fake_probe):
                    mgr._probe_existing_bridge(session, bridge)

                self.assertEqual(session.state, clobber_state)
                self.assertNotEqual(session.state, "READY")

    def test_slow_failed_probe_backoff_uses_completion_time(self) -> None:
        """Finding round6：probe 阻塞超過 backoff 間隔時，失敗後的 next_reprobe_at 必須以 probe
        完成時間（非開始時間）計算；否則 next_reprobe_at 落在過去、下一 tick 立刻重探、過早 exhausted。"""
        mgr, session = self._make_attached_candidate()
        session.reprobe_attempts = 0
        session.next_reprobe_at = None
        session.last_rx_mono = 0.0  # 視為 RX 已閒置

        clock = {"t": 100.0}

        def fake_monotonic() -> float:
            return clock["t"]

        def slow_probe_fail(_session: sm_mod.SessionRuntime, _bridge: FakeBridge) -> dict:
            clock["t"] += 20.0  # 模擬 probe 阻塞 20s（遠大於首次 backoff 2s）
            session.state = "ATTACHED"
            session.last_error = "PROMPT_UNAVAILABLE"
            return {"ok": True, "session": session.to_public_dict()}

        with mock.patch.object(sm_mod.time, "monotonic", side_effect=fake_monotonic):
            with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=slow_probe_fail):
                mgr.reconcile_readiness()
                mgr.join_reprobe_workers(2.0)

        self.assertEqual(session.reprobe_attempts, 1)
        self.assertFalse(session.reprobe_exhausted)
        # 完成時間 120 + 首次 backoff 2 = 122；不得是 start(100)+2=102（落在 120 之前）。
        self.assertIsNotNone(session.next_reprobe_at)
        self.assertGreaterEqual(session.next_reprobe_at, 120.0)


if __name__ == "__main__":
    unittest.main()
