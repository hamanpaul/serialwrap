"""WAL 與 wire byte stream 切段、比對與規則套用（P2-T7）。

不比對 chunk 邊界、筆數、seq 或時間戳；僅比對位元組串流。
TX: WAL com ∈ port_map[port] ↔ wire serial_read
RX: WAL com ∈ port_map[port] ↔ wire serial_write
嚴格禁止 import re、sw_core 或共用產品邏輯。
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Sequence

from uart_stdio_bridge.compare.wal_reader import WalRecord


def format_context_hex(data: bytes, diff_offset: int, secrets: Sequence[bytes]) -> str:
    """產生首個差異處之 hex 上下文字串（至多 32 位元組），將敏感字串以 ** 遮罩。"""
    if not data:
        return ""

    start = min(diff_offset, len(data))
    if start == len(data) and len(data) > 0:
        start = max(0, len(data) - 32)
    end = min(len(data), start + 32)
    chunk = data[start:end]

    masked = [False] * len(chunk)
    for sec in secrets:
        if not sec:
            continue
        sec_len = len(sec)
        search_start = max(0, start - sec_len + 1)
        search_end = min(len(data), end + sec_len - 1)
        region = data[search_start:search_end]
        idx = 0
        while True:
            pos = region.find(sec, idx)
            if pos == -1:
                break
            abs_pos = search_start + pos
            overlap_start = max(start, abs_pos)
            overlap_end = min(end, abs_pos + sec_len)
            for i in range(overlap_start, overlap_end):
                masked[i - start] = True
            idx = pos + 1

    parts: list[str] = []
    for b, is_masked in zip(chunk, masked):
        if is_masked:
            parts.append("**")
        else:
            parts.append(f"{b:02x}")
    return "".join(parts)


def match_streams(
    wal_bytes: bytes,
    wal_boundaries: set[int],
    wire_bytes: bytes,
    *,
    direction: str,
    rules: tuple[str, ...],
    secrets: tuple[bytes, ...],
) -> tuple[bool, int | None, str | None, str | None, list[dict[str, Any]]]:
    """比對單一串流位元組，套用核准規則並產生差異上下文。

    回傳：(equal, first_diff_offset, wal_context_hex, wire_context_hex, rules_applied)。
    """
    k_wal = 0
    k_wire = 0
    rules_applied: list[dict[str, Any]] = []

    while k_wal < len(wal_bytes) and k_wire < len(wire_bytes):
        if wal_bytes[k_wal] == wire_bytes[k_wire]:
            k_wal += 1
            k_wire += 1
            continue

        if "secret_tx_unlogged" in rules and direction == "TX" and k_wal in wal_boundaries:
            matched_target: bytes | None = None
            for sec in secrets:
                if not sec:
                    continue
                target = sec + b"\n"
                if wire_bytes[k_wire : k_wire + len(target)] == target:
                    matched_target = target
                    break
            if matched_target is not None:
                rules_applied.append({
                    "rule": "secret_tx_unlogged",
                    "dir": "TX",
                    "wire_offset": k_wire,
                    "len": len(matched_target),
                })
                k_wire += len(matched_target)
                continue

        break

    while (
        "secret_tx_unlogged" in rules
        and direction == "TX"
        and k_wal == len(wal_bytes)
        and k_wire < len(wire_bytes)
        and k_wal in wal_boundaries
    ):
        matched_target = None
        for sec in secrets:
            if not sec:
                continue
            target = sec + b"\n"
            if wire_bytes[k_wire : k_wire + len(target)] == target:
                matched_target = target
                break
        if matched_target is not None:
            rules_applied.append({
                "rule": "secret_tx_unlogged",
                "dir": "TX",
                "wire_offset": k_wire,
                "len": len(matched_target),
            })
            k_wire += len(matched_target)
        else:
            break

    if k_wal == len(wal_bytes) and k_wire == len(wire_bytes):
        return True, None, None, None, rules_applied

    first_diff = k_wire
    wal_ctx = format_context_hex(wal_bytes, k_wal, secrets)
    wire_ctx = format_context_hex(wire_bytes, k_wire, secrets)
    return False, first_diff, wal_ctx, wire_ctx, rules_applied


def compare_segment_streams(
    *,
    port_id: str,
    coms: tuple[str, ...],
    endpoint_generation: int,
    wal_records: Sequence[WalRecord],
    wire_records: Sequence[dict[str, Any]],
    wal_seq_range: tuple[int, int],
    wire_offsets: Mapping[str, int],
    rules: tuple[str, ...],
    secrets: tuple[bytes, ...],
) -> list[dict[str, Any]]:
    """比對單一世代分段之 TX 與 RX 串流。"""
    wal_min_seq, wal_max_seq = wal_seq_range
    stream_results: list[dict[str, Any]] = []

    # TX 比對: WAL TX ↔ wire serial_read
    tx_wal_records = [
        r
        for r in wal_records
        if r.dir == "TX" and r.com in coms and wal_min_seq < r.seq <= wal_max_seq
    ]
    tx_wal_bytes = b"".join(r.payload for r in tx_wal_records)
    tx_wal_boundaries: set[int] = {0}
    current_b = 0
    for r in tx_wal_records:
        current_b += len(r.payload)
        tx_wal_boundaries.add(current_b)

    tx_wire_cutoff = wire_offsets.get("serial_read", 0)
    tx_wire_payloads: list[bytes] = []
    for r in wire_records:
        if (
            r.get("port_id") == port_id
            and r.get("endpoint_generation") == endpoint_generation
            and r.get("observation") == "serial_read"
        ):
            offset = r.get("stream_offset", 0)
            if offset < tx_wire_cutoff:
                try:
                    payload = base64.b64decode(r.get("payload_b64", ""), validate=True)
                except Exception:
                    continue
                available = tx_wire_cutoff - offset
                tx_wire_payloads.append(payload[:available])

    tx_wire_bytes = b"".join(tx_wire_payloads)
    tx_equal, tx_diff, tx_wal_ctx, tx_wire_ctx, tx_rules = match_streams(
        tx_wal_bytes,
        tx_wal_boundaries,
        tx_wire_bytes,
        direction="TX",
        rules=rules,
        secrets=secrets,
    )
    if len(tx_wire_bytes) != tx_wire_cutoff:
        tx_equal = False
        if tx_diff is None:
            tx_diff = min(len(tx_wal_bytes), len(tx_wire_bytes))

    stream_results.append({
        "port_id": port_id,
        "com": list(coms),
        "endpoint_generation": endpoint_generation,
        "dir": "TX",
        "wal_bytes": len(tx_wal_bytes),
        "wire_bytes": len(tx_wire_bytes),
        "equal": tx_equal,
        "first_diff_offset": tx_diff,
        "wal_context_hex": tx_wal_ctx,
        "wire_context_hex": tx_wire_ctx,
        "rules_applied": tx_rules,
    })

    # RX 比對: WAL RX ↔ wire serial_write
    rx_wal_records = [
        r
        for r in wal_records
        if r.dir == "RX" and r.com in coms and wal_min_seq < r.seq <= wal_max_seq
    ]
    rx_wal_bytes = b"".join(r.payload for r in rx_wal_records)
    rx_wal_boundaries: set[int] = {0}
    current_b = 0
    for r in rx_wal_records:
        current_b += len(r.payload)
        rx_wal_boundaries.add(current_b)

    rx_wire_cutoff = wire_offsets.get("serial_write", 0)
    rx_wire_payloads: list[bytes] = []
    for r in wire_records:
        if (
            r.get("port_id") == port_id
            and r.get("endpoint_generation") == endpoint_generation
            and r.get("observation") == "serial_write"
        ):
            offset = r.get("stream_offset", 0)
            if offset < rx_wire_cutoff:
                try:
                    payload = base64.b64decode(r.get("payload_b64", ""), validate=True)
                except Exception:
                    continue
                available = rx_wire_cutoff - offset
                rx_wire_payloads.append(payload[:available])

    rx_wire_bytes = b"".join(rx_wire_payloads)
    rx_equal, rx_diff, rx_wal_ctx, rx_wire_ctx, rx_rules = match_streams(
        rx_wal_bytes,
        rx_wal_boundaries,
        rx_wire_bytes,
        direction="RX",
        rules=rules,
        secrets=secrets,
    )
    if len(rx_wire_bytes) != rx_wire_cutoff:
        rx_equal = False
        if rx_diff is None:
            rx_diff = min(len(rx_wal_bytes), len(rx_wire_bytes))

    stream_results.append({
        "port_id": port_id,
        "com": list(coms),
        "endpoint_generation": endpoint_generation,
        "dir": "RX",
        "wal_bytes": len(rx_wal_bytes),
        "wire_bytes": len(rx_wire_bytes),
        "equal": rx_equal,
        "first_diff_offset": rx_diff,
        "wal_context_hex": rx_wal_ctx,
        "wire_context_hex": rx_wire_ctx,
        "rules_applied": rx_rules,
    })

    return stream_results
