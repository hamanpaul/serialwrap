from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.core_portable
REPO_ROOT = Path(__file__).resolve().parents[1]


def _evidence_set_type():
    """延遲載入測試 builder，避免缺少 bridge 套件時阻斷全套收集。"""
    builder_dir = REPO_ROOT / "tools" / "uart_stdio_bridge" / "tests"
    if str(builder_dir) not in sys.path:
        sys.path.insert(0, str(builder_dir))
    return importlib.import_module("evidence_builder").EvidenceSet


@pytest.mark.memory_only
def test_builder_wal_matches_contract(tmp_path):
    import jsonschema  # noqa: PLC0415

    EvidenceSet = _evidence_set_type()
    evidence = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"echo one\n", b"echo two\n"],
        tx_chunks_wal=[b"echo one\n", b"echo two\n"],
        rx_chunks_wire=[b"one\r\n"],
        rx_chunks_wal=[b"one\r\n"],
    )
    schema_path = REPO_ROOT / "contracts" / "wal_record.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    records = [
        json.loads(line)
        for line in evidence.wal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records
    for record in records:
        validator.validate(record)
