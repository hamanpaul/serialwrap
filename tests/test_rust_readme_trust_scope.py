"""信任範圍用語守則（OD-12，P3-T18）。

README.md、sw_core/assets/skill/*.md 與 contracts/cli_help/**/*.json 的文字不得出現
「同使用者隔離」「same-user isolation」。OD-12 的信任範圍是「整機本機使用者」，文件與 help
不得宣稱同使用者隔離。

紅燈說明（P3-T18，待擁有者裁決）：依計畫的字面斷言，現行 README.md 第 1203、2948 行與
sw_core/assets/skill/SKILL_WINDOWS.md 第 166 行的「免責」用語（如「does not provide same-user
isolation」「不代表…同使用者隔離」）會命中。本測試依計畫保留字面斷言，以 strict xfail
標示；若擁有者裁定免責句允許，應另開 `test(oracle):` commit 放寬斷言，不得在本檔以
無聲刪除命中的方式通過。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("同使用者隔離", "same-user isolation")
pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.memory_only,
    pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="P3-T18 紅燈：既有免責用語命中 README.md:1203/2948、SKILL_WINDOWS.md:166，待擁有者裁決",
    ),
]


def _scan_targets() -> list[Path]:
    out = [ROOT / "README.md"]
    out.extend(sorted((ROOT / "sw_core" / "assets" / "skill").glob("*.md")))
    out.extend(sorted((ROOT / "contracts" / "cli_help").rglob("*.json")))
    return out


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _hits() -> list[str]:
    hits: list[str] = []
    for path in _scan_targets():
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            texts = [(f"{rel}#{i}", s) for i, s in enumerate(_strings(payload), 1)]
        else:
            texts = [(f"{rel}:{i}", line) for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)]
        for where, text in texts:
            low = text.lower()
            for phrase in FORBIDDEN:
                if phrase in low:
                    hits.append(f"{where}: {phrase}")
    return hits


def test_no_same_user_isolation_claim_in_docs_and_help():
    assert _scan_targets(), "掃描目標為空，證據不足"
    hits = _hits()
    assert not hits, f"信任範圍用語命中（OD-12）：{hits}"
