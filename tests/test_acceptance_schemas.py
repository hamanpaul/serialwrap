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


def _valid_meta_record(event: str, meta: dict, *, loss_flag: bool) -> dict:
    return {
        "schema_version": "bridge.v1", "run_id": "r1", "case_id": "c1", "seq": 2, "mono_ts_ns": 6,
        "wall_ts": "2026-10-15T03:06:17.663778+00:00", "clock_id": "linux-monotonic:x", "port_id": "COM0",
        "endpoint_generation": 1, "dut_generation": 1, "dir": "META", "observation": "bridge_meta",
        "stream_offset": None, "len": 0, "payload_b64": "", "crc32": "00000000", "source": "bridge:external",
        "cmd_id": None, "loss_flag": loss_flag, "meta": {"event": event, **meta},
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


@pytest.mark.memory_only
def test_bridge_record_schema_enforces_data_and_meta_branches():
    import jsonschema  # noqa: PLC0415

    validator = jsonschema.Draft202012Validator(_load_schema("bridge_record.schema.json"))
    data_tampers = {
        "data 方向錯誤": lambda rec: rec.__setitem__("dir", "RX"),
        "data loss_flag": lambda rec: rec.__setitem__("loss_flag", True),
        "data meta 非空": lambda rec: rec.__setitem__("meta", {"event": "mark"}),
        "data offset 為 null": lambda rec: rec.__setitem__("stream_offset", None),
        "data len 為零": lambda rec: rec.__setitem__("len", 0),
        "payload 使用 URL-safe 字元": lambda rec: rec.__setitem__("payload_b64", "__8="),
        "payload padding 錯誤": lambda rec: rec.__setitem__("payload_b64", "AP8"),
        "source 錯誤": lambda rec: rec.__setitem__("source", "serialwrap"),
        "seq 為零": lambda rec: rec.__setitem__("seq", 0),
        "endpoint generation 為零": lambda rec: rec.__setitem__("endpoint_generation", 0),
        "dut generation 為零": lambda rec: rec.__setitem__("dut_generation", 0),
    }
    for label, mutate in data_tampers.items():
        rec = _valid_data_record()
        mutate(rec)
        assert list(validator.iter_errors(rec)), label

    meta = _valid_meta_record("mark", {"label": "x", "op_id": "o"}, loss_flag=False)
    meta_tampers = {
        "event 不在值域": lambda rec: rec["meta"].__setitem__("event", "bridge_strat"),
        "META 方向錯誤": lambda rec: rec.__setitem__("dir", "TX"),
        "META offset 非 null": lambda rec: rec.__setitem__("stream_offset", 0),
        "META len 非零": lambda rec: rec.__setitem__("len", 1),
        "META payload 非空": lambda rec: rec.__setitem__("payload_b64", "AA=="),
        "META CRC 非零": lambda rec: rec.__setitem__("crc32", "00000001"),
    }
    for label, mutate in meta_tampers.items():
        rec = json.loads(json.dumps(meta))
        mutate(rec)
        assert list(validator.iter_errors(rec)), label


@pytest.mark.memory_only
def test_bridge_record_schema_requires_complete_write_uncertain_meta():
    import jsonschema  # noqa: PLC0415

    validator = jsonschema.Draft202012Validator(_load_schema("bridge_record.schema.json"))
    base = _valid_meta_record(
        "write_uncertain",
        {
            "observation": "serial_write",
            "offset_start": 0,
            "requested_len": 3,
            "accepted_len": None,
            "error": "write timeout",
        },
        loss_flag=True,
    )
    assert not list(validator.iter_errors(base))

    tamper_cases = {
        "缺 observation": lambda rec: rec["meta"].pop("observation"),
        "觀測位置不合法": lambda rec: rec["meta"].__setitem__("observation", "serial_read"),
        "缺 offset_start": lambda rec: rec["meta"].pop("offset_start"),
        "offset_start 負值": lambda rec: rec["meta"].__setitem__("offset_start", -1),
        "缺 requested_len": lambda rec: rec["meta"].pop("requested_len"),
        "requested_len 為零": lambda rec: rec["meta"].__setitem__("requested_len", 0),
        "accepted_len 填成成功值": lambda rec: rec["meta"].__setitem__("accepted_len", 3),
        "缺 accepted_len": lambda rec: rec["meta"].pop("accepted_len"),
        "缺 error": lambda rec: rec["meta"].pop("error"),
        "loss_flag 為 false": lambda rec: rec.__setitem__("loss_flag", False),
    }
    for label, mutate in tamper_cases.items():
        rec = json.loads(json.dumps(base))
        mutate(rec)
        assert list(validator.iter_errors(rec)), label


@pytest.mark.memory_only
def test_bridge_record_schema_requires_evidence_failure_code_and_loss_flag():
    import jsonschema  # noqa: PLC0415

    validator = jsonschema.Draft202012Validator(_load_schema("bridge_record.schema.json"))
    base = _valid_meta_record(
        "evidence_failure",
        {"code": "HARNESS_DISK_FULL"},
        loss_flag=True,
    )
    assert not list(validator.iter_errors(base))

    for label, mutate in (
        ("缺 code", lambda rec: rec["meta"].pop("code")),
        ("code 格式錯誤", lambda rec: rec["meta"].__setitem__("code", "DISK_FULL")),
        ("loss_flag 為 false", lambda rec: rec.__setitem__("loss_flag", False)),
    ):
        rec = json.loads(json.dumps(base))
        mutate(rec)
        assert list(validator.iter_errors(rec)), label
