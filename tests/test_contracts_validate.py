"""契約驗證器（`tools/contracts/validate.py`、`cli.py`）的失敗測試（P0-T6，TDD 紅燈階段）。

合成案例一律在 `tmp_path` 建立最小 repo（目錄 YAML、`code_extraction.yaml`、`pkg/`、`tests/`），
直接呼叫對應的 `check_*` 函式，並以 finding 代號（C02～C19）篩選斷言。
本檔不得在模組頂層 import `tools.contracts.*`，一律以 `_load()` 延遲 import，
讓整個 suite 仍可完成 collection。
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# 與 `.project-policy.yml` 的 diagnostic_codes.entry_pattern 逐字相同（T12）。
_ENTRY_PATTERN = (
    r"^\| `(?P<code>[^`]+)` \| [^|\n]* \| [^|\n]* \| "
    r"(?P<meaning>[^|\n]*\S[^|\n]*) \| (?P<action>[^|\n]*\S[^|\n]*) \|$"
)

# §11.3 六項安全不變量 ID（與 SAFETY_INVARIANTS 逐字相同）。
_SAFETY_IDS = (
    "zero_tx_on_reject",
    "no_forbidden_interleave",
    "no_stale_context_replay",
    "no_reopen_before_handoff",
    "no_auto_resend",
    "insufficient_evidence_not_pass",
)

_DUMMY_TEST = "tests/test_dummy.py"
_DUMMY_TEST_SRC = "def test_placeholder():\n    assert True\n"


# Phase 出口測試 test_real_repo_contract_check_passes 由 T12 Step 1 追加到本檔，
# 避免 T6～T11 期間長時間紅燈遮蔽各 Task Step 6 的全套結果。


def _load(name: str):
    """延遲 import `tools.contracts.<name>`；前置 Task 未完成時在這裡以 ModuleNotFoundError 失敗。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module(f"tools.contracts.{name}")


def _find(findings, check: str):
    return [f for f in findings if f.check == check]


def test_summary_counts_are_data_derived():
    cat_mod, ex_mod, val = _load("catalog"), _load("extract"), _load("validate")
    cat = cat_mod.load_catalogs(ROOT)
    res = ex_mod.extract(ROOT, ex_mod.load_extraction_config(ROOT / "contracts" / "code_extraction.yaml"))
    s = val.summarize(cat, res)
    assert s["error_code"]["catalog_total"] == len(cat.error_codes)
    assert s["session_last_error"]["catalog_total"] == len(cat.last_errors)
    assert s["error_code"]["passthrough"] == len(cat.passthrough)


def test_no_hardcoded_78_anywhere_in_validator():
    files = sorted((ROOT / "tools" / "contracts").glob("*.py"))
    # 紅燈階段目錄尚不存在；空清單不得被視為通過。
    assert files, "tools/contracts 尚無 .py 檔，無法檢查是否硬編碼計數"
    for p in files:
        assert not re.search(r"\b78\b", p.read_text(encoding="utf-8")), p


# ---------------------------------------------------------------------------
# 合成 repo 建構輔助
# ---------------------------------------------------------------------------


def _code(code: str, **over):
    """回傳一筆合法的 error_codes.yaml entry（預設：EXISTING、reachable、rpc 介面）。"""
    entry = {
        "code": code,
        "status": "EXISTING",
        "reachable": True,
        "scope": {"interfaces": ["rpc"], "methods": ["health.ping"], "platforms": ["posix", "windows"]},
        "meaning": "測試用說明",
        "side_effect": "無",
        "retryable": "no",
        "uart_tx": "none",
        "agent_action": "測試用處置",
        "extra_fields": [],
        "contract_tests": [],
        "behavior_change": [],
    }
    entry.update(over)
    if entry["reachable"] is False:
        entry.setdefault("unreachable_reason", "測試用不可達原因")
    return entry


def _passthrough():
    return [
        {
            "id": "last_error_passthrough",
            "namespace_ref": "session_last_errors",
            "scope": {"interfaces": ["rpc"], "methods": ["session.attach"], "platforms": ["posix", "windows"]},
            "rule": "測試用 passthrough 規則",
            "owner_confirmation": "pending_default",
            "reconciliation_refs": ["RPC-D-02"],
        }
    ]


