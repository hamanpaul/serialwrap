"""P1 T20：OS write 前授權 permit 零 TX 失敗測試（Stage A1，TDD 紅燈）。

對應計畫：docs/rust-refactor/plans/02-stage0-python-bc.md §Task T20。
測試對象（尚未實作，全部於測試函式內延遲解析）：`TxPermit`、`UARTBridge._submit_guard`、
`_tx_fence_lock`、`system_permit()`、`set_probe_gate()`、`set_tx_gate()`、`revoke_ops()`、
`SessionManager._begin_login_run_locked()`、`_self_test_permit_locked()`、
`_expire_interactive_leases_tick()`、`DRAIN_FENCE_ACQUIRE_S`。

規則：
- 跨 thread 情境一律以 threading.Event 同步，每個等待帶 2 s 上限（`_join`／`_wait_until`）。
- seam（`_after_tx_authorize`／`_inside_tx_submit`）以 `_Pause` 停住 writer thread。
- 「零 TX」以 `port.writes` 與 `dir == "TX"` 的 WAL 記錄同時斷言。
"""

from __future__ import annotations

import ast
import base64
import contextlib
import errno
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import sw_core.uart_io as uio
from stage0_fakes import FakePort, ScriptedWrite, make_bridge, make_manager, make_profile, wal_records

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/X"
ECHO = b"echo hi\n"  # 8 bytes
BANNER = b"U-Boot 2024.04 (Oct 01 2024)\r\n"
REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "contracts" / "golden" / "selftest_refused.json"


# ---------------------------------------------------------------------------
# 共用輔助（延遲 import 新名稱；00 Global Constraints）
# ---------------------------------------------------------------------------


def _load(module: str, name: str) -> Any:
    import importlib

    return getattr(importlib.import_module(module), name)


def _tx_result(outcome: str, confirmed: int, requested: int, error: str | None = None) -> Any:
    return _load("sw_core.serial_port", "TxResult")(outcome, confirmed, requested, error)


def _incomplete_error() -> type:
    return _load("sw_core.uart_io", "TxIncompleteError")


def _permit(
    op_id: str,
    generation: int,
    tx_class: str,
    *,
    owner_gen: int | None = None,
    login_gen: int | None = None,
) -> Any:
    return _load("sw_core.uart_io", "TxPermit")(
        op_id=op_id,
        bridge_generation=generation,
        tx_class=tx_class,
        owner_gen=owner_gen,
        login_gen=login_gen,
    )


def _tx_records(wal: Any) -> list[dict[str, Any]]:
    return [r for r in wal_records(wal) if r.get("dir") == "TX"]


