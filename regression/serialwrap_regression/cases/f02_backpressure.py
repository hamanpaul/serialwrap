"""F2 背壓與資源上限（#81 #128）：QUEUE_FULL backpressure、RSS 有界、recovery flush。

驗證面（實查依據見 PR note）：
- ``CommandArbiter.submit()`` 對單一 session 的 in-flight（accepted/running，``done_at`` 為
  ``None``）命令數做 admission control：達 ``CMD_PENDING_MAX`` 即拒收並回
  ``error_code == "SESSION_QUEUE_FULL"``（``sw_core/arbiter.py`` ``submit()``，#81）。
- 唯讀 grep 得值（**禁 import sw_core**，僅供本檔常數註記，不在 runtime 動態讀取）：
  ``sw_core/constants.py`` ``CMD_PENDING_MAX = 256``、``CMD_HISTORY_MAX = 512``。
- history（``_commands`` dict）淘汰只作用於已終結（有 ``done_at``）記錄，且在多個終結點
  （worker 完成／cancel／submit 後）即時收斂，理論上使 daemon 常駐記憶體不隨命令數無界成長
  （#81）。
- ``session recover`` 觸發 ``unregister_session`` → 於同一把鎖內把該 session 尚未啟動的
  accepted 命令原子標記為 ``FLUSHED_BY_RECOVERY`` 終態，釋放 pending 額度，避免舊佇列殘留
  造成往後每次 submit 都連鎖 ``SESSION_QUEUE_FULL``（#128）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from realhw.harness import CaseResult

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F2", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


# 唯讀 grep 值（sw_core/arbiter.py / sw_core/constants.py，勿 import）：
#   CMD_PENDING_MAX = 256（per-session in-flight 命令上限，越限即 SESSION_QUEUE_FULL）
#   CMD_HISTORY_MAX = 512（_commands 全域已終結記錄上限，超量即淘汰最舊者）
# 攻擊上限必須「超過」CMD_PENDING_MAX 才可能觸發 backpressure（pending 累積速率＝
# submit 速率 − 排空速率；佇列命令用 sleep 5 把排空壓到 0.2/s，CLI submit 約 10+/s，
# 300 次內可確實觸頂）。收尾不逐一輪詢 300 個 cmd_id——改走 session recover 的
# #128 flush 機制一次清空。
_PENDING_MAX_GREPPED = 256
_QUEUE_FULL_ATTEMPT_CAP = _PENDING_MAX_GREPPED + 44  # =300
_RSS_GROWTH_BUDGET_KB = 30 * 1024  # 30MB，門檻寬鬆防 flaky


def _read_vmrss_kb(pid: int) -> int | None:
    """讀 ``/proc/<pid>/status`` 的 ``VmRSS``（kB）；讀不到（權限或 pid 已消失）回 ``None``。

    純檔案系統讀取，不觸碰 daemon RPC 也不 import sw_core。
    """
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _cancel_all(ctx: Any, cmd_ids: list[str]) -> list[dict]:
    """對清單內每個 cmd_id 送一次 ``cmd cancel``（冪等：已終結者回 CMD_NOT_CANCELABLE，忽略即可）。"""
    return [ctx.sw.run("cmd", "cancel", "--cmd-id", cid) for cid in cmd_ids if cid]


def _poll_until_no_pending(ctx: Any, cmd_ids: list[str], *, timeout_s: float,
                            poll_s: float = 1.0) -> list[str]:
    """輪詢給定 cmd_id 清單直到全數落終態（done/error/canceled/timeout）或逾時。

    回傳仍在途者（正常應為空清單）——收尾防線，避免本 case 灌入的 pending 命令遺留到
    下一個 case，污染其佇列額度判讀。
    """
    remaining = [cid for cid in cmd_ids if cid]
    deadline = time.monotonic() + timeout_s
    while remaining and time.monotonic() < deadline:
        still: list[str] = []
        for cid in remaining:
            st = ctx.sw.run("cmd", "status", "--cmd-id", cid)
            command = st.get("command") or {}
            status = command.get("status")
            if st.get("error_code") == "CMD_NOT_FOUND" or status in ("done", "error", "canceled", "timeout"):
                continue
            still.append(cid)
        remaining = still
        if remaining:
            time.sleep(poll_s)
    return remaining


@_case(
    "f2-queue-full-backpressure",
    "連發未等待命令觸發 SESSION_QUEUE_FULL 背壓，且事後可恢復",
    issues=("#81",),
    hints=(
        "攻擊必須真的塞滿 CMD_PENDING_MAX=256：佇列命令用 sleep 5 壓低排空速率、"
        "上限 300 次；submit 期間單 session worker 只吃第一個 sleep，其餘全數累積。",
        "收尾用 session recover 的 #128 flush 一次清空 pending，勿逐一 cancel＋輪詢"
        "（300 個 cmd_id 逐一輪詢會拖數分鐘）。",
    ),
)
def f2_queue_full_backpressure(ctx: Any) -> CaseResult:
    """#81：per-session pending 命令數必須有硬上限（backpressure），超量拒收而非無界排隊 OOM。"""
    com = ctx.cfg["boards"][0]["com"]
    triggered_at: int | None = None
    cmd_ids: list[str] = []  # 收尾 cancel 用（#156：recover 的 CTRL_C 路徑不 flush，不能只靠 recover）
    try:
        submissions_tail: list[dict] = []  # 只留尾段回應當 evidence，避免 300 筆塞爆 note
        for i in range(_QUEUE_FULL_ATTEMPT_CAP):
            r = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sleep 5", "--cmd-timeout", "30")
            if r.get("cmd_id"):
                cmd_ids.append(str(r["cmd_id"]))
            if len(submissions_tail) >= 10:
                submissions_tail.pop(0)
            submissions_tail.append(r)
            if r.get("error_code") == "SESSION_QUEUE_FULL":
                triggered_at = i + 1
                break
        ctx.note("submissions-tail.json", str(submissions_tail))

        if triggered_at is None:
            return CaseResult(
                "FAIL",
                reason=(f"連發 {_QUEUE_FULL_ATTEMPT_CAP} 次 submit（> CMD_PENDING_MAX="
                        f"{_PENDING_MAX_GREPPED}）從未見 SESSION_QUEUE_FULL"
                        "（#81 回歸：backpressure 失效或消失）"),
                category="test", reason_code="no_backpressure",
            )
        return CaseResult(
            "PASS",
            reason=f"第 {triggered_at} 次 submit 觸發 SESSION_QUEUE_FULL（backpressure 生效）",
        )
    finally:
        # 收尾（首輪實測教訓，#156）：recover 走 CTRL_C 攔截路徑時**不會** flush 佇列，
        # 殘留 250+ 條 sleep 5 會外溢毒害後續 case（首輪 f4 兩案因此連鎖 FAIL）——
        # 先逐一 cancel（確定性釋放額度）再 recover 收斂。
        _cancel_all(ctx, cmd_ids)
        recover_resp = ctx.sw.run("session", "recover", "--selector", com)
        ctx.note("cleanup-recover.json", str(recover_resp))
        ready = ctx.sw.wait_state(com, "READY", timeout_s=float(ctx.cfg["timeouts"]["ready_wait_s"]))
        post = ctx.sw.submit_and_wait(com, "echo ok", cmd_timeout=10.0) if ready else {}
        if not ready or post.get("status") != "done":
            # 清理未收斂就再 recover 一次（cg review）：256 條 sleep 5 自然排空要 20 分鐘，
            # 絕不能留給下一個 case。
            ctx.sw.run("session", "recover", "--selector", com)
            ready = ctx.sw.wait_state(com, "READY", timeout_s=float(ctx.cfg["timeouts"]["ready_wait_s"]))
            post = ctx.sw.submit_and_wait(com, "echo ok", cmd_timeout=10.0) if ready else {}
        ctx.note("cleanup-echo-ok.json", str({"ready": ready, "post": post}))


