"""Acceptance cases 清單、schema 與切片凍結鎖的一致性測試。"""
from __future__ import annotations

import collections
import ast
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest
import yaml

# 本檔混合 memory_only 與 process_rpc（子程序調用），依 T14 規則模組層只放平台 marker
pytestmark = [pytest.mark.core_portable]

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_cases() -> dict[str, Any]:
    cases_path = REPO / "tests/acceptance/cases.yaml"
    return yaml.safe_load(cases_path.read_text(encoding="utf-8"))


def _ids(suite: str) -> set[str]:
    cases = _load_cases()
    return {c["case_id"] for c in cases.get("cases", []) if c.get("suite") == suite}


@pytest.mark.memory_only
def test_contract_blackbox_cases_match_test_functions():
    """contract-blackbox 案例須與 Stage 1A daemon black-box 測試一一對應。"""
    source = REPO / "tests/contract_blackbox/test_bb_daemon_e2e.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    all_bb = [c for c in _load_cases().get("cases", []) if c.get("suite") == "contract-blackbox"]
    # P6 Task 0（07-slice22-console.md）：下游切片以 add-only 登記的 contract-blackbox 案例不屬 Stage 1A daemon black-box；
    # Stage 1A 案例仍須與 test_bb_daemon_e2e.py 一一對應，切片案例另驗來源路徑。分群見 _split_contract_blackbox。
    cases, later = _split_contract_blackbox(all_bb)
    expected_refs = {
        f"tests/contract_blackbox/test_bb_daemon_e2e.py::{name}" for name in names
    }
    actual_refs = {c["source_ref"] for c in cases}
    expected_ids = {f"bb-{name.removeprefix('test_').replace('_', '-')}" for name in names}

    assert actual_refs == expected_refs, (
        f"contract-blackbox source_ref 與測試函式不一致：缺少 {sorted(expected_refs - actual_refs)}；"
        f"多出 {sorted(actual_refs - expected_refs)}"
    )
    assert len(actual_refs) == len(cases), "contract-blackbox source_ref 不得重複"
    assert {c["case_id"] for c in cases} == expected_ids


_STAGE1A_PLAN = "04-stage1a-rust-cli.md"
_STAGE1A_SOURCE = "tests/contract_blackbox/test_bb_daemon_e2e.py::"


def _split_contract_blackbox(all_bb: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """contract-blackbox 案例分成 Stage 1A 與下游切片兩群，並驗證各群的結構不變量。

    parity-amendments-2026-10-26 #184（P6 審查 F5）：分群鍵改為結構欄位（``case_id`` 的 ``bb-`` 前綴或
    ``source_ref`` 指向 ``test_bb_daemon_e2e.py``），不再依自由文字 ``implemented_by``；任一 Stage 1A 案例的
    ``implemented_by`` 必須恰為 ``04-stage1a-rust-cli.md``，下游案例不得使用 ``bb-`` 前綴或 Stage 1A 來源。
    """
    stage1a = [c for c in all_bb
               if c["case_id"].startswith("bb-") or str(c.get("source_ref", "")).startswith(_STAGE1A_SOURCE)]
    later = [c for c in all_bb if c not in stage1a]
    for c in stage1a:
        assert c["case_id"].startswith("bb-"), f"{c['case_id']}：source_ref 屬 Stage 1A，case_id 必須以 bb- 開頭"
        assert c.get("implemented_by") == _STAGE1A_PLAN, f"{c['case_id']}：Stage 1A 案例 implemented_by 必須為 {_STAGE1A_PLAN}"
        assert str(c.get("source_ref", "")).startswith(_STAGE1A_SOURCE), f"{c['case_id']}：Stage 1A 案例來源必須為 {_STAGE1A_SOURCE}"
    for c in later:
        assert c.get("implemented_by") != _STAGE1A_PLAN, f"{c['case_id']}：非 bb- 案例不得標為 Stage 1A"
        assert str(c.get("implemented_by", "")).startswith(("07-slice22-console.md", "08-slice23", "09-slice24")), c["case_id"]
        assert str(c["source_ref"]).startswith(("tests/contract_blackbox/", "crates/")), c["case_id"]
        assert "::" in c["source_ref"], c["case_id"]
    assert len({c["source_ref"] for c in later}) == len(later), "切片 contract-blackbox source_ref 不得重複"
    return stage1a, later


@pytest.mark.memory_only
@pytest.mark.parametrize("bad", [
    # bb- 案例被標成切片計畫（舊邏輯會把它歸入切片群而無聲通過）
    {"case_id": "bb-new-case", "implemented_by": "07-slice22-console.md",
     "source_ref": "tests/contract_blackbox/slice22/test_x.py::test_new_case"},
    # Stage 1A 案例 implemented_by 文字漂移
    {"case_id": "bb-drift", "implemented_by": "04-stage1a-rust-cli.md ",
     "source_ref": "tests/contract_blackbox/test_bb_daemon_e2e.py::test_drift"},
    # 指向 Stage 1A 測試檔、卻不是 bb- 案例
    {"case_id": "sc99-x", "implemented_by": "07-slice22-console.md",
     "source_ref": "tests/contract_blackbox/test_bb_daemon_e2e.py::test_x"},
    # 非 bb- 案例宣稱屬 Stage 1A
    {"case_id": "sc98-y", "implemented_by": "04-stage1a-rust-cli.md",
     "source_ref": "tests/contract_blackbox/slice22/test_y.py::test_y"},
])
def test_contract_blackbox_grouping_is_structural(bad):
    ok = {"case_id": "bb-ok", "implemented_by": _STAGE1A_PLAN, "source_ref": _STAGE1A_SOURCE + "test_ok"}
    assert _split_contract_blackbox([ok]) == ([ok], [])
    with pytest.raises(AssertionError):
        _split_contract_blackbox([ok, dict(bad, suite="contract-blackbox")])


def _slice_to_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.split("."))


