"""wire.ndjson 與 correlation.ndjson 讀取器與完整性檢驗（P2-T7）。

獨立實作，自行檢驗欄位與型別，不依賴外部 jsonschema。
嚴格禁止 import re、sw_core 或共用產品內部邏輯。
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
from typing import Any
import zlib

WIRE_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "case_id",
    "seq",
    "mono_ts_ns",
    "wall_ts",
    "clock_id",
    "port_id",
    "endpoint_generation",
    "dut_generation",
    "dir",
    "observation",
    "stream_offset",
    "len",
    "payload_b64",
    "crc32",
    "source",
    "cmd_id",
    "loss_flag",
    "meta",
)

CORRELATION_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "case_id",
    "seq",
    "mono_ts_ns",
    "wall_ts",
    "clock_id",
    "op_id",
    "step_index",
    "kind",
    "action",
    "settled",
)


def _is_hex64(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower())


def _is_int(v: Any, min_val: int = 0) -> bool:
    return type(v) is int and v >= min_val


def _is_hex8(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 8 and all(c in "0123456789abcdef" for c in s)


def load_wire(
    evidence_dir: pathlib.Path,
    *,
    checkpoint: pathlib.Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """載入 wire.ndjson 紀錄並進行完整性與竄改檢查。

    回傳：(wire 紀錄字典清單, 完整性問題字典清單)。
    問題字典格式：{"code": str, "file": str, "line_no": int, "detail": str}。
    """
    records: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    wire_path = evidence_dir / "wire.ndjson"
    if not wire_path.is_file():
        problems.append({
            "code": "HARNESS_TAMPER_DETECTED",
            "file": str(wire_path),
            "line_no": 0,
            "detail": f"wire.ndjson 檔案不存在：{wire_path}",
        })
        return records, problems

    try:
        raw_bytes = wire_path.read_bytes()
    except OSError as exc:
        problems.append({
            "code": "HARNESS_TAMPER_DETECTED",
            "file": str(wire_path),
            "line_no": 0,
            "detail": f"無法讀取 wire.ndjson：{exc}",
        })
        return records, problems

    # 完整性與前綴驗證（Reviewer A3, A11, B1）
    if checkpoint is not None:
        if not checkpoint.is_file():
            problems.append({
                "code": "HARNESS_INSUFFICIENT_EVIDENCE",
                "file": str(checkpoint),
                "line_no": 0,
                "detail": f"checkpoint 檔案不存在：{checkpoint}",
            })
            return records, problems

        try:
            cp_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append({
                "code": "HARNESS_TAMPER_DETECTED",
                "file": str(checkpoint),
                "line_no": 0,
                "detail": f"解析 checkpoint 失敗：{exc}",
            })
            return records, problems

        if not isinstance(cp_data, dict):
            problems.append({
                "code": "HARNESS_TAMPER_DETECTED",
                "file": str(checkpoint),
                "line_no": 0,
                "detail": "checkpoint 內容非 JSON 字典",
            })
            return records, problems

        wire_len = cp_data.get("wire_len")
        wire_seq = cp_data.get("wire_seq")
        wire_sha = cp_data.get("wire_sha256")

        if not _is_int(wire_len, 0) or not _is_int(wire_seq, 0) or not _is_hex64(wire_sha):
            problems.append({
                "code": "HARNESS_TAMPER_DETECTED",
                "file": str(checkpoint),
                "line_no": 0,
                "detail": "checkpoint 缺少必要欄位或格式錯誤",
            })
            return records, problems

        if len(raw_bytes) < wire_len:
            problems.append({
                "code": "HARNESS_TAMPER_DETECTED",
                "file": str(wire_path),
                "line_no": 0,
                "detail": f"wire 檔案長度 ({len(raw_bytes)}) 小於 checkpoint 長度 ({wire_len})",
            })
            return records, problems

        prefix_sha = hashlib.sha256(raw_bytes[:wire_len]).hexdigest()
        if prefix_sha != str(wire_sha).lower():
            problems.append({
                "code": "HARNESS_TAMPER_DETECTED",
                "file": str(wire_path),
                "line_no": 0,
                "detail": f"wire 前綴 SHA-256 與 checkpoint 不符：預期 {wire_sha}，實際 {prefix_sha}",
            })
            return records, problems

        control_len = cp_data.get("control_len", 0)
        if _is_int(control_len, 1):
            control_sha = cp_data.get("control_sha256")
            control_path = evidence_dir / "control.ndjson"
            if not _is_hex64(control_sha) or not control_path.is_file():
                problems.append({
                    "code": "HARNESS_TAMPER_DETECTED",
                    "file": str(control_path),
                    "line_no": 0,
                    "detail": "control 檔案不存在或缺少有效 control_sha256",
                })
                return records, problems

            ctrl_bytes = control_path.read_bytes()
            if len(ctrl_bytes) < control_len or hashlib.sha256(ctrl_bytes[:control_len]).hexdigest() != str(control_sha).lower():
                problems.append({
                    "code": "HARNESS_TAMPER_DETECTED",
                    "file": str(control_path),
                    "line_no": 0,
                    "detail": "control 前綴長度不足或 SHA-256 不符",
                })
                return records, problems

        bytes_to_verify = raw_bytes[:wire_len]
        if bytes_to_verify and not bytes_to_verify.endswith(b"\n"):
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": 0,
                "detail": "checkpoint wire 前綴最後一行未以換行字元 (\\n) 結尾",
            })
    else:
        bytes_to_verify = raw_bytes
        if bytes_to_verify and not bytes_to_verify.endswith(b"\n"):
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": 0,
                "detail": "wire.ndjson 最後一行未以換行字元 (\\n) 結尾",
            })

        manifest_path = evidence_dir / "bridge-manifest.json"
        if not manifest_path.is_file():
            problems.append({
                "code": "HARNESS_TAMPER_DETECTED",
                "file": str(manifest_path),
                "line_no": 0,
                "detail": f"bridge-manifest.json 不存在：{manifest_path}",
            })
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                problems.append({
                    "code": "HARNESS_TAMPER_DETECTED",
                    "file": str(manifest_path),
                    "line_no": 0,
                    "detail": f"解析 bridge-manifest.json 失敗：{exc}",
                })
                manifest = None

            if not isinstance(manifest, dict):
                problems.append({
                    "code": "HARNESS_TAMPER_DETECTED",
                    "file": str(manifest_path),
                    "line_no": 0,
                    "detail": "bridge-manifest.json 內容非 JSON 字典",
                })
            else:
                expected_sha = manifest.get("evidence", {}).get("wire", {}).get("sha256")
                if not _is_hex64(expected_sha):
                    problems.append({
                        "code": "HARNESS_TAMPER_DETECTED",
                        "file": str(manifest_path),
                        "line_no": 0,
                        "detail": "bridge-manifest 缺少有效之 evidence.wire.sha256",
                    })
                else:
                    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
                    if actual_sha != str(expected_sha).lower():
                        problems.append({
                            "code": "HARNESS_TAMPER_DETECTED",
                            "file": str(wire_path),
                            "line_no": 0,
                            "detail": f"wire.ndjson SHA-256 與 manifest 不符：預期 {expected_sha}，實際 {actual_sha}",
                        })

                if manifest.get("closed_cleanly") is not True or manifest.get("failure") is not None:
                    problems.append({
                        "code": "HARNESS_INSUFFICIENT_EVIDENCE",
                        "file": str(manifest_path),
                        "line_no": 0,
                        "detail": "bridge-manifest 表示 bridge 未正常關閉或記錄 failure",
                    })

                if manifest.get("test_overrides"):
                    problems.append({
                        "code": "HARNESS_INSUFFICIENT_EVIDENCE",
                        "file": str(manifest_path),
                        "line_no": 0,
                        "detail": "bridge-manifest 含有 test_overrides，證據無效（§15.4.3／I8-1）",
                    })

    lines = bytes_to_verify.splitlines()
    expected_seq = 1
    expected_stream_offsets: dict[tuple[str, int, str], int] = {}

    for line_no, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue

        try:
            rec = json.loads(raw_line.decode("utf-8"))
        except Exception as exc:
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": f"JSON 解析失敗：{exc}",
            })
            continue

        if not isinstance(rec, dict):
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": "wire 紀錄必須為 JSON 物件字典",
            })
            continue

        missing = [k for k in WIRE_REQUIRED if k not in rec]
        if missing:
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": f"缺少必要欄位：{', '.join(missing)}",
            })
            continue

        if rec.get("schema_version") != "bridge.v1":
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": f"schema_version 必須為 bridge.v1，實為 {rec.get('schema_version')}",
            })

        if rec.get("source") != "bridge:external":
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": f"source 必須為 bridge:external，實為 {rec.get('source')}",
            })

        if rec.get("cmd_id") is not None:
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": "原始 bridge 紀錄 cmd_id 必須為 null",
            })

        # 型別嚴格檢驗（整數排除 bool）
        seq = rec.get("seq")
        endpoint_gen = rec.get("endpoint_generation")
        dut_gen = rec.get("dut_generation")
        mono_ts = rec.get("mono_ts_ns")
        wall_ts = rec.get("wall_ts")
        clock_id = rec.get("clock_id")
        port_id = rec.get("port_id")
        loss_flag = rec.get("loss_flag")
        meta = rec.get("meta")
        crc32_val = rec.get("crc32")
        len_val = rec.get("len")
        direction = rec.get("dir")
        observation = rec.get("observation")
        payload_b64 = rec.get("payload_b64")

        valid_types = (
            _is_int(seq, 1)
            and _is_int(endpoint_gen, 1)
            and _is_int(dut_gen, 1)
            and _is_int(mono_ts, 0)
            and isinstance(wall_ts, str)
            and isinstance(clock_id, str)
            and isinstance(port_id, str)
            and len(port_id) > 0
            and type(loss_flag) is bool
            and isinstance(meta, dict)
            and _is_hex8(crc32_val)
            and _is_int(len_val, 0)
            and direction in ("TX", "RX", "META")
            and observation in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write", "bridge_meta")
            and isinstance(payload_b64, str)
        )

        if not valid_types:
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": "wire 欄位型別不符契約",
            })
            continue

        if observation == "bridge_meta":
            if direction != "META":
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": "bridge_meta 的 dir 必須為 META",
                })
            if rec.get("stream_offset") is not None:
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": "bridge_meta 的 stream_offset 必須為 null",
                })
            if len_val != 0 or payload_b64 != "" or crc32_val != "00000000":
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": "bridge_meta 的 len/payload_b64/crc32 必須為 0/\"\"/00000000",
                })
            if "event" not in meta:
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": "bridge_meta 的 meta 必須包含 event 鍵",
                })
        else:
            expected_dir = "TX" if observation in ("serial_read", "dut_stdin_write") else "RX"
            if direction != expected_dir:
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": f"{observation} 的 dir 必須為 {expected_dir}，實為 {direction}",
                })
            if not _is_int(rec.get("stream_offset"), 0):
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": f"資料紀錄之 stream_offset 必須為非負整數，實為 {rec.get('stream_offset')}",
                })

        if seq != expected_seq:
            problems.append({
                "code": "HARNESS_SEQ_GAP",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": f"seq 序號不連續：預期 {expected_seq}，實際為 {seq}",
            })
            expected_seq = seq + 1
        else:
            expected_seq += 1

        if observation != "bridge_meta":
            try:
                payload = base64.b64decode(payload_b64, validate=True)
            except Exception as exc:
                problems.append({
                    "code": "HARNESS_SCHEMA_INVALID",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": f"Base64 解碼失敗：{exc}",
                })
                continue

            calc_crc = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
            if crc32_val != calc_crc or len_val != len(payload):
                problems.append({
                    "code": "HARNESS_CRC_MISMATCH",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": f"CRC 或長度不符：預期 crc={calc_crc}, len={len(payload)}；記錄 crc={crc32_val}, len={len_val}",
                })

            offset_key = (port_id, endpoint_gen, str(observation))
            expected_offset = expected_stream_offsets.get(offset_key, 0)
            actual_offset = rec.get("stream_offset")
            if actual_offset != expected_offset:
                problems.append({
                    "code": "HARNESS_OFFSET_GAP",
                    "file": str(wire_path),
                    "line_no": line_no,
                    "detail": f"stream_offset 不連續（{offset_key}）：預期 {expected_offset}，實際為 {actual_offset}",
                })
            if _is_int(actual_offset, 0):
                expected_stream_offsets[offset_key] = actual_offset + len(payload)

        if loss_flag is True:
            problems.append({
                "code": "HARNESS_LOSS_FLAG",
                "file": str(wire_path),
                "line_no": line_no,
                "detail": f"紀錄 loss_flag 為 true（line {line_no}）",
            })

        records.append(rec)

    return records, problems


def load_correlation(evidence_dir: pathlib.Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """載入 correlation.ndjson 紀錄並檢查完整性與 settled 快照。

    回傳：(correlation 紀錄字典清單, 完整性問題字典清單)。
    問題字典格式：{"code": str, "file": str, "line_no": int, "detail": str}。
    """
    records: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    corr_path = evidence_dir / "correlation.ndjson"
    if not corr_path.is_file():
        problems.append({
            "code": "HARNESS_WINDOW_UNSETTLED",
            "file": str(corr_path),
            "line_no": 0,
            "detail": f"correlation.ndjson 不存在：{corr_path}",
        })
        return records, problems

    try:
        raw_bytes = corr_path.read_bytes()
    except OSError as exc:
        problems.append({
            "code": "HARNESS_WINDOW_UNSETTLED",
            "file": str(corr_path),
            "line_no": 0,
            "detail": f"無法讀取 correlation.ndjson：{exc}",
        })
        return records, problems

    for line_no, raw_line in enumerate(raw_bytes.splitlines(), start=1):
        if not raw_line.strip():
            continue

        try:
            rec = json.loads(raw_line.decode("utf-8"))
        except Exception as exc:
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(corr_path),
                "line_no": line_no,
                "detail": f"correlation JSON 解析失敗：{exc}",
            })
            continue

        if not isinstance(rec, dict):
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(corr_path),
                "line_no": line_no,
                "detail": "correlation 紀錄非 JSON 字典",
            })
            continue

        missing = [k for k in CORRELATION_REQUIRED if k not in rec]
        if missing:
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(corr_path),
                "line_no": line_no,
                "detail": f"correlation 缺少必要欄位：{', '.join(missing)}",
            })
            continue

        # 欄位型別驗證
        seq = rec.get("seq")
        mono_ts = rec.get("mono_ts_ns")
        wall_ts = rec.get("wall_ts")
        clock_id = rec.get("clock_id")
        op_id = rec.get("op_id")
        step_idx = rec.get("step_index")
        kind = rec.get("kind")
        action = rec.get("action")
        settled = rec.get("settled")

        if (
            not _is_int(seq, 1)
            or not _is_int(mono_ts, 0)
            or not isinstance(wall_ts, str)
            or not isinstance(clock_id, str)
            or not isinstance(op_id, str)
            or not _is_int(step_idx, 0)
            or not isinstance(kind, str)
            or not isinstance(action, str)
            or type(settled) is not bool
        ):
            problems.append({
                "code": "HARNESS_SCHEMA_INVALID",
                "file": str(corr_path),
                "line_no": line_no,
                "detail": "correlation 欄位型別不符契約",
            })
            continue

        records.append(rec)

    has_settled = any(r.get("settled") is True for r in records)
    if not has_settled:
        problems.append({
            "code": "HARNESS_WINDOW_UNSETTLED",
            "file": str(corr_path),
            "line_no": 0,
            "detail": "correlation 中無任何 settled: true 快照",
        })

    return records, problems

