"""P1 裝置交接（destructive）：device release/attach 與外部持有者、跨 daemon restart 持久。

用 COM1（brcm/BDK 板）當交接對象——release 後 COM0（prpl）不受擾。
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


def _force_ready(ctx, com: str, timeout_s: float) -> None:
    """finally 還原：確保外部持有者已無、COM 收回並回 READY。"""
    if ctx.sw.session(com).get("state") == "READY":
        return
    ctx.sw.run("device", "attach", "--selector", com, "--force")
    ctx.sw.wait_state(com, "READY", timeout_s=timeout_s)


@_case("p1-ho-cycle", "release→外部獨佔→attach 回 DEVICE_STILL_HELD→收回 READY",
       hints=("foreign_holders 只掃 serialwrapd fd，外部 minicom 不列入——僅供參考",
              "DEVICE_STILL_HELD 靠 _probe_external_holder 掃 /proc；kill 外部後才收得回"),
       requires=("two_boards",))
def p1_ho_cycle(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    attached = ctx.sw.session("COM1").get("attached_real_path")
    if not attached:
        return CaseResult("FAIL", reason="COM1 無 attached_real_path，無法起外部持有者")
    rel = ctx.sw.run("device", "release", "--selector", "COM1",
                     "--source", "agent:realhw", "--reason", "realhw p1-ho-cycle")
    ctx.note("release.json", str(rel))
    s = ctx.sw.session("COM1")
    if s.get("state") != "RELEASED":
        return CaseResult("FAIL", reason=f"release 後 COM1 非 RELEASED（{s.get('state')}）")
    ses = ctx.tmux.name("hocycle")
    started = False
    try:
        ctx.tmux.new(ses, f"minicom -D {attached} -b 115200")  # 外部獨佔持有者
        started = True
        time.sleep(3)
        ctx.note("daemon-status.json", str(ctx.sw.run("daemon", "status")))  # foreign_holders 供參
        att = ctx.sw.run("device", "attach", "--selector", "COM1")
        ctx.note("attach-held.json", str(att))
        if att.get("error_code") != "DEVICE_STILL_HELD":
            return CaseResult("FAIL", reason=f"外部持有時 attach 未回 DEVICE_STILL_HELD（{att.get('error_code')}）")
        ctx.tmux.kill(ses)
        started = False
        time.sleep(2)
        att2 = ctx.sw.run("device", "attach", "--selector", "COM1")
        ctx.note("attach-ok.json", str(att2))
        if not ctx.sw.wait_state("COM1", "READY", timeout_s=ready_wait):
            return CaseResult("FAIL", reason="kill 外部 minicom 後 attach 未回 READY")
        if ctx.sw.session("COM0").get("state") != "READY":
            return CaseResult("FAIL", reason="交接期間 COM0（prpl）被擾動、非 READY")
        return CaseResult("PASS")
    finally:
        if started:
            ctx.tmux.kill(ses)
            time.sleep(1)
        _force_ready(ctx, "COM1", ready_wait)


@_case("p1-ho-persist", "RELEASED 跨 daemon restart 持久（不被搶回）",
       hints=("restart 後 released map 應自 state.json 還原；COM1 保持 RELEASED、COM0 照常 READY",),
       requires=("two_boards",))
def p1_ho_persist(ctx):
    ready_wait = ctx.cfg["timeouts"]["ready_wait_s"]
    reboot_wait = ctx.cfg["timeouts"]["reboot_wait_s"]
    rel = ctx.sw.run("device", "release", "--selector", "COM1",
                     "--source", "agent:realhw", "--reason", "realhw p1-ho-persist")
    ctx.note("release.json", str(rel))
    if ctx.sw.session("COM1").get("state") != "RELEASED":
        return CaseResult("FAIL", reason="release 後 COM1 非 RELEASED")
    try:
        rc = ctx.systemd.restart()
        if rc != 0:
            return CaseResult("FAIL", reason=f"systemctl restart 回 rc={rc}")
        if not ctx.sw.wait_state("COM0", "READY", timeout_s=reboot_wait):
            return CaseResult("FAIL", reason="restart 後 COM0 未回 READY")
        s1 = ctx.sw.session("COM1")
        ctx.note("com1-after-restart.json", str(s1))
        if s1.get("state") != "RELEASED":
            return CaseResult("FAIL", reason=f"restart 後 COM1 未保持 RELEASED（{s1.get('state')}），被搶回")
        att = ctx.sw.run("device", "attach", "--selector", "COM1")
        ctx.note("attach.json", str(att))
        if not ctx.sw.wait_state("COM1", "READY", timeout_s=ready_wait):
            return CaseResult("FAIL", reason="attach 後 COM1 未回 READY")
        return CaseResult("PASS")
    finally:
        _force_ready(ctx, "COM1", ready_wait)
