"""Case 模型、registry 與過濾、duration 解析、報告產生。純邏輯（可單測）。"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any, Callable


class UnknownCaseError(Exception):
    pass


@dataclasses.dataclass
class CaseResult:
    verdict: str  # PASS | FAIL | SKIP
    reason: str = ""
    evidence: dict[str, str] = dataclasses.field(default_factory=dict)
    duration_s: float = 0.0


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    tier: str  # p0 | p1 | longrun
    title: str
    run: Callable[[Any], CaseResult]
    destructive: bool = False
    requires: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()


REGISTRY: list[Case] = []


def register(case: Case) -> Case:
    if any(c.id == case.id for c in REGISTRY):
        raise ValueError(f"duplicate case id: {case.id}")
    REGISTRY.append(case)
    return case


def select_cases(registry: list[Case], *, tiers: list[str], only: str | None, skip: list[str]) -> list[Case]:
    if only is not None:
        hit = [c for c in registry if c.id == only]
        if not hit:
            raise UnknownCaseError(only)
        return hit
    unknown = [s for s in skip if not any(c.id == s for c in registry)]
    if unknown:
        raise UnknownCaseError(",".join(unknown))
    return [c for c in registry if c.tier in tiers and c.id not in skip]


_DUR = re.compile(r"^(\d+)([hms])$")


def parse_duration(text: str) -> int:
    m = _DUR.match(text.strip())
    if not m:
        raise ValueError(f"duration 格式須為 <N>h/<N>m/<N>s：{text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"h": 3600, "m": 60, "s": 1}[unit]


def render_report_md(meta: dict[str, Any], results: list[tuple[str, CaseResult]],
                     hints: dict[str, tuple[str, ...]]) -> str:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for _, r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    lines = [
        "# realhw 實機穩定性報告",
        "",
        f"- 版本：{meta.get('version')}（git {meta.get('git')}）",
        f"- tiers：{meta.get('tiers')}；開始：{meta.get('started_at')}",
        f"- 結果：PASS: {counts['PASS']}／FAIL: {counts['FAIL']}／SKIP: {counts['SKIP']}",
        "",
        "| case | verdict | 時間(s) | 說明 |",
        "|---|---|---|---|",
    ]
    for cid, r in results:
        lines.append(f"| {cid} | {r.verdict} | {r.duration_s:.1f} | {r.reason} |")
    fails = [(cid, r) for cid, r in results if r.verdict == "FAIL"]
    if fails:
        lines += ["", "## 失敗案例"]
        for cid, r in fails:
            lines += ["", f"### {cid}", f"- 原因：{r.reason}"]
            for h in hints.get(cid, ()):
                lines.append(f"- 提示：{h}")
            for k, v in r.evidence.items():
                lines.append(f"- evidence：[{k}]({v})")
    return "\n".join(lines) + "\n"


def write_reports(report_dir: Path, meta: dict[str, Any], results: list[tuple[str, CaseResult]],
                  hints: dict[str, tuple[str, ...]]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": [
        {"id": cid, "verdict": r.verdict, "reason": r.reason,
         "duration_s": r.duration_s, "evidence": r.evidence} for cid, r in results]}
    (report_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (report_dir / "report.md").write_text(render_report_md(meta, results, hints), encoding="utf-8")


@dataclasses.dataclass
class Ctx:
    cfg: dict
    report_dir: Path
    case_dir: Path
    sw: Any
    tmux: Any
    usbipd: Any
    systemd: Any

    def note(self, name: str, content: str) -> str:
        """寫 evidence 檔，回傳相對路徑（進 CaseResult.evidence）。"""
        self.case_dir.mkdir(parents=True, exist_ok=True)
        p = self.case_dir / name
        p.write_text(content, encoding="utf-8")
        return str(p.relative_to(self.report_dir))


def recovery_command(state: str | None) -> tuple[str, ...]:
    """依 session 狀態選語意正確的恢復動詞（對齊 CLI help，勿一律 device attach）：

    - ``RELEASED``：已交接給外部工具→``device attach`` 收回（外部仍持有時回 DEVICE_STILL_HELD）。
    - ``DETACHED``：無 bridge→``session attach`` 建立 bridge。
    - 其餘不健康（``ATTACHED``/``ATTACHING``/``RECOVERING``…非 READY）：``session recover``
      重建 bridge（TARGET_UNRESPONSIVE 用 recover，非 device attach）。

    純函式、無副作用，供 :func:`run_cases` 的 case 間恢復與單測使用。
    """
    if state == "RELEASED":
        return ("device", "attach")
    if state == "DETACHED":
        return ("session", "attach")
    return ("session", "recover")


def run_cases(cases: list[Case], ctx: Ctx, *, boards: list[str]) -> list[tuple[str, CaseResult]]:
    results: list[tuple[str, CaseResult]] = []
    broken_by: str | None = None
    for case in cases:
        ctx.case_dir = ctx.report_dir / case.id
        if broken_by and ("two_boards" in case.requires or case.destructive):
            results.append((case.id, CaseResult("SKIP", reason=f"前置不滿足（{broken_by} 後板卡未恢復）")))
            continue
        t0 = time.monotonic()
        try:
            r = case.run(ctx)
        except Exception as exc:  # case 內未捕捉例外＝FAIL，不中止套件
            r = CaseResult("FAIL", reason=f"未捕捉例外：{exc!r}")
        r.duration_s = time.monotonic() - t0
        results.append((case.id, r))
        # case 間恢復檢查：兩板 READY 才續跑依賴板卡的 case。依各板當前狀態選語意正確的
        # 恢復動詞（RELEASED→device attach 收回；DETACHED→session attach；其餘不健康→
        # session recover），而非一律 device attach（後者會強搶已正式交接出去的裝置）。
        not_ready = [b for b in boards if ctx.sw.session(b).get("state") != "READY"]
        if not_ready:
            for b in not_ready:
                verb = recovery_command(ctx.sw.session(b).get("state"))
                ctx.sw.run(*verb, "--selector", b)
            time.sleep(5)
            not_ready = [b for b in boards if not ctx.sw.wait_state(b, "READY", timeout_s=60)]
        if not_ready and broken_by is None:
            broken_by = case.id
    return results
