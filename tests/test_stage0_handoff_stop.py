"""tests/test_stage0_handoff_stop.py — SC-11 交接停止 daemon.handoff_stop 原子交接與 admission 邊界測試。"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest

# 嘗試自 stage0_fakes 匯入；若該模組尚未由 T01 建立，提供本地替身以確保 collection 成功且紅燈為預期行為
try:
    from stage0_fakes import FakePort, make_service, wal_records
except ImportError:
    try:
        from tests.stage0_fakes import FakePort, make_service, wal_records
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

        def make_service(tmp_path: Path, ready: list[str] | None = None, port: FakePort | None = None, with_fake_mcu: bool = False) -> Any:
            from sw_core.service import SerialwrapService
            from sw_core.config import SessionProfile, UartProfile
            from sw_core.uart_io import UARTBridge
            profiles = [SessionProfile(profile_name="p", com=c, act_no=1, alias=f"lab+{i + 1}", device_by_id=f"/dev/serial/by-id/{c}", platform="prpl", uart=UartProfile()) for i, c in enumerate(ready or ["COM0"])]
            svc = SerialwrapService(profiles, state_path=str(tmp_path / "state.json"))
            svc.stop_requested = False
            if ready:
                for c in ready:
                    s = svc._sessions.get_session(c)
                    if s:
                        b = UARTBridge(com=c, device_path="fake", profile=UartProfile(), wal=svc._wal)
                        if port is not None:
                            b._serial = port
                            b._serial_fd = None
                        s.bridge = b
                        s.state = "READY"
                        s.bridge_generation = 1

            def _dispatch_for_test(req: dict[str, Any]) -> dict[str, Any]:
                method = req.get("method")
                params = req.get("params") or {}
                if method == "daemon.stop":
                    svc.stop_requested = True
                    return {"id": req.get("id", 1), "ok": True, "stopping": True}
                if method == "daemon.handoff_stop":
                    if hasattr(svc, "handoff_stop"):
                        res = svc.handoff_stop(params)
                        if res.get("ok"):
                            svc.stop_requested = True
                        return res
                    return {"id": req.get("id", 1), "ok": False, "error_code": "METHOD_NOT_FOUND"}
                return svc.rpc(method, params)

            svc.daemon_dispatch_for_test = _dispatch_for_test
            svc.session_state = lambda c: svc._sessions.get_session(c).state if svc._sessions.get_session(c) else None
            svc.session_public = lambda c: svc._sessions.get_session(c).to_public_dict() if svc._sessions.get_session(c) else None
            svc.close_for_test = lambda: svc.stop()
            svc.wal = svc._wal

            if with_fake_mcu:
                class FakeMcu:
                    def __init__(self) -> None:
                        self.received: bytes = b""

                mcu = FakeMcu()

                class FakeFlashEndpoint:
                    def sync_probe_for_test(self, sync_bytes: bytes) -> str:
                        if hasattr(svc, "admission") and svc.admission.frozen:
                            return "refused_frozen"
                        mcu.received += sync_bytes
                        return "admitted"

                svc.flash_endpoint = FakeFlashEndpoint()
                return svc, mcu

            svc.wait_until_admitted = lambda m, c, timeout=1.0: None
            return svc

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


def _rpc(svc: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return svc.daemon_dispatch_for_test({"id": 1, "method": method, "params": params or {}})


def _mutating_methods() -> list[str]:
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "rpc_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    out = [
        m for m, s in schema.get("x-methods", {}).items()
        if s.get("side_effects") and not s.get("retryable_readonly", False)
        and m not in {"daemon.stop", "daemon.handoff_stop"}
    ]
    return sorted(out) or ["command.submit", "file.push", "session.recover", "device.release"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_file_push_after_last_probe(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    assert all(not s["foreground_busy"] for s in _rpc(svc, "session.list")["sessions"])   # CLI 預檢看到閒置
    payload = b"x" * 65536
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(_rpc(svc, "file.push", {"selector": "COM0", "data_b64": payload.hex()})))
    t.start()
    svc.wait_until_admitted("file.push", "COM0", timeout=1.0)              # 預檢之後才開始的 push
    r = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
    assert (r["ok"], r["error_code"]) == (False, "SESSION_BUSY")
    assert r["busy"][0]["com"] == "COM0" and r["busy"][0]["ops"] == ["file.push"]
    assert svc.stop_requested is False and svc.admission.frozen is False
    gate.set()
    t.join(10.0)
    assert out["ok"] is True                                                           # push 完整完成，未被截斷
    assert b"".join(port.writes).count(b"x") >= len(payload)
    r2 = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
    assert r2["ok"] is True and r2["stopping"] is True and re.fullmatch(r"[0-9a-f]{32}", r2["handoff_token"])
    assert "version" not in r2 and svc.stop_requested is True


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("first", ["probe", "handoff", "race"])
def test_handoff_flash_races_prepare(tmp_path, first):
    for _ in range(200 if first == "race" else 1):
        svc, mcu = make_service(tmp_path, ready=["COM0"], with_fake_mcu=True)
        res: dict = {}
        probe = lambda: res.__setitem__("probe", svc.flash_endpoint.sync_probe_for_test(b"\x55\x55"))   # flasher 自身 sync bytes
        handoff = lambda: res.__setitem__("handoff", _rpc(svc, "daemon.handoff_stop", {"reason": "rollback"}))
        if first == "probe":
            probe()
            handoff()
            assert res["handoff"]["error_code"] == "FLASHING_BUSY"                      # 預檢後才開始的 flash 必擋下交接
        elif first == "handoff":
            handoff()
            probe()
            assert res["handoff"]["ok"] is True
        else:
            barrier = threading.Barrier(2)
            ts = [threading.Thread(target=lambda f=f: (barrier.wait(), f())) for f in (probe, handoff)]
            [x.start() for x in ts]
            [x.join(5.0) for x in ts]
        if res["handoff"]["ok"]:                                                       # 交接成立 ⇒ probe 必被拒、MCU 零 byte、未進 FLASHING
            assert res["probe"] == "refused_frozen" and mcu.received == b""
            assert svc.session_state("COM0") != "FLASHING"
        else:                                                                          # probe 先 admit ⇒ 交接必為 FLASHING_BUSY、daemon 不停止
            assert res["handoff"]["error_code"] == "FLASHING_BUSY" and res["probe"] == "admitted"
            assert svc.stop_requested is False and svc.admission.frozen is False
        svc.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_push_after_freeze_rejected_zero_tx(tmp_path):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    assert _rpc(svc, "daemon.handoff_stop")["ok"] is True
    before = len([r for r in wal_records(svc.wal) if r["dir"] == "TX"])
    r = _rpc(svc, "file.push", {"selector": "COM0", "data_b64": "eA=="})
    assert (r["ok"], r["error_code"], r["reason"]) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
    assert port.writes == [] and len([x for x in wal_records(svc.wal) if x["dir"] == "TX"]) == before
    assert svc.session_public("COM0")["foreground_busy"] is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_command_submit_races_handoff(tmp_path):
    for _ in range(10):
        port = FakePort()
        svc = make_service(tmp_path, ready=["COM0"], port=port)
        barrier = threading.Barrier(2)
        res: dict = {}
        t1 = threading.Thread(target=lambda: (barrier.wait(), res.update({"submit": _rpc(svc, "command.submit", {"selector": "COM0", "command": "echo 1"})})))
        t2 = threading.Thread(target=lambda: (barrier.wait(), res.update({"handoff": _rpc(svc, "daemon.handoff_stop", {"reason": "update"})})))
        t1.start()
        t2.start()
        t1.join(5.0)
        t2.join(5.0)
        if res.get("handoff", {}).get("ok"):
            assert res.get("submit", {}).get("error_code") == "FLUSHED_BY_SHUTDOWN"
            assert res.get("submit", {}).get("reason") == "handoff_frozen"
            assert port.writes == []
        else:
            assert res.get("handoff", {}).get("error_code") == "SESSION_BUSY"
            assert "command.submit" in res.get("handoff", {}).get("busy", [{}])[0].get("ops", [])
            assert svc.stop_requested is False


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("method", _mutating_methods())
def test_every_mutating_method_rejected_after_freeze(tmp_path, method):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    handoff_res = _rpc(svc, "daemon.handoff_stop")
    assert handoff_res.get("ok") is True
    port.writes.clear()
    state_mtime = os.path.getmtime(svc._sessions._state_path)
    r = _rpc(svc, method, {"selector": "COM0", "command": "echo 1", "data_b64": "eA=="})
    assert (r.get("ok"), r.get("error_code"), r.get("reason")) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
    assert port.writes == []
    assert os.path.getmtime(svc._sessions._state_path) == state_mtime
    assert _rpc(svc, "health.ping").get("ok") is True
    assert _rpc(svc, "session.list").get("ok") is True


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("reason", ["update", "rollback", "revert", "supervision_set"])
def test_handoff_reason_supervision_set_same_semantics(tmp_path, reason):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    r = _rpc(svc, "daemon.handoff_stop", {"reason": reason})
    assert r.get("ok") is True and r.get("stopping") is True
    assert re.fullmatch(r"[0-9a-f]{32}", r.get("handoff_token", ""))
    r_push = _rpc(svc, "file.push", {"selector": "COM0", "data_b64": "eA=="})
    assert (r_push.get("ok"), r_push.get("error_code"), r_push.get("reason")) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
    assert port.writes == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_busy_does_not_freeze(tmp_path):
    svc = make_service(tmp_path, ready=["COM0"])
    s = svc._sessions.get_session("COM0")
    s.interactive_session_id = "lease-123"
    r = _rpc(svc, "daemon.handoff_stop")
    assert r.get("ok") is False and r.get("error_code") == "SESSION_BUSY"
    assert "session.interactive_open" in r.get("busy", [{}])[0].get("ops", [])
    sub = _rpc(svc, "command.submit", {"selector": "COM0", "command": "echo 1"})
    assert sub.get("error_code") != "FLUSHED_BY_SHUTDOWN"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_recovering_flag_is_busy(tmp_path):
    svc = make_service(tmp_path, ready=["COM0"])
    s = svc._sessions.get_session("COM0")
    s.state = "RECOVERING"
    r = _rpc(svc, "daemon.handoff_stop")
    assert r.get("ok") is False and r.get("error_code") == "SESSION_BUSY"
    assert r.get("busy", [{}])[0].get("recovering") is True


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_handoff_ok_then_sc11_stop_order(tmp_path):
    svc = make_service(tmp_path, ready=["COM0"])
    r = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
    assert r.get("ok") is True and r.get("stopping") is True
    assert re.fullmatch(r"[0-9a-f]{32}", r.get("handoff_token", ""))
    assert "version" not in r
    res = svc._sessions.shutdown_drain()
    assert res == {"graceful": True, "stalled": []}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_stop_contract_merged():
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "rpc_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "daemon.handoff_stop" in schema["x-methods"]
    assert not any(p.get("adds_method") == "daemon.handoff_stop" for p in schema.get("x-pending", []))
    assert "daemon.handoff_stop" in schema.get("x-wire", {}).get("version_absent_for", [])

    error_codes_path = Path(__file__).resolve().parents[1] / "contracts" / "error_codes.yaml"
    import yaml
    codes_doc = yaml.safe_load(error_codes_path.read_text(encoding="utf-8"))
    flushed = next(e for e in codes_doc.get("error_codes", []) if e.get("code") == "FLUSHED_BY_SHUTDOWN")
    assert "rpc" in flushed.get("scope", {}).get("interfaces", [])
