"""tests/test_p1oc_drain_stalled.py — Task P1-OC-T4「DRAIN_STALLED：release drain 逾時新錯誤碼」失敗測試（TDD 紅燈）。

依據：
- `docs/rust-refactor/plans/02b-stage0-owner-confirmations.md` Task T4（A1 失敗測試）
- `docs/rust-refactor/owner-decisions-2026-10-15.md` OD-14 #4
- `docs/rust-refactor/plans/spec-reconciliation.md` SC-04、SES-16.3a
- `docs/rust-refactor/plans/rust-architecture.md` §5.5

同檔混合平台（P0 T14 規則 2）：
- 頂層 core_portable 測試：各帶 @pytest.mark.core_portable 與 @pytest.mark.memory_only
- TestWindowsNativeDrainStalled：class 層帶 pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com]
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any

import pytest
from tests.stage0_fakes import FakePort

ROOT = Path(__file__).resolve().parents[1]
BY_ID = "/dev/serial/by-id/X"


@pytest.fixture
def short_drain(monkeypatch: Any) -> None:
    """縮短 drain 逾時常數至 150 ms 以加速測試。"""
    import sw_core.constants as const
    import sw_core.session_manager as sm_mod
    monkeypatch.setattr(const, "DRAIN_TIMEOUT_MS", 150, raising=False)
    monkeypatch.setattr(sm_mod, "DRAIN_TIMEOUT_MS", 150, raising=False)


def _ready_with_port(tmp_path: Path, port: Any) -> tuple[Any, Any, Any]:
    """建立帶有指定 port 的 READY session 管理器與 bridge。"""
    from sw_core.config import SessionProfile, UartProfile
    from sw_core.session_manager import SessionManager
    from sw_core.uart_io import UARTBridge
    from sw_core.wal import WalWriter

    profile = SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id=BY_ID,
        platform="prpl",
        uart=UartProfile(),
    )
    wal = WalWriter(wal_dir=str(tmp_path / "wal"))
    mgr = SessionManager(
        [profile],
        wal,
        on_ready=lambda _s: None,
        on_detached=lambda _s: None,
        state_path=str(tmp_path / "state.json"),
    )
    s = mgr.get_session("COM0")
    b = UARTBridge(com="COM0", device_path="fake", profile=UartProfile(), wal=wal)
    b._serial = port
    b._serial_fd = None
    with mgr._lock:
        s.bridge = b
        s.state = "READY"
        s.bridge_generation = 4
    return mgr, s, b


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_release_timeout_returns_drain_stalled(tmp_path: Path, short_drain: Any) -> None:
    """release drain 逾時回傳 DRAIN_STALLED，維持 RECOVERING 且去重。"""
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    r = mgr.release_device("COM0")
    assert list(r.keys()) == ["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"]
    assert (r["ok"], r["error_code"], r["drain_stalled"], r["drain_timeout_epoch"]) == (False, "DRAIN_STALLED", True, 4)
    assert r["session"]["state"] == "RECOVERING" and r["session"]["drain_stalled"] is True
    r2 = mgr.release_device("COM0")
    assert r2["error_code"] == "DRAIN_STALLED" and port.close_calls == 0
    gate.set()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_attach_during_drain_still_session_recovering(tmp_path: Path, short_drain: Any) -> None:
    """drain 進行中（包含 stalled）的 attach 維持回傳 SESSION_RECOVERING（回歸防護）。"""
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    assert mgr.release_device("COM0")["error_code"] == "DRAIN_STALLED"
    a = mgr.attach_device("COM0")
    assert a["ok"] is False and a["error_code"] == "SESSION_RECOVERING" and a["drain_stalled"] is True
    gate.set()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_late_receipt_completes_release(tmp_path: Path, short_drain: Any) -> None:
    """逾時後延遲回條抵達，daemon 自動完成 release 並持久化，後續 release 回 already_released。"""
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    assert mgr.release_device("COM0")["error_code"] == "DRAIN_STALLED"
    gate.set()
    for _ in range(100):
        if mgr.get_session("COM0").state == "RELEASED":
            break
        time.sleep(0.02)
    assert mgr.get_session("COM0").state == "RELEASED"
    assert mgr.release_device("COM0")["already_released"] is True and port.close_calls == 1


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_close_failure_returns_drain_stalled(tmp_path: Path) -> None:
    """FakePort.close 擲 OSError 視為關閉失敗，回 DRAIN_STALLED 且維持 RECOVERING，attach 不開新 handle。"""
    class ErrorClosePort(FakePort):
        def close(self):
            super().close()
            raise OSError("close failed")

    port = ErrorClosePort()
    mgr, s, b = _ready_with_port(tmp_path, port)
    r = mgr.release_device("COM0")
    assert r["ok"] is False and r["error_code"] == "DRAIN_STALLED"
    assert mgr.get_session("COM0").state == "RECOVERING"
    assert getattr(mgr.get_session("COM0"), "drain_stalled", False) is True
    att = mgr.attach_device("COM0")
    assert att["ok"] is False and att["error_code"] == "SESSION_RECOVERING"
    assert port.close_calls >= 1


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_duplicate_release_before_deadline_shares_deadline(tmp_path: Path, short_drain: Any) -> None:
    """drain 期限內並行重複 release 共用同一期限，不會各自延長等候時間，close 只發一次。"""
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)

    res1: dict[str, Any] = {}
    res2: dict[str, Any] = {}
    dur2: list[float] = [0.0]

    def call1():
        res1.update(mgr.release_device("COM0"))

    def call2():
        time.sleep(0.05)
        t0 = time.monotonic()
        res2.update(mgr.release_device("COM0"))
        dur2[0] = time.monotonic() - t0

    t1 = threading.Thread(target=call1)
    t2 = threading.Thread(target=call2)
    t1.start()
    t2.start()
    t1.join(2.0)
    t2.join(2.0)

    assert res1.get("error_code") == "DRAIN_STALLED"
    assert res2.get("error_code") == "DRAIN_STALLED"
    assert dur2[0] < 0.25, f"duplicate release took {dur2[0]:.3f}s >= 0.25s"
    assert port.close_calls == 0
    gate.set()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_clear_bind_recover_during_drain_session_recovering(tmp_path: Path, short_drain: Any) -> None:
    """stalled 狀態下 clear_session、bind_session、recover_session(force=False) 各回 SESSION_RECOVERING＋drain 欄位。"""
    gate = threading.Event()
    port = FakePort(block=gate)
    mgr, s, b = _ready_with_port(tmp_path, port)
    threading.Thread(target=lambda: b.send_bytes(b"abc", source="agent:x"), daemon=True).start()
    time.sleep(0.05)
    assert mgr.release_device("COM0")["error_code"] == "DRAIN_STALLED"

    c = mgr.clear_session("COM0")
    assert c["ok"] is False and c["error_code"] == "SESSION_RECOVERING"
    assert c["drain_stalled"] is True and c["drain_timeout_epoch"] == 4

    bi = mgr.bind_session("COM0", "/dev/serial/by-id/new")
    assert bi["ok"] is False and bi["error_code"] == "SESSION_RECOVERING"
    assert bi["drain_stalled"] is True and bi["drain_timeout_epoch"] == 4

    rec = mgr.recover_session("COM0", force=False)
    assert rec["ok"] is False and rec["error_code"] == "SESSION_RECOVERING"
    assert rec["drain_stalled"] is True and rec["drain_timeout_epoch"] == 4

    gate.set()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_drain_stalled_catalog_entry() -> None:
    """契約驗證：error_codes.yaml 含 DRAIN_STALLED，且 safety_invariants 包含 DRAIN_STALLED。"""
    from tools.contracts import catalog

    codes, _ = catalog.load_error_codes(ROOT)
    entry = next((c for c in codes if c.code == "DRAIN_STALLED"), None)
    assert entry is not None, "contracts/error_codes.yaml 缺少 DRAIN_STALLED 條目"
    assert entry.retryable == "no"
    assert entry.uart_tx == "possible"
    assert entry.scope.methods == ("device.release",)
    assert entry.behavior_change == ("SC-04",)

    _, _, invariants = catalog.load_behavior_changes(ROOT)
    inv_map = {inv.id: inv for inv in invariants}
    assert "DRAIN_STALLED" in inv_map["no_auto_resend"].error_codes
    assert "DRAIN_STALLED" in inv_map["no_reopen_before_handoff"].error_codes


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_device_release_drain_stalled_exit(capsys: Any, monkeypatch: Any) -> None:
    """CLI serialwrap device release 遇到 DRAIN_STALLED RPC 回應時正確輸出 stdout JSON 並回傳 exit code 2。"""
    from sw_core import cli
    import sw_core.constants as const

    assert getattr(const, "ERROR_DRAIN_STALLED", None) == "DRAIN_STALLED", "sw_core.constants 缺少 ERROR_DRAIN_STALLED 常數"
    fake_resp = {
        "ok": False,
        "error_code": "DRAIN_STALLED",
        "drain_stalled": True,
        "drain_timeout_epoch": 4,
        "session": {"state": "RECOVERING", "drain_stalled": True},
    }
    monkeypatch.setattr(cli, "_rpc_call_traced", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: cli._RuntimeConfig())
    monkeypatch.setattr(
        cli,
        "_resolve_local_endpoint_info",
        lambda _args: cli._ResolvedEndpoint("/tmp/fake.sock", "config.yaml"),
    )

    rc = cli.main(["device", "release", "--selector", "COM0"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error_code"] == "DRAIN_STALLED"
    assert payload["ok"] is False
    assert payload["drain_stalled"] is True
    assert rc == 2


class TestWindowsNativeDrainStalled:
    pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com]

    def test_windows_guarded_write_stall_drain_stalled_native(self, tmp_path: Path, monkeypatch: Any) -> None:
        """OD-15 原生 Windows COM20 寫入卡住時，release 在時限內回 DRAIN_STALLED，回條前獨占拒絕二次開啟，解除後 RELEASED 且可重開。"""
        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR 未設定")
        com_a, com_b = [c.strip() for c in com_pair.split(":", 1)]

        from sw_core import serial_port as sp
        from sw_core.config import SessionProfile, UartProfile
        from sw_core.session_manager import SessionManager
        from sw_core.uart_io import UARTBridge
        from sw_core.wal import WalWriter

        monkeypatch.setenv("SERIALWRAP_TEST_DEVICE_ALLOWLIST", com_a)
        wal = WalWriter(wal_dir=str(tmp_path / "wal"))
        profile = SessionProfile(
            profile_name="p",
            com=com_a,
            act_no=1,
            alias="lab+1",
            device_by_id=f"COM_{com_a}",
            platform="prpl",
            uart=UartProfile(),
        )
        mgr = SessionManager(
            [profile],
            wal,
            on_ready=lambda _s: None,
            on_detached=lambda _s: None,
            state_path=str(tmp_path / "state.json"),
        )
        mgr._devices[f"COM_{com_a}"] = com_a

        port = sp.open_serial_port(com_a, UartProfile())
        port.open()
        b = UARTBridge(com=com_a, device_path=com_a, profile=UartProfile(), wal=wal)
        b._serial = port
        b._serial_fd = None
        s = mgr.get_session(com_a)
        with mgr._lock:
            s.bridge = b
            s.state = "READY"
            s.bridge_generation = 4

        peer = sp.open_serial_port(com_b, UartProfile())
        peer.open()
        try:
            write_done = threading.Event()

            def do_write():
                try:
                    while not write_done.is_set():
                        b.send_bytes(b"X" * 4096, source="agent:x")
                except Exception:
                    pass

            wt = threading.Thread(target=do_write, daemon=True)
            wt.start()
            time.sleep(0.1)

            t0 = time.monotonic()
            r = mgr.release_device(com_a)
            el = time.monotonic() - t0

            assert el < 3.5, f"release 耗時 {el:.2f}s >= 3.5s"
            assert r["error_code"] == "DRAIN_STALLED"
            assert r["drain_stalled"] is True

            p_dup = sp._PySerialPort(com_a, UartProfile())
            with pytest.raises(sp.DeviceHeldError) as ei:
                p_dup.open()
            assert ei.value.holder.holder_unknown is True

            write_done.set()
            peer.read(65536)

            for _ in range(100):
                if mgr.get_session(com_a).state == "RELEASED":
                    break
                time.sleep(0.05)
            assert mgr.get_session(com_a).state == "RELEASED"

            p_ext = sp.open_serial_port(com_a, UartProfile())
            p_ext.open()
            p_ext.close()
        finally:
            peer.close()
            try:
                port.close()
            except Exception:
                pass
