"""F5 console 共存與 raw ownership（#78 #7 #8 #42 #11 #53）。

實查依據（唯讀，見 PR note）：``serialwrap session console-list --selector COM0`` 回應形狀為
``{"consoles":[{"client_id":..., "interactive_owner": bool, "label": "primary"|..., "vtty": "/dev/pts/N"}],
"session": {...}}``——``interactive_owner`` 欄位名與 ``realhw/cases/p0.py`` 的 ``p0-console-raw``
一致，無需調整。第一個 console 取得 raw ownership 後，第二個以後的 console 走 line-buffer
（``interactive_owner`` 為 false）；agent 命令提交時 daemon 會 ``suspend_interactive()`` →
執行命令（human 按鍵累積在 deferred buffer）→ ``resume_interactive()``（flush 回 UART）。
本檔驗證：ownership 撐過多輪 agent 命令（#78）、deferred bytes 不丟（#78）、對端消失後
console 正確回收不留假性佔用（#53 #11）、第二 console 不搶 ownership 也不擋 agent 命令（#7 #8）。
"""
from __future__ import annotations

import time

from realhw.drivers import strip_ansi
from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F5", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


def _console_list(ctx, com: str) -> list[dict]:
    """讀 ``session console-list``，落 evidence，回傳 ``consoles`` 陣列（可能為空）。"""
    cl = ctx.sw.run("session", "console-list", "--selector", com)
    ctx.note("console-list.json", str(cl))
    return cl.get("consoles") or []


def _has_owner(consoles: list[dict]) -> bool:
    return any(c.get("interactive_owner") for c in consoles)


def _await_terminal(ctx, cmd_id, *, timeout_s: float, poll_s: float = 0.5) -> dict:
    """輪詢 ``cmd status`` 到終態（done/error/timeout）；逾時回最後一次讀值（不 raise）。

    照 ``cases/f06_rpc_liveness.py`` 同名 helper 的作法，避免殘留 pending 命令拖累後續 case。
    """
    if not cmd_id:
        return {}
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        st = ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id))
        last = st.get("command") or {}
        if last.get("status") in ("done", "error", "timeout"):
            return last
        time.sleep(poll_s)
    return last


def _tab_complete_check(ctx, ses: str) -> bool:
    """照 ``p0-console-raw`` 手法：送 "ec"（不含 Enter）＋Tab，capture 應含 "echo"。

    回傳補完是否成功（True＝仍是 raw 路徑；False＝疑似掉回 line-buffer）。
    """
    ctx.tmux.send(ses, "ec", enter=False)
    ctx.tmux.send_key(ses, "Tab")
    time.sleep(2)
    pane = ctx.tmux.capture(ses)
    ctx.note("pane-tab.txt", pane)
    return "echo" in strip_ansi(pane)


@_case(
    "f5-raw-ownership-survives-agent-rounds",
    "raw interactive ownership 撐過連續 5 輪 agent 命令（suspend/resume 不掉權）",
    issues=("#78",),
    requires=("tmux",),
    hints=(
        "每輪 submit_and_wait 都會觸發 bridge.suspend_interactive()→執行→resume_interactive()，"
        "若 resume 路徑有回歸，ownership 可能在某輪之後悄悄消失或退化成 line-buffer。",
        "照 p0-console-raw：new-session 後 sleep 6 等 console-attach＋minicom 起來。",
    ),
)
def f5_raw_ownership_survives_agent_rounds(ctx):
    """開 console 拿 ownership → 連續 5 輪 agent 命令 → ownership 與 Tab 補完須仍在（#78 回歸）。"""
    com = ctx.cfg["boards"][0]["com"]
    ses = ctx.tmux.name("f5rnd")
    ctx.tmux.new(ses, f"serialwrap-minicom {com}")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        consoles = _console_list(ctx, com)
        if not _has_owner(consoles):
            return CaseResult(
                "FAIL",
                reason="第一個 console 一開始就未拿到 raw interactive ownership，無法驗證存活",
                category="test",
                reason_code="raw_ownership_not_granted",
            )

        rounds_evidence = []
        for i in range(5):
            res = ctx.sw.submit_and_wait(com, f"echo r{i}", cmd_timeout=8.0)
            rounds_evidence.append(res)
            if (res.get("status") or "") != "done" or f"r{i}" not in (res.get("stdout") or ""):
                ctx.note("rounds.json", str(rounds_evidence))
                return CaseResult(
                    "FAIL",
                    reason=f"第 {i} 輪 agent 命令未正常完成（status={res.get('status')}），"
                    "無法驗證 ownership 是否存活",
                    category="environment",
                    reason_code="agent_round_precondition_failed",
                    evidence={"rounds": ctx.note("rounds.json", str(rounds_evidence))},
                )
        ctx.note("rounds.json", str(rounds_evidence))

        consoles_after = _console_list(ctx, com)
        if not _has_owner(consoles_after):
            return CaseResult(
                "FAIL",
                reason="連續 5 輪 agent 命令後，raw interactive ownership 已消失（#78 回歸）",
                category="test",
                reason_code="raw_ownership_lost_after_rounds",
            )

        if not _tab_complete_check(ctx, ses):
            return CaseResult(
                "FAIL",
                reason="5 輪 agent 命令後 Tab 補完未出現（疑似 raw 路徑掉回 line-buffer，#78 回歸）",
                category="test",
                reason_code="raw_fallback_linebuffer",
            )
        ctx.tmux.send_key(ses, "C-u")  # 清掉半行 "ec"，還原乾淨 prompt
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)  # 等 router 清理 detach