def _lerr(value: str, **over):
    """回傳一筆合法的 session_last_errors.yaml value entry（預設：named、allowed_shared）。"""
    entry = {
        "value": value,
        "kind": "named",
        "reason_forms": [],
        "reachable": True,
        "unreachable_reason": None,
        "meaning": "測試用意義",
        "agent_action": "測試用處置",
        "terminal": False,
        "auto_reprobe": {"attached": True, "detached": False},
        "manual_recovery": "測試用手動復原",
        "logging": "無",
        "detail": "無",
        "states": ["ATTACHED"],
        "set_by": ["測試用產生點"],
        "cleared_by": ["auto:測試用清除@pkg/mod.py:1"],
        "promoted_via": [],
        "error_code_namespace": "allowed_shared",
        "contract_tests": [],
    }
    entry.update(over)
    return entry


_APPROVALS = {
    "BC": {"owner": "hamanpaul", "date": "2026-10-15", "decision_ref": "OD-04", "basis": "owner_decision"},
    "SC": {
        "owner": "spec-final",
        "date": "2026-10-15",
        "decision_ref": "serialwrap-rust-spec-final.md §9.1",
        "basis": "spec_final",
    },
    "DF": {
        "owner": "spec-reconciliation",
        "date": "2026-10-15",
        "decision_ref": "spec-reconciliation.md §1.2",
        "basis": "reconciliation",
    },
}


def _change(cid: str, status: str = "APPROVED"):
    """回傳一筆合法的 behavior_changes.yaml change（approval 依 BC／SC／DF 前綴填寫）。"""
    kind = cid[:2]
    return {
        "id": cid,
        "kind": kind,
        "title": "測試用變更",
        "status": status,
        "approval": dict(_APPROVALS[kind]),
        "owner_confirmation": "not_required",
        "owner_item": None,
        "default_if_pending": None,
        "applies_to": {"python": None, "rust": {"stage": "stage-1a"}},
        "scope": "rust_only",
        "ab_participation": "absolute_only",
        "approved_at_revision": 1,
        "spec_refs": ["§9.1"],
        "reconciliation_refs": [],
        "phases": ["P1"],
        "case_ids": [cid.lower() + "-case"],
        "compatibility_impact": "測試用相容性影響",
        "approval_conditions": "測試用核准條件",
        "related_decisions": ["OD-04"],
    }


def _change_order(c):
    return ({"BC": 0, "SC": 1, "DF": 2}[c["id"][:2]], c["id"])


def _invariants():
    return [
        {"id": i, "title": "測試用不變量", "spec_ref": "§11.3", "error_codes": [], "case_ids": []}
        for i in _SAFETY_IDS
    ]


def _extraction_config():
    """合成 repo 用的 code_extraction.yaml（sink／relay／passthrough 規則與 T2 相同，roots 改指向 pkg/）。"""
    return {
        "schema_version": 1,
        "roots": ["pkg/**/*.py"],
        "exclude": [],
        "code_pattern": r"^[A-Z][A-Z0-9_]*[A-Z0-9]$",
        "dynamic_prefix_pattern": r"^[A-Z][A-Z0-9_]*:",
        "max_depth": 12,
        "exclude_functions": [],
        "namespaces": {
            "error_code": {
                "sinks": [
                    {"kind": "dict_key", "key": "error_code"},
                    {"kind": "subscript_store", "key": "error_code"},
                    {"kind": "call_keyword", "keyword": "error_code"},
                    {"kind": "param_default", "param": "error_code"},
                    {"kind": "call_arg", "callee": "TunnelError", "index": 0},
                    {"kind": "call_arg", "callee": "_EndpointResolutionError", "index": 0},
                    {
                        "kind": "call_arg",
                        "callee": "RuntimeError",
                        "index": 0,
                        "value_pattern": r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$",
                        "implies_interface": "proc",
                    },
                ],
                "relays": [
                    {"kind": "call_method_arg", "method": "get", "arg": "error_code"},
                    {"kind": "subscript_load", "key": "error_code"},
                    {"kind": "attribute_load", "attr": "code"},
                ],
                "passthrough": [
                    {"kind": "attribute_load", "attr": "last_error", "emits": "last_error_passthrough"},
                    {
                        "kind": "call_method_arg",
                        "method": "get",
                        "arg": "last_error",
                        "emits": "last_error_passthrough",
                    },
                    {"kind": "subscript_load", "key": "last_error", "emits": "last_error_passthrough"},
                ],
            },
            "session_last_error": {
                "sinks": [{"kind": "attribute_store", "attr": "last_error"}],
                "relays": [
                    {"kind": "attribute_load", "attr": "last_error"},
                    {"kind": "call_method_arg", "method": "get", "arg": "last_error"},
                ],
                "passthrough": [],
            },
        },
        "allow_unresolved": [],
        "exclude_values": [],
    }


