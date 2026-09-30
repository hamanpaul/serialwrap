"""F9／#224：BRCM 真板重開後，前導空白 BDK prompt 須完成自動登入。"""
from __future__ import annotations

import base64
import re
import time
from typing import Any

from realhw.harness import CaseResult

from .. import guards
from ..harness import Case, register


_BOOT_ID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
_SPACED_PROMPT = re.compile(rb"(?:^|[\r\n])[ \t]+[>#][ \t]*(?:\r?\n|$)")
_TERMINAL = {"done", "error", "timeout"}


def _read_boot_id(ctx: Any, com: str) -> tuple[str | None, str]:
    """只讀 kernel boot_id；不把 console 原文或帳密寫入 evidence。"""
    sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd",
                     "cat /proc/sys/kernel/random/boot_id", "--source",
                     "agent:serialwrap-regression", "--mode", "line", "--cmd-timeout", "12")
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return None, f"submit:{sub.get('error_code') or sub.get('_stderr') or 'no_cmd_id'}"
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        st = ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id))
        cmd = st.get("command") or {}
        if cmd.get("status") in _TERMINAL:
            match = _BOOT_ID.search(str(cmd.get("stdout") or ""))
            if cmd.get("status") == "done" and match:
                return match.group(0), str(cmd_id)
            return None, f"{cmd_id}:{cmd.get('error_code') or cmd.get('status')}"
        time.sleep(0.5)
    return None, f"{cmd_id}:status_timeout"


def _wal_markers(ctx: Any, com: str, start_seq: int) -> tuple[set[str], list[str], str | None]:
    """只保留事件與序號，不把 RX 原文、使用者名稱或 password 寫入報告。"""
    seen: set[str] = set()
    timeline: list[str] = []
    cursor = start_seq
    # 一次開機有數百筆 WAL；循序翻頁，避免只看最後 200 筆而漏掉 BDK prompt。
    for _ in range(20):
        page = ctx.sw.run("log", "tail-raw", "--selector", com,
                          "--from-seq", str(cursor), "--limit", "200")
        if not page.get("ok") or page.get("truncated"):
            return seen, timeline, "wal_unavailable_or_truncated"
        records = page.get("records") or []
        for rec in records:
            try:
                payload = base64.b64decode(rec.get("payload_b64") or "", validate=True)
            except (ValueError, TypeError):
                return seen, timeline, "wal_decode_failed"
            direction = rec.get("dir")
            tag = None
            if direction == "RX" and _SPACED_PROMPT.search(payload):
                tag = "spaced_bdk_prompt"
            elif direction == "TX" and payload.strip() == b"sh":
                tag = "post_login_sh"
            elif direction == "TX" and payload.startswith(b"echo __READY__"):
                tag = "ready_probe_tx"
            elif direction == "RX" and b"__READY__" in payload:
                tag = "ready_probe_rx"
            if tag:
                seen.add(tag)
                timeline.append(f"seq={rec.get('seq')} {tag}")
            cursor = max(cursor, int(rec.get("seq") or cursor))
        if len(records) < 200 or cursor >= int(page.get("current_seq") or 0):
            return seen, timeline, None
    return seen, timeline, "wal_page_limit"


register(Case(
    id="f9-brcm-spaced-prompt-reboot-login",
    family="F9",
    title="BRCM 重開機後帶前導空白提示符可自動登入並恢復 READY",
    run=lambda ctx: f9_brcm_spaced_prompt_reboot_login(ctx),
    issues=("#224",),
    destructive=True,
    hints=(
        "只對 testbed 中 platform=bcm 的 STA 執行；需 allow_destructive: true。",
        "case 不會輸入帳密、不會讀取或輸出 brcm.env；使用既有 broker profile。",
    ),
))


