"""F4 session 狀態與命令語義（#34 #26 #28）。

實查依據（見 PR note）：
- ``serialwrap session activity --selector <sel>`` 回應 ``session.activity_classification``；
  合法值集合取自 ``SessionRuntime.classify_activity()``（``sw_core/session_manager.py``）：
  ``active``/``idle-healthy``/``quiet-suspicious``/``newly-attached``/``offline``。
- ``serialwrap cmd submit --mode background`` 對應 RPC ``command.submit``（``mode=background``）；
  ``serialwrap cmd result-tail --cmd-id <id> [--from-chunk N] [--limit N]`` 對應
  ``command.result_tail``，回應頂層含 ``status``（``active``/``done``/``error``）、
  ``chunks``（``list[str]``，非巢狀 dict）與 ``next_chunk``（下次應帶入的 ``from_chunk``）。
- ``serialwrap session interactive-open --selector <sel> [--owner] [--timeout] [--command]
  [--allow-attached]``、``interactive-send --interactive-id --data [--encoding]``、
  ``interactive-close --interactive-id``；成功回應含 ``interactive_id``；lease 期間 line
  命令衝突已知錯誤碼含 ``SESSION_INTERACTIVE_BUSY``。
"""
from __future__ import annotations

import re
import time

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F4", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


# session activity 分類欄位的合法值集合（#34；取自 classify_activity() 的 5 個回傳值）。
# quiet-suspicious／idle-healthy 屬安靜態下的正常分類，亦視為「可區分」而非回歸。
_ACTIVITY_VALUES = frozenset({"active", "idle-healthy", "quiet-suspicious", "newly-attached", "offline"})

# background 命令輸出的可數 marker：L1..L50。用貪婪 \d+ 避免子字串誤判
# （例如 "L1" 是 "L10".."L19" 的字首，但 re 的 \d+ 會貪婪吃滿數字，不會拆出 "L1"）。
_LMARKER = re.compile(r"L(\d+)")


@_case("f4-activity-classification",
       "session activity 分類欄位須存在且為合法值（安靜／活動皆可區分）", issues=("#34",))
def f4_activity_classification(ctx):
    """#34：session activity 的分類欄位曾缺失或恆為空值。

    兩板安靜時先驗欄位存在＋合法；COM0 送一次命令後複驗欄位仍存在＋合法（不硬性要求特定
    值變化——安靜態下 quiet-* 類值本身即屬合法可區分結果，見上方合法值集合）。
    """
    for board in ctx.cfg["boards"]:
        com = board["com"]
        r = ctx.sw.run("session", "activity", "--selector", com)
        ctx.note(f"{com}-activity-quiet.json", str(r))
        session = r.get("session") or {}
        cls = session.get("activity_classification")
        if not (isinstance(cls, str) and cls.strip()):
            return CaseResult(
                "FAIL",
                reason=f"{com} session activity 缺 activity_classification 或為空（{r!r}，#34 回歸）",
                category="test", reason_code="activity_not_classified")
        if cls not in _ACTIVITY_VALUES:
            return CaseResult(
                "FAIL",
                reason=f"{com} activity_classification={cls!r} 不在已知合法值集合 {sorted(_ACTIVITY_VALUES)}（#34 回歸）",
                category="test", reason_code="activity_not_classified")

    # 只對第一塊板送命令複驗（第二塊板只查不動，依 #155 規格）。
    com = ctx.cfg["boards"][0]["com"]
    cmd = ctx.sw.submit_and_wait(com, "echo act")
    ctx.note("submit-echo-act.json", str(cmd))
    if (cmd.get("status") or "") != "done":
        return CaseResult(
            "FAIL",
            reason=f"echo act 未正常完成（status={cmd.get('status')!r}），無法驗證命令後分類",
            category="environment", reason_code="submit_precondition_failed")

    r2 = ctx.sw.run("session", "activity", "--selector", com)
    ctx.note("com0-activity-after-cmd.json", str(r2))
    session2 = r2.get("session") or {}
    cls2 = session2.get("activity_classification")
    if not (isinstance(cls2, str) and cls2.strip()):
        return CaseResult(
            "FAIL",
            reason=f"COM0 命令後 session activity 缺 activity_classification 或為空（{r2!r}，#34 回歸）",
            category="test", reason_code="activity_not_classified")
    if cls2 not in _ACTIVITY_VALUES:
        return CaseResult(
            "FAIL",
            reason=f"COM0 命令後 activity_classification={cls2!r} 不在已知合法值集合 {sorted(_ACTIVITY_VALUES)}（#34 回歸）",
            category="test", reason_code="activity_not_classified")
    return CaseResult("PASS")


