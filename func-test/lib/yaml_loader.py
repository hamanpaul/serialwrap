"""YAML test case 載入與驗證。"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class TestCase:
    """一個完整的功能測試案例。"""
    name: str
    category: str
    severity: str
    description: str
    tags: list[str] = field(default_factory=list)
    repeat: int = 1
    target: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    env_files: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    source_path: str = ""


def load_test_case(path: pathlib.Path) -> TestCase:
    """從 YAML 檔案載入 TestCase。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: YAML 頂層必須是 dict")

    meta = raw.get("meta") or {}
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
        source_path=str(path),
    )


def discover_test_cases(
    cases_dir: pathlib.Path,
    *,
    category: str | None = None,
    case_name: str | None = None,
) -> list[TestCase]:
    """掃描 cases 目錄，載入所有 .yaml 測試案例。"""
    results: list[TestCase] = []
    if not cases_dir.is_dir():
        return results
    for yaml_path in sorted(cases_dir.glob("*.yaml")):
        if case_name and yaml_path.stem != case_name:
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
