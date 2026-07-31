"""F1 命令契約（#23 #27 #129 #19 #15）：limits 可查、超限拒收不卡死、cmd_id 不消失。

驗證面（實查依據見 PR note）：
- ``daemon status`` 回應頂層 ``limits.max_submit_cmd_bytes`` / ``limits.warn_submit_cmd_bytes``
  （見 ``sw_core/service.py`` ``health()``，#129）。
- ``cmd submit`` 對超過硬上限（``CMD_REJECT_BYTES``=16384）或含換行的命令，於
  ``CommandArbiter.submit()`` 直接回拒，``error_code`` 落在 CLI 印出的頂層 JSON（``sw_core/cli.py``
  ``_run_rpc`` 直接印 RPC 回應，不巢狀包裝，#23 #27）。
- ``cmd status --cmd-id`` 對已知 cmd_id 一律回 ``{"ok": true, "command": {...}}``；只有真的
  查無該 cmd_id 才回頂層 ``error_code == "CMD_NOT_FOUND"``（``sw_core/arbiter.py`` ``get()``，#15）。
  命令逾時（PROMPT_TIMEOUT 或其 CTRL_C/CTRL_D 復原路徑）後，worker 會把該筆記錄標為終態
  （``status`` ∈ {``done``, ``error``}；historically 亦可能為 ``timeout``），但記錄本身不會消失。
"""
from __future__ import annotations

import time

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F1", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


@_case("f1-limits-queryable", "daemon status 可執行期查詢命令長度上限", issues=("#129", "#27"))
def f1_limits_queryable(ctx):
    """#129：limits 欄位須可查詢，client 不必硬編碼上限值。"""
    st = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status.json", str(st))
    limits = st.get("limits") or {}
    max_bytes = limits.get("max_submit_cmd_bytes")
    warn_bytes = limits.get("warn_submit_cmd_bytes")
    if not (isinstance(max_bytes, int) and max_bytes > 0):
        return CaseResult("FAIL", reason=f"limits.max_submit_cmd_bytes 非正整數：{max_bytes!r}（#129 回歸）",
                          category="test", reason_code="limits_max_missing")
    if not (isinstance(warn_bytes, int) and warn_bytes > 0):
        return CaseResult("FAIL", reason=f"limits.warn_submit_cmd_bytes 非正整數：{warn_bytes!r}（#129 回歸）",
                          category="test", reason_code="limits_warn_missing")
    return CaseResult("PASS")


@_case("f1-too-long-rejected", "超過硬上限的命令直接拒收，且不拖累後續命令", issues=("#23", "#27"))
def f1_too_long_rejected(ctx):
    """#23：超長命令曾造成 daemon 端行為異常（非乾淨拒收）；本 case 驗證拒收後 session 未卡死。"""
    com = ctx.cfg["boards"][0]["com"]
    limits = ctx.sw.run("daemon", "status").get("limits") or {}
    max_bytes = int(limits.get("max_submit_cmd_bytes") or 16384)
    prefix = "echo "
    pad_len = max_bytes + 100 - len(prefix.encode("utf-8"))
    cmd = prefix + ("a" * pad_len)
    r = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", cmd, "--cmd-timeout", "5")
    ctx.note("too-long-submit.json", str(r))
    if r.get("error_code") != "CMD_TOO_LONG":
        return CaseResult("FAIL",
                          reason=f"超長命令（{len(cmd.encode('utf-8'))} bytes > max={max_bytes}）未回 "
                                 f"CMD_TOO_LONG（實得 error_code={r.get('error_code')!r}，#23 回歸）",
                          category="test", reason_code="cmd_too_long_not_rejected")
    follow = ctx.sw.submit_and_wait(com, "echo ok")
    ctx.note("followup-echo-ok.json", str(follow))
    if "ok" not in (follow.get("stdout") or ""):
        return CaseResult("FAIL",
                          reason=f"超長命令拒收後 session 疑似卡死：後續 echo ok 未成功（status="
                                 f"{follow.get('status')!r}，#23 回歸）",
                          category="test", reason_code="session_stuck_after_reject")
    return CaseResult("PASS")


@_case("f1-newline-rejected", "含換行字元的命令直接拒收", issues=("#27",))
def f1_newline_rejected(ctx):
    """#27：``--cmd`` 內嵌 ``\\n`` 須被 admission control 擋下，不得穿透到 UART。"""
    com = ctx.cfg["boards"][0]["com"]
    cmd = "echo a\necho b"
    r = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", cmd, "--cmd-timeout", "5")
    ctx.note("newline-submit.json", str(r))
    if r.get("error_code") != "CMD_CONTAINS_NEWLINE":
        return CaseResult("FAIL",
                          reason=f"含換行命令未回 CMD_CONTAINS_NEWLINE（實得 error_code="
                                 f"{r.get('error_code')!r}，#27 回歸）",
                          category="test", reason_code="cmd_newline_not_rejected")
    return CaseResult("PASS")


