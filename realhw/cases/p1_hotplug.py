"""P1 USB 熱插拔（destructive）：usbipd detach/attach 下的 DETACHED-rebind 與 COM↔by-id 確定性。

busid 換線會變——每條先以 usbipd list 驗 config busid 存在，缺則 SKIP（非 FAIL）。
COM1＝brcm/BDK 板。

p1-hp-cycle 內建 Windows 端自動救援鏈：usbipd attach 回插失敗
→ WinSwCli 探測 Windows 端 serialwrapd 是否持有 → device release → 重試 attach（≤2 次）；
決策在 drivers.plan_hp_rescue（純函式），本檔只保留 subprocess 執行薄層。
"""
from __future__ import annotations

import time

from .. import drivers
from ..harness import Case, CaseResult, register


def _case(id, title, hints=(), requires=(), destructive=True):
    def deco(fn):
        register(Case(id=id, tier="p1", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _board(ctx, com: str) -> dict:
    for b in ctx.cfg["boards"]:
        if b["com"] == com:
            return b
    return {}


def _ensure_attached(ctx, timeout_s: float) -> None:
    """finally 還原：缺席的 busid 補 attach，兩板等回 READY。"""
    present = set(ctx.usbipd.list_busids())
    for b in ctx.cfg["boards"]:
        if b["busid"] not in present:
            ctx.usbipd.attach(b["busid"])
    for b in ctx.cfg["boards"]:
        ctx.sw.wait_state(b["com"], "READY", timeout_s=timeout_s)


def _wait_left_ready(ctx, com: str, timeout_s: float) -> str | None:
    """等 com 離開 READY（熱移除被偵測），回最後觀察到的 state。"""
    deadline = time.monotonic() + timeout_s
    last = ctx.sw.session(com).get("state")
    while time.monotonic() < deadline:
        last = ctx.sw.session(com).get("state")
        if last != "READY":
            return last
        time.sleep(2)
    return last


def _attach_with_rescue(ctx, board: dict, log: list[str]) -> tuple[bool, str]:
    """usbipd attach＋自動救援鏈；回傳 (最終是否成功, 失敗 reason_code)。"""
    rc = ctx.usbipd.attach(board["busid"])
    log.append(f"usbipd attach rc={rc}")
    if rc == 0:
        return True, ""
    win = getattr(ctx, "win", None)
    retries = 0
    held_seen = False
    while True:
        win_ok = bool(win is not None and win.available())
        held_com: str | None = None
        if win_ok:
            held = win.held_devices()
            log.append(f"win held_devices={held}")
            hit = drivers.match_held_for_serial(held, board.get("serial", ""))
            if hit is not None:
                held_com = hit.get("com") or None
                held_seen = True
        plan = drivers.plan_hp_rescue(win_ok, held_com, retries)
        log.append(f"plan_hp_rescue(win={win_ok}, held={held_com}, retries={retries}) -> {plan}")
        if plan == ("fail_attended",):
            return False, "windows_daemon_holds_device" if held_seen else "usbipd_device_lost"
        for action in plan:
            if action.startswith("win_release:"):
                com = action.split(":", 1)[1]
                rel = win.release(com)
                log.append(f"win release {com} -> ok={rel.get('ok')} rc={rel.get('_rc')}")
                time.sleep(2)
            elif action == "attach_retry":
                retries += 1
                rc = ctx.usbipd.attach(board["busid"])
                log.append(f"usbipd attach retry#{retries} rc={rc}")
                if rc == 0:
                    return True, ""


@_case("p1-hp-cycle", "COM1 熱移除轉 DETACHED、COM0 不受擾、回插自動回原 COM READY（含 Windows 端自動救援）",
       hints=("熱插沿用 DETACHED-rebind：同 by-id 板回原 COM 空槽（#100）",
              "busid 不在 usbipd list＝換線，SKIP 非 FAIL",
              "回插後回 READY 靠 attach+login FSM：brcm/BDK 板需 credential（#140）——"
              "deployed daemon 缺 #140 修正時可能卡 ATTACHED 不回 READY",
              "usbipd attach 失敗→自動救援鏈（Windows 端 device release+重試≤2）；"
              "救不回才 FAIL=windows_daemon_holds_device＋attended",
              "config 的 win_serialwrap_exe 為空＝Windows 端不可探測，救援降級裸重試"),
       requires=("two_boards",))
def p1_hp_cycle(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    b1 = _board(ctx, "COM1")
    busids = ctx.usbipd.list_busids()
    ctx.note("usbipd-list.txt", str(busids))
    if b1.get("busid") not in busids:
        return CaseResult("SKIP", reason=f"COM1 busid {b1.get('busid')} 不在 usbipd list（換線？）",
                          category="environment", reason_code="busid_missing")
    rescue_log: list[str] = []
    try:
        ctx.usbipd.detach(b1["busid"])
        state = _wait_left_ready(ctx, "COM1", 30)
        ctx.note("com1-state.txt", f"after detach state={state}")
        if state == "READY":
            return CaseResult("FAIL", reason="detach 後 COM1 未在 30s 內離開 READY",
                              category="test", reason_code="hotunplug_not_detected")
        if ctx.sw.session("COM0").get("state") != "READY":
            return CaseResult("FAIL", reason="COM1 熱移除擾動了 COM0（非 READY）",
                              category="test", reason_code="bystander_disturbed")
        attached, fail_code = _attach_with_rescue(ctx, b1, rescue_log)
        if not attached:
            ev = {"rescue": ctx.note("rescue.log", "\n".join(rescue_log))}
            return CaseResult("FAIL",
                              reason="usbipd attach 回插失敗且自動救援未果（attended：需人工處置 Windows 端／重插線；救援過程見 evidence）",
                              category="environment", reason_code=fail_code, evidence=ev)
        if not ctx.sw.wait_state("COM1", "READY", timeout_s=ready_wait):
            return CaseResult("FAIL", reason="回插後 COM1 未自動回 READY",
                              category="test", reason_code="replug_ready_timeout")
        now = ctx.sw.session("COM1")
        if b1["serial"] not in (now.get("device_by_id") or ""):
            return CaseResult("FAIL", reason=f"回插後 COM1 by-id 不含 {b1['serial']}（未回原 COM）",
                              category="test", reason_code="com_rank_flipped")
        result = CaseResult("PASS")
        if rescue_log:
            result.evidence["rescue"] = ctx.note("rescue.log", "\n".join(rescue_log))
            result.reason = "PASS（經自動救援：Windows 端 release 後回插成功）"
        return result
    finally:
        _ensure_attached(ctx, ready_wait)


@_case("p1-hp-reorder", "兩板反序回插仍各回原 COM，restart 後 rank 不翻轉（#100）",
       hints=("反序 attach 檢驗 DETACHED-rebind 依 by-id 認板、非列舉序",
              "restart 後 startup rank 仍 COM0=AC01QZT0/COM1=AQ00OAQ7 為 #100 核心保證"),
       requires=("two_boards",))
def p1_hp_reorder(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    reboot_wait = ctx.cfg["timeouts"]["reboot_wait_s"]
    b0, b1 = _board(ctx, "COM0"), _board(ctx, "COM1")
    busids = ctx.usbipd.list_busids()
    ctx.note("usbipd-list.txt", str(busids))
    missing = [b["busid"] for b in (b0, b1) if b["busid"] not in busids]
    if missing:
        return CaseResult("SKIP", reason=f"busid 不在 usbipd list：{missing}（換線？）",
                          category="environment", reason_code="busid_missing")
    before = {b["com"]: ctx.sw.session(b["com"]).get("attached_real_path") for b in (b0, b1)}
    try:
        ctx.usbipd.detach(b0["busid"])
        ctx.usbipd.detach(b1["busid"])
        time.sleep(3)
        ctx.usbipd.attach(b1["busid"])
        time.sleep(2)
        ctx.usbipd.attach(b0["busid"])
        for b in (b0, b1):
            if not ctx.sw.wait_state(b["com"], "READY", timeout_s=ready_wait):
                return CaseResult("FAIL", reason=f"反序回插後 {b['com']} 未回 READY",
                                  category="test", reason_code="replug_ready_timeout")
        for b in (b0, b1):
            now = ctx.sw.session(b["com"])
            if b["serial"] not in (now.get("device_by_id") or ""):
                return CaseResult("FAIL", reason=f"{b['com']} by-id 不含 {b['serial']}（COM↔板對調）",
                                  category="test", reason_code="com_rank_flipped")
        after = {b["com"]: ctx.sw.session(b["com"]).get("attached_real_path") for b in (b0, b1)}
        ctx.note("realpath.txt", f"before={before} after={after}")
        rc = ctx.systemd.restart()
        if rc != 0:
            return CaseResult("FAIL", reason=f"systemctl restart 回 rc={rc}",
                              category="environment", reason_code="systemd_restart_failed")
        for b in (b0, b1):
            if not ctx.sw.wait_state(b["com"], "READY", timeout_s=reboot_wait):
                return CaseResult("FAIL", reason=f"restart 後 {b['com']} 未回 READY",
                                  category="test", reason_code="restart_ready_timeout")
        for b in (b0, b1):
            now = ctx.sw.session(b["com"])
            if b["serial"] not in (now.get("device_by_id") or ""):
                return CaseResult("FAIL", reason=f"restart 後 {b['com']} 不對應 {b['serial']}（#100 rank 退化）",
                                  category="test", reason_code="com_rank_flipped")
        return CaseResult("PASS")
    finally:
        _ensure_attached(ctx, reboot_wait)
