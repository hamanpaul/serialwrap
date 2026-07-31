"""F6 RPC 不凍結（#80 #52）：長操作不得凍結全 daemon、雙板互不餓死。"""
from __future__ import annotations

import threading
import time
from typing import Any

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F6", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


def _await_terminal(ctx: Any, cmd_id: Any, *, timeout_s: float, poll_s: float = 0.5) -> dict:
    """輪詢 ``cmd status`` 到終態（done/error/timeout）；逾時回傳最後一次讀值（不 raise）。

    供兩個 case 收尾用：確保長操作命令不會以 pending 狀態殘留、拖累後續 case。
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


@_case("f6-ping-during-long-op", "長操作執行中 daemon RPC 仍即時回應（不被凍結）",
       issues=("#80",))
def f6_ping_during_long_op(ctx: Any) -> CaseResult:
    """對 COM0 送 8 秒長命令、期間每 0.5s 打 ``daemon status`` 量測往返耗時。

    oracle（#80）：daemon RPC event loop 不得被單一 session 的長操作凍結——
    往返耗時應恆 <2s；任何一次 ≥2s 視為回歸（daemon 被長操作卡住）。
    """
    com = ctx.cfg["boards"][0]["com"]
    sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sleep 8",
                     "--cmd-timeout", "15")
    ctx.note("submit.json", str(sub))
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return CaseResult("FAIL", reason=f"submit 未回 cmd_id（無法量測，回應={sub}）",
                          category="test", reason_code="submit_no_cmd_id")

    latencies: list[float] = []
    worst = 0.0
    # 覆蓋整段 sleep 8 執行窗＋緩衝，確保凍結若只發生在長操作尾聲也量得到。
    deadline = time.monotonic() + 9.0
    try:
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            ctx.sw.run("daemon", "status")
            elapsed = time.monotonic() - t0
            latencies.append(elapsed)
            worst = max(worst, elapsed)
            time.sleep(0.5)
    finally:
        # 收尾：不論量測結果為何，都要等該命令收斂到終態，避免殘留 pending 命令
        # 拖累後續 case（session 佇列/仲裁狀態）。
        terminal = _await_terminal(ctx, cmd_id, timeout_s=15.0)
        ctx.note("terminal.json", str(terminal))

    latencies_path = ctx.note("latencies.json", str(latencies))
    if worst >= 2.0:
        return CaseResult(
            "FAIL",
            reason=f"長操作期間 daemon status 往返最大 {worst:.2f}s（≥2s，RPC 疑遭凍結，#80 回歸）",
            category="test", reason_code="rpc_frozen_during_long_op",
            evidence={"latencies": latencies_path},
        )
    return CaseResult(
        "PASS",
        reason=f"{len(latencies)} 次 daemon status 往返最大 {worst:.2f}s（<2s）",
        evidence={"latencies": latencies_path},
    )


@_case("f6-two-boards-no-starvation", "COM0 長操作不得餓死 COM1 命令（雙板互不干擾）",
       issues=("#80", "#52"))
def f6_two_boards_no_starvation(ctx: Any) -> CaseResult:
    """執行緒 A 對 COM0 送 5 秒長命令；主執行緒延遲 1s 後對 COM1 送短命令。

    oracle（#80 #52）：每個 session 各有獨立 worker thread／queue，COM0 的長操作
    不得阻塞 COM1 的命令——COM1 的短命令必須在 COM0 的 sleep 5 結束**之前**完成，
    否則視為 cross-session starvation 回歸。
    """
    boards = ctx.cfg.get("boards") or []
    if len(boards) < 2:
        return CaseResult("SKIP", reason="bench 僅單板，無法驗證雙板互不餓死",
                          category="environment", reason_code="single_board_bench")
    com_a, com_b = boards[0]["com"], boards[1]["com"]

    errors: list[str] = []
    state: dict[str, Any] = {}

    def _thread_a() -> None:
        # 執行緒內例外務必自行捕捉並記錄——絕不可讓執行緒靜默死掉，否則主執行緒
        # 誤判「A 沒送出長命令」而放行 PASS。
        try:
            state["a_start"] = time.monotonic()
            sub = ctx.sw.run("cmd", "submit", "--selector", com_a, "--cmd", "sleep 5",
                             "--cmd-timeout", "10")
            state["a_submit"] = sub
            cmd_id = sub.get("cmd_id")
            if not cmd_id:
                errors.append(f"thread-A：submit 未回 cmd_id（回應={sub}）")
            state["a_cmd_id"] = cmd_id
        except Exception as exc:  # noqa: BLE001 —— 明確記錄後由主執行緒轉譯為 FAIL，不吞例外
            errors.append(f"thread-A 未捕捉例外：{exc!r}")

    thread_a = threading.Thread(target=_thread_a, name="f6-com-a")
    thread_a.start()
    time.sleep(1.0)  # 讓 A 的長命令先進佇列／開始執行，再送 B（照 plan 時序）

    b_start = time.monotonic()
    try:
        b_result = ctx.sw.submit_and_wait(com_b, "echo fast", cmd_timeout=8.0)
    except Exception as exc:  # noqa: BLE001 —— 同樣明確記錄，不吞例外
        errors.append(f"main（COM1 submit_and_wait）未捕捉例外：{exc!r}")
        b_result = {}
    b_done = time.monotonic()

    thread_a.join(timeout=15.0)
    if thread_a.is_alive():
        errors.append("thread-A 逾時未結束（join 15s 未回）")

    # 收尾：確認兩板命令都到終態（A 用輪詢；B 的 submit_and_wait 已內含輪詢至終態）。
    a_terminal = _await_terminal(ctx, state.get("a_cmd_id"), timeout_s=15.0)

    a_start = state.get("a_start", b_start)
    a_deadline = a_start + 5.0
    timeline = {
        "a_start": a_start, "a_deadline": a_deadline, "a_terminal": a_terminal,
        "b_start": b_start, "b_done": b_done, "b_elapsed": b_done - b_start,
        "b_result": b_result, "errors": errors,
    }
    timeline_path = ctx.note("timeline.json", str(timeline))

    if errors:
        return CaseResult("FAIL", reason="；".join(errors), category="test",
                          reason_code="thread_exception", evidence={"timeline": timeline_path})

    if b_done >= a_deadline:
        return CaseResult(
            "FAIL",
            reason=(f"COM1 短命令於 {b_done - a_start:.2f}s 完成，"
                    f"未搶在 COM0 sleep 5 結束（{a_deadline - a_start:.2f}s）之前"
                    "——疑似 cross-session starvation（#80 #52 回歸）"),
            category="test", reason_code="cross_session_starvation",
            evidence={"timeline": timeline_path},
        )
    return CaseResult(
        "PASS",
        reason=f"COM1 於 {b_done - a_start:.2f}s 完成（COM0 sleep 5 於 {a_deadline - a_start:.2f}s 結束）",
        evidence={"timeline": timeline_path},
    )
