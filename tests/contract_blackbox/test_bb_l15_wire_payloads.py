"""L1.5 bridge 証據 NDJSON 讀取回歸。"""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import pytest

from bb_l15 import (
    assert_no_tx_after,
    control_marks,
    rx_bytes,
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

    def terminate_record() -> None:
        time.sleep(0.05)
        with path.open("ab") as stream:
            stream.write(b"\n")

    writer = threading.Thread(target=terminate_record)
    writer.start()
    try:
        wait_wire_contains(tmp_path, b"late-probe", timeout_s=1.0)
    finally:
        writer.join(timeout=1.0)

    assert not writer.is_alive(), "合成證據寫入器未結束"

    path.write_bytes(_wire_row(b"late-rx", direction="RX")[:-1])
    writer = threading.Thread(target=terminate_record)
    writer.start()
    try:
        wait_rx_contains(tmp_path, b"late-rx", timeout_s=1.0)
    finally:
        writer.join(timeout=1.0)

    assert not writer.is_alive(), "合成 RX 證據寫入器未結束"


def test_wal_rows_rejects_unterminated_negative_evidence(tmp_path: Path) -> None:
    (tmp_path / "raw.wal.ndjson").write_bytes(b'{"event":"shutdown"}')

    with pytest.raises(AssertionError, match="未以 LF 結尾"):
        wal_rows({"SERIALWRAP_WAL_DIR": str(tmp_path)})


def test_control_marks_rejects_unterminated_evidence(tmp_path: Path) -> None:
    (tmp_path / "control.ndjson").write_bytes(b'{"action":"mark","phase":"completed"}')

    with pytest.raises(AssertionError, match="未以 LF 結尾"):
        control_marks(tmp_path)
