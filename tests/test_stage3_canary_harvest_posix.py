"""P9-T8 POSIX WAL 大檔串流資源測試。"""
from __future__ import annotations

import base64
import importlib
import json
import resource
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]
ROOT = Path(__file__).resolve().parents[1]


def _record(seq: int) -> dict:
    payload = f"cmd-{seq:05d}".encode().ljust(64, b"x")
    return {
        "seq": seq,
        "mono_ts_ns": seq,
        "wall_ts": (datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(seconds=seq)).isoformat(),
        "com": "COM0",
        "dir": "TX",
        "source": "agent:pytest",
        "cmd_id": f"{seq:032x}",
        "len": len(payload),
        "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "loss_flag": False,
        "meta": {},
    }


def _rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if __import__("sys").platform == "darwin" else peak * 1024)


def test_large_file_streams_posix(tmp_path):
    harvest = importlib.import_module("tools.stage3.canary_harvest")
    policy_module = importlib.import_module("tools.stage3.canary_policy")
    policy = policy_module.load_policy(ROOT / "tests" / "acceptance" / "canary_policy.json")
    wal = tmp_path / "raw.wal.ndjson"
    with wal.open("wb") as stream:
        for seq in range(1, 20_001):
            line = json.dumps(_record(seq), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write(line.encode("utf-8"))
            stream.write(b"\n")
    assert wal.stat().st_size >= 5_000_000
    before = _rss_bytes()

    result = harvest.harvest(
        tmp_path,
        harvest.Cursor(files={}, tx_window={}, human_window={}, human_buckets={}),
        rules=harvest.HarvestRules.from_policy(policy),
    )

    after = _rss_bytes()
    assert len(result.submits) == 20_000
    assert after - before < 64 * 1024 * 1024

