"""P9-T8 Canary WAL 收割的契約測試。"""
from __future__ import annotations

import base64
import importlib
import json
import os
import zlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)


@pytest.fixture
def harvest_module():
    return importlib.import_module("tools.stage3.canary_harvest")


@pytest.fixture
def rules(harvest_module):
    policy = importlib.import_module("tools.stage3.canary_policy").load_policy(
        ROOT / "tests" / "acceptance" / "canary_policy.json"
    )
    return harvest_module.HarvestRules.from_policy(policy)


def _empty(h):
    return h.Cursor(files={}, tx_window={}, human_window={}, human_buckets={})


def _cmd_id(value: int) -> str:
    return f"{value:032x}"


def _record(
    seq: int,
    *,
    direction: str = "TX",
    source: str = "agent:pytest",
    cmd_id: str | None = None,
    payload: bytes = b"echo canary\n",
    com: str | None = "COM0",
    seconds: int | None = None,
    meta: dict | None = None,
    loss_flag: bool = False,
) -> dict:
    return {
        "seq": seq,
        "mono_ts_ns": seq,
        "wall_ts": (BASE_TIME + timedelta(seconds=seq if seconds is None else seconds)).isoformat(),
        "com": com,
        "dir": direction,
        "source": source,
        "cmd_id": cmd_id,
        "len": len(payload),
        "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "loss_flag": loss_flag,
        "meta": meta or {},
    }


def _wire(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _write_records(path: Path, records: list[dict], *, trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"\n".join(_wire(record) for record in records)
    if trailing_newline and records:
        data += b"\n"
    path.write_bytes(data)


def test_rotation_does_not_double_count(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    _write_records(
        wal,
        [
            _record(1, cmd_id=_cmd_id(1)),
            _record(2, cmd_id=_cmd_id(1), payload=b"different bytes"),
            _record(3, cmd_id=_cmd_id(2)),
        ],
    )
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert sorted(row.cmd_id for row in first.submits) == [_cmd_id(1), _cmd_id(2)]

    os.replace(wal, tmp_path / "raw.wal.ndjson.20261102-010000")
    _write_records(wal, [_record(4, cmd_id=_cmd_id(3))])
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert [row.cmd_id for row in second.submits] == [_cmd_id(3)]


def test_rotate_during_file_listing_keeps_archive_cursor(tmp_path, harvest_module, rules, monkeypatch):
    wal = tmp_path / "raw.wal.ndjson"
    archived = tmp_path / "raw.wal.ndjson.20261102-010000"
    _write_records(wal, [_record(1, cmd_id=_cmd_id(1))])
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    original_is_file = Path.is_file
    rotated = False

    def rotate_during_listing(path: Path) -> bool:
        nonlocal rotated
        if path == wal and not rotated:
            rotated = True
            os.replace(wal, archived)
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", rotate_during_listing)
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)

    assert rotated
    assert not any(item.startswith("vanished:") for item in second.anomalies)
    assert second.submits == ()
    assert next(iter(second.cursor.files.values())).path_last_seen == str(archived)


def test_file_transfer_chunks_not_counted(tmp_path, harvest_module, rules):
    _write_records(
        tmp_path / "raw.wal.ndjson",
        [
            *(_record(i, source="agent", cmd_id=f"ft-1a2b3c4d-{i}") for i in range(1, 200)),
            _record(300, source="agent", cmd_id="ft-1a2b3c4d-md5"),
        ],
    )
    assert harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules).submits == ()


def test_non_uuid_cmd_id_ignored(tmp_path, harvest_module, rules):
    uppercase = _cmd_id(0xABCDEF).upper()
    _write_records(
        tmp_path / "raw.wal.ndjson",
        [_record(1, cmd_id="abc"), _record(2, cmd_id=uppercase)],
    )
    assert harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules).submits == ()


def test_partial_line_waits_and_small_file_growth_keeps_identity(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    _write_records(wal, [_record(1, cmd_id=_cmd_id(1))], trailing_newline=False)
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert first.submits == ()
    first_keys = set(first.cursor.files)

    with wal.open("ab") as stream:
        stream.write(b"\n")
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert [row.cmd_id for row in second.submits] == [_cmd_id(1)]
    assert set(second.cursor.files) == first_keys


def test_excluded_sources_and_meta_not_counted(tmp_path, harvest_module, rules):
    _write_records(
        tmp_path / "raw.wal.ndjson",
        [
            _record(1, source="human:cli-1", cmd_id=_cmd_id(11)),
            _record(2, source="system:recover", cmd_id=_cmd_id(12)),
            _record(3, source="flash", cmd_id=_cmd_id(13)),
            _record(
                4,
                direction="META",
                source="system",
                cmd_id=_cmd_id(14),
                payload=b"",
                meta={"event": "command_completion", "completion_regex": "x"},
            ),
            _record(5, direction="RX", source="device", cmd_id=None),
            _record(6, source="agent:x", cmd_id=""),
        ],
    )
    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert result.submits == ()
    assert {signal.kind for signal in result.signals} == {"human_tx", "recover_tx", "flash_tx"}


def test_reset_archive_collision_suffix(tmp_path, harvest_module, rules):
    first = tmp_path / "raw.wal.ndjson.20261102-010000"
    second = tmp_path / "raw.wal.ndjson.20261102-010000.1"
    _write_records(first, [_record(1, cmd_id=_cmd_id(1))])
    _write_records(second, [_record(1, cmd_id=_cmd_id(2))])
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))

    assert harvest_module.wal_files(tmp_path) == [first, second]
    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert sorted(row.cmd_id for row in result.submits) == [_cmd_id(1), _cmd_id(2)]


