"""F9 開機時序與 U-Boot 保護（#69 #130 #139 #44 #14 #20）——全 destructive，收尾必回 READY。

五個 case 都固定操作 COM0（bench 事實：prpl 板、U-Boot 2024.04、autoboot 窗實測 3
秒）；COM1（bcm）的 U-Boot 具備性未確認，本檔暫不涉及。每個 case 的收尾都必須呼叫
``guards.ensure_ready``——任何路徑都不得把板子留在 U-Boot 或非 READY 狀態就結束。
"""
from __future__ import annotations

import random
import time

from realhw.harness import CaseResult

from ..harness import Case, register
from .. import guards


def _case(id, title, issues, hints=(), requires=(), destructive=True):
    def deco(fn):
        register(Case(id=id, family="F9", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


@_case(
    "f9-reboot-autoboot-unmolested",
    "reboot 後 daemon 自身 probe 不得打斷 autoboot（boot quiet window）",
    issues=("#130",),
    hints=(
        "autoboot 窗僅實測 3 秒；若 daemon 的 system probe 沒被 quiet window 擋下、"
        "在窗內送出任何 byte，板子會卡在 U-Boot `=> ` 永遠回不到 READY（#130 回歸現形）。",
    ),
)
def f9_reboot_autoboot_unmolested(ctx):
    """#130：reboot 觸發的 boot quiet window 必須擋住 daemon 自己的 readiness reprobe。

    oracle：COM0 送 ``reboot`` 後，即便沒有任何人為介入，session 也應該在
    ``boot_wait_s`` 內自動回到 ``READY``。若 quiet window 失效，daemon 的 reprobe 會
    在 autoboot 倒數窗內誤送 byte，板子卡在 U-Boot prompt、永遠等不到 READY。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])

    try:
        reboot_resp = ctx.sw.submit_and_wait(com, "reboot", cmd_timeout=8.0)
        ctx.note("reboot-submit.json", str(reboot_resp))  # 板子重開，回應可能不完整，容忍

        time.sleep(5)  # 讓板子確實開始重開機（避免立刻輪詢撞到 reboot 命令本身的 in-flight 視窗）
        ready = ctx.sw.wait_state(com, "READY", timeout_s=boot_wait_s)
        ctx.note("wait-state-result.txt", f"ready={ready} boot_wait_s={boot_wait_s}")
        if ready:
            return CaseResult("PASS", reason=f"reboot 後 {boot_wait_s:.0f}s 內自動回 READY（quiet window 未被誤觸發）")

        tail = ctx.sw.run("log", "tail-text", "--selector", com)
        tail_text = "\n".join(tail.get("lines") or [])
        tail_path = ctx.note("boot-log-tail.txt", tail_text)
        if "=>" in tail_text or "U-Boot>" in tail_text:
            return CaseResult(
                "FAIL",
                reason=f"{boot_wait_s:.0f}s 內未回 READY，log tail 顯示卡在 U-Boot prompt"
                "（daemon probe 打斷 autoboot，#130 回歸）",
                category="test", reason_code="autoboot_interrupted",
                evidence={"boot-log-tail": tail_path},
            )
        return CaseResult(
            "FAIL",
            reason=f"{boot_wait_s:.0f}s 內未回 READY，且 log tail 未見 U-Boot prompt（開機異常，非典型 #130 回歸）",
            category="test", reason_code="boot_not_ready",
            evidence={"boot-log-tail": tail_path},
        )
    finally:
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)


@_case(
    "f9-quiet-window-agent-passthrough",
    "boot quiet window 不得擋真 READY 後的顯式 agent 命令",
    issues=("#130",),
    hints=(
        "quiet window 預設 180s；README 明文（#139 後措辭）『agent 顯式命令僅在 quiet armed"
        " 且 session 未重新確認 READY 的過渡態被 AUTOBOOT_QUIET（可重試）拒絕；session 回"
        " READY 即同步解除』——（真）READY 後送命令必須直達 UART。",
        "submit 回 AUTOBOOT_QUIET＝撞上 stale-READY 窗（板其實仍在開機、banner re-arm）："
        "boot_wait 預算內有界重試；預算耗盡仍被拒才是 #139『READY 未解除 gate』回歸。",
    ),
)
def f9_quiet_window_agent_passthrough(ctx):
    """#130：boot quiet window 解除條件是 READY，但視窗本身仍可能持續到 180s；即便如此，
    一旦 session（真正）回到 READY，agent 顯式命令必須直接送達，不得被 quiet window 誤擋。

    #139 調整：過早 READY（shutdown 尾流誤 match prompt → recovery 過早 READY → 真
    banner 到達 re-arm quiet）的競速窗內，submit 會拿到即時、無副作用的 ``AUTOBOOT_QUIET``
    （取代舊行為：bytes 落入 boot 窗、10s 後 PROMPT_TIMEOUT 吞掉）。此為撞上 stale-READY
    窗的訊號——sleep 2s 重回 wait_state 再試（boot_wait 預算內有界重試）；oracle 本體
    「（真）READY 後命令必須成功」不變（由 READY ⇒ clear_boot_quiet 保證）。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])

    try:
        reboot_resp = ctx.sw.submit_and_wait(com, "reboot", cmd_timeout=8.0)
        ctx.note("reboot-submit.json", str(reboot_resp))

        time.sleep(5)
        ready = ctx.sw.wait_state(com, "READY", timeout_s=boot_wait_s)
        if not ready:
            return CaseResult(
                "FAIL",
                reason=f"reboot 後 {boot_wait_s:.0f}s 內未回 READY，無法驗證 quiet window passthrough",
                category="test", reason_code="boot_not_ready",
            )

        # 立刻（quiet window 180s 內）送顯式命令——不得被 quiet window 誤擋。
        # AUTOBOOT_QUIET（#139）＝stale-READY 窗：boot_wait 預算內有界重試。
        deadline = time.monotonic() + boot_wait_s
        attempts: list[str] = []
        while True:
            marker = f"AFTER_BOOT_{random.randint(10000, 99999)}"
            follow = ctx.sw.submit_and_wait(com, f"echo {marker}", cmd_timeout=10.0)
            attempts.append(str(follow))
            if follow.get("error_code") == "AUTOBOOT_QUIET" and time.monotonic() < deadline:
                time.sleep(2.0)
                ctx.sw.wait_state(com, "READY",
                                  timeout_s=max(deadline - time.monotonic(), 1.0))
                continue
            break
        follow_path = ctx.note("after-boot-echo.json", "\n".join(attempts))
        if follow.get("error_code") == "AUTOBOOT_QUIET":
            return CaseResult(
                "FAIL",
                reason=f"boot_wait 預算（{boot_wait_s:.0f}s）耗盡，顯式命令仍被 AUTOBOOT_QUIET 拒絕"
                "（#139 回歸：session 回 READY 未同步解除 quiet gate）",
                category="test", reason_code="quiet_window_blocks_agent",
                evidence={"after-boot-echo": follow_path},
            )
        if follow.get("status") != "done" or marker not in (follow.get("stdout") or ""):
            return CaseResult(
                "FAIL",
                reason=f"READY 剛回、quiet window 內送出的顯式命令未能正常完成"
                f"（status={follow.get('status')!r}，#130 回歸：quiet window 誤擋 agent 命令）",
                category="test", reason_code="quiet_window_blocks_agent",
                evidence={"after-boot-echo": follow_path},
            )
        return CaseResult("PASS", reason="reboot 回（真）READY 後，顯式命令即刻直達 UART（quiet window 未誤擋）",
                          evidence={"after-boot-echo": follow_path})
    finally:
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)