def _dump_yaml(path: Path, obj) -> None:
    path.write_text(yaml.safe_dump(obj, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write(path: Path, src: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(src), encoding="utf-8")


def _make_repo(
    root: Path,
    *,
    codes=(),
    last_errors=(),
    changes=(),
    rpc=None,
    cli=None,
    pkg=None,
    tests=None,
    config=None,
    policy=None,
) -> None:
    """在 root 建立一個結構合法的合成 repo；每個參數皆可覆寫對應檔案。"""
    contracts = root / "contracts"
    contracts.mkdir(parents=True)
    _dump_yaml(
        contracts / "error_codes.yaml",
        {
            "schema_version": 1,
            "revision": 1,
            "namespace": "error_code",
            "codes": sorted((copy.deepcopy(c) for c in codes), key=lambda c: c["code"]),
            "passthrough": _passthrough(),
        },
    )
    _dump_yaml(
        contracts / "session_last_errors.yaml",
        {
            "schema_version": 1,
            "revision": 1,
            "namespace": "session_last_error",
            "values": sorted((copy.deepcopy(e) for e in last_errors), key=lambda e: e["value"]),
        },
    )
    _dump_yaml(
        contracts / "behavior_changes.yaml",
        {
            "schema_version": 1,
            "revision": 1,
            "changes": sorted((copy.deepcopy(c) for c in changes), key=_change_order),
            "pending_owner_confirmations": [],
            "safety_invariants": _invariants(),
        },
    )
    _dump_yaml(
        contracts / "diagnostic_aux.yaml",
        {
            "schema_version": 1,
            "revision": 1,
            "warning_codes": [],
            "error_details": [],
            "classifications": [],
            "recommended_actions": [],
            "recovery_actions": [],
            "recover_actions": [],
        },
    )
    _dump_json(
        contracts / "rpc_schema.json",
        rpc if rpc is not None else {"$defs": {}, "x-methods": {}, "x-pending": []},
    )
    _dump_json(
        contracts / "cli_schema.json",
        cli if cli is not None else {"x-exit-codes": [], "x-pending": []},
    )
    _dump_yaml(contracts / "code_extraction.yaml", config if config is not None else _extraction_config())

    for rel, src in (pkg if pkg is not None else {"pkg/__init__.py": ""}).items():
        _write(root / rel, src)
    for rel, src in (tests if tests is not None else {_DUMMY_TEST: _DUMMY_TEST_SRC}).items():
        _write(root / rel, src)
    if policy is not None:
        _dump_yaml(root / ".project-policy.yml", policy)


def _catalog(root: Path):
    return _load("catalog").load_catalogs(root)


def _context(root: Path):
    ex = _load("extract")
    cat = _load("catalog").load_catalogs(root)
    res = ex.extract(root, ex.load_extraction_config(root / "contracts" / "code_extraction.yaml"))
    return cat, res


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tools.contracts", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )


# ---------------------------------------------------------------------------
# 抽取比對：C02 C03 C04 C06 C07 C08 C19
# ---------------------------------------------------------------------------


def test_c02_emitted_not_in_catalog(tmp_path):
    _make_repo(
        tmp_path,
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "GHOST_CODE"}
            """
        },
    )
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_catalog_against_emissions(cat, res), "C02")
    assert [f.subject for f in hits] == ["GHOST_CODE"]


def test_c03_approved_but_emitted(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("NEW_CODE", status="APPROVED", reachable=False, behavior_change=["SC-01"])],
        changes=[_change("SC-01", status="APPROVED")],
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "NEW_CODE"}
            """
        },
    )
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_catalog_against_emissions(cat, res), "C03")
    assert [f.subject for f in hits] == ["NEW_CODE"]


def test_c04_effective_but_not_emitted(tmp_path):
    _make_repo(tmp_path, codes=[_code("OLD_CODE")])
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_catalog_against_emissions(cat, res), "C04")
    assert [f.subject for f in hits] == ["OLD_CODE"]


def test_c04_unreachable_not_flagged(tmp_path):
    _make_repo(tmp_path, codes=[_code("OLD_CODE", reachable=False)])
    cat, res = _context(tmp_path)
    assert _find(_load("validate").check_catalog_against_emissions(cat, res), "C04") == []


def test_c06_proc_interface_missing(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("DAEMON_X")],  # interfaces 只含 rpc
        pkg={
            "pkg/mod.py": """
            def boot():
                raise RuntimeError("DAEMON_X")
            """
        },
    )
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_catalog_against_emissions(cat, res), "C06")
    assert [f.subject for f in hits] == ["DAEMON_X"]


