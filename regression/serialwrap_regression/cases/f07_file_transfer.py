"""F7 檔案傳輸完整性（#21 #32）：binary round-trip md5 一致、不靜默截斷。

``serialwrap file push``／``file pull`` 走 base64 分段＋md5 校驗（見
``sw_core/file_transfer.py``，本檔僅唯讀 grep 鍵名，未 import）；板端缺 base64
時應由 OpenSSL fallback 完成，只有兩路皆不可用才誠實 SKIP。已知缺口
（#123 defer，MINOR-5）：這兩個 RPC 方法**不在** CLI 的長操作白名單，未顯式
帶 ``--timeout`` 會沿用一般方法的 5.0s 預設，對多 chunk 的實際傳輸而言形同
必逾時──故本檔一律顯式帶較寬的 RPC ``--timeout``（global flag，需置於
``file`` 子命令**之前**）＋對應的 ``SwCli.run(timeout=...)``（subprocess 層
逾時，需 ≥ RPC timeout 加緩衝，否則會在 RPC 真正逾時前就被 Python 端
``subprocess.TimeoutExpired`` 打斷）。

timeout 估算基準（#161 echo-ACK 後）：``DEFAULT_CHUNK_SIZE=512`` → 64KB≈129 chunks、
1MB=2048 chunks；push 預設走 echo-paced（單行 ~741 字元拆 ~12 個 64-char slice、
逐段等 echo 回讀），每 chunk ≈ 12 slice 往返（~20–35ms/slice）＋prompt 等待，
估 0.3–0.5s/chunk → 64KB≈39–65s（180s 上界仍足）、1MB≈614–1024s（≈10–17 分，
取 1500s 上界；plan 決策 4——維持 echo-paced 預設路徑，不用 ``--ack-mode none``
換吞吐，回歸防線要驗的是新機制）。實際值待真機驗證校準。

echo 停滯（``TRANSFER_ECHO_STALL``，#161）歸獨立 reason_code
``transfer_echo_stall``（不併進 ``_TIMEOUT_CODES``），使「新機制自身失效」與一般
逾時／連線層失敗可辨識。

已知殘留缺口（#157/#161 範圍外）：``sw_core/uart_io.py`` 的 RX 視窗上限 131072 字元
（#158 改為絕對偏移記帳，但視窗仍有界、被修剪頭段永久丟失），``pull_file`` 不分段
一次讀全部——1MB 檔案 base64 輸出 ~1.4MB 遠超上限，``_SENTINEL_BEGIN`` 必被踢出
視窗 → ``PULL_PARSE_FAILED``。故 ``f7-larger-file-not-truncated`` push 端可成功，
pull 端仍預期 SKIP（``transfer_environment_failure``）待 follow-up（另開 issue 追蹤）；
``f7-binary-roundtrip-md5``（64KB，base64 ~88.6KB 在上限內）於 #161/#166 後預期 COM0 轉綠。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F7", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


# push_file／pull_file（sw_core/file_transfer.py）在 target 缺傳輸工具時會
# 於 probe 階段明確回報；若 probe 後工具失效，也不能只因 prompt 回來就算成功。
_TOOL_MISSING_CODES = frozenset({
    "TARGET_DECODER_MISSING", "TARGET_ENCODER_MISSING",
    "TARGET_DECODER_FAILED", "TARGET_ENCODER_FAILED",
    "CHECKSUM_VERIFY_FAILED", "BASE64_DECODE_FAILED",
})

# 逾時／連線層失敗徵兆：RPC 客戶端層的 TIMEOUT／SOCKET_ERROR／EMPTY_RESPONSE
# （sw_core/client.py），以及 file_transfer 內部逐段等待逾時的 TRANSFER_TIMEOUT／
# MOVE_TIMEOUT。這些代表傳輸「未完成」，非「完成但內容有誤」，故一律歸環境性。
_TIMEOUT_CODES = frozenset({
    "TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE", "TRANSFER_TIMEOUT", "MOVE_TIMEOUT",
})


def _probe_target_tools(ctx: Any, com: str) -> bool | None:
    """實測板端是否具備可用的雙向工具鏈：True＝有任一完整鏈、False＝缺、None＝探測失敗。

    區分 cg review 抓到的雙重含義：``CHECKSUM_VERIFY_FAILED``／``BASE64_DECODE_FAILED``
    既可能是板端缺工具（環境、SKIP），也可能是工具都在但傳輸真的壞掉（#32 類回歸、FAIL）。
    先探測工具存在性，後續判定才能分流。
    """
    # 用 which 而非 command -v（bcm 板 shell 無 `command` builtin，`command -v` 回
    # 'sh: command: not found' 被誤判成工具缺失）。busybox which 皆備。
    #
    # sentinel 必須是**命令本文裡不會出現完整值**的字串（#166 實證）：舊版用
    # `... && echo TOOLS_OK` 再判 `"TOOLS_OK" in stdout`，但命令回顯本身就含
    # 該字面，只要板端有回顯就恆為真。此處用實際解碼／編碼結果，再以算式
    # 產生 `TOOLS_B64_42`／`TOOLS_OPENSSL_42`，避免 echo 假陽性。
    probe = ctx.sw.submit_and_wait(
        com, _target_tools_probe_command(),
    )
    ctx.note(f"{com}-tools-probe.json", str(probe))
    if probe.get("status") != "done":
        return None
    stdout = probe.get("stdout") or ""
    return "TOOLS_B64_42" in stdout or "TOOLS_OPENSSL_42" in stdout


def _target_tools_probe_command() -> str:
    """F7 實機 probe：雙向 codec 與 md5sum 均實際成功才輸出 sentinel。"""
    return (
        "if which md5sum >/dev/null 2>&1 && "
        "test \"$(printf '%s' 'c3ctcHJvYmU=' | base64 -d 2>/dev/null)\" = 'sw-probe' && "
        "test \"$(printf '%s' 'sw-probe' | base64 2>/dev/null)\" = 'c3ctcHJvYmU='; "
        "then printf '%s\\n' 'TOOLS_B64_'$((6*7)); "
        "elif which md5sum >/dev/null 2>&1 && "
        "test \"$(printf '%s' 'c3ctcHJvYmU=' | openssl enc -base64 -d -A 2>/dev/null)\" = 'sw-probe' && "
        "test \"$(printf '%s' 'sw-probe' | openssl enc -base64 2>/dev/null)\" = 'c3ctcHJvYmU='; "
        "then printf '%s\\n' 'TOOLS_OPENSSL_'$((6*7)); fi"
    )


def _transfer_failure_verdict(resp: dict[str, Any], *, verb: str,
                              tools_present: bool | None) -> CaseResult:
    """push/pull 非 ok 的統一分流。

    ``CHECKSUM_MISMATCH`` 是已完成傳輸後的明確完整性失敗，無論工具探測結果都必須
    讓 F7 紅燈；工具缺失／工具執行錯誤、逾時與其他未完成傳輸則保留原有環境分流。
    """
    code = str(resp.get("error_code") or "unknown")
    if code == "CHECKSUM_MISMATCH":
        return CaseResult(
            "FAIL",
            reason=f"{verb} 完成但 checksum 不一致（error_code={code}）",
            category="test", reason_code="binary_roundtrip_mismatch",
        )
    if code in _TOOL_MISSING_CODES:
        if tools_present is True:
            return CaseResult(
                "FAIL",
                reason=f"{verb} 失敗（error_code={code}）且板端可用傳輸工具鏈皆在"
                       "——非工具缺失，屬傳輸損壞（#32 類回歸）",
                category="test", reason_code="binary_roundtrip_mismatch",
            )
        return CaseResult(
            "SKIP", reason=f"板端疑缺 base64／OpenSSL／md5sum（error_code={code}，工具探測={tools_present}）",
            category="environment", reason_code="target_tool_missing",
        )
    return _environment_skip(resp, verb=verb)


def _environment_skip(resp: dict[str, Any], *, verb: str) -> CaseResult:
    """push/pull 未完成傳輸（逾時或其他明確失敗），非「完成但內容有誤」，判環境性 SKIP。"""
    code = str(resp.get("error_code") or "unknown")
    if code == "TRANSFER_ECHO_STALL":
        # #161：echo-ACK 節流自身停滯（板端 echo 跟不上／console 死結）。獨立
        # reason_code 使新機制失效可辨識，不與一般逾時混同（plan 決策 5）。
        reason_code = "transfer_echo_stall"
    elif code in _TIMEOUT_CODES:
        reason_code = "transfer_timeout"
    else:
        reason_code = "transfer_environment_failure"
    return CaseResult(
        "SKIP",
        reason=f"{verb} 未完成（error_code={code}），視為環境因素而非程式回歸",
        category="environment",
        reason_code=reason_code,
    )


def _push(ctx: Any, com: str, local_path: Path, remote_path: str,
          *, rpc_timeout_s: float, proc_timeout_s: float) -> dict[str, Any]:
    return ctx.sw.run(
        "--timeout", str(rpc_timeout_s), "file", "push",
        "--selector", com, "--local", str(local_path), "--remote", remote_path,
        timeout=proc_timeout_s,
    )


def _pull(ctx: Any, com: str, remote_path: str, local_path: Path,
          *, rpc_timeout_s: float, proc_timeout_s: float) -> dict[str, Any]:
    return ctx.sw.run(
        "--timeout", str(rpc_timeout_s), "file", "pull",
        "--selector", com, "--remote", remote_path, "--local", str(local_path),
        timeout=proc_timeout_s,
    )


def _cleanup_remote(ctx: Any, com: str, remote_path: str) -> None:
    """收尾清板上暫存檔（case 執行期允許的寫操作）；盡力而為、失敗不影響已判定的 verdict。"""
    try:
        ctx.sw.submit_and_wait(com, f"rm -f {remote_path}")
    except Exception:
        pass


def _cleanup_local(*paths: Path) -> None:
    """收尾清本地暫存檔；盡力而為、失敗不影響已判定的 verdict。"""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def _binary_roundtrip_on(ctx, com):
    remote_path = "/tmp/swreg_rt.bin"
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    # gzip 壓縮亂數 bytes：header FLG 欄位恆為 0x00，實質保證含 null byte（#32 的
    # BASE64_DECODE_FAILED 類回歸正是在 payload 含 null byte 時才會現形）。
    payload = gzip.compress(os.urandom(64 * 1024))
    src_path = ctx.case_dir / "f7-rt-src.bin.gz"
    dst_path = ctx.case_dir / "f7-rt-dst.bin.gz"
    src_path.write_bytes(payload)
    try:
        tools_present = _probe_target_tools(ctx, com)
        if tools_present is False:
            return CaseResult("SKIP", reason="板端缺 base64／OpenSSL／md5sum（探測確認）",
                              category="environment", reason_code="target_tool_missing")

        # chunk 512 下 64KB≈129 chunks；echo-paced 估 39–65s（#161，見 module docstring），
        # 180s 上界仍足、不調整。
        push = _push(ctx, com, src_path, remote_path, rpc_timeout_s=180, proc_timeout_s=200)
        ctx.note(f"{com}-push.json", str(push))
        if not push.get("ok"):
            return _transfer_failure_verdict(push, verb="push", tools_present=tools_present)

        pull = _pull(ctx, com, remote_path, dst_path, rpc_timeout_s=180, proc_timeout_s=200)
        ctx.note(f"{com}-pull.json", str(pull))
        if not pull.get("ok"):
            return _transfer_failure_verdict(pull, verb="pull", tools_present=tools_present)

        md5_src = hashlib.md5(payload).hexdigest()
        md5_dst = hashlib.md5(dst_path.read_bytes()).hexdigest()
        if md5_src != md5_dst:
            return CaseResult(
                "FAIL", reason=f"round-trip md5 不一致：src={md5_src} dst={md5_dst}",
                category="test", reason_code="binary_roundtrip_mismatch")
        return CaseResult("PASS")
    finally:
        _cleanup_remote(ctx, com, remote_path)
        _cleanup_local(src_path, dst_path)


def _larger_file_on(ctx, com):
    remote_path = "/tmp/swreg_big.bin"
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    payload = os.urandom(1024 * 1024)
    src_path = ctx.case_dir / "f7-big-src.bin"
    dst_path = ctx.case_dir / "f7-big-dst.bin"
    src_path.write_bytes(payload)
    try:
        tools_present = _probe_target_tools(ctx, com)
        if tools_present is False:
            return CaseResult("SKIP", reason="板端缺 base64／OpenSSL／md5sum（探測確認）",
                              category="environment", reason_code="target_tool_missing")

        # chunk 512 下 1MB=2048 chunks，echo-paced 估 614–1024s（≈10–17 分，#161）；
        # 取 1500s 上界（plan 決策 4：維持 echo-paced 預設路徑、不用 --ack-mode none
        # 換吞吐——回歸防線要驗的是新機制）。
        # 註：pull 端在 RX 視窗 128KiB 上限修復前仍預期 PULL_PARSE_FAILED → SKIP。
        push = _push(ctx, com, src_path, remote_path, rpc_timeout_s=1500, proc_timeout_s=1520)
        ctx.note(f"{com}-push.json", str(push))
        if not push.get("ok"):
            return _transfer_failure_verdict(push, verb="push", tools_present=tools_present)

        pull = _pull(ctx, com, remote_path, dst_path, rpc_timeout_s=1500, proc_timeout_s=1520)
        ctx.note(f"{com}-pull.json", str(pull))
        if not pull.get("ok"):
            return _transfer_failure_verdict(pull, verb="pull", tools_present=tools_present)

        # push/pull 皆回報成功，才是這個 case 真正要抓的回歸：內容是否被靜默截斷。
        dst_bytes = dst_path.read_bytes()
        if len(dst_bytes) != len(payload):
            return CaseResult(
                "FAIL",
                reason=f"檔案大小不符（疑似截斷）：src={len(payload)} dst={len(dst_bytes)}",
                category="test", reason_code="transfer_truncated")
        md5_src = hashlib.md5(payload).hexdigest()
        md5_dst = hashlib.md5(dst_bytes).hexdigest()
        if md5_src != md5_dst:
            return CaseResult(
                "FAIL", reason=f"1MB round-trip md5 不一致：src={md5_src} dst={md5_dst}",
                category="test", reason_code="transfer_truncated")
        return CaseResult("PASS")
    finally:
        _cleanup_remote(ctx, com, remote_path)
        _cleanup_local(src_path, dst_path)


_ENV_SKIP_CODES = frozenset({
    "transfer_timeout", "transfer_environment_failure", "target_tool_missing",
    "transfer_echo_stall"})


def _run_on_boards(ctx, runner, *, case_tag: str):
    """逐板嘗試（首輪批次驗收調校，#157）：COM0（prpl 無顯式 timeout_s、且真機實測
    存在無流控節流掉字的傳輸層限制）環境性失敗時改試 COM1（bcm 有 timeout_s 配置、
    可實證 #157 chunk/timeout 機制）。第一個決定性結果（PASS 或 test-FAIL）即回傳；
    全數環境性 SKIP 才 SKIP（彙整逐板 reason）。"""
    outcomes = []
    for board in ctx.cfg.get("boards") or []:
        com = str(board["com"])
        r = runner(ctx, com)
        outcomes.append(f"{com}: {r.verdict}/{r.reason_code or '-'}")
        if not (r.verdict == "SKIP" and (r.reason_code or "") in _ENV_SKIP_CODES):
            r.reason = f"[{com}] {r.reason}" if r.reason else f"[{com}]"
            return r
    ctx.note(f"{case_tag}-board-outcomes.txt", "\n".join(outcomes))
    return CaseResult(
        "SKIP",
        reason="全部板卡皆環境性失敗：" + "；".join(outcomes),
        category="environment", reason_code="transfer_env_all_boards",
    )


@_case("f7-binary-roundtrip-md5", "binary round-trip md5 一致（含 null byte）",
       issues=("#32", "#21", "#161", "#166"),
       hints=("逐板嘗試：prpl 板的無流控節流掉字由 #161 echo-ACK 修復（push 預設走"
              "echo-paced，預期 COM0 轉綠）；停滯時 reason_code=transfer_echo_stall"
              "可辨識新機制失效。",))
def f7_binary_roundtrip_md5(ctx):
    return _run_on_boards(ctx, _binary_roundtrip_on, case_tag="rt")


@_case("f7-larger-file-not-truncated", "1MB 檔案 push→pull 不靜默截斷",
       issues=("#21", "#161", "#166"),
       hints=("逐板嘗試（同 f7-binary-roundtrip-md5）；echo-paced 下 1MB push 約"
              "10–17 分，timeout 上界 1500s；pull 端受 RX 視窗 128KiB 上限仍預期"
              "SKIP（待 follow-up issue）。",))
def f7_larger_file_not_truncated(ctx):
    return _run_on_boards(ctx, _larger_file_on, case_tag="big")

def _throwaway_line_command(ta: Any, com: str, command: str, *, timeout_s: float = 15.0) -> dict[str, Any]:
    """在 throwaway session 執行一條 line command，回傳終態 command 欄位。"""
    import time
    submitted = ta.run(
        "cmd", "submit", "--selector", com, "--cmd", command,
        "--cmd-timeout", str(timeout_s), timeout=20.0,
    )
    cmd_id = submitted.get("cmd_id")
    if not submitted.get("ok") or not cmd_id:
        return {"status": "submit_failed", "response": submitted}
    deadline = time.monotonic() + timeout_s + 5.0
    while time.monotonic() < deadline:
        result = ta.run("cmd", "status", "--cmd-id", str(cmd_id), timeout=10.0)
        command_result = result.get("command") or {}
        if command_result.get("status") in {"done", "error", "timeout", "canceled"}:
            return command_result
        time.sleep(0.1)
    return {"status": "wait_timeout", "cmd_id": str(cmd_id)}


def _chunk_tx_count(ta: Any) -> int:
    """計算 throwaway WAL 中已完成的 chunk 命令數。"""
    import base64

    count = 0
    for line in ta.wal_text().splitlines():
        try:
            record = json.loads(line)
            payload = base64.b64decode(record.get("payload_b64") or "", validate=True)
        except (ValueError, TypeError):
            continue
        if record.get("dir") == "TX" and b"SW_XFER_CHUNK" in payload:
            count += 1
    return count


_TRANSFER_CMD_ID = re.compile(r"^ft-[0-9a-f]{8}-\d+$")
_UPLOAD_TEMP_PATH = re.compile(r"^/tmp/\.sw_upload_[0-9a-f]{12}$")


def _decode_wal_records(wal_text: str) -> list[dict[str, Any]]:
    """解碼 WAL 行，依序號排序，略過未完成或損毀的尾段。"""
    import base64

    records: list[dict[str, Any]] = []
    for line in wal_text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
            row["_payload"] = base64.b64decode(row.get("payload_b64") or "", validate=True)
            row["_seq"] = int(row.get("seq", 0))
        except (ValueError, TypeError):
            continue
        records.append(row)
    return sorted(records, key=lambda row: row["_seq"])


def _rx_payload(wal_text: str) -> bytes:
    return b"".join(
        row["_payload"] for row in _decode_wal_records(wal_text)
        if row.get("dir") == "RX"
    )


def _uart_partial_chunk_echo(bridge_rx_bytes: bytes, marker: str) -> bytes | None:
    """由 console bridge 的原始 RX bytes 取出 echo 關閉前最後一條未結束 chunk 行。"""
    marker_at = bridge_rx_bytes.find(marker.encode("ascii"))
    if marker_at < 0:
        return None
    prefix = b"printf '%s' "
    start = bridge_rx_bytes.rfind(prefix, 0, marker_at)
    if start < 0:
        return None
    partial = bridge_rx_bytes[start:marker_at]
    # watcher 在 marker 前固定輸出一個換行；只剝除此一個 delimiter，半行內仍不得含行尾。
    if partial.endswith(b"\r\n"):
        partial = partial[:-2]
    elif partial.endswith(b"\n"):
        partial = partial[:-1]
    if b"\n" in partial or b"\r" in partial:
        return None
    # 正式 chunk 樣板經 shlex.quote(encoded) 產生未加引號的 base64；至少需觀察一個
    # base64 字元，避免只有 shell 前綴或被截在 payload 起點前就宣稱半行已成立。
    if re.match(rb"^printf '%s' [A-Za-z0-9+/=]+", partial) is None:
        return None
    return partial


def _midline_push_observed(
    wal_text: str, *, bridge_rx_bytes: bytes, echo_off_marker: str, foreground_busy: bool,
) -> bool:
    """以 WAL 確認兩個完整 chunk，再由 console bridge RX bytes 確認目前半行。"""
    if not foreground_busy or _uart_partial_chunk_echo(bridge_rx_bytes, echo_off_marker) is None:
        return False

    chunks: dict[str, bytearray] = {}
    order: list[str] = []
    for row in _decode_wal_records(wal_text):
        if row.get("dir") != "TX":
            continue
        cmd_id = str(row.get("cmd_id") or "")
        if not _TRANSFER_CMD_ID.fullmatch(cmd_id):
            continue
        if cmd_id not in chunks:
            chunks[cmd_id] = bytearray()
            order.append(cmd_id)
        chunks[cmd_id].extend(row["_payload"])

    if not order:
        return False
    transfer_prefix = order[-1].rsplit("-", 1)[0]
    completed = {
        cmd_id for cmd_id in order
        if cmd_id.rsplit("-", 1)[0] == transfer_prefix
        and b"SW_XFER_CHUNK_" in chunks[cmd_id]
        and b"\n" in chunks[cmd_id]
    }
    return len(completed) >= 2


def _midline_shutdown_verdict(clear_count: int) -> CaseResult | None:
    """停機半行的硬判準；收尾位元組缺失時必須讓 F7 回歸紅燈。"""
    if clear_count != 1:
        return CaseResult("FAIL", reason=f"停機半行清除位元組次數為 {clear_count}，期望恰一次",
                          category="test", reason_code="midline_clear_count_mismatch")
    return None


class _UartRxObserver:
    """讀取 daemon 提供的 console PTY RX bytes，不直接開啟任何 serial device。"""

    def __init__(self, ta: Any, com: str, path: Path) -> None:
        self._ta = ta
        self._com = com
        self._path = path
        self._client_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._file = None
        attached = ta.run("session", "console-attach", "--selector", com,
                          "--label", "f7-i282-byte-observer")
        endpoint = attached.get("vtty")
        client_id = attached.get("client_id")
        if attached.get("ok") is not True or not isinstance(endpoint, str) or not endpoint.startswith("/dev/pts/"):
            raise RuntimeError(f"無法取得 throwaway daemon console PTY：{attached!r}")
        self._client_id = str(client_id) if client_id else None
        try:
            self._file = path.open("wb", buffering=0)
            self._proc = subprocess.Popen(
                ["cat", endpoint], stdin=subprocess.DEVNULL,
                stdout=self._file, stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except BaseException:
            self.close()
            raise

    def bytes(self) -> bytes:
        return self._path.read_bytes()

    def close(self) -> None:
        if self._client_id:
            try:
                self._ta.run("session", "console-detach", "--selector", self._com,
                             "--client-id", self._client_id, timeout=10.0)
            except Exception:
                pass
            self._client_id = None
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2.0)
        self._proc = None
        if self._file is not None:
            self._file.close()
            self._file = None


def _upload_size_markers(wal_text: str, *, marker: str) -> dict[str, int]:
    """解析 DUT watcher 在清行前回報的暫存檔位元組數。"""
    result: dict[str, int] = {}
    prefix = f"{marker} ".encode("ascii")
    for line in _rx_payload(wal_text).splitlines():
        if not line.startswith(prefix):
            continue
        try:
            path_raw, size_raw = line[len(prefix) :].decode("ascii").split()
            size = int(size_raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if _UPLOAD_TEMP_PATH.fullmatch(path_raw) and size >= 0:
            result[path_raw] = size
    return result


def _changed_upload_temps(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """回報重啟後新建或增長的 upload 暫存檔；清理造成的刪除／縮小不算殘留執行。"""
    return sorted(
        path for path, size in after.items()
        if _UPLOAD_TEMP_PATH.fullmatch(path) and size > before.get(path, -1)
    )


def _echo_stall_watcher_script(*, nonce: str, chunk_size: int = 512) -> str:
    """在 DUT 的第二個完整 chunk 後關 echo，等停機重啟訊號後復原。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必須為正數")
    return f"""#!/bin/sh
trap '' TTOU
done_path=/tmp/.i282-echo-watcher-{nonce}.done
cleanup() {{
  stty echo </dev/tty >/dev/null 2>&1 || :
  rm -f "$0" "$done_path"
}}
trap cleanup EXIT
find_new_upload() {{
  for f in /tmp/.sw_upload_*; do
    [ -f "$f" ] || continue
    is_old=
    for old in "$@"; do [ "$f" = "$old" ] && is_old=1 && break; done
    [ -n "$is_old" ] || {{ printf '%s\\n' "$f"; return; }}
  done
}}
elapsed=0
first=
while [ -z "$first" ] && [ "$elapsed" -lt 30 ]; do
  [ -e "$done_path" ] && exit 0
  first=$(find_new_upload "$@")
  [ -n "$first" ] || {{ sleep 1; elapsed=$((elapsed + 1)); }}
done
if [ -n "$first" ]; then
  target_size={chunk_size * 2}
  elapsed=0
  while [ "$(wc -c < "$first")" -lt "$target_size" ] && [ "$elapsed" -lt 30 ]; do
    [ -e "$done_path" ] && exit 0
    sleep 1
    elapsed=$((elapsed + 1))
  done
  if [ "$(wc -c < "$first")" -ge "$target_size" ]; then
    stty -echo </dev/tty || exit 1
    printf '\\nI282_ECHO_OFF_%s\\n' '{nonce}'
    for f in /tmp/.sw_upload_*; do
      [ -f "$f" ] || continue
      printf 'I282_PRE_{nonce}_TEMP %s ' "$f"
      wc -c < "$f"
    done
    elapsed=0
    while [ ! -e "$done_path" ] && [ "$elapsed" -lt 180 ]; do
      sleep 1
      elapsed=$((elapsed + 1))
    done
    stty echo </dev/tty
    printf '\\nI282_ECHO_RESTORED_%s\\n' '{nonce}'
  fi
fi
rm -f "$0"
"""


