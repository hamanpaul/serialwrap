"""tools/uart_stdio_bridge/tests/test_bridge_faults.py

測試 bridge 故障注入與控制動作：不確定寫入 fail-stop、磁碟滿、斷線重連世代遞增、DUT 重啟丟棄與 kill/spawn 隔離。
"""
from __future__ import annotations

import errno
import json
import os
import sys
import time
from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).resolve().parent
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))
_SRC_DIR = _TEST_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from helpers import (
    FakeEndpoint,
    StubDutProcess,
    bridge_proc,
    write_all,
)


def test_write_uncertain_fail_stop(tmp_path: Path):
    """FakeEndpoint.write_some raise EndpointWriteUncertain 觸發 fail-stop 並記錄 loss_flag:true。"""
    from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
    from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    def _raise_uncertain(data: bytes, timeout_s: float) -> int:
        raise EndpointWriteUncertain(10, "write timeout")

    endpoint = FakeEndpoint(write_fn=_raise_uncertain)
    dut = StubDutProcess()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    writer = EvidenceWriter(
        evidence_dir / "wire.ndjson", run_id="r1", case_id="c1", clock_id="clk1"
    )
    cfg = BridgeConfig(
        run_id="r1", case_id="c1", port_id="COM0", evidence_dir=evidence_dir
    )
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()
    try:
        dut.stdout_buf.extend(b"0123456789")
        exit_code = bridge.wait()
        assert exit_code == 3
        assert bridge.failure == "HARNESS_WRITE_UNCERTAIN"
        wire_lines = [
            json.loads(line)
            for line in (evidence_dir / "wire.ndjson")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        unc_records = [
            r
            for r in wire_lines
            if r.get("meta", {}).get("event") == "write_uncertain"
        ]
        assert len(unc_records) >= 1
        u = unc_records[0]
        assert u["loss_flag"] is True
        assert u["meta"]["accepted_len"] is None
        assert u["meta"]["requested_len"] == 10
    finally:
        pass


def test_evidence_disk_full_fail_stop(tmp_path: Path):
    """注入 writer os_write ENOSPC 觸發 Bridge.wait()==3 且 failure==HARNESS_DISK_FULL。"""
    from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    def _mock_os_write(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    endpoint = FakeEndpoint()
    dut = StubDutProcess()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    writer = EvidenceWriter(
        evidence_dir / "wire.ndjson",
        run_id="r1",
        case_id="c1",
        clock_id="clk1",
        os_write=_mock_os_write,
    )
    cfg = BridgeConfig(
        run_id="r1", case_id="c1", port_id="COM0", evidence_dir=evidence_dir
    )
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()
    try:
        endpoint.read_buf.extend(b"disk full trigger bytes")
        exit_code = bridge.wait()
        assert exit_code == 3
        assert bridge.failure == "HARNESS_DISK_FULL"
    finally:
        pass


@pytest.mark.posix_pty
def test_transport_disconnect_reconnect_generation(bridge_proc):
    """transport_disconnect 造成 peer EIO，reconnect 世代遞增且新世代 offset 從 0 起算。"""
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        write_all(fd, b"hello")
        res1 = b.control.call("transport_disconnect", {"remove_link": True})
        assert "endpoint_generation" in res1
        time.sleep(0.05)
        try:
            os.read(fd, 1024)
        except OSError as e:
            assert e.errno == errno.EIO
        os.close(fd)
        fd = -1

        res2 = b.control.call("transport_reconnect")
        assert res2["endpoint_generation"] == 2

        fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        write_all(fd, b"world")
        time.sleep(0.1)
    finally:
        if fd >= 0:
            os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    gen_records = [
        r
        for r in wire_lines
        if r.get("meta", {}).get("event") == "endpoint_generation"
    ]
    assert any(
        r["meta"].get("from") == 1 and r["meta"].get("to") == 2
        for r in gen_records
    )
    gen2_reads = [
        r
        for r in wire_lines
        if r.get("observation") == "serial_read"
        and r.get("endpoint_generation") == 2
    ]
    assert len(gen2_reads) > 0
    assert gen2_reads[0]["stream_offset"] == 0


@pytest.mark.posix_pty
def test_dut_restart_discard_records_fault(bridge_proc):
    """dut_restart 帶 discard 時回傳丟棄長度，META fault 標記 loss_flag:false。"""
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        b.control.call("pause_pump", {"dir": "TX"})
        write_all(fd, b"A" * 100)
        time.sleep(0.1)
        res = b.control.call("dut_restart", {"pending": "discard"})
        assert res["discarded_len"] == 100
        assert res["dut_generation"] == 2
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    fault_records = [
        r for r in wire_lines if r.get("meta", {}).get("event") == "fault"
    ]
    assert len(fault_records) >= 1
    f = fault_records[0]
    assert f["loss_flag"] is False
    assert f["meta"].get("discarded_len") == 100


@pytest.mark.posix_pty
def test_dut_kill_distinct_from_restart(bridge_proc):
    """dut_kill 標記 action:dut_kill 且 dut_generation 不變，待 dut_spawn 才加 1。"""
    b = bridge_proc()
    st1 = b.control.call("status")
    gen1 = st1["dut_generation"]

    k_res = b.control.call("dut_kill")
    assert "returncode" in k_res

    st2 = b.control.call("status")
    assert st2["dut_generation"] == gen1

    s_res = b.control.call("dut_spawn")
    assert s_res["dut_generation"] == gen1 + 1

    st3 = b.control.call("status")
    assert st3["dut_generation"] == gen1 + 1

    b.control.call("shutdown")
    assert b.wait(10) == 0

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    fault_records = [
        r for r in wire_lines if r.get("meta", {}).get("event") == "fault"
    ]
    assert any(r["meta"].get("action") == "dut_kill" for r in fault_records)


@pytest.mark.posix_pty
def test_dut_control_logical_reboot_generation(bridge_proc):
    """dut_control 觸發 logical_reboot 時記錄 dut_generation META，後續資料歸屬世代 2。"""
    b = bridge_proc(extra_dut_argv=["--mode", "model"])
    res = b.control.call(
        "dut_control",
        {"msg": {"action": "logical_reboot", "banner_delay_ms": 0}},
    )
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        write_all(fd, b"post-reboot-command\n")
        time.sleep(0.2)
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    gen_records = [
        r
        for r in wire_lines
        if r.get("meta", {}).get("event") == "dut_generation"
    ]
    assert any(r["meta"].get("reason") == "logical_reboot" for r in gen_records)
    data_records = [
        r
        for r in wire_lines
        if r.get("observation") in ("serial_read", "dut_stdin_write")
    ]
    assert any(r.get("dut_generation") == 2 for r in data_records)


def test_capability_unavailable_on_win_endpoint(tmp_path: Path):
    """FakeEndpoint(capabilities 全 False) 時 transport_disconnect 回 HARNESS_CAPABILITY_UNAVAILABLE。"""
    from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    endpoint = FakeEndpoint(
        capabilities={
            "transport_disconnect": False,
            "by_id_link": False,
            "peer_state": False,
        }
    )
    dut = StubDutProcess()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    writer = EvidenceWriter(
        evidence_dir / "wire.ndjson", run_id="r1", case_id="c1", clock_id="clk1"
    )
    cfg = BridgeConfig(
        run_id="r1", case_id="c1", port_id="COM0", evidence_dir=evidence_dir
    )
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()
    try:
        res = bridge.handle_action("transport_disconnect", {}, "act_disc")
        assert res.get("ok") is False
        assert res.get("error") == "HARNESS_CAPABILITY_UNAVAILABLE"
    finally:
        bridge.handle_action("shutdown", {}, "act_shutdown")
        bridge.wait()