def test_c07_unresolved_reported(tmp_path):
    _make_repo(
        tmp_path,
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": unknown()}
            """
        },
    )
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_extraction_hygiene(cat, res), "C07")
    assert len(hits) == 1


def test_c08_stale_exclude(tmp_path):
    cfg = _extraction_config()
    cfg["exclude_values"] = [
        {"namespace": "error_code", "value": "STALE_OK", "category": "not_a_code", "reason": "測試用過期排除"}
    ]
    _make_repo(tmp_path, config=cfg)  # pkg 不輸出任何碼，排除項即為過期
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_extraction_hygiene(cat, res), "C08")
    assert len(hits) == 1


def test_c19_exclusion_warned(tmp_path):
    cfg = _extraction_config()
    cfg["exclude_values"] = [
        {"namespace": "error_code", "value": "FOO", "category": "not_a_code", "reason": "測試用非碼"}
    ]
    _make_repo(
        tmp_path,
        config=cfg,
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "FOO"}
            """
        },
    )
    cat, res = _context(tmp_path)
    findings = _load("validate").check_extraction_hygiene(cat, res)
    hits = _find(findings, "C19")
    assert [(f.severity, f.subject) for f in hits if f.severity == "WARN"] == [("WARN", "FOO")]
    # ok 只看 FAIL：WARN 不得使整體判定失敗。
    assert not [f for f in findings if f.severity == "FAIL"]

    all_findings, summary = _load("validate").run_all(tmp_path)
    assert not any(f.severity == "FAIL" for f in all_findings)
    assert any(f.check == "C19" and f.severity == "WARN" for f in all_findings)

    proc = _run_cli("check", "--repo", str(tmp_path))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["ok"] is True


def test_c19_exclusion_hides_catalog_value(tmp_path):
    cfg = _extraction_config()
    cfg["exclude_values"] = [
        {"namespace": "error_code", "value": "OLD_CODE", "category": "not_a_code", "reason": "測試用錯誤排除"}
    ]
    _make_repo(
        tmp_path,
        codes=[_code("OLD_CODE")],
        config=cfg,
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "OLD_CODE"}
            """
        },
    )
    cat, res = _context(tmp_path)
    hits = _find(_load("validate").check_extraction_hygiene(cat, res), "C19")
    fails = [f for f in hits if f.severity == "FAIL"]
    assert [f.subject for f in fails] == ["OLD_CODE"]


# ---------------------------------------------------------------------------
# 契約測試：C09
# ---------------------------------------------------------------------------


def test_c09_missing_contract_test(tmp_path):
    _make_repo(tmp_path, codes=[_code("NEW_OK", contract_tests=[])])
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert [f.subject for f in hits] == ["NEW_OK"]


def test_c09_test_file_lacks_code(tmp_path):
    _make_repo(tmp_path, codes=[_code("NEW_OK", contract_tests=[_DUMMY_TEST])])  # 檔內無 NEW_OK
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert [f.subject for f in hits] == ["NEW_OK"]


def test_c09_node_does_not_exist(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("NEW_OK", contract_tests=[f"{_DUMMY_TEST}::test_does_not_exist"])],
        tests={
            _DUMMY_TEST: """
            def test_other():
                assert {"error_code": "NEW_OK"}["error_code"] == "NEW_OK"
            """
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert [f.subject for f in hits] == ["NEW_OK"]


def test_c09_code_only_in_comment(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("NEW_OK", contract_tests=[f"{_DUMMY_TEST}::test_comment_only"])],
        tests={
            _DUMMY_TEST: '''
            def test_comment_only():
                """NEW_OK 只出現在 docstring。"""
                # NEW_OK 只出現在註解
                assert True
            '''
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert [f.subject for f in hits] == ["NEW_OK"]


def test_c09_code_in_other_test_only(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("NEW_OK", contract_tests=[f"{_DUMMY_TEST}::test_a"])],
        tests={
            _DUMMY_TEST: """
            def test_a():
                assert True

            def test_b():
                assert {"error_code": "NEW_OK"}["error_code"] == "NEW_OK"
            """
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert [f.subject for f in hits] == ["NEW_OK"]


def test_c09_class_node_and_param_id(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("NEW_OK", contract_tests=[f"{_DUMMY_TEST}::TestX::test_y[case1]"])],
        tests={
            _DUMMY_TEST: """
            class TestX:
                def test_y(self):
                    assert {"error_code": "NEW_OK"}["error_code"] == "NEW_OK"
            """
        },
    )
    cat = _catalog(tmp_path)
    assert _find(_load("validate").check_contract_tests(cat, tmp_path), "C09") == []


# ---------------------------------------------------------------------------
# 交叉參照與狀態一致性：C10 C11 C12 C16
# ---------------------------------------------------------------------------


def test_c10_unknown_behavior_change(tmp_path):
    _make_repo(tmp_path, codes=[_code("NEW_OK", behavior_change=["SC-99"])])  # SC-99 不存在
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_cross_refs(cat), "C10")
    assert len(hits) == 1


def test_c11_forbidden_shared_name(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("X_ONLY")],
        last_errors=[_lerr("X_ONLY", error_code_namespace="forbidden")],
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_cross_refs(cat), "C11")
    assert [f.subject for f in hits] == ["X_ONLY"]


def test_c12_harness_prefix(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])
    base = _catalog(tmp_path)
    # loader 會拒絕 HARNESS_ 前綴，因此直接以 Catalogs 建構含該碼的物件。
    harness_entry = dataclasses.replace(base.error_codes[0], code="HARNESS_X")
    cat = dataclasses.replace(base, error_codes=(harness_entry,))
    hits = _find(_load("validate").check_cross_refs(cat), "C12")
    assert [f.subject for f in hits] == ["HARNESS_X"]


def test_c16_approved_code_with_implemented_change(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("NEW_OK", status="APPROVED", reachable=False, behavior_change=["SC-01"])],
        changes=[_change("SC-01", status="IMPLEMENTED")],
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_cross_refs(cat), "C16")
    assert len(hits) == 1


def test_c16_implemented_code_with_approved_change(tmp_path):
    _make_repo(
        tmp_path,
        codes=[
            _code(
                "NEW_OK",
                status="IMPLEMENTED",
                behavior_change=["SC-01"],
                contract_tests=[f"{_DUMMY_TEST}::test_new"],
            )
        ],
        changes=[_change("SC-01", status="APPROVED")],
        tests={
            _DUMMY_TEST: """
            def test_new():
                assert {"error_code": "NEW_OK"}["error_code"] == "NEW_OK"
            """
        },
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "NEW_OK"}
            """
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_cross_refs(cat), "C16")
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# schema 參照：C15 C17 C18
# ---------------------------------------------------------------------------


