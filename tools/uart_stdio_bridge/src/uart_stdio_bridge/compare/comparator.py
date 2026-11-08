"""WAL↔wire 比較器、窗口判定、不變量檢驗與命令列入口（P2-T7）。

依據 serialwrap-rust-spec-final.md §15.6.3 執行比較程序：
1. 完整性先行
2. 窗口判定
3. 串流比對
4. 核准映射
5. 判定輸出（comparison.v1）

嚴格禁止 import re、sw_core 或共用產品邏輯。
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import sys
from typing import Any, Mapping, Sequence

from uart_stdio_bridge import __version__
from uart_stdio_bridge.compare.integrity import verify_integrity
from uart_stdio_bridge.compare.rules import load_rules
from uart_stdio_bridge.compare.streams import compare_segment_streams
from uart_stdio_bridge.identity import tool_tree_sha256

VERDICTS: tuple[str, ...] = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
EXIT_CODES: dict[str, int] = {"PASS": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 4}


@dataclass(frozen=True)
class CompareInputs:
    evidence_dir: pathlib.Path
    wal_dir: pathlib.Path
    port_map: Mapping[str, tuple[str, ...]]
    rules: tuple[str, ...] = ()
    secrets: tuple[bytes, ...] = ()
    until_op: str | None = None
    checkpoint: pathlib.Path | None = None


def compare(inputs: CompareInputs) -> dict[str, Any]:
    """比對 WAL 與 wire 證據，回傳符合 comparison.v1 schema 之字典。"""
    if not inputs.port_map:
        raise ValueError("port_map 不得為空")

    if any(len(s) == 0 for s in inputs.secrets):
        raise ValueError("secret 不得為空字串")

    load_rules(inputs.rules)

    for port, coms in inputs.port_map.items():
        if len(coms) > 1 and "wal_com_alias" not in inputs.rules:
            raise ValueError(f"埠 {port} 指定多個 WAL com 別名 {coms}，必須啟用 wal_com_alias 規則")

    evidence_dir = pathlib.Path(inputs.evidence_dir).resolve()
    wal_dir = pathlib.Path(inputs.wal_dir).resolve()
    checkpoint_path = pathlib.Path(inputs.checkpoint).resolve() if inputs.checkpoint else None

    tool_info = {
        "name": "uart-stdio-bridge",
        "version": __version__,
        "tree_sha256": tool_tree_sha256(),
    }

    integrity_res = verify_integrity(evidence_dir, wal_dir, checkpoint=checkpoint_path)
    wire_records = integrity_res.wire_records
    wal_records = integrity_res.wal_records
    correlation_records = integrity_res.correlation_records
    problems = list(integrity_res.problems)

    run_id = "compare-run"
    case_id = "compare-case"
    if wire_records:
        run_id = str(wire_records[0].get("run_id", run_id))
        case_id = str(wire_records[0].get("case_id", case_id))
    elif correlation_records:
        run_id = str(correlation_records[0].get("run_id", run_id))
        case_id = str(correlation_records[0].get("case_id", case_id))

    zero_offsets = {
        "serial_read": 0,
        "dut_stdin_write": 0,
        "dut_stdout_read": 0,
        "serial_write": 0,
    }

    if problems:
        reasons_list: list[str] = []
        for p in problems:
            code = p["code"]
            if code not in reasons_list:
                reasons_list.append(code)
        return {
            "schema_version": "comparison.v1",
            "run_id": run_id,
            "case_id": case_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reasons": reasons_list,
            "tool": tool_info,
            "integrity": integrity_res.integrity_data,
            "window": {
                "until_op": inputs.until_op,
                "wal_seq": 0,
                "wire_offsets": zero_offsets,
                "endpoint_generation": 1,
            },
            "streams": [],
            "invariants": [],
        }

    # 窗口判定
    window_record: dict[str, Any] | None = None
    if inputs.until_op is not None:
        for r in correlation_records:
            if (
                r.get("op_id") == inputs.until_op
                and r.get("kind") == "op_start"
                and r.get("settled") is True
            ):
                window_record = r
                break
    else:
        for r in reversed(correlation_records):
            if r.get("settled") is True:
                window_record = r
                break

    if window_record is None:
        return {
            "schema_version": "comparison.v1",
            "run_id": run_id,
            "case_id": case_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reasons": ["HARNESS_WINDOW_UNSETTLED"],
            "tool": tool_info,
            "integrity": integrity_res.integrity_data,
            "window": {
                "until_op": inputs.until_op,
                "wal_seq": 0,
                "wire_offsets": zero_offsets,
                "endpoint_generation": 1,
            },
            "streams": [],
            "invariants": [],
        }

    # 嚴格驗證 window_record 欄位與型別（Reviewer A5, A6, B2）
    wal_seq_val = window_record.get("wal_seq")
    wire_info = window_record.get("wire")
    if (
        type(wal_seq_val) is not int
        or isinstance(wal_seq_val, bool)
        or wal_seq_val < 0
        or not isinstance(wire_info, dict)
    ):
        return {
            "schema_version": "comparison.v1",
            "run_id": run_id,
            "case_id": case_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reasons": ["HARNESS_SCHEMA_INVALID"],
            "tool": tool_info,
            "integrity": integrity_res.integrity_data,
            "window": {
                "until_op": inputs.until_op,
                "wal_seq": 0,
                "wire_offsets": zero_offsets,
                "endpoint_generation": 1,
            },
            "streams": [],
            "invariants": [],
        }

    raw_offsets = wire_info.get("offsets")
    wire_seq_val = wire_info.get("wire_seq")
    endpoint_gen_val = wire_info.get("endpoint_generation")

    if (
        not isinstance(raw_offsets, dict)
        or not all(type(raw_offsets.get(k)) is int and not isinstance(raw_offsets.get(k), bool) and raw_offsets.get(k) >= 0 for k in zero_offsets)
        or type(wire_seq_val) is not int
        or isinstance(wire_seq_val, bool)
        or wire_seq_val < 0
        or type(endpoint_gen_val) is not int
        or isinstance(endpoint_gen_val, bool)
        or endpoint_gen_val < 1
    ):
        return {
            "schema_version": "comparison.v1",
            "run_id": run_id,
            "case_id": case_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reasons": ["HARNESS_SCHEMA_INVALID"],
            "tool": tool_info,
            "integrity": integrity_res.integrity_data,
            "window": {
                "until_op": inputs.until_op,
                "wal_seq": 0,
                "wire_offsets": zero_offsets,
                "endpoint_generation": 1,
            },
            "streams": [],
            "invariants": [],
        }

    window_wal_seq = wal_seq_val
    window_wire_seq = wire_seq_val
    window_wire_offsets = {k: int(raw_offsets.get(k, 0)) for k in zero_offsets}
    window_endpoint_generation = endpoint_gen_val

    window_data = {
        "until_op": inputs.until_op,
        "wal_seq": window_wal_seq,
        "wire_offsets": window_wire_offsets,
        "endpoint_generation": window_endpoint_generation,
    }

    if checkpoint_path is not None and checkpoint_path.is_file():
        try:
            cp_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if window_wire_seq > cp_data.get("wire_seq", 0):
                return {
                    "schema_version": "comparison.v1",
                    "run_id": run_id,
                    "case_id": case_id,
                    "verdict": "INSUFFICIENT_EVIDENCE",
                    "reasons": ["HARNESS_TAMPER_DETECTED"],
                    "tool": tool_info,
                    "integrity": integrity_res.integrity_data,
                    "window": window_data,
                    "streams": [],
                    "invariants": [],
                }
        except Exception:
            pass

    # 涵蓋率與一致性驗證（Reviewer A5, B2）
    all_wire_ports = {str(r.get("port_id")) for r in wire_records if r.get("port_id")}
    for p_id in inputs.port_map:
        if p_id not in all_wire_ports:
            return {
                "schema_version": "comparison.v1",
                "run_id": run_id,
                "case_id": case_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                "tool": tool_info,
                "integrity": integrity_res.integrity_data,
                "window": window_data,
                "streams": [],
                "invariants": [],
            }

    wire_in_window = [r for r in wire_records if int(r.get("seq", 0)) <= window_wire_seq]
    for r in wire_in_window:
        if r.get("port_id") and r.get("port_id") not in inputs.port_map:
            return {
                "schema_version": "comparison.v1",
                "run_id": run_id,
                "case_id": case_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                "tool": tool_info,
                "integrity": integrity_res.integrity_data,
                "window": window_data,
                "streams": [],
                "invariants": [],
            }

    all_mapped_coms = {c for coms in inputs.port_map.values() for c in coms}
    for r in wal_records:
        if r.seq <= window_wal_seq and r.com not in all_mapped_coms:
            return {
                "schema_version": "comparison.v1",
                "run_id": run_id,
                "case_id": case_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                "tool": tool_info,
                "integrity": integrity_res.integrity_data,
                "window": window_data,
                "streams": [],
                "invariants": [],
            }

    if window_wal_seq > 0:
        max_wal = max((r.seq for r in wal_records), default=0)
        if max_wal < window_wal_seq:
            return {
                "schema_version": "comparison.v1",
                "run_id": run_id,
                "case_id": case_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                "tool": tool_info,
                "integrity": integrity_res.integrity_data,
                "window": window_data,
                "streams": [],
                "invariants": [],
            }

    # 檢查窗口內是否有 wal reset（Reviewer A12）
    try:
        window_idx = correlation_records.index(window_record)
    except ValueError:
        window_idx = len(correlation_records)

    for r in correlation_records[: window_idx + 1]:
        if r.get("action") == "cli" and isinstance(r.get("argv"), list):
            argv = r["argv"]
            for i in range(len(argv) - 1):
                if argv[i] == "wal" and argv[i + 1] == "reset":
                    return {
                        "schema_version": "comparison.v1",
                        "run_id": run_id,
                        "case_id": case_id,
                        "verdict": "INSUFFICIENT_EVIDENCE",
                        "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                        "tool": tool_info,
                        "integrity": integrity_res.integrity_data,
                        "window": window_data,
                        "streams": [],
                        "invariants": [],
                    }
        if r.get("marker") == "wal reset":
            return {
                "schema_version": "comparison.v1",
                "run_id": run_id,
                "case_id": case_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                "tool": tool_info,
                "integrity": integrity_res.integrity_data,
                "window": window_data,
                "streams": [],
                "invariants": [],
            }

    # 檢查窗口內是否有 WAL loss_flag
    for wal_rec in wal_records:
        if wal_rec.seq <= window_wal_seq and wal_rec.loss_flag:
            return {
                "schema_version": "comparison.v1",
                "run_id": run_id,
                "case_id": case_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "reasons": ["HARNESS_LOSS_FLAG"],
                "tool": tool_info,
                "integrity": integrity_res.integrity_data,
                "window": window_data,
                "streams": [],
                "invariants": [],
            }

    # 檢查多世代切段（Reviewer A4）
    distinct_gens: list[int] = []
    for r in wire_in_window:
        gen = int(r.get("endpoint_generation", 1))
        if gen not in distinct_gens:
            distinct_gens.append(gen)
    distinct_gens.sort()

    segments: list[dict[str, Any]] = []
    if len(distinct_gens) <= 1:
        segments.append({
            "endpoint_generation": window_endpoint_generation,
            "wal_seq_range": (0, window_wal_seq),
            "wire_offsets": window_wire_offsets,
        })
    else:
        final_offsets: dict[int, dict[str, int]] = {}
        for g in distinct_gens:
            g_offsets = {k: 0 for k in zero_offsets}
            for obs in zero_offsets:
                obs_records = [
                    r for r in wire_in_window
                    if int(r.get("endpoint_generation", 1)) == g and r.get("observation") == obs
                ]
                if obs_records:
                    last_rec = max(obs_records, key=lambda x: int(x.get("stream_offset", 0)))
                    g_offsets[obs] = int(last_rec.get("stream_offset", 0)) + int(last_rec.get("len", 0))
            final_offsets[g] = g_offsets

        last_wal_seq = 0
        for idx_g, gen in enumerate(distinct_gens):
            if idx_g < len(distinct_gens) - 1:
                matching_snap: dict[str, Any] | None = None
                for r in correlation_records[:window_idx]:
                    if r.get("settled") is not True:
                        continue
                    w_info = r.get("wire")
                    if not isinstance(w_info, dict):
                        continue
                    if int(w_info.get("endpoint_generation", 0)) != gen:
                        continue
                    w_offsets = {k: int((w_info.get("offsets") or {}).get(k, 0)) for k in zero_offsets}
                    if w_offsets != final_offsets[gen]:
                        continue
                    r_wal_seq = int(r.get("wal_seq", 0))
                    if r_wal_seq < last_wal_seq:
                        continue
                    matching_snap = r

                if matching_snap is None:
                    return {
                        "schema_version": "comparison.v1",
                        "run_id": run_id,
                        "case_id": case_id,
                        "verdict": "INSUFFICIENT_EVIDENCE",
                        "reasons": ["HARNESS_INSUFFICIENT_EVIDENCE"],
                        "tool": tool_info,
                        "integrity": integrity_res.integrity_data,
                        "window": window_data,
                        "streams": [],
                        "invariants": [],
                    }

                snap_wal_seq = int(matching_snap.get("wal_seq", 0))
                snap_wire_offsets = final_offsets[gen]
                segments.append({
                    "endpoint_generation": gen,
                    "wal_seq_range": (last_wal_seq, snap_wal_seq),
                    "wire_offsets": snap_wire_offsets,
                })
                last_wal_seq = snap_wal_seq
            else:
                segments.append({
                    "endpoint_generation": gen,
                    "wal_seq_range": (last_wal_seq, window_wal_seq),
                    "wire_offsets": window_wire_offsets,
                })

    all_streams: list[dict[str, Any]] = []
    for port_id, coms in inputs.port_map.items():
        for seg in segments:
            seg_results = compare_segment_streams(
                port_id=port_id,
                coms=coms,
                endpoint_generation=seg["endpoint_generation"],
                wal_records=wal_records,
                wire_records=wire_records,
                wal_seq_range=seg["wal_seq_range"],
                wire_offsets=seg["wire_offsets"],
                rules=inputs.rules,
                secrets=inputs.secrets,
            )
            all_streams.extend(seg_results)

    all_equal = all(s["equal"] for s in all_streams) if all_streams else False
    if not all_streams:
        verdict = "INSUFFICIENT_EVIDENCE"
        reasons = ["HARNESS_INSUFFICIENT_EVIDENCE"]
    elif all_equal:
        verdict = "PASS"
        reasons = []
    else:
        verdict = "FAIL"
        reasons = ["HARNESS_STREAM_MISMATCH"]

    return {
        "schema_version": "comparison.v1",
        "run_id": run_id,
        "case_id": case_id,
        "verdict": verdict,
        "reasons": reasons,
        "tool": tool_info,
        "integrity": integrity_res.integrity_data,
        "window": window_data,
        "streams": all_streams,
        "invariants": [],
    }


def check_zero_tx(inputs: CompareInputs, op_id: str) -> dict[str, Any]:
    """檢驗拒絕或未授權命令期間是否為零 TX 寫入。"""
    res = verify_integrity(inputs.evidence_dir, inputs.wal_dir, checkpoint=inputs.checkpoint)
    if res.problems:
        codes = sorted(list({p["code"] for p in res.problems}))
        return {
            "id": "zero_tx_on_reject",
            "op_id": op_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "detail": f"證據完整性檢查失敗：{', '.join(codes)}",
        }

    raw_corr_records = res.correlation_records
    wire_records = res.wire_records
    wal_records = res.wal_records

    op_start: dict[str, Any] | None = None
    op_end: dict[str, Any] | None = None
    start_idx = -1
    end_idx = -1

    for idx, r in enumerate(raw_corr_records):
        if r.get("op_id") == op_id:
            if r.get("kind") == "op_start":
                op_start = r
                start_idx = idx
            elif r.get("kind") == "op_end":
                op_end = r
                end_idx = idx

    if op_start is None or op_end is None or start_idx > end_idx:
        return {
            "id": "zero_tx_on_reject",
            "op_id": op_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "detail": "找不到對應之 op_start 與 op_end 快照",
        }

    if op_start.get("settled") is not True or op_end.get("settled") is not True:
        return {
            "id": "zero_tx_on_reject",
            "op_id": op_id,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "detail": "op_start 或 op_end 快照尚未 settled",
        }

    end_wire_seq = int((op_end.get("wire") or {}).get("wire_seq", 0))
    if inputs.checkpoint is not None and inputs.checkpoint.is_file():
        try:
            cp_data = json.loads(inputs.checkpoint.read_text(encoding="utf-8"))
            if end_wire_seq > int(cp_data.get("wire_seq", 0)):
                return {
                    "id": "zero_tx_on_reject",
                    "op_id": op_id,
                    "verdict": "INSUFFICIENT_EVIDENCE",
                    "detail": "end_wire_seq 超出 checkpoint wire_seq",
                }
        except Exception:
            pass

    # 檢查並行衝突操作（Reviewer A10, B5）
    def is_nested_coordinator_step(step_op_id: str) -> bool:
        return len(step_op_id) > 5 and step_op_id[0] == "s" and step_op_id[1:4].isdigit() and step_op_id[4] == "-"

    other_ops_spans: dict[str, list[int]] = {}
    for idx, r in enumerate(raw_corr_records):
        o_id = r.get("op_id")
        if o_id and o_id != op_id:
            if o_id not in other_ops_spans:
                other_ops_spans[o_id] = [idx, idx]
            else:
                other_ops_spans[o_id][1] = idx

    for other_id, (o_start, o_end) in other_ops_spans.items():
        if not (o_end < start_idx or o_start > end_idx):
            if o_start >= start_idx and o_end <= end_idx and is_nested_coordinator_step(other_id):
                continue
            return {
                "id": "zero_tx_on_reject",
                "op_id": op_id,
                "verdict": "INSUFFICIENT_EVIDENCE",
                "detail": f"區間內偵測到其他並行操作（concurrent overlapping op: {other_id}）",
            }

    start_wire_offsets = (op_start.get("wire") or {}).get("offsets") or {}
    end_wire_offsets = (op_end.get("wire") or {}).get("offsets") or {}
    start_sr = int(start_wire_offsets.get("serial_read", 0))
    end_sr = int(end_wire_offsets.get("serial_read", 0))
    if end_sr > start_sr:
        return {
            "id": "zero_tx_on_reject",
            "op_id": op_id,
            "verdict": "FAIL",
            "detail": f"操作期間 wire serial_read 增加 {end_sr - start_sr} 位元組",
        }

    start_wire_seq = int((op_start.get("wire") or {}).get("wire_seq", 0))
    for r in wire_records:
        seq = int(r.get("seq", 0))
        if start_wire_seq < seq <= end_wire_seq:
            if r.get("observation") == "serial_read" and int(r.get("len", 0)) > 0:
                return {
                    "id": "zero_tx_on_reject",
                    "op_id": op_id,
                    "verdict": "FAIL",
                    "detail": "操作期間觀測到 wire serial_read 紀錄",
                }

    start_wal_seq = int(op_start.get("wal_seq", 0))
    end_wal_seq = int(op_end.get("wal_seq", 0))
    mapped_coms = {c for coms in inputs.port_map.values() for c in coms} if inputs.port_map else None

    for r in wal_records:
        if start_wal_seq < r.seq <= end_wal_seq:
            if mapped_coms is None or r.com in mapped_coms:
                if r.dir == "TX" and len(r.payload) > 0:
                    return {
                        "id": "zero_tx_on_reject",
                        "op_id": op_id,
                        "verdict": "FAIL",
                        "detail": f"操作期間產生 WAL TX 紀錄（seq={r.seq}）",
                    }

    return {
        "id": "zero_tx_on_reject",
        "op_id": op_id,
        "verdict": "PASS",
        "detail": "零 TX 寫入檢驗通過",
    }


def check_contiguous(inputs: CompareInputs, payloads: Sequence[bytes]) -> dict[str, Any]:
    """檢驗多筆 payload 是否在 wire serial_read 串流中連續出現且未被交錯打斷。"""
    res = verify_integrity(inputs.evidence_dir, inputs.wal_dir, checkpoint=inputs.checkpoint)
    if res.problems:
        codes = sorted(list({p["code"] for p in res.problems}))
        return {
            "id": "no_forbidden_interleave",
            "verdict": "INSUFFICIENT_EVIDENCE",
            "detail": f"證據完整性檢查失敗：{', '.join(codes)}",
        }

    wire_records = res.wire_records
    grouped_streams: dict[tuple[str, int], list[bytes]] = {}
    for r in wire_records:
        if r.get("observation") == "serial_read" and r.get("payload_b64"):
            port_id = str(r.get("port_id", ""))
            gen = int(r.get("endpoint_generation", 1))
            if inputs.port_map and port_id not in inputs.port_map:
                continue
            try:
                p_bytes = base64.b64decode(r["payload_b64"], validate=True)
                grouped_streams.setdefault((port_id, gen), []).append(p_bytes)
            except Exception:
                pass

    if not grouped_streams:
        return {
            "id": "no_forbidden_interleave",
            "verdict": "FAIL",
            "detail": "wire 中無任何 serial_read 串流",
        }

    expected_counts = Counter(payloads)
    matched_stream = False
    last_fail_detail = ""

    for stream_key, chunks in grouped_streams.items():
        wire_tx = b"".join(chunks)
        pos = 0
        ordered_ok = True
        for idx, p in enumerate(payloads):
            find_idx = wire_tx.find(p, pos)
            if find_idx == -1:
                ordered_ok = False
                p_sha = hashlib.sha256(p).hexdigest()[:8]
                last_fail_detail = f"payload (idx={idx}, len={len(p)}, sha256={p_sha}) 未在串流中依序連續出現"
                break
            pos = find_idx + len(p)

        if not ordered_ok:
            continue

        counts_ok = True
        for p, expected_n in expected_counts.items():
            actual_n = 0
            cur = wire_tx.find(p)
            while cur != -1:
                actual_n += 1
                cur = wire_tx.find(p, cur + 1)
            if actual_n != expected_n:
                counts_ok = False
                p_sha = hashlib.sha256(p).hexdigest()[:8]
                last_fail_detail = f"payload (len={len(p)}, sha256={p_sha}) 出現次數不符：預期 {expected_n} 次，實際 {actual_n} 次"
                break

        if counts_ok:
            matched_stream = True
            break

    if not matched_stream:
        return {
            "id": "no_forbidden_interleave",
            "verdict": "FAIL",
            "detail": last_fail_detail or "payloads 未在單一埠與端點世代串流中完整且依序呈現",
        }

    return {
        "id": "no_forbidden_interleave",
        "verdict": "PASS",
        "detail": "所有 payload 皆在 wire TX 串流中連續、依序且次數完全相符",
    }


def main(argv: list[str]) -> int:
    """compare 子命令命令列入口。"""
    parser = argparse.ArgumentParser(prog="uart-stdio-bridge compare")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--wal-dir", required=True)
    parser.add_argument("--port-map", action="append", required=True)
    parser.add_argument("--rule", action="append", default=[])
    parser.add_argument("--secret-env", action="append", default=[])
    parser.add_argument("--until-op", default=None)
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--zero-tx", action="append", default=[])
    parser.add_argument("--contiguous-b64", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    evidence_dir = pathlib.Path(args.evidence_dir)
    wal_dir = pathlib.Path(args.wal_dir)
    checkpoint_path = pathlib.Path(args.checkpoint) if args.checkpoint else None
    out_path = pathlib.Path(args.out)

    port_map: dict[str, tuple[str, ...]] = {}
    for mapping_str in args.port_map:
        if "=" in mapping_str:
            port, coms_str = mapping_str.split("=", 1)
            coms = tuple(c.strip() for c in coms_str.split(",") if c.strip())
            port_map[port.strip()] = coms
        else:
            port_map[mapping_str.strip()] = (mapping_str.strip(),)

    secrets: list[bytes] = []
    for env_name in args.secret_env:
        val = os.environ.get(env_name)
        if val is None:
            sys.stderr.write(f"錯誤：環境變數 {env_name} 未設定\n")
            return 2
        if len(val) == 0:
            sys.stderr.write(f"錯誤：環境變數 {env_name} 為空\n")
            return 2
        secrets.append(val.encode("utf-8"))

    inputs = CompareInputs(
        evidence_dir=evidence_dir,
        wal_dir=wal_dir,
        port_map=port_map,
        rules=tuple(args.rule),
        secrets=tuple(secrets),
        until_op=args.until_op,
        checkpoint=checkpoint_path,
    )

    try:
        result = compare(inputs)
        # 執行 invariants 檢驗
        for op_id in args.zero_tx:
            inv = check_zero_tx(inputs, op_id)
            result["invariants"].append(inv)

        if args.contiguous_b64:
            payloads = [base64.b64decode(b64_str, validate=True) for b64_str in args.contiguous_b64]
            inv = check_contiguous(inputs, payloads)
            result["invariants"].append(inv)
    except ValueError as exc:
        sys.stderr.write(f"比對參數錯誤：{exc}\n")
        return 2
    except Exception as exc:
        fallback_result = {
            "schema_version": "comparison.v1",
            "run_id": "compare-run",
            "case_id": "compare-case",
            "verdict": "INSUFFICIENT_EVIDENCE",
            "reasons": ["HARNESS_SCHEMA_INVALID"],
            "tool": {
                "name": "uart-stdio-bridge",
                "version": __version__,
                "tree_sha256": tool_tree_sha256(),
            },
            "integrity": {
                "wire_records": 0,
                "wire_sha256": "",
                "wire_sha256_expected": None,
                "wal_files": [],
                "wal_records": 0,
                "problems": [{"code": "HARNESS_SCHEMA_INVALID", "file": "", "line_no": 0, "detail": str(exc)}],
            },
            "window": {
                "until_op": args.until_op,
                "wal_seq": 0,
                "wire_offsets": {"serial_read": 0, "dut_stdin_write": 0, "dut_stdout_read": 0, "serial_write": 0},
                "endpoint_generation": 1,
            },
            "streams": [],
            "invariants": [],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(fallback_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with out_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(json_text)
        return EXIT_CODES["INSUFFICIENT_EVIDENCE"]

    # 依 invariant 更新總判定（Reviewer A9, B8）
    inv_verdicts = [inv["verdict"] for inv in result["invariants"]]
    if "FAIL" in inv_verdicts:
        result["verdict"] = "FAIL"
        if "HARNESS_INVARIANT_FAIL" not in result["reasons"]:
            result["reasons"].append("HARNESS_INVARIANT_FAIL")
    elif "INSUFFICIENT_EVIDENCE" in inv_verdicts:
        if result["verdict"] != "FAIL":
            result["verdict"] = "INSUFFICIENT_EVIDENCE"
            if "HARNESS_INSUFFICIENT_EVIDENCE" not in result["reasons"]:
                result["reasons"].append("HARNESS_INSUFFICIENT_EVIDENCE")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json_text)

    return EXIT_CODES[result["verdict"]]

