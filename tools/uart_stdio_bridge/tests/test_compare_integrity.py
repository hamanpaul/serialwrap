from __future__ import annotations

import pytest

from evidence_builder import EvidenceSet, comparator_api, wal_reader_api


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
