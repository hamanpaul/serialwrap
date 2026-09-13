"""檔案傳輸功能：host ↔ target 透過 UART 分段傳輸。

資料面仍以 base64 表示，但板端工具不再被硬編碼成單一路徑：傳輸前以
實際解碼／編碼結果探測 ``base64``，再 fallback 到 OpenSSL。所有成功判定
均需要由 target 實際輸出的 sentinel 證明，不能只看 command echo 或 prompt。
"""
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

_TOOL_PROBE_PAYLOAD = base64.b64encode(b"sw-probe").decode("ascii")
_DECODER_B64_SENTINEL = "SW_XFER_DECODER_B64_42"
_DECODER_OPENSSL_SENTINEL = "SW_XFER_DECODER_OPENSSL_42"
_ENCODER_B64_SENTINEL = "SW_XFER_ENCODER_B64_42"
_ENCODER_OPENSSL_SENTINEL = "SW_XFER_ENCODER_OPENSSL_42"
_CHUNK_SENTINEL = "SW_XFER_CHUNK_42"
_MD5_SENTINEL = "SW_XFER_MD5_42"
_MV_SENTINEL = "SW_XFER_MV_42"
_CLEANUP_SENTINEL = "SW_XFER_CLEANUP_42"
_ENCODER_FAILURE_SENTINEL = "SW_XFER_ENCODER_FAILED_42"
_DECODERS = ("base64", "openssl")
_ENCODERS = ("base64", "openssl")

# 預設 chunk 大小（#157）：512B 的 base64 為 ceil(512/3)*4=684 字元，加上
# `printf '%s' '' | base64 -d >> /tmp/.sw_upload_<12hex>` 樣板約 57 字元固定
# 開銷 ≈ 741 字元——相對 issue 實證會截斷的 ~2789 字元（chunk=2048）約 3.8x
# 安全餘裕。注意：此值是依 #157 附的截斷長度反推的保守估計、非真機量測值；
# 呼叫端可經 CLI `--chunk-size`／RPC `chunk_size` 覆寫。
DEFAULT_CHUNK_SIZE = 512

# echo-ACK 節流預設（#161）：slice 64 字元、每段 echo 等待 `DEFAULT_ECHO_TIMEOUT_S`。
# 停滯＝板端連 echo 都跟不上或 console 死結，此時換行尚未送出＝命令未執行＝可安全重試。
DEFAULT_ECHO_SLICE_SIZE = 64
DEFAULT_ECHO_TIMEOUT_S = 5.0
"""單一 slice 等待 echo 回讀的預設逾時（秒）。

2.0 → 5.0（#161 實機調校）仍是 echo-ACK 的等待下限；實際生效值由
:meth:`SessionManager.file_push` 依 profile
``timeout_s`` 推導（``max(profile.timeout_s, DEFAULT_ECHO_TIMEOUT_S)``，比照 #157
``chunk_timeout_s`` 的推導精神），故 bcm 類已調大 ``timeout_s`` 的慢板自動放寬。
調高只影響**失敗路徑**的等待上限：echo 正常到達時 `_await_echo_progress` 立即返回，
成功路徑的吞吐不受影響。#166 的實機根因翻案確認：兩板缺 base64，且 prpl 約 505
字元的單行限制會截斷 chunk；並非把 timeout 調大即可修復，因此此處不再把 timeout
描述成傳輸停滯根因。"""

_ACK_MODES = ("auto", "echo", "none")


