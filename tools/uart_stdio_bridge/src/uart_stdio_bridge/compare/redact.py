"""證據遮罩處理器與 redaction-manifest.json 產生（P2-T7、§15.6.4）。

將原始證據中的敏感字串（如密碼、帳密）替換為等長 * 字符，
重算 CRC32 與 Base64，並標註 byte_equivalent: false。
嚴格禁止 import re、sw_core 或共用產品邏輯。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import sys
from typing import Any, Sequence
import zlib

from uart_stdio_bridge.evidence.manifest import ensure_private_dir


def _mask_bytes(data: bytes, secrets: Sequence[tuple[str, bytes]]) -> bytes:
    result = data
    for _, sec in secrets:
        if not sec:
            continue
        mask = b"*" * len(sec)
        result = result.replace(sec, mask)
    return result


def _redact_records_in_stream(
    records: list[dict[str, Any]],
    secrets: Sequence[tuple[str, bytes]],
) -> None:
    """串接整條串流 payload、遮罩跨紀錄 secret 後按原長度切回並重算 Base64/CRC32。"""
    payloads: list[bytes] = []
    for rec in records:
        try:
            p = base64.b64decode(rec.get("payload_b64", ""), validate=True)
        except Exception:
            p = b""
        payloads.append(p)

    joined = b"".join(payloads)
    masked = bytearray(joined)
    for _, sec in secrets:
        if not sec:
            continue
        idx = 0
        while True:
            pos = joined.find(sec, idx)
            if pos == -1:
                break
            masked[pos : pos + len(sec)] = b"*" * len(sec)
            idx = pos + 1

    offset = 0
    for rec, orig_p in zip(records, payloads):
        p_len = len(orig_p)
        new_p = bytes(masked[offset : offset + p_len])
        offset += p_len
        rec["payload_b64"] = base64.b64encode(new_p).decode("ascii")
        rec["len"] = len(new_p)
        rec["crc32"] = f"{zlib.crc32(new_p) & 0xFFFFFFFF:08x}"


def redact(
    evidence_dir: pathlib.Path,
    out_dir: pathlib.Path,
    *,
    secrets: Sequence[tuple[str, bytes]],
) -> dict[str, Any]:
    """產生遮罩衍生目錄與 redaction-manifest.json。"""
    for name, sec in secrets:
        if len(sec) == 0:
            raise ValueError(f"secret {name} 不得為空")

    evidence_dir = pathlib.Path(evidence_dir).resolve()
    out_dir = pathlib.Path(out_dir).resolve()
    ensure_private_dir(out_dir)

    files_list: list[dict[str, str]] = []

    for path in sorted(evidence_dir.rglob("*")):
        if not path.is_file():
            continue

        rel_path = path.relative_to(evidence_dir).as_posix()
        raw_bytes = path.read_bytes()
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()

        dest_file = out_dir / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        is_wire = path.name == "wire.ndjson"
        is_wal = path.name == "raw.wal.ndjson" or path.name.startswith("raw.wal.ndjson.")

        if is_wire or is_wal:
            lines = raw_bytes.splitlines()
            parsed_records: list[dict[str, Any] | None] = []
            for line in lines:
                if not line.strip():
                    parsed_records.append(None)
                    continue
                try:
                    rec = json.loads(line.decode("utf-8"))
                    if isinstance(rec, dict):
                        parsed_records.append(rec)
                    else:
                        parsed_records.append(None)
                except Exception:
                    parsed_records.append(None)

            # 按串流分組
            stream_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            for rec in parsed_records:
                if rec is None or not rec.get("payload_b64"):
                    continue
                if is_wire:
                    if rec.get("observation") == "bridge_meta":
                        continue
                    key = (rec.get("port_id"), rec.get("endpoint_generation"), rec.get("observation"))
                else:
                    key = (rec.get("com"), rec.get("dir"))
                stream_groups.setdefault(key, []).append(rec)

            for group in stream_groups.values():
                _redact_records_in_stream(group, secrets)

            redacted_lines: list[bytes] = []
            for orig_line, rec in zip(lines, parsed_records):
                if not orig_line.strip():
                    continue
                if rec is not None:
                    rec_bytes = json.dumps(
                        rec,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    final_bytes = _mask_bytes(rec_bytes, secrets)
                    redacted_lines.append(final_bytes + b"\n")
                else:
                    masked_line = _mask_bytes(orig_line, secrets)
                    redacted_lines.append(masked_line + b"\n")

            redacted_content = b"".join(redacted_lines)
            dest_file.write_bytes(redacted_content)
            redacted_sha = hashlib.sha256(redacted_content).hexdigest()
        else:
            redacted_content = _mask_bytes(raw_bytes, secrets)
            dest_file.write_bytes(redacted_content)
            redacted_sha = hashlib.sha256(redacted_content).hexdigest()

        files_list.append({
            "path": rel_path,
            "raw_sha256": raw_sha,
            "redacted_sha256": redacted_sha,
        })

    manifest: dict[str, Any] = {
        "schema_version": "redaction.v1",
        "rules": [
            {"kind": "secret_env", "name": name, "len": len(sec)}
            for name, sec in secrets
        ],
        "files": files_list,
        "byte_equivalent": False,
    }

    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    (out_dir / "redaction-manifest.json").write_bytes(manifest_bytes)

    return manifest


def main(argv: list[str]) -> int:
    """命令列 redact 子命令入口。"""
    parser = argparse.ArgumentParser(prog="uart-stdio-bridge redact")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--secret-env", action="append", default=[])
    args = parser.parse_args(argv)

    evidence_dir = pathlib.Path(args.evidence_dir)
    out_dir = pathlib.Path(args.out)

    if out_dir.exists():
        sys.stderr.write(f"錯誤：輸出目錄已存在：{out_dir}\n")
        return 2

    if not evidence_dir.is_dir():
        sys.stderr.write(f"錯誤：證據目錄不存在：{evidence_dir}\n")
        return 2

    secrets: list[tuple[str, bytes]] = []
    for env_name in args.secret_env:
        val = os.environ.get(env_name)
        if val is None:
            sys.stderr.write(f"錯誤：環境變數 {env_name} 未設定\n")
            return 2
        if len(val) == 0:
            sys.stderr.write(f"錯誤：環境變數 {env_name} 為空\n")
            return 2
        secrets.append((env_name, val.encode("utf-8")))

    try:
        redact(evidence_dir, out_dir, secrets=secrets)
    except Exception as exc:
        sys.stderr.write(f"遮罩失敗：{exc}\n")
        return 2

    return 0

