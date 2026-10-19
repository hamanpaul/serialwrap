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
from types import SimpleNamespace
from typing import Any

from realhw.drivers import strip_ansi
from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False, pending_issue=False):
    def deco(fn):
        register(Case(id=id, family="F5", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints),
                      pending_issue=pending_issue))
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


# ---- Stage 0 BC-02：reboot 後不回放舊按鍵（pending_issue，見 docs/regression-plugin.md）----
_STALE_MARK = "STALE"


def evaluate_stale_replay(ctx: Any) -> CaseResult:
    """BC-02 oracle：上下文失效（reboot）後，agent 命令期間 human 鍵入的舊按鍵不得回放。

    ctx 欄位：
    - ``test_capture`` / ``control_capture``：含 scrollback 的 console 擷取（測試臂＝reboot、對照組＝未 reboot）；
    - 可選 ``test_wal_stale`` / ``control_wal_stale``：WAL 中含 STALE 的 TX 紀錄筆數（None＝未取得證據）；
    - 可選 ``test_in_flight`` / ``control_in_flight``：送出按鍵當下 agent 命令仍在途（False＝證據不足）；
    - 可選 ``test_incomplete`` / ``control_incomplete``：擷取或 WAL 有缺口（True＝不得 PASS）；
    - 可選 ``reboot_proven``：boot ID 是否確實改變（False＝reboot 未被證明，不得 PASS）。

    判定：測試臂（reboot）出現 STALE（畫面或 WAL）→ FAIL（舊 owner_gen 的 deferred 按鍵被回放，BC-02 回歸）；
    對照組未見 STALE → SKIP ``insufficient_evidence``（無法區分「丟棄」與「未回放」）；
    在途未確認、擷取不完整、reboot 未被證明 → SKIP；其餘 → PASS。
    """
    test_cap = strip_ansi(str(getattr(ctx, "test_capture", "") or ""))
    ctrl_cap = strip_ansi(str(getattr(ctx, "control_capture", "") or ""))
    test_wal = getattr(ctx, "test_wal_stale", None)
    ctrl_wal = getattr(ctx, "control_wal_stale", None)
    if _STALE_MARK in test_cap or bool(test_wal):
        return CaseResult(
            "FAIL",
            reason="reboot 後 console（畫面或 WAL TX）出現 STALE 回放，舊 owner_gen 的 deferred 按鍵未被丟棄（BC-02 回歸）",
            category="test", reason_code="stale_replayed_after_reboot",
        )
    if getattr(ctx, "test_in_flight", True) is False or getattr(ctx, "control_in_flight", True) is False:
        return CaseResult("SKIP", reason="鍵入按鍵時 agent 命令已非在途，無法驗證 deferred 行為",
                          category="environment", reason_code="in_flight_not_confirmed")
    if getattr(ctx, "test_incomplete", False) or getattr(ctx, "control_incomplete", False):
        return CaseResult("SKIP", reason="console 擷取或 WAL 證據有缺口（scrollback 可能被截斷），不判 PASS",
                          category="environment", reason_code="capture_incomplete")
    if getattr(ctx, "reboot_proven", True) is False:
        return CaseResult("SKIP", reason="boot ID 未改變，reboot 未被證明，不判 PASS",
                          category="environment", reason_code="reboot_unproven")
    if _STALE_MARK not in ctrl_cap and not bool(ctrl_wal):
        return CaseResult(
            "SKIP",
            reason="對照組（未 reboot）也未見 STALE 回放，無法區分「已丟棄」與「未回放」，不判 PASS",
            category="environment", reason_code="insufficient_evidence",
        )
    return CaseResult("PASS", reason="reboot 臂未回放 STALE（畫面與 WAL），對照組（未 reboot）正常回放，BC-02 語意成立")


def _open_wal_cursor(ctx: Any):
    """以 daemon status 的 wal_path 開游標（與 F12 相同路徑）；取不到回 None。"""
    from realhw import wal_oracle

    status = ctx.sw.run("daemon", "status")
    path = status.get("wal_path")
    if not (isinstance(path, str) and path.strip()):
        return None
    try:
        return wal_oracle.open_cursor(path)
    except OSError:
        return None


def _wal_stale_tx(cursor: Any) -> tuple[int, bool]:
    """自游標掃描新增 WAL，回傳 (含 STALE 的 TX 筆數, 掃描是否有問題)。"""
    import base64

    from realhw import wal_oracle

    records, _raw_lines, problems = wal_oracle.scan_new_records(cursor)
    hits = 0
    for rec in records:
        if rec.get("dir") != "TX":
            continue
        try:
            payload = base64.b64decode(rec.get("payload_b64") or "")
        except (ValueError, TypeError):
            continue
        if _STALE_MARK.encode("ascii") in payload:
            hits += 1
    return hits, bool(problems)


def _boot_id_matches(ctx: Any, com: str, before: str) -> tuple[bool, str]:
    from .f09_brcm_reboot_login import _read_boot_id

    current, detail = _read_boot_id(ctx, com)
    return bool(current and current != before), detail