def push_file(
    bridge: UARTBridge,
    local_path: str,
    remote_path: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    timeout_s: float = 10.0,
    prompt_regex: str,
    source: str = "file_transfer",
    ack_mode: str = "auto",
    echo_slice_size: int = DEFAULT_ECHO_SLICE_SIZE,
    echo_timeout_s: float = DEFAULT_ECHO_TIMEOUT_S,
    max_console_line_chars: int | None = None,
) -> dict[str, Any]:
    """將 host 端檔案推送到 target（透過 UART base64 分段傳輸）。

    ``ack_mode``（#161）——chunk 命令行的送出方式：

    - ``auto``（預設）：bridge 具 ``send_command_echo_paced`` 即走 echo-ACK 節流，
      否則退回 legacy 整行送出（第三方／測試用 fake bridge 不破）。
    - ``echo``：強制 echo-ACK；bridge 不支援時回 ``ECHO_ACK_UNSUPPORTED``。
    - ``none``：維持 legacy 整行送出（急件換吞吐、放棄無流控保護）。

    echo-ACK 路徑上，chunk 命令行拆成 ``echo_slice_size`` 短段逐段送出，每段等板端
    echo 回讀確認再續送（echo 即天然應用層流控）；echo 停滯時以 ``cancel_input_line()``
    清半行後回 ``TRANSFER_ECHO_STALL``——換行未送出＝命令未執行＝可安全重試。
    """
    # ack_mode 白名單（Copilot review）：與 RPC 層（service.py `file.push`）同一道
    # 檢查，但這裡是**模組入口**——`push_file` 亦被 `SessionManager.file_push` 與
    # 非 RPC 呼叫端（測試／未來的內部流程）直接呼叫，只在 RPC 層驗證會讓未知模式從
    # 那些路徑漏進來並靜默降級成 legacy 整行送出（`ack_mode="ehco"` 這類手誤會悄悄
    # 失去無流控保護）。回應形狀與 RPC 層一致（`INVALID_ARGS`），另附 `ack_mode` 便於定位。
    if ack_mode not in _ACK_MODES:
        return {"ok": False, "error_code": "INVALID_ARGS", "ack_mode": ack_mode}
    budget_error = _validate_line_budget(max_console_line_chars)
    if budget_error is not None:
        return budget_error
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        return {"ok": False, "error_code": "INVALID_ARGS", "chunk_size": chunk_size}
    if not os.path.isfile(local_path):
        return {"ok": False, "error_code": "LOCAL_FILE_NOT_FOUND", "local_path": local_path}

    paced_send = getattr(bridge, "send_command_echo_paced", None)
    if ack_mode == "none":
        paced_send = None
    elif ack_mode == "echo" and not callable(paced_send):
        return {"ok": False, "error_code": "ECHO_ACK_UNSUPPORTED"}

    data = _read_local_file(local_path)
    md5_expected = hashlib.md5(data).hexdigest()
    tmp_name = f"/tmp/.sw_upload_{uuid.uuid4().hex[:12]}"
    cmd_id_prefix = f"ft-{uuid.uuid4().hex[:8]}"

    # 先檢查不依賴工具選擇的固定命令。任何一條固定命令放不進 profile
    # 預算時都必須在第一個 TX 前拒絕，避免留下半成品。
    fixed_commands = (
        _md5_command(tmp_name),
        _mv_command(tmp_name, remote_path),
        _cleanup_command(tmp_name),
    )
    if _line_budget_exceeded(fixed_commands, max_console_line_chars):
        return _line_limit_error(max_console_line_chars)

    probe_cmd = _decoder_probe_command()
    if _line_budget_exceeded((probe_cmd,), max_console_line_chars):
        return _line_limit_error(max_console_line_chars)

    # 以較長的 decoder 與 append 樣板做保守 preflight；實際探測完成後再以
    # 真正選中的 decoder 計算一次，確保每個實際送出的命令仍在預算內。
    if max_console_line_chars is not None:
        max_overhead = max(
            len(_chunk_command("", decoder, op, tmp_name).encode("utf-8"))
            for decoder in _DECODERS
            for op in (">", ">>")
        )
        raw_capacity = max(0, (max_console_line_chars - max_overhead) // 4 * 3)
        if _line_budget_exceeded(
            tuple(
                _chunk_command("", decoder, op, tmp_name)
                for decoder in _DECODERS
                for op in (">", ">>")
            ),
            max_console_line_chars,
        ):
            return _line_limit_error(max_console_line_chars)
        if data and raw_capacity < 1:
            return _line_limit_error(max_console_line_chars)
    else:
        raw_capacity = chunk_size

    decoder, probe_error = _probe_decoder(
        bridge, prompt_regex, timeout_s, source, f"{cmd_id_prefix}-probe"
    )
    if probe_error is not None:
        return {"ok": False, "error_code": probe_error}
    assert decoder is not None

    effective_chunk_size = min(chunk_size, raw_capacity)
    if data and effective_chunk_size < 1:
        return _line_limit_error(max_console_line_chars)
    if max_console_line_chars is not None:
        # raw_capacity 是以最長 overhead 計算，但此處再用實際 decoder／實際
        # encoded 長度確認；這也涵蓋 base64 4/3 ceiling 的邊界。
        probe_chunk = base64.b64encode(b"\0" * effective_chunk_size).decode("ascii")
        if _line_budget_exceeded(
            tuple(_chunk_command(probe_chunk, decoder, op, tmp_name) for op in (">", ">>")),
            max_console_line_chars,
        ):
            return _line_limit_error(max_console_line_chars)

    chunks = _split_chunks(data, effective_chunk_size)
    total = len(chunks)

    for idx, chunk in enumerate(chunks):
        b64 = base64.b64encode(chunk).decode("ascii")
        op = ">" if idx == 0 else ">>"
        cmd = _chunk_command(b64, decoder, op, tmp_name)
        if _line_budget_exceeded((cmd,), max_console_line_chars):
            # 理論上已由 preflight 擋下；保留 fail-closed 防線，且此處仍在
            # 送出該 chunk 前拒絕。
            _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
            return _line_limit_error(max_console_line_chars)
        pre = bridge.rx_snapshot_len()
        if callable(paced_send):
            paced = paced_send(
                cmd,
                source=source,
                cmd_id=f"{cmd_id_prefix}-{idx}",
                slice_size=echo_slice_size,
                echo_timeout_s=echo_timeout_s,
            )
            if not paced.get("ok"):
                # echo 停滯：換行未送出＝命令未執行；清半行復原後回報（可安全重試）。
                bridge.cancel_input_line(source=source)
                _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
                return {
                    "ok": False,
                    "error_code": "TRANSFER_ECHO_STALL",
                    "chunks_sent": idx,
                    "chunks_total": total,
                    "acked_chars": paced.get("acked_chars"),
                    "sent_chars": paced.get("sent_chars"),
                }
        else:
            bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-{idx}")
        if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
            _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
            return {
                "ok": False,
                "error_code": "TRANSFER_TIMEOUT",
                "chunks_sent": idx,
                "chunks_total": total,
            }
        if not _runtime_sentinel_seen(
            bridge.rx_text_from(pre), _CHUNK_SENTINEL, command=cmd
        ):
            _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
            return {
                "ok": False,
                "error_code": "TARGET_DECODER_FAILED",
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
    mv_cmd = _mv_command(tmp_name, remote_path)
    if _line_budget_exceeded((mv_cmd,), max_console_line_chars):
        _cleanup_remote(bridge, tmp_name, prompt_regex, timeout_s, source, cmd_id_prefix)
        return _line_limit_error(max_console_line_chars)
    pre = bridge.rx_snapshot_len()
    bridge.send_command(mv_cmd, source=source, cmd_id=f"{cmd_id_prefix}-mv")
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return {"ok": False, "error_code": "MOVE_TIMEOUT"}
    if not _runtime_sentinel_seen(bridge.rx_text_from(pre), _MV_SENTINEL, command=mv_cmd):
        return {"ok": False, "error_code": "MOVE_FAILED"}

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
    max_console_line_chars: int | None = None,
) -> dict[str, Any]:
    """從 target 拉取檔案到 host（透過 UART base64 傳輸）。"""
    if local_path is None:
        local_path = os.path.basename(remote_path)

    budget_error = _validate_line_budget(max_console_line_chars)
    if budget_error is not None:
        return budget_error

    cmd_id_prefix = f"ft-{uuid.uuid4().hex[:8]}"

    md5_cmd = _md5_command(remote_path)
    probe_cmd = _encoder_probe_command()
    if _line_budget_exceeded((probe_cmd, md5_cmd), max_console_line_chars):
        return _line_limit_error(max_console_line_chars)

    # 預先檢查兩種 encoder 的完整 marker／path 命令；command echo 不能含有
    # 完整 marker，實際輸出才由 shell 的 quote-split expression 產生。
    candidate_pull_commands = tuple(
        _pull_command(remote_path, encoder) for encoder in _ENCODERS
    )
    if _line_budget_exceeded(candidate_pull_commands, max_console_line_chars):
        return _line_limit_error(max_console_line_chars)

    encoder, probe_error = _probe_encoder(
        bridge, prompt_regex, timeout_s, source, f"{cmd_id_prefix}-probe"
    )
    if probe_error is not None:
        return {"ok": False, "error_code": probe_error}
    assert encoder is not None

    # 用 sentinel 包裹 base64 輸出，便於可靠擷取
    cmd = _pull_command(remote_path, encoder)
    if _line_budget_exceeded((cmd,), max_console_line_chars):
        return _line_limit_error(max_console_line_chars)
    pre = bridge.rx_snapshot_len()
    bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-b64")
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return {"ok": False, "error_code": "TRANSFER_TIMEOUT"}

    raw_text = bridge.rx_text_from(pre)
    b64_content = _extract_between_sentinels(raw_text, command=cmd)
    if b64_content is None:
        if _runtime_sentinel_seen(
            raw_text, _ENCODER_FAILURE_SENTINEL, command=cmd
        ):
            return {"ok": False, "error_code": "TARGET_ENCODER_FAILED"}
        return {"ok": False, "error_code": "PULL_PARSE_FAILED"}

    try:
        data = base64.b64decode(b64_content)
    except Exception:
        return {"ok": False, "error_code": "BASE64_DECODE_FAILED"}

    # 驗證 checksum
    md5_local = hashlib.md5(data).hexdigest()
    md5_remote = _remote_md5(bridge, remote_path, prompt_regex, timeout_s, source, cmd_id_prefix)
    if md5_remote is None:
        return {"ok": False, "error_code": "CHECKSUM_VERIFY_FAILED"}
    if md5_remote != md5_local:
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


def _validate_line_budget(budget: int | None) -> dict[str, Any] | None:
    """驗證 direct API 收到的 line budget，避免 bool／字串悄悄轉型。"""
    if budget is None:
        return None
    if isinstance(budget, bool) or not isinstance(budget, int):
        return {"ok": False, "error_code": "INVALID_ARGS", "max_console_line_chars": budget}
    if budget <= 0:
        return _line_limit_error(budget)
    return None


def _line_limit_error(budget: int | None) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "CONSOLE_LINE_LIMIT_TOO_SMALL",
        "max_console_line_chars": budget,
    }


