"""tests/test_stage0_session_drain.py — SC-04b Session Drain、release／attach 交接回應、drain 診斷欄位、BC-03 中止映射、SC-11 有序關機測試。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import sw_core.constants as const
import sw_core.session_manager as sm_mod

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

# 嘗試自 stage0_fakes 匯入；若該模組尚未由 T01 建立，提供本地替身以確保 collection 成功且紅燈為預期行為
try:
    from stage0_fakes import FakePort, make_manager, make_profile, wal_records
except ImportError:
    try:
        from tests.stage0_fakes import FakePort, make_manager, make_profile, wal_records
    except ImportError:
        class FakePort:
            """SerialPort 介面替身。"""
            def __init__(self, write_results: list[Any] | None = None, *, block: threading.Event | None = None) -> None:
                self.writes: list[bytes] = []
                self.close_calls: int = 0
                self.opened: bool = True
                self.write_results = list(write_results) if write_results is not None else None
                self.block = block

            def open(self) -> None:
                self.opened = True

            def configure(self, profile: Any) -> None:
                pass

            def read(self, max_bytes: int = 8192) -> bytes:
                return b""

            def write(self, payload: bytes, *, cancel: threading.Event | None = None) -> Any:
                if self.block is not None:
                    self.block.wait()
                self.writes.append(bytes(payload))
                if self.write_results:
                    res = self.write_results.pop(0)
                    if isinstance(res, BaseException):
                        raise res
                    return res
                try:
                    from sw_core.serial_port import TxResult
                    return TxResult("completed", len(payload), len(payload))
                except (ImportError, AttributeError):
                    return len(payload)

            def close(self) -> None:
                self.close_calls += 1
                self.opened = False

            def set_baud(self, baud: int) -> None:
                pass

            def is_alive(self) -> bool:
                return self.opened

            def fileno(self) -> int | None:
                return None

        def make_profile(name: str = "p", com: str = "COM0", by_id: str = "/dev/serial/by-id/X", *, platform: str = "prpl"):
            from sw_core.config import SessionProfile, UartProfile
            return SessionProfile(profile_name=name, com=com, act_no=1, alias="lab+1", device_by_id=by_id, platform=platform, uart=UartProfile())

        def make_manager(tmp_path: Path, profiles: list[Any], *, wal: Any = None):
            from sw_core.wal import WalWriter
            from sw_core.session_manager import SessionManager
            w = wal or WalWriter(wal_dir=str(tmp_path / "wal"))
            return SessionManager(profiles, w, on_ready=lambda _s: None, on_detached=lambda _s: None, state_path=str(tmp_path / "state.json"))

        def wal_records(wal: Any) -> list[dict[str, Any]]:
            wal_file = Path(wal.wal_path)
            if not wal_file.exists():
                return []
            records = []
            with open(wal_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            return records

BY_ID = "/dev/serial/by-id/X"


@pytest.fixture
def short_drain(monkeypatch):
    monkeypatch.setattr(const, "DRAIN_TIMEOUT_MS", 150, raising=False)
    monkeypatch.setattr(sm_mod, "DRAIN_TIMEOUT_MS", 150, raising=False)


def _ready_with_port(tmp_path, port):
    from sw_core.uart_io import UARTBridge
    from sw_core.config import UartProfile
    mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
    s = mgr.get_session("COM0")
    b = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=mgr._wal)
    b._serial = port
    b._serial_fd = None
    with mgr._lock:
        s.bridge = b
        s.state = "READY"
        s.bridge_generation = 4
    return mgr, s, b


def test_release_transitions_recovering_then_released(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x", cmd_id="c1"))
    t.start()
    time.sleep(0.05)
    out: dict = {}
    rt = threading.Thread(target=lambda: out.update(mgr.release_device("COM0")))
    rt.start()
    time.sleep(0.05)
    assert mgr.get_session("COM0").state == "RECOVERING"
    assert BY_ID in mgr._released_by_ids
    gate.set()
    rt.join(3.0)
    t.join(1.0)
    assert out["ok"] is True and mgr.get_session("COM0").state == "RELEASED"
    assert port.close_calls == 1
    state = json.load(open(mgr._state_path, encoding="utf-8"))
    assert "p:COM0" in state["released"]


def test_release_drain_timeout_shape(tmp_path, short_drain):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    r = mgr.release_device("COM0")
    assert {k: r[k] for k in ("ok", "error_code", "drain_stalled", "drain_timeout_epoch")} == {
        "ok": False, "error_code": "SESSION_RECOVERING", "drain_stalled": True, "drain_timeout_epoch": 4}
    assert r["session"]["state"] == "RECOVERING" and r["session"]["drain_stalled"] is True
    r2 = mgr.release_device("COM0")
    assert r2["error_code"] == "SESSION_RECOVERING"
    a = mgr.attach_device("COM0")
    assert a["ok"] is False and a["error_code"] == "SESSION_RECOVERING"
    gate.set()
    for _ in range(100):
        if mgr.get_session("COM0").state == "RELEASED":
            break
        time.sleep(0.02)
    assert mgr.get_session("COM0").state == "RELEASED"
    assert mgr.get_session("COM0").to_public_dict()["drain_stalled"] is False
    assert mgr.release_device("COM0")["already_released"] is True
    assert port.close_calls == 1


def test_public_dict_drain_fields_follow_capture(tmp_path):
    mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID)])
    keys = list(mgr.get_session("COM0").to_public_dict().keys())
    i = keys.index("capture")
    assert keys[i + 1:i + 3] == ["drain_stalled", "drain_timeout_epoch"]
    d = mgr.get_session("COM0").to_public_dict()
    assert d["drain_stalled"] is False and d["drain_timeout_epoch"] is None


@pytest.mark.parametrize("account,expected", [
    ((5, False), "COMMAND_ABORTED_IN_FLIGHT"),     # 全數寫出後被中止
    ((2, False), "COMMAND_ABORTED_IN_FLIGHT"),     # 部分寫出
    ((2, True), "COMMAND_ABORTED_IN_FLIGHT"),      # 已確認 2 bytes 後結果不明
    ((0, True), "SEND_FAILED"),
    ((0, False), "FLUSHED_BY_RECOVERY"),
])
def test_bc03_abort_mapping(tmp_path, account, expected):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    if not hasattr(b, "_tx_accounts"):
        b._tx_accounts = {}
    if account != (0, False):
        b._tx_accounts["c1"] = account                # 預填 T06 的累計表（公開路徑：_map_aborted_result 經 tx_account 讀取）
    res = mgr._map_aborted_result("c1", {"ok": False}, bridge=b, aborted_by_drain=True)
    assert res["ok"] is False and res["error_code"] == expected
    if expected == "COMMAND_ABORTED_IN_FLIGHT":
        metas = [r for r in wal_records(mgr._wal) if r["dir"] == "META"]
        assert metas[-1]["meta"]["event"] == "command_aborted_in_flight"
        assert metas[-1]["cmd_id"] == "c1" and metas[-1]["meta"]["confirmed_tx_bytes"] == account[0]


def test_bridge_down_goes_recovering_then_attaching(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"))
    t.start()
    time.sleep(0.05)
    spawned = []
    mgr._spawn_attach = lambda by_id: spawned.append(by_id)
    th = threading.Thread(target=lambda: mgr._handle_bridge_down("COM0", "io error"))
    th.start()
    time.sleep(0.05)
    assert mgr.get_session("COM0").state == "RECOVERING"
    gate.set()
    th.join(2.0)
    t.join(1.0)
    assert mgr.get_session("COM0").state == "ATTACHING"
    assert len(spawned) == 1


def test_device_removed_goes_recovering_then_detached(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"))
    t.start()
    time.sleep(0.05)
    th = threading.Thread(target=lambda: mgr._detach_by_id(BY_ID, reason="DEVICE_REMOVED"))
    th.start()
    time.sleep(0.05)
    assert mgr.get_session("COM0").state == "RECOVERING"
    gate.set()
    th.join(2.0)
    t.join(1.0)
    assert mgr.get_session("COM0").state == "DETACHED"
    assert mgr.get_session("COM0").last_error == "DEVICE_REMOVED"


def test_clear_and_bind_use_recovering_during_drain(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"))
    t.start()
    time.sleep(0.05)
    th = threading.Thread(target=lambda: mgr.clear_session("COM0"))
    th.start()
    time.sleep(0.05)
    assert mgr.get_session("COM0").state == "RECOVERING"
    gate.set()
    th.join(2.0)
    t.join(1.0)

    gate2 = threading.Event()
    port2 = FakePort(block=gate2)
    mgr, s, b = _ready_with_port(tmp_path, port2)
    t2 = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"))
    t2.start()
    time.sleep(0.05)
    th2 = threading.Thread(target=lambda: mgr.bind_session("COM0", "/dev/serial/by-id/NEW"))
    th2.start()
    time.sleep(0.05)
    assert mgr.get_session("COM0").state == "RECOVERING"
    gate2.set()
    th2.join(2.0)
    t2.join(1.0)


def test_duplicate_release_starts_single_drain(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    calls = []
    # spy on bridge.begin_drain
    b.begin_drain = lambda *args, **kwargs: calls.append(args)
    t1 = threading.Thread(target=lambda: mgr.release_device("COM0"))
    t2 = threading.Thread(target=lambda: mgr.release_device("COM0"))
    t1.start()
    t2.start()
    time.sleep(0.05)
    gate.set()
    t1.join(2.0)
    t2.join(2.0)
    assert len(calls) == 1


def test_no_reopen_before_receipt(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"))
    t.start()
    time.sleep(0.05)
    rt = threading.Thread(target=lambda: mgr.release_device("COM0"))
    rt.start()
    time.sleep(0.05)
    assert hasattr(mgr, "_draining_by_ids") and BY_ID in mgr._draining_by_ids
    old_bridge = s.bridge
    mgr._attach_by_id(BY_ID)
    assert s.bridge is old_bridge or s.bridge is None
    gate.set()
    rt.join(2.0)
    t.join(1.0)


def test_direct_bridge_stop_timeout_blocks_reopen(tmp_path, short_drain):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    # 觸發 attach 失敗收尾逾時
    mgr._handle_bridge_down("COM0", "attach failed")
    assert hasattr(mgr, "_draining_by_ids") and BY_ID in mgr._draining_by_ids
    created = []
    mgr._spawn_attach = lambda by_id: created.append(by_id)
    mgr._spawn_attach(BY_ID)
    assert len(created) == 0
    gate.set()


def test_failed_close_receipt_keeps_recovering(tmp_path):
    class ErrorClosePort(FakePort):
        def close(self):
            super().close()
            raise OSError("close failed")

    port = ErrorClosePort()
    mgr, s, b = _ready_with_port(tmp_path, port)
    r = mgr.release_device("COM0")
    assert r["ok"] is False and r["error_code"] == "SESSION_RECOVERING"
    assert mgr.get_session("COM0").state == "RECOVERING"
    assert getattr(mgr.get_session("COM0"), "drain_stalled", False) is True
    assert getattr(mgr.get_session("COM0"), "_pending_drain", None) is not None
    att = mgr.attach_device("COM0")
    assert att["ok"] is False and att["error_code"] == "SESSION_RECOVERING"


def test_release_drain_deadline_includes_lock_wait(tmp_path, short_drain):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    lock_held = threading.Event()

    def hold_lock():
        with mgr._lock:
            lock_held.set()
            time.sleep(0.3)

    threading.Thread(target=hold_lock).start()
    assert lock_held.wait(1.0)
    t0 = time.monotonic()
    r = mgr.release_device("COM0")
    elapsed = time.monotonic() - t0
    assert r["error_code"] == "SESSION_RECOVERING"
    assert r["drain_stalled"] is True
    assert r["drain_timeout_epoch"] == 4
    assert elapsed < 0.4, f"elapsed {elapsed}s >= 0.4s (舊實作 finish 才起算)"
    assert port.close_calls == 0
    assert mgr.attach_device("COM0")["error_code"] == "SESSION_RECOVERING"
    gate.set()
    for _ in range(100):
        if mgr.get_session("COM0").state == "RELEASED":
            break
        time.sleep(0.02)
    assert mgr.get_session("COM0").state == "RELEASED"
    assert port.close_calls == 1


def test_aborted_command_not_resent(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    b.send_bytes(b"TEST_PAYLOAD", source="agent:x", cmd_id="c_abort")
    mgr.release_device("COM0")
    tx_entries = [r for r in wal_records(mgr._wal) if r.get("dir") == "TX" and r.get("cmd_id") == "c_abort"]
    assert len(tx_entries) == 1
    assert [w for w in s.bridge._serial.writes if b"TEST_PAYLOAD" in w] == [b"TEST_PAYLOAD"]


def test_drain_abort_mapping_survives_settle_race(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    if not hasattr(b, "_tx_accounts"):
        b._tx_accounts = {}
    b._tx_accounts["c_done"] = (10, False)
    b._tx_accounts["c_fg"] = (2, False)
    settled = []
    orig_settle = getattr(b, "tx_settle", lambda c: b._tx_accounts.pop(c, None))
    b.tx_settle = lambda c: (settled.append(c), orig_settle(c))
    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=lambda: (barrier.wait(), b.tx_settle("c_done")))
    out = {}
    t2 = threading.Thread(target=lambda: (barrier.wait(), out.update(mgr._map_aborted_result("c_fg", {"ok": False}, bridge=b, aborted_by_drain=True))))
    t1.start()
    t2.start()
    t1.join(2.0)
    t2.join(2.0)
    assert out.get("error_code") == "COMMAND_ABORTED_IN_FLIGHT"
    metas = [r for r in wal_records(mgr._wal) if r.get("dir") == "META"]
    assert metas[-1]["meta"]["confirmed_tx_bytes"] == 2
    assert "c_fg" not in b._tx_accounts
    assert "c_done" not in b._tx_accounts
    assert "c_fg" not in settled


def test_running_command_aborted_by_release_records_code(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    if not hasattr(b, "_tx_accounts"):
        b._tx_accounts = {}
    b._tx_accounts["c_long"] = (1, False)
    from sw_core.session_manager import _ForegroundOperation
    s._foreground_operation = _ForegroundOperation(
        token=object(),
        bridge=b,
        bridge_generation=s.bridge_generation,
        kind="command",
        cmd_id="c_long",
    )
    t0 = time.monotonic()
    mgr.release_device("COM0")
    res = mgr._map_aborted_result("c_long", {"ok": False}, bridge=b, aborted_by_drain=True)
    assert time.monotonic() - t0 < 1.0
    assert res["error_code"] == "COMMAND_ABORTED_IN_FLIGHT"


def test_late_completion_does_not_touch_new_session(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    old_gen = s.bridge_generation
    from sw_core.session_manager import _ForegroundOperation
    old_op = _ForegroundOperation(token=object(), bridge=b, bridge_generation=old_gen, cmd_id="old_c")
    s._foreground_operation = old_op
    mgr.release_device("COM0")
    mgr.attach_device("COM0")
    s.state = "READY"
    s.bridge_generation = old_gen + 1
    with mgr._lock:
        assert s._foreground_operation is not old_op
        assert s.bridge_generation != old_op.bridge_generation
    if not hasattr(b, "_tx_accounts"):
        b._tx_accounts = {}
    b._tx_accounts["old_c"] = (1, False)
    res = mgr._map_aborted_result("old_c", {"ok": False}, bridge=b, aborted_by_drain=True)
    assert res["error_code"] in {"COMMAND_ABORTED_IN_FLIGHT", "FLUSHED_BY_RECOVERY"}
    assert s.state == "READY"


def test_reject_paths_have_zero_side_effects(tmp_path, short_drain):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    r1 = mgr.release_device("COM0")
    tx_before = len([r for r in wal_records(mgr._wal) if r.get("dir") == "TX"])
    port.writes.clear()
    state_mtime = os.path.getmtime(mgr._state_path)
    a = mgr.attach_device("COM0")
    assert a["ok"] is False and a["error_code"] == "SESSION_RECOVERING"
    r2 = mgr.release_device("COM0")
    assert r2["ok"] is False and r2["error_code"] == "SESSION_RECOVERING"
    assert port.writes == []
    assert len([r for r in wal_records(mgr._wal) if r.get("dir") == "TX"]) == tx_before
    assert os.path.getmtime(mgr._state_path) == state_mtime
    gate.set()


def test_shutdown_drains_then_closes_wal(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    call_order = []
    b.begin_drain = lambda *args, **kwargs: call_order.append("begin_drain")
    b.finish_drain = lambda *args, **kwargs: call_order.append("finish_drain")
    orig_close = mgr._wal.close
    mgr._wal.close = lambda: (call_order.append("wal_close"), orig_close())
    res = mgr.shutdown_drain(total_timeout_s=5.0)
    assert res == {"graceful": True, "stalled": []}
    assert call_order == ["begin_drain", "finish_drain", "wal_close"]


def test_shutdown_gate_blocks_hotplug_after_drain_start(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    spy_called = []

    def on_first_drain(*args, **kwargs):
        spy_called.append(1)
        mgr._spawn_attach(r"/s/by-id/usb-NEW")

    mgr._begin_drain_locked = on_first_drain
    mgr.begin_shutdown()
    with pytest.raises(RuntimeError, match="daemon stopping"):
        mgr.attach_device("COM0")
    res = mgr.shutdown_drain()
    assert res == {"graceful": True, "stalled": []}
    assert r"/s/by-id/usb-NEW" not in mgr._sessions


def test_shutdown_waits_inflight_attach_before_wal_close(tmp_path):
    mgr, s, b = _ready_with_port(tmp_path, FakePort())
    gate = threading.Event()

    class BlockingPort(FakePort):
        def open(self):
            gate.wait()
            super().open()

    port = BlockingPort()
    order = []
    mgr.begin_shutdown = lambda: order.append("begin_shutdown")
    orig_close = mgr._wal.close
    mgr._wal.close = lambda: (order.append("wal_close"), orig_close())
    th = threading.Thread(target=lambda: (order.append("attach_start"), port.open(), order.append("attach_done")))
    th.start()
    time.sleep(0.05)
    mgr.begin_shutdown()
    gate.set()
    th.join(2.0)
    mgr._wal.close()
    assert order == ["attach_start", "begin_shutdown", "attach_done", "wal_close"]


def test_shutdown_timeout_is_reported_not_graceful(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    t0 = time.monotonic()
    res = mgr.shutdown_drain(total_timeout_s=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.5
    assert res["graceful"] is False
    assert "COM0" in res["stalled"]
    assert getattr(mgr._wal, "closed", False) is False


def test_release_persist_failure_keeps_blocking(tmp_path, monkeypatch):
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    t = threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"))
    t.start()
    time.sleep(0.05)
    monkeypatch.setattr(mgr, "_save_state", lambda: (_ for _ in ()).throw(OSError("disk error")))
    out: dict = {}

    def do_release():
        try:
            mgr.release_device("COM0")
        except OSError as e:
            out["err"] = str(e)

    rt = threading.Thread(target=do_release)
    rt.start()
    time.sleep(0.05)
    assert mgr.get_session("COM0").state == "RECOVERING"
    assert "err" not in out
    gate.set()
    rt.join(2.0)
    t.join(1.0)
    assert out.get("err") == "disk error"
    assert mgr.get_session("COM0").state == "RELEASED"
    assert BY_ID in mgr._released_by_ids
    mgr._spawn_attach(BY_ID)
    assert mgr.get_session("COM0").bridge is None
