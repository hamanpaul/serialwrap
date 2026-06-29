"""Human console raw interactive mode 整合測試。

驗證方案 2（minicom 預設 raw interactive ownership）的核心行為：
- READY 狀態 console-attach 自動授予 interactive
- ESC 序列（方向鍵）在 raw mode 下正確透傳
- Agent 命令暫時掛起 human interactive 後執行
- Human 輸入在 agent 執行期間 deferred → 完成後 flush
- Console detach 釋放 interactive ownership
"""
from __future__ import annotations

import os
import pty
import select
import tempfile
import termios
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import InteractiveLease, SessionManager
from sw_core.uart_io import ConsoleClient, UARTBridge
from sw_core.util import now_iso
from sw_core.wal import WalWriter


class FakeTarget:
    """簡易 PTY 模擬 target：收集 TX 資料，可注入 RX 回應。"""

    def __init__(self) -> None:
        master, slave = pty.openpty()
        self.master_fd = master
        self.slave_fd = slave
        self.slave_path = os.ttyname(slave)
        self.received: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            rlist, _, _ = select.select([self.master_fd], [], [], 0.05)
            if rlist:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        self.received.append(data)
                except OSError:
                    break

    def emit(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    def collected(self) -> bytes:
        return b"".join(self.received)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        os.close(self.master_fd)
        os.close(self.slave_fd)


def _make_profile(com: str = "COM0", alias: str = "test") -> SessionProfile:
    return SessionProfile(
        profile_name="test-profile",
        com=com,
        act_no=0,
        alias=alias,
        platform="shell",
        device_by_id="/dev/serial/by-id/test",
        uart=UartProfile(),
        prompt_regex=r"(?m)^root@.*[#$]\s*$",
    )


class TestAttachConsoleInReadyGrantsInteractive(unittest.TestCase):
    """READY 狀態下 console-attach 應自動授予 interactive ownership。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from sw_core import session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        from sw_core import session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def test_attach_console_in_ready_grants_interactive(self) -> None:
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.attach_console.return_value = {
            "client_id": "cid-1", "label": "minicom:1", "vtty": "/dev/pts/99",
        }
        session.bridge.console_has_external_peer.return_value = True
        session.bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        session.state = "READY"

        resp = mgr.attach_console("COM0", label="minicom:1")

        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("interactive_owner"))
        self.assertIsNotNone(resp.get("interactive_session_id"))
        self.assertIsNotNone(session.interactive_session_id)

    def test_second_console_does_not_get_interactive(self) -> None:
        """已有 interactive lease 時，第二個 console 不應取得 ownership。"""
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.attach_console.side_effect = [
            {"client_id": "cid-1", "label": "first", "vtty": "/dev/pts/10"},
            {"client_id": "cid-2", "label": "second", "vtty": "/dev/pts/11"},
        ]
        session.bridge.console_has_external_peer.return_value = True
        session.bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        session.state = "READY"

        resp1 = mgr.attach_console("COM0", label="first")
        self.assertTrue(resp1.get("interactive_owner"))

        resp2 = mgr.attach_console("COM0", label="second")
        self.assertFalse(resp2.get("interactive_owner", False))

    def test_detach_console_releases_interactive(self) -> None:
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.bridge.attach_console.return_value = {
            "client_id": "cid-1", "label": "minicom:1", "vtty": "/dev/pts/99",
        }
        session.bridge.console_has_external_peer.return_value = True
        session.bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        session.bridge.detach_console.return_value = True
        session.bridge.vtty_path = "/dev/pts/99"
        session.state = "READY"

        mgr.attach_console("COM0", label="minicom:1")
        self.assertIsNotNone(session.interactive_session_id)

        mgr.detach_console("COM0", "cid-1")
        self.assertIsNone(session.interactive_session_id)


class TestEscapeSequencesPassThrough(unittest.TestCase):
    """方向鍵 / Tab 在 raw interactive mode 下正確透傳到 UART。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._target = FakeTarget()
        self.addCleanup(self._target.close)
        wal = WalWriter(wal_dir=self._tmp.name)
        self._bridge = UARTBridge(
            com="COM0",
            device_path=self._target.slave_path,
            profile=UartProfile(),
            wal=wal,
        )
        self._bridge.start()
        time.sleep(0.1)

    def tearDown(self) -> None:
        self._bridge.stop()

    def test_arrow_keys_pass_through_raw(self) -> None:
        """ESC [ A/B/C/D 在 raw mode 下完整送到 target。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        vtty_fd = os.open(console["vtty"], os.O_RDWR | os.O_NOCTTY)
        try:
            for seq in [b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"]:
                os.write(vtty_fd, seq)
            time.sleep(0.3)
        finally:
            os.close(vtty_fd)

        collected = self._target.collected()
        for seq in [b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"]:
            self.assertIn(seq, collected, f"target 應收到 {seq!r}")

    def test_tab_passes_through_raw(self) -> None:
        """Tab (0x09) 在 raw mode 下立即送到 target，不等 Enter。"""
        console = self._bridge.attach_console(label="human")
        cid = console["client_id"]
        self._bridge.set_interactive_owner(f"human:{cid}")

        vtty_fd = os.open(console["vtty"], os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(vtty_fd, b"\x09")
            time.sleep(0.2)
        finally:
            os.close(vtty_fd)

        collected = self._target.collected()
        self.assertIn(b"\x09", collected, "target 應收到 Tab")


class TestAgentSuspendsHumanInteractive(unittest.TestCase):
    """Agent 命令到達時 suspend human interactive，完成後 resume。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from sw_core import session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        from sw_core import session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def test_agent_command_suspends_and_resumes(self) -> None:
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.console_has_external_peer.return_value = True
        bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        bridge.vtty_path = "/dev/pts/9"
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        bridge.rx_text_from.return_value = "hello\nroot@host:~# "
        session.bridge = bridge
        session.state = "READY"

        lease = InteractiveLease(
            interactive_id="lease-1",
            session_id=session.session_id,
            owner="human:cid-1",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        resp = mgr.execute_command("test-profile:COM0", "echo hello", "agent:1", "cmd-1", timeout_s=5.0)

        self.assertTrue(resp["ok"])
        bridge.suspend_interactive.assert_called_once()
        bridge.resume_interactive.assert_called_once()
        self.assertEqual(session.interactive_session_id, "lease-1",
                         "interactive lease 不應被關閉，只是暫時掛起")

    def test_human_input_deferred_during_agent_then_flushed(self) -> None:
        """Agent 執行期間 human 輸入進 deferred buffer，完成後 flush。"""
        tmp = self._tmp
        target = FakeTarget()
        self.addCleanup(target.close)
        wal = WalWriter(wal_dir=tmp.name)
        bridge = UARTBridge(
            com="COM0",
            device_path=target.slave_path,
            profile=UartProfile(),
            wal=wal,
        )
        bridge.start()
        self.addCleanup(bridge.stop)
        time.sleep(0.1)

        console = bridge.attach_console(label="human")
        cid = console["client_id"]
        bridge.set_interactive_owner(f"human:{cid}")

        bridge.suspend_interactive()

        vtty_fd = os.open(console["vtty"], os.O_RDWR | os.O_NOCTTY)
        try:
            os.write(vtty_fd, b"deferred input\r")
            time.sleep(0.2)
        finally:
            os.close(vtty_fd)

        with bridge._state_lock:
            buf = bridge._deferred_buffers.get(cid, bytearray())
        self.assertIn(b"deferred input", bytes(buf))

        bridge.resume_interactive()
        time.sleep(0.3)

        collected = target.collected()
        self.assertIn(b"deferred input", collected, "flush 後 target 應收到 deferred 的內容")

    def test_interactive_restored_after_agent_command(self) -> None:
        """Agent 命令完成後 human console 恢復 raw mode。"""
        profiles = [_make_profile()]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                             on_ready=lambda _: None, on_detached=lambda _: None)
        session = mgr.get_session("COM0")
        assert session is not None

        suspend_calls: list[str] = []
        resume_calls: list[str] = []

        bridge = mock.MagicMock()
        bridge.console_has_external_peer.return_value = True
        bridge.snapshot.return_value = {"interactive_owner": "human:cid-1"}
        bridge.vtty_path = "/dev/pts/9"
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        bridge.rx_text_from.return_value = "output\nroot@host:~# "
        bridge.suspend_interactive.side_effect = lambda: suspend_calls.append("suspend")
        bridge.resume_interactive.side_effect = lambda: resume_calls.append("resume")
        session.bridge = bridge
        session.state = "READY"

        lease = InteractiveLease(
            interactive_id="lease-r",
            session_id=session.session_id,
            owner="human:cid-1",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        mgr.execute_command("test-profile:COM0", "ls", "agent:1", "cmd-r", timeout_s=5.0)

        self.assertEqual(suspend_calls, ["suspend"])
        self.assertEqual(resume_calls, ["resume"])


class TestHumanLeasePeerLossGrace(unittest.TestCase):
    """human lease peer-loss grace 測試（症狀1 觸發B）。

    _refresh_interactive_locked 對 human lease 的 peer-loss 應有 grace 窗：
    - 首次 peer-False：記錄時間戳，暫不拆 lease。
    - 再次 peer-False 但仍在 grace 窗內：繼續持有 lease。
    - peer 回復：清 peer_lost_at。
    - 超過 grace 窗：才真正拆 lease。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import sw_core.session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        import sw_core.session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def _make_mgr_with_human_lease(self) -> tuple:
        """建立 SessionManager + 一個已持有 human interactive lease 的 session。

        Returns:
            (mgr, session, bridge_mock, lease)
        """
        profiles = [_make_profile()]
        mgr = SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None,
            on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.console_has_external_peer.return_value = True
        bridge.snapshot.return_value = {"interactive_owner": "human:cid-grace"}
        bridge.vtty_path = "/dev/pts/99"
        bridge.detach_console.return_value = True
        session.bridge = bridge
        session.state = "READY"

        lease = InteractiveLease(
            interactive_id="lease-grace",
            session_id=session.session_id,
            owner="human:cid-grace",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        return mgr, session, bridge, lease

    def test_peer_flap_within_grace_keeps_lease(self) -> None:
        """grace 窗內首次 peer-loss 不應拆 lease，peer 回復後清 peer_lost_at。"""
        mgr, session, bridge, lease = self._make_mgr_with_human_lease()

        # 模擬 peer 瞬時消失
        bridge.console_has_external_peer.return_value = False

        with mgr._lock:
            result, _post = mgr._refresh_interactive_locked(session)

        self.assertIsNotNone(result, "grace 窗內首次 peer-loss 不應拆 lease")
        self.assertIsNotNone(result.peer_lost_at, "首次 peer-loss 應記錄 peer_lost_at")
        # session 的 interactive_session_id 應仍存在
        self.assertIsNotNone(session.interactive_session_id)

        # peer 回復
        bridge.console_has_external_peer.return_value = True
        bridge.snapshot.return_value = {"interactive_owner": "human:cid-grace"}

        with mgr._lock:
            result2, _post2 = mgr._refresh_interactive_locked(session)

        self.assertIsNotNone(result2, "peer 回復後 lease 應持有")
        self.assertIsNone(result2.peer_lost_at, "peer 回復應清 peer_lost_at")

    def test_peer_gone_past_grace_tears_down(self) -> None:
        """超過 grace 窗後，_refresh_interactive_locked 應拆 lease。"""
        from sw_core.constants import _HUMAN_PEER_GRACE_S

        mgr, session, bridge, lease = self._make_mgr_with_human_lease()

        # 首次 peer-False → 記錄 peer_lost_at
        bridge.console_has_external_peer.return_value = False

        with mgr._lock:
            result, _post = mgr._refresh_interactive_locked(session)

        self.assertIsNotNone(result, "首次 peer-loss 應仍持有 lease")
        # 強制 peer_lost_at 超過 grace 窗
        result.peer_lost_at -= (_HUMAN_PEER_GRACE_S + 1.0)

        # 再次 refresh：應拆 lease
        with mgr._lock:
            result2, _post2 = mgr._refresh_interactive_locked(session)

        self.assertIsNone(result2, "超過 grace 窗應拆 lease，回傳 None")
        self.assertIsNone(session.interactive_session_id, "拆後 session.interactive_session_id 應為 None")


class TestReconcileReapsOrphanConsole(unittest.TestCase):
    """reconcile_readiness 應週期回收無外部 reader 的孤兒 console（#76）。

    以不啟動 bridge（無真實 UART）的方式，直接注入孤兒 ConsoleClient，
    確認 reconcile_readiness 在 wired 後能主動回收。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import sw_core.session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        import sw_core.session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def test_reconcile_reaps_orphan_console(self) -> None:
        """孤兒 console（attached_at 已逾 grace、無外部 holder）應在 reconcile 週期被回收。"""
        profile = _make_profile("COM0")
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None,
            on_detached=lambda _: None,
        )

        # 建立不啟動的 UARTBridge（不需要真實 UART）
        bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=profile.uart,
            wal=WalWriter(wal_dir=self._tmp.name),
        )

        # 注入孤兒 console client：attached_at 設為 100s 前，已遠超過 grace period
        orphan = ConsoleClient(
            client_id="orphan-cid",
            label="dead-minicom",
            master_fd=-1,
            slave_fd=-1,
            slave_path="/dev/pts/999",
            attached_at=time.time() - 100.0,
        )
        bridge._clients["orphan-cid"] = orphan

        # 將 bridge 掛到 session（模擬 ATTACHED/READY 狀態下 bridge 存在）
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            session.bridge = bridge

        # monkeypatch：模擬無外部 process 持有任何 pts（確定性，不依賴真實 /proc 狀態）
        bridge._enumerate_all_held_paths = lambda: set()  # type: ignore[assignment]

        # 重置節流時間戳（若已存在），確保本次 reconcile 必然觸發回收
        try:
            mgr._last_console_reap_at = 0.0
        except AttributeError:
            pass  # RED 階段：屬性尚不存在，跳過；GREEN 後 __init__ 會建立

        mgr.reconcile_readiness()

        # 孤兒應已被回收（從 bridge._clients 移除）
        self.assertNotIn(
            "orphan-cid",
            bridge._clients,
            "reconcile_readiness 應回收無外部 holder 且逾 grace 的孤兒 console",
        )


