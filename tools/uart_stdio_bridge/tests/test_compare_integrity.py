from __future__ import annotations

import json

import pytest

from evidence_builder import EvidenceSet, _load_compare_module, comparator_api, wal_reader_api


pytestmark = pytest.mark.core_portable

TAMPERS = {
    "flip_payload_keep_crc": ("HARNESS_CRC_MISMATCH",),
    "flip_payload_fix_crc": ("HARNESS_TAMPER_DETECTED",),
    "delete_middle_record": ("HARNESS_SEQ_GAP", "HARNESS_OFFSET_GAP"),
    "duplicate_record": ("HARNESS_SEQ_GAP",),
    "swap_two_records": ("HARNESS_SEQ_GAP",),
    "change_len": ("HARNESS_CRC_MISMATCH",),
    "set_meta_loss_flag": ("HARNESS_LOSS_FLAG",),
    "truncate_last_line": ("HARNESS_SCHEMA_INVALID",),
    "remove_bridge_manifest": ("HARNESS_TAMPER_DETECTED",),
    "manifest_closed_uncleanly": ("HARNESS_INSUFFICIENT_EVIDENCE",),
    "wal_flip_payload": ("HARNESS_CRC_MISMATCH",),
    "wal_drop_required_key": ("HARNESS_WAL_UNREADABLE",),
    "correlation_unsettled": ("HARNESS_WINDOW_UNSETTLED",),
}


@pytest.mark.parametrize("tamper,codes", sorted(TAMPERS.items()))
@pytest.mark.memory_only
def test_tampered_evidence_never_passes(tmp_path, tamper, codes):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.clean(tmp_path)
    assert compare(ev.inputs())["verdict"] == "PASS"
    ev.apply_tamper(tamper)
    out = compare(ev.inputs())
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert set(codes) <= set(out["reasons"]), out["reasons"]


@pytest.mark.memory_only
def test_content_difference_is_fail_with_first_diff(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_wire=[b"echo a\n", b"echo b\n"],
        tx_wal=[b"echo a\n", b"echo c\n"],
    )
    out = compare(ev.inputs())
    assert out["verdict"] == "FAIL"
    s = next(x for x in out["streams"] if x["dir"] == "TX")
    assert s["equal"] is False and s["first_diff_offset"] == len(b"echo a\necho ")


@pytest.mark.memory_only
def test_wal_archive_order(tmp_path):
    load_wal, list_wal_files = wal_reader_api()
    ev = EvidenceSet.build(tmp_path)
    ev.install_wal_archive_order_fixture()

    files = list_wal_files(ev.wal_dir)
    assert [path.name for path in files] == [
        "raw.wal.ndjson.20261015-030617",
        "raw.wal.ndjson.20261015-030617.1",
        "raw.wal.ndjson",
    ]
    records, problems = load_wal(ev.wal_dir)
    assert problems == []
    assert [record.payload for record in records] == [b"archive-a", b"archive-b", b"current"]


@pytest.mark.memory_only
def test_incomplete_write_uncertain_record_is_insufficient_evidence(tmp_path):
    load_wire = _load_compare_module("wire_reader").load_wire
    ev = EvidenceSet.clean(tmp_path)
    records = [json.loads(line) for line in ev.wire_path.read_text(encoding="utf-8").splitlines()]
    last = records[-1]
    records.append({
        "schema_version": "bridge.v1",
        "run_id": last["run_id"],
        "case_id": last["case_id"],
        "seq": last["seq"] + 1,
        "mono_ts_ns": last["mono_ts_ns"] + 1,
        "wall_ts": last["wall_ts"],
        "clock_id": last["clock_id"],
        "port_id": last["port_id"],
        "endpoint_generation": last["endpoint_generation"],
        "dut_generation": last["dut_generation"],
        "dir": "META",
        "observation": "bridge_meta",
        "stream_offset": None,
        "len": 0,
        "payload_b64": "",
        "crc32": "00000000",
        "source": "bridge:external",
        "cmd_id": None,
        "loss_flag": False,
        "meta": {"event": "write_uncertain"},
    })
    ev.wire_path.write_text(
        "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    manifest = json.loads(ev.manifest_path.read_text(encoding="utf-8"))
    ev._refresh_wire_digest(manifest)

    _, problems = load_wire(ev.evidence_dir)
    codes = {problem["code"] for problem in problems}
    assert "HARNESS_INSUFFICIENT_EVIDENCE" in codes
