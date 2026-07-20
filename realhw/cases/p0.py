"""P0 煙霧（~15 分鐘）。全部非破壞性。"""
from __future__ import annotations

import random
import time
from pathlib import Path

from ..drivers import strip_ansi
from ..harness import Case, CaseResult, register


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="p0", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


@_case("p0-doctor", "doctor 全綠＋兩板 READY", requires=("two_boards",))
def p0_doctor(ctx):
    doc = ctx.sw.run("doctor")
    ctx.note("doctor.json", str(doc))
    # 欄位核對（#122）：doctor checks[] 的名稱鍵為 "check"（非 "name"）。
    bad = [c for c in (doc.get("checks") or []) if not c.get("ok")]
    if bad:
        return CaseResult("FAIL", reason=f"doctor 未過：{[c.get('check') for c in bad]}",
                          category="test", reason_code="doctor_not_green")
    for b in ctx.cfg["boards"]:  # COM0=prpl、COM1=brcm/BDK
        s = ctx.sw.session(b["com"])
        if s.get("state") != "READY":
            return CaseResult("FAIL", reason=f"{b['com']} 非 READY（{s.get('state')}）",
                              category="test", reason_code="board_not_ready")
        if b["serial"] not in (s.get("device_by_id") or ""):
            return CaseResult("FAIL", reason=f"{b['com']} by-id 不含預期 serial {b['serial']}",
                              category="configuration", reason_code="testbed_board_mismatch")
    return CaseResult("PASS")


@_case("p0-cmd-async", "cmd submit→status async 全流程",
       hints=("submit 後立刻讀 status 有 line race——submit_and_wait 已隔拍輪詢",
              "雙板要序列化送，back-to-back 會撞 foreground busy"),
       requires=("two_boards",))
def p0_cmd_async(ctx):
    for b in ctx.cfg["boards"]:  # 逐板序列化
        marker = f"P0_{random.randint(10000, 99999)}"
        cmd = ctx.sw.submit_and_wait(b["com"], f"echo {marker}")
        ctx.note(f"{b['com']}-cmd.json", str(cmd))
        if marker not in (cmd.get("stdout") or ""):
            return CaseResult("FAIL", reason=f"{b['com']} stdout 未含 marker（status={cmd.get('status')}）",
                              category="test", reason_code="cmd_marker_missing")
    return CaseResult("PASS")


@_case("p0-console-raw", "minicom 連線＋raw ownership（Tab 補完）",
       hints=("方向鍵/Tab 失效＝掉回 line-buffer，多半 orphan lease 佔住授予閘（#76/#99）",
              "minicom 顯示 Offline（DCD 未拉）不影響輸入"),
       requires=("tmux", "two_boards"))
def p0_console_raw(ctx):
    ses = ctx.tmux.name("p0con")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    try:
        time.sleep(6)  # 等 console-attach＋minicom 起來
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        consoles = cl.get("consoles") or []
        if not any(c.get("interactive_owner") for c in consoles):
            return CaseResult("FAIL", reason="第一個 console 未拿到 raw interactive ownership",
                              category="test", reason_code="raw_ownership_not_granted")
        ctx.tmux.send(ses, "ec", enter=False)
        ctx.tmux.send_key(ses, "Tab")
        time.sleep(2)
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        if "echo" not in strip_ansi(pane):
            return CaseResult("FAIL", reason="Tab 補完未出現（raw 路徑疑掉回 line-buffer）",
                              category="test", reason_code="raw_fallback_linebuffer")
        ctx.tmux.send_key(ses, "C-u")  # 清掉半行，還原乾淨 prompt
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        time.sleep(3)  # 等 router 清理 detach


@_case("p0-clear-reattach", "session clear 自動 re-attach", requires=("two_boards",))
def p0_clear_reattach(ctx):
    ctx.sw.run("session", "clear", "--selector", "COM0")
    if not ctx.sw.wait_state("COM0", "READY", timeout_s=ctx.cfg["timeouts"]["ready_wait_s"]):
        return CaseResult("FAIL", reason="clear 後未在時限內回 READY",
                          category="test", reason_code="reattach_timeout")
    return CaseResult("PASS")


