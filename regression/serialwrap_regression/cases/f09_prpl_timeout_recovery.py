"""F9／issue 308：prpl 逾時只送 Ctrl-C，且 shell 不會被 Ctrl-D 登出。"""
from __future__ import annotations

import base64
import re
import time

from realhw.harness import CaseResult

from ..harness import Case, register


def _tx_control_bytes(ctx, com: str, start_seq: int) -> tuple[list[int], str | None]:
    """只回傳命令後的 Ctrl-C／Ctrl-D TX，不把 RX 原文寫入回歸報告。"""
    hits: list[int] = []
    cursor = start_seq
    for _ in range(40):
        page = ctx.sw.run("log", "tail-raw", "--selector", com,
                          "--from-seq", str(cursor), "--limit", "200")
        if not page.get("ok") or not page.get("wal_file_exists", True):
            return hits, "wal_unavailable"
        records = page.get("records") or []
        if not records and page.get("truncated"):
            return hits, "wal_page_stalled"
        for record in records:
            if record.get("dir") == "TX":
                try:
                    payload = base64.b64decode(record.get("payload_b64") or "", validate=True)
                except (ValueError, TypeError):
                    return hits, "wal_decode_failed"
                if payload in (b"\x03", b"\x04"):
                    hits.append(payload[0])
            cursor = max(cursor, int(record.get("seq") or cursor))
        if not page.get("truncated"):
            return hits, None
    return hits, "wal_page_limit"


def _run(ctx) -> CaseResult:
    board = next((row for row in ctx.cfg.get("boards", [])
                  if str(row.get("platform", "")).lower() == "prpl"), None)
    if board is None:
        return CaseResult("SKIP", reason="testbed 無 prpl DUT", category="environment",
                          reason_code="prpl_board_missing")
    com = str(board["com"])
    before = ctx.sw.session(com)
    if before.get("state") != "READY":
        return CaseResult("SKIP", reason=f"{com} 尚非 READY", category="environment",
                          reason_code="board_not_ready")
    wal = ctx.sw.run("wal", "current-seq")
    if not wal.get("ok") or not isinstance(wal.get("seq"), int):
        return CaseResult("SKIP", reason="無法取得命令前 WAL 序號", category="environment",
                          reason_code="wal_unavailable")

    identity_marker = f"P290_SHELL_ID_{time.time_ns()}"
    identity_command = f"echo {identity_marker}_$$"
    before_identity = ctx.sw.submit_and_wait(com, identity_command, cmd_timeout=10.0)
    ctx.note("shell-identity-before.json", str(before_identity))
    before_match = re.search(rf"{re.escape(identity_marker)}_(\d+)",
                             str(before_identity.get("stdout") or ""))
    if before_identity.get("status") != "done" or before_match is None:
        return CaseResult("FAIL", reason="timeout 前無法取得可辨識的 ash shell PID",
                          category="test", reason_code="shell_identity_unavailable")
    before_pid = before_match.group(1)

    start_seq = int(wal["seq"])
    timeout_s = 4.0
    result = ctx.sw.submit_and_wait(com, "sleep 30", cmd_timeout=timeout_s,
                                    settle_s=0.3, poll_s=0.2)
    ctx.note("timeout-command.json", str(result))
    keys, wal_problem = _tx_control_bytes(ctx, com, start_seq)
    ctx.note("recovery-key-sequence.txt", f"keys={keys!r} wal_problem={wal_problem!r}\n")
    if wal_problem:
        return CaseResult("FAIL", reason=f"無法從 WAL 驗證救援鍵：{wal_problem}",
                          category="environment", reason_code=wal_problem)
    if (result.get("status") != "done" or "error_code" not in result
            or result.get("error_code") is not None or result.get("recovery_action") != "CTRL_C"):
        return CaseResult("FAIL", reason=("prpl 逾時未以 Ctrl-C 恢復 prompt："
                                           f"status={result.get('status')!r} "
                                           f"error_code={result.get('error_code')!r} "
                                           f"recovery_action={result.get('recovery_action')!r}"),
                          category="test", reason_code="timeout_not_recovered_by_ctrl_c")
    if keys != [0x03]:
        return CaseResult("FAIL", reason=f"預期只送一個 Ctrl-C（0x03），實際救援 TX={keys!r}；Ctrl-D 會登出 ash",
                          category="test", reason_code="unexpected_timeout_recovery_keys")

    session = ctx.sw.session(com)
    if session.get("state") != "READY":
        return CaseResult("FAIL", reason=f"Ctrl-C 後 session 未維持 READY：{session.get('state')!r}",
                          category="test", reason_code="session_not_ready_after_ctrl_c")
    after_identity = ctx.sw.submit_and_wait(com, identity_command, cmd_timeout=10.0)
    ctx.note("shell-identity-after.json", str(after_identity))
    after_match = re.search(rf"{re.escape(identity_marker)}_(\d+)",
                            str(after_identity.get("stdout") or ""))
    if after_identity.get("status") != "done" or after_match is None:
        return CaseResult("FAIL", reason="timeout 後無法取得 ash shell PID",
                          category="test", reason_code="shell_identity_unavailable")
    if after_match.group(1) != before_pid:
        return CaseResult("FAIL", reason=("Ctrl-C 前後 ash shell 身分不同："
                                           f"before={before_pid} after={after_match.group(1)}"),
                          category="test", reason_code="shell_identity_changed")

    marker = f"P290_ALIVE_{int(time.time())}"
    follow = ctx.sw.submit_and_wait(com, f"echo {marker}", cmd_timeout=10.0)
    ctx.note("post-timeout-shell.json", str(follow))
    if follow.get("status") != "done" or marker not in str(follow.get("stdout") or ""):
        return CaseResult("FAIL", reason="逾時後 shell 後續命令未成功，可能已登出",
                          category="test", reason_code="shell_not_usable_after_timeout")
    return CaseResult("PASS", reason="prpl 逾時只送 Ctrl-C，session 留在 READY 且 ash 可繼續執行命令")


register(Case(
    id="f9-prpl-timeout-ctrl-c-no-logout",
    family="F9",
    title="prpl 命令逾時只送 Ctrl-C 且不登出 ash",
    run=_run,
    issues=("#308",),
    destructive=True,
    hints=(
        "此 case 會中斷 DUT 上的 sleep 命令；需 owner 排定授權的 prpl DUT，並確認其 managed profile 使用 p290 欄位。",
        "依裁決，本機只登記 case，不連真板；owner 實跑前保持 BLOCKED-OWNER。",
    ),
))
