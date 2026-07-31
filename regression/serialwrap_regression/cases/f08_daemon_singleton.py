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
    "f8-tty-holders-reported",
    "daemon status 正確回報各板 tty 持有者（#101 被動偵測基線）",
    issues=("#101", "#53"),
    hints=(
        "首輪實測教訓：`detect_multi_open()` 只掃 `_is_serialwrapd()` 判定的 serialwrapd 行程"
        "之 fd——任意外部行程（python O_RDONLY 開 tty）不在 #101 的偵測範圍，勿以此設計 oracle；"
        "泛用外部持有者偵測屬新需求、非回歸標的。",
        "健康基線＝每板真實 tty（realpath of device_by_id）在 foreign_holders 映射到主 daemon"
        " pid（唯一 reader），且 doctor 的 single_daemon 檢查一致。",
    ),
)
def f8_tty_holders_reported(ctx):
    """#101 被動偵測契約基線：foreign_holders 須正確映射每板 tty→serialwrapd 持有者 pid，
    doctor 的 single_daemon 與之一致。偵測面壞掉（掃描失效、映射空缺、pid 錯置）即回歸。"""
    import os

    status = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status.json", str(status))
    holders = status.get("foreign_holders") or {}
    daemon_pid = status.get("pid")
    if not daemon_pid:
        return CaseResult("FAIL", reason="daemon status 未回 pid，無法比對持有者",
                          category="test", reason_code="daemon_pid_missing")

    for board in ctx.cfg["boards"]:
        com = board["com"]
        sess = ctx.sw.session(com)
        by_id = sess.get("device_by_id") or ""
        dev = os.path.realpath(by_id) if by_id else ""
        if not (dev.startswith("/dev/") and os.path.exists(dev)):
            return CaseResult("SKIP", reason=f"{com} 無法解析 tty 路徑（device_by_id={by_id!r}）",
                              category="environment", reason_code="device_path_unresolved")
        if dev not in holders:
            return CaseResult(
                "FAIL",
                reason=f"{com} 的 tty（{dev}）未出現在 foreign_holders（#101 回歸：持有者掃描失效）",
                category="test", reason_code="tty_holder_not_reported",
            )
        if int(holders[dev]) != int(daemon_pid):
            return CaseResult(
                "FAIL",
                reason=f"{com} 的 tty 持有者 pid={holders[dev]} ≠ 主 daemon pid={daemon_pid}"
                "（#101 回歸：持有者歸屬錯置，或存在未偵測的 two-reader）",
                category="test", reason_code="tty_holder_wrong_pid",
            )

    doc = ctx.sw.run("doctor")
    ctx.note("doctor.json", str(doc))
    single = next((c for c in (doc.get("checks") or []) if c.get("check") == "single_daemon"), {})
    if not single.get("ok"):
        return CaseResult(
            "FAIL",
            reason=f"doctor single_daemon 檢查未過（{single!r}），與 daemon status 偵測不一致",
            category="test", reason_code="doctor_single_daemon_inconsistent",
        )
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
    ta_cm = guards.ThrowawayDaemon(
        exe=str(ctx.cfg["serialwrap_exe"]),
        workdir=workdir,
        by_id_dir=by_id_dir,
        profile_yaml=profile_yaml,
    )
    # except 範圍只包 __enter__：body 內的例外（如 prod daemon status 異常）不得被
    # 誤判成「throwaway 未就緒」的環境 SKIP。
    try:
        ta_cm.__enter__()
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
    try:
        # multi_open 掃描為 on-demand，但保險起見輪詢至多 15s（同 family 首 case 慣例）。
        detected = False
        during: dict = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            during = ctx.sw.run("daemon", "status")
            daemons = (during.get("multi_open_detail") or {}).get("daemons") or []
            if bool(during.get("multi_open")) or len(daemons) > 1:
                detected = True
                break
            time.sleep(2)
        ctx.note("prod-daemon-status-during.json", str(during))
        if not detected:
            result = CaseResult(
                "FAIL",
                reason="throwaway 第二個 daemon 存在期間，prod daemon status 未偵測到 multi_open"
                "（#101 回歸）",
                category="test",
                reason_code="second_daemon_not_detected",
                evidence={"during": "prod-daemon-status-during.json"},
            )
    finally:
        ta_cm.__exit__(None, None, None)  # kill throwaway daemon，prod 全程未被寫入

    # with 區塊退出後應恢復單一 daemon——落 evidence 供比對，不另立硬性 reason_code。
    after = ctx.sw.run("daemon", "status")
    ctx.note("prod-daemon-status-after.json", str(after))

    return result or CaseResult("PASS")
