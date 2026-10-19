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


ACCEPTANCE_DIR = REPO_ROOT / "tests" / "acceptance"
BRIDGE_RECORD_REQUIRED = {
    "schema_version", "run_id", "case_id", "seq", "mono_ts_ns", "wall_ts", "clock_id", "port_id",
    "endpoint_generation", "dut_generation", "dir", "observation", "stream_offset", "len",
    "payload_b64", "crc32", "source", "cmd_id", "loss_flag", "meta",
}


def _load_schema(name: str) -> dict:
    return json.loads((ACCEPTANCE_DIR / name).read_text(encoding="utf-8"))


def _valid_data_record() -> dict:
    return {
        "schema_version": "bridge.v1", "run_id": "r1", "case_id": "c1", "seq": 1, "mono_ts_ns": 5,
        "wall_ts": "2026-10-15T03:06:17.663778+00:00", "clock_id": "linux-monotonic:x", "port_id": "COM0",
        "endpoint_generation": 1, "dut_generation": 1, "dir": "TX", "observation": "serial_read",
        "stream_offset": 0, "len": 3, "payload_b64": "AP8K", "crc32": "8cb0cd7e", "source": "bridge:external",
        "cmd_id": None, "loss_flag": False, "meta": {},
    }


@pytest.mark.memory_only
def test_all_acceptance_schemas_are_valid_draft2020():
    import jsonschema  # noqa: PLC0415

    paths = sorted(ACCEPTANCE_DIR.glob("*.schema.json"))
    names = {p.name for p in paths}
    assert {"bridge_record.schema.json", "bridge_control.schema.json", "correlation.schema.json",
            "l15_manifest.schema.json", "comparison.schema.json"} <= names
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", path.name
        assert schema.get("$id"), path.name


@pytest.mark.memory_only
def test_bridge_record_schema_required_fields():
    schema = _load_schema("bridge_record.schema.json")
    assert set(schema["required"]) == BRIDGE_RECORD_REQUIRED
    assert len(schema["required"]) == 20
    assert schema["additionalProperties"] is False


@pytest.mark.memory_only
def test_bridge_record_schema_rejects_tamper():
    import jsonschema  # noqa: PLC0415

    validator = jsonschema.Draft202012Validator(_load_schema("bridge_record.schema.json"))
    base = _valid_data_record()
    assert not list(validator.iter_errors(base))

    def without_crc(rec):
        rec.pop("crc32")

    tampers = {
        "刪 crc32": without_crc,
        "crc32 大寫": lambda rec: rec.__setitem__("crc32", rec["crc32"].upper()),
        "len=-1": lambda rec: rec.__setitem__("len", -1),
        "dir=tx": lambda rec: rec.__setitem__("dir", "tx"),
        "cmd_id=c1": lambda rec: rec.__setitem__("cmd_id", "c1"),
        "observation=x": lambda rec: rec.__setitem__("observation", "x"),
    }
    for label, mutate in tampers.items():
        rec = json.loads(json.dumps(base))
        mutate(rec)
        assert list(validator.iter_errors(rec)), label
