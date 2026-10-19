from __future__ import annotations

import ast
import json

import pytest

from evidence_builder import EvidenceSet, REPO_ROOT, SOURCE_ROOT, comparator_api


pytestmark = pytest.mark.core_portable


@pytest.mark.memory_only
def test_zero_tx_pass(tmp_path):
    _, _, check_zero_tx, _ = comparator_api()
    ev = EvidenceSet.build(tmp_path, op_id="rejected")
    out = check_zero_tx(ev.inputs(), "rejected")
    assert out["verdict"] == "PASS"


@pytest.mark.memory_only
def test_zero_tx_fail_on_wire_bytes(tmp_path):
    _, _, check_zero_tx, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"x"],
        tx_chunks_wal=[],
        op_id="rejected",
    )
    out = check_zero_tx(ev.inputs(), "rejected")
    assert out["verdict"] == "FAIL"


@pytest.mark.memory_only
def test_zero_tx_fail_on_wal_record(tmp_path):
    _, _, check_zero_tx, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[],
        tx_chunks_wal=[b"x"],
        op_id="rejected",
    )
    out = check_zero_tx(ev.inputs(), "rejected")
    assert out["verdict"] == "FAIL"


@pytest.mark.memory_only
def test_zero_tx_insufficient_unsettled(tmp_path):
    _, _, check_zero_tx, _ = comparator_api()
    ev = EvidenceSet.build(tmp_path, op_id="rejected")
    ev.set_last_correlation_settled(False)
    out = check_zero_tx(ev.inputs(), "rejected")
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.memory_only
def test_zero_tx_insufficient_concurrent_ops(tmp_path):
    _, _, check_zero_tx, _ = comparator_api()
    ev = EvidenceSet.build(tmp_path, op_id="rejected")
    offsets = {"serial_read": 0, "dut_stdin_write": 0, "dut_stdout_read": 0, "serial_write": 0}
    points = [
        {"op_id": "rejected", "kind": "op_start", "action": "command.submit", "cmd_id": "cmd-a",
         "wal_seq": 0, "wire_seq": 1, "endpoint_generation": 1, "offsets": offsets, "settled": True},
        {"op_id": "other", "kind": "op_start", "action": "command.submit", "cmd_id": "cmd-b",
         "wal_seq": 0, "wire_seq": 1, "endpoint_generation": 1, "offsets": offsets, "settled": True},
        {"op_id": "other", "kind": "op_end", "action": "command.submit", "cmd_id": "cmd-b",
         "wal_seq": 0, "wire_seq": 1, "endpoint_generation": 1, "offsets": offsets, "settled": True},
        {"op_id": "rejected", "kind": "op_end", "action": "command.submit", "cmd_id": "cmd-a",
         "wal_seq": 0, "wire_seq": 1, "endpoint_generation": 1, "offsets": offsets, "settled": True},
    ]
    ev.replace_correlation([EvidenceSet._correlation_from_point(index + 1, point)
                            for index, point in enumerate(points)])
    out = check_zero_tx(ev.inputs(), "rejected")
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.memory_only
def test_contiguous_pass_and_fail(tmp_path):
    _, _, _, check_contiguous = comparator_api()
    payloads = [b"echo A1\n", b"echo B1\n"]
    good = EvidenceSet.build(
        tmp_path / "good",
        tx_chunks_wire=[b"echo A1\necho B1\n"],
        tx_chunks_wal=[b"echo A1\necho B1\n"],
    )
    assert check_contiguous(good.inputs(), payloads)["verdict"] == "PASS"

    interleaved = EvidenceSet.build(
        tmp_path / "interleaved",
        tx_chunks_wire=[b"echo Aecho B1\n1\n"],
        tx_chunks_wal=[b"echo Aecho B1\n1\n"],
    )
    assert check_contiguous(interleaved.inputs(), payloads)["verdict"] == "FAIL"


@pytest.mark.memory_only
def test_comparison_schema_valid(tmp_path):
    _, compare, _, _ = comparator_api()
    import jsonschema  # noqa: PLC0415

    outputs = []
    clean = EvidenceSet.build(
        tmp_path / "pass",
        tx_chunks_wire=[b"same\n"],
        tx_chunks_wal=[b"same\n"],
    )
    outputs.append(compare(clean.inputs()))

    different = EvidenceSet.build(
        tmp_path / "fail",
        tx_chunks_wire=[b"left\n"],
        tx_chunks_wal=[b"right\n"],
    )
    outputs.append(compare(different.inputs()))

    incomplete = EvidenceSet.build(
        tmp_path / "insufficient",
        tx_chunks_wire=[b"same\n"],
        tx_chunks_wal=[b"same\n"],
    )
    incomplete.apply_tamper("flip_payload_keep_crc")
    outputs.append(compare(incomplete.inputs()))

    schema_path = REPO_ROOT / "tests" / "acceptance" / "comparison.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for output in outputs:
        validator.validate(output)


@pytest.mark.memory_only
def test_comparator_has_no_product_logic():
    compare_root = SOURCE_ROOT / "uart_stdio_bridge" / "compare"
    files = sorted(compare_root.glob("*.py"))
    assert files, "比較器原始檔不存在"

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        constants = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                constants.add(node.value)
        assert "re" not in imported, f"{path.name} 匯入 re"
        assert "prompt_regex" not in constants
        assert "login_regex" not in constants
