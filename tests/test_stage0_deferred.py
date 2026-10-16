"""tests/test_stage0_deferred.py — P1 Stage 0 T09：BC-02 DeferredBuffer 整段清空與上下文標記。

A1 紅燈測試，斷言依 docs/rust-refactor/plans/02-stage0-python-bc.md §Task T09 原文。
- 超過 DEFERRED_INPUT_MAX_BYTES 時整段清空（不截頭留尾）。
- Reboot／Quiet 重新啟動、Recovery、Release、Flash 失效時丟棄整段舊輸入，且舊 owner_gen 項目不回放。
- 未發生失效時（#53 Soft Preempt）回放語意不變。

替身與 bridge／manager 建構為本檔自備（對應 T01 `tests/stage0_fakes.py` 的 `FakePort`、`make_bridge`、
`make_manager`、`make_profile`；T01 落地後可改為 import 該模組，斷言不變）。
依 Global Constraints，本 Phase 新增的名稱（`owner_gen`、`invalidate_deferred`、`uio._DeferredEntry`、
`captured_owner_gen`、`begin_drain`）一律於測試函式內存取，使 collection 不因實作尚未存在而失敗。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import sw_core.uart_io as uio
from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager, SessionRuntime
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


class FakePort:
    """SerialPort 介面替身（fileno() 為 None → UARTBridge 走 port.write 路徑）。

    writes 逐次記錄寫出的 payload；其餘方法僅滿足介面，不產生 I/O。
    """

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.close_calls = 0
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def configure(self, profile: Any) -> None:
        return None

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def write(self, payload: bytes, *, cancel: threading.Event | None = None) -> None:
        self.writes.append(bytes(payload))

    def close(self) -> None:
        self.close_calls += 1

    def set_baud(self, baud: int) -> None:
        return None

    def is_alive(self) -> bool:
        return True

    def fileno(self) -> int | None:
        return None


def _make_bridge(tmp_path: Path, port: FakePort | None = None) -> tuple[uio.UARTBridge, WalWriter]:
    """建 UARTBridge（不呼叫 start），有 port 時直接以替身取代序列埠（比照 T01 make_bridge）。"""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    wal = WalWriter(wal_dir=str(wal_dir))
    bridge = uio.UARTBridge("COM0", device_path="fake", profile=UartProfile(), wal=wal)
    if port is not None:
        bridge._serial = port  # type: ignore[assignment]
        bridge._serial_fd = None
    return bridge, wal


def _client(cid: str = "cid1") -> uio.ConsoleClient:
    return uio.ConsoleClient(client_id=cid, label="x", master_fd=-1, slave_fd=-1,
                             slave_path="", attached_at=0.0)


def _suspended_bridge(tmp_path: Path, port: FakePort) -> tuple[uio.UARTBridge, uio.ConsoleClient]:
    """human 持有 raw ownership 後進入 agent 命令的 suspend 狀態（human 輸入改進 deferred）。"""
    b, _ = _make_bridge(tmp_path, port)
    c = _client()
    b._clients = {c.client_id: c}
    b._interactive_owner = f"human:{c.client_id}"
    b.suspend_interactive()
    return b, c


def _profile(name: str = "s", com: str = "COM0") -> SessionProfile:
    return SessionProfile(
        profile_name=name, platform="prpl", com=com, act_no=0, alias=name,
        device_by_id="/dev/serial/by-id/X", prompt_regex=r"[$#] $",
        login_regex=r"(?mi)login:\s*$", password_regex=r"(?mi)password:\s*$",
        user_env="U", pass_env="P", ready_probe="echo __READY__${nonce}", timeout_s=5.0,
    )


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(
        [], WalWriter(wal_dir=str(tmp_path / "wal")),
        on_ready=lambda _s: None, on_detached=lambda _s: None,
        state_path=str(tmp_path / "state.json"),
    )


def _attach_session(mgr: SessionManager, bridge: uio.UARTBridge, state: str) -> SessionRuntime:
    """把 bridge 掛到 mgr 的 session "s"（直接寫入 _sessions，不走 attach 流程）。"""
    sess = SessionRuntime(session_id="s", profile=_profile())
    sess.bridge = bridge
    sess.state = state
    mgr._sessions["s"] = sess
    return sess


def _record_bridge_calls(bridge: uio.UARTBridge, monkeypatch: pytest.MonkeyPatch,
                         events: list[tuple[str, Any]]) -> None:
    """記錄 send_bytes（實際送 UART 的位元組）與 invalidate_deferred 的呼叫順序。"""
    orig_send = bridge.send_bytes

    def _send(payload: bytes, **kwargs: Any) -> None:
        events.append(("send", bytes(payload)))
        return orig_send(payload, **kwargs)

    def _invalidate(reason: str) -> int:
        events.append(("invalidate", reason))
        return 0

    monkeypatch.setattr(bridge, "send_bytes", _send)
    monkeypatch.setattr(bridge, "invalidate_deferred", _invalidate, raising=False)


# ── BC-02 核心行為（bridge 層）─────────────────────────────────────────────

def test_soft_preempt_replay_unchanged_without_invalidation(tmp_path):
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
    b.resume_interactive()
    assert port.writes == [b"ls"]


def test_boot_quiet_invalidation_drops_deferred(tmp_path):
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
    assert b.invalidate_deferred("boot_quiet") == 2
    b.resume_interactive()
    assert port.writes == []


def test_stale_mailbox_bytes_dropped_after_invalidation(tmp_path):
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    captured = b.owner_gen
    b.invalidate_deferred("reboot")
    b._handle_console_rx(c, b"rm -rf x", captured_owner_gen=captured)
    assert c.client_id not in b._deferred_buffers
    b.resume_interactive()
    assert port.writes == []


def test_overflow_clears_and_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(uio, "DEFERRED_INPUT_MAX_BYTES", 8)
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    with caplog.at_level(logging.WARNING, logger="serialwrap"):
        b._handle_console_rx(c, b"123456789", captured_owner_gen=b.owner_gen)
    assert c.client_id not in b._deferred_buffers
    assert "BC-02" in caplog.text


def test_flash_probe_miss_keeps_deferred_context(tmp_path):
    # service._BridgeProbe 在每個候選 probe 起點 set_flash_mode(True)、未命中即解除：上下文不得失效
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    gen = b.owner_gen
    b._handle_console_rx(c, b"ls", captured_owner_gen=gen)
    b.set_flash_mode(True)
    b.set_flash_mode(False)
    assert b.owner_gen == gen
    b.resume_interactive()
    assert port.writes == [b"ls"]


def test_drain_invalidates_deferred(tmp_path):
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
    b.begin_drain(1, preserve_consoles=True)
    assert b._deferred_buffers == {}


# ── BC-02 補充（bridge 層：admission、prune、raw 上下文）─────────────────────

def test_admission_gate_replay_skips_old_owner_gen(tmp_path):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    c = _client()
    b._clients = {c.client_id: c}
    b._interactive_owner = f"human:{c.client_id}"
    b._set_interactive_admission(False)  # admission 關閉：human 輸入先排入 deferred
    gen = b.owner_gen
    b._handle_console_rx(c, b"ls", captured_owner_gen=gen)
    assert "cid1" in b._deferred_buffers  # 前提：確實已累積一筆 entry
    assert b.invalidate_deferred("reboot") == 2
    assert b._set_interactive_admission(True) == []
    assert port.writes == []


def test_prune_stale_console_drops_its_deferred(tmp_path):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    c = _client()
    b._clients = {c.client_id: c}
    b._primary_client_id = None
    b._enumerate_all_held_paths = lambda: set()  # 無外部 peer → 視為 stale
    b._deferred_buffers = {c.client_id: bytearray(b"ls")}
    with b._state_lock:
        pruned = b._prune_stale_consoles_locked(now=c.attached_at + uio._STALE_CONSOLE_GRACE_S + 1.0)
    assert [x.client_id for x in pruned] == ["cid1"]
    assert "cid1" not in b._deferred_buffers


def test_send_bytes_raw_context_deferred_uses_entry(tmp_path):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    b._interactive_admission_blocked = True
    raw = b._console_raw_context
    raw.token = b._console_raw_token
    raw.client_id = "cid1"
    try:
        b.send_bytes(b"ls", source="human:cid1")
    finally:
        for attr in ("token", "client_id"):
            try:
                delattr(raw, attr)
            except AttributeError:
                pass
    entry = b._deferred_buffers["cid1"]
    assert isinstance(entry, uio._DeferredEntry)
    assert entry.owner_gen == b.owner_gen
    assert bytes(entry) == b"ls"
    assert port.writes == []


def test_session_arm_boot_quiet_invalidates_bridge(tmp_path):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    sess = SessionRuntime(session_id="s", profile=_profile())
    sess.bridge = b
    gen = b.owner_gen
    sess.arm_boot_quiet()
    assert b.owner_gen == gen + 1


def test_session_arm_boot_quiet_tolerates_none_or_mock_bridge():
    sess = SessionRuntime(session_id="s", profile=_profile())
    sess.bridge = None
    sess.arm_boot_quiet()
    assert sess.boot_quiet_active()
    sess.bridge = mock.MagicMock()
    sess.arm_boot_quiet()
    assert sess.boot_quiet_active()


# ── BC-02 失效呼叫點（session／manager 層）──────────────────────────────────

def test_reboot_command_entering_recovering_invalidates(tmp_path, monkeypatch):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    mgr = _manager(tmp_path)
    sess = _attach_session(mgr, b, state="READY")
    monkeypatch.setattr(mgr, "_spawn_reboot_recovery", lambda *_a, **_k: None)
    calls: list[tuple[str, str]] = []

    def _spy(reason: str) -> int:
        calls.append((reason, sess.state))  # 記錄呼叫當下的 session 狀態
        return 0

    monkeypatch.setattr(b, "invalidate_deferred", _spy, raising=False)
    mgr._handle_reboot_command(sess, b, command="reboot", source="agent", cmd_id="c1",
                               timeout_s=0.05, execution_mode="foreground")
    reboot_states = [state for reason, state in calls if reason == "reboot"]
    assert reboot_states == ["READY"]  # 必須在進入 RECOVERING 之前呼叫
    assert sess.state == "RECOVERING"


def test_enter_flashing_invalidates(tmp_path):
    port = FakePort()
    b, c = _suspended_bridge(tmp_path, port)
    mgr = _manager(tmp_path)
    _attach_session(mgr, b, state="READY")
    b._handle_console_rx(c, b"ls", captured_owner_gen=b.owner_gen)
    gen = b.owner_gen
    out = mgr.enter_flashing("s")
    assert out["ok"] is True
    assert b.owner_gen == gen + 1
    assert b._deferred_buffers == {}
    b.resume_interactive()
    assert port.writes == []


def test_recover_session_invalidates(tmp_path, monkeypatch):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    mgr = _manager(tmp_path)
    _attach_session(mgr, b, state="READY")
    # 失敗路徑會升級到 _force_recover（含 sleep）；本測試只驗入口失效與按鍵順序，故以樁取代。
    monkeypatch.setattr(mgr, "_force_recover", lambda _s: {"ok": False, "error_code": "SESSION_NOT_READY"})
    events: list[tuple[str, Any]] = []
    _record_bridge_calls(b, monkeypatch, events)
    mgr.recover_session("s", timeout_s=0.05)
    assert events and events[0] == ("invalidate", "recover")


def test_recover_after_failure_invalidates_before_keys(tmp_path, monkeypatch):
    port = FakePort()
    b, _ = _make_bridge(tmp_path, port)
    mgr = _manager(tmp_path)
    sess = _attach_session(mgr, b, state="READY")
    events: list[tuple[str, Any]] = []
    _record_bridge_calls(b, monkeypatch, events)
    mgr._recover_after_failure(sess, b, cmd_id="c1", timeout_s=0.05, source="agent",
                               command="x", prompt_regex=r"[$#] $", pre_offset=0)
    invalidate_idx = [i for i, e in enumerate(events) if e == ("invalidate", "recover")]
    send_idx = [i for i, e in enumerate(events) if e[0] == "send"]
    assert invalidate_idx and send_idx
    assert invalidate_idx[0] < send_idx[0]  # CTRL_C 寫出前必須已失效
