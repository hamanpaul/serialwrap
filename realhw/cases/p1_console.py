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
            return CaseResult("FAIL", reason=f"console 畫面缺 marker：{missing}",
                              category="test", reason_code="console_fanout_lost")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-defer", "human 打字不擋 agent（T7 suspend/deferred/resume）",
       hints=("deferred flush 後 human 輸入應自成一行、不與 agent 命令 byte 交錯",
              "T7 deferred 只捕捉 agent 命令『執行期間』抵達的按鍵；submit 之前敲的半行"
              "在 raw 模式即時透傳、會與命令交錯污染 stdout——須於執行視窗內敲（#122 實機）"),
       requires=("tmux", "two_boards"))
def p1_con_defer(ctx):
    ses = ctx.tmux.name("defer")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)
        # async submit 一個帶延遲的 agent 命令撐開執行視窗，再於視窗內敲 human 半行
        # → 按鍵進 deferred buffer（非即時透傳），故 agent stdout 乾淨、且 resume 後
        # human 輸入 flush。若在 submit 前敲，raw 透傳會讓兩者在共用 UART 行交錯。
        sub = ctx.sw.run("cmd", "submit", "--selector", "COM0",
                         "--cmd", "sleep 2; echo T7_AGENT", "--cmd-timeout", "15")
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return CaseResult("FAIL",
                              reason=f"agent 命令 submit 未回 cmd_id（{sub.get('error_code')}）",
                              category="test", reason_code="submit_no_cmd_id")
        t0 = time.monotonic()
        time.sleep(0.4)  # 待命令進入執行（sleep 視窗）
        ctx.tmux.send(ses, "echo HUMAN_HALF", enter=False)  # 執行期間敲入 → deferred
        command: dict = {}
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            command = ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {}
            if command.get("status") in ("done", "error", "timeout"):
                break
            time.sleep(0.5)
        took = time.monotonic() - t0
        ctx.note("agent-status.json", str(command))
        if command.get("status") != "done" or "T7_AGENT" not in (command.get("stdout") or ""):
            return CaseResult("FAIL",
                              reason=f"agent 命令未乾淨完成（status={command.get('status')} "
                                     f"stdout={command.get('stdout')!r}）",
                              category="test", reason_code="defer_interleaved")
        if took > 15:
            return CaseResult("FAIL", reason=f"agent 命令耗時 {took:.1f}s（疑似被 human console 阻擋）",
                              category="test", reason_code="defer_agent_blocked")
        ctx.tmux.send_key(ses, "Enter")  # flush deferred
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        if not find_marker(pane, "HUMAN_HALF"):
            return CaseResult("FAIL", reason="deferred human 輸入未 flush 回 UART",
                              category="test", reason_code="defer_flush_lost")
        return CaseResult("PASS")
    finally:
        ctx.tmux.send_key(ses, "C-u")  # 清殘留輸入行，避免污染後續 console case
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
                              reason=f"human_active 窗內 agent 竟奪權（error_code={res.get('error_code')} ok={res.get('ok')}）",
                              category="test", reason_code="busy_gate_bypassed")
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
                              reason=f"idle human lease 未被軟奪（ok={res.get('ok')} soft_preempted={res.get('soft_preempted')}）",
                              category="test", reason_code="soft_preempt_denied")
        _close_lease(ctx, iid)
        iid = ""
        time.sleep(2)
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        consoles = cl.get("consoles") or []
        if not any(c.get("interactive_owner") for c in consoles):
            return CaseResult("FAIL", reason="close 後原 human console owner 未恢復",
                              category="test", reason_code="owner_not_restored")
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
            return CaseResult("FAIL", reason="未偵測到本套件新起的 minicom PID",
                              category="environment", reason_code="minicom_spawn_failed")
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
        return CaseResult("FAIL", reason="≤60s 內 human_attached 未轉 false（孤兒未回收）",
                          category="test", reason_code="orphan_not_recycled")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)


@_case("p1-con-orphan", "孤兒回收後重開 minicom 立即拿回 raw ownership（#76 自癒）",
       hints=("#76 孤兒回收＋自癒；不需 daemon restart",
              "孤兒 lease 由 liveness 探測（self-test）觸發回收、非背景計時器；且『重開拿回 raw』"
              "只在孤兒『已回收後』重開才成立——回收前重開者落 line-buffer 且事後不升等（#122 實機）"),
       requires=("tmux", "two_boards"))
def p1_con_orphan(ctx):
    before = _minicom_pids()
    ses1 = ctx.tmux.name("orphan1")
    ctx.tmux.new(ses1, "serialwrap-minicom COM0")
    ses2 = ctx.tmux.name("orphan2")
    try:
        time.sleep(6)
        ours = _minicom_pids() - before
        for pid in ours:  # 製造孤兒（SIGKILL 不觸發正常 detach）
            _kill9(pid)
        ctx.tmux.kill(ses1)
        # 先輪詢 self-test 驅動孤兒回收（human_attached→False），再重開第二個 minicom；
        # 若在回收前重開，第二 console 會落 line-buffer 且事後不再升等（#76 語意）。
        recycled = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            st = ctx.sw.run("session", "self-test", "--selector", "COM0")
            if not st.get("human_attached"):
                recycled = True
                break
            time.sleep(3)
        ctx.note("recycle.json", str(st))
        if not recycled:
            return CaseResult("FAIL", reason="≤60s 內孤兒未回收（human_attached 未轉 False）",
                              category="test", reason_code="orphan_not_recycled")
        ctx.tmux.new(ses2, "serialwrap-minicom COM0")  # 回收後重開，不 restart daemon
        cl: dict = {}
        owned = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
            if any(c.get("interactive_owner") for c in (cl.get("consoles") or [])):
                owned = True
                break
            time.sleep(2)
        ctx.note("console-list.json", str(cl))
        if not owned:
            return CaseResult("FAIL", reason="重開 minicom 未拿回 raw interactive ownership",
                              category="test", reason_code="raw_ownership_not_granted")
        ctx.tmux.send(ses2, "ec", enter=False)
        ctx.tmux.send_key(ses2, "Tab")
        time.sleep(2)
        pane = ctx.tmux.capture(ses2)
        ctx.note("pane.txt", pane)
        if "echo" not in strip_ansi(pane):
            return CaseResult("FAIL", reason="Tab 補完未出現（raw 路徑疑掉回 line-buffer）",
                              category="test", reason_code="raw_fallback_linebuffer")
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
            return CaseResult("FAIL",
                              reason=f"interactive_owner 數應為 1，實為 {len(owners)}（consoles={len(consoles)}）",
                              category="test", reason_code="owner_count_mismatch")
        if len(consoles) < 2:
            return CaseResult("FAIL", reason=f"第二 console 未建立（consoles={len(consoles)}）",
                              category="test", reason_code="second_console_missing")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses1)
        ctx.tmux.kill(ses2)
        time.sleep(3)