def test_truncated_file_rereads_with_anomaly(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    repeated = [_record(i, cmd_id=_cmd_id(1)) for i in range(1, 501)]
    _write_records(wal, repeated)
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert len(first.submits) == 1
    old_size = wal.stat().st_size

    shortened = b"\n".join(_wire(row) for row in repeated[:450]) + b"\n"
    assert 4096 < len(shortened) < old_size
    wal.write_bytes(shortened)
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert any(item.startswith("truncated:") for item in second.anomalies)
    assert [row.cmd_id for row in second.submits] == [_cmd_id(1)]


def test_replaced_file_same_inode_detected(tmp_path, harvest_module, rules, monkeypatch):
    wal = tmp_path / "raw.wal.ndjson"
    initial = [_record(i, cmd_id=_cmd_id(i)) for i in range(1, 8)]
    _write_records(wal, initial)
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    old_stat = os.stat(wal)
    old_size = wal.stat().st_size

    replacement = tmp_path / "replacement.ndjson"
    changed = [_record(i, cmd_id=_cmd_id(100 + i)) for i in range(1, 8)]
    _write_records(replacement, changed)
    assert replacement.stat().st_size >= old_size
    os.replace(replacement, wal)
    real_stat = os.stat

    class SameIdentity:
        def __init__(self, actual):
            self._actual = actual

        def __getattr__(self, name):
            if name == "st_dev":
                return old_stat.st_dev
            if name == "st_ino":
                return old_stat.st_ino
            return getattr(self._actual, name)

    def stat_with_reused_inode(path, *args, **kwargs):
        actual = real_stat(path, *args, **kwargs)
        if Path(path) == wal:
            return SameIdentity(actual)
        return actual

    monkeypatch.setattr(harvest_module.os, "stat", stat_with_reused_inode)
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert any(item.startswith("replaced:") for item in second.anomalies)
    assert sorted(row.cmd_id for row in second.submits) == [
        _cmd_id(100 + i) for i in range(1, 8)
    ]


def test_malformed_line_skipped(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    wal.write_bytes(
        _wire(_record(1, cmd_id=_cmd_id(1)))
        + b"\n{bad\n"
        + _wire(_record(2, cmd_id=_cmd_id(2)))
        + b"\n"
    )
    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert [row.cmd_id for row in result.submits] == [_cmd_id(1), _cmd_id(2)]
    assert any(item.startswith("malformed_line:") for item in result.anomalies)


def test_crc_mismatch_not_counted(tmp_path, harvest_module, rules):
    bad = _record(1, cmd_id=_cmd_id(1))
    bad["crc32"] = "00000000"
    _write_records(tmp_path / "raw.wal.ndjson", [bad, _record(2, cmd_id=_cmd_id(2))])

    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert [row.cmd_id for row in result.submits] == [_cmd_id(2)]
    assert result.signals == ()
    assert any(item.startswith("crc_mismatch:") for item in result.anomalies)


def test_vanished_archive(tmp_path, harvest_module, rules):
    archived = tmp_path / "raw.wal.ndjson.20261102-010000"
    _write_records(archived, [_record(1, cmd_id=_cmd_id(1))])
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    identity = next(iter(first.cursor.files.values())).identity
    archived.unlink()

    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert any(item == f"vanished:{identity}" for item in second.anomalies)
    assert second.cursor.files == {}


def test_recreated_meta_has_single_signal_priority(tmp_path, harvest_module, rules):
    _write_records(
        tmp_path / "raw.wal.ndjson",
        [
            _record(
                1,
                direction="META",
                source="system",
                payload=b"",
                loss_flag=True,
                meta={"event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"},
            ),
            _record(2, direction="RX", source="device", cmd_id=None, loss_flag=True),
        ],
    )

    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert [signal.kind for signal in result.signals] == ["wal_recreated", "wal_loss"]


def test_human_tx_deduped_per_bucket_across_calls(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    _write_records(
        wal,
        [
            _record(i, source="human:cli-1", cmd_id=None, payload=f"{i:02}".encode(), seconds=i)
            for i in range(1, 51)
        ],
    )
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert [signal.kind for signal in first.signals].count("human_tx") == 1

    with wal.open("ab") as stream:
        for i in range(51, 101):
            stream.write(_wire(_record(i, source="human:cli-1", payload=f"{i:03}".encode(), seconds=i)))
            stream.write(b"\n")
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert not any(signal.kind == "human_tx" for signal in second.signals)


def test_unauthorized_source_signal(tmp_path, harvest_module, rules):
    _write_records(tmp_path / "raw.wal.ndjson", [_record(1, source="weird", cmd_id=_cmd_id(1))])
    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert [signal.kind for signal in result.signals] == ["unauthorized_tx"]
    assert [row.cmd_id for row in result.submits] == [_cmd_id(1)]


def test_resend_suspect_same_cmd_same_payload(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    _write_records(wal, [_record(1, cmd_id=_cmd_id(1), seconds=1)])
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)

    with wal.open("ab") as stream:
        stream.write(_wire(_record(2, cmd_id=_cmd_id(1), seconds=2)))
        stream.write(b"\n")
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert [signal.kind for signal in second.signals] == ["resend_suspect"]
    assert "payload_b64" not in asdict(second.signals[0])

    with wal.open("ab") as stream:
        stream.write(_wire(_record(3, cmd_id=_cmd_id(1), payload=b"different bytes", seconds=3)))
        stream.write(b"\n")
    third = harvest_module.harvest(tmp_path, second.cursor, rules=rules)
    assert third.signals == ()


def test_human_replay_suspect_but_not_short_key_repeat(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    _write_records(wal, [_record(1, source="human:cli-1", cmd_id=None, payload=b"abcdefghij")])
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    with wal.open("ab") as stream:
        stream.write(_wire(_record(2, source="human:cli-1", cmd_id=None, payload=b"abcdefghij")))
        stream.write(b"\n")
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert [signal.kind for signal in second.signals] == ["human_replay_suspect"]
    assert "payload_b64" not in asdict(second.signals[0])

    short_dir = tmp_path / "short"
    _write_records(
        short_dir / "raw.wal.ndjson",
        [
            _record(1, source="human:cli-1", cmd_id=None, payload=b"x"),
            _record(2, source="human:cli-1", cmd_id=None, payload=b"x"),
        ],
    )
    short = harvest_module.harvest(short_dir, _empty(harvest_module), rules=rules)
    assert not any(signal.kind == "human_replay_suspect" for signal in short.signals)


def test_flash_signal_rate_limit_survives_calls(tmp_path, harvest_module, rules):
    wal = tmp_path / "raw.wal.ndjson"
    _write_records(wal, [_record(1, source="flash", cmd_id=None, seconds=1)])
    first = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert [signal.kind for signal in first.signals] == ["flash_tx"]

    with wal.open("ab") as stream:
        stream.write(_wire(_record(2, source="flash", cmd_id=None, seconds=30)))
        stream.write(b"\n")
    second = harvest_module.harvest(tmp_path, first.cursor, rules=rules)
    assert second.signals == ()

    with wal.open("ab") as stream:
        stream.write(_wire(_record(3, source="flash", cmd_id=None, seconds=61)))
        stream.write(b"\n")
        stream.write(_wire(_record(4, source="flash", cmd_id=None, seconds=70)))
        stream.write(b"\n")
    third = harvest_module.harvest(tmp_path, second.cursor, rules=rules)
    assert [signal.kind for signal in third.signals] == ["flash_tx"]


def test_cursor_roundtrip_uses_atomic_write(tmp_path, harvest_module, rules):
    _write_records(tmp_path / "raw.wal.ndjson", [_record(1, cmd_id=_cmd_id(1))])
    cursor = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules).cursor
    cursor_path = tmp_path / "state" / "cursor.json"

    harvest_module.save_cursor(cursor_path, cursor)

    assert harvest_module.load_cursor(cursor_path) == cursor
    assert list(cursor_path.parent.iterdir()) == [cursor_path]


def test_no_payload_in_observations(tmp_path, harvest_module, rules):
    _write_records(tmp_path / "raw.wal.ndjson", [_record(1, cmd_id=_cmd_id(1))])
    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert "payload_b64" not in asdict(result.submits[0])
    assert "payload_b64" not in json.dumps([asdict(signal) for signal in result.signals])


def test_min_seq_kept_within_call(tmp_path, harvest_module, rules):
    _write_records(
        tmp_path / "raw.wal.ndjson",
        [_record(7, cmd_id=_cmd_id(1), seconds=7), _record(3, cmd_id=_cmd_id(1), seconds=3)],
    )
    result = harvest_module.harvest(tmp_path, _empty(harvest_module), rules=rules)
    assert len(result.submits) == 1
    assert result.submits[0].seq == 3
    assert any(item.startswith("seq_regress:") for item in result.anomalies)

