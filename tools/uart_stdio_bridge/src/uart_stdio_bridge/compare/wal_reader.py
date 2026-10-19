"""WAL 紀錄讀取器與完整性檢驗（P2-T7）。

獨立實作，僅依據 contracts/wal_record.schema.json 與盤點 04 §1.3 欄位定義，
嚴格禁止 import sw_core 或共用產品內部之 WAL 邏輯。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import pathlib
from typing import Any
import zlib

WAL_REQUIRED: tuple[str, ...] = (
    "cmd_id",
    "com",
    "crc32",
    "dir",
    "len",
    "loss_flag",
    "meta",
    "mono_ts_ns",
    "payload_b64",
    "seq",
    "source",
    "wall_ts",
)


@dataclass(frozen=True)
class WalRecord:
    file: str
    line_no: int
    seq: int
    com: str
    dir: str
    source: str
    cmd_id: str | None
    payload: bytes
    loss_flag: bool
    meta: dict[str, Any]


def list_wal_files(wal_dir: pathlib.Path) -> list[pathlib.Path]:
    """列出指定目錄下所有 WAL 檔案。

    歸檔 raw.wal.ndjson.<YYYYmmdd-HHMMSS>[.<N>] 依（時間戳, N）排序，
    現行 raw.wal.ndjson 排在最後。
    """
    if not wal_dir.is_dir():
        return []

    archived: list[tuple[tuple[str, int], pathlib.Path]] = []
    current: pathlib.Path | None = None

    for item in wal_dir.iterdir():
        if not item.is_file():
            continue
        if item.name == "raw.wal.ndjson":
            current = item
        elif item.name.startswith("raw.wal.ndjson."):
            suffix = item.name[len("raw.wal.ndjson."):]
            parts = suffix.split(".")
            if len(parts) == 1:
                timestamp = parts[0]
                n = 0
            elif len(parts) == 2 and parts[1].isdigit():
                timestamp = parts[0]
                n = int(parts[1])
            else:
                continue

            # 時間戳格式必須為 15 字元之 YYYYmmdd-HHMMSS (8 數字 + '-' + 6 數字)
            if (
                len(timestamp) == 15
                and timestamp[:8].isdigit()
                and timestamp[8] == "-"
                and timestamp[9:].isdigit()
            ):
                archived.append(((timestamp, n), item))

    archived.sort(key=lambda pair: pair[0])
    result = [path for _, path in archived]
    if current is not None:
        result.append(current)
    return result


def load_wal(wal_dir: pathlib.Path) -> tuple[list[WalRecord], list[dict[str, Any]]]:
    """載入 WAL 檔案並檢查完整性。

    回傳：(依檔案與行序排列之 WalRecord 清單, 完整性問題字典清單)。
    問題字典格式：{"code": str, "file": str, "line_no": int, "detail": str}。
    """
    records: list[WalRecord] = []
    problems: list[dict[str, Any]] = []

    if not wal_dir.is_dir():
        problems.append({
            "code": "HARNESS_WAL_UNREADABLE",
            "file": str(wal_dir),
            "line_no": 0,
            "detail": f"WAL 目錄不存在或非目錄：{wal_dir}",
        })
        return records, problems

    last_seq = 0
    wal_files = list_wal_files(wal_dir)
    for file_path in wal_files:
        try:
            content = file_path.read_bytes()
        except OSError as exc:
            problems.append({
                "code": "HARNESS_WAL_UNREADABLE",
                "file": str(file_path),
                "line_no": 0,
                "detail": f"無法讀取 WAL 檔案：{exc}",
            })
            continue

        lines = content.splitlines()
        for line_no, raw_line in enumerate(lines, start=1):
            if not raw_line.strip():
                continue

            try:
                rec = json.loads(raw_line.decode("utf-8"))
            except Exception as exc:
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": f"JSON 解析失敗：{exc}",
                })
                continue

            if not isinstance(rec, dict):
                problems.append({
                    "code": "HARNESS_WAL_UNREADABLE",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": "WAL 紀錄必須為 JSON 物件字典",
                })
                continue

            missing = [k for k in WAL_REQUIRED if k not in rec]
            if missing:
                problems.append({
                    "code": "HARNESS_WAL_UNREADABLE",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": f"缺少必要欄位：{', '.join(missing)}",
                })
                continue

            # 型別檢查（整數嚴格排除 bool，crc32 驗證 8 位 16 進位）
            seq_val = rec.get("seq")
            len_val = rec.get("len")
            mono_val = rec.get("mono_ts_ns")
            crc32_val = rec.get("crc32")
            com_val = rec.get("com")
            dir_val = rec.get("dir")
            source_val = rec.get("source")
            cmd_id_val = rec.get("cmd_id")
            loss_flag_val = rec.get("loss_flag")
            meta_val = rec.get("meta")
            wall_ts_val = rec.get("wall_ts")
            payload_b64 = rec.get("payload_b64")

            if (
                type(seq_val) is not int
                or seq_val < 1
                or not isinstance(com_val, str)
                or dir_val not in ("TX", "RX", "META")
                or not isinstance(source_val, str)
                or (cmd_id_val is not None and not isinstance(cmd_id_val, str))
                or type(len_val) is not int
                or len_val < 0
                or type(loss_flag_val) is not bool
                or not isinstance(meta_val, dict)
                or type(mono_val) is not int
                or mono_val < 0
                or not isinstance(wall_ts_val, str)
                or not (
                    isinstance(crc32_val, str)
                    and len(crc32_val) == 8
                    and all(c in "0123456789abcdef" for c in crc32_val)
                )
                or not isinstance(payload_b64, str)
            ):
                problems.append({
                    "code": "HARNESS_WAL_UNREADABLE",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": "WAL 欄位型別或格式不符契約",
                })
                continue

            # 序號嚴格遞增檢查（Reviewer A12：seq 回退或重複為 HARNESS_INSUFFICIENT_EVIDENCE）
            if seq_val <= last_seq:
                problems.append({
                    "code": "HARNESS_INSUFFICIENT_EVIDENCE",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": f"WAL seq 未嚴格遞增（{seq_val} <= {last_seq}），可能發生 wal reset",
                })
                continue
            last_seq = seq_val

            try:
                payload = base64.b64decode(payload_b64, validate=True)
            except Exception as exc:
                problems.append({
                    "code": "HARNESS_WAL_UNREADABLE",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": f"Base64 解碼失敗：{exc}",
                })
                continue

            calc_crc = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
            if crc32_val != calc_crc or len_val != len(payload):
                problems.append({
                    "code": "HARNESS_CRC_MISMATCH",
                    "file": str(file_path),
                    "line_no": line_no,
                    "detail": f"CRC 或長度不符：預期 crc={calc_crc}, len={len(payload)}；記錄 crc={crc32_val}, len={len_val}",
                })
                continue

            loss_flag = bool(loss_flag_val or rec.get("rotation_failed"))
            records.append(
                WalRecord(
                    file=str(file_path),
                    line_no=line_no,
                    seq=seq_val,
                    com=com_val,
                    dir=dir_val,
                    source=source_val,
                    cmd_id=cmd_id_val,
                    payload=payload,
                    loss_flag=loss_flag,
                    meta=dict(meta_val),
                )
            )

    return records, problems