@_case(
    "f9-attach-during-boot-reprobes",
    "開機窗期間人為撞擊 clear+attach 後，daemon 須自動 reprobe 回 READY",
    issues=("#69", "#14", "#20"),
    hints=(
        "撞開機窗預期 session clear／attach 先失敗或 TIMEOUT——這是預期中的雜訊，不參與判定；"
        "真正的 oracle 是「之後完全不介入，daemon 是否自己把 session 從 DETACHED 撿回 READY」。",
    ),
)
def f9_attach_during_boot_reprobes(ctx):
    """#69 #14 #20：開機期間人為撞擊（clear 後在 boot 窗內 attach）造成 session 落在
    DETACHED，daemon 的自動 reprobe／hotplug 邏輯必須不靠人為介入，自行把 session 帶回
    READY——不得卡死在 DETACHED。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    poll_deadline_s = boot_wait_s + 60.0

    try:
        # 送出 reboot 但不等終態（cmd submit 本身即為非同步 fire-and-forget）。
        reboot_resp = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "reboot",
                                 "--cmd-timeout", "8")
        ctx.note("reboot-submit.json", str(reboot_resp))

        # 立即（不等 reboot 命令收斂）撞開機窗：clear 後在窗內 attach，預期先失敗或 TIMEOUT。
        clear_resp = ctx.sw.run("session", "clear", "--selector", com)
        ctx.note("clear-during-boot.json", str(clear_resp))
        attach_resp = ctx.sw.run("session", "attach", "--selector", com, timeout=60)
        ctx.note("attach-during-boot.json", str(attach_resp))  # 回應不參與判定，僅存證

        # 之後完全不再介入，只輪詢 session list 記錄狀態轉移序列，直到自動回 READY 或逾時。
        transitions: list[str] = []
        last_state: str | None = None
        deadline = time.monotonic() + poll_deadline_s
        final_state: str | None = None
        while time.monotonic() < deadline:
            state = ctx.sw.session(com).get("state")
            if state != last_state:
                transitions.append(f"{time.monotonic():.1f} {last_state!r} -> {state!r}")
                last_state = state
            final_state = state
            if state == "READY":
                break
            time.sleep(3.0)

        transitions_path = ctx.note("state-transitions.txt", "\n".join(transitions))

        if final_state == "READY":
            return CaseResult(
                "PASS",
                reason="clear+attach 撞開機窗先失敗屬預期；之後未介入，daemon 自動 reprobe 回 READY",
                evidence={"state-transitions": transitions_path},
            )
        return CaseResult(
            "FAIL",
            reason=f"{poll_deadline_s:.0f}s 內未自動回 READY（末態={final_state!r}），"
            "daemon 未能在無人介入下自行 reprobe（#69/#14/#20 回歸）",
            category="test", reason_code="stuck_detached",
            evidence={"state-transitions": transitions_path},
        )
    finally:
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)


@_case(
    "f9-uboot-readonly-and-console-kept",
    "U-Boot 停留期間唯讀操作＋console 不得被踢，離開後自動回 READY",
    issues=("#44", "#130"),
    requires=("tmux",),
    hints=(
        "全程只能經 guards.UBootConsole 唯讀白名單互動，絕不可繞過送 saveenv/setenv/flash 寫入。",
        "#44 的回歸是『U-Boot 停留會把既有 human console 踢掉』——用 console-list 數量比對。",
    ),
)
def f9_uboot_readonly_and_console_kept(ctx):
    """#44 #130：板子停留在 U-Boot 期間，既有 human console 不得被踢；唯讀命令
    （printenv 等）須正常運作；用 ``boot`` 離開後 session 必須自動恢復 READY。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    ses = ctx.tmux.name("f9uboot")
    ctx.tmux.new(ses, f"serialwrap-minicom {com}")
    ub: guards.UBootConsole | None = None
    interrupted = False
    left = False

    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來（照 p0-console-raw／f8 手法）
        cl_before = ctx.sw.run("session", "console-list", "--selector", com)
        ctx.note("console-list-before.json", str(cl_before))
        count_before = len(cl_before.get("consoles") or [])

        ub = guards.UBootConsole(ctx, com, ses)
        # 經 human console 送 reboot（非 agent submit），才能無縫接著 interrupt_autoboot。
        ctx.tmux.send(ses, "reboot")

        # 60s 窗：Linux shutdown → U-Boot banner 可能吃掉 20s 以上，窗太短會假 FAIL；
        # interrupt_autoboot 以 0.3s 週期送鍵，3 秒 autoboot 窗一到即可攔住。
        interrupted = ub.interrupt_autoboot(window_s=60.0)
        ctx.note("interrupt-autoboot.txt", f"interrupted={interrupted}")
        if not interrupted:
            return CaseResult(
                "FAIL",
                reason="reboot 後 60s 內未能攔到 U-Boot autoboot（COM0 意外沒出現 banner/prompt）",
                category="test", reason_code="autoboot_interrupt_failed",
            )

        printenv_out = ub.readonly_cmd("printenv")
        printenv_path = ctx.note("printenv.txt", printenv_out)
        if not printenv_out.strip():
            return CaseResult(
                "FAIL",
                reason="停在 U-Boot prompt 後 printenv 唯讀命令輸出為空（互動疑似失效）",
                category="test", reason_code="uboot_printenv_empty",
                evidence={"printenv": printenv_path},
            )

        # 期間（U-Boot 停留中）console 不得被踢掉（#44 回歸：U-Boot 停留會踢 console）。
        cl_during = ctx.sw.run("session", "console-list", "--selector", com)
        ctx.note("console-list-during.json", str(cl_during))
        count_during = len(cl_during.get("consoles") or [])
        if count_during < count_before or count_during == 0:
            return CaseResult(
                "FAIL",
                reason=f"U-Boot 停留期間 console 數量從 {count_before} 掉到 {count_during}"
                "（human console 被踢，#44 回歸）",
                category="test", reason_code="console_kicked_in_uboot",
                evidence={"printenv": printenv_path},
            )

        ub.leave("boot")  # 讓板子正常繼續開完機
        left = True
        if not guards.ensure_ready(ctx, com, timeout_s=boot_wait_s):
            return CaseResult(
                "FAIL",
                reason=f"leave('boot') 後 {boot_wait_s:.0f}s 內未回 READY（板子疑似留在 U-Boot）",
                category="test", reason_code="board_left_in_uboot",
                evidence={"printenv": printenv_path},
            )
        return CaseResult("PASS", reason="U-Boot 唯讀互動正常、console 全程未被踢、離開後自動回 READY",
                          evidence={"printenv": printenv_path})
    finally:
        # 早退路徑（printenv 空／console 被踢／例外）板子可能還停在 U-Boot prompt——
        # ensure_ready 的 recover 不會替板子下 `boot`，必須先在 kill console 前補送。
        if interrupted and not left:
            try:
                if ub is not None:
                    ub.leave("boot")
            except Exception:
                ctx.tmux.send(ses, "boot")  # 護欄異常時的最後手段，仍只送 boot
        ctx.tmux.kill(ses)
        # 雙保險：不論上面哪個分支提前 return，都再確認一次板子確實回到 READY。
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)


