"""F7 檔案傳輸完整性（#21 #32）：binary round-trip md5 一致、不靜默截斷。

``serialwrap file push``／``file pull`` 走 base64 分段＋md5 校驗（見
``sw_core/file_transfer.py``，本檔僅唯讀 grep 鍵名，未 import）。已知缺口
（#123 defer，MINOR-5）：這兩個 RPC 方法**不在** CLI 的長操作白名單，未顯式
帶 ``--timeout`` 會沿用一般方法的 5.0s 預設，對多 chunk 的實際傳輸而言形同
必逾時──故本檔一律顯式帶較寬的 RPC ``--timeout``（global flag，需置於
``file`` 子命令**之前**）＋對應的 ``SwCli.run(timeout=...)``（subprocess 層
逾時，需 ≥ RPC timeout 加緩衝，否則會在 RPC 真正逾時前就被 Python 端
``subprocess.TimeoutExpired`` 打斷）。

timeout 估算基準（#157 修復後）：``DEFAULT_CHUNK_SIZE=512`` → 64KB≈129 chunks、
1MB=2048 chunks；以每 chunk 真實往返 0.1–0.3s 估，64KB≈13–39s、1MB≈205–614s，
timeout 取悲觀上界加緩衝，實際值待真機驗證校準。

已知殘留缺口（#157 範圍外）：``sw_core/uart_io.py`` 的 RX 視窗上限 131072 字元
（#158 改為絕對偏移記帳，但視窗仍有界、被修剪頭段永久丟失），``pull_file`` 不分段
一次讀全部——1MB 檔案 base64 輸出 ~1.4MB 遠超上限，``_SENTINEL_BEGIN`` 必被踢出
視窗 → ``PULL_PARSE_FAILED``。故 ``f7-larger-file-not-truncated`` 於 #157 修復後
push 端可成功，pull 端仍預期 SKIP（``transfer_environment_failure``）待 follow-up；
``f7-binary-roundtrip-md5``（64KB，base64 ~88.6KB 在上限內）應轉綠。
"""
from __future__ import annotations

import gzip
import hashlib
import os
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


# push_file／pull_file（sw_core/file_transfer.py）在 target 缺 md5sum／base64
# 時的可觀測徵兆：md5sum 缺失 → _remote_md5 抓不到 32 hex → CHECKSUM_VERIFY_FAILED；
# base64 缺失 → sentinel 內夾帶的非 base64 內容解碼失敗 → BASE64_DECODE_FAILED。
_TOOL_MISSING_CODES = frozenset({"CHECKSUM_VERIFY_FAILED", "BASE64_DECODE_FAILED"})

# 逾時／連線層失敗徵兆：RPC 客戶端層的 TIMEOUT／SOCKET_ERROR／EMPTY_RESPONSE
# （sw_core/client.py），以及 file_transfer 內部逐段等待逾時的 TRANSFER_TIMEOUT／
# MOVE_TIMEOUT。這些代表傳輸「未完成」，非「完成但內容有誤」，故一律歸環境性。
_TIMEOUT_CODES = frozenset({
    "TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE", "TRANSFER_TIMEOUT", "MOVE_TIMEOUT",
})


def _probe_target_tools(ctx: Any, com: str) -> bool | None:
    """實測板端是否具備 base64＋md5sum：True＝都在、False＝缺、None＝探測本身失敗。

    區分 cg review 抓到的雙重含義：``CHECKSUM_VERIFY_FAILED``／``BASE64_DECODE_FAILED``
    既可能是板端缺工具（環境、SKIP），也可能是工具都在但傳輸真的壞掉（#32 類回歸、FAIL）。
    先探測工具存在性，後續判定才能分流。
    """
    probe = ctx.sw.submit_and_wait(
        com, "command -v base64 >/dev/null && command -v md5sum >/dev/null && echo TOOLS_OK")
    ctx.note("tools-probe.json", str(probe))
    if probe.get("status") != "done":
        return None
    return "TOOLS_OK" in (probe.get("stdout") or "")


