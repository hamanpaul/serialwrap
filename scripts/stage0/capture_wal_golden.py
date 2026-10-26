#!/usr/bin/env python3
"""以 Python 基線 WalWriter 擷取固定輸入的 WAL 與 mirror 位元組。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import sw_core.wal as wal_module
from sw_core.constants import DEFAULT_WAL_ROTATE_BYTES
from sw_core.wal import WalWriter

MONO_TS_NS = 5_798_622_898_976
WALL_UNIX_US = 1_792_033_577_663_778
WALL_TS = "2026-10-15T03:06:17.663778+00:00"

SCENARIOS = {
    "rx_binary": [
        {
            "com": "COM0",
            "dir": "RX",
            "source": "uart",
            "payload_hex": "68656c6c6f0d0a1b5b306d0b7fff",
            "cmd_id": None,
            "loss_flag": False,
            "meta": None,
        }
    ],
    "tx_metadata": [
        {
            "com": "COM0",
            "dir": "TX",
            "source": "agent:x",
            "payload_hex": "c3a9",
            "cmd_id": "c1",
            "loss_flag": False,
            "meta": {"z": 1, "a": "中"},
        },
        {
            "com": "COM0",
            "dir": "META",
            "source": "system",
            "payload_hex": "",
            "cmd_id": None,
            "loss_flag": False,
            "meta": {"event": "x"},
        },
    ],
}


def _capture(name: str, requests: list[dict[str, Any]]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="serialwrap-wal-golden-") as wal_dir:
        with (
            patch.object(wal_module, "monotonic_ns", return_value=MONO_TS_NS),
            patch.object(wal_module, "now_iso", return_value=WALL_TS),
        ):
            writer = WalWriter(
                wal_dir=wal_dir,
                rotate_bytes=DEFAULT_WAL_ROTATE_BYTES,
                clock=lambda: MONO_TS_NS / 1_000_000_000,
            )
            for request in requests:
                writer.append(
                    com=str(request["com"]),
                    direction=str(request["dir"]),
                    source=str(request["source"]),
                    payload=bytes.fromhex(str(request["payload_hex"])),
                    cmd_id=request["cmd_id"],
                    loss_flag=bool(request["loss_flag"]),
                    meta=request["meta"],
                )
            writer.close()
        record = {
            "case": name,
            "initial_seq": 0,
            "mono_ts_ns": MONO_TS_NS,
            "wall_unix_us": WALL_UNIX_US,
            "requests": requests,
            "wal_hex": (Path(writer.wal_path)).read_bytes().hex(),
            "mirror_hex": (Path(writer.mirror_path)).read_bytes().hex(),
        }
    return (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="以 Python WalWriter 擷取 WAL 位元組 golden")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "contracts/golden/wal",
        help="golden 輸出目錄",
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, requests in SCENARIOS.items():
        (args.out_dir / f"{name}.json").write_bytes(_capture(name, requests))
    print(f"已擷取 {len(SCENARIOS)} 組 WAL golden：{args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
