"""`.project-policy.yml` 的 OD-06／R-26 宣告、preflight 步驟與契約治理文件的失敗測試（P0-T12，TDD 紅燈階段）。

本檔只讀設定與文件，不開行程、不開 socket，故模組層標 `core_portable`＋`memory_only`（T14 預設）。
本檔不得在模組頂層 import `tools.contracts.*`，一律在函式內延遲 import，
讓整個 suite 仍可完成 collection。
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

# `python3 -m tools.contracts` 的五個子命令（與 cli.py 的子命令表逐字相同）。
_SUBCOMMANDS = ("extract", "check", "summary", "skill-section", "effective")


def _policy():
    return yaml.safe_load((ROOT / ".project-policy.yml").read_text(encoding="utf-8"))


def _collect_catalog_values(node, out: set[str]) -> None:
    """遞迴收集 YAML 節點中 `code`／`value` 鍵的字串值；動態前綴（`X:*`）同時收前綴。"""
    if isinstance(node, dict):
        for key, val in node.items():
            if key in ("code", "value") and isinstance(val, str):
                out.add(val)
                out.add(val.split(":", 1)[0])
            _collect_catalog_values(val, out)
    elif isinstance(node, list):
        for item in node:
            _collect_catalog_values(item, out)


def _catalog_code_values() -> set[str]:
    """抽出 `contracts/` 三份目錄中的碼值。

    直接讀 YAML、不呼叫 `tools.contracts` loader，使本測試不綁定驗證器實作細節；
    只收 `code`／`value` 鍵，不收 meaning／agent_action 等自由文字。
    """
    values: set[str] = set()
    for name in ("error_codes.yaml", "session_last_errors.yaml", "diagnostic_aux.yaml"):
        doc = yaml.safe_load((ROOT / "contracts" / name).read_text(encoding="utf-8"))
        _collect_catalog_values(doc, values)
    return values


def test_diagnostic_codes_declared_for_both_namespaces():
    decl = {d["namespace"]: d for d in _policy()["diagnostic_codes"]}
    assert set(decl) == {"error_code", "session_last_error"}
    for d in decl.values():
        assert d["extract"].startswith("python3 -m tools.contracts extract")
        assert all((ROOT / p).is_file() for p in d["docs"])
        assert {"code", "meaning", "action"} <= set(re.compile(d["entry_pattern"]).groupindex)


def test_doc_coverage_local_mirror_of_r27_passes():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    val = importlib.import_module("tools.contracts.validate")
    findings, _ = val.run_all(ROOT)
    assert [f for f in findings if f.check in ("C13", "C14")] == []


def test_generated_facts_cover_both_skills():
    facts = _policy()["generated_facts"]
    pairs = {(f["reflected_in"], f["marker"]) for f in facts}
    expected = {
        (skill, marker)
        for skill in ("sw_core/assets/skill/SKILL.md", "sw_core/assets/skill/SKILL_WINDOWS.md")
        for marker in ("error-codes", "session-last-errors")
    }
    assert pairs == expected


def test_contracts_in_code_paths():
    assert "contracts/**" in _policy()["code_paths"]


def test_preflight_has_contracts_step_before_tests():
    steps = _policy()["preflight"]["steps"]
    names = [s["name"] for s in steps]
    assert "contracts" in names, f"preflight.steps 缺少 contracts 步驟：{names}"
    assert "tests" in names, f"preflight.steps 缺少 tests 步驟：{names}"
    assert names.index("contracts") < names.index("tests"), names
    contracts_step = steps[names.index("contracts")]
    assert contracts_step["argv"] == ["python3", "-m", "tools.contracts", "check"]


def test_policy_rule_declaration_has_no_code_list():
    # 先取宣告區塊：尚未宣告時於此以 KeyError 紅燈。
    block = _policy()["diagnostic_codes"]
    block_text = json.dumps(block, ensure_ascii=False, sort_keys=True)
    tokens = set(re.findall(r"[A-Za-z0-9_]+", block_text))
    leaked = sorted(tokens & _catalog_code_values())
    assert leaked == [], f"diagnostic_codes 宣告不得內含目錄碼值（OD-06，只放抽取方式與文件位置）：{leaked}"


def test_claude_md_has_contract_governance_section():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## 契約治理政策" in text
    assert "python3 -m tools.contracts check" in text


def test_governance_doc_lists_all_checks():
    doc = ROOT / "docs" / "contracts-governance.md"
    assert doc.is_file(), "docs/contracts-governance.md 尚未建立（T12 Step 4）"
    text = doc.read_text(encoding="utf-8")
    missing_checks = [f"C{n:02d}" for n in range(1, 20) if not re.search(rf"\bC{n:02d}\b", text)]
    assert missing_checks == [], f"契約治理文件缺少檢查代號：{missing_checks}"
    # 子命令以完整呼叫形式出現，避免只以裸字（check、summary）在散文中偶然命中。
    missing_subs = [s for s in _SUBCOMMANDS if f"python3 -m tools.contracts {s}" not in text]
    assert missing_subs == [], f"契約治理文件缺少子命令：{missing_subs}"
