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

2.0 → 5.0（#161 實機調校）：真機兩案都在**第 8 個 slice（448/512 字元）確定性卡住**
——固定的失敗位置說明不是隨機掉字，而是板端在累積輸入到某長度後 echo 延遲超過 2s
（printk 插隊、console 節流），2.0s 對慢板偏緊、把「還在追」誤判成「停滯」。
5.0 為新的**下限**；實際生效值由 :meth:`SessionManager.file_push` 依 profile
``timeout_s`` 推導（``max(profile.timeout_s, DEFAULT_ECHO_TIMEOUT_S)``，比照 #157
``chunk_timeout_s`` 的推導精神），故 bcm 類已調大 ``timeout_s`` 的慢板自動放寬。
調高只影響**失敗路徑**的等待上限：echo 正常到達時 `_await_echo_progress` 立即返回，
成功路徑的吞吐不受影響。"""

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

    chunks = _split_chunks(data, chunk_size)
    total = len(chunks)

    for idx, chunk in enumerate(chunks):
        b64 = base64.b64encode(chunk).decode("ascii")
        op = ">" if idx == 0 else ">>"
        cmd = f"printf '%s' '{b64}' | base64 -d {op} {shlex.quote(tmp_name)}"
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
