"""#198 ownership：操作 admission、epoch cleanup 與 interactive handle 邊界。

這些測試刻意以 SessionManager 搭配可記錄 TX 的 fake UART bridge 驗證邊界，
並以 Event/barrier 固定競態時序；不以 sleep 推測另一執行緒是否已進入操作。
"""
from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
from sw_core.uart_io import UARTBridge
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class RecordingBridge:
    """最小但具 UART 邊界語意的 bridge：記錄 bytes，必要時在 TX barrier 停住。"""

    def __init__(self, name: str, *, block_tx: bool = False) -> None:
        self.name = name
        self.tx: list[bytes] = []
        self.tx_started = threading.Event()
        self.release_tx = threading.Event()
        self.block_tx = block_tx
        self.stop_calls = 0
        self.suspend_calls = 0
        self.resume_calls = 0
        self.raise_on_resume = False
        self.interactive_owner: str | None = None
        self.external_peers: set[str] | None = None
        self.vtty_path: str | None = None

    def send_bytes(
        self,
        payload: bytes,
        *,
        source: str,
        cmd_id: str | None = None,
        log: bool = True,
        _allow_during_flash: bool = False,
    ) -> None:
        del source, cmd_id, log, _allow_during_flash
        self.tx.append(bytes(payload))
        if self.block_tx:
            self.tx_started.set()
            if not self.release_tx.wait(5.0):
                raise AssertionError(f"{self.name} TX barrier 未釋放")

    def send_command(self, command: str, *, source: str, cmd_id: str | None = None) -> None:
        self.send_bytes(
            command.encode("utf-8") + b"\n",
            source=source,
            cmd_id=cmd_id,
        )

    def stop(self, *, preserve_consoles: bool = True) -> None:
        del preserve_consoles
        self.stop_calls += 1

    def set_interactive_owner(self, owner: str | None) -> None:
        self.interactive_owner = owner

    def list_consoles(self) -> list[dict[str, Any]]:
        return []

    def console_endpoint(self) -> str | None:
        return None

    def suspend_interactive(self) -> None:
        self.suspend_calls += 1

    def resume_interactive(self) -> None:
        self.resume_calls += 1
        if self.raise_on_resume:
            raise RuntimeError("resume failure")

    def console_has_external_peer(self, client_id: str) -> bool:
        return self.external_peers is None or client_id in self.external_peers

    def detach_console(self, _client_id: str) -> bool:
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": True,
            "serial_alive": True,
            "vtty_alive": True,
            "interactive_owner": self.interactive_owner,
            "last_human_input_at": None,
            "vtty": None,
        }

    def rx_snapshot_len(self) -> int:
        return 0

    def wait_for_regex_from(self, _pattern: str, _offset: int, _timeout_s: float) -> bool:
        return True

    def rx_text_from(self, _offset: int) -> str:
        return ""

    def rx_tail(self, _max_chars: int = 2048) -> str:
        return ""


