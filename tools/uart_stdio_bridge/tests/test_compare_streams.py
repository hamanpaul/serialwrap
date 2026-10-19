from __future__ import annotations

import base64
import json

import pytest

from evidence_builder import EvidenceSet, comparator_api, rules_api


pytestmark = pytest.mark.core_portable


@pytest.mark.memory_only
def test_chunk_boundaries_ignored(tmp_path):
    _, compare, _, _ = comparator_api()
    payload = bytes(range(256)) * 16
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[bytes((value,)) for value in payload],
        tx_chunks_wal=[payload],
    )
    out = compare(ev.inputs())
    assert out["verdict"] == "PASS"


@pytest.mark.memory_only
def test_rx_direction_compared_against_serial_write(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        rx_chunks_wire=[b"peer-gated\n"],
        rx_chunks_wal=[b"peer-gated\n"],
    )
    ev.set_observation_payload("dut_stdout_read", b"filtered-before-peer\n")
    wire_records = [
        json.loads(line)
        for line in ev.wire_path.read_text(encoding="utf-8").splitlines()
    ]
    stdout = b"".join(
        base64.b64decode(record["payload_b64"])
        for record in wire_records
        if record.get("observation") == "dut_stdout_read"
    )
    serial_write = b"".join(
        base64.b64decode(record["payload_b64"])
        for record in wire_records
        if record.get("observation") == "serial_write"
    )
    assert stdout != serial_write
    assert compare(ev.inputs())["verdict"] == "PASS"


@pytest.mark.memory_only
def test_window_respects_settle_point(tmp_path):
    _, compare, _, _ = comparator_api()
    before = b"settled\n"
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[before, b"w" * 100],
        tx_chunks_wal=[before, b"d" * 100],
        settle_points=[{
            "op_id": "settled-window",
            "kind": "op_end",
            "wal_seq": 1,
            "wire_seq": 3,
            "endpoint_generation": 1,
            "offsets": {
                "serial_read": len(before),
                "dut_stdin_write": len(before),
                "dut_stdout_read": 0,
                "serial_write": 0,
            },
            "settled": True,
        }],
    )
    out = compare(ev.inputs())
    assert out["verdict"] == "PASS"
    assert out["window"]["wal_seq"] == 1
    assert out["window"]["wire_offsets"]["serial_read"] == len(before)


@pytest.mark.memory_only
def test_secret_rule_only_when_enabled(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"command\n", b"s3cr3t\n"],
        tx_chunks_wal=[b"command\n"],
    )
    assert compare(ev.inputs())["verdict"] == "FAIL"

    out = compare(ev.inputs(rules=("secret_tx_unlogged",), secrets=(b"s3cr3t",)))
    assert out["verdict"] == "PASS"
    tx_stream = next(stream for stream in out["streams"] if stream["dir"] == "TX")
    assert len(tx_stream["rules_applied"]) == 1
    assert "s3cr3t" not in json.dumps(out, ensure_ascii=False, sort_keys=True)


@pytest.mark.memory_only
def test_secret_rule_not_mid_record(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"command\ns3cr3t\n"],
        tx_chunks_wal=[b"command\nx", b"3cr3t\n"],
    )
    out = compare(ev.inputs(rules=("secret_tx_unlogged",), secrets=(b"s3cr3t",)))
    assert out["verdict"] == "FAIL"


@pytest.mark.memory_only
def test_unknown_rule_rejected():
    load_rules = rules_api()
    with pytest.raises(ValueError):
        load_rules(["strip_ansi"])


@pytest.mark.memory_only
def test_wal_com_alias_requires_rule(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"probe\n"],
        tx_chunks_wal=[b"probe\n"],
    )
    ev.set_wal_com(0, "PROBE")
    port_map = {"COM0": ("COM0", "PROBE")}
    with pytest.raises(ValueError):
        compare(ev.inputs(port_map=port_map))

    out = compare(ev.inputs(port_map=port_map, rules=("wal_com_alias",)))
    assert out["verdict"] == "PASS"


@pytest.mark.memory_only
def test_wal_loss_flag_insufficient(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"command\n"],
        tx_chunks_wal=[b"command\n"],
    )
    ev.set_wal_loss_flag()
    out = compare(ev.inputs())
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "HARNESS_LOSS_FLAG" in out["reasons"]


@pytest.mark.memory_only
def test_wal_reset_in_window_insufficient(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"command\n"],
        tx_chunks_wal=[b"command\n"],
    )
    ev.append_wal_reset()
    out = compare(ev.inputs())
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.memory_only
def test_multi_generation_requires_split_snapshot(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        wire_generations=[
            {"generation": 1, "tx_chunks": [b"first\n"], "tx_wal_chunks": [b"first\n"]},
            {"generation": 2, "tx_chunks": [b"second\n"], "tx_wal_chunks": [b"second\n"]},
        ],
    )
    assert compare(ev.inputs())["verdict"] == "INSUFFICIENT_EVIDENCE"

    ev.add_reconnect_settle_snapshot()
    assert compare(ev.inputs())["verdict"] == "PASS"