def _spawn(fn: Any, *args: Any, **kwargs: Any) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["ret"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — 測試需保留 writer 端的例外
            box["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _join(t: threading.Thread, box: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    t.join(timeout)
    assert not t.is_alive(), "執行緒未在時限內返回（疑似死鎖或仍被 seam 阻擋）"
    return box


def _outcome(box: dict[str, Any]) -> Any:
    """取得 TX 結果：拋出 TxIncompleteError 或正常返回兩種路徑都取出 TxResult。"""
    if "exc" in box:
        assert isinstance(box["exc"], _incomplete_error()), repr(box["exc"])
        return box["exc"].result
    return box["ret"]


def _wait_until(pred: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("條件未在時限內成立")


class _Pause:
    """於 bridge 的 seam 第 nth 次呼叫時暫停 writer thread，直到測試呼叫 resume()。"""

    def __init__(self, bridge: Any, seam: str = "_after_tx_authorize", *, nth: int = 1) -> None:
        self.reached = threading.Event()
        self._go = threading.Event()
        self._nth = nth
        self._count = 0
        self._mu = threading.Lock()
        setattr(bridge, seam, self._hook)

    def _hook(self) -> None:
        with self._mu:
            self._count += 1
            hit = self._count == self._nth
        if hit:
            self.reached.set()
            self._go.wait(2.0)

    def wait_reached(self) -> None:
        assert self.reached.wait(2.0), "writer 未抵達 seam"

    def resume(self) -> None:
        self._go.set()


def _ready(
    tmp_path: Path,
    port: Any = None,
    *,
    profile: Any = None,
    generation: int = 4,
    state: str = "READY",
    fd: int | None = None,
) -> tuple[Any, Any, Any]:
    """建立已指派 bridge 的 session（同 T08 `_ready_with_port`），bridge 掛上 manager 的 RX 回呼，
    並以 set_tx_gate 設定 attach 成功時的初始 gate。"""
    mgr = make_manager(tmp_path, [profile or make_profile(by_id=BY_ID)])
    s = mgr.get_session("COM0")
    assert s is not None
    from sw_core.device_watcher import DeviceInfo

    b, _wal = make_bridge(
        tmp_path,
        port=port,
        fd=fd,
        wal=mgr._wal,
        on_rx_data=lambda data, wal_seq=None, sid=s.session_id: mgr._on_bridge_rx(
            sid, data, wal_seq
        ),
    )
    # 這個 helper 代表已成功 attach 的 session。原替身未啟動 bridge、未登記
    # discovery device，且直接呼叫 manager RX observer，會讓互動／self-test
    # 在抵達 T20 提交點前就被錯誤拒絕或等不到 RX。以記憶體狀態補足 attach 成功條件。
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="fake-uart")
    manager_rx = b._on_rx_data

    def inject_rx(data: bytes, wal_seq: int | None = None) -> None:
        b._append_rx_text(data)
        if manager_rx is not None:
            manager_rx(data, wal_seq=wal_seq)

    b._on_rx_data = inject_rx
    real_snapshot = b.snapshot

    def snapshot_as_attached() -> dict[str, Any]:
        snapshot = real_snapshot()
        snapshot.update(running=True, serial_alive=True, vtty_alive=True, vtty="fake-vtty")
        return snapshot

    b.snapshot = snapshot_as_attached
    with mgr._lock:
        s.bridge = b
        s.state = state
        s.bridge_generation = generation
    # set_tx_gate 為 T20 B1 新增：紅燈階段不存在時略過，讓各測試於自身首個缺口（TxPermit 等）失敗，
    # 不被共用 helper 遮蔽；綠燈階段 attach 路徑本身會設定 gate，且下列斷言驗證其行為。
    setter = getattr(b, "set_tx_gate", None)
    if setter is not None:
        setter(generation=generation, quiet=False, lease_owner=None)
    return mgr, s, b


def _quiet_bridge(tmp_path: Path, port: Any, *, profile: Any = None) -> tuple[Any, Any, Any]:
    """ATTACHED session，RX 見到 autoboot 倒數行而 arm quiet（#114 救援情境）。"""
    mgr, s, b = _ready(tmp_path, port, profile=profile, state="ATTACHED")
    b._on_rx_data(b"Hit any key to stop autoboot:  3\r\n")
    assert s.boot_quiet_active() is True
    assert b._gate_quiet is True
    return mgr, s, b


def _rescue_lease(mgr: Any, *, owner: str = "agent:rescue", timeout_s: float = 30.0) -> dict[str, Any]:
    r = mgr.interactive_open("COM0", owner=owner, timeout_s=timeout_s, allow_attached=True)
    assert r["ok"] is True, r
    return r


class _BridgeSpy:
    def __init__(self) -> None:
        self.permits: list[Any] = []  # system_permit() 進入時的 permit
        self.sends: list[tuple[Any, Any, int]] = []  # (source, permit 關鍵字, 呼叫當下 system_permit 深度)
        self.depth = 0


def _spy_bridge(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> _BridgeSpy:
    spy = _BridgeSpy()
    orig_sp = bridge.system_permit
    orig_send = bridge.send_bytes

    @contextlib.contextmanager
    def sp(permit: Any):
        spy.permits.append(permit)
        spy.depth += 1
        try:
            with orig_sp(permit):
                yield
        finally:
            spy.depth -= 1

    def send(
        payload: bytes,
        *,
        source: str,
        cmd_id: str | None = None,
        permit: Any = None,
        **kwargs: Any,
    ) -> Any:
        spy.sends.append((source, permit, spy.depth))
        return orig_send(
            payload, source=source, cmd_id=cmd_id, permit=permit, **kwargs
        )

    monkeypatch.setattr(bridge, "system_permit", sp)
    monkeypatch.setattr(bridge, "send_bytes", send)
    return spy


def _record_guard(bridge: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    records: list[tuple[Any, Any]] = []
    orig = bridge._submit_guard

    @contextlib.contextmanager
    def spy(permit: Any, payload_len: int):
        with orig(permit, payload_len) as reason:
            records.append((permit, reason))
            yield reason

    monkeypatch.setattr(bridge, "_submit_guard", spy)
    return records


def _always_eagain(fd: int, data: Any) -> int:
    raise BlockingIOError(errno.EAGAIN, "again")


def _login_profile(**extra: Any) -> Any:
    return make_profile(
        by_id=BY_ID,
        user_env="SW_T20_USER",
        pass_env="SW_T20_PASS",
        login_regex=r"(?mi)^login:\s*$",
        password_regex=r"(?mi)^password:\s*$",
        timeout_s=0.1,
        **extra,
    )


def _login_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SW_T20_USER", "root")
    monkeypatch.setenv("SW_T20_PASS", "s3cret")


def _begin_login(mgr: Any, s: Any) -> Any:
    with mgr._lock:
        return mgr._begin_login_run_locked(s)


def _send_system(bridge: Any, permit: Any, cmd: str = "") -> None:
    """模擬 login FSM：在 system_permit 內以 source="system" 送出（空字串即 prompt probe `\\n`）。"""
    with bridge.system_permit(permit):
        bridge.send_command(cmd, source="system")


def _nonce_responder(holder: dict[str, Any]) -> Any:
    """FakePort on_write：收到 `echo __READY__<nonce>` 即於同步 RX 回應（holder["b"] 於建立後填入）。"""

    def on_write(data: bytes) -> None:
        if data.startswith(b"echo __READY__"):
            nonce = data[len(b"echo __READY__") :].strip()
            holder["b"]._on_rx_data(b"__READY__" + nonce + b"\r\nroot@prplOS:/# ")

    return on_write


# ---------------------------------------------------------------------------
# A. 命令出列後、OS write 前的失效（quiet／release／cancel／lease／gate）
# ---------------------------------------------------------------------------


def test_quiet_armed_after_dequeue_before_write_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    b._after_tx_authorize = lambda: s.arm_boot_quiet()  # pre-check 通過後才 arm quiet
    res = mgr.execute_command(s.session_id, "echo hi", "agent:x", "c1", timeout_s=2.0)
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    assert res.get("ok") is False
    assert res.get("error_code") in {"AUTOBOOT_QUIET", "FLUSHED_BY_RECOVERY"}


@pytest.mark.parametrize("outcome,confirmed", [("completed", 2), ("unknown", 0)])
def test_reboot_write_uncertain_arms_quiet_before_return(
    tmp_path: Path, outcome: str, confirmed: int
) -> None:
    requested = len(b"reboot\n")
    port = FakePort(write_results=[_tx_result(outcome, confirmed, requested, "driver_error")])
    mgr, session, bridge = _ready(tmp_path, port)

    with pytest.raises(_incomplete_error()):
        mgr._handle_reboot_command(
            session,
            bridge,
            command="reboot",
            source="agent:test",
            cmd_id="reboot-uncertain",
            timeout_s=0.1,
            execution_mode="line",
        )

    assert session.boot_quiet_active()
    assert bridge._gate_quiet is True


def test_quiet_armed_by_other_thread_after_authorize_before_submit_zero_tx(tmp_path: Path) -> None:
    # R0-F1：舊「驗證→放鎖→write」實作在此窗口會寫出 bytes，本測試必紅。
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    pause = _Pause(b)
    t, box = _spawn(b.send_bytes, ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    pause.wait_reached()
    arm, arm_box = _spawn(s.arm_boot_quiet)
    _join(arm, arm_box, timeout=0.5)  # writer 不持 TX fence 鎖：arm 必須立即返回
    pause.resume()
    _join(t, box)
    assert _outcome(box) == _tx_result("not_submitted", 0, len(ECHO), "quiet")
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_release_by_other_thread_after_authorize_before_submit_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    pause = _Pause(b)
    t, box = _spawn(b.send_bytes, ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    pause.wait_reached()
    b.begin_drain(5, preserve_consoles=False)  # 不等 finish
    pause.resume()
    _join(t, box)
    res = _outcome(box)
    assert res.outcome == "not_submitted"
    assert res.error in ("draining", "cancelled")
    assert res.confirmed == 0
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_interactive_lease_handoff_after_authorize_before_submit_zero_tx(tmp_path: Path) -> None:
    # 注意：session_manager.interactive_send 於 `_lock` 內呼叫 send_bytes（約 4237 行）；
    # 若實作仍持鎖，下列 handoff thread 會阻塞，本測試會於 _join 的 1 s 上限失敗（見報告的計畫衝突項）。
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    a_id = mgr.interactive_open("COM0", owner="agent:A", timeout_s=30.0)["interactive_id"]
    pause = _Pause(b)
    t, box = _spawn(mgr.interactive_send, a_id, data="ctrl-c", encoding="key")
    pause.wait_reached()

    def handoff() -> dict[str, Any]:
        assert mgr.interactive_close(a_id)["ok"] is True
        return mgr.interactive_open("COM0", owner="agent:B", timeout_s=30.0)

    h, hbox = _spawn(handoff)
    _join(h, hbox, timeout=1.0)
    assert hbox["ret"]["ok"] is True
    b_id = hbox["ret"]["interactive_id"]
    pause.resume()
    _join(t, box)
    ret = box["ret"]
    assert ret["ok"] is False  # A 的寫入以 lease 原因被拒
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    assert mgr.interactive_send(b_id, data="ctrl-c", encoding="key")["ok"] is True
    assert port.writes == [b"\x03"]


def test_invalidation_during_submit_waits_and_counts_committed(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    pause = _Pause(b, seam="_inside_tx_submit")
    t, box = _spawn(b.send_bytes, ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    pause.wait_reached()
    arm, arm_box = _spawn(s.arm_boot_quiet)
    arm.join(0.2)
    assert arm.is_alive(), "arm 不得在提交臨界區（持 _tx_fence_lock）內返回"
    assert b._gate_quiet is False
    pause.resume()
    _join(t, box)
    _join(arm, arm_box, timeout=1.0)
    assert _outcome(box) == _tx_result("completed", len(ECHO), len(ECHO))
    assert port.writes == [ECHO]
    tx = _tx_records(mgr._wal)
    assert len(tx) == 1 and tx[0]["len"] == len(ECHO)
    assert b._gate_quiet is True
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(b"echo two\n", source="agent:x", cmd_id="c2", permit=_permit("c2", 4, "agent"))
    assert ei.value.result == _tx_result("not_submitted", 0, len(b"echo two\n"), "quiet")
    assert port.writes == [ECHO]


def test_quiet_between_partial_chunks_stops_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sw = ScriptedWrite([2, BlockingIOError(errno.EAGAIN, "again")])
    monkeypatch.setattr(uio.os, "write", sw)
    mgr, s, b = _ready(tmp_path, fd=99)
    in_backoff = threading.Event()
    resume = threading.Event()

    def fake_wait(timeout: float | None = None) -> bool:
        in_backoff.set()
        resume.wait(2.0)
        return False

    monkeypatch.setattr(b._tx_cancel, "wait", fake_wait)
    t, box = _spawn(b.send_bytes, b"abcdef", source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    assert in_backoff.wait(2.0), "writer 未進入 EAGAIN backoff"
    arm, arm_box = _spawn(s.arm_boot_quiet)
    _join(arm, arm_box, timeout=0.5)  # backoff 期間不得持 TX 鎖
    resume.set()
    _join(t, box)
    assert _outcome(box) == _tx_result("completed", 2, 6, "revoked:quiet")
    assert sw.calls == [b"abcdef", b"cdef"]  # quiet 後尾段 b"cdef" 永不交 OS
    tx = _tx_records(mgr._wal)
    assert len(tx) == 1 and tx[0]["len"] == 2

    # execute_command 路徑：窗口內已確認 2 bytes 後被 quiet 撤銷 → COMMAND_ABORTED_IN_FLIGHT
    sub = tmp_path / "exec"
    sub.mkdir()
    sw2 = ScriptedWrite([2, BlockingIOError(errno.EAGAIN, "again")])
    monkeypatch.setattr(uio.os, "write", sw2)
    mgr2, s2, b2 = _ready(sub, fd=99)
    monkeypatch.setattr(b2._tx_cancel, "wait", lambda timeout=None: (s2.arm_boot_quiet(), False)[1])
    res = mgr2.execute_command(s2.session_id, "echo abcdef", "agent:x", "c2", timeout_s=2.0)
    assert res.get("ok") is False
    assert res.get("error_code") == "COMMAND_ABORTED_IN_FLIGHT"


def test_os_write_always_inside_tx_fence_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    violations: list[str] = []
    calls: list[bytes] = []

    def spy_write(fd: int, data: Any) -> int:
        raw = bytes(data)
        calls.append(raw)
        if not b._tx_fence_lock.locked():
            violations.append("os.write 未持 _tx_fence_lock")
        probe: dict[str, Any] = {}

        def try_acquire() -> None:
            probe["got"] = b._tx_fence_lock.acquire(blocking=False)

        th = threading.Thread(target=try_acquire)
        th.start()
        th.join(1.0)
        if probe.get("got"):
            violations.append("其他 thread 取得 _tx_fence_lock")
            b._tx_fence_lock.release()
        return len(raw)

    monkeypatch.setattr(uio.os, "write", spy_write)
    mgr, s, b = _ready(tmp_path, fd=99)
    b.send_bytes(ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))  # agent 命令
    a_id = mgr.interactive_open("COM0", owner="agent:A", timeout_s=30.0)["interactive_id"]
    mgr.interactive_send(a_id, data="ctrl-c", encoding="key")  # interactive send
    b.send_bytes(b"typed", source="human:c1", permit=_permit("console:c1", 4, "human_raw", owner_gen=b._owner_gen))
    b.send_bytes(b"replayed", source="human:c2", permit=_permit("console:c2", 4, "human_raw", owner_gen=b._owner_gen))
    b.send_secret("s3cret")  # send_secret
    assert violations == []
    assert len(calls) >= 5


def test_tx_fence_lock_not_held_during_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uio.os, "write", _always_eagain)
    mgr, s, b = _ready(tmp_path, fd=99)
    in_backoff = threading.Event()
    resume = threading.Event()

    def fake_wait(timeout: float | None = None) -> bool:
        in_backoff.set()
        resume.wait(2.0)
        return b._tx_cancel.is_set()  # 與 Event.wait 語意相同：被 drain 設定時回 True

    monkeypatch.setattr(b._tx_cancel, "wait", fake_wait)
    t, box = _spawn(b.send_bytes, ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    assert in_backoff.wait(2.0), "writer 未進入 EAGAIN backoff"
    assert b._tx_fence_lock.acquire(timeout=0.1) is True, "EAGAIN backoff 期間不得持 _tx_fence_lock"
    b._tx_fence_lock.release()
    b.begin_drain(5, preserve_consoles=False)
    resume.set()
    _join(t, box)
    assert _outcome(box).error == "cancelled"


def test_windows_guarded_write_stall_reports_drain_stalled_on_time(tmp_path: Path) -> None:
    gate = threading.Event()
    port = FakePort(block=gate, block_in_guard=True)
    mgr, s, b = _ready(tmp_path, port, generation=4)
    t, box = _spawn(b.send_bytes, b"abc", source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    _wait_until(lambda: port.writes == [b"abc"] and b._tx_fence_lock.locked())
    timer = threading.Timer(3.5, gate.set)  # 模擬 Windows 寫入卡住 >3 s
    timer.start()
    probe: dict[str, Any] = {}

    def probe_lock() -> None:
        probe["got"] = mgr._lock.acquire(timeout=0.5)
        probe["at"] = time.monotonic()
        if probe["got"]:
            mgr._lock.release()

    probe_timer = threading.Timer(0.2, probe_lock)
    probe_timer.start()
    t0 = time.monotonic()
    r = mgr.release_device("COM0")
    el = time.monotonic() - t0
    assert {k: r[k] for k in ("ok", "error_code", "drain_stalled", "drain_timeout_epoch")} == {
        "ok": False,
        "error_code": "SESSION_RECOVERING",
        "drain_stalled": True,
        "drain_timeout_epoch": 4,
    }
    assert 2.9 <= el < 3.4, f"drain 期限應自請求起算 3 s，實際 {el:.2f}s（舊實作 ≥ 6.5 s）"
    probe_timer.join(1.0)
    assert probe.get("got") is True
    assert probe["at"] - t0 < 0.5, "manager _lock 不得跨 fence 等待"
    assert port.close_calls == 0
    assert b.drain_pending is True
    assert port.cancel_write_calls == 1
    assert mgr.attach_device("COM0")["error_code"] == "SESSION_RECOVERING"
    with pytest.raises(RuntimeError):
        b.start()  # 回條前不得重開 handle
    _wait_until(lambda: s.state == "RELEASED", timeout=2.0)
    assert port.close_calls == 1
    _join(t, box)
    tx = [x for x in _tx_records(mgr._wal) if x.get("cmd_id") == "c1"]
    assert len(tx) == 1 and tx[0]["len"] == 3  # guard 內已交 OS 的段照實記錄
    timer.join(1.0)


def test_begin_drain_fence_acquire_bounded_and_freezes_tail(tmp_path: Path) -> None:
    fence_s = _load("sw_core.constants", "DRAIN_FENCE_ACQUIRE_S")
    gate = threading.Event()
    port = FakePort(block=gate, block_in_guard=True)
    b, wal = make_bridge(tmp_path, port=port)
    b.set_tx_gate(generation=4, quiet=False, lease_owner=None)
    owner_gen = b.owner_gen
    ta, abox = _spawn(b.send_bytes, b"abc", source="agent:a", cmd_id="a", permit=_permit("a", 4, "agent"))
    _wait_until(lambda: port.writes == [b"abc"])
    tb, bbox = _spawn(b.send_bytes, b"def", source="agent:b", cmd_id="b", permit=_permit("b", 4, "agent"))
    time.sleep(0.05)  # 讓 B 排在 _write_lock 之後
    t0 = time.monotonic()
    b.begin_drain(5, preserve_consoles=False)
    el = time.monotonic() - t0
    assert el < fence_s + 0.1, f"begin_drain 取 fence 鎖必須有上限，實際 {el:.3f}s"
    assert b._tx_fence_lock.locked() is True
    assert b._tx_frozen is True
    assert b._tx_cancel.is_set() is True
    gate.set()
    _join(ta, abox)
    _join(tb, bbox)
    assert _outcome(abox) == _tx_result("completed", 3, 3)
    bres = _outcome(bbox)
    assert bres.outcome == "not_submitted"
    assert bres.error in ("draining", "cancelled")
    _wait_until(lambda: b.owner_gen == owner_gen + 1)
    assert port.writes == [b"abc"]
    assert len(_tx_records(wal)) == 1
    rec = b.finish_drain(1.0)
    assert rec is not None and rec.handle_closed is True


def test_pyserial_chunked_write_drain_stops_after_inflight_chunk(tmp_path: Path) -> None:
    from sw_core.config import UartProfile

    sp_cls = _load("sw_core.serial_port", "_PySerialPort")
    fence_s = _load("sw_core.constants", "DRAIN_FENCE_ACQUIRE_S")
    release = threading.Event()
    calls: list[bytes] = []

    class _FakeSer:
        def __init__(self) -> None:
            self.cancel_calls = 0
            self.is_open = True

        def write(self, data: bytes) -> int:
            calls.append(bytes(data))
            if len(calls) == 1:
                release.wait(2.0)  # 第一段阻塞於 pyserial 寫入（持 fence 鎖）
            return len(data)

        def cancel_write(self) -> None:
            self.cancel_calls += 1

    port = sp_cls("COM0", UartProfile())
    fake_ser = _FakeSer()
    port._ser = fake_ser
    mgr, s, b = _ready(tmp_path, port)
    permit = _permit("c1", 4, "agent")
    ev = threading.Event()
    t, box = _spawn(port.write, b"x" * 130, cancel=ev, submit_guard=lambda: b._submit_guard(permit, 130))
    _wait_until(lambda: len(calls) == 1)
    t0 = time.monotonic()
    b.begin_drain(5, preserve_consoles=False)
    assert time.monotonic() - t0 < fence_s + 0.1
    assert fake_ser.cancel_calls == 1  # best-effort CancelIoEx，不等待
    release.set()
    _join(t, box)
    res = _outcome(box)
    assert res.outcome == "completed"
    assert res.confirmed == 64 and res.requested == 130
    assert res.error in ("revoked:draining", "cancelled")
    assert _load("sw_core.serial_port", "PYSERIAL_SUBMIT_CHUNK") == 64
    assert len(calls) == 1  # 後兩段永不交 OS


def test_pyserial_partial_cancel_keeps_cancelled_reason() -> None:
    sp_cls = _load("sw_core.serial_port", "_PySerialPort")
    cancel = threading.Event()
    calls: list[bytes] = []

    class _FakeSer:
        is_open = True

        def write(self, data: bytes) -> int:
            calls.append(bytes(data))
            cancel.set()
            return len(data)

    port = sp_cls("COM0", _load("sw_core.config", "UartProfile")())
    port._ser = _FakeSer()
    result = port.write(b"x" * 100, cancel=cancel)
    chunk = _load("sw_core.serial_port", "PYSERIAL_SUBMIT_CHUNK")
    assert result == _tx_result("completed", chunk, 100, "cancelled")
    assert calls == [b"x" * chunk]


def test_posix_partial_cancel_keeps_cancelled_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    cancel = threading.Event()
    calls: list[bytes] = []

    def write(_fd: int, payload: Any) -> int:
        calls.append(bytes(payload))
        cancel.set()
        return 2

    monkeypatch.setattr(uio.os, "write", write)
    result = _load("sw_core.serial_port", "_posix_write_all")(7, b"abcdef", cancel)
    assert result == _tx_result("completed", 2, 6, "cancelled")
    assert calls == [b"abcdef"]


def test_release_after_dequeue_before_write_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    pause = _Pause(b)
    t, box = _spawn(b.send_bytes, ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    pause.wait_reached()
    rel, rbox = _spawn(mgr.release_device, "COM0")
    _wait_until(lambda: b._tx_frozen is True)
    pause.resume()
    _join(t, box)
    _join(rel, rbox)
    res = _outcome(box)
    assert res.outcome == "not_submitted" and res.confirmed == 0
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    assert rbox["ret"]["ok"] is True
    assert port.close_calls == 1


def test_cancel_after_dequeue_before_write_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    arb_cls = _load("sw_core.arbiter", "CommandArbiter")

    def send_cb(
        session_id: str,
        command: str,
        source: str,
        cmd_id: str,
        timeout_s: float,
        mode: str,
        expected_duration_s: float | None = None,
        completion_regex: str | None = None,
    ) -> dict[str, Any]:
        return mgr.execute_command(
            session_id,
            command,
            source,
            cmd_id,
            timeout_s=timeout_s,
            mode=mode,
            expected_duration_s=expected_duration_s,
            completion_regex=completion_regex,
        )

    arb = arb_cls(send_cb)
    arb.register_session(s.session_id)
    pause = _Pause(b)
    cmd_id = arb.submit(
        session_id=s.session_id, command="echo hi", source="agent:x", mode="line", timeout_s=2.0
    )["cmd_id"]
    pause.wait_reached()
    assert arb.cancel(cmd_id)["status"] == "canceled"
    pause.resume()
    time.sleep(0.2)  # 讓 worker 結束其 send 路徑
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    assert arb._commands[cmd_id]["status"] == "canceled"


@pytest.mark.parametrize("mode", ["line", "interactive"])
def test_cancel_between_dequeue_and_manager_registration_zero_tx(
    tmp_path: Path, mode: str
) -> None:
    port = FakePort()
    mgr, s, _bridge = _ready(tmp_path, port)
    arb_cls = _load("sw_core.arbiter", "CommandArbiter")
    dequeued = threading.Event()
    continue_worker = threading.Event()

    def send_cb(
        session_id: str,
        command: str,
        source: str,
        cmd_id: str,
        timeout_s: float,
        run_mode: str,
        expected_duration_s: float | None = None,
        completion_regex: str | None = None,
    ) -> dict[str, Any]:
        dequeued.set()
        assert continue_worker.wait(2.0)
        return mgr.execute_command(
            session_id,
            command,
            source,
            cmd_id,
            timeout_s=timeout_s,
            mode=run_mode,
            expected_duration_s=expected_duration_s,
            completion_regex=completion_regex,
        )

    arb = arb_cls(send_cb)
    arb.register_session(s.session_id)
    cmd_id = arb.submit(
        session_id=s.session_id,
        command="echo hi",
        source="agent:x",
        mode=mode,
        timeout_s=0.1,
    )["cmd_id"]
    assert dequeued.wait(1.0)
    assert arb.cancel(cmd_id)["status"] == "canceled"
    continue_worker.set()
    _wait_until(lambda: arb._running.get(s.session_id) is None)
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    assert arb._commands[cmd_id]["status"] == "canceled"


def test_lease_switch_before_write_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    a_id = mgr.interactive_open("COM0", owner="agent:A", timeout_s=30.0)["interactive_id"]
    pause = _Pause(b)
    t, box = _spawn(mgr.interactive_send, a_id, data="ctrl-c", encoding="key")
    pause.wait_reached()
    b.set_tx_gate(generation=4, quiet=False, lease_owner="interactive:B")  # lease B 取代 A
    pause.resume()
    _join(t, box)
    ret = box["ret"]
    assert ret["ok"] is False
    assert ret["error_code"] in {"INTERACTIVE_EXPIRED", "SESSION_BUSY"}
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_interactive_open_command_expired_before_submit_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    b._after_tx_authorize = lambda: mgr._expire_interactive_leases_tick(
        now=time.monotonic() + 1.0
    )

    result = mgr.interactive_open(
        "COM0", owner="agent:x", timeout_s=0.1, command="echo hi"
    )

    assert result == {"ok": False, "error_code": "INTERACTIVE_EXPIRED"}
    assert s.interactive_session_id is None
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


# ---------------------------------------------------------------------------
# A（續）. quiet 期間的救援 lease 與 agent 命令分流（R2-F2）
# ---------------------------------------------------------------------------


def test_interactive_rescue_send_during_quiet_reaches_uart(tmp_path: Path) -> None:
    import base64 as _b64

    port = FakePort()
    mgr, s, b = _quiet_bridge(tmp_path, port)
    r = mgr.interactive_open("COM0", owner="agent:rescue", timeout_s=30.0, allow_attached=True)
    assert r["ok"] is True
    assert r["recovery_mode"] is True
    assert r["boot_interrupt"] is True
    rid = r["interactive_id"]
    assert mgr.interactive_send(rid, data="ctrl-c", encoding="key") == {
        "ok": True,
        "interactive_id": rid,
        "bytes": 1,
    }
    assert port.writes == [b"\x03"]
    tx = _tx_records(mgr._wal)
    assert len(tx) == 1
    assert tx[0]["source"] == "agent:rescue"
    assert _b64.b64decode(tx[0]["payload_b64"]) == b"\x03"
    assert b._gate_quiet is True  # 救援送出不解除 quiet


def test_interactive_open_command_during_quiet_reaches_uart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakePort()
    mgr, s, b = _quiet_bridge(tmp_path, port)
    spy = _spy_bridge(b, monkeypatch)
    r = mgr.interactive_open("COM0", owner="agent:rescue", timeout_s=30.0, allow_attached=True, command="stop")
    assert r["ok"] is True
    assert port.writes == [b"stop\n"]
    tx = _tx_records(mgr._wal)
    assert len(tx) == 1 and tx[0]["source"] == "agent:rescue"
    permit = spy.sends[-1][1]
    assert permit is not None
    assert permit.tx_class == "interactive"
    assert permit.op_id == f"interactive:{r['interactive_id']}"


def test_agent_command_during_quiet_zero_tx_while_rescue_lease_sends(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _quiet_bridge(tmp_path, port)
    rid = _rescue_lease(mgr)["interactive_id"]
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(ECHO, source="agent:y", cmd_id="c1", permit=_permit("c1", b._gate_generation, "agent"))
    assert ei.value.result == _tx_result("not_submitted", 0, len(ECHO), "quiet")
    assert mgr.interactive_send(rid, data="ctrl-c", encoding="key")["ok"] is True
    assert port.writes == [b"\x03"]  # 只有救援鍵
    tx = _tx_records(mgr._wal)
    assert len(tx) == 1
    assert tx[0]["cmd_id"] != "c1"


def test_expired_rescue_lease_during_quiet_zero_tx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakePort()
    mgr, s, b = _quiet_bridge(tmp_path, port)
    a_id = _rescue_lease(mgr, timeout_s=0.05)["interactive_id"]
    real = time.monotonic
    monkeypatch.setattr(uio.time, "monotonic", lambda: real() + 0.06)
    assert mgr.interactive_send(a_id, data="ctrl-c", encoding="key") == {
        "ok": False,
        "error_code": "INTERACTIVE_EXPIRED",
        "interactive_id": a_id,
    }
    monkeypatch.setattr(uio.time, "monotonic", real)

    b_id = _rescue_lease(mgr, timeout_s=0.05)["interactive_id"]
    b._after_tx_authorize = lambda: mgr._expire_interactive_leases_tick(now=real() + 1.0)
    rescue_permit = _permit(f"interactive:{b_id}", 4, "interactive", owner_gen=b._owner_gen)
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(b"\x03", source="agent:rescue", cmd_id=None, permit=rescue_permit)
    assert ei.value.result == _tx_result("not_submitted", 0, 1, "lease")
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_interactive_permit_before_quiet_rearm_rejected_then_resend_ok(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _quiet_bridge(tmp_path, port)
    a_id = _rescue_lease(mgr)["interactive_id"]
    pause = _Pause(b)
    t, box = _spawn(mgr.interactive_send, a_id, data="ctrl-c", encoding="key")
    pause.wait_reached()
    rearm, rbox = _spawn(s.arm_boot_quiet)  # 模擬倒數行再次 arm quiet（bump owner_gen）
    _join(rearm, rbox, timeout=1.0)
    pause.resume()
    _join(t, box)
    if "exc" in box:
        assert box["exc"].result == _tx_result("not_submitted", 0, 1, "lease")
    else:
        assert box["ret"]["ok"] is False
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    with mgr._lock:
        assert mgr._interactive[a_id].status == "active"
    assert mgr.interactive_send(a_id, data="ctrl-c", encoding="key")["ok"] is True
    assert port.writes == [b"\x03"]


@pytest.mark.parametrize("case", ["drain", "flash", "stale_generation"])
def test_interactive_during_quiet_still_fenced_by_drain_flash_generation(tmp_path: Path, case: str) -> None:
    port = FakePort()
    mgr, s, b = _quiet_bridge(tmp_path, port)
    a_id = _rescue_lease(mgr)["interactive_id"]
    assert b._gate_quiet is True
    if case == "drain":
        b.begin_drain(5, preserve_consoles=False)
    elif case == "flash":
        b.set_flash_mode(True)
    gen = b._gate_generation - 1 if case == "stale_generation" else b._gate_generation
    permit = _permit(f"interactive:{a_id}", gen, "interactive", owner_gen=b._owner_gen)
    try:
        b.send_bytes(b"\x03", source="agent:rescue", cmd_id=None, permit=permit)
        res = None
    except _incomplete_error() as exc:
        res = exc.result
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    if case == "drain":
        assert res is not None and res.outcome == "not_submitted"
        assert res.error in ("draining", "cancelled")
    elif case == "stale_generation":
        assert res == _tx_result("not_submitted", 0, 1, "stale_generation")
    else:
        # flash gate：既有語意為靜默丟棄或 not_submitted，兩者皆須零 TX
        assert res is None or res.outcome == "not_submitted"


def test_stale_generation_permit_rejected(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", b._gate_generation - 1, "agent"))
    assert ei.value.result == _tx_result("not_submitted", 0, len(ECHO), "stale_generation")
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_bare_bridges_do_not_share_revoked_operation_history() -> None:
    first = uio.UARTBridge.__new__(uio.UARTBridge)
    second = uio.UARTBridge.__new__(uio.UARTBridge)
    first._revoke_ops_locked(["cmd-one"])
    assert "cmd-one" in first._revoked_ops
    assert "cmd-one" not in (getattr(second, "_revoked_ops", None) or {})


# ---------------------------------------------------------------------------
# B. raw／replay、ok 路徑不變、permit=None 白名單
# ---------------------------------------------------------------------------


def test_raw_replay_invalidated_while_waiting_write_lock(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    og = b._owner_gen
    b._write_lock.acquire()
    try:
        raw_t, raw_box = _spawn(
            b.send_bytes, b"raw", source="human:c1", permit=_permit("console:c1", 4, "human_raw", owner_gen=og)
        )
        rep_t, rep_box = _spawn(
            b.send_bytes, b"replay", source="human:c2", permit=_permit("console:c2", 4, "human_raw", owner_gen=og)
        )
        time.sleep(0.1)  # 兩者皆排在 _write_lock 之後
        b.invalidate_deferred("reboot")
    finally:
        b._write_lock.release()
    _join(raw_t, raw_box)
    _join(rep_t, rep_box)
    assert "exc" not in raw_box and "exc" not in rep_box  # 丟棄（stale_owner_gen）不 raise
    assert port.writes == []
    assert all(len(buf) == 0 for buf in b._deferred_buffers.values())  # 不重新 defer


def test_permit_ok_path_unchanged(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    r = b.send_bytes(ECHO, source="agent:x", cmd_id="c1", permit=_permit("c1", 4, "agent"))
    assert r == _tx_result("completed", len(ECHO), len(ECHO))
    assert port.writes == [ECHO]
    b.send_bytes(b"replay", source="human:c2", permit=_permit("console:c2", 4, "human_raw", owner_gen=b._owner_gen))
    assert port.writes == [ECHO, b"replay"]


def _unpermitted_send_bytes_calls(tree: ast.AST) -> tuple[dict[str, int], dict[str, int]]:
    """列出未傳 permit 或明確傳入 None 的 send_bytes 呼叫。"""
    missing: dict[str, int] = {}
    explicit_none: dict[str, int] = {}

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[str] = []
            self.aliases: list[set[str]] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.aliases.append(set())
            self.generic_visit(node)
            self.aliases.pop()
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def _record_alias(self, targets: list[ast.expr], value: ast.expr) -> None:
            if not self.aliases or not (
                isinstance(value, ast.Attribute) and value.attr == "send_bytes"
            ):
                return
            self.aliases[-1].update(
                target.id for target in targets if isinstance(target, ast.Name)
            )

        def visit_Assign(self, node: ast.Assign) -> None:
            self._record_alias(node.targets, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                self._record_alias([node.target], node.value)
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            direct = isinstance(node.func, ast.Attribute) and node.func.attr == "send_bytes"
            alias = isinstance(node.func, ast.Name) and any(
                node.func.id in names for names in self.aliases
            )
            if direct or alias:
                function = self.functions[-1] if self.functions else "<module>"
                permit = next((kw.value for kw in node.keywords if kw.arg == "permit"), None)
                if permit is None:
                    missing[function] = missing.get(function, 0) + 1
                elif isinstance(permit, ast.Constant) and permit.value is None:
                    explicit_none[function] = explicit_none.get(function, 0) + 1
            self.generic_visit(node)

    _Visitor().visit(tree)
    return missing, explicit_none


def test_permit_none_only_for_listed_internal_callers() -> None:
    root = REPO / "sw_core"
    # 僅相容 adapter 可在舊版測試替身不接受 permit 關鍵字時省略它；
    # 實際 UARTBridge 路徑一律傳 permit。flash pump 與 send_secret 是規格明列例外。
    allowed_missing = {
        ("uart_io.py", "_send_bytes_with_permit"): 3,
        ("uart_io.py", "flash_tx"): 1,
        ("uart_io.py", "send_secret"): 1,
        ("session_manager.py", "_send_bytes_with_permit"): 2,
    }
    found: dict[tuple[str, str], int] = {}
    explicit_none: dict[tuple[str, str], int] = {}
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        missing, none_calls = _unpermitted_send_bytes_calls(tree)
        found.update({(path.name, name): count for name, count in missing.items()})
        explicit_none.update({(path.name, name): count for name, count in none_calls.items()})
    assert found == allowed_missing, f"未傳 permit 的 send_bytes 呼叫點超出相容白名單：{found}"
    assert explicit_none == {}, f"不得明確使用 permit=None：{explicit_none}"


def test_permit_none_guard_detects_aliases_and_explicit_none() -> None:
    tree = ast.parse(
        """\
def send_via_alias(bridge):
    write = bridge.send_bytes
    write(b'alias')
    bridge.send_bytes(b'none', permit=None)
"""
    )
    missing, explicit_none = _unpermitted_send_bytes_calls(tree)
    assert missing == {"send_via_alias": 1}
    assert explicit_none == {"send_via_alias": 1}


# ---------------------------------------------------------------------------
# C. 自動登入、密碼、Ready Probe、recover 按鍵（R2-F1）
# ---------------------------------------------------------------------------


def test_login_probe_quiet_armed_after_precheck_before_submit_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, state="ATTACHED")
    permit = _begin_login(mgr, s)
    assert permit.tx_class == "system_probe"
    assert permit.login_gen == 1
    assert b._gate_login_gen_floor == 1
    pause = _Pause(b)
    t, box = _spawn(_send_system, b, permit)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    _join(rx, rbox, timeout=0.5)
    assert s.boot_quiet_active() is True
    assert s.login_gen == 2
    assert b._gate_login_gen_floor == 2
    assert b._gate_probe_quiet_until == s.boot_quiet_until
    pause.resume()
    _join(t, box)
    assert _outcome(box) == _tx_result("not_submitted", 0, 1, "quiet")
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_attach_probe_banner_after_precheck_sends_no_login_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _login_env(monkeypatch)
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, profile=_login_profile(), state="ATTACHED")
    pause = _Pause(b)
    t, box = _spawn(mgr._probe_existing_bridge, s, b)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    _join(rx, rbox, timeout=0.5)
    pause.resume()
    _join(t, box)
    for line in (b"login: ", b"Password: ", b"root@prplOS:/# "):
        b._on_rx_data(line)  # 腳本照常輸出，用以證明 FSM 不會再送
    assert b"".join(port.writes) == b""
    assert [r for r in _tx_records(mgr._wal) if str(r.get("source", "")).startswith("system")] == []
    assert s.state == "ATTACHED"
    assert s.last_error == "PROMPT_UNAVAILABLE"
    assert s.last_error_detail is None
    assert s.ready_reconfirm_pending is True


@pytest.mark.parametrize("dynamic", [False, True])
def test_attach_bridge_quiet_updates_first_attach_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dynamic: bool
) -> None:
    from sw_core.config import ProfileTemplate
    from sw_core.device_watcher import DeviceInfo
    from sw_core.session_manager import SessionManager
    from sw_core.wal import WalWriter

    monkeypatch.setenv("SW_ATTACH_USER", "root")
    monkeypatch.setenv("SW_ATTACH_PASS", "secret")
    port = FakePort()
    monkeypatch.setattr(uio, "open_serial_port", lambda *_args, **_kwargs: port)

    def start_without_reader(bridge: Any) -> None:
        bridge._serial = bridge._preopened_port
        bridge._serial_fd = None

    monkeypatch.setattr(uio.UARTBridge, "start", start_without_reader)

    def banner_then_probe(bridge: Any, _profile: Any, *, auth: Any = None) -> tuple[bool, str | None]:
        bridge._on_rx_data(BANNER)
        bridge.send_command("", source="system")
        return True, None

    monkeypatch.setattr("sw_core.session_manager.ensure_ready", banner_then_probe)

    if dynamic:
        template = ProfileTemplate(
            profile_name="attach-template",
            username="root",
            user_env="SW_ATTACH_USER",
            pass_env="SW_ATTACH_PASS",
        )
        mgr = SessionManager(
            [],
            WalWriter(wal_dir=str(tmp_path / "wal")),
            templates=[template],
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
            state_path=str(tmp_path / "state.json"),
        )
        mgr._profile_pins[BY_ID] = template.profile_name
    else:
        profile = make_profile(by_id=BY_ID, user_env="SW_ATTACH_USER", pass_env="SW_ATTACH_PASS")
        mgr = make_manager(tmp_path, [profile])

    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="fake-uart")
    mgr._attach_by_id(BY_ID)

    session = next(iter(mgr._sessions.values()))
    assert port.writes == []
    assert _tx_records(mgr._wal) == []
    assert session.bridge is not None
    assert session.bridge._gate_probe_quiet_until == session.boot_quiet_until
    assert session.state == "ATTACHED"
    session.bridge.stop()


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("credentials", ["none", "unresolved"])
def test_attach_probe_failure_keeps_manual_login_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dynamic: bool,
    credentials: str,
) -> None:
    from sw_core.config import ProfileTemplate
    from sw_core.device_watcher import DeviceInfo
    from sw_core.session_manager import SessionManager
    from sw_core.wal import WalWriter

    port = FakePort()
    monkeypatch.setattr(uio, "open_serial_port", lambda *_args, **_kwargs: port)

    def start_without_reader(bridge: Any) -> None:
        bridge._serial = bridge._preopened_port
        bridge._serial_fd = None

    monkeypatch.setattr(uio.UARTBridge, "start", start_without_reader)
    probes: list[str] = []

    def failed_probe(_bridge: Any, _profile: Any) -> tuple[bool, str]:
        probes.append("probe")
        return False, "PROMPT_UNAVAILABLE"

    def unexpected_login(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        raise AssertionError("沒有可用帳密時不應呼叫 ensure_ready")

    monkeypatch.setattr("sw_core.session_manager.probe_ready", failed_probe)
    monkeypatch.setattr("sw_core.session_manager.ensure_ready", unexpected_login)

    env_file = str(tmp_path / "missing.env") if credentials == "unresolved" else None
    if dynamic:
        template = ProfileTemplate(
            profile_name="attach-template",
            user_env="SW_ATTACH_USER" if env_file else None,
            pass_env="SW_ATTACH_PASS" if env_file else None,
            env_file=env_file,
        )
        mgr = SessionManager(
            [],
            WalWriter(wal_dir=str(tmp_path / "wal")),
            templates=[template],
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
            state_path=str(tmp_path / "state.json"),
        )
        mgr._profile_pins[BY_ID] = template.profile_name
    else:
        profile = make_profile(
            by_id=BY_ID,
            user_env="SW_ATTACH_USER" if env_file else None,
            pass_env="SW_ATTACH_PASS" if env_file else None,
            env_file=env_file,
        )
        mgr = make_manager(tmp_path, [profile])

    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="fake-uart")
    mgr._attach_by_id(BY_ID)

    session = next(iter(mgr._sessions.values()))
    assert session.state == "ATTACHED"
    assert session.bridge is not None
    if credentials == "unresolved":
        assert session.last_error == "CREDENTIALS_UNRESOLVED"
        assert probes == []
    else:
        assert session.last_error == "PROMPT_UNAVAILABLE"
        assert probes == ["probe"]
    assert port.writes == []
    session.bridge.stop()


def test_password_not_sent_when_quiet_arms_before_secret_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _login_env(monkeypatch)
    scripts = {b"\n": b"login: ", b"root\n": b"\r\nPassword: "}
    holder: dict[str, Any] = {}

    def on_write(data: bytes) -> None:
        reply = scripts.get(data)
        if reply is not None:
            holder["b"]._on_rx_data(reply)

    port = FakePort(on_write=on_write)
    mgr, s, b = _ready(tmp_path, port, profile=_login_profile(), state="ATTACHED")
    holder["b"] = b
    pause = _Pause(b, nth=3)  # 依序：prompt probe `\n`、帳號 `root\n`、密碼（第 3 次）
    seen_errors: list[Any] = []
    orig_secret = b.send_secret

    def spy_secret(secret: str, **kwargs: Any) -> Any:
        try:
            return orig_secret(secret, **kwargs)
        except _incomplete_error() as exc:
            seen_errors.append(exc.result)
            raise

    b.send_secret = spy_secret
    t, box = _spawn(mgr._probe_existing_bridge, s, b)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    _join(rx, rbox, timeout=0.5)
    pause.resume()
    _join(t, box)
    assert port.writes == [b"\n", b"root\n"]
    assert seen_errors == [_tx_result("not_submitted", 0, 7, "quiet")]
    assert s.last_error == "PROMPT_UNAVAILABLE"
    tx = _tx_records(mgr._wal)
    assert [base64.b64decode(r["payload_b64"]) for r in tx] == [b"\n", b"root\n"]
    assert "s3cret" not in Path(mgr._wal.wal_path).read_text(encoding="utf-8")


def test_ready_reconfirm_probe_quiet_rearmed_after_precheck_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, state="READY")
    s.ready_reconfirm_pending = True
    pause = _Pause(b)
    t, box = _spawn(mgr._probe_existing_bridge, s, b)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, BANNER)  # re-arm
    _join(rx, rbox, timeout=0.5)
    pause.resume()
    _join(t, box)
    assert port.writes == []
    assert s.state == "READY"
    assert s.last_error is None
    assert s.ready_reconfirm_pending is True
    assert s.boot_quiet_active() is True  # 未被 confirm_ready() 清除


def test_login_run_superseded_after_precheck_stale_login_gen_zero_tx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, state="ATTACHED")
    permit_a = _begin_login(mgr, s)
    assert permit_a.login_gen == 1
    pause = _Pause(b)
    ta, boxa = _spawn(_send_system, b, permit_a)
    pause.wait_reached()
    holder, hbox = _spawn(_begin_login, mgr, s)  # 另一 run 持 _lock 取得新 permit
    _join(holder, hbox, timeout=0.5)
    permit_b = hbox["ret"]
    assert permit_b.login_gen == 2
    assert b._gate_login_gen_floor == 2
    pause.resume()
    _join(ta, boxa)
    assert _outcome(boxa) == _tx_result("not_submitted", 0, 1, "stale_login_gen")
    assert port.writes == []
    _send_system(b, permit_b)
    assert port.writes == [b"\n"]

    # quiet 期滿後 permit_a 仍回 stale（世代失效不因 quiet 解除而復活）
    b.set_probe_gate(quiet_until=time.monotonic() + 180.0, login_gen_floor=2)
    real = time.monotonic
    monkeypatch.setattr(uio.time, "monotonic", lambda: real() + 181.0)
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(b"\n", source="system", cmd_id=None, permit=permit_a)
    assert ei.value.result.error == "stale_login_gen"


def test_login_probe_drain_after_precheck_zero_tx(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, state="ATTACHED")
    permit = _begin_login(mgr, s)
    pause = _Pause(b)
    t, box = _spawn(_send_system, b, permit)
    pause.wait_reached()
    b.begin_drain(5, preserve_consoles=False)  # 不等 finish
    pause.resume()
    _join(t, box)
    res = _outcome(box)
    assert res.outcome == "not_submitted"
    assert res.error in ("draining", "cancelled")
    assert res.confirmed == 0
    assert port.writes == []
    assert _tx_records(mgr._wal) == []


def test_login_probe_quiet_armed_during_submit_counts_committed(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, state="ATTACHED")
    permit = _begin_login(mgr, s)
    pause = _Pause(b, seam="_inside_tx_submit")
    t, box = _spawn(_send_system, b, permit)
    pause.wait_reached()
    assert b._gate_probe_quiet_until == 0.0
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    rx.join(0.2)
    assert rx.is_alive(), "banner 不得在提交臨界區內完成（持 _tx_fence_lock）"
    pause.resume()
    _join(t, box)
    assert "exc" not in box
    assert port.writes == [b"\n"]
    tx = _tx_records(mgr._wal)
    assert len(tx) == 1 and tx[0]["len"] == 1
    _join(rx, rbox, timeout=1.0)
    with pytest.raises(_incomplete_error()) as ei:
        with b.system_permit(permit):
            b.send_command("root", source="system")
    assert ei.value.result == _tx_result("not_submitted", 0, 5, "quiet")
    assert port.writes == [b"\n"]


def test_console_unlock_login_not_blocked_by_banner(tmp_path: Path) -> None:
    port = FakePort()
    prof = make_profile(by_id=BY_ID, console_unlock_env="SW_T20_UNLOCK")
    mgr, s, b = _ready(tmp_path, port, profile=prof, state="ATTACHED")
    permit = _begin_login(mgr, s)
    g0 = s.login_gen
    f0 = b._gate_login_gen_floor
    pause = _Pause(b)
    t, box = _spawn(_send_system, b, permit)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, b"CFE version 1.0\r\nU-Boot 2024.04\r\n")
    _join(rx, rbox, timeout=0.5)
    assert b._gate_probe_quiet_until == 0.0
    assert s.login_gen == g0
    assert b._gate_login_gen_floor == f0
    pause.resume()
    _join(t, box)
    assert "exc" not in box
    assert port.writes == [b"\n"]


def test_probe_gate_quiet_follows_monotonic_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    clock = {"now": 100.0}
    monkeypatch.setattr(uio.time, "monotonic", lambda: clock["now"])
    b.set_probe_gate(quiet_until=280.0, login_gen_floor=0)
    permit = _permit("system:login:1", 4, "system_probe")
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(b"\n", source="system", cmd_id=None, permit=permit)
    assert ei.value.result == _tx_result("not_submitted", 0, 1, "quiet")
    clock["now"] = 280.0  # 期滿即放行，不需任何清除事件
    b.send_bytes(b"\n", source="system", cmd_id=None, permit=permit)
    assert port.writes == [b"\n"]
    b.set_probe_gate(quiet_until=0.0, login_gen_floor=3)
    b.set_probe_gate(quiet_until=0.0, login_gen_floor=0)  # floor 只增不減
    assert b._gate_login_gen_floor == 3


def test_implicit_system_source_without_permit_still_quiet_gated(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port)
    b.set_probe_gate(quiet_until=time.monotonic() + 10.0, login_gen_floor=0)
    with pytest.raises(_incomplete_error()) as ei1:
        b.send_command("", source="system")
    assert ei1.value.result == _tx_result("not_submitted", 0, 1, "quiet")
    with pytest.raises(_incomplete_error()) as ei2:
        b.send_secret("x")
    assert ei2.value.result == _tx_result("not_submitted", 0, 2, "quiet")
    assert port.writes == []
    # flash 來源不受此規則影響
    b.set_flash_mode(True)
    b.send_bytes(b"f", source="flash", cmd_id=None, _allow_during_flash=True)
    assert port.writes == [b"f"]
    b.set_flash_mode(False)
    # R5-F2：隱含 self_test_probe permit 同樣受 self-test quiet 擋下
    b.set_probe_gate(quiet_until=0.0, login_gen_floor=0, self_test_quiet_until=time.monotonic() + 10.0)
    with pytest.raises(_incomplete_error()) as ei3:
        b.send_command("echo __READY__x", source="system:self_test", cmd_id=None)
    assert ei3.value.result == _tx_result("not_submitted", 0, 16, "quiet")
    assert port.writes == [b"f"]


@pytest.mark.parametrize("console_unlock", [False, True])
def test_recover_key_quiet_armed_after_precheck_zero_tx(
    tmp_path: Path, console_unlock: bool
) -> None:
    port = FakePort()
    profile = make_profile(
        by_id=BY_ID,
        console_unlock_env="SW_RECOVER_UNLOCK" if console_unlock else None,
    )
    mgr, s, b = _ready(tmp_path, port, profile=profile, state="READY")
    pause = _Pause(b)
    t, box = _spawn(
        mgr._recover_after_failure,
        s,
        b,
        cmd_id="cmd-x",
        timeout_s=1.0,
        source="agent:test",
        command="some-long-cmd",
        prompt_regex=s.profile.prompt_regex,
        pre_offset=0,
    )
    pause.wait_reached()  # CTRL-C 通過迴圈內 pre-check 後停在 seam
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    _join(rx, rbox, timeout=0.5)
    pause.resume()
    _join(t, box)
    assert port.writes == []  # 無 \x03／\x04 交 OS
    assert [r for r in _tx_records(mgr._wal) if r.get("source") == "system:recover"] == []
    assert s.state == "ATTACHED"


def test_login_fsm_sends_all_carry_system_probe_permit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _login_env(monkeypatch)
    holder: dict[str, Any] = {}

    def script(data: bytes) -> None:
        bb = holder["b"]
        if data == b"\n":
            bb._on_rx_data(b"login: ")
        elif data == b"root\n":
            bb._on_rx_data(b"\r\nPassword: ")
        elif data == b"s3cret\n":
            bb._on_rx_data(b"\r\nroot@prplOS:/# ")
        elif data.startswith(b"echo __READY__"):
            nonce = data[len(b"echo __READY__") :].strip()
            bb._on_rx_data(b"__READY__" + nonce + b"\r\nroot@prplOS:/# ")

    port = FakePort(on_write=script)
    mgr, s, b = _ready(tmp_path, port, profile=_login_profile(), state="ATTACHED")
    holder["b"] = b
    spy = _spy_bridge(b, monkeypatch)
    mgr._probe_existing_bridge(s, b)
    assert s.state == "READY"
    attach_permits = list(spy.permits)
    assert attach_permits
    assert {p.login_gen for p in attach_permits} == {1}
    s.ready_reconfirm_pending = True
    mgr._probe_existing_bridge(s, b)
    reconfirm_permits = spy.permits[len(attach_permits):]
    assert reconfirm_permits
    assert len({p.login_gen for p in reconfirm_permits}) == 1
    assert min(p.login_gen for p in reconfirm_permits) > max(p.login_gen for p in attach_permits)
    mgr._recover_after_failure(
        s,
        b,
        cmd_id="cmd-x",
        timeout_s=0.5,
        source="agent:test",
        command="x",
        prompt_regex=s.profile.prompt_regex,
        pre_offset=0,
    )
    assert spy.permits, "system_permit 未被使用"
    assert all(p.tx_class == "system_probe" for p in spy.permits)
    assert len(attach_permits) + len(reconfirm_permits) == len(spy.permits)
    for source, permit_kw, depth in spy.sends:
        if source in ("system", "system:secret"):
            assert depth > 0, f"{source} 送出時未處於 system_permit 內"
            assert permit_kw is None
        elif source == "system:recover":
            assert permit_kw is not None
            assert permit_kw.op_id == "system:recover"
            assert permit_kw.login_gen is None


def test_console_unlock_login_uses_system_probe_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SW_T20_UNLOCK", "unlock-secret")
    holder: dict[str, Any] = {}

    def script(data: bytes) -> None:
        bridge = holder["b"]
        if data == b"\n":
            bridge._on_rx_data(b"root@prplOS:/# ")
        elif data.startswith(b"echo __READY__"):
            nonce = data[len(b"echo __READY__"):].strip()
            bridge._on_rx_data(b"__READY__" + nonce + b"\r\nroot@prplOS:/# ")

    profile = make_profile(
        by_id=BY_ID,
        console_unlock_env="SW_T20_UNLOCK",
        console_unlock_idle_s=0.0,
    )
    port = FakePort(on_write=script)
    mgr, session, bridge = _ready(tmp_path, port, profile=profile, state="ATTACHED")
    holder["b"] = bridge
    spy = _spy_bridge(bridge, monkeypatch)

    mgr._probe_existing_bridge(session, bridge)

    assert session.state == "READY"
    assert spy.permits
    assert len({permit.login_gen for permit in spy.permits}) == 1
    assert all(permit.tx_class == "system_probe" for permit in spy.permits)
    assert [call[0] for call in spy.sends].count("system") >= 2


def test_reprobe_worker_login_uses_system_probe_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_env(monkeypatch)
    holder: dict[str, Any] = {}

    def script(data: bytes) -> None:
        bridge = holder["b"]
        if data == b"\n":
            bridge._on_rx_data(b"login: ")
        elif data == b"root\n":
            bridge._on_rx_data(b"\r\nPassword: ")
        elif data == b"s3cret\n":
            bridge._on_rx_data(b"\r\nroot@prplOS:/# ")
        elif data.startswith(b"echo __READY__"):
            nonce = data[len(b"echo __READY__"):].strip()
            bridge._on_rx_data(b"__READY__" + nonce + b"\r\nroot@prplOS:/# ")

    port = FakePort(on_write=script)
    mgr, session, bridge = _ready(
        tmp_path, port, profile=_login_profile(), state="ATTACHED"
    )
    holder["b"] = bridge
    session.last_error = "PROMPT_UNAVAILABLE"
    spy = _spy_bridge(bridge, monkeypatch)

    mgr._spawn_reprobe_probe(session.session_id, bridge)
    mgr.join_reprobe_workers(timeout=2.0)

    assert session.state == "READY"
    assert spy.permits
    assert len({permit.login_gen for permit in spy.permits}) == 1
    assert all(permit.tx_class == "system_probe" for permit in spy.permits)


def test_reboot_recovery_login_uses_system_probe_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_env(monkeypatch)
    holder: dict[str, Any] = {}

    def script(data: bytes) -> None:
        bridge = holder["b"]
        if data == b"\n":
            bridge._on_rx_data(b"login: ")
        elif data == b"root\n":
            bridge._on_rx_data(b"\r\nPassword: ")
        elif data == b"s3cret\n":
            bridge._on_rx_data(b"\r\nroot@prplOS:/# ")
        elif data.startswith(b"echo __READY__"):
            nonce = data[len(b"echo __READY__"):].strip()
            bridge._on_rx_data(b"__READY__" + nonce + b"\r\nroot@prplOS:/# ")

    port = FakePort(on_write=script)
    mgr, session, bridge = _ready(
        tmp_path, port, profile=_login_profile(), state="RECOVERING"
    )
    holder["b"] = bridge
    session.recovering = True
    spy = _spy_bridge(bridge, monkeypatch)

    mgr._spawn_reboot_recovery(session.session_id, timeout_s=2.0)
    _wait_until(lambda: session.state == "READY" and not session.recovering, timeout=2.0)

    assert spy.permits
    assert len({permit.login_gen for permit in spy.permits}) == 1
    assert all(permit.tx_class == "system_probe" for permit in spy.permits)


# ---------------------------------------------------------------------------
# D. session.self_test 的 nonce probe（R5-F2，DF-19）
# ---------------------------------------------------------------------------


def test_self_test_probe_other_reject_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from sw_core.daemon import make_rpc_handler
    from sw_core.device_watcher import DeviceInfo
    from sw_core.rpc_posix import serve_connection
    from stage0_fakes import make_service

    observed: dict[str, Any] = {}
    for case in ("drain", "stale_generation", "cancelled", "wal_unhealthy"):
        sub = tmp_path / case
        sub.mkdir()
        holder: dict[str, Any] = {}
        port = FakePort(on_write=_nonce_responder(holder))
        service = make_service(sub, ready=["COM0"], port=port)
        mgr = service._sessions
        s = mgr.get_session("COM0")
        assert s is not None
        b = s.bridge
        assert b is not None
        by_id = s.profile.device_by_id
        mgr._devices[by_id] = DeviceInfo(by_id=by_id, real_path="fake-uart")
        s.attached_real_path = "fake-uart"
        holder["b"] = b
        real_snapshot = b.snapshot

        def snapshot_as_attached(_snapshot: Any = real_snapshot) -> dict[str, Any]:
            snapshot = _snapshot()
            snapshot.update(running=True, serial_alive=True, vtty_alive=True, vtty="fake-vtty")
            return snapshot

        b.snapshot = snapshot_as_attached
        probes: list[Any] = []
        waits: list[int] = []
        orig_permit = mgr._self_test_permit_locked

        def rec_permit(session: Any, nonce: str, _orig: Any = orig_permit) -> Any:
            p = _orig(session, nonce)
            probes.append(p)
            return p

        monkeypatch.setattr(mgr, "_self_test_permit_locked", rec_permit)
        orig_wait = uio.UARTBridge.wait_for_regex_from

        def count_wait(self: Any, *a: Any, _orig: Any = orig_wait, **k: Any) -> Any:
            waits.append(1)
            return _orig(self, *a, **k)

        monkeypatch.setattr(uio.UARTBridge, "wait_for_regex_from", count_wait)

        def hook(_case: str = case, _b: Any = b, _mgr: Any = mgr, _probes: list[Any] = probes) -> None:
            if _case == "drain":
                _b.begin_drain(5, preserve_consoles=False)
            elif _case == "stale_generation":
                _b.set_tx_gate(generation=_b._gate_generation + 1, quiet=False, lease_owner=None)
            elif _case == "cancelled":
                _b.revoke_ops([_probes[-1].op_id])
            else:
                _mgr._wal._unhealthy = True

        b._after_tx_authorize = hook

        class MemoryWriter:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, data: bytes) -> None:
                self.data.extend(data)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        writer = MemoryWriter()
        request = {"id": 1, "method": "session.self_test", "params": {"selector": "COM0", "timeout_s": 0.3}}
        handler = make_rpc_handler(service, lambda: None)

        async def serve() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps(request).encode("utf-8") + b"\n")
            reader.feed_eof()
            await serve_connection(reader, writer, handler, set())

        asyncio.run(serve())
        response = json.loads(writer.data.decode("utf-8").splitlines()[0])
        assert waits == [], f"{case}：零 TX 時不得等待 nonce"
        assert port.writes == [], f"{case}：不得有 TX"
        assert response["id"] == 1
        observed[case] = {
            "error_code": response.get("error_code"),
            "message": response.get("message"),
            "ok": bool(response.get("ok")),
        }

    if os.environ.get("SW_REGEN_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(golden) == {"drain", "stale_generation", "cancelled", "wal_unhealthy"}
    assert golden == observed


def test_self_test_probe_quiet_armed_after_precheck_before_submit_zero_tx(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    port = FakePort(on_write=_nonce_responder(holder))
    mgr, s, b = _ready(tmp_path, port, state="READY")
    holder["b"] = b
    assert s.boot_quiet_active() is False
    pause = _Pause(b)
    t, box = _spawn(mgr.self_test, "COM0", timeout_s=2.0)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    _join(rx, rbox, timeout=0.5)
    assert s.boot_quiet_active() is True
    assert b._gate_self_test_quiet_until == s.boot_quiet_until
    pause.resume()
    _join(t, box, timeout=0.5)  # 不得等待 nonce 的 2 s
    r = box["ret"]
    assert port.writes == []
    assert [x for x in _tx_records(mgr._wal) if x.get("source") == "system:self_test"] == []
    assert r["ok"] is True
    assert r["classification"] == "AUTOBOOT_QUIET"
    assert r["recommended_action"] == "wait"
    assert "probe_ok" not in r
    r2 = mgr.self_test("COM0", timeout_s=2.0)  # pre-check 分支
    assert list(r.keys()) == list(r2.keys())
    keys = ("ok", "classification", "recommended_action", "command_capable")
    assert {k: r[k] for k in keys} == {k: r2[k] for k in keys}
    assert port.writes == []
    assert s.state == "READY"
    assert s.ready_reconfirm_pending is True


def test_self_test_probe_console_unlock_banner_after_precheck_zero_tx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, Any] = {}
    port = FakePort(on_write=_nonce_responder(holder))
    prof = make_profile(by_id=BY_ID, console_unlock_env="SW_T20_UNLOCK")
    mgr, s, b = _ready(tmp_path, port, profile=prof, state="READY")
    holder["b"] = b
    g0 = s.login_gen
    f0 = b._gate_login_gen_floor
    guard_records = _record_guard(b, monkeypatch)
    pause = _Pause(b)
    t, box = _spawn(mgr.self_test, "COM0", timeout_s=2.0)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, b"CFE version 1.0\r\nU-Boot 2024.04\r\n")
    _join(rx, rbox, timeout=0.5)
    assert s.boot_quiet_active() is True
    assert b._gate_probe_quiet_until == 0.0  # login FSM 不被擋（同 console unlock 基線）
    assert s.login_gen == g0
    assert b._gate_login_gen_floor == f0
    assert b._gate_self_test_quiet_until == s.boot_quiet_until
    pause.resume()
    _join(t, box, timeout=0.5)
    assert "quiet" in [reason for _permit_obj, reason in guard_records]
    assert port.writes == []
    r = box["ret"]
    assert r["classification"] == "AUTOBOOT_QUIET"
    assert r["recommended_action"] == "wait"


def test_self_test_probe_quiet_armed_during_submit_counts_committed(tmp_path: Path) -> None:
    port = FakePort()
    mgr, s, b = _ready(tmp_path, port, state="READY")
    pause = _Pause(b, seam="_inside_tx_submit")
    t, box = _spawn(mgr.self_test, "COM0", timeout_s=0.3)
    pause.wait_reached()
    rx, rbox = _spawn(b._on_rx_data, BANNER)
    rx.join(0.2)
    assert rx.is_alive(), "banner 不得在提交臨界區內完成"
    assert b._gate_self_test_quiet_until == 0.0
    pause.resume()
    _join(t, box)
    _join(rx, rbox, timeout=1.0)
    assert len(port.writes) == 1
    probe = port.writes[0]
    assert probe.startswith(b"echo __READY__")  # 已提交的 probe 照實計、不宣稱撤回
    tx = [x for x in _tx_records(mgr._wal) if x.get("source") == "system:self_test"]
    assert len(tx) == 1 and tx[0]["len"] == len(probe)
    r = box["ret"]
    assert r["classification"] == "TARGET_UNRESPONSIVE"
    assert r["probe_ok"] is False
    assert s.boot_quiet_active() is True  # 未 confirm_ready()


def test_self_test_probe_permit_shape_ok_path_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    holder: dict[str, Any] = {}
    port = FakePort(on_write=_nonce_responder(holder))
    mgr, s, b = _ready(tmp_path, port, state="READY")
    holder["b"] = b
    spy = _spy_bridge(b, monkeypatch)
    g0 = s.login_gen
    f0 = b._gate_login_gen_floor
    r = mgr.self_test("COM0", timeout_s=2.0)
    assert r["classification"] == "OK"
    assert r["probe_ok"] is True
    assert s.ready_reconfirm_pending is False
    assert len(port.writes) == 1
    nonce = port.writes[0][len(b"echo __READY__") : -1].decode()
    assert port.writes == [f"echo __READY__{nonce}\n".encode()]
    sp = [p for p in spy.permits if p.tx_class == "self_test_probe"]
    assert len(sp) == 1
    p = sp[0]
    assert p.op_id == f"system:self_test:{nonce}"
    assert p.bridge_generation == s.bridge_generation
    assert p.login_gen is None
    assert s.login_gen == g0  # self-test 不推進登入世代
    assert b._gate_login_gen_floor == f0


def test_self_test_probe_not_blocked_by_reconfirm_gate(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    port = FakePort(on_write=_nonce_responder(holder))
    mgr, s, b = _ready(tmp_path, port, state="READY")
    holder["b"] = b
    s.ready_reconfirm_pending = True
    assert s.boot_quiet_until == 0.0
    b.set_tx_gate(generation=s.bridge_generation, quiet=True, lease_owner=None)  # reconfirm 期間 agent gate 為真
    with pytest.raises(_incomplete_error()) as ei:
        b.send_bytes(b"echo c1\n", source="agent:x", cmd_id="c1", permit=_permit("c1", b._gate_generation, "agent"))
    assert ei.value.result == _tx_result("not_submitted", 0, 8, "quiet")  # 對照組：agent 受 gate 擋
    r = mgr.self_test("COM0", timeout_s=2.0)
    assert r["classification"] == "OK"
    assert len(port.writes) == 1 and port.writes[0].startswith(b"echo __READY__")
    assert s.ready_reconfirm_pending is False
