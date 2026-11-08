#!/usr/bin/env python3
"""產生 ``contracts/golden/wal_line.tsv``（P3-T7 的 WAL 行序列化基準）。

以 ``sw_core.util.dumps_stable`` 產生 50 筆代表性的 WAL 紀錄行。
輸入為以 unit separator ``\\x1f`` 分隔的 13 欄字串：
``seq\\x1fmono_ts_ns\\x1fwall_ts\\x1fcom\\x1fdir\\x1fsource\\x1fcmd_id\\x1flen\\x1fcrc32\\x1fpayload_b64\\x1floss_flag\\x1fmeta_json\\x1frotation_failed``

用法::

    python3 scripts/rust/goldens/gen_wal_line.py [--out contracts/golden/wal_line.tsv]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import sw_core.util as util
from _tsv import Row, write_tsv


def generate_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    # 1. 基本 RX 紀錄
    records.append({
        "seq": 1,
        "mono_ts_ns": 100000000,
        "wall_ts": "2026-10-16T10:00:00.000000Z",
        "com": "COM0",
        "dir": "RX",
        "source": "uart",
        "cmd_id": None,
        "len": 5,
        "crc32": "3610a686",
        "payload_b64": "aGVsbG8=",
        "loss_flag": False,
        "meta": {},
    })

    # 2. 基本 TX 紀錄（附帶 cmd_id）
    records.append({
        "seq": 2,
        "mono_ts_ns": 105000000,
        "wall_ts": "2026-10-16T10:00:00.005000Z",
        "com": "COM0",
        "dir": "TX",
        "source": "agent",
        "cmd_id": "cmd-001",
        "len": 4,
        "crc32": "d87f7e0c",
        "payload_b64": "dGVzdA==",
        "loss_flag": False,
        "meta": {},
    })

    # 3. 標記 loss_flag=True
    records.append({
        "seq": 3,
        "mono_ts_ns": 110000000,
        "wall_ts": "2026-10-16T10:00:00.010000Z",
        "com": "COM1",
        "dir": "RX",
        "source": "uart",
        "cmd_id": None,
        "len": 0,
        "crc32": "00000000",
        "payload_b64": "",
        "loss_flag": True,
        "meta": {},
    })

    # 4. 包含 rotation_failed=True
    records.append({
        "seq": 4,
        "mono_ts_ns": 120000000,
        "wall_ts": "2026-10-16T10:00:00.020000Z",
        "com": "COM0",
        "dir": "RX",
        "source": "uart",
        "cmd_id": None,
        "len": 12,
        "crc32": "abcd1234",
        "payload_b64": "bG9nZ2luZyBkYXRh",
        "loss_flag": False,
        "meta": {},
        "rotation_failed": True,
    })

    # 5. 包含 rotation_failed=False
    records.append({
        "seq": 5,
        "mono_ts_ns": 130000000,
        "wall_ts": "2026-10-16T10:00:00.030000Z",
        "com": "COM0",
        "dir": "TX",
        "source": "human",
        "cmd_id": None,
        "len": 1,
        "crc32": "0000000a",
        "payload_b64": "Cg==",
        "loss_flag": False,
        "meta": {},
        "rotation_failed": False,
    })

    # 6. com 為 None（例如全域 META）
    records.append({
        "seq": 6,
        "mono_ts_ns": 140000000,
        "wall_ts": "2026-10-16T10:00:00.040000Z",
        "com": None,
        "dir": "META",
        "source": "daemon",
        "cmd_id": None,
        "len": 0,
        "crc32": "00000000",
        "payload_b64": "",
        "loss_flag": False,
        "meta": {
            "event": "DAEMON_START",
            "version": "0.3.2",
        },
    })

    # 7. WAL_RECREATED_AFTER_EXTERNAL_UNLINK meta 事件
    records.append({
        "seq": 7,
        "mono_ts_ns": 150000000,
        "wall_ts": "2026-10-16T10:00:00.050000Z",
        "com": "COM0",
        "dir": "META",
        "source": "wal",
        "cmd_id": None,
        "len": 0,
        "crc32": "00000000",
        "payload_b64": "",
        "loss_flag": False,
        "meta": {
            "event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK",
            "reason": "external_unlink",
            "previous_identity": "daemon-id-old",
            "potentially_lost_seq_range": [10, 50],
        },
    })

    # 8. WAL 自癒重建（無 previous_identity、無 range）
    records.append({
        "seq": 8,
        "mono_ts_ns": 160000000,
        "wall_ts": "2026-10-16T10:00:00.060000Z",
        "com": None,
        "dir": "META",
        "source": "wal",
        "cmd_id": None,
        "len": 0,
        "crc32": "00000000",
        "payload_b64": "",
        "loss_flag": False,
        "meta": {
            "event": "WAL_RECREATED_AFTER_EXTERNAL_UNLINK",
            "reason": "dir_missing",
            "previous_identity": None,
            "potentially_lost_seq_range": None,
        },
    })

    # 9～50. 多樣化合成紀錄（遞增 seq、各種 mono_ts_ns、com、方向與 meta）
    sources = ["uart", "agent", "human", "daemon", "wal"]
    dirs = ["RX", "TX", "META"]
    coms = ["COM0", "COM1", "COM2", None]

    for i in range(9, 51):
        com = coms[i % len(coms)]
        direction = dirs[i % len(dirs)]
        source = sources[i % len(sources)]
        has_cmd = (i % 3 == 0)
        cmd_id = f"cmd-{i:04d}" if has_cmd else None
        loss = (i % 7 == 0)
        has_rot = (i % 11 == 0)
        rot_val = True if (i % 22 == 0) else (False if has_rot else None)

        meta: dict[str, object] = {}
        if i % 4 == 0:
            meta["step"] = i
            meta["note"] = f"test record {i}"
        elif i % 5 == 0:
            meta["recreated"] = True

        rec: dict[str, object] = {
            "seq": i * 1000 + i,
            "mono_ts_ns": 1000000000 + i * 1234567,
            "wall_ts": f"2026-10-16T10:{i:02d}:00.000000Z",
            "com": com,
            "dir": direction,
            "source": source,
            "cmd_id": cmd_id,
            "len": (i * 17) % 1024,
            "crc32": f"{(i * 999999) & 0xFFFFFFFF:08x}",
            "payload_b64": f"payload_{i}",
            "loss_flag": loss,
            "meta": meta,
        }
        if rot_val is not None:
            rec["rotation_failed"] = rot_val

        records.append(rec)

    return records


def encode_input(rec: dict[str, object]) -> str:
    # 格式：seq\x1fmono_ts_ns\x1fwall_ts\x1fcom\x1fdir\x1fsource\x1fcmd_id\x1flen\x1fcrc32\x1fpayload_b64\x1floss_flag\x1fmeta_json\x1frotation_failed
    seq = str(rec["seq"])
    mono_ts_ns = str(rec["mono_ts_ns"])
    wall_ts = str(rec["wall_ts"])
    com = str(rec["com"]) if rec["com"] is not None else ""
    direction = str(rec["dir"])
    source = str(rec["source"])
    cmd_id = str(rec["cmd_id"]) if rec["cmd_id"] is not None else ""
    length = str(rec["len"])
    crc32 = str(rec["crc32"])
    payload_b64 = str(rec["payload_b64"])
    loss_flag = "true" if rec["loss_flag"] else "false"
    meta_json = json.dumps(rec["meta"], sort_keys=True, separators=(",", ":"))
    rot = ""
    if "rotation_failed" in rec:
        rot = "true" if rec["rotation_failed"] else "false"

    return "\x1f".join([
        seq, mono_ts_ns, wall_ts, com, direction, source, cmd_id,
        length, crc32, payload_b64, loss_flag, meta_json, rot,
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "contracts/golden/wal_line.tsv",
        help="輸出路徑（預設 contracts/golden/wal_line.tsv）",
    )
    args = parser.parse_args()

    records = generate_records()
    rows: list[Row] = []
    for idx, rec in enumerate(records):
        case_id = f"wal_{idx + 1:04d}"
        input_str = encode_input(rec)
        expected = util.dumps_stable(rec)
        rows.append((case_id, "to_line", input_str, expected))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_tsv(args.out, rows)
    print(f"Wrote {len(rows)} WAL record lines to {args.out}")


if __name__ == "__main__":
    main()
