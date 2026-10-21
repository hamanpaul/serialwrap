"""Slice 2.1 完成校準報告的結構與分母一致性測試（P5-T23，§12.2、OD-02、OD-13）。

只讀取文件，不啟動 daemon、不接真實序列埠。

報告格式契約（Task 23 Step 3 實作者必須遵守）：
- `docs/rust-refactor/slice21-calibration.md` 含下列 7 個固定 `## ` 章節標題（順序不限）。
- `## 承接率` 章節內，以單行「分母：<整數>」宣告分母（數字可由反引號或 `*` 包裹）；
  所有宣告值必須等於 `tests/acceptance/breaker/slice_2.1.json` 的 `denominator`（Task 0b 凍結值）。
- `## OD-02 24 週上限輸入` 章節含「規劃估計」字樣（標明為規劃估計、非承諾）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "rust-refactor" / "slice21-calibration.md"
FROZEN = ROOT / "tests" / "acceptance" / "breaker" / "slice_2.1.json"

FIXED_SECTIONS = [
    "實測工作量",
    "Bench 等待",
    "安全里程碑",
    "平台缺陷",
    "回歸重跑",
    "承接率",
    "OD-02 24 週上限輸入",
]

# 承接率章節內宣告分母的唯一格式：「分母：<整數>」（允許反引號或 * 包裹）。
_DENOM_RE = re.compile(r"分母\s*[:：]\s*[`*]*(\d+)")


def _read_report() -> str:
    assert REPORT.is_file(), f"缺少 Slice 2.1 校準報告：{REPORT}（P5-T23 Step 3 尚未實作）"
    return REPORT.read_text(encoding="utf-8")


def _section_body(text: str, title: str) -> str | None:
    """回傳 `## <title>` 章節本文（至下一個 `## ` 標題或檔尾）；標題不存在時回傳 None。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == f"## {title}":
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if re.match(r"^## (?!#)", line):
            break
        body.append(line)
    return "\n".join(body)


def _frozen_denominator() -> int:
    assert FROZEN.is_file(), f"缺少 Task 0b 凍結檔：{FROZEN}（slice_2.1.json 須先由 Task 0b 產生）"
    data = json.loads(FROZEN.read_text(encoding="utf-8"))
    return data["denominator"]


def test_calibration_report_file_exists():
    assert REPORT.is_file(), f"缺少 Slice 2.1 校準報告：{REPORT}"


@pytest.mark.parametrize("title", FIXED_SECTIONS)
def test_fixed_section_heading_present(title):
    text = _read_report()
    assert _section_body(text, title) is not None, f"校準報告缺少固定章節標題 `## {title}`"


def test_case_uptake_denominator_matches_frozen_slice():
    text = _read_report()
    body = _section_body(text, "承接率")
    assert body is not None, "校準報告缺少 `## 承接率` 章節"
    declared = [int(m) for m in _DENOM_RE.findall(body)]
    assert declared, "`## 承接率` 章節未以「分母：<整數>」宣告分母"
    denominator = _frozen_denominator()
    assert all(d == denominator for d in declared), (
        f"`## 承接率` 宣告的分母 {declared} 與 slice_2.1.json.denominator={denominator} 不一致"
    )


def test_od02_section_marks_planning_estimate():
    text = _read_report()
    body = _section_body(text, "OD-02 24 週上限輸入")
    assert body is not None, "校準報告缺少 `## OD-02 24 週上限輸入` 章節"
    assert "規劃估計" in body, "`## OD-02 24 週上限輸入` 章節未標明「規劃估計」（非承諾）"
