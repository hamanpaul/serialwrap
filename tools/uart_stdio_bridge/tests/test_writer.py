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
    assert summary["records"] == 1


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
    finally:
        release.set()
        w.close()


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
    assert sorted(r["seq"] for r in recs) == list(range(1, 4001))
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


def test_platform_info_windows_unknown_not_zero(monkeypatch):
    from uart_stdio_bridge.evidence import manifest

    def boom(*args, **kwargs):
        raise OSError("query denied")

    monkeypatch.setattr(manifest.platform, "system", lambda: "Windows")
    monkeypatch.setattr(manifest.subprocess, "run", boom)
    monkeypatch.setattr(manifest, "_read_winreg_build", boom)
    info = manifest.platform_info()
    assert info["secure_boot"] == "unknown"
    assert info["hvci"] == "unknown"
    assert info["endpoint_av"]["product"] == "unknown"
    for key in ("secure_boot", "hvci", "endpoint_av"):
        assert key in info["evidence_insufficient"]
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
