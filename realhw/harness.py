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
