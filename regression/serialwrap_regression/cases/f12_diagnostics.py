"""F12 診斷保真（#154）：daemon status／CLI 回報的版本欄位必須存在且彼此一致。

當初的錯誤行為＝`daemon status` 完全不帶版本欄位，必須繞 `/proc cmdline +
importlib.metadata` 才問得到 daemon 版本；preflight 的 `daemon_version_probe()`
對此有 fallback，會安靜地接手繞路、繼續拿到看起來正常的版本號，因而**遮蔽**
版本欄位悄悄消失這件事——這條「快路徑本身有沒有在跑」的斷言，只有在真正部署
的 daemon＋pinned CLI 這組真實產物上才驗得到（pytest 測的是原始碼 checkout，
非「已安裝、已部署」的那份二進位），故仍需這個 case。
"""
from __future__ import annotations

import os
import random

from realhw.harness import CaseResult
from realhw.preflight import parse_version

from ..harness import Case, register


def _case(id, title, issues, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, family="F12", title=title, run=fn, issues=tuple(issues),
                      destructive=destructive, requires=tuple(requires), hints=tuple(hints)))
        return fn
    return deco


@_case(
    "f12-version-reported-consistent",
    "daemon status 回報版本欄位，且與 pinned CLI --version 一致（#154 診斷可信度）",
    issues=("#154",),
    hints=(
        "#154 根因：health.status 回應原本完全沒有 version 欄位，daemon 端也無等價於"
        "CLI _resolve_version() 的解析器；preflight 的 daemon_version_probe() 因此得繞"
        "/proc cmdline + importlib.metadata。",
        "回歸即代表 version 欄位又從 daemon status 消失，或消失但沒被任何東西攔下——"
        "preflight 的 fallback 會默默接手、掩蓋掉這個退化，所以本 case 直接斷言快路徑"
        "本身（而非最終拿到的版本字串）。",
    ),
)
def f12_version_reported_consistent(ctx):
    """daemon status 必須直接帶版本欄位（不靠 fallback），且與 pinned CLI 同源。"""
    status = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status.json", str(status))
    daemon_version = status.get("version")
    if not (isinstance(daemon_version, str) and daemon_version.strip()):
        return CaseResult(
            "FAIL",
            reason="daemon status 回應缺少 version 欄位（#154 回歸：健康狀態的可診斷性退化）",
            category="test",
            reason_code="version_field_missing",
            evidence={"daemon-status.json": "daemon-status.json"},
        )

    cli_out = ctx.sw.run("--version")
    ctx.note("cli-version.json", str(cli_out))
    daemon_parsed = parse_version(daemon_version)
    cli_parsed = parse_version(str(cli_out.get("_raw", "")))
    if daemon_parsed is None or cli_parsed is None:
        return CaseResult(
            "FAIL",
            reason=f"版本字串無法解析：daemon={daemon_version!r} cli={cli_out.get('_raw')!r}",
            category="test",
            reason_code="version_unparseable",
            evidence={"daemon-status.json": "daemon-status.json", "cli-version.json": "cli-version.json"},
        )
    if daemon_parsed != cli_parsed:
        return CaseResult(
            "FAIL",
            reason=f"client↔daemon 版本不齊：cli={cli_parsed} daemon={daemon_parsed}"
            "（preflight 的 version_gate() 理論上已先擋過，此處紅燈代表兩層防線同時失守）",
            category="test",
            reason_code="cli_daemon_version_mismatch",
            evidence={"daemon-status.json": "daemon-status.json", "cli-version.json": "cli-version.json"},
        )
    return CaseResult("PASS")


@_case(
    "f12-wal-path-live",
    "daemon status 的 wal_path 真實存在且持續寫入；doctor 含 wal_dir 診斷項（#148）",
    issues=("#148",),
    hints=(
        "#148 根因：systemd unit 不會繼承 shell 匯出的 SERIALWRAP_WAL_DIR（.bashrc 的"
        "export 對它無效），daemon 實際寫入位置與使用者以為的（常誤設到 ~/b-log，"
        "那其實是 agent on-demand capture 目錄）可能完全不同；舊版 doctor 也完全沒有"
        "任何一項能揭露 daemon 實際生效的 WAL 路徑，只能用猜的。",
        "回歸即代表 doctor 又漏了 wal_dir 這項，或 wal_path 回報的其實是死路徑——"
        "daemon 已停止寫入卻無從得知；mtime 前後比對是這條 case 唯一能在真機上驗到"
        "的『daemon 是否真的持續在寫』活體事實，pytest 的短命 throwaway daemon 驗不出。",
    ),
)
def f12_wal_path_live(ctx):
    """wal_path 需指向實際存在、持續被寫入的檔案；doctor 需含 wal_dir 檢查項。"""
    status = ctx.sw.run("daemon", "status")
    ctx.note("daemon-status.json", str(status))
    wal_path = status.get("wal_path")
    if not (isinstance(wal_path, str) and wal_path.strip()):
        return CaseResult("FAIL", reason="daemon status 未回報 wal_path（#148 回歸）",
                          category="test", reason_code="wal_path_missing")
    if not os.path.exists(wal_path):
        return CaseResult("FAIL",
                          reason=f"wal_path={wal_path} 指向不存在的檔案（daemon 實際未寫入該路徑）",
                          category="test", reason_code="wal_path_not_found")

    doc = ctx.sw.run("doctor")
    ctx.note("doctor.json", str(doc))
    checks = {c.get("check") for c in (doc.get("checks") or [])}
    if "wal_dir" not in checks:
        return CaseResult("FAIL", reason="doctor 報告缺少 wal_dir 檢查項（#148 回歸）",
                          category="test", reason_code="doctor_wal_dir_check_missing")

    mtime_before = os.path.getmtime(wal_path)
    com = ctx.cfg["boards"][0]["com"]
    marker = f"MARKER_{random.randint(100000, 999999)}"
    cmd = ctx.sw.submit_and_wait(com, f"echo {marker}")
    ctx.note("submit.json", str(cmd))
    if (cmd.get("status") or "") != "done":
        return CaseResult(
            "FAIL",
            reason=f"submit echo 未正常完成（status={cmd.get('status')}），無法驗證 WAL 存活",
            category="environment", reason_code="submit_precondition_failed",
        )
    mtime_after = os.path.getmtime(wal_path)
    ctx.note("wal-mtime.txt", f"wal_path={wal_path}\nbefore={mtime_before}\nafter={mtime_after}")
    if mtime_after <= mtime_before:
        return CaseResult(
            "FAIL",
            reason=f"送出命令後 wal_path mtime 未前進（before={mtime_before} after={mtime_after}），"
            "daemon 疑似已停止寫入該路徑",
            category="test", reason_code="wal_not_advancing",
        )
    return CaseResult("PASS")