@_case(
    "f5-deferred-bytes-flushed",
    "agent 命令執行期間 human console 按鍵不丟（deferred buffer 正確 flush）",
    issues=("#78",),
    requires=("tmux",),
    hints=(
        "agent 命令執行中 bridge 會 suspend human raw mode，此時的按鍵會進 deferred buffer；"
        "命令結束 resume 時應把 deferred buffer flush 回 UART，不應丟鍵。",
        "submit（非 submit_and_wait）不等待，才能在命令仍在跑時把鍵盤輸入送進 deferred buffer。",
    ),
)
def f5_deferred_bytes_flushed(ctx):
    """命令執行中送 "ec"（不含 Enter）→ 命令結束後補 Tab → 應出現 "echo"（#78 回歸）。"""
    com = ctx.cfg["boards"][0]["com"]
    ses = ctx.tmux.name("f5def")
    ctx.tmux.new(ses, f"serialwrap-minicom {com}")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        consoles = _console_list(ctx, com)
        if not _has_owner(consoles):
            return CaseResult(
                "FAIL",
                reason="console 未拿到 raw interactive ownership，無法驗證 deferred buffer 行為",
                category="environment",
                reason_code="raw_ownership_not_granted",
            )

        sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sleep 3", "--cmd-timeout", "8")
        ctx.note("submit.json", str(sub))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return CaseResult(
                "FAIL",
                reason=f"submit 未回 cmd_id（無法測試 deferred flush，回應={sub}）",
                category="environment",
                reason_code="submit_no_cmd_id",
            )

        time.sleep(0.5)  # 讓 daemon 進入該命令、suspend_interactive() 已生效
        ctx.tmux.send(ses, "ec", enter=False)  # 命令執行中送鍵，應進 deferred buffer

        terminal = _await_terminal(ctx, cmd_id, timeout_s=10.0)
        ctx.note("terminal.json", str(terminal))
        time.sleep(2)  # 等 resume_interactive() flush 完成（如 plan 所述）

        ctx.tmux.send_key(ses, "Tab")
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane-tab.txt", pane)
        if "echo" not in strip_ansi(pane):
            return CaseResult(
                "FAIL",
                reason="命令執行期間送出的 'ec' 未在命令結束後被 flush 回 UART"
                "（Tab 補完未見 'echo'，deferred bytes 疑似遺失，#78 回歸）",
                category="test",
                reason_code="deferred_bytes_lost",
            )
        ctx.tmux.send_key(ses, "C-u")  # 清掉半行，還原乾淨 prompt
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)  # 等 router 清理 detach


