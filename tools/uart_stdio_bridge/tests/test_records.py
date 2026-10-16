"""tools/uart_stdio_bridge/tests/test_records.py

測試 evidence/records.py（P2-T2）：bridge.v1 紀錄欄位、CRC golden vectors、序列化 bytes、
observation↔dir 對映、META 形狀與 schema 相容性。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TEST_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

FIX = _TEST_DIR / "fixtures"
REPO_ROOT = _TEST_DIR.parents[2]
BRIDGE_RECORD_SCHEMA = REPO_ROOT / "tests" / "acceptance" / "bridge_record.schema.json"


def _data_kwargs(**overrides):
    kwargs = dict(
        run_id="r1",
        case_id="c1",
        seq=1,
        mono_ts_ns=5,
        wall_ts="2026-10-15T03:06:17.663778+00:00",
        clock_id="linux-monotonic:x",
        port_id="COM0",
        endpoint_generation=1,
        dut_generation=1,
        observation="serial_read",
        stream_offset=0,
        payload=b"\x00\xff\n",
    )
    kwargs.update(overrides)
    return kwargs


def _meta_kwargs(**overrides):
    kwargs = dict(
        run_id="r1",
        case_id="c1",
        seq=2,
        mono_ts_ns=6,
        wall_ts="2026-10-15T03:06:17.663778+00:00",
        clock_id="linux-monotonic:x",
        port_id="COM0",
        endpoint_generation=1,
        dut_generation=1,
        meta={"event": "mark", "label": "x", "op_id": "o"},
    )
    kwargs.update(overrides)
    return kwargs


def test_crc_golden_vectors():
    from uart_stdio_bridge.evidence import records

    data = json.loads((FIX / "crc_golden.json").read_text(encoding="utf-8"))
    for v in data["vectors"]:
        payload = bytes(range(256)) if v["payload_hex"] == "RANGE_0_255" else bytes.fromhex(v["payload_hex"])
        assert records.crc32_hex(payload) == v["crc32"]


def test_data_record_exact_bytes():
    from uart_stdio_bridge.evidence import records

    rec = records.make_data_record(**_data_kwargs())
    assert records.dumps_record(rec) == (
        b'{"case_id":"c1","clock_id":"linux-monotonic:x","cmd_id":null,"crc32":"' + records.crc32_hex(b"\x00\xff\n").encode()
        + b'","dir":"TX","dut_generation":1,"endpoint_generation":1,"len":3,"loss_flag":false,"meta":{},'
        b'"mono_ts_ns":5,"observation":"serial_read","payload_b64":"AP8K","port_id":"COM0","run_id":"r1",'
        b'"schema_version":"bridge.v1","seq":1,"source":"bridge:external","stream_offset":0,'
        b'"wall_ts":"2026-10-15T03:06:17.663778+00:00"}\n')


def test_observation_dir_mapping():
    from uart_stdio_bridge.evidence import records

    dirs = [
        records.make_data_record(**_data_kwargs(observation=obs))["dir"]
        for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write")
    ]
    assert dirs == ["TX", "TX", "RX", "RX"]


def test_meta_record_shape():
    from uart_stdio_bridge.evidence import records

    rec = records.make_meta_record(**_meta_kwargs())
    assert rec["observation"] == "bridge_meta"
    assert rec["dir"] == "META"
    assert rec["stream_offset"] is None
    assert rec["len"] == 0
    assert rec["payload_b64"] == ""
    assert rec["crc32"] == "00000000"


def test_meta_requires_event():
    from uart_stdio_bridge.evidence import records

    with pytest.raises(ValueError):
        records.make_meta_record(**_meta_kwargs(meta={}))


def test_unknown_observation_rejected():
    from uart_stdio_bridge.evidence import records

    with pytest.raises(ValueError):
        records.make_data_record(**_data_kwargs(observation="dut_read"))


def test_wall_ts_format():
    from uart_stdio_bridge.evidence import records

    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00", records.wall_ts_now())


def test_records_validate_against_schema():
    import jsonschema  # noqa: PLC0415

    from uart_stdio_bridge.evidence import records

    schema = json.loads(BRIDGE_RECORD_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    samples = [records.make_data_record(**_data_kwargs(observation=obs))
               for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write")]
    samples.append(records.make_meta_record(**_meta_kwargs()))
    samples.append(records.make_meta_record(**_meta_kwargs(meta={"event": "evidence_failure", "code": "HARNESS_DISK_FULL"},
                                                           loss_flag=True)))
    for rec in samples:
        errors = sorted(validator.iter_errors(json.loads(records.dumps_record(rec))), key=str)
        assert errors == [], errors