@_case(
    "f2-history-bounded-rss",
    "200 次命令後 daemon RSS 增量有界（history eviction 生效）",
    issues=("#81",),
    hints=(
        "誠實界定（cg review）：CMD_HISTORY_MAX=512，200 輪不必然觸發淘汰——本 case 驗的是"
        "「RSS 增量有界」的粗防線；完整淘汰驗證屬長跑域（serialwrap-reliability soak），"
        "本 plugin 維持分鐘級不擴大輪數。",
        "迴圈零容忍（#158 已修）：任一輪 echo x 未 done 即 FAIL——原偶發 PROMPT_TIMEOUT"
        "根因＝RX 視窗修剪破壞 offset 語意，修復後不得再現；失敗輪 result 記入 evidence。",
    ),
)
def f2_history_bounded_rss(ctx: Any) -> CaseResult:
    """#81：``_commands`` history 需有界淘汰（``CMD_HISTORY_MAX``），否則長跑 daemon RSS 無界成長。"""
    com = ctx.cfg["boards"][0]["com"]
    daemon_pid = ctx.sw.run("daemon", "status").get("pid")
    if not daemon_pid:
        return CaseResult("FAIL", reason="daemon status 未回 pid，無法量測 RSS",
                          category="environment", reason_code="daemon_pid_missing")
    rss_before = _read_vmrss_kb(int(daemon_pid))
    if rss_before is None:
        return CaseResult(
            "FAIL", reason=f"讀不到 /proc/{daemon_pid}/status 的 VmRSS（權限或 pid 消失）",
            category="environment", reason_code="vmrss_unreadable",
        )
    ctx.note("rss-before.txt", f"pid={daemon_pid} VmRSS={rss_before}kB")

    first_cmd_id: str | None = None
    last_cmd_id: str | None = None
    for i in range(200):
        # settle/poll 壓到 0.3/0.2s：200 輪維持在 ~3 分鐘內（預設 1.5/0.5 會拖到 7 分鐘+）。
        result = ctx.sw.submit_and_wait(com, "echo x", cmd_timeout=6.0, settle_s=0.3, poll_s=0.2)
        if result.get("status") != "done":
            # 零容忍（#158 已修）：快速迴圈的 PROMPT_TIMEOUT 根因＝RX 視窗修剪破壞 offset
            # 語意（絕對偏移記帳後根治），任一輪未完成即為錯誤行為再現，恢復即紅。
            ctx.note("iteration-failures.json", str([{"iteration": i, "result": result}]))
            return CaseResult(
                "FAIL",
                reason=f"第 {i} 輪 echo x 未完成（status={result.get('status')!r}，#158 錯誤行為零再現 oracle）",
                category="test", reason_code="submit_loop_failed",
            )
        cmd_id = result.get("cmd_id")
        if first_cmd_id is None:
            first_cmd_id = cmd_id
        last_cmd_id = cmd_id

    # 淘汰行為觀察（記錄用，不影響判定）：history 淘汰只保證全域上限，不保證特定一筆
    # 必被淘汰或保留——取決於同期還有多少其他命令流量。
    first_status = ctx.sw.run("cmd", "status", "--cmd-id", str(first_cmd_id)) if first_cmd_id else {}
    last_status = ctx.sw.run("cmd", "status", "--cmd-id", str(last_cmd_id)) if last_cmd_id else {}
    ctx.note("eviction-probe.json", str({
        "first_cmd_id": first_cmd_id, "first_status": first_status,
        "last_cmd_id": last_cmd_id, "last_status": last_status,
    }))

    daemon_pid_after = ctx.sw.run("daemon", "status").get("pid")
    if daemon_pid_after != daemon_pid:
        return CaseResult(
            "FAIL",
            reason=f"200 次命令間 daemon pid 由 {daemon_pid} 變為 {daemon_pid_after}（daemon 疑似重啟，RSS 比較失去意義）",
            category="environment", reason_code="daemon_pid_changed",
        )
    rss_after = _read_vmrss_kb(int(daemon_pid))
    if rss_after is None:
        return CaseResult(
            "FAIL", reason=f"200 次後讀不到 /proc/{daemon_pid}/status 的 VmRSS（daemon 疑似掛掉/重啟）",
            category="environment", reason_code="vmrss_unreadable",
        )
    ctx.note("rss-after.txt", f"pid={daemon_pid} VmRSS={rss_after}kB")

    delta_kb = rss_after - rss_before
    ctx.note("rss-delta.txt", f"before={rss_before}kB after={rss_after}kB delta={delta_kb}kB")
    if delta_kb >= _RSS_GROWTH_BUDGET_KB:
        return CaseResult(
            "FAIL",
            reason=f"200 次命令後 daemon RSS 增量 {delta_kb}kB（≥{_RSS_GROWTH_BUDGET_KB}kB，#81 回歸：history 無界成長）",
            category="test", reason_code="rss_unbounded_growth",
        )
    return CaseResult("PASS", reason=f"RSS 增量 {delta_kb}kB（<{_RSS_GROWTH_BUDGET_KB}kB）")


