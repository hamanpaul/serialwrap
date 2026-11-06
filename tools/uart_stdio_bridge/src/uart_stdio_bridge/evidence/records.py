"""bridge.v1 紀錄格式定義與序列化。"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import re
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
META_EVENTS = frozenset(
    {
        "bridge_start",
        "bridge_stop",
        "peer_state",
        "endpoint_generation",
        "dut_generation",
        "dut_stdout_eof",
        "dut_exited",
        "write_uncertain",
        "io_error",
        "mark",
        "fault",
        "evidence_failure",
        "quiesce",
        "checkpoint",
    }
)


def crc32_hex(payload: bytes) -> str:
    """計算與 WAL 相同變體之 8 位小寫 16 進位 CRC32。"""
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def b64(payload: bytes) -> str:
    """標準 Base64 編碼字串（含 padding）。"""
    return base64.b64encode(payload).decode("ascii")


def wall_ts_now() -> str:
    """取得當前 UTC 牆上時間 ISO8601 字串（微秒為 0 時省略小數；時區一律 `+00:00`，與 WAL 相同）。"""
    return datetime.now(timezone.utc).isoformat()


def _check_common(*, seq: int, endpoint_generation: int, dut_generation: int) -> None:
    """驗證 seq 與兩個 generation 皆 ≥1（§15.6.2）。"""
    if type(seq) is not int or seq < 1:
        raise ValueError(f"seq 必須 ≥1：{seq}")
    if (
        type(endpoint_generation) is not int
        or type(dut_generation) is not int
        or endpoint_generation < 1
        or dut_generation < 1
    ):
        raise ValueError(
            f"generation 必須 ≥1：endpoint={endpoint_generation} dut={dut_generation}"
        )


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
    """建立資料傳輸紀錄（DIR 為 TX 或 RX）；未知 observation 一律 ValueError，不猜方向。"""
    if observation not in OBSERVATIONS:
        raise ValueError(f"未知 observation：{observation!r}")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("payload 必須是非空 bytes")
    if type(stream_offset) is not int or stream_offset < 0:
        raise ValueError(f"stream_offset 必須是非負整數：{stream_offset!r}")
    _check_common(seq=seq, endpoint_generation=endpoint_generation, dut_generation=dut_generation)
    direction = OBSERVATIONS[observation]
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
    """建立中繼事件紀錄（DIR 為 META）；事件必須符合 bridge.v1 值域。"""
    event = meta.get("event") if isinstance(meta, dict) else None
    if not isinstance(event, str) or event not in META_EVENTS:
        raise ValueError(f"META 紀錄含未知 meta.event：{event!r}")
    if type(loss_flag) is not bool:
        raise ValueError(f"loss_flag 必須是 bool：{loss_flag!r}")
    if event == "write_uncertain":
        observation = meta.get("observation")
        offset_start = meta.get("offset_start")
        requested_len = meta.get("requested_len")
        if (
            observation not in ("dut_stdin_write", "serial_write")
            or type(offset_start) is not int
            or offset_start < 0
            or type(requested_len) is not int
            or requested_len < 1
            or "accepted_len" not in meta
            or meta["accepted_len"] is not None
            or not isinstance(meta.get("error"), str)
            or not meta["error"]
            or loss_flag is not True
        ):
            raise ValueError("write_uncertain META 必須描述 loss_flag=true 的不確定寫入區間")
    elif event == "evidence_failure":
        code = meta.get("code")
        if (
            not isinstance(code, str)
            or re.fullmatch(r"HARNESS_[A-Z0-9_]+", code) is None
            or loss_flag is not True
        ):
            raise ValueError("evidence_failure META 必須含 HARNESS_* code 並設 loss_flag=true")
    _check_common(seq=seq, endpoint_generation=endpoint_generation, dut_generation=dut_generation)
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
