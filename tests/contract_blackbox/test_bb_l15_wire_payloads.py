"""L1.5 bridge 証據 NDJSON 讀取回歸。"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import bb_l15
from bb_l15 import (
    assert_no_tx_after,
    control_marks,
    rx_bytes,
    tx_bytes,
    wait_rx_contains,
    wait_wire_contains,
    wal_rows,
    wire_payloads,
)

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _wire_row(payload: bytes, *, direction: str = "TX") -> bytes:
    row = {
        "dir": direction,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
    }
    return (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")


def _read_during_complete_appends(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_record: bytes,
    second_record: bytes,
    read: Callable[[], Any],
) -> Any:
    """讓 append 寫入器只寫完整 LF 紀錄，並注入一次同時讀取的撕裂快照。"""
    first_written = threading.Event()
    torn_snapshot_returning = threading.Event()
    second_written = threading.Event()
    writer_errors: list[BaseException] = []

    def append_records() -> None:
        fd: int | None = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.write(fd, first_record)
            first_written.set()
            if not torn_snapshot_returning.wait(5.0):
                raise TimeoutError("測試讀取器未回報撕裂快照")
            os.write(fd, second_record)
            second_written.set()
        except BaseException as exc:
            writer_errors.append(exc)
            second_written.set()
        finally:
            if fd is not None:
                os.close(fd)

    writer = threading.Thread(target=append_records, daemon=True)
    writer.start()
    original_read_bytes = Path.read_bytes
    first_read = True

    def read_with_torn_first_snapshot(candidate: Path) -> bytes:
        nonlocal first_read
        data = original_read_bytes(candidate)
        if candidate == path and first_read:
            first_read = False
            assert data.endswith(b"\n"), "寫入器必須只追加完整 LF 紀錄"
            torn_snapshot_returning.set()
            assert second_written.wait(5.0), "測試寫入器未完成第二筆紀錄"
            return data[:-1]
        return data

    try:
        assert first_written.wait(5.0), "測試寫入器未完成第一筆紀錄"
        monkeypatch.setattr(Path, "read_bytes", read_with_torn_first_snapshot)
        result = read()
    finally:
        torn_snapshot_returning.set()
        writer.join(5.0)

    assert not writer.is_alive(), "測試寫入器未結束"
    assert not writer_errors, f"測試寫入器失敗：{writer_errors}"
    assert second_written.is_set(), "測試寫入器未追加第二筆完整紀錄"
    return result


def test_wire_payloads_ignores_incomplete_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "wire.ndjson"
    path.write_bytes(_wire_row(b"complete") + b'{"dir":"TX","payload_b64":"cGFydGl')

    assert wire_payloads(tmp_path, "TX") == [b"complete"]


def test_wire_payloads_rejects_corrupt_complete_line(tmp_path: Path) -> None:
    path = tmp_path / "wire.ndjson"
    path.write_bytes(_wire_row(b"complete") + b'{"dir":"TX","payload_b64":bad}\n')

    with pytest.raises(json.JSONDecodeError):
        wire_payloads(tmp_path, "TX")


def test_assert_no_tx_after_rejects_unterminated_late_tx(tmp_path: Path) -> None:
    path = tmp_path / "wire.ndjson"
    path.write_bytes(
        _wire_row(b"frozen-baseline")
        + _wire_row(b"late-probe")[:-1]
    )

    with pytest.raises(AssertionError, match="未以 LF 結尾"):
        assert_no_tx_after(tmp_path, b"frozen-baseline", b"console-marker")


def test_rx_bytes_rejects_unterminated_evidence(tmp_path: Path) -> None:
    (tmp_path / "wire.ndjson").write_bytes(_wire_row(b"late-rx", direction="RX")[:-1])

    with pytest.raises(AssertionError, match="未以 LF 結尾"):
        rx_bytes(tmp_path)


def test_wait_wire_contains_retries_until_trailing_record_is_terminated(tmp_path: Path) -> None:
    path = tmp_path / "wire.ndjson"
    path.write_bytes(_wire_row(b"late-probe")[:-1])
    terminated = threading.Event()
    termination_lock = threading.Lock()

    def terminate_record() -> None:
        time.sleep(0.05)
        with termination_lock:
            with path.open("ab") as stream:
                stream.write(b"\n")
            terminated.set()

    writer = threading.Thread(target=terminate_record)
    writer.start()
    try:
        wait_wire_contains(tmp_path, b"late-probe", timeout_s=1.0)
    finally:
        writer.join(timeout=1.0)

    assert not writer.is_alive(), "合成證據寫入器未結束"
    with termination_lock:
        assert terminated.is_set(), "wait_wire_contains 在紀錄 LF 寫入前就返回"

    path.write_bytes(_wire_row(b"late-rx", direction="RX")[:-1])
    terminated.clear()
    termination_lock = threading.Lock()
    writer = threading.Thread(target=terminate_record)
    writer.start()
    try:
        wait_rx_contains(tmp_path, b"late-rx", timeout_s=1.0)
    finally:
        writer.join(timeout=1.0)

    assert not writer.is_alive(), "合成 RX 證據寫入器未結束"
    with termination_lock:
        assert terminated.is_set(), "wait_rx_contains 在紀錄 LF 寫入前就返回"


def test_wal_rows_rejects_unterminated_negative_evidence(tmp_path: Path) -> None:
    (tmp_path / "raw.wal.ndjson").write_bytes(b'{"event":"shutdown"}')

    with pytest.raises(AssertionError, match="未以 LF 結尾"):
        wal_rows({"SERIALWRAP_WAL_DIR": str(tmp_path)})


def test_control_marks_rejects_unterminated_evidence(tmp_path: Path) -> None:
    (tmp_path / "control.ndjson").write_bytes(b'{"action":"mark","phase":"completed"}')

    with pytest.raises(AssertionError, match="未以 LF 結尾"):
        control_marks(tmp_path)


def test_tx_bytes_retries_torn_snapshot_during_complete_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wire.ndjson"

    result = _read_during_complete_appends(
        path,
        monkeypatch,
        _wire_row(b"first"),
        _wire_row(b"second"),
        lambda: tx_bytes(tmp_path),
    )

    assert result == b"firstsecond"


def test_wal_rows_retries_torn_snapshot_during_complete_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "raw.wal.ndjson"
    rows = [
        (json.dumps({"event": "chunk", "n": n}, separators=(",", ":")) + "\n").encode()
        for n in (1, 2)
    ]

    result = _read_during_complete_appends(
        path,
        monkeypatch,
        rows[0],
        rows[1],
        lambda: wal_rows({"SERIALWRAP_WAL_DIR": str(tmp_path)}),
    )

    assert result == [{"event": "chunk", "n": 1}, {"event": "chunk", "n": 2}]


def test_tx_bytes_reports_insufficient_while_unterminated_tail_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wire.ndjson"
    path.write_bytes(b'{"dir":"TX","payload_b64":"')
    monkeypatch.setattr(bb_l15, "_NDJSON_READ_RETRY_TIMEOUT_S", 0.05)
    original_read_bytes = Path.read_bytes
    reads = 0

    def grow_after_each_snapshot(candidate: Path) -> bytes:
        nonlocal reads
        data = original_read_bytes(candidate)
        if candidate == path:
            reads += 1
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, b"x")
            finally:
                os.close(fd)
        return data

    monkeypatch.setattr(Path, "read_bytes", grow_after_each_snapshot)
    with pytest.raises(AssertionError, match="證據不足"):
        tx_bytes(tmp_path)

    assert reads >= 2, "活取樣至少要重讀一次才能判定證據不足"
    assert path.stat().st_size > len(b'{"dir":"TX","payload_b64":"')