@_case(
    "f5-console-peer-gone-recycled",
    "對端消失（非正常 detach）後 console 正確回收，無假性佔用",
    issues=("#53", "#11"),
    requires=("tmux",),
    hints=(
        "tmux kill-session 模擬對端異常消失（非透過 minicom 正常退出 detach）；daemon 需靠"
        "holder-probe／socket EOF 之類的機制自行偵測對端已離開才能回收 console。",
        "回收判定用 console-list 的 consoles 數量前後比對（#155 plan 原話），非逐一比對 client_id。",
    ),
)
def f5_console_peer_gone_recycled(ctx):
    """kill 對端 tmux → console-list 應自動回收該筆 → 新開 console 仍可取得 ownership（#53 #11 回歸）。"""
    com = ctx.cfg["boards"][0]["com"]
    ses = ctx.tmux.name("f5peer")
    ctx.tmux.new(ses, f"serialwrap-minicom {com}")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        consoles_opened = _console_list(ctx, com)
        count_opened = len(consoles_opened)

        ctx.tmux.kill(ses)  # 模擬對端消失（不做正常 detach）
        time.sleep(10)

        consoles_after = _console_list(ctx, com)
        count_after = len(consoles_after)
        if count_after >= count_opened:
            return CaseResult(
                "FAIL",
                reason=f"對端消失後 console 數未減少（開啟時={count_opened}、消失後={count_after}），"
                "疑似殘留假性佔用（#53 #11 回歸）",
                category="test",
                reason_code="orphan_console_not_recycled",
            )
    finally:
        ctx.tmux.kill(ses)  # 保險：前面已 kill 過也無妨（idempotent）

    ses2 = ctx.tmux.name("f5peer2")
    ctx.tmux.new(ses2, f"serialwrap-minicom {com}")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        consoles_new = _console_list(ctx, com)
        if not _has_owner(consoles_new):
            return CaseResult(
                "FAIL",
                reason="孤兒 console 回收後，新開 console 仍拿不到 raw interactive ownership"
                "（疑似被假性佔用擋住，#53 #11 回歸）",
                category="test",
                reason_code="ownership_blocked_by_orphan",
            )
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses2)
        time.sleep(3)  # 等 router 清理 detach


@_case(
    "f5-second-console-linebuffer",
    "第二個 console 走 line-buffer、不搶 ownership，也不擋 agent 命令",
    issues=("#7", "#8"),
    requires=("tmux",),
    hints=(
        "第一個 console 已持 raw interactive ownership 期間再開第二個，第二個應停在"
        "line-buffer（interactive_owner=false），且 agent 命令走 arbiter、不受任何 console 佔用影響"
        "（不應被 SESSION_INTERACTIVE_BUSY 之類的鎖擋住）。",
    ),
)
def f5_second_console_linebuffer(ctx):
    """第一 console 持 ownership 時開第二個 → 第二個非 owner＋期間 agent 命令仍成功（#7 #8 回歸）。"""
    com = ctx.cfg["boards"][0]["com"]
    ses1 = ctx.tmux.name("f5two1")
    ses2 = ctx.tmux.name("f5two2")
    try:
        ctx.tmux.new(ses1, f"serialwrap-minicom {com}")
        time.sleep(6)  # 等第一個 console-attach＋minicom 起來
        consoles_one = _console_list(ctx, com)
        if not _has_owner(consoles_one):
            return CaseResult(
                "FAIL",
                reason="第一個 console 未拿到 raw interactive ownership，無法驗證第二個的行為",
                category="environment",
                reason_code="raw_ownership_not_granted",
            )

        ctx.tmux.new(ses2, f"serialwrap-minicom {com}")
        time.sleep(6)  # 等第二個 console-attach＋minicom 起來
        consoles_two = _console_list(ctx, com)
        if len(consoles_two) < 2:
            return CaseResult(
                "FAIL",
                reason=f"開了兩個 console，但 console-list 只見 {len(consoles_two)} 筆",
                category="test",
                reason_code="second_console_not_registered",
            )
        owners = [c for c in consoles_two if c.get("interactive_owner")]
        if len(owners) != 1:
            return CaseResult(
                "FAIL",
                reason=f"兩個 console 存在期間，interactive_owner 應恰有 1 筆為真，實際 {len(owners)} 筆"
                "（第二個疑似搶到 ownership 或第一個掉權，#7 #8 回歸）",
                category="test",
                reason_code="second_console_not_linebuffer",
            )

        res = ctx.sw.submit_and_wait(com, "echo agentok", cmd_timeout=8.0)
        ctx.note("agent-submit.json", str(res))
        if (res.get("status") or "") != "done" or "agentok" not in (res.get("stdout") or ""):
            return CaseResult(
                "FAIL",
                reason=f"雙 console 共存期間 agent 命令未正常完成（status={res.get('status')}），"
                "疑似被 console 佔用鎖卡住（#7 #8 回歸）",
                category="test",
                reason_code="agent_blocked_by_console",
            )
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses2)
        ctx.tmux.kill(ses1)
        time.sleep(3)  # 等 router 清理 detach