def _body_push_stop_midline_clean_restart(ctx: Any, ta: Any, ta_com: str, board: dict[str, Any]) -> CaseResult:
    """在第二個完整 chunk 後製造 echo 停滯，於可證明的半行中 SIGTERM。"""
    import signal
    import time
    from concurrent.futures import ThreadPoolExecutor
    from .f10_login_creds import _session_by_com, _ta_com, _wait_dynamic_sessions, _wait_state

    local_path = ctx.case_dir / "f7-stop-midline.bin"
    watcher_local_path = ctx.case_dir / f"f7-i282-echo-watcher-{os.urandom(4).hex()}.sh"
    nonce = os.urandom(4).hex()
    watcher_remote_path = f"/tmp/.i282-echo-watcher-{nonce}.sh"
    watcher_done_path = f"/tmp/.i282-echo-watcher-{nonce}.done"
    remote_path = f"/tmp/swreg-i282-{os.getpid()}-{time.monotonic_ns()}.bin"
    uart_rx_path = ta._workdir / f"f7-i282-uart-rx-{nonce}.bin"
    uart_observer: _UartRxObserver | None = None
    chunk_size = 512
    current_com = ta_com
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(os.urandom(64 * 1024))

    def temp_snapshot() -> tuple[set[str], dict[str, int], bool, str]:
        command = (
            "printf 'I282_'$((6*7))'_BEGIN\\n'; "
            "for f in /tmp/* /tmp/.[!.]* /tmp/..?*; do [ -f \"$f\" ] || continue; "
            "printf 'I282_'$((6*7))'_PATH %s\\n' \"$f\"; "
            "case \"$f\" in /tmp/.sw_upload_*) "
            "printf 'I282_'$((6*7))'_TEMP %s ' \"$f\"; wc -c < \"$f\";; esac; done; "
            f"if [ ! -e {remote_path} ]; then printf 'I282_'$((6*7))'_DEST_ABSENT\\n'; fi; "
            "printf 'I282_'$((6*7))'_END\\n'"
        )
        result = _throwaway_line_command(ta, current_com, command)
        output = str(result.get("stdout") or "")
        begin, end = "I282_42_BEGIN", "I282_42_END"
        if result.get("status") != "done" or begin not in output or end not in output:
            return set(), {}, False, output
        section = output.split(begin, 1)[1].split(end, 1)[0]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        paths: set[str] = set()
        upload_sizes: dict[str, int] = {}
        for line in lines:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "I282_42_PATH":
                paths.add(fields[1])
            if len(fields) == 3 and fields[0] == "I282_42_TEMP":
                try:
                    upload_sizes[fields[1]] = int(fields[2])
                except ValueError:
                    continue
        return paths, upload_sizes, "I282_42_DEST_ABSENT" in lines, output

    def restart_throwaway() -> tuple[str | None, dict[str, Any] | None]:
        """以原 sandbox 重啟 daemon 並找回原板 session。"""
        ta.__exit__(None, None, None)
        ta.__enter__()
        listing = _wait_dynamic_sessions(ta, timeout_s=30.0, poll_s=0.5)
        restarted = _ta_com(listing, str(board.get("serial") or ""))
        ready = _wait_state(ta, restarted, "READY", timeout_s=90.0, poll_s=0.5)
        ctx.note("f7-stop-midline-restarted-session.json", str(ready))
        return (restarted, ready) if ready.get("state") == "READY" else (None, ready)

    def restore_target_echo(com: str, *, expect_marker: bool) -> bool:
        """通知 DUT watcher 復原 echo；未確認時直接復原並清掉遠端檔案。"""
        restore = _throwaway_line_command(
            ta,
            com,
            f"touch {shlex.quote(watcher_done_path)}; sleep 2; stty echo; "
            f"rm -f {shlex.quote(watcher_remote_path)} {shlex.quote(watcher_done_path)}",
            timeout_s=5.0,
        )
        marker = f"I282_ECHO_RESTORED_{nonce}".encode("ascii")
        marker_seen = marker in _rx_payload(ta.wal_text())
        if expect_marker:
            return marker_seen
        return restore.get("status") == "done" or marker_seen

    try:
        before_paths, before_upload_sizes, before_dest_absent, before_output = temp_snapshot()
        if not before_dest_absent:
            return CaseResult("SKIP", reason=f"目的路徑已存在或板端快照不完整：{before_output!r}",
                              category="environment", reason_code="remote_path_not_clean")

        stty_probe = _throwaway_line_command(ta, ta_com, "stty -g")
        if stty_probe.get("status") != "done":
            return CaseResult("SKIP", reason="DUT shell 無法以 stty 控制終端 echo，不能建立固定半行切點",
                              category="environment", reason_code="echo_stall_injection_unavailable")

        watcher_local_path.write_text(
            _echo_stall_watcher_script(
                nonce=nonce,
                chunk_size=chunk_size,
            ),
            encoding="utf-8",
        )
        watcher_push = ta.run(
            "--timeout", "60", "file", "push", "--selector", ta_com,
            "--local", str(watcher_local_path), "--remote", watcher_remote_path,
            timeout=80.0,
        )
        if watcher_push.get("ok") is not True:
            return CaseResult("SKIP", reason=f"無法安裝 DUT echo watcher：{watcher_push!r}",
                              category="environment", reason_code="echo_stall_injection_unavailable")
        baseline_upload_args = " ".join(shlex.quote(path) for path in before_upload_sizes)
        watcher_start = _throwaway_line_command(
            ta, ta_com,
            f"sh {shlex.quote(watcher_remote_path)} {baseline_upload_args} >/dev/tty 2>&1 </dev/null &",
        )
        if watcher_start.get("status") != "done":
            _throwaway_line_command(ta, ta_com, f"rm -f {shlex.quote(watcher_remote_path)}")
            return CaseResult("SKIP", reason=f"無法啟動 DUT echo watcher：{watcher_start!r}",
                              category="environment", reason_code="echo_stall_injection_unavailable")

        # watcher 已在 DUT 上就緒；以此刻的 `/tmp` 狀態作為新檔比較基準。
        before_paths, before_upload_sizes, before_dest_absent, before_output = temp_snapshot()
        if not before_dest_absent:
            restore_target_echo(ta_com, expect_marker=False)
            return CaseResult("SKIP", reason=f"watcher 安裝後目的路徑快照不完整：{before_output!r}",
                              category="environment", reason_code="remote_path_not_clean")

        try:
            uart_observer = _UartRxObserver(ta, ta_com, uart_rx_path)
        except (OSError, RuntimeError) as exc:
            return CaseResult("SKIP", reason=f"無法建立 throwaway console bridge byte observer：{exc!r}",
                              category="environment", reason_code="bridge_observer_unavailable")

        with ThreadPoolExecutor(max_workers=1) as pool:
            push = pool.submit(
                ta.run,
                "--timeout", "180", "file", "push", "--selector", ta_com,
                "--local", str(local_path), "--remote", remote_path,
                "--chunk-size", str(chunk_size),
                timeout=210.0,
            )
            deadline = time.monotonic() + 30.0
            observed_inflight = False
            while time.monotonic() < deadline and not push.done():
                session = _session_by_com(ta.run("session", "list", timeout=10.0), ta_com)
                if _midline_push_observed(
                    ta.wal_text(),
                    bridge_rx_bytes=uart_observer.bytes(),
                    echo_off_marker=f"I282_ECHO_OFF_{nonce}",
                    foreground_busy=session.get("foreground_busy") is True,
                ):
                    observed_inflight = True
                    partial_line = _uart_partial_chunk_echo(
                        uart_observer.bytes(), f"I282_ECHO_OFF_{nonce}",
                    )
                    ctx.note(
                        "f7-stop-midline-uart-cutpoint.json",
                        json.dumps({
                            "observer": "throwaway console PTY RX",
                            "captured_bytes": len(uart_observer.bytes()),
                            "partial_line_bytes": len(partial_line or b""),
                            "echo_off_marker_seen": True,
                            "foreground_busy": True,
                        }, ensure_ascii=False, sort_keys=True),
                    )
                    break
                time.sleep(0.1)
            if not observed_inflight:
                if push.done():
                    push.result()
                    restore_target_echo(ta_com, expect_marker=False)
                    return CaseResult("SKIP", reason="push 未進入兩個已送達 chunk 的在途狀態，未觸發停機",
                                      category="environment", reason_code="push_not_observed_inflight")
                # 限時內未取得 chunk 證據時停止 throwaway，避免留下背景傳輸。
                old_pgid = int(ta._proc_pgid)
                os.killpg(old_pgid, signal.SIGTERM)
                push.result(timeout=150.0)
                try:
                    daemon_exit_code, live_members = ta.wait_for_exit(timeout_s=15.0)
                except Exception as exc:
                    return CaseResult("FAIL", reason=f"throwaway 停止後無法確認已回收：{exc!r}",
                                      category="test", reason_code="daemon_stop_unconfirmed")
                if live_members:
                    try:
                        os.killpg(old_pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return CaseResult("FAIL", reason=f"throwaway 行程群仍有存活成員：{live_members}",
                                      category="test", reason_code="daemon_stop_timeout")
                if daemon_exit_code != 0:
                    return CaseResult("FAIL", reason=f"throwaway daemon 結束碼非 0：{daemon_exit_code}",
                                      category="test", reason_code="daemon_stop_exit_failed")
                restarted_com, ready = restart_throwaway()
                if restarted_com is None:
                    return CaseResult("FAIL", reason=f"清理 echo watcher 後 session 未 READY：{ready!r}",
                                      category="test", reason_code="restart_not_ready")
                restore_target_echo(restarted_com, expect_marker=False)
                return CaseResult("SKIP", reason="30 秒內未證明停在完整 chunk 後的半行，未觸發有效驗收",
                                  category="environment", reason_code="push_midline_not_observed")

            old_pgid = int(ta._proc_pgid)
            os.killpg(old_pgid, signal.SIGTERM)
            push_reply = push.result(timeout=150.0)

        try:
            daemon_exit_code, live_members = ta.wait_for_exit(timeout_s=15.0)
        except Exception as exc:
            return CaseResult("FAIL", reason=f"SIGTERM 後無法有界等待並回收 throwaway daemon：{exc!r}",
                              category="test", reason_code="daemon_stop_timeout")
        if live_members:
            try:
                os.killpg(old_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return CaseResult("FAIL", reason=f"SIGTERM 後 throwaway 行程群仍有存活成員：{live_members}",
                              category="test", reason_code="daemon_stop_timeout")
        if daemon_exit_code != 0:
            return CaseResult("FAIL", reason=f"throwaway daemon 結束碼非 0：{daemon_exit_code}",
                              category="test", reason_code="daemon_stop_exit_failed")

        ctx.note("f7-stop-midline-push-reply.json", str(push_reply))
        if push_reply.get("ok") is True:
            stop_observed = False
        else:
            stop_observed = push_reply.get("error_code") in {
                "COMMAND_ABORTED_IN_FLIGHT", "FLUSHED_BY_RECOVERY",
            }

        stopped_wal = ta.wal_text()
        pre_restart_sizes = _upload_size_markers(
            stopped_wal, marker=f"I282_PRE_{nonce}_TEMP",
        )
        if not pre_restart_sizes:
            restarted_com, ready = restart_throwaway()
            if restarted_com is not None:
                restore_target_echo(restarted_com, expect_marker=False)
            return CaseResult("SKIP", reason="DUT watcher 未留下停機前暫存檔位元組數，殘留執行證據不足",
                              category="environment", reason_code="insufficient_evidence")
        tx_stream = b"".join(
            row["_payload"] for row in _decode_wal_records(stopped_wal)
            if row.get("dir") == "TX"
        )
        clear_count = tx_stream.count(b"\x15\n")
        clear_verdict = _midline_shutdown_verdict(clear_count)
        if clear_verdict is not None:
            restarted_com, ready = restart_throwaway()
            if restarted_com is not None:
                restore_target_echo(restarted_com, expect_marker=False)
            return clear_verdict

        chunks_before_restart = _chunk_tx_count(ta)
        # 清理已退出的第一個 run dir，再用相同隔離 workdir 啟動 throwaway。
        restarted_com, ready = restart_throwaway()
        if restarted_com is None:
            return CaseResult("FAIL", reason=f"SIGTERM 重啟後 session 未回 READY：{ready!r}",
                              category="test", reason_code="restart_not_ready")
        current_com = restarted_com

        if not restore_target_echo(restarted_com, expect_marker=True):
            return CaseResult("FAIL", reason="DUT echo watcher 未確認復原並自行退出",
                              category="test", reason_code="echo_state_restore_unconfirmed")

        self_test = ta.run("session", "self-test", "--selector", restarted_com, timeout=60.0)
        ctx.note("f7-stop-midline-self-test.json", str(self_test))
        if self_test.get("ok") is not True:
            return CaseResult("FAIL", reason=f"重啟後 self-test 未通過：{self_test!r}",
                              category="test", reason_code="restart_self_test_failed")

        after_temps, after_upload_sizes, after_dest_absent, after_output = temp_snapshot()
        new_temps = after_temps - before_paths
        ctx.note("f7-stop-midline-temp-files.txt", after_output)
        if not after_dest_absent:
            return CaseResult("FAIL", reason="中止的 push 不得建立目的檔",
                              category="test", reason_code="partial_push_destination_created")
        if any(not _UPLOAD_TEMP_PATH.fullmatch(path) for path in new_temps):
            return CaseResult("FAIL", reason=f"DUT 出現非 push 暫存檔：{sorted(new_temps)}",
                              category="test", reason_code="unexpected_remote_file")

        changed_temps = _changed_upload_temps(pre_restart_sizes, after_upload_sizes)
        if changed_temps:
            return CaseResult("FAIL", reason=f"重啟後殘留 push 命令改動 DUT 暫存檔：{changed_temps}",
                              category="test", reason_code="residual_command_executed")

        extra_chunk_commands = _chunk_tx_count(ta) - chunks_before_restart
        if extra_chunk_commands != 0:
            return CaseResult("FAIL", reason=f"重啟後多送 {extra_chunk_commands} 個 chunk 命令",
                              category="test", reason_code="extra_chunk_after_restart")

        if new_temps:
            cleanup = "rm -f " + " ".join(new_temps)
            _throwaway_line_command(ta, restarted_com, cleanup)
        if not stop_observed:
            return CaseResult("SKIP", reason=f"push 未回報在途中止碼：{push_reply!r}；restart/self-test 已通過",
                              category="environment", reason_code="midline_abort_not_observed")
        return CaseResult("PASS", reason="push 停在兩個完整 chunk 後的半行；SIGTERM 恰清行一次，重啟後無暫存檔增長或額外 chunk")
    finally:
        if uart_observer is not None:
            uart_observer.close()
        uart_rx_path.unlink(missing_ok=True)
        _cleanup_local(local_path, watcher_local_path)


@_case(
    "f7-push-stop-midline-clean-restart",
    "push 停在 chunk 半行時 SIGTERM 後重啟回 READY，DUT 不執行殘留命令",
    issues=("#282",),
    requires=("posix_throwaway_daemon",),
    destructive=True,
    hints=(
        "只在擁有者 bench 執行：DUT watcher 在兩個完整 chunk 後關閉 echo，確認下一個 chunk TX 停在無換行半行才 SIGTERM。",
        "以 DUT 回報的停機前暫存檔位元組數對照重啟後大小，並驗證停機 TX 恰有一次 Ctrl-U＋換行、沒有額外 chunk 或目的檔。",
        "此 case 會先由既有 F10 交接骨架 release 正式 session，finally 以 device attach 收回；需 allow_destructive: true。",
    ),
)
def f7_push_stop_midline_clean_restart(ctx: Any) -> CaseResult:
    """#282 真機差異：Ctrl-U 相容性與實體線路行收尾期限。"""
    from .f10_device_handoff import _run_with_handoff

    return _run_with_handoff(ctx, "f7-push-stop-midline-clean-restart", _body_push_stop_midline_clean_restart)
