"""tools/uart_stdio_bridge/tests/test_writer.py

測試 evidence/writer.py 與 evidence/manifest.py（P2-T2）：seq／stream_offset 分配、
endpoint generation 重置、close digest、既有檔拒絕、磁碟滿／一般寫入錯誤 fail-stop、
短寫補寫、佇列溢位、fsync 失敗、並發唯一 seq、空 payload、私有權限與 platform_info 形狀。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TEST_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

OBS = ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write")


def _writer(path: Path, **kwargs):
    from uart_stdio_bridge.evidence.writer import EvidenceWriter

    return EvidenceWriter(path, run_id="r1", case_id="c1", clock_id="test-clock", **kwargs)


def _emit(w, observation="serial_read", payload=b"abc", gen=1):
    return w.emit_data(port_id="COM0", endpoint_generation=gen, dut_generation=1,
                       observation=observation, payload=payload)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writer_seq_and_offsets(tmp_path):
    p = tmp_path / "wire.ndjson"
    w = _writer(p)
    w.start()
    _emit(w, "serial_read", b"abc")
    _emit(w, "serial_read", b"defgh")
    _emit(w, "serial_write", b"xy")
    assert w.offsets("COM0", 1) == {"serial_read": 8, "dut_stdin_write": 0, "dut_stdout_read": 0, "serial_write": 2}
    w.close()
    recs = _lines(p)
    assert [r["seq"] for r in recs] == [1, 2, 3]
    assert [r["stream_offset"] for r in recs] == [0, 3, 0]


def test_writer_offsets_reset_per_endpoint_generation(tmp_path):
    p = tmp_path / "wire.ndjson"
    w = _writer(p)
    w.start()
    _emit(w, "serial_read", b"abcd", gen=1)
    _emit(w, "serial_read", b"e", gen=2)
    w.close()
    recs = _lines(p)
    assert recs[1]["endpoint_generation"] == 2
    assert recs[1]["stream_offset"] == 0


def test_writer_close_digest(tmp_path):
    p = tmp_path / "wire.ndjson"
    w = _writer(p)
    w.start()
    for i in range(5):
        _emit(w, payload=bytes([i]) * (i + 1))
    w.emit_meta(port_id="COM0", endpoint_generation=1, dut_generation=1, meta={"event": "bridge_stop"})
    summary = w.close()
    raw = p.read_bytes()
    assert summary["sha256"] == hashlib.sha256(raw).hexdigest()
    assert summary["records"] == 6
    assert summary["bytes"] == len(raw)
    assert w.close() == summary


def test_writer_refuses_existing_file(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    p = tmp_path / "wire.ndjson"
    p.write_bytes(b"old\n")
    w = _writer(p)
    with pytest.raises(EvidenceFailure) as excinfo:
        w.start()
    assert excinfo.value.code == "HARNESS_EVIDENCE_WRITE_FAILED"
    assert p.read_bytes() == b"old\n"


def test_writer_disk_full_fail_stop(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    calls = {"n": 0}

    def fake_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.ENOSPC, "x")
        return os.write(fd, data)

    p = tmp_path / "wire.ndjson"
    w = _writer(p, os_write=fake_write)
    w.start()
    _emit(w, payload=b"one")
    w.flush()
    _emit(w, payload=b"two")
    with pytest.raises(EvidenceFailure):
        w.flush()
    assert w.failure == "HARNESS_DISK_FULL"
    with pytest.raises(EvidenceFailure) as excinfo:
        _emit(w, payload=b"three")
    assert excinfo.value.code == "HARNESS_DISK_FULL"
    summary = w.close()
    assert summary["records"] == 2
    assert _lines(p)[-1]["meta"] == {
        "event": "evidence_failure",
        "code": "HARNESS_DISK_FULL",
    }


def test_writer_generic_write_error(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    def fake_write(fd, data):
        raise OSError(errno.EIO, "io")

    w = _writer(tmp_path / "wire.ndjson", os_write=fake_write)
    w.start()
    _emit(w)
    with pytest.raises(EvidenceFailure):
        w.flush()
    assert w.failure == "HARNESS_EVIDENCE_WRITE_FAILED"
    w.close()


def test_writer_short_write_retried(tmp_path):
    from uart_stdio_bridge.evidence import records

    def short_write(fd, data):
        return os.write(fd, bytes(data[:3]))

    p = tmp_path / "wire.ndjson"
    w = _writer(p, os_write=short_write)
    w.start()
    _emit(w, payload=b"hello world")
    _emit(w, "serial_write", payload=bytes(range(256)))
    summary = w.close()
    raw = p.read_bytes()
    recs = _lines(p)
    assert len(recs) == 2
    # 逐行重新序列化必須與檔案 bytes 完全相同（不丟、不重複）
    assert b"".join(records.dumps_record(r) for r in recs) == raw
    assert summary["bytes"] == len(raw)


def test_writer_overflow_fail_stop(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    entered = threading.Event()
    release = threading.Event()

    def blocking_write(fd, data):
        entered.set()
        release.wait()
        return os.write(fd, data)

    w = _writer(tmp_path / "wire.ndjson", max_pending_records=2, block_timeout_s=0.2, os_write=blocking_write)
    w.start()
    try:
        _emit(w, payload=b"1")
        assert entered.wait(5.0)
        _emit(w, payload=b"2")
        _emit(w, payload=b"3")
        t0 = time.monotonic()
        with pytest.raises(EvidenceFailure) as excinfo:
            _emit(w, payload=b"4")
        elapsed = time.monotonic() - t0
        assert excinfo.value.code == "HARNESS_EVIDENCE_OVERFLOW"
        assert 0.15 <= elapsed < 3.0
        assert w.failure == "HARNESS_EVIDENCE_OVERFLOW"
        assert w.offsets("COM0", 1)["serial_read"] == 3
    finally:
        release.set()
    w.close()
    recs = _lines(tmp_path / "wire.ndjson")
    assert recs[-1]["meta"] == {"event": "evidence_failure", "code": "HARNESS_EVIDENCE_OVERFLOW"}
    assert recs[-1]["loss_flag"] is True
    assert recs[-1]["seq"] == recs[-2]["seq"] + 1


def test_writer_overflow_marker_slot_survives_pending_flush_barrier(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    entered = threading.Event()
    release = threading.Event()
    flush_error = {}

    def blocking_write(fd, data):
        entered.set()
        release.wait()
        return os.write(fd, data)

    w = _writer(
        tmp_path / "wire.ndjson",
        max_pending_records=2,
        block_timeout_s=0.05,
        os_write=blocking_write,
    )
    w.start()
    _emit(w, payload=b"1")
    assert entered.wait(5.0)

    def flush():
        try:
            w.flush(timeout_s=2.0)
        except Exception as exc:  # noqa: BLE001 - capture expected overflow propagation
            flush_error["error"] = exc

    flush_thread = threading.Thread(target=flush)
    flush_thread.start()
    deadline = time.monotonic() + 1.0
    while w._queue.qsize() < 1 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert w._queue.qsize() == 1
    _emit(w, payload=b"2")
    _emit(w, payload=b"3")
    with pytest.raises(EvidenceFailure) as excinfo:
        _emit(w, payload=b"4")
    assert excinfo.value.code == "HARNESS_EVIDENCE_OVERFLOW"

    release.set()
    flush_thread.join(timeout=5.0)
    assert not flush_thread.is_alive()
    assert isinstance(flush_error.get("error"), EvidenceFailure)
    w.close()
    recs = _lines(tmp_path / "wire.ndjson")
    assert [record["seq"] for record in recs] == [1, 2, 3, 4]
    assert recs[-1]["meta"] == {
        "event": "evidence_failure",
        "code": "HARNESS_EVIDENCE_OVERFLOW",
    }


def test_writer_meta_loss_flag_is_persisted(tmp_path):
    p = tmp_path / "wire.ndjson"
    w = _writer(p)
    w.start()
    w.emit_meta(
        port_id="COM0",
        endpoint_generation=1,
        dut_generation=1,
        meta={"event": "evidence_failure", "code": "HARNESS_DISK_FULL"},
        loss_flag=True,
    )
    w.close()
    rec = _lines(p)[0]
    assert rec["meta"]["event"] == "evidence_failure"
    assert rec["loss_flag"] is True


def test_writer_fsync_failure(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    def bad_fsync(fd):
        raise OSError(errno.EIO, "fsync")

    w = _writer(tmp_path / "wire.ndjson", os_fsync=bad_fsync)
    w.start()
    _emit(w)
    with pytest.raises(EvidenceFailure):
        w.flush()
    assert w.failure == "HARNESS_EVIDENCE_WRITE_FAILED"
    w.close()


def test_writer_close_final_fsync_failure_sets_failure(tmp_path):
    calls = {"n": 0}

    def bad_final_fsync(fd):
        calls["n"] += 1
        raise OSError(errno.ENOSPC, "final sync")

    w = _writer(tmp_path / "wire.ndjson", os_fsync=bad_final_fsync)
    w.start()
    _emit(w)
    summary = w.close()
    assert summary["records"] == 1
    assert calls["n"] == 1
    assert w.failure == "HARNESS_DISK_FULL"


def test_writer_flush_timeout_sets_failure_and_returns(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    entered = threading.Event()
    release = threading.Event()
    outcome = {}

    def blocking_write(fd, data):
        entered.set()
        release.wait()
        return os.write(fd, data)

    w = _writer(tmp_path / "wire.ndjson", os_write=blocking_write)
    w.start()
    _emit(w)
    assert entered.wait(5.0)

    def flush():
        try:
            w.flush(timeout_s=0.05)
        except Exception as exc:  # noqa: BLE001 - collect thread result for assertion
            outcome["error"] = exc

    thread = threading.Thread(target=flush)
    thread.start()
    thread.join(timeout=0.5)
    timed_out = thread.is_alive()
    release.set()
    thread.join(timeout=5.0)
    try:
        assert not timed_out, "flush(timeout_s=...) 未遵守期限"
        assert not thread.is_alive(), "flush timeout 後仍卡住"
        assert isinstance(outcome.get("error"), EvidenceFailure)
        assert w.failure == "HARNESS_EVIDENCE_WRITE_FAILED"
    finally:
        w.close()


def test_writer_flush_fsync_does_not_hold_emit_lock(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    emit_result = {}

    def blocking_fsync(fd):
        entered.set()
        release.wait()

    w = _writer(tmp_path / "wire.ndjson", os_fsync=blocking_fsync)
    w.start()
    _emit(w, payload=b"before")

    def flush():
        try:
            w.flush(timeout_s=2.0)
        except Exception as exc:  # noqa: BLE001 - collect thread result for assertion
            emit_result["flush_error"] = exc

    flush_thread = threading.Thread(target=flush)
    flush_thread.start()
    assert entered.wait(5.0)

    def emit_during_flush():
        try:
            emit_result["seq"] = _emit(w, payload=b"during")
        except Exception as exc:  # noqa: BLE001 - collect thread result for assertion
            emit_result["emit_error"] = exc

    emit_thread = threading.Thread(target=emit_during_flush)
    emit_thread.start()
    emit_thread.join(timeout=0.1)
    blocked = emit_thread.is_alive()
    release.set()
    emit_thread.join(timeout=5.0)
    flush_thread.join(timeout=5.0)
    try:
        assert not blocked, "flush 的 fsync 阻塞了 emit"
        assert not emit_thread.is_alive()
        assert emit_result.get("seq") == 2
        assert "emit_error" not in emit_result
        assert "flush_error" not in emit_result
    finally:
        w.close()


def test_writer_close_stuck_worker_sets_failure_and_keeps_fd_open(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def blocking_write(fd, data):
        entered.set()
        release.wait()
        return os.write(fd, data)

    w = _writer(
        tmp_path / "wire.ndjson",
        os_write=blocking_write,
        close_timeout_s=0.05,
    )
    w.start()
    _emit(w)
    assert entered.wait(5.0)
    t0 = time.monotonic()
    w.close()
    elapsed = time.monotonic() - t0
    fd = w._fd
    assert elapsed < 1.0
    assert w.failure == "HARNESS_EVIDENCE_WRITE_FAILED"
    assert fd is not None
    os.fstat(fd)

    release.set()
    w.close()
    assert w._fd is None


def test_writer_stops_writing_after_first_worker_failure(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fail_first_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            release.wait()
            raise OSError(errno.EIO, "write failed")
        return os.write(fd, data)

    p = tmp_path / "wire.ndjson"
    w = _writer(p, os_write=fail_first_write)
    w.start()
    _emit(w, payload=b"first")
    assert entered.wait(5.0)
    _emit(w, payload=b"must-not-follow")
    _emit(w, payload=b"also-must-not-follow")
    release.set()
    with pytest.raises(Exception):
        w.flush()
    w.close()
    recs = _lines(p)
    assert recs
    assert all(r["meta"].get("event") == "evidence_failure" for r in recs)
    assert calls["n"] == 2


def test_writer_concurrent_emit_unique_seq(tmp_path):
    p = tmp_path / "wire.ndjson"
    w = _writer(p)
    w.start()

    def worker(idx):
        for i in range(1000):
            _emit(w, OBS[idx], payload=bytes([i % 256]) * (1 + (i % 7)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    w.close()
    recs = _lines(p)
    assert [r["seq"] for r in recs] == list(range(1, 4001))
    for obs in OBS:
        mine = sorted((r for r in recs if r["observation"] == obs), key=lambda r: r["seq"])
        assert len(mine) == 1000
        expected = 0
        for r in mine:
            assert r["stream_offset"] == expected
            expected += r["len"]


def test_writer_empty_payload_rejected(tmp_path):
    w = _writer(tmp_path / "wire.ndjson")
    w.start()
    with pytest.raises(ValueError):
        _emit(w, payload=b"")
    w.close()


def test_writer_validation_failure_does_not_consume_seq_or_offset(tmp_path):
    w = _writer(tmp_path / "wire.ndjson")
    w.start()
    with pytest.raises(ValueError):
        _emit(w, payload=b"bad", gen=0)
    seq = _emit(w, payload=b"ok")
    assert seq == 1
    assert w.offsets("COM0", 1)["serial_read"] == 2
    w.close()


def test_writer_rejects_emit_before_start(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    w = _writer(tmp_path / "wire.ndjson")
    with pytest.raises(EvidenceFailure):
        _emit(w)


def test_writer_rejects_flush_before_start(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    w = _writer(tmp_path / "wire.ndjson")
    with pytest.raises(EvidenceFailure):
        w.flush()


def test_writer_rejects_emit_after_close(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    w = _writer(tmp_path / "wire.ndjson")
    w.start()
    w.close()
    with pytest.raises(EvidenceFailure):
        _emit(w)


def test_writer_close_serializes_against_concurrent_emit(tmp_path):
    from uart_stdio_bridge.evidence.writer import EvidenceFailure

    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    emit_started = threading.Event()
    emit_result = {}

    def blocking_write(fd, data):
        entered.set()
        release.wait()
        return os.write(fd, data)

    w = _writer(tmp_path / "wire.ndjson", os_write=blocking_write, close_timeout_s=1.0)
    w.start()
    _emit(w)
    assert entered.wait(5.0)

    close_thread = threading.Thread(target=lambda: (close_started.set(), w.close()))
    close_thread.start()
    assert close_started.wait(1.0)
    deadline = time.monotonic() + 1.0
    while not w._emit_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert w._emit_lock.locked()

    def late_emit():
        emit_started.set()
        try:
            _emit(w)
        except Exception as exc:  # noqa: BLE001 - collect the lifecycle result
            emit_result["error"] = exc

    emit_thread = threading.Thread(target=late_emit)
    emit_thread.start()
    assert emit_started.wait(1.0)
    release.set()
    close_thread.join(timeout=5.0)
    emit_thread.join(timeout=5.0)
    assert not close_thread.is_alive()
    assert not emit_thread.is_alive()
    assert isinstance(emit_result.get("error"), EvidenceFailure)


@pytest.mark.skipif(os.name == "nt", reason="cap:posix_permissions 僅 POSIX")
def test_writer_private_permissions(tmp_path):
    from uart_stdio_bridge.evidence.manifest import ensure_private_dir, write_json_atomic

    d = tmp_path / "evidence" / "run"
    ensure_private_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    w = _writer(d / "wire.ndjson")
    w.start()
    assert stat.S_IMODE((d / "wire.ndjson").stat().st_mode) == 0o600
    w.close()
    write_json_atomic(d / "bridge-manifest.json", {"a": 1})
    assert stat.S_IMODE((d / "bridge-manifest.json").stat().st_mode) == 0o600
    assert (d / "bridge-manifest.json").read_text(encoding="utf-8") == '{\n  "a": 1\n}\n'


@pytest.mark.skipif(os.name == "nt", reason="cap:posix_permissions 僅 POSIX")
def test_write_json_atomic_preserves_existing_parent_permissions(tmp_path):
    from uart_stdio_bridge.evidence.manifest import write_json_atomic

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    write_json_atomic(shared / "manifest.json", {"ok": True})
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_clock_id_is_cached_for_evidence_run(monkeypatch):
    from uart_stdio_bridge.evidence import clock

    clock.clock_id.cache_clear()
    wall = {"value": 1000.0}
    monotonic = {"value": 100.0}
    monkeypatch.setattr(clock.sys, "platform", "win32")
    monkeypatch.setattr(clock.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(clock.time, "time", lambda: wall["value"])
    monkeypatch.setattr(clock.time, "monotonic", lambda: monotonic["value"])
    try:
        first = clock.clock_id()
        wall["value"] = 1002.0
        assert clock.clock_id() == first
    finally:
        clock.clock_id.cache_clear()


def test_selftest_cli_records_win_serial_driver_install_method(monkeypatch, tmp_path):
    from uart_stdio_bridge.selftest import runner

    captured = {}

    def fake_run_selftest(ctx, *, only=None):
        captured["context"] = ctx
        captured["only"] = only
        return {"verdict": "PASS"}

    monkeypatch.setattr(runner, "run_selftest", fake_run_selftest)
    rc = runner.main([
        "--endpoint", "win-serial",
        "--com-pair", "COM20:COM21",
        "--com-driver-name", "com0com",
        "--com-driver-version", "3.0",
        "--com-driver-sha256", "a" * 64,
        "--com-driver-install-method", "signed-installer",
        "--out", str(tmp_path / "report"),
        "--only", "ST-01",
    ])
    assert rc == 0
    ctx = captured["context"]
    assert ctx.com_driver == {"name": "com0com", "version": "3.0", "sha256": "a" * 64}
    assert ctx.com_driver_install_method == "signed-installer"
    assert captured["only"] == ["ST-01"]


def test_selftest_report_passes_win_serial_qualification_to_platform_info(monkeypatch, tmp_path):
    from uart_stdio_bridge.evidence import manifest
    from uart_stdio_bridge.selftest import runner
    from uart_stdio_bridge.selftest.checks import CheckResult, SelftestContext

    calls = []
    monkeypatch.setattr(
        runner,
        "run_check",
        lambda check_id, ctx: CheckResult(check_id, "PASS", "", str(tmp_path)),
    )
    monkeypatch.setattr(
        manifest,
        "platform_info",
        lambda **kwargs: calls.append(kwargs) or {"os": "Windows", "evidence_insufficient": []},
    )
    ctx = SelftestContext(
        endpoint="win-serial",
        com_pair=("COM20", "COM21"),
        transport_mode="virtual_com0com",
        work_dir=tmp_path,
        com_driver={"name": "com0com", "version": "3.0", "sha256": "a" * 64},
        com_driver_install_method="signed-installer",
    )
    report = runner.run_selftest(ctx, only=["ST-01"])
    assert report["platform"]["os"] == "Windows"
    assert calls == [{
        "endpoint": "win-serial",
        "com_driver": {"name": "com0com", "version": "3.0", "sha256": "a" * 64},
        "com_driver_install_method": "signed-installer",
    }]


def test_platform_info_windows_unknown_not_zero(monkeypatch):
    from uart_stdio_bridge.evidence import manifest

    def boom(*args, **kwargs):
        raise OSError("query denied")

    monkeypatch.setattr(manifest.platform, "system", lambda: "Windows")
    monkeypatch.setattr(manifest.subprocess, "run", boom)
    monkeypatch.setattr(manifest, "_read_winreg_build", boom)
    info = manifest.platform_info(endpoint="win-serial")
    assert info["secure_boot"] == "unknown"
    assert info["hvci"] == "unknown"
    assert info["endpoint_av"]["product"] == "unknown"
    for key in ("secure_boot", "hvci", "endpoint_av"):
        assert key in info["evidence_insufficient"]
    assert info["com_driver"] == {
        "name": "unknown",
        "version": "unknown",
        "sha256": "unknown",
        "install_method": "unknown",
    }
    assert "com_driver" in info["evidence_insufficient"]
    flat = json.dumps(info)
    assert "false" not in flat
    assert info["secure_boot"] is not False and info["hvci"] is not False
    assert info["secure_boot"] != 0 and info["hvci"] != 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="cap:linux 僅 Linux 形狀")
def test_platform_info_linux_shape():
    from uart_stdio_bridge.evidence import manifest

    info = manifest.platform_info()
    assert {"os", "release", "version", "machine", "python", "evidence_insufficient"} <= set(info)
    for key in ("windows_build", "secure_boot", "hvci", "com_driver", "endpoint_av"):
        assert key not in info


def test_platform_info_win_serial_records_driver_qualification(monkeypatch):
    from uart_stdio_bridge.evidence import manifest

    monkeypatch.setattr(manifest.platform, "system", lambda: "Windows")
    monkeypatch.setattr(manifest, "_query_windows_build", lambda: "Windows 10.0.26200 (26200.1)")
    monkeypatch.setattr(manifest, "_query_secure_boot", lambda: True)
    monkeypatch.setattr(manifest, "_query_hvci", lambda: True)
    monkeypatch.setattr(manifest, "_query_endpoint_av", lambda: {"product": "AV", "state": "active"})
    info = manifest.platform_info(
        endpoint="win-serial",
        com_driver={"name": "com0com", "version": "3.0", "sha256": "a" * 64},
        com_driver_install_method="signed-installer",
    )
    assert info["windows_build"] == "Windows 10.0.26200 (26200.1)"
    assert info["secure_boot"] is True
    assert info["hvci"] is True
    assert info["com_driver"] == {
        "name": "com0com",
        "version": "3.0",
        "sha256": "a" * 64,
        "install_method": "signed-installer",
    }
    assert "com_driver" not in info["evidence_insufficient"]


def test_windows_build_parser_combines_windows_version_and_registry_build(monkeypatch):
    from uart_stdio_bridge.evidence import manifest

    monkeypatch.setattr(manifest, "_powershell", lambda command: "Microsoft Windows [Version 10.0.26200.1]")
    monkeypatch.setattr(manifest, "_read_winreg_build", lambda: "26200.1")
    assert manifest._query_windows_build() == "Microsoft Windows [Version 10.0.26200.1] (26200.1)"


@pytest.mark.parametrize(("output", "expected"), [("1,2", True), ("1, 2, 3", True), ("12", False), ("1", False)])
def test_hvci_parser_requires_exact_service_id(monkeypatch, output, expected):
    from uart_stdio_bridge.evidence import manifest

    monkeypatch.setattr(manifest, "_powershell", lambda command: output)
    assert manifest._query_hvci() is expected