def _line_budget_exceeded(commands: tuple[str, ...], budget: int | None) -> bool:
    if budget is None:
        return False
    return any(len(command.encode("utf-8")) > budget for command in commands)


def _runtime_sentinel(prefix: str) -> str:
    """產生只在 target 執行後才出現完整值的 sentinel。"""
    # 以 LF 結束控制 marker，讓既有的 ``^prompt`` regex 能在 marker 後的
    # prompt 行首命中；沒有 LF 時 prompt 會黏在 marker 尾端而被誤判逾時。
    return f"printf '%s\\n' '{prefix}'$((6*7))"


def _strip_command_echo(text: str, command: str | None = None) -> str:
    """移除目前命令的本地 echo；fake bridge 未模擬 echo 時保持原文。"""
    if command and text.startswith(command):
        text = text[len(command) :]
        if text.startswith("\r\n"):
            text = text[2:]
        elif text.startswith("\n"):
            text = text[1:]
    return text


def _runtime_sentinel_seen(text: str, sentinel: str, *, command: str | None = None) -> bool:
    """只在 command echo 之後找到完整 sentinel 才算 target 成功。"""
    output = _strip_command_echo(text, command)
    return re.search(rf"(?:^|\r?\n){re.escape(sentinel)}(?:\r?\n|$)", output) is not None