@_case("f4-background-result-tail-consistent",
       "background 命令的 result-tail 增量拼接須不漏不重", issues=("#28",))
def f4_background_result_tail_consistent(ctx):
    """#28：background 模式輸出經 result-tail 增量讀取，曾發生 chunk 邊界漏段或重複段。

    以 50 個可數 marker（L1..L50）驗證：迴圈用 ``next_chunk`` 當下次 ``--from-chunk``，
    收到終態（``status`` ∈ {done, error}）且該次 ``chunks`` 已空才停止；拼接後每個 marker
    必須恰出現一次。
    """
    com = "COM0"
    cmd_text = "for i in $(seq 1 50); do echo L$i; done"
    sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", cmd_text,
                     "--mode", "background", "--cmd-timeout", "30")
    ctx.note("bg-submit.json", str(sub))
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return CaseResult(
            "FAIL", reason=f"background submit 未回 cmd_id（{sub!r}）",
            category="environment", reason_code="submit_precondition_failed")

    from_chunk = 0
    parts: list[str] = []
    status: str | None = None
    last_resp: dict = {}
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        r = ctx.sw.run("cmd", "result-tail", "--cmd-id", cmd_id, "--from-chunk", str(from_chunk))
        last_resp = r
        chunks = r.get("chunks") or []
        parts.extend(str(c) for c in chunks)
        from_chunk = int(r.get("next_chunk") or (from_chunk + len(chunks)))
        status = r.get("status")
        if status in ("done", "error") and not chunks:
            break
        time.sleep(0.5)
    ctx.note("result-tail-last.json", str(last_resp))
    combined = "".join(parts)
    ctx.note("combined-output.txt", combined)

    if status not in ("done", "error"):
        return CaseResult(
            "FAIL", reason=f"background 命令未在時限內到達終態（實得 status={status!r}）",
            category="environment", reason_code="background_result_tail_timeout")

    counts: dict[int, int] = {}
    for m in _LMARKER.finditer(combined):
        n = int(m.group(1))
        if 1 <= n <= 50:
            counts[n] = counts.get(n, 0) + 1

    missing = [i for i in range(1, 51) if counts.get(i, 0) == 0]
    if missing:
        shown = missing[:10]
        return CaseResult(
            "FAIL",
            reason=f"拼接輸出缺少 marker：{shown}{'...' if len(missing) > 10 else ''}（共缺 {len(missing)} 個，#28 回歸：chunk 漏段）",
            category="test", reason_code="result_tail_dropped_chunk")

    duplicated = [i for i in range(1, 51) if counts.get(i, 0) > 1]
    if duplicated:
        shown = duplicated[:10]
        return CaseResult(
            "FAIL",
            reason=f"拼接輸出重複 marker：{shown}{'...' if len(duplicated) > 10 else ''}（共重複 {len(duplicated)} 個，#28 回歸：chunk 重複段）",
            category="test", reason_code="result_tail_duplicated_chunk")
    return CaseResult("PASS")


@_case("f4-interactive-line-cmd-defined",
       "interactive lease 期間 line 命令的終態語意須明確（拒絕或排隊完成，不得懸空逾時）",
       issues=("#26",))