def _get_case_item_schema(schema: dict[str, Any]) -> dict[str, Any]:
    items = schema.get("properties", {}).get("cases", {}).get("items", {})
    if "$ref" in items:
        ref = items["$ref"].lstrip("#/").split("/")
        curr: Any = schema
        for part in ref:
            curr = curr.get(part, {})
        return curr
    return items


@pytest.mark.memory_only
def test_regression_cases_match_registry():
    sys.path.insert(0, str(REPO / "regression"))
    from serialwrap_regression import harness as rh

    reg = rh.load_registry()
    assert _ids("regression") == {c.id for c in reg}  # 以 registry 為準，不寫死數字


@pytest.mark.memory_only
def test_realhw_cases_match_registry():
    sys.path.insert(0, str(REPO))
    import realhw.cases  # noqa: F401  註冊所有 case
    from realhw import harness as hh

    assert _ids("realhw") == {c.id for c in hh.REGISTRY}


@pytest.mark.memory_only
def test_functest_cases_match_yaml():
    assert _ids("func-test") == {p.stem for p in (REPO / "func-test/cases").glob("*.yaml")}


@pytest.mark.memory_only
def test_scenarios_match_yaml():
    assert _ids("l15-scenario") == {p.stem for p in (REPO / "func-test/scenarios").glob("l15-*.yaml")}


@pytest.mark.memory_only
def test_windows_native_p2_cases_have_scenarios():
    own = {
        c["case_id"]
        for c in _load_cases().get("cases", [])
        if c.get("suite") == "windows-native"
        and str(c.get("implemented_by", "")).startswith("03-stage0-test-infra.md")
    }
    assert own == {p.stem for p in (REPO / "func-test/scenarios").glob("win-com-*.yaml")}


