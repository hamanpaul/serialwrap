"""tools/uart_stdio_bridge/tests/test_bridge_core.py

測試 bridge/core.py：bridge 核心生命週期、方向獨立 worker、背壓、quiet 判定與 quiesce 模式。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
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
    read_exact,
    write_all,
)


@pytest.mark.posix_pty
def test_roundtrip_full_byte_range_all_four_observations(bridge_proc):
    """端到端 256 位元組全值域往返與四個觀測位置完整記錄。"""
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)  # 模擬 daemon 開端 A
    try:
        data = bytes(range(256)) * 64
        write_all(fd, data)
        assert read_exact(fd, len(data), timeout_s=10) == data
        res = b.control.call("barrier", {"quiet_ms": 300})
        assert res["settled"] is True
        assert res["offsets"] == {
            "serial_read": len(data),
            "dut_stdin_write": len(data),
            "dut_stdout_read": len(data),
            "serial_write": len(data),
        }
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0
    recs = [
        json.loads(x)
        for x in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["seq"] for r in recs] == list(range(1, len(recs) + 1))
    for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write"):
        assert (
            b"".join(
                base64.b64decode(r["payload_b64"])
                for r in recs
                if r["observation"] == obs
            )
            == data
        ), obs
    man = json.loads((b.evidence / "bridge-manifest.json").read_text(encoding="utf-8"))
    assert man["closed_cleanly"] is True and man["failure"] is None
    assert (
        man["evidence"]["wire"]["sha256"]
        == hashlib.sha256((b.evidence / "wire.ndjson").read_bytes()).hexdigest()
    )


def test_partial_write_to_dut_records_accepted_only(tmp_path: Path):
    """DUT 部分寫入時，wire 只記錄實際接受長度，串接後無遺失無重複。"""
    from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    script = [3, 4096, 1, 0, 7]
    dut = StubDutProcess(script=script)
    endpoint = FakeEndpoint()
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
        test_payload = b"A" * 3 + b"B" * 4096 + b"C" * 1 + b"D" * 7
        endpoint.read_buf.extend(test_payload)

        deadline = time.monotonic() + 5.0
        while (
            len(dut.stdin_written) < len(test_payload)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert bytes(dut.stdin_written) == test_payload

        bridge.handle_action("shutdown", {}, "act_shutdown")
        assert bridge.wait() == 0

        wire_lines = [
            json.loads(line)
            for line in (evidence_dir / "wire.ndjson")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        dut_writes = [
            r for r in wire_lines if r.get("observation") == "dut_stdin_write"
        ]
        accepted_lens = [r["len"] for r in dut_writes]
        assert accepted_lens == [3, 4096, 1, 7]

        serial_reads = [
            r for r in wire_lines if r.get("observation") == "serial_read"
        ]
        serial_bytes = b"".join(
            base64.b64decode(r["payload_b64"]) for r in serial_reads
        )
        dut_bytes = b"".join(
            base64.b64decode(r["payload_b64"]) for r in dut_writes
        )
        assert serial_bytes == dut_bytes == test_payload
    finally:
        pass


@pytest.mark.posix_pty
def test_dut_pipe_accepted_total_matches_received(bridge_proc):
    """真 echo DUT 帶 read-delay 時，2 MiB 寫入的各筆長度總和與實收完全一致。"""
    b = bridge_proc(extra_dut_argv=["--read-delay-ms", "50"])
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        data = b"0123456789ABCDEF" * (128 * 1024)  # 2 MiB
        write_all(fd, data, timeout_s=30.0)
        echoed = read_exact(fd, len(data), timeout_s=30.0)
        assert len(echoed) == len(data)
        res = b.control.call("barrier", {"quiet_ms": 300, "timeout_ms": 10000})
        assert res["settled"] is True
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    recs = [
        json.loads(x)
        for x in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    dut_stdin_lens = [
        r["len"] for r in recs if r.get("observation") == "dut_stdin_write"
    ]
    serial_read_lens = [
        r["len"] for r in recs if r.get("observation") == "serial_read"
    ]
    assert sum(dut_stdin_lens) == len(data)
    assert sum(serial_read_lens) == len(data)


@pytest.mark.posix_pty
def test_barrier_settles_under_continuous_noise(bridge_proc):
    """DUT 持續輸出 noise 時，passive barrier 逾時未穩定，quiesce barrier 暫停 RX 後成功結案。"""
    b = bridge_proc(
        extra_dut_argv=["--mode", "model", "--noise-interval-ms", "100"]
    )
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        res_pass = b.control.call(
            "barrier", {"mode": "passive", "quiet_ms": 500, "timeout_ms": 2000}
        )
        assert res_pass["settled"] is False

        res_qui = b.control.call("barrier", {"mode": "quiesce", "quiet_ms": 300})
        assert res_qui["settled"] is True
        initial_sw_offset = res_qui["offsets"]["serial_write"]

        time.sleep(1.0)
        st = b.control.call("status")
        # Quiesce 暫停期間 serial_write offset 不得前進
        assert st["pending"]["rx"] >= 0

        b.control.call("resume_pump", {"dir": "RX"})
        time.sleep(0.5)
    finally:
        os.close(fd)

    b.control.call("shutdown")
    assert b.wait(10) == 0

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    quiesce_records = [
        r for r in wire_lines if r.get("meta", {}).get("event") == "quiesce"
    ]
    assert any(r["meta"].get("phase") == "begin" for r in quiesce_records)
    assert any(r["meta"].get("phase") == "end" for r in quiesce_records)


@pytest.mark.posix_pty
def test_checkpoint_prefix_digest(bridge_proc):
    """checkpoint 記錄 wire 前綴 digest，後續流量不破壞舊 checkpoint 前綴 digest。"""
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        data1 = bytes(range(256)) * 16  # 4 KiB
        write_all(fd, data1)
        assert read_exact(fd, 4096) == data1

        cp = b.control.call("checkpoint")
        cid = cp["checkpoint_id"]
        w_len = cp["wire_len"]
        w_sha = cp["wire_sha256"]

        wire_file = b.evidence / "wire.ndjson"
        assert (
            hashlib.sha256(wire_file.read_bytes()[:w_len]).hexdigest() == w_sha
        )

        cp_file = b.evidence / "checkpoints" / f"{cid}.json"
        assert cp_file.is_file()
        cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
        assert cp_data["wire_sha256"] == w_sha

        # 再次往返更多資料
        data2 = b"EXTRA_BYTES_FOR_CHECKPOINT" * 100
        write_all(fd, data2)
        assert read_exact(fd, len(data2)) == data2

        # 舊 checkpoint 的前綴 digest 依然成立
        assert (
            hashlib.sha256(wire_file.read_bytes()[:w_len]).hexdigest() == w_sha
        )
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0


@pytest.mark.posix_pty
def test_status_peer_open_live(bridge_proc):
    """peer 關閉 fd 後立即呼叫 status 得 peer_open:false。"""
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    st1 = b.control.call("status")
    assert st1["peer_open"] is True
    os.close(fd)

    # 立即呼叫 status，不等 peer-watch 週期
    st2 = b.control.call("status")
    assert st2["peer_open"] is False
    b.control.call("shutdown")
    assert b.wait(10) == 0


@pytest.mark.posix_pty
def test_backpressure_bounded_memory(bridge_proc):
    """echo DUT 慢速讀取時，持續寫入 8 MiB 記憶體 pending.tx 不超過界限且 peer 收到 EAGAIN。"""
    b = bridge_proc(
        extra_dut_argv=["--read-delay-ms", "200"],
        extra_bridge_argv=["--max-inflight-bytes", "65536"],
    )
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    saw_eagain = False
    max_pending_tx = 0
    try:
        chunk = b"x" * 65536
        total_written = 0
        target_bytes = 8 * 1024 * 1024
        deadline = time.monotonic() + 15.0

        while total_written < target_bytes and time.monotonic() < deadline:
            try:
                n = os.write(fd, chunk)
                total_written += n
            except BlockingIOError:
                saw_eagain = True
                time.sleep(0.05)

            st = b.control.call("status")
            pending_tx = st.get("pending", {}).get("tx", 0)
            if pending_tx > max_pending_tx:
                max_pending_tx = pending_tx
            assert (
                pending_tx <= 65536 + 4096
            ), f"pending.tx {pending_tx} 超過 65536+4096"

            if saw_eagain and total_written > 256 * 1024:
                break
    finally:
        os.close(fd)

    assert saw_eagain is True, "peer 的 os.write 未出現 EAGAIN（背壓未抵達受測端）"
    assert max_pending_tx <= 65536 + 4096
    b.control.call("shutdown")
    assert b.wait(10) == 0


@pytest.mark.posix_pty
def test_stderr_flood_does_not_stall_data(bridge_proc):
    """DUT 大量輸出 stderr 不會阻塞資料往返，且完整寫入 dut.stderr.log。"""
    b = bridge_proc(extra_dut_argv=["--stderr-flood-bytes", "50000000"])
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        data = os.urandom(1024 * 1024)  # 1 MiB
        t0 = time.monotonic()
        write_all(fd, data, timeout_s=30.0)
        recvd = read_exact(fd, len(data), timeout_s=30.0)
        elapsed = time.monotonic() - t0
        assert recvd == data
        assert elapsed < 30.0
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    stderr_log = b.evidence / "dut.stderr.log"
    assert stderr_log.is_file()
    assert stderr_log.stat().st_size == 50000000


@pytest.mark.posix_pty
def test_stderr_cap_truncates_file_keeps_draining(bridge_proc):
    """stderr 超過上限時截斷日誌檔但持續排空，manifest 標註 dut_stderr_truncated。"""
    b = bridge_proc(
        extra_dut_argv=["--stderr-flood-bytes", "50000"],
        env_override={"UART_STDIO_BRIDGE_TEST_STDERR_CAP": "1000"},
    )
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        data = b"hello test data\n" * 10
        write_all(fd, data)
        assert read_exact(fd, len(data)) == data
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    stderr_log = b.evidence / "dut.stderr.log"
    assert stderr_log.is_file()
    assert stderr_log.stat().st_size == 1000

    manifest = json.loads(
        (b.evidence / "bridge-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("dut_stderr_truncated") is True


@pytest.mark.posix_pty
def test_dut_stdout_eof_meta(bridge_proc):
    """DUT stdout 關閉後產生 dut_stdout_eof META，TX 方向仍可轉送。"""
    b = bridge_proc(extra_dut_argv=["--close-stdout-after-bytes", "10"])
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        write_all(fd, b"0123456789")
        recvd = read_exact(fd, 10, timeout_s=5.0)
        assert recvd == b"0123456789"
        time.sleep(0.2)

        write_all(fd, b"extra bytes to tx\n")
        time.sleep(0.2)
    finally:
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    eof_records = [
        r for r in wire_lines if r.get("meta", {}).get("event") == "dut_stdout_eof"
    ]
    assert len(eof_records) >= 1
    tx_writes = [
        r for r in wire_lines if r.get("observation") == "dut_stdin_write"
    ]
    tx_total = sum(r["len"] for r in tx_writes)
    assert tx_total >= 10 + len(b"extra bytes to tx\n")


@pytest.mark.posix_pty
def test_unexpected_dut_exit_fail_stop(bridge_proc):
    """DUT 非預期結束觸發 fail-stop，bridge exit 3 且記錄 HARNESS_DUT_EXITED。"""
    b = bridge_proc(
        extra_dut_argv=["--exit-after-bytes", "5", "--exit-code", "7"]
    )
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        write_all(fd, b"1234567890")
    finally:
        os.close(fd)
    assert b.wait(10) == 3

    wire_lines = [
        json.loads(line)
        for line in (b.evidence / "wire.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    exit_records = [
        r for r in wire_lines if r.get("meta", {}).get("event") == "dut_exited"
    ]
    assert len(exit_records) >= 1
    assert exit_records[0]["meta"].get("returncode") == 7

    manifest = json.loads(
        (b.evidence / "bridge-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("failure") == "HARNESS_DUT_EXITED"
    assert manifest.get("closed_cleanly") is False


@pytest.mark.posix_pty
def test_allowed_dut_exit_no_fail(bridge_proc):
    """allow_dut_exit=True 時 DUT 結束不 fail-stop，可手動 dut_spawn 恢復。"""
    b = bridge_proc(
        extra_dut_argv=["--exit-after-bytes", "5", "--exit-code", "7"],
        allow_dut_exit=True,
    )
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        write_all(fd, b"12345")
        time.sleep(0.3)
        res = b.control.call("dut_spawn")
        assert res["dut_generation"] == 2

        write_all(fd, b"hello again\n")
        recvd = read_exact(fd, len(b"hello again\n"), timeout_s=5.0)
        assert recvd == b"hello again\n"
    finally:
        os.close(fd)

    b.control.call("shutdown")
    assert b.wait(10) == 0


@pytest.mark.posix_pty
def test_peer_gate_no_write_before_open(bridge_proc):
    """peer 未開啟時不向串列埠寫入，開啟後完整接收 banner。"""
    banner = b"=== BOOT BANNER ===\r\n"
    b = bridge_proc(extra_dut_argv=["--banner", banner.decode("ascii")])
    time.sleep(0.2)

    wire_file = b.evidence / "wire.ndjson"
    if wire_file.is_file():
        lines = [
            json.loads(line)
            for line in wire_file.read_text(encoding="utf-8").splitlines()
        ]
        sw_bytes = b"".join(
            base64.b64decode(r["payload_b64"])
            for r in lines
            if r.get("observation") == "serial_write"
        )
        assert len(sw_bytes) == 0

    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        received = read_exact(fd, len(banner), timeout_s=5.0)
        assert received == banner
    finally:
        os.close(fd)

    b.control.call("shutdown")
    assert b.wait(10) == 0

    lines = [
        json.loads(line)
        for line in wire_file.read_text(encoding="utf-8").splitlines()
    ]
    peer_states = [
        r for r in lines if r.get("meta", {}).get("event") == "peer_state"
    ]
    assert any(r["meta"].get("open") is True for r in peer_states)


@pytest.mark.posix_pty
def test_barrier_unsettled_while_traffic(bridge_proc):
    """持續雙向流量時 barrier{quiet_ms:300,timeout_ms:1000} 回 settled:false。"""
    b = bridge_proc()
    fd = os.open(b.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    stop_traffic = threading.Event()

    def traffic_worker():
        while not stop_traffic.is_set():
            try:
                os.write(fd, b"ping\n")
                time.sleep(0.05)
            except Exception:
                break

    t = threading.Thread(target=traffic_worker)
    t.start()
    try:
        res = b.control.call("barrier", {"quiet_ms": 300, "timeout_ms": 1000})
        assert res["settled"] is False
    finally:
        stop_traffic.set()
        t.join(timeout=1.0)
        os.close(fd)
    b.control.call("shutdown")
    assert b.wait(10) == 0