def _stale_arm(ctx: Any, com: str, *, reboot: bool, boot_wait_s: float) -> dict[str, Any] | CaseResult:
    """單臂：agent 命令在途時 human 鍵入 ``echo STALE``；reboot 臂的在途命令為 ``sleep 5; sync; reboot``。

    回傳證據 dict（成功）或 CaseResult（前置條件不滿足）。證據含含 scrollback 的畫面、WAL 中 STALE TX 筆數、
    在途確認、擷取是否截斷、reboot 是否被 boot ID 證明。
    """
    from .f09_boot_uboot import _wait_quiet_cleared

    tag = "rb" if reboot else "ctl"
    ses = ctx.tmux.name(f"f5stale{tag}")
    ctx.tmux.new(ses, f"serialwrap-minicom {com}")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        if not _has_owner(_console_list(ctx, com)):
            return CaseResult(
                "FAIL",
                reason=f"{tag} 臂 console 未拿到 raw interactive ownership，無法驗證 deferred 行為",
                category="environment", reason_code="raw_ownership_not_granted",
            )
        boot_before = None
        if reboot:
            from .f09_brcm_reboot_login import _read_boot_id

            boot_before, boot_detail = _read_boot_id(ctx, com)
            if not boot_before:
                return CaseResult("SKIP", reason=f"reboot 前無法取得 boot ID（{boot_detail}），reboot 無法被證明",
                                  category="environment", reason_code="boot_id_unavailable")
        cursor = _open_wal_cursor(ctx)
        if cursor is None:
            return CaseResult("SKIP", reason="無法取得 daemon wal_path 游標，WAL 證據不可用",
                              category="environment", reason_code="wal_unavailable")
        cmd = "sleep 5; sync; reboot" if reboot else "sleep 5"
        sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", cmd, "--cmd-timeout", "8")
        ctx.note(f"stale-{tag}-submit.json", str(sub))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return CaseResult("FAIL", reason=f"{tag} 臂 submit 未回 cmd_id（回應={sub}）",
                              category="environment", reason_code="submit_no_cmd_id")
        time.sleep(0.5)  # 讓 daemon 進入在途命令、suspend_interactive() 已生效
        ctx.tmux.send(ses, f"echo {_STALE_MARK}", enter=True)  # 在途期間鍵入，應進 deferred buffer
        # 按鍵送出後再確認命令仍在途（狀態不可讀或已終態 → in_flight 為假，呼叫端判證據不足）。
        cmd_status = (ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {}).get("status")
        in_flight = cmd_status is not None and cmd_status not in ("done", "error", "timeout")
        boot_changed: bool | None = None
        if reboot:
            deadline = time.monotonic() + boot_wait_s
            boot_changed = False
            while time.monotonic() < deadline:
                time.sleep(3)
                changed, _detail = _boot_id_matches(ctx, com, boot_before)
                if changed:
                    boot_changed = True
                    break
            if boot_changed:
                cleared, _last = _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)
                if not cleared:
                    return CaseResult("SKIP", reason="reboot 後 boot quiet 未在 boot_wait_s 內清空，無法判定回放",
                                      category="environment", reason_code="boot_not_ready")
            time.sleep(3)
        else:
            _await_terminal(ctx, cmd_id, timeout_s=15.0)
            time.sleep(2)  # 等 resume_interactive() flush 完成
        pane, truncated = ctx.tmux.capture_history(ses)
        ctx.note(f"stale-{tag}-pane.txt", pane)
        wal_hits, wal_problems = _wal_stale_tx(cursor)
        ctx.note(f"stale-{tag}-wal.json", str({"stale_tx": wal_hits, "problems": wal_problems}))
        return {
            "pane": pane,
            "capture_truncated": truncated,
            "wal_stale_tx": wal_hits,
            "wal_incomplete": wal_problems,
            "in_flight": in_flight,
            "boot_changed": boot_changed,
        }
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)  # 等 router 清理 detach


@_case(
    "f5-deferred-dropped-after-reboot",
    "agent 命令在途時 human 鍵入的舊按鍵，於 reboot（上下文失效）後不回放；未 reboot 之對照組照常回放",
    issues=(),
    pending_issue=True,
    requires=("tmux",),
    destructive=True,
    hints=(
        "對照組先跑：`sleep 5` 在途期間鍵入 echo STALE，命令結束 resume 時應回放（scrollback 與 WAL TX 見 STALE）。",
        "測試臂：`sleep 5; sync; reboot` 在途期間鍵入 echo STALE；以 boot ID 證明 reboot 後 boot quiet 清空，deferred 須丟棄。",
        "擷取以 scrollback 為準（`tmux capture-pane -S -`）；history 已滿或 WAL 掃描有問題一律 SKIP，不判 PASS。",
        "reboot 為板端動作，case 以 destructive=True 執行（同 F9 reboot 類）；結束前由 boot quiet 清空與 READY 確認。",
        "issues 欄待擁有者為 Stage 0 開立 issue 號後補上（不得填假號）。",
    ),
)
def f5_deferred_dropped_after_reboot(ctx: Any) -> CaseResult:
    """BC-02 實機 oracle：reboot 臂的畫面與 WAL 不得出現 STALE；對照組（未 reboot）須出現 STALE 才能判 PASS。"""
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    control = _stale_arm(ctx, com, reboot=False, boot_wait_s=boot_wait_s)
    if isinstance(control, CaseResult):
        return control
    if control["capture_truncated"]:
        return CaseResult("SKIP", reason="對照組 scrollback 可能被截斷，不判定", category="environment",
                          reason_code="capture_incomplete")
    test = _stale_arm(ctx, com, reboot=True, boot_wait_s=boot_wait_s)
    if isinstance(test, CaseResult):
        return test
    return evaluate_stale_replay(SimpleNamespace(
        test_capture=test["pane"], control_capture=control["pane"],
        test_wal_stale=test["wal_stale_tx"], control_wal_stale=control["wal_stale_tx"],
        test_in_flight=test["in_flight"], control_in_flight=control["in_flight"],
        test_incomplete=bool(test["capture_truncated"] or test["wal_incomplete"]),
        control_incomplete=bool(control["capture_truncated"] or control["wal_incomplete"]),
        reboot_proven=test["boot_changed"] is True,
    ))