def _probe_shell_error(text: str, command: str) -> bool:
    """辨識 shell 語法／執行環境錯誤，避免與「候選工具都不存在」混淆。"""
    output = _strip_command_echo(text, command)
    return re.search(
        r"(?i)(?:syntax error|bad substitution|unterminated|permission denied)",
        output,
    ) is not None


def _marker_expression(marker: str) -> str:
    """以 quote split 生成 marker，避免完整 marker 出現在 command echo。"""
    if not (marker.startswith("===SW_XFER_") and marker.endswith("===")):
        raise ValueError(f"unsupported transfer marker: {marker!r}")
    middle = marker[len("===SW_XFER_") : -3]
    return f"'===SW_XFER_'{middle}'==='"


def _decoder_probe_command() -> str:
    return (
        f"if test \"$(printf '%s' '{_TOOL_PROBE_PAYLOAD}' | base64 -d 2>/dev/null)\" = 'sw-probe'; "
        f"then {_runtime_sentinel('SW_XFER_DECODER_B64_')}; "
        f"elif test \"$(printf '%s' '{_TOOL_PROBE_PAYLOAD}' | openssl enc -base64 -d -A 2>/dev/null)\" = 'sw-probe'; "
        f"then {_runtime_sentinel('SW_XFER_DECODER_OPENSSL_')}; fi"
    )


