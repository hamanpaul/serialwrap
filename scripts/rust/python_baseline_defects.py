#!/usr/bin/env python3
"""OD-75：Python daemon 基線缺陷登記。

空登記不讀清單。非空時 defect_id 必須出現在清單 B 段（`## B.` 到下一個 `## `）。
比對用 re.fullmatch，並套用 re.DOTALL，與 L15_OWNER_DEFERRED 相同。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "python-baseline-defects.v1"
ENV_REGISTRY = "SERIALWRAP_PYTHON_BASELINE_DEFECTS"
ENV_LEDGER = "SERIALWRAP_STAGE1_LEDGER"
_DEFECT_ID = re.compile(r"B[1-9][0-9]*")
_LEDGER_ID = re.compile(r"^\|\s*(B[1-9][0-9]*)\s*\|")
_PLATFORMS = frozenset({"linux", "windows"})
_FIELDS = frozenset({
    "defect_id", "l15_case_id", "nodeid", "platform", "allowed_statuses",
    "message_regex", "root_cause", "od75_basis",
})
_REQUIRED = ("defect_id", "platform", "allowed_statuses", "message_regex", "root_cause", "od75_basis")


class BaselineDefectRegistryError(ValueError):
    """登記檔缺欄、regex 無效，或 defect_id 不在清單 B 段。"""


@dataclass(frozen=True)
class DefectEntry:
    defect_id: str
    l15_case_id: str | None
    nodeid: str | None
    platform: str
    allowed_statuses: frozenset[str]
    message_regex: re.Pattern[str]
    root_cause: str
    od75_basis: str


@dataclass(frozen=True)
class Registry:
    entries: tuple[DefectEntry, ...]

    def match_l15(
        self,
        case_id: object,
        platform: object,
        status: object,
        message: object,
        diagnostics: object = None,
    ) -> DefectEntry | None:
        if not isinstance(case_id, str) or not isinstance(platform, str):
            return None
        if not isinstance(status, str) or not isinstance(message, str):
            return None
        if _harness_stop_failed(diagnostics):
            return None
        hits = [
            entry for entry in self.entries
            if entry.l15_case_id == case_id
            and entry.platform == platform
            and status in entry.allowed_statuses
            and entry.message_regex.fullmatch(message) is not None
        ]
        return hits[0] if len(hits) == 1 else None

    def match_node(
        self,
        nodeid: object,
        platform: object,
        status: object,
        message: object,
    ) -> DefectEntry | None:
        if not isinstance(nodeid, str) or not nodeid:
            return None
        if not isinstance(platform, str) or not isinstance(status, str) or not isinstance(message, str):
            return None
        hits = [
            entry for entry in self.entries
            if entry.nodeid is not None
            and entry.platform == platform
            and status in entry.allowed_statuses
            and entry.message_regex.fullmatch(message) is not None
            and nodeids_equal(entry.nodeid, nodeid)
        ]
        return hits[0] if len(hits) == 1 else None


def _harness_stop_failed(diagnostics: object) -> bool:
    return isinstance(diagnostics, list) and any(
        isinstance(item, dict) and item.get("kind") == "harness_stop_failed" for item in diagnostics
    )


def normalize_nodeid(nodeid: str) -> str:
    """轉成 JUnit 的完整 classname::name，保留目錄、類別與參數。"""
    base, bracket, params = nodeid.partition("[")
    names = base.split("::")
    if len(names) < 2:
        return nodeid
    path = names[0].removeprefix("./").removesuffix(".py").replace("/", ".")
    classname = ".".join([path, *names[1:-1]])
    return f"{classname}::{names[-1]}{bracket}{params}"


def nodeids_equal(registered: str, observed: str) -> bool:
    if registered == observed:
        return True
    left = normalize_nodeid(registered)
    right = normalize_nodeid(observed)
    return bool(left) and left == right


def ledger_ids(text: str) -> set[str]:
    """只讀 `## B.` 段表格第一欄的 B 編號。"""
    found: set[str] = set()
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.startswith("## B.")
            continue
        if not in_section:
            continue
        match = _LEDGER_ID.match(line)
        if match:
            found.add(match.group(1))
    return found


def default_registry_path(repo_root: Path) -> Path:
    env = os.environ.get(ENV_REGISTRY)
    if env:
        return Path(env)
    return repo_root / "tests" / "acceptance" / "python_baseline_defects.json"


def resolve_ledger_path(repo_root: Path) -> Path | None:
    """環境變數有設時只用該路徑，不再改看其他清單。"""
    if ENV_LEDGER in os.environ:
        raw = os.environ[ENV_LEDGER]
        return Path(raw) if raw else None
    # 回退清單在使用者家目錄下。不以個人絕對路徑字面寫入，避免 R-21 structural。
    candidates = (
        repo_root / "docs" / "rust-refactor" / "reviews" / "backlog" / "stage1-ledger.md",
        Path.home() / "b-log" / "rust-refactor" / "exec" / "stage1-ledger.md",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _parse_entry(item: Any, index: int) -> DefectEntry:
    where = f"defects[{index}]"
    if not isinstance(item, dict):
        raise BaselineDefectRegistryError(f"{where} 不是物件")
    unknown = sorted(set(item) - _FIELDS)
    if unknown:
        raise BaselineDefectRegistryError(f"{where} 有未知欄位：{unknown}")
    missing = [name for name in _REQUIRED if name not in item]
    has_l15 = "l15_case_id" in item
    has_node = "nodeid" in item
    if has_l15 == has_node:
        missing.append("l15_case_id 或 nodeid（須恰有一個）")
    if missing:
        raise BaselineDefectRegistryError(f"{where} 缺少欄位：{', '.join(missing)}")
    defect_id = item["defect_id"]
    if not isinstance(defect_id, str) or _DEFECT_ID.fullmatch(defect_id) is None:
        raise BaselineDefectRegistryError(f"{where} 的 defect_id 不是清單 B 段編號")
    platform = item["platform"]
    if platform not in _PLATFORMS:
        raise BaselineDefectRegistryError(f"{where} 的 platform 必須是 linux 或 windows")
    statuses = item["allowed_statuses"]
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(isinstance(status, str) and status for status in statuses)
    ):
        raise BaselineDefectRegistryError(f"{where} 的 allowed_statuses 必須是非空字串陣列")
    regex = item["message_regex"]
    if not isinstance(regex, str) or not regex:
        raise BaselineDefectRegistryError(f"{where} 的 message_regex 必須是非空字串")
    try:
        compiled = re.compile(regex, re.DOTALL)
    except re.error as exc:
        raise BaselineDefectRegistryError(f"{where} 的 message_regex 無效：{exc}") from exc
    root_cause = item["root_cause"]
    basis = item["od75_basis"]
    if not isinstance(root_cause, str) or not root_cause.strip():
        raise BaselineDefectRegistryError(f"{where} 的 root_cause 必須是非空字串")
    if not isinstance(basis, str) or "OD-75" not in basis:
        raise BaselineDefectRegistryError(f"{where} 的 od75_basis 必須引用 OD-75")
    l15_case_id = item.get("l15_case_id")
    nodeid = item.get("nodeid")
    if has_l15 and (not isinstance(l15_case_id, str) or not l15_case_id):
        raise BaselineDefectRegistryError(f"{where} 的 l15_case_id 必須是非空字串")
    if has_node and (not isinstance(nodeid, str) or not nodeid):
        raise BaselineDefectRegistryError(f"{where} 的 nodeid 必須是非空字串")
    return DefectEntry(
        defect_id=defect_id,
        l15_case_id=l15_case_id if has_l15 else None,
        nodeid=nodeid if has_node else None,
        platform=platform,
        allowed_statuses=frozenset(statuses),
        message_regex=compiled,
        root_cause=root_cause,
        od75_basis=basis,
    )


def load_registry(
    path: Path,
    *,
    ledger_path: Path | None = None,
    repo_root: Path | None = None,
) -> Registry:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineDefectRegistryError(f"無法讀取登記檔：{exc}") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(document.get("defects"), list)
    ):
        raise BaselineDefectRegistryError("登記檔 schema 不符或缺少 defects")
    entries = tuple(_parse_entry(item, index) for index, item in enumerate(document["defects"]))
    if entries:
        root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
        ledger = ledger_path if ledger_path is not None else resolve_ledger_path(root)
        if ledger is None or not ledger.is_file():
            raise BaselineDefectRegistryError("登記非空但找不到 Stage 1 清單 B 段")
        try:
            known = ledger_ids(ledger.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise BaselineDefectRegistryError(f"無法讀取清單：{exc}") from exc
        for entry in entries:
            if entry.defect_id not in known:
                raise BaselineDefectRegistryError(f"清單 B 段沒有 {entry.defect_id}")
    return Registry(entries)


def load_for_gate(
    repo_root: Path,
    *,
    registry_path: Path | None = None,
    ledger_path: Path | None = None,
) -> Registry:
    path = registry_path if registry_path is not None else default_registry_path(repo_root)
    if not path.is_file():
        raise BaselineDefectRegistryError(f"找不到登記檔：{path}")
    return load_registry(path, ledger_path=ledger_path, repo_root=repo_root)
