"""P1 USB 熱插拔（destructive）：usbipd detach/attach 下的 DETACHED-rebind 與 COM↔by-id 確定性。

busid 換線會變——每條先以 usbipd list 驗 config busid 存在，缺則 SKIP（非 FAIL）。
COM1＝brcm/BDK 板。
"""
from __future__ import annotations

import time

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


@_case("p1-hp-cycle", "COM1 熱移除轉 DETACHED、COM0 不受擾、回插自動回原 COM READY",
       hints=("熱插沿用 DETACHED-rebind：同 by-id 板回原 COM 空槽（#100）",
              "busid 不在 usbipd list＝換線，SKIP 非 FAIL"),
       requires=("two_boards",))
def p1_hp_cycle(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    b1 = _board(ctx, "COM1")
    busids = ctx.usbipd.list_busids()
    ctx.note("usbipd-list.txt", str(busids))
    if b1.get("busid") not in busids:
        return CaseResult("SKIP", reason=f"COM1 busid {b1.get('busid')} 不在 usbipd list（換線？）")
    try:
        ctx.usbipd.detach(b1["busid"])
        state = _wait_left_ready(ctx, "COM1", 30)
        ctx.note("com1-state.txt", f"after detach state={state}")
        if state == "READY":
            return CaseResult("FAIL", reason="detach 後 COM1 未在 30s 內離開 READY")
        if ctx.sw.session("COM0").get("state") != "READY":
            return CaseResult("FAIL", reason="COM1 熱移除擾動了 COM0（非 READY）")
        ctx.usbipd.attach(b1["busid"])
        if not ctx.sw.wait_state("COM1", "READY", timeout_s=ready_wait):
            return CaseResult("FAIL", reason="回插後 COM1 未自動回 READY")
        now = ctx.sw.session("COM1")
        if b1["serial"] not in (now.get("device_by_id") or ""):
            return CaseResult("FAIL", reason=f"回插後 COM1 by-id 不含 {b1['serial']}（未回原 COM）")
        return CaseResult("PASS")
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
        return CaseResult("SKIP", reason=f"busid 不在 usbipd list：{missing}（換線？）")
    before = {b["com"]: ctx.sw.session(b["com"]).get("attached_real_path") for b in (b0, b1)}
    try:
        ctx.usbipd.detach(b0["busid"])
        ctx.usbipd.detach(b1["busid"])
        time.sleep(3)
        # 反序回插：COM1 的 busid 先
        ctx.usbipd.attach(b1["busid"])
        time.sleep(2)
        ctx.usbipd.attach(b0["busid"])
        for b in (b0, b1):
            if not ctx.sw.wait_state(b["com"], "READY", timeout_s=ready_wait):
                return CaseResult("FAIL", reason=f"反序回插後 {b['com']} 未回 READY")
        for b in (b0, b1):
            now = ctx.sw.session(b["com"])
            if b["serial"] not in (now.get("device_by_id") or ""):
                return CaseResult("FAIL", reason=f"{b['com']} by-id 不含 {b['serial']}（COM↔板對調）")
        after = {b["com"]: ctx.sw.session(b["com"]).get("attached_real_path") for b in (b0, b1)}
        ctx.note("realpath.txt", f"before={before} after={after}")
        # restart：startup rank 下仍 COM0=AC01QZT0 / COM1=AQ00OAQ7
        rc = ctx.systemd.restart()
        if rc != 0:
            return CaseResult("FAIL", reason=f"systemctl restart 回 rc={rc}")
        for b in (b0, b1):
            if not ctx.sw.wait_state(b["com"], "READY", timeout_s=reboot_wait):
                return CaseResult("FAIL", reason=f"restart 後 {b['com']} 未回 READY")
        for b in (b0, b1):
            now = ctx.sw.session(b["com"])
            if b["serial"] not in (now.get("device_by_id") or ""):
                return CaseResult("FAIL", reason=f"restart 後 {b['com']} 不對應 {b['serial']}（#100 rank 退化）")
        return CaseResult("PASS")
    finally:
        _ensure_attached(ctx, reboot_wait)