def _encoder_probe_command() -> str:
    return (
        "if test \"$(printf '%s' 'sw-probe' | base64 2>/dev/null)\" = 'c3ctcHJvYmU='; "
        f"then {_runtime_sentinel('SW_XFER_ENCODER_B64_')}; "
        "elif test \"$(printf '%s' 'sw-probe' | openssl enc -base64 2>/dev/null)\" = 'c3ctcHJvYmU='; "
        f"then {_runtime_sentinel('SW_XFER_ENCODER_OPENSSL_')}; fi"
    )


def _decoder_command_name(decoder: str) -> str:
    if decoder == "base64":
        return "base64 -d"
    if decoder == "openssl":
        return "openssl enc -base64 -d -A"
    raise ValueError(f"unknown decoder: {decoder!r}")


def _encoder_command_name(encoder: str) -> str:
    if encoder == "base64":
        return "base64"
    if encoder == "openssl":
        # 保留 OpenSSL 的換行，避免大型 pull 輸出變成單一超長 console line。
        return "openssl enc -base64"
    raise ValueError(f"unknown encoder: {encoder!r}")


def _chunk_command(encoded: str, decoder: str, op: str, path: str) -> str:
    return (
        f"printf '%s' {shlex.quote(encoded)} | {_decoder_command_name(decoder)} "
        f"{op} {shlex.quote(path)} && {_runtime_sentinel('SW_XFER_CHUNK_')}"
    )


def _md5_command(path: str) -> str:
    return f"md5sum {shlex.quote(path)} && {_runtime_sentinel('SW_XFER_MD5_')}"


def _mv_command(source_path: str, remote_path: str) -> str:
    return (
        f"mv {shlex.quote(source_path)} {shlex.quote(remote_path)} && "
        f"{_runtime_sentinel('SW_XFER_MV_')}"
    )


def _cleanup_command(path: str) -> str:
    return f"rm -f {shlex.quote(path)} && {_runtime_sentinel('SW_XFER_CLEANUP_')}"


def _pull_command(remote_path: str, encoder: str) -> str:
    return (
        f"printf '%s\\n' {_marker_expression(_SENTINEL_BEGIN)} && "
        f"{_encoder_command_name(encoder)} < {shlex.quote(remote_path)} && "
        f"printf '%s\\n' {_marker_expression(_SENTINEL_END)} || "
        f"{_runtime_sentinel('SW_XFER_ENCODER_FAILED_')}"
    )