class TestRefineOwnership(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        profile = SessionProfile(
            profile_name="p",
            com="COM0",
            act_no=1,
            alias="lab+1",
            device_by_id="/dev/serial/by-id/fake",
            platform="prpl",
            ready_probe="echo __READY__${nonce}",
            uart=UartProfile(),
        )
        self.manager = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
            state_path=str(Path(self._tmp.name) / "state.json"),
        )
        self.session = self.manager.get_session("COM0")
        assert self.session is not None
        self.session.state = "READY"
        self.bridge = RecordingBridge("initial")
        self.session.bridge = self.bridge  # type: ignore[assignment]
        self.session.bridge_generation = 1

    def _join(self, thread: threading.Thread) -> None:
        thread.join(5.0)
        self.assertFalse(thread.is_alive(), "操作執行緒未在 barrier 釋放後結束")

    def test_blocking_push_rejects_competing_push_without_tx(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def blocking_push(bridge: RecordingBridge, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(bridge.name)
            bridge.send_command("push-start", source=kwargs["source"])
            started.set()
            if len(calls) > 1:
                return {"ok": True}
            self.assertTrue(release.wait(5.0))
            bridge.send_command("push-end", source=kwargs["source"])
            return {"ok": True}

        with mock.patch("sw_core.file_transfer.push_file", side_effect=blocking_push):
            first_result: list[dict[str, Any]] = []
            first = threading.Thread(
                target=lambda: first_result.append(
                    self.manager.file_push(
                        "COM0", local_path="/tmp/src", remote_path="/tmp/dst"
                    )
                )
            )
            first.start()
            self.assertTrue(started.wait(2.0), "第一個 push 未抵達 bridge barrier")
            writes_before = len(self.bridge.tx)

            try:
                loser = self.manager.file_push(
                    "COM0", local_path="/tmp/src2", remote_path="/tmp/dst2"
                )
                self.assertFalse(loser["ok"])
                self.assertEqual(loser["error_code"], "SESSION_BUSY")
                self.assertEqual(len(self.bridge.tx), writes_before)
                self.assertEqual(calls, ["initial"])
            finally:
                release.set()
                self._join(first)

        self.assertEqual(first_result, [{"ok": True}])
        self.assertFalse(self.session.foreground_busy)

    def test_blocking_command_rejects_competing_pull_without_tx(self) -> None:
        self.bridge.block_tx = True
        first_result: list[dict[str, Any]] = []
        first = threading.Thread(
            target=lambda: first_result.append(
                self.manager.execute_command(
                    "p:COM0", "echo first", "agent:a", "cmd-a"
                )
            )
        )
        first.start()
        self.assertTrue(self.bridge.tx_started.wait(2.0), "第一個 command 未抵達 bridge barrier")
        writes_before = len(self.bridge.tx)

        with mock.patch("sw_core.file_transfer.pull_file", return_value={"ok": True}) as pull:
            loser = self.manager.file_pull(
                "COM0", remote_path="/tmp/remote", local_path="/tmp/local"
            )

        try:
            self.assertFalse(loser["ok"])
            self.assertEqual(loser["error_code"], "SESSION_BUSY")
            self.assertEqual(len(self.bridge.tx), writes_before)
            pull.assert_not_called()
        finally:
            self.bridge.release_tx.set()
            self._join(first)
        self.assertTrue(first_result[0]["ok"])
        self.assertFalse(self.session.foreground_busy)

    def test_command_admission_covers_gap_before_inner_execution(self) -> None:
        entered_inner = threading.Event()
        release_inner = threading.Event()

        def hold_inner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            entered_inner.set()
            self.assertTrue(release_inner.wait(5.0))
            return {"ok": True}

        def competing_pull(bridge: RecordingBridge, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            bridge.send_command("unexpected-loser-tx", source=kwargs["source"])
            return {"ok": True}

        with mock.patch.object(self.manager, "_execute_command_inner", side_effect=hold_inner):
            first_result: list[dict[str, Any]] = []
            first = threading.Thread(
                target=lambda: first_result.append(
                    self.manager.execute_command(
                        "p:COM0", "echo first", "agent:a", "cmd-a", mode="interactive"
                    )
                )
            )
            first.start()
            self.assertTrue(entered_inner.wait(2.0), "command 未進入 inner barrier")
            writes_before = len(self.bridge.tx)

            with mock.patch("sw_core.file_transfer.pull_file", side_effect=competing_pull):
                loser = self.manager.file_pull(
                    "COM0", remote_path="/tmp/remote", local_path="/tmp/local"
                )

            try:
                self.assertFalse(loser["ok"])
                self.assertEqual(loser["error_code"], "SESSION_BUSY")
                self.assertEqual(len(self.bridge.tx), writes_before)
            finally:
                release_inner.set()
                self._join(first)

        self.assertEqual(first_result, [{"ok": True}])
        self.assertFalse(self.session.foreground_busy)

    def _assert_new_console_is_line_buffered_during_operation(
        self,
        patch_kind: str,
        invoke: Callable[[], dict[str, Any]],
        *,
        fail_after_release: bool = False,
    ) -> None:
        """操作 admission 後新 console 僅能走 line broker，結束後恢復 raw。"""
        lines: list[tuple[str, str]] = []
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
            on_console_line=lambda client_id, line: lines.append((client_id, line)),
        )
        self.addCleanup(bridge.stop)
        self.session.bridge = bridge  # type: ignore[assignment]
        transmitted: list[bytes] = []
        entered = threading.Event()
        release = threading.Event()
        outcomes: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def record_tx(payload: bytes, **_kwargs: Any) -> None:
            transmitted.append(bytes(payload))

        def guarded_operation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.assertTrue(self.session.foreground_busy)
            attached = self.manager.attach_console("COM0", label="during-operation")
            self.assertTrue(attached["ok"])
            client_id = attached["client_id"]
            assert client_id is not None
            client = bridge._clients[client_id]
            bridge._handle_console_rx(client, b"human-input")
            self.assertEqual(transmitted, [], "operation 期間新 console 不得直送 UART")
            bridge._handle_console_rx(client, b"\n")
            self.assertEqual(lines, [(client_id, "human-input")])
            self.assertFalse(
                attached.get("interactive_owner", False),
                "operation 期間新 console 不得宣告已取得 raw ownership",
            )
            entered.set()
            self.assertTrue(release.wait(5.0), "operation barrier 未釋放")
            if fail_after_release:
                raise RuntimeError("operation failure")
            return {"ok": True}

        operation_patch: Any
        if patch_kind == "command":
            operation_patch = mock.patch.object(
                self.manager, "_execute_command_inner", side_effect=guarded_operation
            )
        else:
            operation_patch = mock.patch(
                f"sw_core.file_transfer.{patch_kind}_file", side_effect=guarded_operation
            )

        with mock.patch.object(bridge, "send_bytes", side_effect=record_tx), operation_patch:
            worker = threading.Thread(
                target=lambda: self._capture_operation(invoke, outcomes, errors),
            )
            worker.start()
            try:
                if not entered.wait(2.0):
                    self.fail(f"操作未抵達新 console barrier：errors={errors!r}")
            finally:
                release.set()
                self._join(worker)

        if fail_after_release:
            self.assertEqual(outcomes, [])
            self.assertEqual([type(error) for error in errors], [RuntimeError])
        else:
            self.assertEqual(outcomes, [{"ok": True}])
            self.assertEqual(errors, [])
        self.assertFalse(self.session.foreground_busy)
        with bridge._state_lock:
            owner = bridge._interactive_owner
        self.assertIsNotNone(owner, "operation 完成後新 console 應恢復 raw ownership")
        client_id = owner.split(":", 1)[1]
        client = bridge._clients[client_id]
        with mock.patch.object(bridge, "send_bytes", side_effect=record_tx):
            bridge._handle_console_rx(client, b"after-operation")
        self.assertEqual(transmitted, [b"after-operation"])

    @staticmethod
    def _capture_operation(
        invoke: Callable[[], dict[str, Any]],
        outcomes: list[dict[str, Any]],
        errors: list[BaseException],
    ) -> None:
        try:
            outcomes.append(invoke())
        except BaseException as error:  # noqa: BLE001 — 測試需保留 operation 例外供主執行緒斷言
            errors.append(error)

    def test_new_console_during_transfer_does_not_send_raw(self) -> None:
        """root review regression：file push 期間新 console 不得 raw TX。"""
        self._assert_new_console_is_line_buffered_during_operation(
            "push",
            lambda: self.manager.file_push(
                "COM0", local_path="/tmp/fake", remote_path="/tmp/fake"
            ),
        )

    def test_new_console_during_file_pull_is_line_buffered_and_restored(self) -> None:
        self._assert_new_console_is_line_buffered_during_operation(
            "pull",
            lambda: self.manager.file_pull(
                "COM0", remote_path="/tmp/fake", local_path="/tmp/fake"
            ),
        )

    def test_new_console_during_command_is_line_buffered_and_restored(self) -> None:
        self._assert_new_console_is_line_buffered_during_operation(
            "command",
            lambda: self.manager.execute_command(
                "p:COM0", "echo command", "agent:test", "cmd-console-gate"
            ),
        )

    def test_new_console_gate_reopens_after_operation_exception(self) -> None:
        self._assert_new_console_is_line_buffered_during_operation(
            "push",
            lambda: self.manager.file_push(
                "COM0", local_path="/tmp/fake", remote_path="/tmp/fake"
            ),
            fail_after_release=True,
        )

    def test_existing_human_owner_window_is_deferred_before_suspend(self) -> None:
        """admission 到實際 suspend 間既有 human owner 也不得 raw TX。"""
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
        )
        self.addCleanup(bridge.stop)
        self.session.bridge = bridge  # type: ignore[assignment]
        attached = self.manager.attach_console("COM0", label="existing-human")
        self.assertTrue(attached["ok"])
        client_id = attached["client_id"]
        assert client_id is not None
        client = bridge._clients[client_id]
        transmitted: list[bytes] = []
        admission_seen = threading.Event()

        def record_tx(payload: bytes, **_kwargs: Any) -> None:
            transmitted.append(bytes(payload))

        original_set_admission = bridge._set_interactive_admission

        def inject_during_admission(allowed: bool) -> None:
            original_set_admission(allowed)
            if not allowed:
                bridge._handle_console_rx(client, b"window-input")
                self.assertEqual(transmitted, [])
                admission_seen.set()

        bridge._set_interactive_admission = inject_during_admission  # type: ignore[method-assign]
        with mock.patch.object(bridge, "send_bytes", side_effect=record_tx), mock.patch(
            "sw_core.file_transfer.push_file", return_value={"ok": True}
        ):
            result = self.manager.file_push(
                "COM0", local_path="/tmp/fake", remote_path="/tmp/fake"
            )

        self.assertTrue(result["ok"])
        self.assertTrue(admission_seen.is_set())
        self.assertEqual(transmitted, [b"window-input"], "resume 後才可 flush deferred human input")
        self.assertFalse(self.session.foreground_busy)

    def test_tcp_accept_during_operation_gate_does_not_get_raw_owner(self) -> None:
        """TCP accept 路徑也必須服從同一 interactive admission gate。"""
        lines: list[tuple[str, str]] = []
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
            on_console_line=lambda client_id, line: lines.append((client_id, line)),
        )
        self.addCleanup(bridge.stop)
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        bridge._console_endpoint = "127.0.0.1:48700"
        bridge._set_interactive_admission(False)
        bridge._accept_console_conn(left, ("127.0.0.1", 12345))
        with bridge._state_lock:
            client_id = next(iter(bridge._clients))
            client = bridge._clients[client_id]
            self.assertIsNone(bridge._interactive_owner)
            self.assertEqual(bridge._pending_interactive_owner, f"human:{client_id}")

        transmitted: list[bytes] = []

        def record_tx(payload: bytes, **_kwargs: Any) -> None:
            transmitted.append(bytes(payload))

        with mock.patch.object(bridge, "send_bytes", side_effect=record_tx):
            bridge._handle_console_rx(client, b"tcp-input")
            bridge._handle_console_rx(client, b"\n")

        self.assertEqual(transmitted, [])
        self.assertEqual(lines, [(client_id, "tcp-input")])
        bridge._set_interactive_admission(True)
        with bridge._state_lock:
            self.assertEqual(bridge._interactive_owner, f"human:{client_id}")

        # TCP bridge 已有 human owner、但尚未有 SessionManager lease 時，後續
        # admission race 仍須沿同一 deferred/replay 路徑，不可遺失輸入。
        written: list[bytes] = []
        bridge._serial_fd = 123456
        try:
            with mock.patch.object(
                bridge, "_write_all", side_effect=lambda _fd, payload: written.append(payload)
            ):
                bridge._set_interactive_admission(False)
                bridge._handle_console_rx(client, b"tcp-deferred")
                self.assertEqual(written, [])
                for source, payload in bridge._set_interactive_admission(True):
                    bridge.send_bytes(payload, source=source, cmd_id=None)
                self.assertEqual(written, [b"tcp-deferred"])
        finally:
            bridge._serial_fd = None

    def test_raw_write_rechecks_admission_at_actual_write(self) -> None:
        """raw RX 已進 send_bytes 後，admission 關閉仍不得落到實際 UART write。"""
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
        )
        self.addCleanup(bridge.stop)
        attached = bridge.attach_console(label="raw-inflight")
        client_id = attached["client_id"]
        assert client_id is not None
        bridge.set_interactive_owner(f"human:{client_id}")
        client = bridge._clients[client_id]
        reached_send = threading.Event()
        resume_send = threading.Event()
        written: list[bytes] = []
        errors: list[BaseException] = []
        original_send = bridge.send_bytes

        def delayed_send(payload: bytes, **kwargs: Any) -> None:
            reached_send.set()
            self.assertTrue(resume_send.wait(3.0), "raw send barrier 未釋放")
            original_send(payload, **kwargs)

        def receive() -> None:
            try:
                bridge._handle_console_rx(client, b"raw-before-admission")
            except BaseException as error:  # noqa: BLE001 — 測試需回報 worker 例外
                errors.append(error)

        # 只替代最底層 write_all；send_bytes 仍保留 write_lock→state gate 順序。
        bridge._serial_fd = 123456
        try:
            with mock.patch.object(
                bridge, "_write_all", side_effect=lambda _fd, payload: written.append(payload)
            ), mock.patch.object(bridge, "send_bytes", side_effect=delayed_send):
                worker = threading.Thread(target=receive)
                worker.start()
                try:
                    self.assertTrue(reached_send.wait(3.0), "raw RX 未抵達 send_bytes barrier")
                    bridge._set_interactive_admission(False)
                    resume_send.set()
                    self._join(worker)
                    self.assertEqual(errors, [])
                    self.assertEqual(
                        written,
                        [],
                        "admission 關閉後，舊 raw RX 不得延遲寫進操作中途",
                    )
                    for source, payload in bridge._set_interactive_admission(True):
                        bridge.send_bytes(payload, source=source, cmd_id=None)
                    self.assertEqual(
                        written,
                        [b"raw-before-admission"],
                        "gate 開放後 deferred raw bytes 應只回放一次",
                    )
                finally:
                    resume_send.set()
                    self._join(worker)
        finally:
            bridge._serial_fd = None

    def test_operation_finish_replays_raw_bytes_without_manager_lease(self) -> None:
        """bridge 已有 human owner 但 manager 無 lease 時，操作收尾只回放一次 deferred。"""
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
        )
        self.addCleanup(bridge.stop)
        self.session.bridge = bridge  # type: ignore[assignment]
        attached = bridge.attach_console(label="tcp-like-human")
        client_id = attached["client_id"]
        assert client_id is not None
        bridge.set_interactive_owner(f"human:{client_id}")
        client = bridge._clients[client_id]
        bridge._serial_fd = 123456
        written: list[bytes] = []
        replay_lock_free = threading.Event()

        def record_replay(_fd: int, payload: bytes) -> None:
            observed: list[bool] = []
            probe_done = threading.Event()

            def probe_manager_lock() -> None:
                acquired = self.manager._lock.acquire(blocking=False)
                observed.append(acquired)
                if acquired:
                    self.manager._lock.release()
                probe_done.set()

            probe = threading.Thread(target=probe_manager_lock)
            probe.start()
            self.assertTrue(probe_done.wait(1.0), "replay lock probe 未完成")
            probe.join(1.0)
            if observed == [True]:
                replay_lock_free.set()
            written.append(payload)

        def push_with_human_input(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            bridge._handle_console_rx(client, b"operation-deferred")
            self.assertEqual(written, [])
            return {"ok": True}

        try:
            with mock.patch.object(
                bridge, "_write_all", side_effect=record_replay
            ), mock.patch("sw_core.file_transfer.push_file", side_effect=push_with_human_input):
                result = self.manager.file_push(
                    "COM0", local_path="/tmp/fake", remote_path="/tmp/fake"
                )
        finally:
            bridge._serial_fd = None

        self.assertTrue(result["ok"])
        self.assertEqual(written, [b"operation-deferred"])
        self.assertTrue(replay_lock_free.is_set(), "deferred replay 不得持有 manager lock")
        self.assertFalse(self.session.foreground_busy)

    def test_secondary_console_raw_bytes_use_line_broker_without_uart_tx(self) -> None:
        """secondary console 的 raw bytes 不直送，newline 才交給 line broker。"""
        lines: list[tuple[str, str]] = []
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
            on_console_line=lambda client_id, line: lines.append((client_id, line)),
        )
        self.addCleanup(bridge.stop)
        primary = bridge.attach_console(label="primary")
        secondary = bridge.attach_console(label="secondary")
        primary_id = primary["client_id"]
        secondary_id = secondary["client_id"]
        assert primary_id is not None
        assert secondary_id is not None
        bridge.set_interactive_owner(f"human:{primary_id}")
        with bridge._state_lock:
            secondary_client = bridge._clients[secondary_id]

        tx: list[bytes] = []

        def record_tx(
            payload: bytes,
            *,
            source: str,
            cmd_id: str | None = None,
            **_kwargs: Any,
        ) -> None:
            del source, cmd_id
            tx.append(bytes(payload))

        with mock.patch.object(bridge, "send_bytes", side_effect=record_tx):
            bridge._handle_console_rx(secondary_client, b"raw-secondary")
            self.assertEqual(tx, [], "secondary raw bytes 不得繞過 line broker 直送 UART")
            self.assertEqual(lines, [])

            bridge._handle_console_rx(secondary_client, b"\n")

        self.assertEqual(tx, [], "secondary newline 也不應由 console raw 路徑 TX")
        self.assertEqual(lines, [(secondary_id, "raw-secondary")])

    def test_human_peer_grace_rejects_old_id_and_allows_new_client(self) -> None:
        """human peer grace 內保留，逾時後舊 ID 失效且新 client 可取得 lease。"""
        from sw_core.constants import _HUMAN_PEER_GRACE_S

        opened = self.manager.interactive_open("COM0", owner="human:old")
        self.assertTrue(opened["ok"])
        old_id = opened["interactive_id"]
        self.bridge.external_peers = set()

        with self.manager._lock:
            grace_lease, _post = self.manager._refresh_interactive_locked(self.session)
        self.assertIsNotNone(grace_lease)
        self.assertIsNotNone(grace_lease.peer_lost_at)
        self.assertEqual(self.session.interactive_session_id, old_id)

        grace_lease.peer_lost_at -= _HUMAN_PEER_GRACE_S + 1.0
        with self.manager._lock:
            expired_lease, _post = self.manager._refresh_interactive_locked(self.session)
        self.assertIsNone(expired_lease)
        self.assertIsNone(self.session.interactive_session_id)

        old_send = self.manager.interactive_send(old_id, data="stale")
        self.assertFalse(old_send["ok"])
        self.assertEqual(old_send["error_code"], "INTERACTIVE_NOT_FOUND")
        self.assertEqual(self.bridge.tx, [])

        self.bridge.external_peers = {"new"}
        replacement = self.manager.interactive_open("COM0", owner="human:new")
        self.assertTrue(replacement["ok"])
        self.assertNotEqual(replacement["interactive_id"], old_id)
        self.assertTrue(self.manager.interactive_close(replacement["interactive_id"])["ok"])

    def test_blocking_pull_rejects_competing_foreground_command_without_tx(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_pull(bridge: RecordingBridge, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            bridge.send_command("pull-start", source=kwargs["source"])
            started.set()
            self.assertTrue(release.wait(5.0))
            bridge.send_command("pull-end", source=kwargs["source"])
            return {"ok": True}

        with mock.patch("sw_core.file_transfer.pull_file", side_effect=blocking_pull):
            first_result: list[dict[str, Any]] = []
            first = threading.Thread(
                target=lambda: first_result.append(
                    self.manager.file_pull(
                        "COM0", remote_path="/tmp/remote", local_path="/tmp/local"
                    )
                )
            )
            first.start()
            self.assertTrue(started.wait(2.0), "第一個 pull 未抵達 bridge barrier")
            writes_before = len(self.bridge.tx)

            loser = self.manager.execute_command(
                "p:COM0", "echo loser", "agent:b", "cmd-b"
            )
            try:
                self.assertFalse(loser["ok"])
                self.assertEqual(loser["error_code"], "SESSION_BUSY")
                self.assertEqual(len(self.bridge.tx), writes_before)
            finally:
                release.set()
                self._join(first)

        self.assertEqual(first_result, [{"ok": True}])
        self.assertFalse(self.session.foreground_busy)

    def test_foreground_busy_rejects_interactive_open_and_send(self) -> None:
        lease_result = self.manager.interactive_open("COM0", owner="human:primary")
        self.assertTrue(lease_result["ok"])
        interactive_id = lease_result["interactive_id"]
        started = threading.Event()
        release = threading.Event()

        def blocking_push(bridge: RecordingBridge, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            bridge.send_command("push-start", source=kwargs["source"])
            started.set()
            self.assertTrue(release.wait(5.0))
            return {"ok": True}

        with mock.patch("sw_core.file_transfer.push_file", side_effect=blocking_push):
            first_result: list[dict[str, Any]] = []
            first = threading.Thread(
                target=lambda: first_result.append(
                    self.manager.file_push("COM0", local_path="/tmp/src", remote_path="/tmp/dst")
                )
            )
            first.start()
            self.assertTrue(started.wait(2.0), "push 未抵達 barrier")
            writes_before = len(self.bridge.tx)

            opened = self.manager.interactive_open("COM0", owner="agent:other")
            self.assertFalse(opened["ok"])
            self.assertEqual(opened["error_code"], "SESSION_BUSY")
            interactive_command = self.manager.execute_command(
                "p:COM0", "echo interactive", "agent:other", "cmd-interactive", mode="interactive"
            )
            self.assertFalse(interactive_command["ok"])
            self.assertEqual(interactive_command["error_code"], "SESSION_BUSY")
            sent = self.manager.interactive_send(interactive_id, data="while-busy")
            self.assertFalse(sent["ok"])
            self.assertEqual(sent["error_code"], "SESSION_BUSY")
            self.assertEqual(len(self.bridge.tx), writes_before)

            release.set()
            self._join(first)

        self.assertEqual(first_result, [{"ok": True}])
        self.assertFalse(self.session.foreground_busy)
        self.assertTrue(self.manager.interactive_close(interactive_id)["ok"])

    def test_interactive_command_keeps_agent_owner_after_operation(self) -> None:
        source = "agent:interactive"
        result = self.manager.execute_command(
            "p:COM0", "", source, "cmd-interactive-owner", mode="interactive"
        )

        self.assertTrue(result["ok"])
        self.assertFalse(self.session.foreground_busy)
        self.assertEqual(self.bridge.interactive_owner, source)
        self.assertEqual(result["interactive_session_id"], self.session.interactive_session_id)
        self.assertTrue(self.manager.interactive_close(result["interactive_session_id"])["ok"])

    def test_interactive_command_replaces_existing_human_bridge_owner(self) -> None:
        bridge = UARTBridge(
            "COM0",
            "/dev/serial/by-id/fake",
            UartProfile(),
            WalWriter(wal_dir=self._tmp.name),
        )
        self.addCleanup(bridge.stop)
        self.session.bridge = bridge  # type: ignore[assignment]
        # 真 bridge 的 raw owner 由 interactive_open 建立；fake external-peer 判定
        # 只避免此未啟動 bridge 的 PTY 被當成 stale，並不替代 owner/admission 邏輯。
        with mock.patch.object(bridge, "console_has_external_peer", return_value=True):
            human = self.manager.interactive_open("COM0", owner="human:existing")
            self.assertTrue(human["ok"])
            human_id = human["interactive_id"]
            source = "agent:interactive"

            result = self.manager.execute_command(
                "p:COM0", "", source, "cmd-interactive-existing-human", mode="interactive"
            )

            self.assertTrue(result["ok"])
            with bridge._state_lock:
                self.assertEqual(bridge._interactive_owner, source)
            self.assertEqual(result["interactive_session_id"], self.session.interactive_session_id)
            self.assertTrue(self.manager.interactive_close(result["interactive_session_id"])["ok"])
            # execute interactive 的既有語意保留：舊 human lease 仍可由其 id 清理，
            # 不得因 foreground admission gate 把 agent owner 排入 pending 而遺失。
            self.assertTrue(self.manager.interactive_close(human_id)["ok"])

    def test_resume_exception_still_releases_operation_token(self) -> None:
        lease_result = self.manager.interactive_open("COM0", owner="human:primary")
        self.assertTrue(lease_result["ok"])
        self.bridge.raise_on_resume = True

        with mock.patch("sw_core.file_transfer.push_file", return_value={"ok": True}):
            with self.assertRaisesRegex(RuntimeError, "resume failure"):
                self.manager.file_push("COM0", local_path="/tmp/src", remote_path="/tmp/dst")

        self.assertFalse(self.session.foreground_busy)
        self.assertIsNone(self.session._foreground_operation)

    def test_post_close_exception_still_releases_operation_token(self) -> None:
        with mock.patch.object(
            sm_mod._PostCloseAction,
            "execute",
            side_effect=RuntimeError("post-close failure"),
        ), mock.patch("sw_core.file_transfer.push_file", return_value={"ok": True}) as push:
            with self.assertRaisesRegex(RuntimeError, "post-close failure"):
                self.manager.file_push("COM0", local_path="/tmp/src", remote_path="/tmp/dst")

        push.assert_not_called()
        self.assertFalse(self.session.foreground_busy)
        self.assertIsNone(self.session._foreground_operation)

    def test_old_operation_cannot_clear_new_epoch_busy(self) -> None:
        old_started = threading.Event()
        old_release = threading.Event()
        new_started = threading.Event()
        new_release = threading.Event()
        old_bridge = self.bridge
        new_bridge = RecordingBridge("new", block_tx=False)

        def transfer(bridge: RecordingBridge, *_args: Any, **kwargs: Any) -> dict[str, Any]:
            bridge.send_command(f"{bridge.name}-start", source=kwargs["source"])
            if bridge is old_bridge:
                old_started.set()
                self.assertTrue(old_release.wait(5.0))
            else:
                new_started.set()
                self.assertTrue(new_release.wait(5.0))
            bridge.send_command(f"{bridge.name}-end", source=kwargs["source"])
            return {"ok": True}

        with mock.patch("sw_core.file_transfer.push_file", side_effect=transfer):
            old_result: list[dict[str, Any]] = []
            old_thread = threading.Thread(
                target=lambda: old_result.append(
                    self.manager.file_push("COM0", local_path="/tmp/a", remote_path="/tmp/a")
                )
            )
            old_thread.start()
            self.assertTrue(old_started.wait(2.0), "舊 epoch 未抵達 barrier")

            # 用 manager 的 detach/re-register 邊界取代任意 sleep：舊 callback 仍在
            # bridge 外執行，新 epoch 取得全新的 bridge 與 operation token。
            with self.manager._lock:
                self.manager._detach_session_locked(self.session, reason="TEST_REBIND")
                self.session.bridge = new_bridge  # type: ignore[assignment]
                self.session.state = "READY"
                self.session.attached_real_path = "/dev/ttyFAKE1"
                self.session.bridge_generation += 1

            new_result: list[dict[str, Any]] = []
            new_thread = threading.Thread(
                target=lambda: new_result.append(
                    self.manager.file_push("COM0", local_path="/tmp/b", remote_path="/tmp/b")
                )
            )
            new_thread.start()
            self.assertTrue(new_started.wait(2.0), "新 epoch 未抵達 barrier")
            self.assertTrue(self.session.foreground_busy)

            old_release.set()
            self._join(old_thread)
            busy_after_old = self.session.foreground_busy
            self.assertEqual(new_bridge.tx, [b"new-start\n"])

            new_release.set()
            self._join(new_thread)
            self.assertTrue(
                busy_after_old,
                "舊 callback 的 finally 不得清掉新 epoch 的 busy",
            )

        self.assertEqual(old_result, [{"ok": True}])
        self.assertEqual(new_result, [{"ok": True}])
        self.assertFalse(self.session.foreground_busy)
        self.assertEqual(new_bridge.tx, [b"new-start\n", b"new-end\n"])

    def test_unknown_closed_expired_and_replaced_interactive_ids_do_not_tx(self) -> None:
        unknown = self.manager.interactive_send("unknown", data="x")
        self.assertEqual(unknown["error_code"], "INTERACTIVE_NOT_FOUND")
        self.assertEqual(self.bridge.tx, [])

        opened = self.manager.interactive_open("COM0", owner="agent:a")
        self.assertTrue(opened["ok"])
        closed_id = opened["interactive_id"]
        self.assertTrue(self.manager.interactive_close(closed_id)["ok"])
        closed = self.manager.interactive_send(closed_id, data="closed")
        self.assertEqual(closed["error_code"], "INTERACTIVE_NOT_FOUND")
        self.assertEqual(self.bridge.tx, [])

        expired_open = self.manager.interactive_open("COM0", owner="agent:b")
        expired_id = expired_open["interactive_id"]
        lease = self.manager._interactive[expired_id]
        lease.last_activity_at = 0.0
        expired = self.manager.interactive_send(expired_id, data="expired")
        self.assertEqual(expired["error_code"], "INTERACTIVE_EXPIRED")
        self.assertEqual(self.bridge.tx, [])

        replacement = self.manager.interactive_open("COM0", owner="agent:c")
        replacement_id = replacement["interactive_id"]
        self.assertNotEqual(replacement_id, expired_id)
        replaced = self.manager.interactive_send(expired_id, data="replaced")
        self.assertEqual(replaced["error_code"], "INTERACTIVE_NOT_FOUND")
        self.assertEqual(self.bridge.tx, [])
        self.assertTrue(self.manager.interactive_close(replacement_id)["ok"])


if __name__ == "__main__":
    unittest.main()
