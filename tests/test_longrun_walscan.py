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


@pytest.mark.parametrize("source", ["agent", "mcu", "flash", "system:Bad", "human:", "intruder"])
def test_walscan_source_allowlist_applies_to_every_tx_source(tmp_path, source):
    """白名單必須套用到所有有效 TX，不只 `agent:` 前綴（裸 `agent`、未知來源都不得通過）。"""
    walscan = _walscan()
    _write(tmp_path / "raw.wal.ndjson", [_record(1, b"ls\n", source=source)])
    result = _scan(walscan, tmp_path)
    assert {"seq": 1, "source": source, "reason": "source_not_allowed"} in result["unauthorized_tx"]


@pytest.mark.parametrize("source", ["agent:rhw0", "human:abc123def456", "system", "system:recover"])
def test_walscan_allowed_sources_pass_allowlist(tmp_path, source):
    walscan = _walscan()
    _write(tmp_path / "raw.wal.ndjson", [_record(1, b"ls\n", source=source)])
    assert _scan(walscan, tmp_path)["unauthorized_tx"] == []


def test_walscan_seq_discontinuity_is_problem(tmp_path):
    """缺號（含未寫入即 loss 的筆）是證據斷裂，不得讓安全掃描判無違規。"""
    walscan = _walscan()
    _write(tmp_path / "raw.wal.ndjson", [_record(1, b"ls\n"), _record(3, b"ls\n")])
    result = _scan(walscan, tmp_path)
    assert any(item.startswith("seq_discontinuity") for item in result["problems"])


def test_walscan_empty_run_window_is_problem(tmp_path):
    """WAL 檔存在但本 run 區間內無有效紀錄，不得視為「沒有違規」。"""
    walscan = _walscan()
    (tmp_path / "raw.wal.ndjson").write_text("", encoding="utf-8")
    result = _scan(walscan, tmp_path)
    assert result["records"] == 0
    assert "no_run_records" in result["problems"]


def test_walscan_tx_outcome_unknown_is_uncertain(tmp_path):
    walscan = _walscan()
    meta = {"cmd_id": None, "com": "COM0", "crc32": "00000000", "dir": "META", "len": 0, "loss_flag": False,
            "meta": {"event": "tx_outcome_unknown", "requested_bytes": 5, "confirmed_bytes": 2,
                     "unknown_range": [2, 5], "error": "EIO"},
            "mono_ts_ns": 2_000_000_000, "payload_b64": "", "seq": 2, "source": "agent:rhw0",
            "wall_ts": "2026-10-15T03:06:18+00:00"}
    _write(tmp_path / "raw.wal.ndjson", [_record(1, b"ls\n", source="agent:rhw0"), meta])
    result = _scan(walscan, tmp_path)
    assert {"seq": 2, "marker": None, "reason": "tx_outcome_unknown", "kind": "tx_unknown"} in result["uncertain"]


def test_walscan_rotation_failed_is_top_level_flag_only(tmp_path):
    """契約（wal_record.schema.json、sw_core/wal.py）為頂層 `rotation_failed`；meta 內同名鍵不算。"""
    walscan = _walscan()
    top = _record(1, b"ls\n")
    top["rotation_failed"] = True
    nested = _record(2, b"ls\n")
    nested["meta"] = {"rotation_failed": True}
    _write(tmp_path / "raw.wal.ndjson", [top, nested])
    result = _scan(walscan, tmp_path)
    assert result["rotation_failed"] == [1]


def test_walscan_invalid_utf8_is_problem_not_crash(tmp_path):
    walscan = _walscan()
    good = json.dumps(_record(1, b"ls\n"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    (tmp_path / "raw.wal.ndjson").write_bytes(good + b"\n\xff\xfe truncated\n")
    result = _scan(walscan, tmp_path)
    assert any("utf8_decode" in item for item in result["problems"])
    assert result["records"] == 1