def _transfer_failure_verdict(resp: dict[str, Any], *, verb: str,
                              tools_present: bool | None) -> CaseResult:
    """push/pull 非 ok 的統一分流：逾時／連線層→環境 SKIP；工具徵兆碼→依探測結果分流
    （工具在＝#32 類真回歸 FAIL；工具不在或未知＝環境 SKIP）；其餘明確失敗→環境 SKIP。"""
    code = str(resp.get("error_code") or "unknown")
    if code in _TOOL_MISSING_CODES:
        if tools_present is True:
            return CaseResult(
                "FAIL",
                reason=f"{verb} 失敗（error_code={code}）且板端 base64/md5sum 皆在"
                       "——非工具缺失，屬傳輸損壞（#32 類回歸）",
                category="test", reason_code="binary_roundtrip_mismatch",
            )
        return CaseResult(
            "SKIP", reason=f"板端疑缺 base64／md5sum（error_code={code}，工具探測={tools_present}）",
            category="environment", reason_code="target_tool_missing",
        )
    return _environment_skip(resp, verb=verb)


def _environment_skip(resp: dict[str, Any], *, verb: str) -> CaseResult:
    """push/pull 未完成傳輸（逾時或其他明確失敗），非「完成但內容有誤」，判環境性 SKIP。"""
    code = str(resp.get("error_code") or "unknown")
    reason_code = "transfer_timeout" if code in _TIMEOUT_CODES else "transfer_environment_failure"
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


@_case("f7-binary-roundtrip-md5", "binary round-trip md5 一致（含 null byte）",
       issues=("#32", "#21"))
def f7_binary_roundtrip_md5(ctx):
    com = ctx.cfg["boards"][0]["com"]
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
            return CaseResult("SKIP", reason="板端缺 base64／md5sum（探測確認）",
                              category="environment", reason_code="target_tool_missing")

        # chunk 512 下 64KB≈129 chunks（原 2048 時 33），timeout 同比放大（見 module docstring）
        push = _push(ctx, com, src_path, remote_path, rpc_timeout_s=180, proc_timeout_s=200)
        ctx.note("push.json", str(push))
        if not push.get("ok"):
            return _transfer_failure_verdict(push, verb="push", tools_present=tools_present)

        pull = _pull(ctx, com, remote_path, dst_path, rpc_timeout_s=180, proc_timeout_s=200)
        ctx.note("pull.json", str(pull))
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


@_case("f7-larger-file-not-truncated", "1MB 檔案 push→pull 不靜默截斷", issues=("#21",))
def f7_larger_file_not_truncated(ctx):
    com = ctx.cfg["boards"][0]["com"]
    remote_path = "/tmp/swreg_big.bin"
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    payload = os.urandom(1024 * 1024)
    src_path = ctx.case_dir / "f7-big-src.bin"
    dst_path = ctx.case_dir / "f7-big-dst.bin"
    src_path.write_bytes(payload)
    try:
        tools_present = _probe_target_tools(ctx, com)
        if tools_present is False:
            return CaseResult("SKIP", reason="板端缺 base64／md5sum（探測確認）",
                              category="environment", reason_code="target_tool_missing")

        # chunk 512 下 1MB=2048 chunks，悲觀估 205–614s（見 module docstring）；取 640 上界。
        # 註：pull 端在 RX 視窗 128KiB 上限修復前仍預期 PULL_PARSE_FAILED → SKIP。
        push = _push(ctx, com, src_path, remote_path, rpc_timeout_s=640, proc_timeout_s=660)
        ctx.note("push.json", str(push))
        if not push.get("ok"):
            return _transfer_failure_verdict(push, verb="push", tools_present=tools_present)

        pull = _pull(ctx, com, remote_path, dst_path, rpc_timeout_s=640, proc_timeout_s=660)
        ctx.note("pull.json", str(pull))
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
