"""F8 daemon 單一性（#101 #53）：two-reader／外部持有者必須被動偵測回報。"""
from __future__ import annotations

import time

from realhw.harness import CaseResult

from ..harness import Case, register
from .. import guards


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F8", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


@_case(
    "f8-foreign-holder-reported",
    "外部 tty 持有者被動偵測（真 foreign holder 開/關）",
    issues=("#101", "#53"),
    hints=(
        "broker console（serialwrap-minicom）持的是 PTY、不是 UART tty——#101 的 foreign holder"
        "指『直接開真實 tty 的外部行程』，故本 case 以 O_RDONLY|O_NONBLOCK 開 tty fd（不讀不寫、"
        "不消耗 bytes）扮演 foreign holder。",
        "baseline 的 foreign_holders 本就含 daemon 自身持有的 tty——oracle 用『新 pid 出現/消失』"
        "的相對變化，不判空。",
    ),
)
def f8_foreign_holder_reported(ctx):
    """以非阻塞唯讀 fd 短暫持有真實 tty，驗 foreign_holders 回報該 pid、釋放後消失（#101 #53）。"""
    import os
    import subprocess

    com = ctx.cfg["boards"][0]["com"]
    sess = ctx.sw.session(com)
    ctx.note("session.json", str(sess))
    by_id = sess.get("device_by_id") or ""
    dev = os.path.realpath(by_id) if by_id else ""
    if not (dev.startswith("/dev/") and os.path.exists(dev)):
        return CaseResult("SKIP", reason=f"無法從 session 解析 tty 裝置路徑（device_by_id={by_id!r}）",
                          category="environment", reason_code="device_path_unresolved")

    before = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status-before.json", str(before))

    # foreign holder：開 fd 後純 sleep——不 read/write、不動 termios，對線路零干擾。
    holder = subprocess.Popen(
        ["python3", "-c",
         f"import os,time; os.open({dev!r}, os.O_RDONLY | os.O_NONBLOCK); time.sleep(30)"],
    )
    try:
        detected = False
        deadline = time.monotonic() + 15
        during = {}
        while time.monotonic() < deadline:
            during = ctx.sw.run("daemon", "status")
            holders = during.get("foreign_holders") or {}
            if any(int(pid) == holder.pid for pid in holders.values()):
                detected = True
                break
            time.sleep(2)
        ctx.note("daemon-status-during.json", str(during))
        if not detected:
            return CaseResult(
                "FAIL",
                reason=f"外部行程（pid={holder.pid}）持有 {dev} 期間 foreign_holders 未回報它（#101/#53 回歸）",
                category="test", reason_code="foreign_holder_not_reported",
            )
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # 釋放後：該 pid 不得殘留（stale）。
    gone = False
    deadline = time.monotonic() + 15
    after = {}
    while time.monotonic() < deadline:
        after = ctx.sw.run("daemon", "status")
        holders = after.get("foreign_holders") or {}
        if not any(int(pid) == holder.pid for pid in holders.values()):
            gone = True
            break
        time.sleep(2)
    ctx.note("daemon-status-after.json", str(after))
    if not gone:
        return CaseResult("FAIL", reason=f"foreign holder 結束後 foreign_holders 仍列 pid={holder.pid}（stale，#53 回歸）",
                          category="test", reason_code="foreign_holder_stale")
    return CaseResult("PASS")


@_case(
    "f8-second-daemon-detected",
    "第二個 daemon（two-reader）須被 prod 被動偵測",
    issues=("#101",),
    hints=(
        "唯讀實查 daemon status：multi_open（bool）與 multi_open_detail.daemons（list[{pid}]）"
        "為健康單一 daemon 時分別為 false／單一元素；second daemon 存在期間應翻正／列出第二筆。",
        "ThrowawayDaemon 對 prod 唯讀，by_id_dir 給空目錄＝不綁任何裝置，非 destructive。",
    ),
)
def f8_second_daemon_detected(ctx):
    """起一個不綁裝置的 throwaway 第二 daemon，驗 prod daemon status 被動偵測到（#101 回歸）。"""
    workdir = ctx.case_dir / "ta"
    by_id_dir = workdir / "byid"  # 空目錄＝不綁任何裝置，throwaway 對真實裝置零接觸
    profile_yaml = "profiles: {}\ntargets: {}\n"  # 最小合法 YAML（無 target，動態偵測池也是空的）

    result = None
    try:
        with guards.ThrowawayDaemon(
            exe=str(ctx.cfg["serialwrap_exe"]),
            workdir=workdir,
            by_id_dir=by_id_dir,
            profile_yaml=profile_yaml,
        ):
            time.sleep(1)  # 讓 prod daemon 下一輪 /proc 掃描有機會反映第二個 serialwrapd
            during = ctx.sw.run("daemon", "status")
            ctx.note("prod-daemon-status-during.json", str(during))
            daemons = (during.get("multi_open_detail") or {}).get("daemons") or []
            detected = bool(during.get("multi_open")) or len(daemons) > 1
            if not detected:
                result = CaseResult(
                    "FAIL",
                    reason="throwaway 第二個 daemon 存在期間，prod daemon status 未偵測到 multi_open"
                    "（#101 回歸）",
                    category="test",
                    reason_code="second_daemon_not_detected",
                    evidence={"during": "prod-daemon-status-during.json"},
                )
            # with 區塊結束（__exit__）會 kill throwaway daemon，prod 全程未被寫入。
    except RuntimeError as exc:
        log_path = workdir / "daemon.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "(無 daemon.log)"
        ctx.note("throwaway-start-failed.txt", f"{exc}\n\n--- daemon.log ---\n{log_text}")
        return CaseResult(
            "SKIP",
            reason=f"throwaway daemon 未在時限內就緒：{exc}",
            category="environment",
            reason_code="throwaway_start_failed",
        )

    # with 區塊退出後應恢復單一 daemon——落 evidence 供比對，不另立硬性 reason_code。
    after = ctx.sw.run("daemon", "status")
    ctx.note("prod-daemon-status-after.json", str(after))

    return result or CaseResult("PASS")