@_case("f1-near-limit-no-logout", "接近 warn 門檻的合法長命令不致 session 登出", issues=("#19",))
def f1_near_limit_no_logout(ctx):
    """#19：長度落在 warn（非拒收）區間的合法命令，曾回歸性造成 target 端登出。"""
    com = ctx.cfg["boards"][0]["com"]
    limits = ctx.sw.run("daemon", "status").get("limits") or {}
    warn_bytes = int(limits.get("warn_submit_cmd_bytes") or 4096)
    max_bytes = int(limits.get("max_submit_cmd_bytes") or 16384)
    prefix = "echo "
    # 越過 warn 門檻但遠低於硬上限，確保命令合法（不觸發 CMD_TOO_LONG）。
    target_len = min(warn_bytes + 200, max_bytes - 200)
    pad_len = max(target_len - len(prefix.encode("utf-8")), 1)
    cmd = prefix + ("a" * pad_len)
    result = ctx.sw.submit_and_wait(com, cmd, cmd_timeout=20.0)
    ctx.note("near-limit-cmd.json", str(result))
    if result.get("status") != "done":
        return CaseResult("FAIL",
                          reason=f"接近 warn 門檻（{len(cmd.encode('utf-8'))} bytes）的合法 echo 未成功完成"
                                 f"（status={result.get('status')!r}，#19 回歸）",
                          category="test", reason_code="near_limit_cmd_failed")
    sess = ctx.sw.session(com)
    ctx.note("session-after.json", str(sess))
    if sess.get("state") != "READY":
        return CaseResult("FAIL",
                          reason=f"長命令後 session 非 READY（{sess.get('state')!r}），疑似登出（#19 回歸）",
                          category="test", reason_code="session_logged_out")
    followup = ctx.sw.submit_and_wait(com, "echo alive")
    ctx.note("followup-alive.json", str(followup))
    if "alive" not in (followup.get("stdout") or ""):
        return CaseResult("FAIL",
                          reason=f"長命令後續 echo alive 未成功（status={followup.get('status')!r}，"
                                 f"疑似登出殘留影響，#19 回歸）",
                          category="test", reason_code="followup_after_long_cmd_failed")
    return CaseResult("PASS")


@_case("f1-cmdid-survives-timeout", "命令逾時後 cmd_id 仍可查詢，不回 CMD_NOT_FOUND", issues=("#15",))
def f1_cmdid_survives_timeout(ctx):
    """#15：命令逾時（PROMPT_TIMEOUT 及其 CTRL_C/CTRL_D 復原路徑）後，cmd_id 記錄曾一度消失。"""
    com = ctx.cfg["boards"][0]["com"]
    cmd_timeout = 3.0
    sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sleep 20",
                     "--cmd-timeout", str(cmd_timeout))
    ctx.note("sleep-submit.json", str(sub))
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return CaseResult("FAIL", reason=f"submit 未回 cmd_id（{sub!r}）",
                          category="test", reason_code="submit_no_cmd_id")
    # daemon 內部逾時復原會嘗試 CTRL_C／CTRL_D（各最多等 2s）才落終態，給足緩衝再輪詢。
    deadline = time.monotonic() + cmd_timeout + 30.0
    status = None
    st: dict = {}
    while time.monotonic() < deadline:
        st = ctx.sw.run("cmd", "status", "--cmd-id", cmd_id)
        if st.get("error_code") == "CMD_NOT_FOUND":
            break
        command = st.get("command") or {}
        status = command.get("status")
        if status in ("timeout", "error", "done"):
            break
        time.sleep(1.0)
    ctx.note("status-after-timeout.json", str(st))
    if st.get("error_code") == "CMD_NOT_FOUND":
        return CaseResult("FAIL", reason="逾時後 cmd_id 查無此命令（CMD_NOT_FOUND，#15 回歸：cmd_id 消失）",
                          category="test", reason_code="cmdid_vanished")
    if status not in ("timeout", "error", "done"):
        return CaseResult("FAIL", reason=f"逾時後未見預期終態（實得 status={status!r}，{st!r}）",
                          category="test", reason_code="unexpected_status_after_timeout")
    return CaseResult("PASS")