def test_c17_pending_not_folded(tmp_path):
    _make_repo(
        tmp_path,
        changes=[_change("SC-04", status="IMPLEMENTED")],
        rpc={
            "$defs": {},
            "x-methods": {},
            "x-pending": [{"id": "SC-04", "change": "SC-04", "adds": {"SessionPublic": ["drain_stalled"]}}],
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C17")
    assert len(hits) == 1


def test_c17_pending_missing_change(tmp_path):
    _make_repo(
        tmp_path,
        rpc={"$defs": {}, "x-methods": {}, "x-pending": [{"id": "SC-04"}]},  # 缺 change 欄
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C17")
    assert len(hits) == 1


def test_c18_empty_result_placeholder(tmp_path):
    _make_repo(
        tmp_path,
        rpc={
            "$defs": {},
            "x-methods": {
                "health.ping": {
                    "params": {},
                    "result": {},  # 空物件佔位，C18 必須擋下
                    "errors": [],
                    "error_order": [],
                    "side_effects": [],
                }
            },
            "x-pending": [],
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C18")
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# 文件覆蓋：C13（R-27 等價）
# ---------------------------------------------------------------------------


def test_c13_doc_coverage_missing(tmp_path):
    doc = tmp_path / "SKILL.md"
    doc.write_text("| `A_CODE` | 來源 | 行號 | 意義 A | 處置 A |\n", encoding="utf-8")
    findings = _load("validate").check_doc_coverage(["A_CODE", "B_CODE"], [doc], _ENTRY_PATTERN)
    hits = _find(findings, "C13")
    assert [f.subject for f in hits] == ["B_CODE"]


def test_c13_empty_action_not_counted(tmp_path):
    doc = tmp_path / "SKILL.md"
    doc.write_text(
        "| `A_CODE` | 來源 | 行號 | 意義 A | 處置 A |\n"
        "| `B_CODE` | 來源 | 行號 | 意義 B |  |\n",
        encoding="utf-8",
    )
    findings = _load("validate").check_doc_coverage(["A_CODE", "B_CODE"], [doc], _ENTRY_PATTERN)
    hits = _find(findings, "C13")
    assert [f.subject for f in hits] == ["B_CODE"]


def test_c13_require_in_any(tmp_path):
    d1 = tmp_path / "SKILL.md"
    d2 = tmp_path / "SKILL_WINDOWS.md"
    d1.write_text(
        "| `A_CODE` | 來源 | 行號 | 意義 A | 處置 A |\n| `B_CODE` | 來源 | 行號 | 意義 B | 處置 B |\n",
        encoding="utf-8",
    )
    d2.write_text("| `A_CODE` | 來源 | 行號 | 意義 A | 處置 A |\n", encoding="utf-8")  # B_CODE 只在 d1
    findings = _load("validate").check_doc_coverage(
        ["A_CODE", "B_CODE"], [d1, d2], _ENTRY_PATTERN, require_in="any"
    )
    assert _find(findings, "C13") == []


# R-27 語意對等（T13 Step 5）：與 paulsha-conventions `tests/fixtures/r27_parity/*.yaml` 逐字相同。
# 任一邊修改 fixture 必須同步另一邊；fixture 的 sha256 記於 PR 描述。
_R27_PARITY_DIR = ROOT / "tests" / "fixtures" / "r27_parity"
_R27_PARITY_FIXTURES = sorted(_R27_PARITY_DIR.glob("*.yaml"))


@pytest.mark.memory_only
def test_parity_fixtures_present():
    """守門測試：確保 parity fixture 存在且至少有 7 個，防止空集無聲 SKIP。"""
    assert len(_R27_PARITY_FIXTURES) >= 7
    expected_stems = {
        "01_all_documented",
        "02_one_missing",
        "03_empty_action",
        "04_require_in_all_two_docs",
        "05_require_in_any_one_doc",
        "06_comments_and_blank_lines",
        "07_require_in_any_both_missing",
        "08_padded_code_cell",
    }
    assert expected_stems.issubset({p.stem for p in _R27_PARITY_FIXTURES})


@pytest.mark.memory_only
@pytest.mark.parametrize("fixture_path", _R27_PARITY_FIXTURES, ids=lambda p: p.stem)
def test_c13_parity_fixtures(tmp_path, fixture_path):
    spec = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    docs: list[Path] = []
    for name, text in spec["docs"].items():
        doc = tmp_path / name
        doc.write_text(text, encoding="utf-8")
        docs.append(doc)
    codes = list(spec["codes"])
    findings = _load("validate").check_doc_coverage(
        codes, docs, _ENTRY_PATTERN, require_in=spec.get("require_in", "all")
    )
    hits = _find(findings, "C13")
    expected = spec["expected"]
    assert ("FAIL" if hits else "PASS") == expected["status"], fixture_path.name
    assert {f.subject for f in hits} == set(expected["missing"]), fixture_path.name


@pytest.mark.memory_only
def test_c13_code_with_whitespace_fails(tmp_path):
    doc = tmp_path / "SKILL.md"
    doc.write_text("| `A_ONE` | 來源 | 行號 | 意義 A | 處置 A |\n", encoding="utf-8")
    findings = _load("validate").check_doc_coverage(
        ["A ONE"], [doc], _ENTRY_PATTERN
    )
    hits = _find(findings, "C13")
    assert hits and hits[0].severity == "FAIL"
    assert "空白" in hits[0].message


# ---------------------------------------------------------------------------
# 受管區段漂移：C14（R-26 等價）
# ---------------------------------------------------------------------------


def test_c14_generated_fact_drift(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        '<!-- BEGIN: generated-fact marker="error-codes" -->\n'
        "OLD\n"
        '<!-- END: generated-fact marker="error-codes" -->\n',
        encoding="utf-8",
    )
    policy = {
        "generated_facts": [
            {"command": "echo NEW", "reflected_in": "SKILL.md", "marker": "error-codes"},
        ]
    }
    findings = _load("validate").check_generated_facts(tmp_path, policy)
    hits = _find(findings, "C14")
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# 總覽與政策宣告缺席
# ---------------------------------------------------------------------------


def test_run_all_without_policy_file(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])  # 不寫 .project-policy.yml
    findings, summary = _load("validate").run_all(tmp_path)
    assert isinstance(summary, dict)
    assert _find(findings, "C13") == []
    assert _find(findings, "C14") == []


# ---------------------------------------------------------------------------
# CLI（python3 -m tools.contracts）
# ---------------------------------------------------------------------------


def test_cli_extract_lines_sorted(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("ALPHA_OK"), _code("ZETA_OK")],
        pkg={
            "pkg/mod.py": """
            def build(rec):
                rec["error_code"] = "ZETA_OK"
                return {"error_code": "ALPHA_OK"}

            def relay(s):
                return s.last_error
            """
        },
    )
    proc = _run_cli("extract", "--namespace", "error_code", "--format", "lines", "--repo", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines == sorted(lines)
    assert "last_error_passthrough" not in lines
    assert lines == ["ALPHA_OK", "ZETA_OK"]


def test_cli_check_exit_1_on_fail(tmp_path):
    _make_repo(
        tmp_path,
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "GHOST_OK"}
            """
        },
    )
    proc = _run_cli("check", "--repo", str(tmp_path))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert any(f["check"] == "C02" for f in out["findings"])


def test_cli_bad_config_exit_1(tmp_path):
    cfg = _extraction_config()
    cfg["namespaces"]["error_code"]["sinks"].append({"kind": "no_such_kind"})
    _make_repo(tmp_path, codes=[_code("OK_OK")], config=cfg)
    proc = _run_cli("extract", "--namespace", "error_code", "--format", "lines", "--repo", str(tmp_path))
    assert proc.returncode == 1
    assert proc.stderr.startswith("contracts: ")


# ---------------------------------------------------------------------------
# 邊界與防禦性回歸測試（P0-T6 評審回饋追加）
# ---------------------------------------------------------------------------


def test_c01_corrupt_catalog_reported(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])
    (tmp_path / "contracts" / "error_codes.yaml").write_text("corrupted: [yaml", encoding="utf-8")
    findings, summary = _load("validate").run_all(tmp_path)
    hits = _find(findings, "C01")
    assert len(hits) >= 1
    assert all(f.subject == "contracts" and f.severity == "FAIL" for f in hits)


def test_cli_check_only_unknown_code(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])
    proc = _run_cli("check", "--only", "C99", "--repo", str(tmp_path))
    assert proc.returncode == 1
    assert "contracts: unknown check code(s): C99" in proc.stderr


def test_cli_check_only_broken_catalog_retains_c01(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])
    (tmp_path / "contracts" / "error_codes.yaml").write_text("corrupted: [yaml", encoding="utf-8")
    proc = _run_cli("check", "--only", "C09", "--repo", str(tmp_path))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert any(f["check"] == "C01" for f in out["findings"])


def test_c14_command_failure_exit_code(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        '<!-- BEGIN: generated-fact marker="error-codes" -->\n'
        "OLD\n"
        '<!-- END: generated-fact marker="error-codes" -->\n',
        encoding="utf-8",
    )
    policy = {
        "generated_facts": [
            {"command": "sh -c 'printf \"OLD\"; exit 7'", "reflected_in": "SKILL.md", "marker": "error-codes"},
        ]
    }
    findings = _load("validate").check_generated_facts(tmp_path, policy)
    hits = _find(findings, "C14")
    assert len(hits) == 1
    assert "exit=7" in hits[0].message


def test_c09_dynamic_prefix_requires_colon(tmp_path):
    _make_repo(
        tmp_path,
        last_errors=[
            _lerr(
                "ATTACH_FAILED:*",
                kind="dynamic_prefix",
                reason_forms=["ATTACH_FAILED:OSError"],
                contract_tests=[f"{_DUMMY_TEST}::test_bare"],
            )
        ],
        tests={
            _DUMMY_TEST: """
            def test_bare():
                msg = "ATTACH_FAILED"
                assert msg == "ATTACH_FAILED"
            """
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert len(hits) == 1

    (tmp_path / _DUMMY_TEST).write_text(
        textwrap.dedent("""
        def test_bare():
            err = "OSError"
            msg = f"ATTACH_FAILED:{err}"
            assert msg == "ATTACH_FAILED:OSError"
        """),
        encoding="utf-8",
    )
    hits_ok = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert len(hits_ok) == 0


def test_c09_decorator_does_not_count(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("PLAIN_OK", contract_tests=[f"{_DUMMY_TEST}::test_dec_only"])],
        tests={
            _DUMMY_TEST: """
            import pytest
            @pytest.mark.skip(reason="PLAIN_OK")
            def test_dec_only():
                assert True
            """
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_contract_tests(cat, tmp_path), "C09")
    assert len(hits) == 1
    assert "node 本體內找不到該碼" in hits[0].message


def test_c15_duplicate_context_and_unregistered_code(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("PLAIN_OK", exit_codes={"remote": 0})],
        cli={
            "x-exit-codes": [
                {"context": "remote", "codes": {"GHOST_CODE": 1}},
                {"context": "remote", "codes": {"PLAIN_OK": 0}},
            ]
        },
    )
    cat = _catalog(tmp_path)
    findings = _load("validate").check_schema_refs(cat, tmp_path)
    hits = _find(findings, "C15")
    assert any("重複之 context" in f.message for f in hits)
    assert any("GHOST_CODE" in f.message for f in hits)


def test_c15_exit_code_mismatch_both_directions(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("PLAIN_OK", exit_codes={"remote": 0})],
        cli={
            "x-exit-codes": [
                {"context": "remote", "codes": {"PLAIN_OK": 2, "EXTRA_CODE": 1}},
            ]
        },
    )
    cat = _catalog(tmp_path)
    findings = _load("validate").check_schema_refs(cat, tmp_path)
    hits = _find(findings, "C15")
    assert any("PLAIN_OK" in f.subject for f in hits)
    assert any("EXTRA_CODE" in f.subject for f in hits)


def test_c15_context_outside_cli_contexts(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("PLAIN_OK")],
        cli={
            "x-exit-codes": [
                {"context": "not_a_valid_context", "codes": {}},
            ]
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C15")
    assert any("not_a_valid_context" in f.subject for f in hits)


def test_c15_missing_schema_files(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])
    (tmp_path / "contracts" / "rpc_schema.json").unlink()
    (tmp_path / "contracts" / "cli_schema.json").unlink()
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C15")
    assert len(hits) == 2
    assert {f.subject for f in hits} == {"contracts/rpc_schema.json", "contracts/cli_schema.json"}


def test_c18_hollow_schema_missing_x_methods(tmp_path):
    _make_repo(tmp_path, rpc={})
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C18")
    assert any("x-methods" in f.message for f in hits)


def test_c18_empty_properties_object_fails(tmp_path):
    _make_repo(
        tmp_path,
        rpc={
            "$defs": {},
            "x-methods": {
                "health.ping": {
                    "params": {},
                    "result": {"type": "object", "properties": {}},
                    "errors": [],
                    "error_order": [],
                    "side_effects": [],
                }
            },
        },
    )
    cat = _catalog(tmp_path)
    hits = _find(_load("validate").check_schema_refs(cat, tmp_path), "C18")
    assert any("空 properties 佔位符" in f.message for f in hits)


def test_c13_missing_doc_fails_even_with_require_in_any(tmp_path):
    good_doc = tmp_path / "doc_good.md"
    good_doc.write_text("### `A_CODE`\n- **意義**：說明\n- **處置**：處置\n", encoding="utf-8")
    missing_doc = tmp_path / "doc_missing.md"
    findings = _load("validate").check_doc_coverage(["A_CODE"], [good_doc, missing_doc], _ENTRY_PATTERN, require_in="any")
    hits = _find(findings, "C13")
    assert any(f.subject == str(missing_doc) and "文件不存在" in f.message for f in hits)


def test_c13_invalid_require_in(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("### `A_CODE`\n- **意義**：說明\n- **處置**：處置\n", encoding="utf-8")
    findings = _load("validate").check_doc_coverage(["A_CODE"], [doc], _ENTRY_PATTERN, require_in="unsupported")
    hits = _find(findings, "C13")
    assert any("未知的 require_in" in f.message for f in hits)


def test_c02_seen_dedupes_per_namespace(tmp_path):
    _make_repo(
        tmp_path,
        pkg={
            "pkg/mod.py": """
            def build(rec):
                rec["error_code"] = "SHARED_GHOST"
                rec.last_error = "SHARED_GHOST"
                return {}
            """
        },
    )
    cat, res = _context(tmp_path)
    findings = _load("validate").check_catalog_against_emissions(cat, res)
    hits = _find(findings, "C02")
    assert len(hits) == 2
    assert [f.subject for f in hits] == ["SHARED_GHOST", "SHARED_GHOST"]


def test_cli_summary_subcommand(tmp_path):
    _make_repo(tmp_path, codes=[_code("PLAIN_OK")])
    proc = _run_cli("summary", "--repo", str(tmp_path))
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "behavior_changes" in data
    assert "error_code" in data
    assert "session_last_error" in data
    assert "safety_invariants" in data


def test_cli_effective_subcommand(tmp_path):
    _make_repo(
        tmp_path,
        codes=[_code("PLAIN_OK")],
        pkg={
            "pkg/mod.py": """
            def build():
                return {"error_code": "PLAIN_OK"}
            """
        },
    )
    proc = _run_cli("effective", "--namespace", "error_code", "--repo", str(tmp_path))
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert lines == ["PLAIN_OK"]


# Phase 出口測試（P0-T12 Step 1 追加）：真實 repo 的契約檢查必須全綠。
@pytest.mark.process_rpc
def test_real_repo_contract_check_passes():
    proc = subprocess.run([sys.executable, "-m", "tools.contracts", "check"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = json.loads(proc.stdout)
    assert proc.returncode == 0, json.dumps(out["findings"], ensure_ascii=False, indent=1)
    assert out["ok"] is True
