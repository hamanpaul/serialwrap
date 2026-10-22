"""YAML test case 載入與驗證。"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

import yaml


@dataclass
class TestCase:
    """一個完整的功能測試案例。"""
    name: str
    category: str
    severity: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    repeat: int = 1
    target: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    env_files: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    bridge: dict[str, Any] = field(default_factory=lambda: {"applicable": False, "compare": "none"})
    scenario: dict[str, Any] | None = None
    source_path: str = ""


_REVISION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_ALLOWED_INVARIANTS = frozenset({
    "zero_tx_on_reject",
    "no_forbidden_interleave",
    "no_stale_context_replay",
    "no_reopen_before_handoff",
    "no_auto_resend",
    "insufficient_evidence_not_pass",
})


def _validate_scenario_meta(scenario: Any, path: pathlib.Path) -> None:
    """驗證 meta.scenario 之規格與型別約束。"""
    if not isinstance(scenario, dict):
        raise ValueError(f"{path}: meta.scenario 必須是 dict")
    rev = scenario.get("revision")
    if not isinstance(rev, str) or not _REVISION_RE.match(rev):
        raise ValueError(f"{path}: scenario.revision 格式錯誤: {rev!r}")
    seed = scenario.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(f"{path}: scenario.seed 必須為整數: {seed!r}")
    caps = scenario.get("capabilities")
    if not isinstance(caps, list) or not caps:
        raise ValueError(f"{path}: scenario.capabilities 必須為非空 list")
    timeout_s = scenario.get("timeout_s")
    if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError(f"{path}: scenario.timeout_s 必須 > 0: {timeout_s!r}")
    invariants = scenario.get("invariants")
    if not isinstance(invariants, list):
        raise ValueError(f"{path}: scenario.invariants 必須為 list")
    unknown = set(invariants) - _ALLOWED_INVARIANTS
    if unknown:
        raise ValueError(f"{path}: scenario.invariants 包含未知 ID: {unknown}")
    expected = scenario.get("expected")
    if expected != "PASS":
        raise ValueError(f"{path}: scenario.expected 必須為 'PASS': {expected!r}")


def load_test_case(path: pathlib.Path) -> TestCase:
    """從 YAML 檔案載入 TestCase。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: YAML 頂層必須是 dict")

    meta = raw.get("meta") or {}
    scenario = meta.get("scenario") or raw.get("scenario")
    if scenario is not None:
        _validate_scenario_meta(scenario, path)

    return TestCase(
        name=meta.get("name", path.stem),
        category=meta.get("category", "unknown"),
        severity=meta.get("severity", "medium"),
        description=meta.get("description", ""),
        tags=meta.get("tags") or [],
        repeat=meta.get("repeat", 1),
        target=raw.get("target") or {},
        profile=raw.get("profile") or {},
        env_files=raw.get("env_files") or {},
        steps=raw.get("steps") or [],
        bridge=raw.get("bridge") or {"applicable": False, "compare": "none"},
        scenario=scenario,
        source_path=str(path),
    )


def discover_test_cases(
    cases_dir: pathlib.Path,
    *,
    category: str | None = None,
    case_name: str | Sequence[str] | None = None,
) -> list[TestCase]:
    """掃描 cases 目錄，依分類與一個或多個案例名稱載入 YAML 測試案例。"""
    results: list[TestCase] = []
    if not cases_dir.is_dir():
        return results
    case_names = ({case_name} if isinstance(case_name, str) else set(case_name or ()))
    for yaml_path in sorted(cases_dir.glob("*.yaml")):
        if case_names and yaml_path.stem not in case_names:
            continue
        try:
            tc = load_test_case(yaml_path)
        except Exception as exc:
            print(f"⚠ 載入失敗 {yaml_path.name}: {exc}")
            continue
        if category and tc.category != category:
            continue
        results.append(tc)
    return results
