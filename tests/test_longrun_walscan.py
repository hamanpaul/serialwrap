"""獨立 WAL 長跑掃描器的契約測試。"""

from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _walscan():
    import importlib

    return importlib.import_module("realhw.acceptance.walscan")


def _record(seq: int, payload: bytes, *, source: str = "human:h1", wall_ts: str = "2026-10-15T03:06:17+00:00",
            crc: int | None = None):
    return {
        "cmd_id": f"cmd-{seq}",
        "com": "COM0",
        "crc32": f"{(zlib.crc32(payload) if crc is None else crc) & 0xFFFFFFFF:08x}",
        "dir": "TX",
        "len": len(payload),
        "loss_flag": False,
        "meta": {},
        "mono_ts_ns": seq * 1_000_000_000,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "seq": seq,
        "source": source,
        "wall_ts": wall_ts,
    }


def _write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in records),
                    encoding="utf-8")


def _scan(module, directory: Path, *, since: str = "2026-10-15T00:00:00+00:00"):
    return module.scan_wal(directory, since_wall_ts=since,
                           allowed_tx_source_regex=r"^(agent:rhw[0-9]+|human:[A-Za-z0-9._:-]+|system|system:[a-z_]+)$")


def test_walscan_counts_human_across_records(tmp_path):
    walscan = _walscan()
    marker = b"HUMAN_TICK_7"
    assert len(marker) == 12
    records = [_record(i + 1, marker[i:i + 1]) for i in range(len(marker))]
    _write(tmp_path / "raw.wal.ndjson", records)
    first = _scan(walscan, tmp_path)
    assert first["cross_context_replay"] == []

    records.append(_record(len(records) + 1, marker, wall_ts="2026-10-15T03:07:17+00:00"))
    _write(tmp_path / "raw.wal.ndjson", records)
    repeated = _scan(walscan, tmp_path)
    assert repeated["cross_context_replay"] == [{"marker": "HUMAN_TICK_7", "count": 2}]


def test_walscan_agent_resend(tmp_path):
    walscan = _walscan()
    payload = b"echo LR_rhw0_3_1234\n"
    _write(tmp_path / "raw.wal.ndjson", [
        _record(1, payload, source="agent:rhw0"),
        _record(2, payload, source="agent:rhw0", wall_ts="2026-10-15T03:07:17+00:00"),
    ])
    result = _scan(walscan, tmp_path)
    assert result["dangerous_auto_resend"] == [{"marker": "LR_rhw0_3_1234", "count": 2}]


def test_walscan_since_filter(tmp_path):
    walscan = _walscan()
    old = _record(1, b"echo LR_rhw0_3_1234\n", wall_ts="2026-10-14T23:59:59+00:00")
    current = _record(2, b"echo LR_rhw0_4_5678\n", wall_ts="2026-10-15T00:00:00+00:00")
    _write(tmp_path / "raw.wal.ndjson", [old, current])
    result = _scan(walscan, tmp_path)
    assert result["records"] == 1
    assert result["dangerous_auto_resend"] == []


def test_walscan_crc_problem(tmp_path):
    walscan = _walscan()
    record = _record(1, b"echo LR_rhw0_3_1234\n", crc=0)
    _write(tmp_path / "raw.wal.ndjson", [record])
    result = _scan(walscan, tmp_path)
    assert result["problems"]


def test_walscan_archives_included(tmp_path):
    walscan = _walscan()
    archive = tmp_path / "raw.wal.ndjson.20261015-030617"
    _write(archive, [_record(1, b"echo LR_rhw0_3_1234\n")])
    result = _scan(walscan, tmp_path)
    assert archive.name in result["files"]
    assert result["records"] == 1