@_case(
    "f9-spontaneous-reboot-agent-gated",
    "自發重開機（daemon 不知情）期間 agent 顯式命令必須被 AUTOBOOT_QUIET 擋下",
    issues=("#139", "#130"),
    requires=("tmux",),
    hints=(
        "受測窗口＝『state=READY 且 boot_quiet_remaining_s 非 null』的過渡態（human console"
        " 送 reboot、daemon 視角為自發重開機、state 停留 READY）；90s 內未觀測到該組合屬"
        " 時序未現形（SKIP/environment），非產品回歸。",
        "FAIL 判定＝當初錯誤行為再現：quiet 窗內 agent 命令被放行（status=done）或被"
        " PROMPT_TIMEOUT 吞掉（bytes 已打進 autoboot 窗）。",
        "state-quiet-timeline.txt 的 (state, boot_quiet_remaining_s) 時序可順帶佐證"
        " round-3 抖動歸因（premature READY + banner re-arm）。",
    ),
)
def f9_spontaneous_reboot_agent_gated(ctx):
    """#139（#130 Finding 4 收斂）：human console 送 reboot（daemon 視角＝自發重開機、
    session 名義上停 READY、RX banner arm quiet）期間，agent 顯式命令不得再被放行——

    oracle＝當初錯誤行為不得再現：「自發重開機期間 agent 顯式命令被放行、bytes 打進
    autoboot 窗、以 PROMPT_TIMEOUT 吞掉或打斷 autoboot」。修正後：第一層（submit-time）
    直接回 ``AUTOBOOT_QUIET`` 無 cmd_id；或第二層（execute-time）以終態
    ``error_code=AUTOBOOT_QUIET`` 攔下。session 回（真）READY 後重送必須成功
    （READY ⇒ clear_boot_quiet 相容性約束）。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    ses = ctx.tmux.name("f9spon")
    ctx.tmux.new(ses, f"serialwrap-minicom {com}")

    try:
        time.sleep(6)  # 等 console-attach＋minicom 拿到 raw ownership（照 f9-uboot-readonly 手法）
        # 經 human console 送 reboot——daemon 視角為自發重開機，session 停 READY。
        ctx.tmux.send(ses, "reboot")

        # 以 0.5s 週期輪詢，等「state==READY 且 boot_quiet_remaining_s 非 null」的
        # 受測窗口；逐拍存時序作 evidence（順帶佐證 round-3 抖動歸因）。
        timeline: list[str] = []
        window_seen = False
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            s = ctx.sw.session(com)
            state = s.get("state")
            quiet = s.get("boot_quiet_remaining_s")
            timeline.append(f"{time.monotonic():.1f} state={state!r} quiet={quiet!r}")
            if state == "READY" and quiet is not None:
                window_seen = True
                break
            time.sleep(0.5)
        timeline_path = ctx.note("state-quiet-timeline.txt", "\n".join(timeline))

        if not window_seen:
            return CaseResult(
                "SKIP",
                reason="90s 內未觀測到『state=READY 且 quiet armed』受測窗口"
                "（state 先離開 READY 或直接開完機）——時序未現形非產品回歸",
                category="environment", reason_code="quiet_window_not_observed",
                evidence={"state-quiet-timeline": timeline_path},
            )

        # 窗口內立即送 agent 顯式命令：必須被第一層或第二層 gate 攔下。
        marker = f"GATE_{random.randint(10000, 99999)}"
        sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", f"echo {marker}")
        sub_path = ctx.note("gate-submit.json", str(sub))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            if sub.get("error_code") != "AUTOBOOT_QUIET":
                return CaseResult(
                    "FAIL",
                    reason=f"quiet 窗內 submit 未回 cmd_id 也非 AUTOBOOT_QUIET"
                    f"（error_code={sub.get('error_code')!r}）",
                    category="test", reason_code="quiet_gate_missing",
                    evidence={"gate-submit": sub_path, "state-quiet-timeline": timeline_path},
                )
            # 第一層（submit-time）gate 生效。
        else:
            # 有 cmd_id → 輪詢終態：第二層（execute-time）必須以 AUTOBOOT_QUIET 攔下。
            final: dict = {}
            poll_deadline = time.monotonic() + 30.0
            while time.monotonic() < poll_deadline:
                st = ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id))
                command = st.get("command") or {}
                if command.get("status") in ("done", "error", "timeout"):
                    final = command
                    break
                time.sleep(0.5)
            final_path = ctx.note("gate-cmd-final.json", str(final))
            if final.get("error_code") != "AUTOBOOT_QUIET":
                return CaseResult(
                    "FAIL",
                    reason=f"quiet 窗內 agent 命令未被 AUTOBOOT_QUIET 攔下"
                    f"（status={final.get('status')!r} error_code={final.get('error_code')!r}）"
                    "——#139 回歸：命令被放行或被 PROMPT_TIMEOUT 吞掉",
                    category="test", reason_code="quiet_gate_missing",
                    evidence={"gate-cmd-final": final_path, "state-quiet-timeline": timeline_path},
                )

        # 等板子真正開完機回 READY。
        if not ctx.sw.wait_state(com, "READY", timeout_s=boot_wait_s):
            tail = ctx.sw.run("log", "tail-text", "--selector", com)
            tail_text = "\n".join(tail.get("lines") or [])
            tail_path = ctx.note("boot-log-tail.txt", tail_text)
            if "=>" in tail_text or "U-Boot>" in tail_text:
                return CaseResult(
                    "FAIL",
                    reason=f"{boot_wait_s:.0f}s 內未回 READY，log tail 顯示卡在 U-Boot prompt"
                    "（gate 漏擋、bytes 打斷 autoboot）",
                    category="test", reason_code="autoboot_interrupted",
                    evidence={"boot-log-tail": tail_path, "state-quiet-timeline": timeline_path},
                )
            return CaseResult(
                "FAIL",
                reason=f"{boot_wait_s:.0f}s 內未回 READY，且 log tail 未見 U-Boot prompt（開機異常）",
                category="test", reason_code="boot_not_ready",
                evidence={"boot-log-tail": tail_path, "state-quiet-timeline": timeline_path},
            )

        # READY 後重送同 echo：必須成功（READY ⇒ clear 的相容性約束）。
        follow = ctx.sw.submit_and_wait(com, f"echo {marker}", cmd_timeout=10.0)
        follow_path = ctx.note("after-ready-echo.json", str(follow))
        if follow.get("status") != "done" or marker not in (follow.get("stdout") or ""):
            return CaseResult(
                "FAIL",
                reason=f"READY 後重送同 echo 未成功（status={follow.get('status')!r}）"
                "——READY 未解除 gate，違反 #139 相容性約束",
                category="test", reason_code="quiet_window_blocks_agent",
                evidence={"after-ready-echo": follow_path, "state-quiet-timeline": timeline_path},
            )
        return CaseResult(
            "PASS",
            reason="自發重開機過渡態 agent 命令被 AUTOBOOT_QUIET 攔下（零 UART 副作用），READY 後重送成功",
            evidence={"state-quiet-timeline": timeline_path, "after-ready-echo": follow_path},
        )
    finally:
        # 收尾還原：任何路徑不得把板留在 U-Boot／非 READY。
        ctx.tmux.kill(ses)
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)