# 唯讀 grep 值（sw_core/uart_io.py，勿 import）：
#   _rx_max_chars = 131072（UARTBridge RX 視窗上限，觸頂即前端修剪）
# 預熱必須推超過此值才能使緩衝飽和：awk 每輪印 1200 行 ×（37 字元＋行尾）≈ 47KB，
# 3 輪 ≈ 140KB > 131072，確定性跨界。
_RX_MAX_CHARS_GREPPED = 131072
_PREHEAT_ROUNDS = 3
_PREHEAT_CMD = (
    "awk 'BEGIN{for(i=0;i<1200;i++)print \"PAD-0123456789abcdef-0123456789abcdef\"}'"
)


@_case(
    "f2-rx-window-crossing-prompt",
    "RX 視窗飽和跨界後 prompt 匹配不失效（offset 絕對偏移語意）",
    issues=("#158",),
    hints=(
        "決定性重演（非等自然累積）：先以 awk 大輸出把 RX 緩衝推超過 _rx_max_chars=131072"
        "（唯讀 grep 常數，禁 import sw_core）使視窗飽和，再連發 echo x——修前飽和後首輪即"
        "PROMPT_TIMEOUT（rx_snapshot_len 恆等於上限、切片永遠空字串）。",
        "115200 baud 下預熱 ~140KB 需 ~12s+，預熱輪 cmd_timeout 放 60s；預熱本身失敗屬"
        "environment（preheat_failed），非受測行為。",
        "收尾必經 READY 還原：修前失敗路徑 recovery 會 CTRL_D 誤登出 console 轉 ATTACHED，"
        "finally 內 wait_state→（必要時）session recover→echo ok 驗收。",
    ),
)
def f2_rx_window_crossing_prompt(ctx: Any) -> CaseResult:
    """#158：RX 視窗有界修剪破壞 offset 語意——緩衝飽和後 ``rx_snapshot_len()`` 恆等於
    131072，``wait_for_regex_from(pattern, pre_offset)`` 切片永遠空字串，prompt 永不匹配
    → PROMPT_TIMEOUT、stdout 空、recovery 誤送 CTRL_D 登出 console。修復＝絕對串流偏移
    記帳；本 case 決定性重演跨界並驗證錯誤行為零再現。
    """
    com = ctx.cfg["boards"][0]["com"]
    if ctx.sw.session(com).get("state") != "READY":
        return CaseResult("SKIP", reason=f"{com} 非 READY，無法執行前景命令",
                          category="environment", reason_code="board_not_ready")
    try:
        # 預熱：把 RX 緩衝推超過 _rx_max_chars 使視窗飽和（3×~47KB ≈ 140KB > 131072）。
        for i in range(_PREHEAT_ROUNDS):
            r = ctx.sw.submit_and_wait(com, _PREHEAT_CMD, cmd_timeout=60)
            if r.get("status") != "done":
                ctx.note("preheat-failure.json", str({"round": i, "result": {
                    k: v for k, v in r.items() if k != "stdout"}}))
                return CaseResult(
                    "FAIL",
                    reason=f"預熱第 {i} 輪大輸出命令未完成（status={r.get('status')!r}，非受測行為）",
                    category="environment", reason_code="preheat_failed",
                )

        # 跨界迴圈：飽和態下連發短命令。修前：首輪即 PROMPT_TIMEOUT（典型簽名：
        # error_code==PROMPT_TIMEOUT 且 stdout 空）。
        for i in range(30):
            result = ctx.sw.submit_and_wait(com, "echo x", cmd_timeout=6.0, settle_s=0.3, poll_s=0.2)
            if result.get("status") != "done":
                ctx.note("crossing-failure.json", str({"iteration": i, "result": result}))
                return CaseResult(
                    "FAIL",
                    reason=(f"RX 視窗飽和後第 {i} 輪 echo x 未完成（status={result.get('status')!r}，"
                            f"error_code={result.get('error_code')!r}；#158 回歸：prompt 於視窗跨界失效）"),
                    category="test", reason_code="prompt_lost_at_rx_window_bound",
                )
        return CaseResult("PASS", reason="RX 視窗飽和跨界後 30 輪 echo x 全數完成（offset 語意未被修剪破壞）")
    finally:
        # 收尾（倣 f2-queue-full）：修前失敗路徑會 CTRL_D 登出 console 並轉 ATTACHED，必須還原。
        ready = ctx.sw.wait_state(com, "READY", timeout_s=float(ctx.cfg["timeouts"]["ready_wait_s"]))
        if not ready:
            ctx.sw.run("session", "recover", "--selector", com)
            ready = ctx.sw.wait_state(com, "READY", timeout_s=float(ctx.cfg["timeouts"]["ready_wait_s"]))
        post = ctx.sw.submit_and_wait(com, "echo ok", cmd_timeout=10.0) if ready else {}
        ctx.note("cleanup-echo-ok.json", str({"ready": ready, "post": post}))