def f4_interactive_line_cmd_defined(ctx):
    """#26：interactive lease 開啟期間送 line 模式命令，過去曾出現「被接受卻永遠等不到
    prompt」（最終 PROMPT_TIMEOUT）的未定義行為。合法終態僅二擇一：

    (a) 明確拒絕（非空 ``error_code``，如 ``HUMAN_INTERACTIVE_ACTIVE``／``SESSION_INTERACTIVE_BUSY``）；
    (b) 被接受（拿到 ``cmd_id``）並排隊後正常到達 ``done``/``error`` 終態。

    不合法（＝#26 回歸）：被接受但最終 ``PROMPT_TIMEOUT``／``timeout`` 終態。

    收尾一律 ``interactive-close``（finally）＋確認 session 回 READY；若前段判定 PASS 但收
    尾未回 READY，覆蓋為 FAIL（lease 收尾異常本身就是需要被抓到的回歸面）。
    """
    com = "COM0"
    interactive_id = ""
    result: CaseResult | None = None
    try:
        opened = ctx.sw.run("session", "interactive-open", "--selector", com,
                            "--owner", "agent:f4-regression", "--timeout", "20")
        ctx.note("interactive-open.json", str(opened))
        interactive_id = str(opened.get("interactive_id") or "")
        if not opened.get("ok") or not interactive_id:
            result = CaseResult(
                "FAIL",
                reason=f"interactive-open 未成功，無法驗證 lease 期間 line 命令語意（{opened!r}）",
                category="environment", reason_code="interactive_open_failed")
            return result

        sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "echo during_lease",
                         "--cmd-timeout", "10")
        ctx.note("submit-during-lease.json", str(sub))
        error_code = str(sub.get("error_code") or "").strip()
        if error_code:
            # (a) 明確拒絕——合法路徑，不必追終態。
            result = CaseResult("PASS")
            return result

        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            # 既未拒絕（無 error_code）也未接受（無 cmd_id）＝行為未定義，等同 #26 回歸。
            result = CaseResult(
                "FAIL",
                reason=f"submit 既未回 error_code 也未回 cmd_id，行為未定義（{sub!r}，#26 回歸）",
                category="test", reason_code="accepted_then_prompt_timeout")
            return result

        # (b) 被接受——追到終態；容許排隊到 lease 到期（20s）後才被 flush 執行，故給足緩衝。
        status: str | None = None
        st: dict = {}
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            st = ctx.sw.run("cmd", "status", "--cmd-id", cmd_id)
            command = st.get("command") or {}
            status = command.get("status")
            if status in ("done", "error", "timeout"):
                break
            time.sleep(1.0)
        ctx.note("status-after-lease-submit.json", str(st))
        command = st.get("command") or {}
        cmd_error = str(command.get("error_code") or "").upper()
        if status == "timeout" or "PROMPT_TIMEOUT" in cmd_error:
            result = CaseResult(
                "FAIL",
                reason=f"lease 期間被接受的 line 命令（cmd_id={cmd_id}）最終逾時"
                       f"（status={status!r}, error_code={cmd_error!r}，#26 回歸）",
                category="test", reason_code="accepted_then_prompt_timeout")
            return result
        if status not in ("done", "error"):
            result = CaseResult(
                "FAIL",
                reason=f"lease 期間被接受的 line 命令（cmd_id={cmd_id}）未在時限內到達終態"
                       f"（status={status!r}），視同懸空逾時（#26 回歸）",
                category="test", reason_code="accepted_then_prompt_timeout")
            return result
        result = CaseResult("PASS")
        return result
    finally:
        if interactive_id:
            closed = ctx.sw.run("session", "interactive-close", "--interactive-id", interactive_id)
            ctx.note("interactive-close.json", str(closed))
        ready_timeout_s = float(ctx.cfg["timeouts"]["ready_wait_s"])
        ready = ctx.sw.wait_state(com, "READY", timeout_s=ready_timeout_s)
        ctx.note("post-close-session.json", str(ctx.sw.session(com)))
        if result is not None and result.verdict == "PASS" and not ready:
            result.verdict = "FAIL"
            result.category = "test"
            result.reason_code = "session_not_ready_after_close"
            result.reason = f"{com} interactive-close 後於 {ready_timeout_s:.0f}s 內未回 READY"
