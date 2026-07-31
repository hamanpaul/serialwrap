"""F12 診斷保真（#154）：daemon status／CLI 回報的版本欄位必須存在且彼此一致。

當初的錯誤行為＝`daemon status` 完全不帶版本欄位，必須繞 `/proc cmdline +
importlib.metadata` 才問得到 daemon 版本；preflight 的 `daemon_version_probe()`
對此有 fallback，會安靜地接手繞路、繼續拿到看起來正常的版本號，因而**遮蔽**
版本欄位悄悄消失這件事——這條「快路徑本身有沒有在跑」的斷言，只有在真正部署
的 daemon＋pinned CLI 這組真實產物上才驗得到（pytest 測的是原始碼 checkout，
非「已安裝、已部署」的那份二進位），故仍需這個 case。
"""
from __future__ import annotations

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