def f9_brcm_spaced_prompt_reboot_login(ctx: Any) -> CaseResult:
    """實機 oracle：boot ID 必變，且 WAL 實際出現空白 prompt → sh → READY probe。"""
    board = next((b for b in ctx.cfg.get("boards", [])
                  if str(b.get("platform", "")).lower() == "bcm"), None)
    if board is None:
        return CaseResult("SKIP", reason="testbed 無 bcm 板卡", category="environment",
                          reason_code="bcm_board_missing")
    com = str(board["com"])
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    session = ctx.sw.session(com)
    if session.get("state") != "READY" or session.get("platform") != "bcm":
        return CaseResult("SKIP", reason=f"{com} 尚非 READY 的 bcm session",
                          category="environment", reason_code="bcm_not_ready")
    before, before_cmd = _read_boot_id(ctx, com)
    if not before:
        return CaseResult("SKIP", reason=f"無法取得 reboot 前 boot ID（{before_cmd}）",
                          category="environment", reason_code="boot_id_unavailable")
    wal = ctx.sw.run("wal", "current-seq")
    if not wal.get("ok") or not isinstance(wal.get("seq"), int):
        return CaseResult("SKIP", reason="無法取得 reboot 前 WAL seq", category="environment",
                          reason_code="wal_unavailable")
    start_seq = int(wal["seq"])
    submitted = False
    try:
        reboot = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sync; reboot",
                            "--source", "agent:serialwrap-regression", "--mode", "line",
                            "--cmd-timeout", "10")
        reboot_id = reboot.get("cmd_id")
        if not reboot_id:
            return CaseResult("FAIL", reason=f"reboot 未被 broker 接受：{reboot.get('error_code')}",
                              category="environment", reason_code="reboot_submit_failed")
        submitted = True
        ctx.note("reboot-command.txt", f"com={com} cmd_id={reboot_id} wal_start_seq={start_seq}\n")

        # 避免把 reboot 尚未執行時的舊 READY 誤認為「重開後已恢復」。
        time.sleep(5.0)
        deadline = time.monotonic() + boot_wait_s
        transitions: list[str] = []
        last_state = None
        ready = False
        while time.monotonic() < deadline:
            s = ctx.sw.session(com)
            state = s.get("state")
            if state != last_state:
                transitions.append(f"{time.monotonic():.1f} {state!r}")
                last_state = state
            if (state == "READY" and not s.get("ready_reconfirm_pending")
                    and not s.get("ready_reconfirm_failed")
                    and s.get("boot_quiet_remaining_s") is None):
                current, current_cmd = _read_boot_id(ctx, com)
                if current and current != before:
                    ready = True
                    break
                transitions.append(f"boot_id_check={current_cmd} changed={bool(current and current != before)}")
            time.sleep(3.0)
        transition_path = ctx.note("sta-state-transitions.txt", "\n".join(transitions) + "\n")
        markers, timeline, wal_error = _wal_markers(ctx, com, start_seq)
        wal_path = ctx.note("sta-wal-markers.txt", "\n".join(timeline) + "\n")
        evidence = {"states": transition_path, "wal-markers": wal_path}
        if not ready:
            return CaseResult("FAIL", reason=f"{boot_wait_s:.0f}s 內 boot ID 未變且恢復 READY",
                              category="test", reason_code="bcm_reboot_not_ready", evidence=evidence)
        if wal_error:
            return CaseResult("SKIP", reason=f"WAL 證據不完整：{wal_error}",
                              category="environment", reason_code=wal_error, evidence=evidence)
        needed = {"spaced_bdk_prompt", "post_login_sh", "ready_probe_tx", "ready_probe_rx"}
        if "spaced_bdk_prompt" not in markers:
            return CaseResult("SKIP", reason="本輪未出現帶前導空白的 BDK prompt，未觸發 #224 情境",
                              category="environment", reason_code="spaced_prompt_not_observed",
                              evidence=evidence)
        if not needed <= markers:
            return CaseResult("FAIL", reason=f"#224 登入證據缺失：{','.join(sorted(needed - markers))}",
                              category="test", reason_code="bcm_auto_login_incomplete", evidence=evidence)
        ordered = [item.split(" ", 1)[1] for item in timeline]
        positions = [ordered.index(tag) for tag in (
            "spaced_bdk_prompt", "post_login_sh", "ready_probe_tx", "ready_probe_rx")]
        if positions != sorted(positions):
            return CaseResult("FAIL", reason="#224 登入事件順序錯誤（BDK prompt／sh／READY nonce）",
                              category="test", reason_code="bcm_auto_login_order", evidence=evidence)
        return CaseResult("PASS", reason="boot ID 改變；前導空白 BDK prompt 後自動 sh 與 READY probe 成功",
                          evidence=evidence)
    finally:
        if submitted:
            guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)
