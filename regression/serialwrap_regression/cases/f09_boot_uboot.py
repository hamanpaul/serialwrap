"""F9 開機時序與 U-Boot 保護（#69 #130 #139 #44 #14 #20）——全 destructive，收尾必回 READY。

本檔 case 都固定操作 COM0（bench 事實：prpl 板、U-Boot 2024.04、autoboot 窗實測 3
秒）；COM1（bcm）的 U-Boot 具備性未確認，#224 BRCM 登入回歸另見
``f09_brcm_reboot_login.py``。每個 case 的收尾都必須呼叫
``guards.ensure_ready``——任何路徑都不得把板子留在 U-Boot 或非 READY 狀態就結束。
"""
from __future__ import annotations

import base64
import json
import random
import time
from pathlib import Path
from typing import Any

import yaml
from realhw.harness import CaseResult

from ..harness import Case, register
from .. import guards
from .f10_login_creds import (
    _reclaim_and_ensure_ready,
    _sandbox_symlink_for_board,
    _session_by_com,
    _wait_state,
)
from .f09_brcm_reboot_login import _read_boot_id


def _case(id, title, issues, hints=(), requires=(), destructive=True, pending_issue=False):
    def deco(fn):
        register(Case(id=id, family="F9", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints),
                      pending_issue=pending_issue))
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
        if follow.get("error_code") == "READY_UNCONFIRMED":
            # #162 有界化終態：daemon 已無自動路徑可解（多半板子卡在 bootloader），
            # 重試迴圈在此**必須**停手——這正是有界化要防的「永遠不可能成功的可重試錯誤」。
            return CaseResult(
                "FAIL",
                reason="READY 再確認逾時／逾次落 READY_UNCONFIRMED 終態"
                f"（recommended_action={follow.get('recommended_action')}）；"
                "多半是板子卡在 bootloader，需 self-test 取分類後 interactive-open 處理",
                category="test", reason_code="ready_unconfirmed",
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
    "f9-reboot-stale-ready",
    "單獨 reboot 後，banner 前舊 shell 應答 nonce 不得讓 READY 提早確認（stale-READY）",
    issues=("#162",),
    hints=(
        "prplOS／BusyBox 的 `reboot` 是非同步：shell 立刻重印 prompt，但 shutdown 很慢（IB8 實測 U-Boot"
        " banner 在 reboot 後 17～26 秒）。舊行為在 +3～6 秒就送確認 probe，舊 shell 照常回應 nonce → 提早"
        "確認 READY，agent 命令落進關機中的系統、讀到舊 boot_id。",
        "oracle：reboot 後持續送 `cat /proc/sys/kernel/random/boot_id`——banner 前必須被擋（AUTOBOOT_QUIET）；"
        "任何一次讀到『reboot 前的 boot_id』＝FAIL（stale-READY，或板子根本沒重開）；最終 boot_id 改變並回（真）READY。",
        "此 case 寫成後尚未在真板跑過；上板前需在 allow_destructive 的 bench 執行並確認結果。",
    ),
)
def f9_reboot_stale_ready(ctx):
    """#162 延伸：非同步 reboot 後，舊 shell 在 banner 前應答確認 probe 的 nonce，不得讓 READY 提早確認。

    當初的錯誤行為＝reboot 送出後 3～4 秒（RX 閒置 `REPROBE_RX_IDLE_S`）daemon 就送
    `\\n`＋`echo __READY__<nonce>`，關機中的舊 shell 回應 → `confirm_ready()` 清掉 pending →
    session 在板子真正重開前就是 stale-READY，隨後的 agent 命令通過 gate、讀到舊 boot_id。
    修正後確認 probe 要等 RX 出現 boot banner 才送（寬限內沒有 banner 則照常確認）。

    判讀：reboot 之後以 1 秒週期送 boot_id 讀取命令——
    - 被 `AUTOBOOT_QUIET`／`READY_UNCONFIRMED` 擋下：預期（零 UART 副作用）；
    - 被接受且讀到 reboot 前的 boot_id（或通過 gate 卻沒讀到）：FAIL（stale-READY）；
    - 讀到新的 boot_id：板子已重開並回（真）READY，PASS。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    if ctx.sw.session(com).get("state") != "READY":
        return CaseResult("SKIP", reason=f"{com} 尚非 READY", category="environment", reason_code="board_not_ready")
    before, before_cmd = _read_boot_id(ctx, com)
    if not before:
        return CaseResult("SKIP", reason=f"無法取得 reboot 前 boot ID（{before_cmd}）",
                          category="environment", reason_code="boot_id_unavailable")
    submitted = False
    try:
        t0 = time.monotonic()
        reboot_resp = ctx.sw.submit_and_wait(com, "reboot", cmd_timeout=8.0)
        submitted = True
        ctx.note("reboot-submit.json", str(reboot_resp))

        timeline: list[str] = []
        blocked = 0
        new_boot_id: str | None = None
        stale_at: float | None = None
        stale_info = ""
        while time.monotonic() - t0 < boot_wait_s:
            current, info = _read_boot_id(ctx, com)
            elapsed = time.monotonic() - t0
            accepted = not info.startswith("submit:")  # _read_boot_id：被 gate 拒絕時 info 以 "submit:" 開頭
            if current is None and not accepted:
                timeline.append(f"+{elapsed:.1f}s rejected {info}")
                if info.startswith("submit:AUTOBOOT_QUIET"):
                    blocked += 1
                elif info.startswith("submit:READY_UNCONFIRMED"):
                    break  # 有界化終態，等下去也不會好
            elif current is None:
                # 命令通過 gate 卻沒讀到 boot_id：被關機中的系統吃掉（逾時／被打斷）。
                timeline.append(f"+{elapsed:.1f}s accepted_but_lost {info}")
                stale_at, stale_info = elapsed, f"命令通過 gate 但沒有讀到 boot_id（{info}）"
                break
            elif current == before:
                timeline.append(f"+{elapsed:.1f}s accepted old_boot_id {info}")
                stale_at, stale_info = elapsed, "讀到 reboot 前的 boot_id"
                break
            else:
                timeline.append(f"+{elapsed:.1f}s accepted new_boot_id {info}")
                new_boot_id = current
                break
            time.sleep(1.0)
        timeline_path = ctx.note("stale-ready-timeline.txt", "\n".join(timeline))

        if stale_at is not None:
            return CaseResult(
                "FAIL",
                reason=f"reboot 後 +{stale_at:.1f}s agent 命令在 banner 前被放行：{stale_info}"
                "（stale-READY：舊 shell 應答 nonce 讓 READY 提早確認；或板子根本沒重開）",
                category="test", reason_code="stale_ready_old_boot_id",
                evidence={"stale-ready-timeline": timeline_path},
            )
        if new_boot_id is None:
            return CaseResult(
                "FAIL",
                reason=f"{boot_wait_s:.0f}s 內沒有讀到新的 boot_id（被擋 {blocked} 次）：未回可下命令的 READY",
                category="test", reason_code="boot_not_ready",
                evidence={"stale-ready-timeline": timeline_path},
            )
        if blocked == 0:
            # 第一個被接受的命令就是新 boot_id：沒有觀測到 banner 前的受測窗口，gate 沒被驗到。
            return CaseResult(
                "SKIP",
                reason="未觀測到 banner 前被 AUTOBOOT_QUIET 擋下的命令（受測窗口未現形），無法驗證 stale-READY 防線",
                category="environment", reason_code="stale_window_not_observed",
                evidence={"stale-ready-timeline": timeline_path},
            )
        cleared, last_sess = _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)
        ctx.note("quiet-cleared.json", str({"cleared": cleared, "session": last_sess}))
        if not cleared:
            return CaseResult(
                "FAIL",
                reason=f"讀到新 boot_id 後 {boot_wait_s:.0f}s 內 session 未回可下命令的 READY"
                f"（state={last_sess.get('state')!r}）",
                category="test", reason_code="boot_not_ready",
                evidence={"stale-ready-timeline": timeline_path},
            )
        return CaseResult(
            "PASS",
            reason=f"banner 前 agent 命令被擋 {blocked} 次（零 UART 副作用），重開後 boot_id 改變並回（真）READY",
            evidence={"stale-ready-timeline": timeline_path},
        )
    finally:
        if submitted:
            guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)
            _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)


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
    requires=("human_frontend",),
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
    human = ctx.human("f9uboot", "minicom")
    human.open(com)
    ub: guards.UBootConsole | None = None
    interrupted = False
    left = False

    try:
        time.sleep(6)  # 等 console frontend attach
        cl_before = ctx.sw.run("session", "console-list", "--selector", com)
        ctx.note("console-list-before.json", str(cl_before))
        count_before = len(cl_before.get("consoles") or [])

        ub = guards.UBootConsole(ctx, com, human)
        # 經 human console 送 reboot（非 agent submit），才能無縫接著 interrupt_autoboot。
        human.send_line("reboot")

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

        ub.leave("reset")  # 完整硬重開（operator 裁示：脫困一律 reset）
        left = True
        if not guards.ensure_ready(ctx, com, timeout_s=boot_wait_s):
            return CaseResult(
                "FAIL",
                reason=f"leave('reset') 後 {boot_wait_s:.0f}s 內未回 READY（板子疑似留在 U-Boot）",
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
                    ub.leave("reset")
            except Exception:
                human.send_line("reset")  # 護欄異常時的最後手段，仍只送 reset
        human.close()
        # 雙保險：不論上面哪個分支提前 return，都再確認一次板子確實回到 READY。
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)


def _wait_quiet_cleared(ctx, com: str, *, timeout_s: float) -> tuple[bool, dict]:
    """等 boot quiet window 真正清空（state=READY、``boot_quiet_remaining_s`` 歸零，
    且 ``ready_reconfirm_pending`` 已被 READY 再確認清除）。

    #139 的自發重開機流程 state **全程停留 READY**——``wait_state(READY)`` 對它是
    no-op（首輪驗收實證：誤用它導致 quiet 窗未清就重送、假 FAIL，且級聯污染下一案）。
    #162 起 quiet 過期不再等於可下命令：agent gate 綁 READY 再確認（nonce probe），
    須加等 ``ready_reconfirm_pending`` 歸 False（舊 daemon 無此欄位→falsy，向下相容）。
    #162 有界化：pending 逾時／逾次會落 ``ready_reconfirm_failed`` 終態——此時 pending
    雖為 False 但命令會被不可重試的 ``READY_UNCONFIRMED`` 拒絕，故不得視為「已清空」，
    立即回 False 讓上層走 FAIL 診斷路徑（等下去也不會好）。
    等待「板子真的可下命令」一律以本函式為準。回傳 (是否清空, 最後一次 session dict)。
    """
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = ctx.sw.session(com)
        if last.get("ready_reconfirm_failed"):
            return False, last
        if (
            last.get("state") == "READY"
            and last.get("boot_quiet_remaining_s") is None
            and not last.get("ready_reconfirm_pending")
        ):
            return True, last
        time.sleep(2.0)
    return False, last


@_case(
    "f9-spontaneous-reboot-agent-gated",
    "自發重開機（daemon 不知情）期間 agent 顯式命令必須被 AUTOBOOT_QUIET 擋下",
    issues=("#139", "#130"),
    requires=("human_frontend",),
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
    human = ctx.human("f9spon", "minicom")
    human.open(com)

    try:
        time.sleep(6)  # 等 console frontend attach 並取得 raw ownership
        # 經 human console 送 reboot——daemon 視角為自發重開機，session 停 READY。
        human.send_line("reboot")

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

        # 等板子真正開完機且 quiet window 清空——不能用 wait_state(READY)（state 全程
        # READY、no-op），必須等 boot_quiet_remaining_s 歸零（首輪驗收假 FAIL 教訓）。
        cleared, last_sess = _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)
        ctx.note("quiet-cleared.json", str({"cleared": cleared, "session": last_sess}))
        if not cleared:
            tail = ctx.sw.run("log", "tail-text", "--selector", com)
            tail_text = "\n".join(tail.get("lines") or [])
            tail_path = ctx.note("boot-log-tail.txt", tail_text)
            if "=>" in tail_text or "U-Boot>" in tail_text:
                return CaseResult(
                    "FAIL",
                    reason=f"{boot_wait_s:.0f}s 內 quiet 未清空，log tail 顯示卡在 U-Boot prompt"
                    "（gate 漏擋、bytes 打斷 autoboot）",
                    category="test", reason_code="autoboot_interrupted",
                    evidence={"boot-log-tail": tail_path, "state-quiet-timeline": timeline_path},
                )
            return CaseResult(
                "FAIL",
                reason=f"{boot_wait_s:.0f}s 內 quiet window 未清空"
                f"（state={last_sess.get('state')!r} quiet={last_sess.get('boot_quiet_remaining_s')!r}），開機異常",
                category="test", reason_code="boot_not_ready",
                evidence={"boot-log-tail": tail_path, "state-quiet-timeline": timeline_path},
            )

        # quiet 清空後重送同 echo：必須成功（READY＋quiet 清空 ⇒ gate 解除的相容性約束）。
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
        # 收尾還原：任何路徑不得把板留在 U-Boot／非 READY，**且不得把仍在倒數的
        # quiet window 留給下一案**（首輪驗收實證：殘留 quiet 級聯拖垮 f9-uboot 案）。
        human.close()
        guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)
        _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)


def parse_wal_ndjson(text: str) -> tuple[list[dict[str, Any]], str]:
    """解析 sandbox ``raw.wal.ndjson`` 全文，回傳 ``(紀錄, 證據問題)``；問題為空字串＝證據完整可用。

    判定（證據不足不得 PASS，規格 §11.3）：
    - 以換行結尾的非空行必須是 JSON object，無法解析即為證據缺損；
    - 最後一行若無換行結尾視為 daemon 寫入中，略過；
    - ``seq`` 必須自 1 起連續遞增，缺號或亂序代表擷取缺口。
    """
    records: list[dict[str, Any]] = []
    for idx, line in enumerate(text.split("\n")[:-1], start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return records, f"WAL 第 {idx} 行無法解析為 JSON"
        if not isinstance(rec, dict):
            return records, f"WAL 第 {idx} 行不是 JSON object"
        records.append(rec)
    for expect, rec in enumerate(records, start=1):
        if rec.get("seq") != expect:
            return records, f"WAL seq 缺口或亂序：預期 {expect}、實得 {rec.get('seq')!r}"
    return records, ""


def count_tx(records: list[dict[str, Any]]) -> int:
    """WAL 紀錄中 ``dir == "TX"`` 的筆數（任何 com、任何 payload）。"""
    return sum(1 for rec in records if rec.get("dir") == "TX")


def has_tx_containing(records: list[dict[str, Any]], needle: str) -> bool:
    """是否有 TX 紀錄的 payload（base64 解碼後）含 ``needle``，用以證明 WAL 確實擷取到該命令。"""
    for rec in records:
        if rec.get("dir") != "TX":
            continue
        try:
            payload = base64.b64decode(str(rec.get("payload_b64") or ""), validate=True)
        except ValueError:
            continue
        if needle.encode("utf-8") in payload:
            return True
    return False


PT_QUIET_S: float = 30.0          # SC-35 靜默下限；regression 套件不 import 產品碼，與 PASSTHROUGH_READY_QUIET_S 同值
PT_ATTACH_WINDOW_S: float = 90.0  # 自 daemon 就緒起最長等待 READY（靜默 30 秒＋餘裕）
_PT_AGE_TOL_S: float = 0.1        # daemon 的 last_rx_age_s 取至 0.1 秒，比較時容許取整誤差
_PT_CMD_TERMINAL = frozenset({"done", "error", "canceled"})


def _rx_active(sample: dict[str, Any], quiet_s: float) -> bool:
    if int(sample.get("rx_bytes_last_10s") or 0) > 0:
        return True
    age = sample.get("last_rx_age_s")
    return age is not None and float(age) < quiet_s


def evaluate_pt_quiet_ready(samples: list[dict[str, Any]], *, attach_sent_s: float,
                            quiet_s: float = PT_QUIET_S) -> tuple[str, str, str]:
    """SC-35 真板 oracle 純函式，回傳 ``(verdict, reason_code, reason)``。

    ``samples`` 依時間排序，每筆為一次 ``session list`` 觀測：``t0``／``t1`` 為 RPC 送出前、
    回應收到後的 monotonic 秒；``state``；``last_rx_age_s``；``rx_bytes_last_10s``；
    ``tx`` 為截至該次觀測的 WAL TX 累計筆數；``wal_problem`` 為該次讀取的證據問題（空字串＝無）。
    ``attach_sent_s`` 為 daemon 啟動前的 monotonic 秒：session 建立不可能早於此，故 ATTACHED 起算不早於此。
    """
    if not samples:
        return ("SKIP", "insufficient_evidence", "未取得任何 session 觀測")
    for s in samples:
        if int(s.get("tx") or 0) > 0:
            return ("FAIL", "passthrough_attach_sent_tx",
                    f"passthrough attach 至 READY 期間 WAL 出現 {s['tx']} 筆 TX（SC-35 要求零 TX）")
    for s in samples:
        if s.get("wal_problem"):
            return ("SKIP", "insufficient_evidence", f"WAL 證據不完整：{s['wal_problem']}")
    ready = next((i for i, s in enumerate(samples) if s.get("state") == "READY"), None)
    if ready is None:
        last = samples[-1]
        if _rx_active(last, quiet_s):
            return ("SKIP", "rx_not_quiet", "板子持續輸出（最後觀測仍有 RX 未靜默），無法驗證安靜升 READY")
        return ("FAIL", "quiet_not_promoted",
                f"靜默 {quiet_s:.0f} 秒後仍未升 READY（最後 state={last.get('state')!r}）")
    ready_sample = samples[ready]
    # 下限一：ATTACHED 起算不早於 attach_sent_s，故 READY 不早於 attach_sent_s + quiet_s。
    # 下限二：READY 前（仍為 ATTACHED）觀測到的 RX 必定早於 promotion，故 promotion 需 ≥ 該 RX + quiet_s；
    # RX 年齡推得的時刻為真值的下界（取整誤差由 _PT_AGE_TOL_S 容許）。
    # READY 只能發生在 t1 之前，故 t1 若早於下限即為確定的違規，優先於下方的歧義 SKIP 判定。
    lower = attach_sent_s + quiet_s
    for s in samples[:ready]:
        age = s.get("last_rx_age_s")
        if age is not None:
            lower = max(lower, float(s["t0"]) - float(age) + quiet_s)
    if float(ready_sample["t1"]) + _PT_AGE_TOL_S < lower:
        return ("FAIL", "quiet_promoted_early",
                f"READY 早於 30 秒靜默下限（attach 後 {float(ready_sample['t1']) - attach_sent_s:.1f} 秒即觀測到 READY），違反 SC-35")
    # READY 觀測點本身的 RX 可能發生在 promotion 之後（SC-35：READY 後 RX 不降級），無法區分，故保守 SKIP。
    if _rx_active(ready_sample, quiet_s):
        return ("SKIP", "rx_not_quiet",
                "READY 觀測當下仍有 RX（last_rx_age_s < 30），無法證明 READY 前連續靜默 30 秒（證據不足）")
    return ("PASS", "", "attach 至 READY 全程 WAL 零 TX，且 READY 不早於 30 秒靜默下限")


def command_from_get(resp: dict[str, Any]) -> dict[str, Any]:
    """``cmd status`` 回應為 ``{"ok": ..., "command": {...}}``；取出其中的 command 紀錄（缺則空 dict）。"""
    command = resp.get("command") if isinstance(resp, dict) else None
    return dict(command) if isinstance(command, dict) else {}


def evaluate_pt_command(command: dict[str, Any], *, marker: str, wal_problem: str,
                        wal_marker_seen: bool) -> tuple[str, str, str]:
    """``cmd submit --completion-regex`` 的 oracle：產品行為先判；證據不足降為 SKIP，絕不為 PASS。"""
    status = command.get("status")
    completed = (status == "done" and not command.get("partial")
                 and marker in str(command.get("stdout") or ""))
    if not completed:
        return ("FAIL", "completion_regex_not_matched",
                f"READY 後以 --completion-regex 送出的 echo 未完成（status={status!r}, partial={command.get('partial')!r}）")
    if wal_problem:
        return ("SKIP", "insufficient_evidence", f"WAL 證據不完整：{wal_problem}")
    if not wal_marker_seen:
        return ("SKIP", "insufficient_evidence", "WAL 未記錄該命令的 TX（證據不足，不得 PASS）")
    return ("PASS", "", "passthrough READY 後 --completion-regex 命令完成，且 WAL 有對應 TX 紀錄")


def _verdict_result(verdict: str, reason: str, reason_code: str, evidence: dict[str, str]) -> CaseResult:
    category = {"SKIP": "environment", "FAIL": "test"}.get(verdict, "")
    return CaseResult(verdict, reason=reason, category=category, reason_code=reason_code, evidence=evidence)


@_case(
    "pt-quiet-ready",
    "passthrough（真板 U-Boot／MCU 線）attach 後零 TX、靜默 30 秒才升 READY，且 cmd submit --completion-regex 可完成",
    issues=(),
    pending_issue=True,
    hints=(
        "throwaway daemon 以 explicit target 釘定 pt-quiet-ready（passthrough）綁 COM0 的 sandbox by-id 線；"
        "不走動態偵測，故 attach 前不會有 PROBE 探測，WAL 全部 TX 皆為該 session 所送。",
        "判定：自 daemon 就緒起每秒取樣直到 READY；attach 至 READY 全程 WAL 零 TX；READY 不得早於 30 秒靜默下限。",
        "板子持續輸出（最後觀測仍有 RX 未靜默）時無法驗證安靜升 READY，判 SKIP（environment），不得判 PASS。",
        "WAL 缺漏、無法解析或 seq 有缺口時判 SKIP insufficient_evidence，原始 WAL 留作 evidence。",
        "命令 `echo PTQ_<nonce>` 的完成條件為 `(?m)^PTQ_<nonce>\\r*$`（錨定行首，命令回顯的行不以 marker 起頭而不會誤匹配）；echo 屬 U-Boot 唯讀白名單。",
        "issues 欄待擁有者為 OD-14 開立 issue 號後補上（不得填假號）。",
    ),
)
def pt_quiet_ready(ctx: Any) -> CaseResult:
    """SC-35 真板 oracle：passthrough 不介入 UART、靜默 30 秒後才升 READY，READY 後命令可用。"""
    board = ctx.cfg["boards"][0]
    com = str(board["com"])
    by_id_dir = ctx.case_dir / "byid"
    try:
        symlink_name = _sandbox_symlink_for_board(board, by_id_dir)
    except LookupError as exc:
        return CaseResult("SKIP", reason=str(exc), category="environment",
                          reason_code="board_by_id_not_found")

    release = ctx.sw.run("device", "release", "--selector", com,
                         "--source", "agent:swreg", "--reason", "pt-quiet-ready regression")
    ctx.note("device-release.json", str(release))
    if not release.get("ok"):
        return CaseResult("FAIL", reason=f"prod device release 失敗（selector={com}）：{release!r}",
                          category="environment", reason_code="release_failed")

    profile_yaml = yaml.safe_dump(
        {
            "defaults": {},
            "profiles": {"pt-quiet-ready": {
                "platform": "passthrough", "prompt_regex": ".*", "login_regex": "$^",
                "password_regex": "$^", "ready_probe": "", "timeout_s": 15,
                "uart": {"baud": 115200, "data_bits": 8, "parity": "N", "stop_bits": 1,
                         "flow_control": "none", "xonxoff": False},
            }},
            # explicit target 取代動態偵測：不會有 detect_template 的 PROBE 探測，故 WAL 全部 TX 皆屬該 session。
            "targets": [{"act_no": 1, "com": com, "profile": "pt-quiet-ready",
                         "device_by_id": symlink_name}],
        },
        allow_unicode=True, sort_keys=False,
    )
    result: CaseResult | None = None
    try:
        result = _run_pt_quiet_ready(ctx, com, by_id_dir, profile_yaml)
    finally:
        # 收尾與 f10 同一契約：prod device attach 收回裝置並等 READY。
        reclaim = _reclaim_and_ensure_ready(ctx, com)
    return reclaim or result


def _read_wal_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _sample_until_ready(ta: Any, com: str, wal_path: Path, *, timeout_s: float) -> list[dict[str, Any]]:
    """每秒取樣 session 狀態與 WAL（TX 累計、證據問題），直到 READY 或時限。"""
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_s
    while True:
        t0 = time.monotonic()
        sess = _session_by_com(ta.run("session", "list"), com)
        t1 = time.monotonic()
        records, problem = parse_wal_ndjson(_read_wal_text(wal_path))
        samples.append({
            "t0": t0, "t1": t1,
            "state": sess.get("state"),
            "last_rx_age_s": sess.get("last_rx_age_s"),
            "rx_bytes_last_10s": sess.get("rx_bytes_last_10s"),
            "tx": count_tx(records),
            "wal_problem": problem,
        })
        if sess.get("state") == "READY" or time.monotonic() >= deadline:
            return samples
        time.sleep(1.0)


def _run_pt_quiet_ready(ctx: Any, com: str, by_id_dir: Any, profile_yaml: str) -> CaseResult:
    """throwaway 內的 SC-35 觀測：時間軌跡判定 → READY 後 --completion-regex 命令完成判定。"""
    workdir = ctx.case_dir / "ta"
    wal_path = workdir / "wal" / "raw.wal.ndjson"
    try:
        daemon_start_s = time.monotonic()
        with guards.ThrowawayDaemon(exe=str(ctx.cfg["serialwrap_exe"]), workdir=workdir,
                                    by_id_dir=by_id_dir, profile_yaml=profile_yaml) as ta:
            samples = _sample_until_ready(ta, com, wal_path, timeout_s=PT_ATTACH_WINDOW_S)
            timeline_note = ctx.note("pt-quiet-timeline.json",
                                     json.dumps(samples, ensure_ascii=False, indent=1))
            verdict, reason_code, reason = evaluate_pt_quiet_ready(
                samples, attach_sent_s=daemon_start_s)
            if verdict != "PASS":
                return _verdict_result(verdict, reason, reason_code, {"timeline": timeline_note})

            marker = f"PTQ_{random.randint(10000, 99999)}"
            regex = rf"(?m)^{marker}\r*$"
            sub = ta.run("cmd", "submit", "--selector", com, "--source", "agent:swreg",
                         "--cmd", f"echo {marker}", "--mode", "line",
                         "--completion-regex", regex, timeout=60)
            cmd_id = str(sub.get("cmd_id") or "")
            if not sub.get("ok") or not cmd_id:
                submit_note = ctx.note("cmd-submit.json", json.dumps(sub, ensure_ascii=False, indent=1))
                return _verdict_result(
                    "FAIL",
                    f"READY 後 cmd submit 被拒絕（error_code={sub.get('error_code')!r}）",
                    "cmd_submit_rejected",
                    {"timeline": timeline_note, "cmd-submit": submit_note},
                )
            command: dict[str, Any] = {}
            deadline = time.monotonic() + 60.0
            while True:
                command = command_from_get(ta.run("cmd", "status", "--cmd-id", cmd_id))
                if command.get("status") in _PT_CMD_TERMINAL or time.monotonic() >= deadline:
                    break
                time.sleep(1.0)
            status_note = ctx.note("cmd-status.json", json.dumps(command, ensure_ascii=False, indent=1))
            wal_text = _read_wal_text(wal_path)
            raw_wal_note = ctx.note("raw.wal.ndjson", wal_text)
            records, wal_problem = parse_wal_ndjson(wal_text)
            verdict, reason_code, reason = evaluate_pt_command(
                command, marker=marker, wal_problem=wal_problem,
                wal_marker_seen=has_tx_containing(records, marker))
            return _verdict_result(verdict, reason, reason_code, {
                "timeline": timeline_note, "cmd-status": status_note, "raw-wal": raw_wal_note,
            })
    except RuntimeError as exc:
        log_path = workdir / "daemon.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "(無 daemon.log)"
        ctx.note("throwaway-start-failed.txt", f"{exc}\n\n--- daemon.log ---\n{log_text}")
        return CaseResult(
            "SKIP",
            reason=f"throwaway daemon 未在時限內就緒：{exc}",
            category="environment", reason_code="throwaway_start_failed",
        )
def _tx_control_bytes(ctx, com: str, start_seq: int) -> tuple[list[int], str | None]:
    """掃 start_seq 之後的 WAL，回傳 TX 為單獨 Ctrl-C／Ctrl-D 的 seq（只留序號，不存 RX 原文）。"""
    hits: list[int] = []
    cursor = start_seq
    for _ in range(40):
        page = ctx.sw.run("log", "tail-raw", "--selector", com, "--from-seq", str(cursor), "--limit", "200")
        if not page.get("ok") or not page.get("wal_file_exists", True):
            return hits, "wal_unavailable"
        records = page.get("records") or []
        if not records and page.get("truncated"):
            return hits, "wal_page_stalled"
        for rec in records:
            if rec.get("dir") == "TX":
                try:
                    payload = base64.b64decode(rec.get("payload_b64") or "", validate=True)
                except (ValueError, TypeError):
                    return hits, "wal_decode_failed"
                if payload in (b"\x03", b"\x04"):
                    hits.append(int(rec.get("seq") or 0))
            cursor = max(cursor, int(rec.get("seq") or cursor))
        if not page.get("truncated"):
            return hits, None
    return hits, "wal_page_limit"


@_case(
    "f9-expect-reboot-chained",
    "cmd submit --expect-reboot：串接 reboot 走 reboot 路徑，不送 Ctrl-C／Ctrl-D、自動回 READY",
    issues=("#130", "#162"),
    hints=(
        "命令是 `sync && reboot`（串接，_is_reboot_command 認不出）加 --expect-reboot。沒帶旗標的對照"
        "（不 arm quiet window，shell 不回 prompt 時逾時送 Ctrl-C／Ctrl-D）會打在 autoboot 窗上，故不在真板重現。",
        "oracle：submit 回應帶 expect_reboot 回聲（舊 daemon 會默默忽略旗標）、command.get 回報 expect_reboot、"
        "命令送出後 WAL 沒有單獨的 TX 0x03／0x04、boot_id 改變並回（真）READY。",
        "此 case 寫成後尚未在真板跑過；上板前需在 allow_destructive 的 bench 執行並確認結果。",
    ),
)
def f9_expect_reboot_chained(ctx):
    """串接 reboot 加 --expect-reboot：與單獨 reboot 同一條 reboot 路徑。

    當初的錯誤行為＝串接命令被當一般命令，等 prompt 逾時後送 Ctrl-C／Ctrl-D，正好打在
    U-Boot autoboot 倒數上。oracle 以 WAL 的 TX 控制位元組與 boot_id 改變判定。
    """
    com = ctx.cfg["boards"][0]["com"]
    boot_wait_s = float(ctx.cfg["timeouts"]["boot_wait_s"])
    if ctx.sw.session(com).get("state") != "READY":
        return CaseResult("SKIP", reason=f"{com} 尚非 READY", category="environment", reason_code="board_not_ready")
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
        sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", "sync && reboot",
                         "--source", "agent:serialwrap-regression", "--mode", "line",
                         "--cmd-timeout", "20", "--expect-reboot")
        sub_path = ctx.note("expect-reboot-submit.json", str(sub))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return CaseResult("FAIL", reason=f"--expect-reboot 命令未被接受：{sub.get('error_code')}",
                              category="test", reason_code="expect_reboot_submit_failed",
                              evidence={"expect-reboot-submit": sub_path})
        submitted = True
        if sub.get("expect_reboot") is not True:
            return CaseResult("FAIL", reason="submit 回應沒有 expect_reboot 回聲（daemon 版本過舊而忽略旗標？）",
                              category="test", reason_code="expect_reboot_not_acknowledged",
                              evidence={"expect-reboot-submit": sub_path})

        time.sleep(5.0)  # 避免把 reboot 尚未執行時的舊 READY 當成重開後已恢復
        cleared, last_sess = _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)
        ctx.note("quiet-cleared.json", str({"cleared": cleared, "session": last_sess}))
        if not cleared:
            tail = ctx.sw.run("log", "tail-text", "--selector", com)
            tail_path = ctx.note("boot-log-tail.txt", "\n".join(tail.get("lines") or []))
            return CaseResult("FAIL", reason=f"{boot_wait_s:.0f}s 內未回可下命令的 READY",
                              category="test", reason_code="boot_not_ready",
                              evidence={"boot-log-tail": tail_path})

        after, after_cmd = _read_boot_id(ctx, com)
        record = (ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {})
        ctrl, wal_error = _tx_control_bytes(ctx, com, start_seq)
        ctx.note("expect-reboot-result.json", str({
            "boot_id_changed": bool(after and after != before), "after_cmd": after_cmd,
            "record_expect_reboot": record.get("expect_reboot"), "record_status": record.get("status"),
            "tx_ctrl_seqs": ctrl, "wal_error": wal_error,
        }))
        if ctrl:
            return CaseResult("FAIL", reason=f"命令送出後 WAL 出現單獨 TX Ctrl-C／Ctrl-D（seq={ctrl}）：串接命令仍被當一般命令",
                              category="test", reason_code="ctrl_bytes_sent")
        if wal_error:
            return CaseResult("SKIP", reason=f"WAL 證據不完整：{wal_error}", category="environment",
                              reason_code=wal_error)
        if not after or after == before:
            # 讀到舊 boot ID 有兩種成因：板子真的沒重開，或 daemon 在非同步 reboot 的關機期間
            # 就確認了 READY（舊 shell 回應 nonce 探測，stale-READY），讀 boot ID 的命令落在
            # 重開之前。2026-10-28 IB8 實機首跑屬後者：TX reboot 後 +3.9 s nonce 被舊 shell
            # 回應，+5.2 s 讀到舊 boot ID，+8.6 s 板子才開始重開。
            return CaseResult("FAIL", reason="boot ID 未改變：板子沒有重開機，或 daemon 在關機期間就確認了 READY（stale-READY）",
                              category="test", reason_code="boot_id_unchanged")
        if record.get("expect_reboot") is not True:
            return CaseResult("FAIL", reason="command.get 沒有回報 expect_reboot=true", category="test",
                              reason_code="record_missing_flag")
        return CaseResult("PASS", reason="串接 reboot 加 --expect-reboot：boot ID 改變、WAL 無 Ctrl-C／Ctrl-D、回（真）READY")
    finally:
        if submitted:
            guards.ensure_ready(ctx, com, timeout_s=boot_wait_s)
            _wait_quiet_cleared(ctx, com, timeout_s=boot_wait_s)
