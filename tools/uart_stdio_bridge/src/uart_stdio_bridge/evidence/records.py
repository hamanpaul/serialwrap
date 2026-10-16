"""bridge.v1 紀錄格式定義與序列化。"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Any
import zlib

DIRS = ("TX", "RX", "META")
OBSERVATIONS = {
    "serial_read": "TX",
    "dut_stdin_write": "TX",
    "dut_stdout_read": "RX",
    "serial_write": "RX",
}
META_OBSERVATION = "bridge_meta"
BRIDGE_SOURCE = "bridge:external"


def crc32_hex(payload: bytes) -> str:
    """計算與 WAL 相同變體之 8 位小寫 16 進位 CRC32。"""
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def b64(payload: bytes) -> str:
    """標準 Base64 編碼字串（含 padding）。"""
    return base64.b64encode(payload).decode("ascii")


def wall_ts_now() -> str:
    """取得當前 UTC 牆上時間 ISO8601 字串（微秒為 0 時省略小數）。"""
    dt = datetime.now(timezone.utc)
    if dt.microsecond == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.isoformat()


def dumps_record(rec: dict[str, Any]) -> bytes:
    """將單筆紀錄緊湊序列化為 JSON bytes，加換行尾。"""
    return (
        json.dumps(
            rec,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def make_data_record(
    *,
    run_id: str,
    case_id: str,
    seq: int,
    mono_ts_ns: int,
    wall_ts: str,
    clock_id: str,
    port_id: str,
    endpoint_generation: int,
    dut_generation: int,
    observation: str,
    stream_offset: int,
    payload: bytes,
) -> dict[str, Any]:
    """建立資料傳輸紀錄（DIR 為 TX 或 RX）。"""
    direction = OBSERVATIONS.get(observation, "TX")
    return {
        "schema_version": "bridge.v1",
        "run_id": run_id,
        "case_id": case_id,
        "seq": seq,
        "mono_ts_ns": mono_ts_ns,
        "wall_ts": wall_ts,
        "clock_id": clock_id,
        "port_id": port_id,
        "endpoint_generation": endpoint_generation,
        "dut_generation": dut_generation,
        "dir": direction,
        "observation": observation,
        "stream_offset": stream_offset,
        "len": len(payload),
        "payload_b64": b64(payload),
        "crc32": crc32_hex(payload),
        "source": BRIDGE_SOURCE,
        "cmd_id": None,
        "loss_flag": False,
        "meta": {},
    }


def make_meta_record(
    *,
    run_id: str,
    case_id: str,
    seq: int,
    mono_ts_ns: int,
    wall_ts: str,
    clock_id: str,
    port_id: str,
    endpoint_generation: int,
    dut_generation: int,
    meta: dict[str, Any],
    loss_flag: bool = False,
) -> dict[str, Any]:
    """建立中繼事件紀錄（DIR 為 META）。"""
    return {
        "schema_version": "bridge.v1",
        "run_id": run_id,
        "case_id": case_id,
        "seq": seq,
        "mono_ts_ns": mono_ts_ns,
        "wall_ts": wall_ts,
        "clock_id": clock_id,
        "port_id": port_id,
        "endpoint_generation": endpoint_generation,
        "dut_generation": dut_generation,
        "dir": "META",
        "observation": META_OBSERVATION,
        "stream_offset": None,
        "len": 0,
        "payload_b64": "",
        "crc32": "00000000",
        "source": BRIDGE_SOURCE,
        "cmd_id": None,
        "loss_flag": loss_flag,
        "meta": meta,
    }