@pytest.mark.memory_only
def test_lock_never_shrinks():
    lock = json.loads((REPO / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    sys.path.insert(0, str(REPO))
    from tests.acceptance.regen_lock import compute_sets  # 由 tests/acceptance/regen_lock.py 匯入

    current = compute_sets(_load_cases())
    for name, ids in lock["sets"].items():
        missing = sorted(set(ids) - set(current[name]))
        assert missing == [], f"{name} 少了 {missing}（不得刪除案例或延後切片）"


@pytest.mark.memory_only
def test_schema_valid():
    import jsonschema

    cases_data = _load_cases()
    schema_path = REPO / "tests/acceptance/cases.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    validator_cls = jsonschema.Draft202012Validator
    validator_cls.check_schema(schema)

    case_schema = _get_case_item_schema(schema)
    required_fields = {
        "case_id",
        "contract",
        "invariants",
        "layer",
        "platforms",
        "capabilities",
        "daemon_slice",
        "prerequisites",
        "necessity",
        "issues",
        "behavior_changes",
        "expected",
        "evidence",
    }
    missing_required = required_fields - set(case_schema.get("required", []))
    assert missing_required == set(), f"Schema cases item missing required fields: {missing_required}"

    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(cases_data), key=lambda e: e.path)
    assert errors == [], f"Validation errors: {[e.message for e in errors]}"


@pytest.mark.memory_only
def test_case_ids_unique():
    cases = _load_cases().get("cases", [])
    ids = [c["case_id"] for c in cases]
    duplicates = [cid for cid, count in collections.Counter(ids).items() if count > 1]
    assert duplicates == [], f"Found duplicate case_ids: {duplicates}"


@pytest.mark.memory_only
def test_regression_cases_have_issues():
    # 規則修正（P2-T12 實作者）：P1 T21 新增的 pending_issue 案例（擁有者尚未開立 issue 號）
    # 依 docs/regression-plugin.md 與 registry 規則允許 issues 為空，但不得以假號補位；
    # 與 issues 互斥。其餘 regression 案例仍須有 issue。
    cases = _load_cases().get("cases", [])
    bad = [
        c["case_id"]
        for c in cases
        if c.get("suite") == "regression" and not c.get("issues") and not c.get("pending_issue")
    ]
    assert bad == [], f"regression cases missing issues: {bad}"
    both = [c["case_id"] for c in cases if c.get("pending_issue") and c.get("issues")]
    assert both == [], f"pending_issue 與 issues 互斥，不得同時存在: {both}"


@pytest.mark.memory_only
def test_prerequisite_slice_order():
    cases_data = _load_cases()
    features = cases_data.get("features", {})
    bad = []
    for c in cases_data.get("cases", []):
        dsl = c.get("daemon_slice")
        for prereq in c.get("prerequisites", []):
            if prereq == "remote_cli":
                if c.get("cli_stage") != "1B":
                    bad.append((c["case_id"], prereq, "requires cli_stage==1B", c.get("cli_stage")))
            elif dsl is not None:
                assert prereq in features, f"Unknown prerequisite {prereq} in case {c['case_id']}"
                feat_slice = features[prereq]
                if _slice_to_tuple(dsl) < _slice_to_tuple(feat_slice):
                    bad.append((c["case_id"], prereq, feat_slice, dsl))
    assert bad == [], f"Cases violating prerequisite slice ordering: {bad}"


@pytest.mark.memory_only
def test_cli_stage_rule():
    cases = _load_cases().get("cases", [])
    target_prereqs = {"console_pty", "console_telnet", "remote_cli", "supervision"}
    target_caps = {"human_frontend", "supervisor_runtime"}
    bad = []
    for c in cases:
        prereqs = set(c.get("prerequisites") or [])
        caps = set(c.get("capabilities") or [])
        if (prereqs & target_prereqs) or (caps & target_caps):
            if c.get("cli_stage") != "1B":
                bad.append((c["case_id"], c.get("cli_stage")))
    assert bad == [], f"Cases violating cli_stage rule: {bad}"


@pytest.mark.memory_only
def test_spec_slice_families():
    cases = _load_cases().get("cases", [])
    bad = []
    for c in cases:
        fam = c.get("family")
        cid = c.get("case_id")
        dsl = c.get("daemon_slice")
        if fam in ("F1", "F2", "F3", "F12"):
            if dsl != "2.1":
                bad.append((cid, fam, dsl, "2.1"))
        elif fam == "F4":
            expected_slice = "2.2" if cid == "f4-interactive-line-cmd-defined" else "2.1"
            if dsl != expected_slice:
                bad.append((cid, fam, dsl, expected_slice))
        elif fam in ("F5", "F6", "F8"):
            if dsl != "2.2":
                bad.append((cid, fam, dsl, "2.2"))
        elif fam in ("F9", "F10", "F11"):
            if dsl != "2.3":
                bad.append((cid, fam, dsl, "2.3"))
        elif fam == "F7":
            if dsl != "2.4":
                bad.append((cid, fam, dsl, "2.4"))
    assert bad == [], f"Families with wrong daemon_slice: {bad}"


@pytest.mark.memory_only
def test_cross_slice_cases_declared():
    cases_dict = {c["case_id"]: c for c in _load_cases().get("cases", [])}
    target_ids = ["f4-interactive-line-cmd-defined", "p1-wal-reset", "p1-wal-fullrun"]
    for cid in target_ids:
        assert cid in cases_dict, f"Case {cid} not found in cases.yaml"
        reason = cases_dict[cid].get("cross_slice_reason")
        assert isinstance(reason, str) and len(reason.strip()) > 0, (
            f"Case {cid} is cross-slice and must have non-empty cross_slice_reason"
        )


@pytest.mark.memory_only
def test_windows_gap_has_reason():
    cases = _load_cases().get("cases", [])
    bad = []
    for c in cases:
        status = c.get("windows_status")
        reason = c.get("windows_gap_reason")
        if status == "gap":
            if not isinstance(reason, str) or not reason.strip():
                bad.append((c["case_id"], "windows_status is gap but windows_gap_reason is empty/missing"))
        elif status in ("native", "ported"):
            if reason is not None:
                bad.append((c["case_id"], f"windows_status is {status} but windows_gap_reason is {reason}"))
    assert bad == [], f"Cases with invalid windows_gap_reason: {bad}"


@pytest.mark.memory_only
def test_windows_com_not_satisfied_by_memory():
    cases = _load_cases().get("cases", [])
    bad = []
    for c in cases:
        # P2-T12 審查修正：platform_capabilities.windows 亦為 Windows 能力宣告來源，守衛須一併檢查
        caps = list(c.get("capabilities") or []) + list((c.get("platform_capabilities") or {}).get("windows") or [])
        if "windows_com" in caps:
            layer = c.get("layer")
            platforms = c.get("platforms") or []
            if layer not in ("L1.5", "L2"):
                bad.append((c["case_id"], f"layer is {layer}, expected L1.5 or L2"))
            if "windows" not in platforms:
                bad.append((c["case_id"], f"platforms {platforms} missing windows"))
    assert bad == [], f"Cases violating windows_com constraints: {bad}"


@pytest.mark.memory_only
def test_pending_issue_matches_registry():
    """pending_issue 不得自標繞過 #155：yaml 與 regression registry 的待補清單與 issues 逐筆一致（P2-T12 審查修正）。"""
    sys.path.insert(0, str(REPO / "regression"))
    try:
        from serialwrap_regression import harness as rh
    finally:
        sys.path.pop(0)
    reg = {c.id: c for c in rh.load_registry()}
    cases = [c for c in _load_cases().get("cases", []) if c.get("suite") == "regression"]
    yaml_pending = {c["case_id"] for c in cases if c.get("pending_issue")}
    reg_pending = {cid for cid, c in reg.items() if c.pending_issue}
    assert yaml_pending == reg_pending, f"pending_issue 不一致：yaml={sorted(yaml_pending)} registry={sorted(reg_pending)}"
    mismatched = [
        c["case_id"]
        for c in cases
        if sorted(c.get("issues") or []) != sorted(reg[c["case_id"]].issues)
    ]
    assert mismatched == [], f"regression issues 與 registry 不一致: {mismatched}"


@pytest.mark.process_rpc
def test_regen_lock_refuses_denominator_attr_change(tmp_path: pathlib.Path):
    """既有案例的分母屬性（platforms／layer／expected 等）被改動時，--add-only 與 --check 皆拒絕（P2-T12 審查修正）。"""
    regen_script = REPO / "tests/acceptance/regen_lock.py"
    cases_data = _load_cases()
    target = next(c for c in cases_data["cases"] if c["case_id"] == "f1-too-long-rejected")
    target["platforms"] = ["linux"]  # 無聲縮小分母：Windows 覆蓋消失，但集合 ID 不變
    tmp_cases = tmp_path / "cases.yaml"
    tmp_cases.write_text(yaml.safe_dump(cases_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp_lock = tmp_path / "cases.lock.json"
    orig_lock = (REPO / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8")
    tmp_lock.write_text(orig_lock, encoding="utf-8")
    (tmp_path / "cases.counts.json").write_text(
        (REPO / "tests/acceptance/cases.counts.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    add = subprocess.run(
        [sys.executable, str(regen_script), "--add-only", "--cases", str(tmp_cases), "--lock", str(tmp_lock)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
    )
    assert add.returncode == 1, f"expected refusal, got {add.returncode}:\n{add.stderr}"
    assert "分母屬性變更" in add.stderr
    assert tmp_lock.read_text(encoding="utf-8") == orig_lock, "Lock file must remain unchanged on refusal"

    chk = subprocess.run(
        [sys.executable, str(regen_script), "--check", "--cases", str(tmp_cases), "--lock", str(tmp_lock)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
    )
    assert chk.returncode == 1
    assert "案例屬性與 cases.yaml 不一致：f1-too-long-rejected" in chk.stderr


@pytest.mark.memory_only
def test_behavior_changes_exist():
    bc_path = REPO / "contracts/behavior_changes.yaml"
    bc_data = yaml.safe_load(bc_path.read_text(encoding="utf-8"))
    # 修正（P2-T12 實作者）：P0 契約檔（contracts/behavior_changes.yaml）頂層鍵為 `changes`，
    # 與 tests/test_contracts_cli_schema.py、test_contracts_rpc_schema.py 一致；原本讀 `behavior_changes` 恆為空集合。
    valid_ids = {bc["id"] for bc in bc_data.get("changes", [])}
    cases = _load_cases().get("cases", [])
    bad = []
    for c in cases:
        for bc_id in c.get("behavior_changes") or []:
            if bc_id not in valid_ids:
                bad.append((c["case_id"], bc_id))
    assert bad == [], f"Unknown behavior_changes IDs: {bad}"


@pytest.mark.memory_only
def test_each_bc_has_stage0_case():
    cases = _load_cases().get("cases", [])
    stage0_cases = [c for c in cases if c.get("stage0_required") is True]
    referenced_bcs = {bc for c in stage0_cases for bc in (c.get("behavior_changes") or [])}
    for bc_id in ["BC-01", "BC-02", "BC-03", "BC-04", "BC-05"]:
        assert bc_id in referenced_bcs, (
            f"{bc_id} must be referenced by at least one stage0_required: true case"
        )


@pytest.mark.memory_only
def test_no_duplicate_platform_rows():
    cases = _load_cases().get("cases", [])
    win_native = [c for c in cases if c.get("suite") == "windows-native"]
    func_cases = [c for c in cases if c.get("suite") == "func-test"]
    func_refs = {c.get("source_ref") for c in func_cases if c.get("source_ref")}
    bad = []
    for c in win_native:
        title = c.get("title", "")
        if "於 Windows" in title:
            bad.append((c["case_id"], "title contains '於 Windows'"))
        if c.get("source_ref") and c.get("source_ref") in func_refs:
            bad.append((c["case_id"], f"duplicate source_ref: {c.get('source_ref')}"))
    assert bad == [], f"Duplicate platform rows found: {bad}"


@pytest.mark.memory_only
def test_spike_is_not_gate_evidence():
    cases = _load_cases().get("cases", [])
    bad = []
    for c in cases:
        for ev in c.get("evidence") or []:
            if "spike-report.json" in ev:
                bad.append((c["case_id"], ev))
    assert bad == [], f"Cases referencing spike-report.json in evidence: {bad}"


@pytest.mark.memory_only
def test_conditional_cases_declare_capability():
    cases = _load_cases().get("cases", [])
    bad = []
    for c in cases:
        if c.get("necessity") == "conditional":
            caps = set(c.get("capabilities") or [])
            if not caps:
                bad.append((c["case_id"], "conditional necessity but empty capabilities"))
            if c.get("case_id", "").startswith("f7-") and "target_b64_tool" not in caps:
                bad.append((c["case_id"], "f7-* conditional case must have target_b64_tool capability"))
    assert bad == [], f"Conditional cases with invalid capabilities: {bad}"


@pytest.mark.memory_only
def test_invariants_from_spec_only():
    cases_data = _load_cases()
    invariant_ids = set(cases_data.get("invariant_ids") or [])
    expected_invariants = {
        "zero_tx_on_reject",
        "no_forbidden_interleave",
        "no_stale_context_replay",
        "no_reopen_before_handoff",
        "no_auto_resend",
        "insufficient_evidence_not_pass",
    }
    assert invariant_ids == expected_invariants, (
        f"invariant_ids must match spec §11.3 six invariants, got {invariant_ids}"
    )
    bad = []
    stage0_covered = set()
    for c in cases_data.get("cases", []):
        for inv in c.get("invariants") or []:
            if inv not in invariant_ids:
                bad.append((c["case_id"], inv))
            if c.get("stage0_required") is True:
                stage0_covered.add(inv)
    assert bad == [], f"Cases with invalid invariants: {bad}"
    missing_stage0 = invariant_ids - stage0_covered
    assert missing_stage0 == set(), (
        f"All 6 invariant_ids must be referenced by at least one stage0_required case, missing: {missing_stage0}"
    )


@pytest.mark.memory_only
def test_f4_sentinel_state_consistent():
    cases_dict = {c["case_id"]: c for c in _load_cases().get("cases", [])}
    assert "f4-background-result-tail-consistent" in cases_dict
    f4 = cases_dict["f4-background-result-tail-consistent"]
    assert f4.get("expected") == "PASS", f"f4 expected must be PASS, got {f4.get('expected')}"
    assert f4.get("sentinel") is None, f"f4 sentinel must be None, got {f4.get('sentinel')}"
    plugin_doc = (REPO / "docs/regression-plugin.md").read_text(encoding="utf-8")
    assert "刻意保留的紅燈哨兵" not in plugin_doc, (
        "docs/regression-plugin.md must not contain '刻意保留的紅燈哨兵'"
    )


@pytest.mark.memory_only
def test_xfail_requires_sentinel():
    bad = []
    for c in _load_cases().get("cases", []):
        if c.get("expected") == "XFAIL":
            sentinel = c.get("sentinel")
            if not isinstance(sentinel, dict):
                bad.append((c["case_id"], "sentinel is not a dict"))
                continue
            if not sentinel.get("issue") or not sentinel.get("signature") or not sentinel.get("expires"):
                bad.append((c["case_id"], f"sentinel missing required fields: {sentinel}"))
                continue
            try:
                datetime.date.fromisoformat(sentinel["expires"])
            except ValueError:
                bad.append((c["case_id"], f"invalid ISO date format in expires: {sentinel.get('expires')}"))
    assert bad == [], f"XFAIL cases with invalid sentinel: {bad}"


@pytest.mark.memory_only
def test_manual_procedure_not_counted():
    cases_dict = {c["case_id"]: c for c in _load_cases().get("cases", [])}
    assert "p2-1-mcu-flash-manual" in cases_dict
    manual = cases_dict["p2-1-mcu-flash-manual"]
    proc = manual.get("procedure")
    assert isinstance(proc, dict), "p2-1-mcu-flash-manual must have procedure dict"
    assert proc.get("counted_in") == "none", f"counted_in must be 'none', got {proc.get('counted_in')}"
    proc_path = REPO / proc.get("path", "realhw/procedures/p2-1-mcu-flash.md")
    content = proc_path.read_bytes()
    expected_sha1 = hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content).hexdigest()
    assert proc.get("revision") == expected_sha1, (
        f"procedure.revision {proc.get('revision')} does not match blob sha1 {expected_sha1}"
    )


@pytest.mark.memory_only
def test_lr_mixed_stage0_and_evidence():
    cases_dict = {c["case_id"]: c for c in _load_cases().get("cases", [])}
    assert "lr-mixed" in cases_dict
    lr = cases_dict["lr-mixed"]
    assert lr.get("stage0_required") is True, f"lr-mixed stage0_required must be True, got {lr.get('stage0_required')}"
    ev = lr.get("evidence") or []
    assert "release-gate.json" in ev, f"lr-mixed evidence missing release-gate.json: {ev}"
    assert "observer.ndjson" in ev, f"lr-mixed evidence missing observer.ndjson: {ev}"


@pytest.mark.process_rpc
def test_regen_lock_check_clean():
    regen_script = REPO / "tests/acceptance/regen_lock.py"
    proc = subprocess.run(
        [sys.executable, str(regen_script), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"regen_lock.py --check failed (rc={proc.returncode}):\n{proc.stderr}\n{proc.stdout}"


@pytest.mark.process_rpc
def test_regen_lock_refuses_removal(tmp_path: pathlib.Path):
    regen_script = REPO / "tests/acceptance/regen_lock.py"
    cases_path = REPO / "tests/acceptance/cases.yaml"
    lock_path = REPO / "tests/acceptance/cases.lock.json"

    tmp_cases = tmp_path / "cases.yaml"
    tmp_lock = tmp_path / "cases.lock.json"

    cases_data = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    orig_lock = lock_path.read_text(encoding="utf-8")
    tmp_lock.write_text(orig_lock, encoding="utf-8")

    req_indices = [i for i, c in enumerate(cases_data["cases"]) if c.get("necessity") == "required"]
    assert req_indices, "No required cases found to remove"
    cases_data["cases"].pop(req_indices[0])
    tmp_cases.write_text(yaml.safe_dump(cases_data), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(regen_script), "--add-only", "--cases", str(tmp_cases), "--lock", str(tmp_lock)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 1, f"Expected exit 1 when removing required case, got {proc.returncode}"
    assert tmp_lock.read_text(encoding="utf-8") == orig_lock, "Lock file must remain unchanged on refusal"


@pytest.mark.memory_only
def test_counts_report():
    counts_path = REPO / "tests/acceptance/cases.counts.json"
    counts = json.loads(counts_path.read_text(encoding="utf-8"))
    cases = _load_cases().get("cases", [])

    by_suite: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    by_slice: dict[str, int] = {}
    for c in cases:
        s = c.get("suite", "unknown")
        by_suite[s] = by_suite.get(s, 0) + 1

        layer = c.get("layer", "unknown")
        by_layer[layer] = by_layer.get(layer, 0) + 1

        dsl = str(c.get("daemon_slice", "unknown"))
        by_slice[dsl] = by_slice.get(dsl, 0) + 1

    assert counts.get("by_suite") == by_suite, f"counts by_suite mismatch: {counts.get('by_suite')} vs {by_suite}"
    assert counts.get("by_layer") == by_layer, f"counts by_layer mismatch: {counts.get('by_layer')} vs {by_layer}"
    assert counts.get("by_slice") == by_slice, f"counts by_slice mismatch: {counts.get('by_slice')} vs {by_slice}"


@pytest.mark.process_rpc
def test_regen_lock_authorized_deferral_is_per_name(tmp_path: pathlib.Path):
    """OD-34（擁有者裁決 2026-10-28）：案例延後切片只能逐名授權（`--authorize-deferral <case_id>:OD-<n>`）。
    未授權者照舊拒絕；授權只放行該案例的 daemon_slice 往後移，不放行其他分母屬性改動或其他案例；
    沒有實際延後的授權（過期 token）亦拒絕。"""
    regen_script = REPO / "tests/acceptance/regen_lock.py"
    orig_lock = (REPO / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8")
    counts = (REPO / "tests/acceptance/cases.counts.json").read_text(encoding="utf-8")
    lock_now = json.loads(orig_lock)
    cid = next(c for c in lock_now["sets"]["slice_2.2"] if c not in lock_now["sets"]["slice_2.1"]
               and lock_now["case_attrs"][c]["daemon_slice"] == "2.2")

    def run(tag: str, mutate, *extra: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        d = tmp_path / tag
        d.mkdir()
        cases_data = _load_cases()
        mutate({c["case_id"]: c for c in cases_data["cases"]})
        (d / "cases.yaml").write_text(yaml.safe_dump(cases_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        (d / "cases.lock.json").write_text(orig_lock, encoding="utf-8")
        (d / "cases.counts.json").write_text(counts, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(regen_script), "--add-only", "--cases", str(d / "cases.yaml"),
             "--lock", str(d / "cases.lock.json"), *extra],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        )
        return proc, d / "cases.lock.json"

    def defer(cases):
        cases[cid]["daemon_slice"] = "2.3"

    proc, lock = run("unauth", defer)
    assert proc.returncode == 1 and "被延後切片" in proc.stderr, proc.stderr
    assert lock.read_text(encoding="utf-8") == orig_lock

    proc, lock = run("auth", defer, "--authorize-deferral", f"{cid}:OD-34")
    assert proc.returncode == 0, proc.stderr
    new = json.loads(lock.read_text(encoding="utf-8"))
    assert cid not in new["sets"]["slice_2.2"] and cid in new["sets"]["slice_2.3"]
    assert new["case_attrs"][cid]["daemon_slice"] == "2.3"
    assert f"{cid}" in proc.stdout and "OD-34" in proc.stdout

    def defer_and_shrink(cases):
        defer(cases)
        cases[cid]["platforms"] = ["linux"]

    proc, lock = run("attr", defer_and_shrink, "--authorize-deferral", f"{cid}:OD-34")
    assert proc.returncode == 1 and "分母屬性變更" in proc.stderr, proc.stderr
    assert lock.read_text(encoding="utf-8") == orig_lock

    proc, lock = run("stale", lambda cases: None, "--authorize-deferral", f"{cid}:OD-34")
    assert proc.returncode == 1 and "未對應" in proc.stderr, proc.stderr

    proc, lock = run("badref", defer, "--authorize-deferral", f"{cid}:owner")
    assert proc.returncode == 2, proc.stderr