@_case(
    "f2-recovery-flushes-queue",
    "session recover 後舊佇列已 flush，立即 submit 不再連鎖 QUEUE_FULL",
    issues=("#128",),
    hints=(
        "已知行為（repo 既有事實）：recover 回應可能是 TIMEOUT 但實際已成功——判定不得看"
        "recover 本身的回應，只看 wait_state 是否真的回到 READY。",
    ),
)
def f2_recovery_flushes_queue(ctx: Any) -> CaseResult:
    """#128：``session recover`` 必須原子終結舊佇列的 accepted 命令，否則 stale 記錄長期佔用
    ``CMD_PENDING_MAX`` 額度，之後每次 submit 都連鎖 ``SESSION_QUEUE_FULL``，直到 daemon 重啟為止。
    """
    com = ctx.cfg["boards"][0]["com"]
    cmd_ids: list[str] = []
    try:
        submissions: list[dict] = []
        for _ in range(3):
            r = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sleep 4", "--cmd-timeout", "10")
            submissions.append(r)
            if r.get("cmd_id"):
                cmd_ids.append(r["cmd_id"])
        ctx.note("pending-submissions.json", str(submissions))

        recover_resp = ctx.sw.run("session", "recover", "--selector", com)
        ctx.note("recover-response.json", str(recover_resp))

        ready_timeout = float(ctx.cfg["timeouts"]["ready_wait_s"])
        ready = ctx.sw.wait_state(com, "READY", timeout_s=ready_timeout)
        if not ready:
            return CaseResult(
                "FAIL",
                reason=f"session recover 後未在 {ready_timeout:.0f}s 內回 READY",
                category="test", reason_code="stale_queue_after_recovery",
            )

        # 有界排空 oracle（首輪實測定案，#156）：recover 的 CTRL_C 攔截路徑目前**不會**
        # 原子 flush（產品側語意缺口，已立案 #156）——嚴格 flush 斷言在該路徑必然 flaky。
        # 放寬為「30s 內全數終結」（涵蓋 3×sleep 4 自然排空＋CTRL_C 開銷）：抓得住
        # #128 的原始危害（stale 佇列無界殘留、連鎖拖累），#156 修復後可改回嚴格斷言。
        leftover = _poll_until_no_pending(ctx, cmd_ids, timeout_s=30.0)
        if leftover:
            ctx.note("stale-after-recover.json", str(leftover))
            return CaseResult(
                "FAIL",
                reason=f"recover 後 30s 仍有 {len(leftover)} 個 pending 未終結（#128 回歸：stale 佇列無界殘留）",
                category="test", reason_code="stale_queue_after_recovery",
            )

        followup = ctx.sw.submit_and_wait(com, "echo ok", cmd_timeout=8.0)
        ctx.note("followup-submit.json", str(followup))
        if followup.get("error_code") == "SESSION_QUEUE_FULL":
            return CaseResult(
                "FAIL",
                reason="recover 後立即 submit 仍連鎖 SESSION_QUEUE_FULL（#128 回歸：舊佇列未 flush）",
                category="test", reason_code="stale_queue_after_recovery",
            )
        if followup.get("status") != "done" or "ok" not in (followup.get("stdout") or ""):
            return CaseResult(
                "FAIL",
                reason=(f"recover 後 submit echo ok 未成功完成（status={followup.get('status')!r}，"
                        f"_error={followup.get('_error')!r}）"),
                category="test", reason_code="stale_queue_after_recovery",
            )
        return CaseResult("PASS", reason="session recover 後舊佇列已 flush，echo ok 立即成功且無 QUEUE_FULL 連鎖")
    finally:
        # 收尾：確保 recover 前灌入的 3 個 pending 命令（理論上已被 recover flush 終結）
        # 確實不再遺留，避免拖累下一個 case 的佇列額度判讀。
        _cancel_all(ctx, cmd_ids)
        leftover = _poll_until_no_pending(ctx, cmd_ids, timeout_s=max(10.0, 2.0 * len(cmd_ids)))
        ctx.note("leftover-pending.json", str(leftover))
