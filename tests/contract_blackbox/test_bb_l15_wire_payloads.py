"""L1.5 bridge 証據 NDJSON 讀取回歸。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from bb_l15 import wire_payloads

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _wire_row(payload: bytes) -> bytes:
    row = {
        "dir": "TX",
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
