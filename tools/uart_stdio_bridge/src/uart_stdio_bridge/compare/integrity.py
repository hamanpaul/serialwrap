"""WAL 與 wire 證據完整性總體校驗（P2-T7）。

匯整 wire_reader、wal_reader 與 correlation 檢驗結果，
嚴格落實「完整性先行」原則。
禁止 import re、sw_core 或共用產品邏輯。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import pathlib
from typing import Any

from uart_stdio_bridge.compare.wal_reader import WalRecord, list_wal_files, load_wal
from uart_stdio_bridge.compare.wire_reader import load_correlation, load_wire


@dataclass(frozen=True)
class IntegrityResult:
    wire_records: list[dict[str, Any]]
    wal_records: list[WalRecord]
    correlation_records: list[dict[str, Any]]
    integrity_data: dict[str, Any]
    problems: list[dict[str, Any]]


def verify_integrity(
    evidence_dir: pathlib.Path,
    wal_dir: pathlib.Path,
    *,
    checkpoint: pathlib.Path | None = None,
) -> IntegrityResult:
    """執行完整性檢查並回傳紀錄與問題匯總。"""
    wire_records, wire_problems = load_wire(evidence_dir, checkpoint=checkpoint)
    wal_records, wal_problems = load_wal(wal_dir)
    correlation_records, corr_problems = load_correlation(evidence_dir)

    all_problems = list(wire_problems) + list(wal_problems) + list(corr_problems)

    wire_path = evidence_dir / "wire.ndjson"
    actual_wire_sha = ""
    expected_wire_sha: str | None = None

    if checkpoint is not None and checkpoint.is_file():
        try:
            cp_obj = json.loads(checkpoint.read_text(encoding="utf-8"))
            expected_wire_sha = cp_obj.get("wire_sha256")
            wire_len = cp_obj.get("wire_len")
            if wire_path.is_file() and isinstance(wire_len, int) and wire_len >= 0:
                raw_b = wire_path.read_bytes()
                actual_wire_sha = hashlib.sha256(raw_b[:wire_len]).hexdigest()
            elif wire_path.is_file():
                actual_wire_sha = hashlib.sha256(wire_path.read_bytes()).hexdigest()
        except Exception:
            if wire_path.is_file():
                try:
                    actual_wire_sha = hashlib.sha256(wire_path.read_bytes()).hexdigest()
                except OSError:
                    pass
    else:
        if wire_path.is_file():
            try:
                actual_wire_sha = hashlib.sha256(wire_path.read_bytes()).hexdigest()
            except OSError:
                pass
        manifest_path = evidence_dir / "bridge-manifest.json"
        if manifest_path.is_file():
            try:
                manifest_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
                expected_wire_sha = manifest_obj.get("evidence", {}).get("wire", {}).get("sha256")
            except Exception:
                pass

    wal_file_list = [p.name for p in list_wal_files(wal_dir)]

    integrity_dict = {
        "wire_records": len(wire_records),
        "wire_sha256": actual_wire_sha,
        "wire_sha256_expected": expected_wire_sha,
        "wal_files": wal_file_list,
        "wal_records": len(wal_records),
        "problems": all_problems,
    }

    return IntegrityResult(
        wire_records=wire_records,
        wal_records=wal_records,
        correlation_records=correlation_records,
        integrity_data=integrity_dict,
        problems=all_problems,
    )
