from __future__ import annotations

import base64
import hashlib
import json
import zlib

import pytest

from evidence_builder import EvidenceSet, comparator_api, redact_api


pytestmark = pytest.mark.core_portable


@pytest.mark.memory_only
def test_checkpoint_mid_run_pass_and_tamper(tmp_path):
    _, compare, _, _ = comparator_api()
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"checkpointed\n"],
        tx_chunks_wal=[b"checkpointed\n"],
        closed_cleanly=False,
    )
    checkpoint = ev.write_checkpoint()
    assert compare(ev.inputs(checkpoint=checkpoint))["verdict"] == "PASS"

    wire = ev.wire_path.read_bytes()
    ev.wire_path.write_bytes(wire.replace(b"checkpointed", b"Checkpointed", 1))
    out = compare(ev.inputs(checkpoint=checkpoint))
    assert out["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "HARNESS_TAMPER_DETECTED" in out["reasons"]

    without_checkpoint = compare(ev.inputs())
    assert without_checkpoint["verdict"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.memory_only
def test_redact_masks_secret_and_records_hashes(tmp_path):
    redact = redact_api()
    secret = b"s3cr3t"
    ev = EvidenceSet.build(
        tmp_path / "raw",
        tx_chunks_wire=[b"echo s3cr3t\n"],
        tx_chunks_wal=[b"echo s3cr3t\n"],
    )
    raw_wire = ev.wire_path.read_bytes()
    redacted_dir = tmp_path / "redacted"
    redact(ev.evidence_dir, redacted_dir, secrets=[("TEST_SECRET", secret)])

    redacted_wire_path = redacted_dir / "wire.ndjson"
    redacted_wire = redacted_wire_path.read_bytes()
    records = [json.loads(line) for line in redacted_wire.splitlines()]
    decoded = [
        base64.b64decode(record["payload_b64"])
        for record in records
        if record.get("observation") == "serial_read"
    ]
    assert all(secret not in payload for payload in decoded)
    for record in records:
        payload = base64.b64decode(record["payload_b64"])
        assert record["len"] == len(payload)
        assert record["crc32"] == f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"

    redaction_manifest_path = redacted_dir / "redaction-manifest.json"
    redaction_manifest = json.loads(redaction_manifest_path.read_text(encoding="utf-8"))
    wire_entry = next(item for item in redaction_manifest["files"] if item["path"] == "wire.ndjson")
    assert wire_entry["raw_sha256"] == hashlib.sha256(raw_wire).hexdigest()
    assert wire_entry["redacted_sha256"] == hashlib.sha256(redacted_wire).hexdigest()
    assert redaction_manifest["byte_equivalent"] is False
    assert all(secret not in path.read_bytes() for path in redacted_dir.rglob("*") if path.is_file())


@pytest.mark.memory_only
def test_failure_context_masks_secret(tmp_path):
    _, compare, _, _ = comparator_api()
    secret = b"s3cr3t"
    ev = EvidenceSet.build(
        tmp_path,
        tx_chunks_wire=[b"echo s3cr3t\n"],
        tx_chunks_wal=[b"echo xxxxxx\n"],
    )
    out = compare(ev.inputs(secrets=(secret,)))
    assert out["verdict"] == "FAIL"
    stream = next(item for item in out["streams"] if item["dir"] == "TX")
    secret_hex = secret.hex()
    assert secret_hex not in stream["wal_context_hex"]
    assert secret_hex not in stream["wire_context_hex"]
