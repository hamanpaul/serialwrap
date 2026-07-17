"""P1 console 對抗（human console 與 agent 命令共存的邊界）。"""
from __future__ import annotations

import random
import subprocess
import time

from ..drivers import find_marker, strip_ansi
from ..harness import Case, CaseResult, register


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="p1", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _minicom_pids() -> set[int]:
    cp = subprocess.run(["pgrep", "-x", "minicom"], capture_output=True, text=True)
    return {int(p) for p in cp.stdout.split() if p.strip().isdigit()}


def _kill9(pid: int) -> None:
    subprocess.run(["kill", "-9", str(pid)], capture_output=True, text=True)


def _close_lease(ctx, iid: str) -> None:
    if iid:
        ctx.sw.run("session", "interactive-close", "--interactive-id", iid)


@_case("p1-con-fanout", "human 即時看到 agent 命令與回應（T6）",
       hints=("畫面缺 marker 先確認 console 沒掉回 line-buffer、無洩漏 daemon 掉字",),
       requires=("tmux", "two_boards"))
def p1_con_fanout(ctx):
    ses = ctx.tmux.name("fanout")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        markers = [f"T6_{i}_{random.randint(1000, 9999)}" for i in range(3)]
        for m in markers:
            ctx.sw.submit_and_wait("COM0", f"echo {m}")
            time.sleep(0.5)
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        missing = [m for m in markers if not find_marker(pane, m)]
        if missing:
            return CaseResult("FAIL", reason=f"console 畫面缺 marker：{missing}")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-defer", "human 打字不擋 agent（T7 suspend/deferred/resume）",
       hints=("deferred flush 後 human 輸入應自成一行、不與 agent 命令 byte 交錯",),
       requires=("tmux", "two_boards"))
def p1_con_defer(ctx):
    ses = ctx.tmux.name("defer")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        ctx.tmux.send(ses, "echo HUMAN_HALF", enter=False)  # 半行不送出
        t0 = time.monotonic()
        cmd = ctx.sw.submit_and_wait("COM0", "echo T7_AGENT")
        took = time.monotonic() - t0
        if cmd.get("status") != "done" or "T7_AGENT" not in (cmd.get("stdout") or ""):
            return CaseResult("FAIL", reason=f"human 打字期間 agent 命令未完成（status={cmd.get('status')}）")
        if took > 15:
            return CaseResult("FAIL", reason=f"agent 命令耗時 {took:.1f}s（疑似被 human console 阻擋）")
        ctx.tmux.send_key(ses, "Enter")  # flush deferred
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        if not find_marker(pane, "HUMAN_HALF"):
            return CaseResult("FAIL", reason="deferred human 輸入未 flush 回 UART")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-busy", "human_active 窗內 agent 不奪 interactive（SESSION_INTERACTIVE_BUSY）",
       hints=("human_active 窗＝60s（HUMAN_ACTIVE_WINDOW_S）；剛敲鍵即在窗內",),
       requires=("tmux", "two_boards"))
def p1_con_busy(ctx):
    ses = ctx.tmux.name("conbusy")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    iid = ""
    try:
        time.sleep(6)
        ctx.tmux.send(ses, "echo BUSYCHK", enter=False)  # 造 human_active（半行、不送出）
        time.sleep(1)
        res = ctx.sw.run("session", "interactive-open", "--selector", "COM0",
                         "--owner", "agent:realhw", "--timeout", "10")
        ctx.note("interactive-open.json", str(res))
        iid = res.get("interactive_id") or ""
        if res.get("error_code") != "SESSION_INTERACTIVE_BUSY":
            return CaseResult("FAIL",
                              reason=f"human_active 窗內 agent 竟奪權（error_code={res.get('error_code')} ok={res.get('ok')}）")
        return CaseResult("PASS")
    finally:
        _close_lease(ctx, iid)  # 萬一意外拿到 lease，收回
        ctx.tmux.send_key(ses, "C-u")
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-softpreempt", "閒置降級：agent 軟奪 idle human lease（soft_preempted）",
       hints=("閒置降級不中斷 human console；close 後原 console owner 應恢復",),
       requires=("tmux", "two_boards"))