class TestSelfHealPeriodicGrant(unittest.TestCase):
    """lease-backed 週期自癒重授 raw ownership（症狀1 觸發C、Task 7）。

    self-heal 在 reconcile_readiness 週期：bridge 無 owner + 無 agent_active + 無 flash +
    primary console 有外部 peer → try_grant_interactive_if_idle 原子授予 → 補 session 層 lease。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import sw_core.session_manager as sm_mod
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")

    def tearDown(self) -> None:
        import sw_core.session_manager as sm_mod
        sm_mod.STATE_PATH = self._old_state_path

    def _attach_human_owner(self) -> tuple:
        """建立 SessionManager + 持有 human interactive lease 的 session。

        bridge 不啟動（無 UART），直接注入 ConsoleClient 到 _clients，並 monkeypatch
        console_has_external_peer 永遠對 primary_cid 回 True（確定性，不依賴真實 /proc）。

        Returns:
            (mgr, session, bridge, primary_cid)
        """
        profiles = [_make_profile()]
        mgr = SessionManager(
            profiles,
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _: None,
            on_detached=lambda _: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "READY"

        bridge = UARTBridge(
            com="COM0",
            device_path="/dev/null",
            profile=_make_profile().uart,
            wal=WalWriter(wal_dir=self._tmp.name),
        )
        cid = "heal-cid-1"
        client = ConsoleClient(
            client_id=cid,
            label="minicom-heal",
            master_fd=-1,
            slave_fd=-1,
            slave_path="/dev/pts/77",
            attached_at=time.time(),
        )
        with bridge._state_lock:
            bridge._clients[cid] = client
            bridge._primary_client_id = cid

        # 設定 bridge owner（表示 console 當前持有 ownership）
        bridge.set_interactive_owner(f"human:{cid}")

        # monkeypatch /proc 掃描：reaper 算出的共享 held 含 primary 的 slave_path（確定性，
        # 不依賴真實 /proc）。self-heal 複用此 held（snap["vtty"] in held）判定活 primary。
        bridge._enumerate_all_held_paths = lambda: {"/dev/pts/77"}  # type: ignore[method-assign]
        # 保留 console_has_external_peer monkeypatch 作 held=None fallback 路徑的保險（一般不會走到）
        bridge.console_has_external_peer = lambda client_id: client_id == cid  # type: ignore[method-assign]

        # 掛到 session
        with mgr._lock:
            session.bridge = bridge

        # 建 interactive lease
        lease = InteractiveLease(
            interactive_id=uuid.uuid4().hex,
            session_id=session.session_id,
            owner=f"human:{cid}",
            created_at=now_iso(),
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        return mgr, session, bridge, cid

    def test_self_heal_regrants_after_owner_loss(self) -> None:
        """owner 掉失（bridge owner=None, session lease=None）+ primary console 活著
        → reconcile_readiness 後自癒重授 ownership 並補 session lease。"""
        mgr, session, bridge, cid = self._attach_human_owner()

        # 模擬 owner 掉失：清 bridge owner 與 session lease
        bridge.set_interactive_owner(None)
        session.interactive_session_id = None

        # 強制通過節流
        mgr._last_console_reap_at = 0.0
        mgr.reconcile_readiness()

        snap = bridge.snapshot()
        self.assertEqual(
            snap["interactive_owner"],
            f"human:{cid}",
            "自癒後 bridge 的 interactive_owner 應重授給 primary console",
        )
        self.assertIsNotNone(
            session.interactive_session_id,
            "自癒後 session.interactive_session_id 應有新 lease",
        )

    def test_self_heal_skips_during_agent_active(self) -> None:
        """agent 進行中（suspend_interactive → agent_active=True）→ reconcile 不應自癒奪權。"""
        mgr, session, bridge, cid = self._attach_human_owner()

        # 清 bridge owner 和 session lease，再模擬 agent 進行中
        bridge.set_interactive_owner(None)
        session.interactive_session_id = None
        bridge.suspend_interactive()  # _agent_active = True

        mgr._last_console_reap_at = 0.0
        mgr.reconcile_readiness()

        self.assertIsNone(
            bridge.snapshot()["interactive_owner"],
            "agent 進行中不得自癒奪權（agent_active=True 應跳過）",
        )
        bridge.resume_interactive()  # 清理 suspend 狀態

    def test_self_heal_grant_fails_on_toctou(self) -> None:
        """snapshot 判為 idle 後、grant 前另一條路設了 owner → try_grant 回 False → 不開 lease。"""
        mgr, session, bridge, cid = self._attach_human_owner()

        # 清 bridge owner 和 session lease
        bridge.set_interactive_owner(None)
        session.interactive_session_id = None

        # monkeypatch try_grant：在 grant 前模擬 owner 已被搶先設定（TOCTOU 競態）
        orig_grant = bridge.try_grant_interactive_if_idle

        def racing_grant(owner: str) -> bool:
            # 模擬 grant 前另一條路已設 owner → orig_grant 看到非 None → 回 False
            bridge.set_interactive_owner(f"human:{cid}")
            return orig_grant(owner)

        bridge.try_grant_interactive_if_idle = racing_grant  # type: ignore[method-assign]

        mgr._last_console_reap_at = 0.0
        mgr.reconcile_readiness()

        # try_grant 回 False → 不呼 _record_self_heal_lease_locked → interactive_session_id 仍 None
        self.assertIsNone(
            session.interactive_session_id,
            "TOCTOU race 後不得開出衝突的新 lease（interactive_session_id 應仍 None）",
        )