def _probe_decoder(
    bridge: UARTBridge,
    prompt_regex: str,
    timeout_s: float,
    source: str,
    cmd_id: str,
) -> tuple[str | None, str | None]:
    pre = bridge.rx_snapshot_len()
    command = _decoder_probe_command()
    bridge.send_command(command, source=source, cmd_id=cmd_id)
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return None, "TRANSFER_TIMEOUT"
    raw = bridge.rx_text_from(pre)
    if _runtime_sentinel_seen(raw, _DECODER_B64_SENTINEL, command=command):
        return "base64", None
    if _runtime_sentinel_seen(raw, _DECODER_OPENSSL_SENTINEL, command=command):
        return "openssl", None
    if _probe_shell_error(raw, command):
        return None, "TRANSFER_PROBE_FAILED"
    return None, "TARGET_DECODER_MISSING"


def _probe_encoder(
    bridge: UARTBridge,
    prompt_regex: str,
    timeout_s: float,
    source: str,
    cmd_id: str,
) -> tuple[str | None, str | None]:
    pre = bridge.rx_snapshot_len()
    command = _encoder_probe_command()
    bridge.send_command(command, source=source, cmd_id=cmd_id)
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return None, "TRANSFER_TIMEOUT"
    raw = bridge.rx_text_from(pre)
    if _runtime_sentinel_seen(raw, _ENCODER_B64_SENTINEL, command=command):
        return "base64", None
    if _runtime_sentinel_seen(raw, _ENCODER_OPENSSL_SENTINEL, command=command):
        return "openssl", None
    if _probe_shell_error(raw, command):
        return None, "TRANSFER_PROBE_FAILED"
    return None, "TARGET_ENCODER_MISSING"


def _remote_md5(
    bridge: UARTBridge,
    path: str,
    prompt_regex: str,
    timeout_s: float,
    source: str,
    cmd_id_prefix: str,
) -> str | None:
    """在 target 上執行 md5sum 並擷取 hash 值。"""
    cmd = _md5_command(path)
    pre = bridge.rx_snapshot_len()
    bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-md5")
    if not bridge.wait_for_regex_from(prompt_regex, pre, timeout_s):
        return None
    raw = bridge.rx_text_from(pre)
    output = _strip_command_echo(raw, cmd)
    if not _runtime_sentinel_seen(raw, _MD5_SENTINEL, command=cmd):
        return None
    # md5sum 的 checksum 是輸出行的第一欄；不能取任意最後一段 32-hex，
    # 否則 remote path 恰好含 32 個十六進位字元時會把檔名誤當 checksum。
    before_sentinel = output[: output.find(_MD5_SENTINEL)]
    for line in before_sentinel.splitlines():
        match = re.match(r"\s*([0-9a-fA-F]{32})(?:\s|$)", line)
        if match:
            return match.group(1).lower()
    return None


def _cleanup_remote(
    bridge: UARTBridge,
    path: str,
    prompt_regex: str,
    timeout_s: float,
    source: str,
    cmd_id_prefix: str,
) -> None:
    """盡力清除 target 上的暫存檔（忽略失敗）。"""
    cmd = _cleanup_command(path)
    pre = bridge.rx_snapshot_len()
    bridge.send_command(cmd, source=source, cmd_id=f"{cmd_id_prefix}-cleanup")
    bridge.wait_for_regex_from(prompt_regex, pre, timeout_s)


def _extract_between_sentinels(text: str, *, command: str | None = None) -> str | None:
    """從 RX 文字中擷取 sentinel 標記之間的 base64 內容。"""
    text = _strip_command_echo(text, command)
    # command echo 可能包含舊版 literal marker；實際輸出的 marker 位於後段，
    # 因此取最後一組，而不是讓 echo 內容污染 base64 擷取。
    begin_idx = text.rfind(_SENTINEL_BEGIN)
    end_idx = text.rfind(_SENTINEL_END)
    if begin_idx < 0 or end_idx < 0 or end_idx <= begin_idx:
        return None
    content = text[begin_idx + len(_SENTINEL_BEGIN) : end_idx]
    # 去除 ANSI 逸出序列（顏色、游標控制、括弧貼上模式等），再去除空白
    return re.sub(r"\s+", "", strip_ansi(content))
