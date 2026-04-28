"""檔案傳輸功能：host ↔ target 透過 UART base64 分段傳輸。"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import uuid
from typing import TYPE_CHECKING, Any

from .util import strip_ansi

if TYPE_CHECKING:
    from .uart_io import UARTBridge

_SENTINEL_BEGIN = "===SW_XFER_BEGIN==="
_SENTINEL_END = "===SW_XFER_END==="


def push_file(
    bridge: UARTBridge,
    local_path: str,
    remote_path: str,
    *,
    chunk_size: int = 2048,
    timeout_s: float = 10.0,
    prompt_regex: str,
    source: str = "file_transfer",
) -> dict[str, Any]:
    """將 host 端檔案推送到 target（透過 UART base64 分段傳輸）。"""
    if not os.path.isfile(local_path):
        return {"ok": False, "error_code": "LOCAL_FILE_NOT_FOUND", "local_path": local_path}

    data = _read_local_file(local_path)
    md5_expected = hashlib.md5(data).hexdigest()
    tmp_name = f"/tmp/.sw_upload_{uuid.uuid4().hex[:12]}"
    cmd_id_prefix = f"ft-{uuid.uuid4().hex[:8]}"

    chunks = _split_chunks(data, chunk_size)
    total = len(chunks)

    for idx, chunk in enumerate(chunks):
        b64 = base64.b64encode(chunk).decode("ascii")
        op = ">" if idx == 0 else ">>"
        cmd = f"printf '%s' '{b64}' | base64 -d {op} {shlex.quote(tmp_name)}"
        pre = bridge.rx_snapshot_len()
        bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-{idx}")
        if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
            _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
            return {
                "ok": False,
                "error_code": "TRANSFER_TIMEOUT",
                "chunks_sent": idx,
                "chunks_total": total,
            }

    # 驗證 checksum
    md5_actual = _remote_md5(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
    if md5_actual is None:
        _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
        return {"ok": False, "error_code": "CHECKSUM_VERIFY_FAILED"}
    if md5_actual != md5_expected:
        _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
        return {
            "ok": False,
            "error_code": "CHECKSUM_MISMATCH",
            "expected": md5_expected,
            "actual": md5_actual,
        }

    # 搬移到目的路徑
    mv_cmd = f"mv {shlex.quote(tmp_name)} {shlex.quote(remote_path)}"
    pre = bridge.rx_snapshot_len()
    bridge.send_command(mv_cmd, source=source, cmd_id=f"{cmd_id_prefix}-mv")
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return {"ok": False, "error_code": "MOVE_TIMEOUT"}

    return {
        "ok": True,
        "bytes": len(data),
        "chunks": total,
        "md5": md5_expected,
        "remote_path": remote_path,
    }


def pull_file(
    bridge: UARTBridge,
    remote_path: str,
    local_path: str | None = None,
    *,
    timeout_s: float = 30.0,
    prompt_regex: str,
    source: str = "file_transfer",
) -> dict[str, Any]:
    """從 target 拉取檔案到 host（透過 UART base64 傳輸）。"""
    if local_path is None:
        local_path = os.path.basename(remote_path)

    cmd_id_prefix = f"ft-{uuid.uuid4().hex[:8]}"

    # 用 sentinel 包裹 base64 輸出，便於可靠擷取
    cmd = (
        f"echo '{_SENTINEL_BEGIN}' && "
        f"base64 < {shlex.quote(remote_path)} && "
        f"echo '{_SENTINEL_END}'"
    )
    pre = bridge.rx_snapshot_len()
    bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-b64")
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return {"ok": False, "error_code": "TRANSFER_TIMEOUT"}

    raw_text = bridge.rx_text_from(pre)
    b64_content = _extract_between_sentinels(raw_text)
    if b64_content is None:
        return {"ok": False, "error_code": "PULL_PARSE_FAILED"}

    try:
        data = base64.b64decode(b64_content)
    except Exception:
        return {"ok": False, "error_code": "BASE64_DECODE_FAILED"}

    # 驗證 checksum
    md5_local = hashlib.md5(data).hexdigest()
    md5_remote = _remote_md5(bridge, remote_path, prompt_regex, timeout_s, source, cmd_id_prefix)
    if md5_remote is not None and md5_remote != md5_local:
        return {
            "ok": False,
            "error_code": "CHECKSUM_MISMATCH",
            "expected": md5_remote,
            "actual": md5_local,
        }

    with open(local_path, "wb") as f:
        f.write(data)

    return {
        "ok": True,
        "bytes": len(data),
        "md5": md5_local,
        "local_path": local_path,
    }


# ── 內部輔助函式 ──────────────────────────────────────────────


def _read_local_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _split_chunks(data: bytes, chunk_size: int) -> list[bytes]:
    """將 data 切成 chunk_size 大小的分段；空檔案回傳一段空 bytes。"""
    if not data:
        return [b""]
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def _remote_md5(
    bridge: UARTBridge,
    path: str,
    prompt_regex: str,
    timeout_s: float,
    source: str,
    cmd_id_prefix: str,
) -> str | None:
    """在 target 上執行 md5sum 並擷取 hash 值。"""
    cmd = f"md5sum {shlex.quote(path)}"
    pre = bridge.rx_snapshot_len()
    bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-md5")
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return None
    raw = bridge.rx_text_from(pre)
    m = re.search(r"([0-9a-f]{32})", raw)
    return m.group(1) if m else None


def _cleanup_remote(
    bridge: UARTBridge,
    path: str,
    prompt_regex: str,
    timeout_s: float,
    source: str,
    cmd_id_prefix: str,
) -> None:
    """盡力清除 target 上的暫存檔（忽略失敗）。"""
    cmd = f"rm -f {shlex.quote(path)}"
    pre = bridge.rx_snapshot_len()
    bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-cleanup")
    bridge.wait_for_regex_from(prompt_regex, pre, timeout_s)


def _extract_between_sentinels(text: str) -> str | None:
    """從 RX 文字中擷取 sentinel 標記之間的 base64 內容。"""
    begin_idx = text.find(_SENTINEL_BEGIN)
    end_idx = text.find(_SENTINEL_END)
    if begin_idx < 0 or end_idx < 0 or end_idx <= begin_idx:
        return None
    content = text[begin_idx + len(_SENTINEL_BEGIN) : end_idx]
    # 去除 ANSI 逸出序列（顏色、游標控制、括弧貼上模式等），再去除空白
    return re.sub(r"\s+", "", strip_ansi(content))