def p1_con_softpreempt(ctx):
    ses = ctx.tmux.name("softp")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    iid = ""
    try:
        time.sleep(6)  # 起 minicom 後刻意不輸入
        idle = ctx.cfg["timeouts"]["human_active_window_s"] + 5
        time.sleep(idle)  # 等出 human_active 窗
        res = ctx.sw.run("session", "interactive-open", "--selector", "COM0",
                         "--owner", "agent:realhw", "--timeout", "10")
        ctx.note("interactive-open.json", str(res))
        iid = res.get("interactive_id") or ""
        if not (res.get("ok") and res.get("soft_preempted") and iid):
            return CaseResult("FAIL",
                              reason=f"idle human lease 未被軟奪（ok={res.get('ok')} soft_preempted={res.get('soft_preempted')}）")
        _close_lease(ctx, iid)
        iid = ""
        time.sleep(2)
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        consoles = cl.get("consoles") or []
        if not any(c.get("interactive_owner") for c in consoles):
            return CaseResult("FAIL", reason="close 後原 human console owner 未恢復")
        return CaseResult("PASS")
    finally:
        _close_lease(ctx, iid)
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-liveness", "human console SIGKILL 後孤兒偵測（human_attached 轉 false）",
       hints=("勿 pkill -f（self-match exit 144）；孤兒只來自 SIGKILL/crash",
              "只殺本套件新起的 minicom（before/after PID 差集），不動既有 minicom"),
       requires=("tmux", "two_boards"))
def p1_con_liveness(ctx):
    before = _minicom_pids()
    ses = ctx.tmux.name("liveness")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        ours = _minicom_pids() - before
        if not ours:
            return CaseResult("FAIL", reason="未偵測到本套件新起的 minicom PID")
        for pid in ours:
            _kill9(pid)  # 模擬 crash（SIGKILL 不觸發正常 detach）
        ctx.note("killed-pids.txt", str(sorted(ours)))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            st = ctx.sw.run("session", "self-test", "--selector", "COM0")
            if not st.get("human_attached"):
                ctx.note("selftest.json", str(st))
                return CaseResult("PASS")
            time.sleep(3)
        return CaseResult("FAIL", reason="≤60s 內 human_attached 未轉 false（孤兒未回收）")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-orphan", "孤兒回收後重開 minicom 立即拿回 raw ownership（#76 自癒）",
       hints=("#76 孤兒回收＋自癒；grace 3s 內 flap 不掉 line-buffer；不需 daemon restart",),
       requires=("tmux", "two_boards"))
def p1_con_orphan(ctx):
    before = _minicom_pids()
    ses1 = ctx.tmux.name("orphan1")
    ctx.tmux.new(ses1, "serialwrap-minicom COM0")
    ses2 = ctx.tmux.name("orphan2")
    try:
        time.sleep(6)
        ours = _minicom_pids() - before
        for pid in ours:  # 製造孤兒
            _kill9(pid)
        ctx.tmux.kill(ses1)
        time.sleep(4)  # 過 grace 讓孤兒回收
        ctx.tmux.new(ses2, "serialwrap-minicom COM0")  # 直接重開，不 restart daemon
        time.sleep(6)
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        consoles = cl.get("consoles") or []
        if not any(c.get("interactive_owner") for c in consoles):
            return CaseResult("FAIL", reason="重開 minicom 未拿回 raw interactive ownership")
        ctx.tmux.send(ses2, "ec", enter=False)
        ctx.tmux.send_key(ses2, "Tab")
        time.sleep(2)
        pane = ctx.tmux.capture(ses2)
        ctx.note("pane.txt", pane)
        if "echo" not in strip_ansi(pane):
            return CaseResult("FAIL", reason="Tab 補完未出現（raw 路徑疑掉回 line-buffer）")
        ctx.tmux.send_key(ses2, "C-u")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses1)
        ctx.tmux.kill(ses2)
        time.sleep(3)


@_case("p1-con-second", "第二個 console 走 line-buffer（恰一個 interactive owner）",
       hints=("第二 console 走 line-buffer 是契約；只第一個拿 raw ownership",),
       requires=("tmux", "two_boards"))
def p1_con_second(ctx):
    ses1 = ctx.tmux.name("second1")
    ses2 = ctx.tmux.name("second2")
    ctx.tmux.new(ses1, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        ctx.tmux.new(ses2, "serialwrap-minicom COM0")
        time.sleep(6)
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        consoles = cl.get("consoles") or []
        owners = [c for c in consoles if c.get("interactive_owner")]
        if len(owners) != 1:
            return CaseResult("FAIL", reason=f"interactive_owner 數應為 1，實為 {len(owners)}（consoles={len(consoles)}）")
        if len(consoles) < 2:
            return CaseResult("FAIL", reason=f"第二 console 未建立（consoles={len(consoles)}）")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses1)
        ctx.tmux.kill(ses2)
        time.sleep(3)