@_case("p0-selftest", "self-test 基本判讀")
def p0_selftest(ctx):
    st = ctx.sw.run("session", "self-test", "--selector", "COM0")
    ctx.note("selftest.json", str(st))
    if not (st.get("probe_ok") and st.get("classification") == "OK"):
        return CaseResult("FAIL",
                          reason=f"classification={st.get('classification')} probe_ok={st.get('probe_ok')}",
                          category="test", reason_code="selftest_not_ok")
    return CaseResult("PASS")


@_case("p0-blog-clean", "b-log 純淨度（無 ANSI transcript 回歸）",
       hints=("回歸根因＝script transcript 模式（6df17a5）；預設應為 minicom 原生 -C（PR#98）",),
       requires=("tmux",))
def p0_blog_clean(ctx):
    before = set((Path.home() / "b-log").glob("mini_COM0_*.log"))
    ses = ctx.tmux.name("p0blog")
    ctx.tmux.new(ses, "serialwrap-minicom COM0")
    time.sleep(6)
    ctx.tmux.kill(ses)
    time.sleep(3)
    new = sorted(set((Path.home() / "b-log").glob("mini_COM0_*.log")) - before)
    if not new:
        return CaseResult("FAIL", reason="未產生新的 mini_COM0_*.log capture",
                          category="test", reason_code="blog_capture_missing")
    text = new[-1].read_bytes()
    esc = text.count(b"\x1b")
    ctx.note("capture-head.txt", text[:2000].decode("utf-8", errors="replace"))
    if b"Script started" in text or esc > 0:
        return CaseResult("FAIL", reason=f"capture 含 transcript 標頭或 ANSI（ESC×{esc}）",
                          category="test", reason_code="blog_ansi_regression")
    return CaseResult("PASS")


@_case("p0-wal-live", "WAL 活性與位置",
       hints=("live WAL 一律在 ~/.local/state/serialwrap/wal（systemd 不繼承 shell env）；勿讀 stale ~/b-log/raw.*",))
def p0_wal_live(ctx):
    mirror = Path.home() / ".local/state/serialwrap/wal/raw.mirror.log"
    before = mirror.stat().st_mtime if mirror.exists() else 0
    marker = f"P0WAL_{random.randint(10000, 99999)}"
    ctx.sw.submit_and_wait("COM0", f"echo {marker}")
    time.sleep(2)
    if not mirror.exists() or mirror.stat().st_mtime <= before:
        return CaseResult("FAIL", reason="live WAL mirror mtime 未跳動",
                          category="test", reason_code="wal_mirror_stale")
    tail = mirror.read_text(errors="replace")[-8000:]
    if marker not in tail:
        return CaseResult("FAIL", reason="WAL mirror 未見命令 marker",
                          category="test", reason_code="wal_marker_missing")
    return CaseResult("PASS")


@_case("p0-multiopen", "無多開（multi_open 假、無外來 tty 持有者）")
def p0_multiopen(ctx):
    st = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status.json", str(st))
    # 欄位核對（#122）：daemon status 的 foreign_holders 是「所有 serialwrapd 對已 attach
    # tty 的持有者 map（tty→pid）」，健康的單一 daemon 一定含自身 pid（非空）。真正的
    # two-reader 訊號＝multi_open 為真，或 holders 內出現 daemon 自身 pid 以外的 pid。
    own_pid = st.get("pid")
    holders = st.get("foreign_holders") or {}
    foreign = {tty: p for tty, p in holders.items() if p != own_pid}
    if st.get("multi_open") or foreign:
        return CaseResult("FAIL", reason=f"multi_open={st.get('multi_open')} 外來持有者={foreign}",
                          category="environment", reason_code="foreign_tty_holder")
    return CaseResult("PASS")
