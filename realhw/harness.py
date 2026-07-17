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
