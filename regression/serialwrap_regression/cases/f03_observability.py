"""F3 失敗可觀測性（#94 #16 #124）：任何失敗必須可觀測、log tail 預設取最新。"""
from __future__ import annotations

import random

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F3", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


@_case("f3-fail-error-code", "失敗 CLI 必有非空 error_code＋stderr", issues=("#94",))
def f3_fail_error_code(ctx):
    r = ctx.sw.run("session", "attach", "--selector", "NOSUCH")
    ctx.note("attach-nosuch.json", str(r))
    if r["_rc"] == 0:
        return CaseResult("FAIL", reason="不存在 selector 竟回成功", category="test", reason_code="error_not_reported")
    if not (r.get("error_code") or "").strip():
        return CaseResult("FAIL", reason="stdout JSON error_code 為空（#94 回歸）", category="test", reason_code="empty_error_code")
    if "failed" not in (r.get("_stderr") or ""):
        return CaseResult("FAIL", reason="stderr 無具體錯誤行（#94 回歸）", category="test", reason_code="empty_stderr")
    return CaseResult("PASS")


@_case("f3-cmd-fail-observable", "cmd submit 對不存在 selector 必須可觀測失敗", issues=("#94",))
def f3_cmd_fail_observable(ctx):
    r = ctx.sw.run("cmd", "submit", "--selector", "NOSUCH", "--cmd", "echo x")
    ctx.note("cmd-submit-nosuch.json", str(r))
    if r["_rc"] == 0:
        return CaseResult("FAIL", reason="對不存在 selector 提交命令竟回成功",
                          category="test", reason_code="error_not_reported")
    if not (r.get("error_code") or "").strip():
        return CaseResult("FAIL", reason="stdout JSON error_code 為空（#94 回歸）",
                          category="test", reason_code="empty_error_code")
    if "failed" not in (r.get("_stderr") or ""):
        return CaseResult("FAIL", reason="stderr 無具體錯誤行（#94 回歸）",
                          category="test", reason_code="empty_stderr")
    return CaseResult("PASS")


@_case("f3-device-error-names-selector", "不存在 selector 的錯誤輸出須點名該 selector", issues=("#16",))
def f3_device_error_names_selector(ctx):
    # 用有辨識度的字串，避免與其他常態輸出（如既有 COM 別名）撞字誤判。
    selector = "NOSUCH_DEVICE_XYZ"
    r = ctx.sw.run("session", "attach", "--selector", selector)
    ctx.note("attach-named-selector.json", str(r))
    if r["_rc"] == 0:
        return CaseResult("FAIL", reason="不存在 selector 竟回成功",
                          category="test", reason_code="error_not_reported")
    # oracle：stderr 或 stdout JSON 任一含該 selector 字串即可（#16 回歸＝兩邊都只有
    # 泛用 "not found"、看不出是哪個 selector 出錯）。
    haystack = f"{r.get('_stderr') or ''} {str(r)}"
    if selector not in haystack:
        return CaseResult("FAIL", reason=f"錯誤輸出（stderr＋JSON）未點名 selector={selector}（#16 回歸）",
                          category="test", reason_code="selector_not_named")
    return CaseResult("PASS")


@_case("f3-log-tail-latest", "log tail-text 預設參數需含最新一段（非從最舊 seq 起算）", issues=("#124",))
def f3_log_tail_latest(ctx):
    com = ctx.cfg["boards"][0]["com"]
    marker = f"MARKER_{random.randint(100000, 999999)}"
    cmd = ctx.sw.submit_and_wait(com, f"echo {marker}")
    ctx.note("submit.json", str(cmd))
    if (cmd.get("status") or "") != "done" or marker not in (cmd.get("stdout") or ""):
        return CaseResult("FAIL", reason=f"submit echo 未正常完成（status={cmd.get('status')}），無法驗證 log tail-text 行為",
                          category="environment", reason_code="submit_precondition_failed")
    tail = ctx.sw.run("log", "tail-text", "--selector", "COM0")
    ctx.note("tail-text.json", str(tail))
    lines = tail.get("lines") or []
    if not any(marker in line for line in lines):
        return CaseResult("FAIL",
                          reason=f"log tail-text 預設輸出未見新 marker（{marker}），疑似從最舊 seq 起算漏掉新段（#124 回歸）",
                          category="test", reason_code="tail_not_latest")
    return CaseResult("PASS")
