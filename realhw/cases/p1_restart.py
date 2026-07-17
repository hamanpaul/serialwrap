"""P1 重啟／恢復（全 destructive：restart daemon、target reboot、開機窗 attach、recover）。

註（#122）：plan 的 ⚡ 表未於 `p1-rst-recover` 標破壞性；依本任務指示「restart×4 全
destructive」統一標 destructive=True（保守：只影響 --list ⚡ 與 broken 後 SKIP，不擋 --only）。
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


def _restore_ready(ctx, com: str, timeout_s: float) -> None:
    """finally 還原：板非 READY 時 clear+attach 並等回 READY。"""
    if ctx.sw.session(com).get("state") == "READY":
        return
    ctx.sw.run("session", "clear", "--selector", com)
    if ctx.sw.session(com).get("state") != "READY":
        ctx.sw.run("session", "attach", "--selector", com)
    ctx.sw.wait_state(com, "READY", timeout_s=timeout_s)


@_case("p1-rst-daemon", "systemd restart 後映射／profile 逐板不變（#100/#95）",
       hints=("MainPID 未變＝restart 未生效（NOPASSWD sudo？）",
              "by-id 對調＝#100 startup rank 退化；profile 漂移＝#95 偵測不穩"),
       requires=("two_boards",))
def p1_rst_daemon(ctx):
    boards = ctx.cfg["boards"]
    before = {b["com"]: ctx.sw.session(b["com"]) for b in boards}
    pid0 = ctx.systemd.main_pid()
    # 安靜檢查：吵的板重啟會打斷 log；byte_count>0 → SKIP
    for b in boards:
        ctx.sw.run("session", "log-start", "--selector", b["com"])
    time.sleep(3)
    noisy = {}
    for b in boards:
        st = ctx.sw.run("session", "log-stop", "--selector", b["com"])
        if (st.get("byte_count") or 0) > 0:
            noisy[b["com"]] = st.get("byte_count")
    if noisy:
        return CaseResult("SKIP", reason=f"板不安靜（byte_count={noisy}），跳過重啟避免打斷 log")

    rc = ctx.systemd.restart()
    if rc != 0:
        return CaseResult("FAIL", reason=f"systemctl restart 回 rc={rc}（NOPASSWD sudo 未設？）")
    for b in boards:
        if not ctx.sw.wait_state(b["com"], "READY", timeout_s=ctx.cfg["timeouts"]["reboot_wait_s"]):
            return CaseResult("FAIL", reason=f"{b['com']} restart 後未回 READY")
    pid1 = ctx.systemd.main_pid()
    ctx.note("pids.txt", f"before={pid0} after={pid1}")
    if pid1 == 0 or pid1 == pid0:
        return CaseResult("FAIL", reason=f"MainPID 未變更（{pid0}->{pid1}），restart 未生效")
    for b in boards:
        now, was = ctx.sw.session(b["com"]), before[b["com"]]
        if now.get("device_by_id") != was.get("device_by_id"):
            return CaseResult("FAIL", reason=f"{b['com']} by-id 對調（#100 startup rank 退化）")
        if now.get("profile") != was.get("profile"):
            return CaseResult("FAIL", reason=f"{b['com']} profile 漂移（#95）：{was.get('profile')}->{now.get('profile')}")
    return CaseResult("PASS")


@_case("p1-rst-reboot", "target reboot 後自動恢復 READY＋console 存活＋WAL 連續",
       hints=("reboot status 可能 timeout（prplOS 回 prompt 後才斷）——容忍",
              "console client 應跨 reboot 存活（daemon 保 bridge，走 RECOVERING）"),
       requires=("two_boards",))
def p1_rst_reboot(ctx):
    att = ctx.sw.run("session", "console-attach", "--selector", "COM0", "--label", "realhw")
    vtty, cid = att.get("vtty"), att.get("client_id")
    ses = ctx.tmux.name("rstreboot")
    if vtty:
        ctx.tmux.new(ses, f"cat {vtty}")
    try:
        start_seq = ctx.sw.run("wal", "current-seq").get("seq") or 0
        rc = ctx.sw.submit_and_wait("COM0", "reboot", cmd_timeout=10)
        ctx.note("reboot-cmd.json", str(rc))
        seen, saw_transition = [], False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            s = ctx.sw.session("COM0").get("state")
            if s and (not seen or seen[-1] != s):
                seen.append(s)
            if s in ("RECOVERING", "DETACHED", "ATTACHING"):
                saw_transition = True
                break
            time.sleep(1)
        ok_ready = ctx.sw.wait_state("COM0", "READY", timeout_s=ctx.cfg["timeouts"]["reboot_wait_s"])
        end_seq = ctx.sw.run("wal", "current-seq").get("seq") or 0
        ctx.note("state-seq.txt", f"seq={seen} saw_transition={saw_transition} wal {start_seq}->{end_seq}")
        if not ok_ready:
            return CaseResult("FAIL", reason=f"reboot 後未在時限內自動回 READY（狀態序列={seen}）")
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        if cid and not any(c.get("client_id") == cid for c in cl.get("consoles") or []):
            return CaseResult("FAIL", reason="reboot 後 console client 未存活")
        if end_seq <= start_seq:
            return CaseResult("FAIL", reason=f"WAL 未跨 reboot 連續記錄（seq {start_seq}->{end_seq}）")
        return CaseResult("PASS")
    finally:
        if vtty:
            ctx.tmux.kill(ses)
        if cid:
            ctx.sw.run("session", "console-detach", "--selector", "COM0", "--client-id", cid)
        _restore_ready(ctx, "COM0", ctx.cfg["timeouts"]["reboot_wait_s"])


@_case("p1-rst-bootwindow", "開機窗 clear+attach 不卡死、最終自動 READY（#69/#94）",
       hints=("attach 回應非致命 error_code 或 ok 皆可；降級斷言＝最終自動 READY 即 PASS",
              "reprobe_attempts 實況記進 evidence"),
       requires=("two_boards",))
def p1_rst_bootwindow(ctx):
    try:
        rc = ctx.sw.submit_and_wait("COM0", "reboot", cmd_timeout=10)
        ctx.note("reboot-cmd.json", str(rc))
        time.sleep(8)  # 開機窗
        ctx.sw.run("session", "clear", "--selector", "COM0")
        att = ctx.sw.run("session", "attach", "--selector", "COM0")
        ctx.note("attach.json", str(att))  # 非致命 error_code 或 ok 皆記錄
        if not ctx.sw.wait_state("COM0", "READY", timeout_s=ctx.cfg["timeouts"]["reboot_wait_s"]):
            return CaseResult("FAIL", reason="開機窗 attach 後未在時限內自動回 READY")
        s = ctx.sw.session("COM0")
        ctx.note("final-session.json", str(s))  # 含 reprobe_attempts / reprobe_exhausted
        return CaseResult("PASS")
    finally:
        _restore_ready(ctx, "COM0", ctx.cfg["timeouts"]["reboot_wait_s"])


@_case("p1-rst-recover", "session recover 後 self-test OK（TIMEOUT≠失敗是契約）",
       hints=("recover 回 TIMEOUT 常其實已成功——以 self-test 複檢為準",
              "bridge_generation 記進 evidence"),
       requires=("two_boards",))
def p1_rst_recover(ctx):
    gen0 = ctx.sw.session("COM0").get("bridge_generation")
    rec = ctx.sw.run("session", "recover", "--selector", "COM0")
    ctx.note("recover.json", str(rec))  # TIMEOUT/ok 都接受
    st = ctx.sw.run("session", "self-test", "--selector", "COM0")
    ctx.note("selftest.json", f"gen0={gen0} " + str(st))
    if not (st.get("probe_ok") and st.get("classification") == "OK"):
        return CaseResult("FAIL",
                          reason=f"recover 後 self-test 未 OK（classification={st.get('classification')} probe_ok={st.get('probe_ok')}）")
    return CaseResult("PASS")
