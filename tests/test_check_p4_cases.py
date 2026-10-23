"""`scripts/rust/check_p4_cases.py` 完成度 gate 的自測（P4-T1，審查修正回歸）。

每個測試以暫存映射表、lock 與證據目錄替換模組常數，不碰真實 `target/` 與 `tests/acceptance/`。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = REPO_ROOT / "scripts" / "rust" / "check_p4_cases.py"
HEAD = "a" * 40
OLD = "b" * 40


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_p4_cases_under_test", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _case(case_id: str, **over) -> dict:
    base = {
        "case_id": case_id,
        "invariants": ["zero_tx_on_reject"],
        "layer": "L1",
        "platforms": ["linux"],
        "capabilities": [],
        "artifact": "rust-cli-bundle",
        "tests": [f"sw-cli::mgmt_remote::{case_id.replace('-', '_')}"],
        "evidence": f"target/p4-evidence/{case_id}.json",
        "necessity": "required",
    }
    base.update(over)
    return base


@pytest.fixture
def gate(tmp_path, monkeypatch):
    mod = _load_gate()
    acceptance = tmp_path / "acceptance"
    acceptance.mkdir()
    ev = tmp_path / "evidence"
    ev.mkdir()
    codes = tmp_path / "error_codes.yaml"
    codes.write_text("codes:\n- code: TIMEOUT\n", encoding="utf-8")
    artifact = tmp_path / "serialwrap"
    artifact.write_bytes(b"\x7fELF-test-binary")
    monkeypatch.setattr(mod, "CASE_MAP", acceptance / "p4_case_map.yaml")
    monkeypatch.setattr(mod, "LOCK", acceptance / "p4_required.lock.json")
    monkeypatch.setattr(mod, "ERROR_CODES", codes)
    monkeypatch.setattr(mod, "EVIDENCE_DIR", ev)
    # P4-T16 新增 L1 完整性規則；本檔只測案例證據邏輯，L1 規則由
    # tests/test_rust_bench_tools.py::test_check_l1_completeness_rules 以合成報告獨立覆蓋。
    monkeypatch.setattr(mod, "P4_L1_SETS", {})
    monkeypatch.setattr(mod, "head_commit", lambda: HEAD)
    mod.artifact = artifact  # type: ignore[attr-defined]
    mod.tmp = tmp_path  # type: ignore[attr-defined]
    return mod


def _write_map(mod, cases: list[dict]) -> None:
    mod.CASE_MAP.write_text(yaml.safe_dump({"schema_version": 1, "cases": cases}, sort_keys=False), encoding="utf-8")


def _pass_record(mod, case_id: str, platform: str = "linux", **over) -> dict:
    sha = hashlib.sha256(mod.artifact.read_bytes()).hexdigest()
    rec = {
        "case_id": case_id,
        "round": "rust",
        "platform": platform,
        "commit": HEAD,
        "artifact_path": str(mod.artifact),
        "artifact_sha256": sha,
        "result": "PASS",
    }
    rec.update(over)
    return rec


def _write_evidence(mod, case_id: str, records: list[dict]) -> None:
    (mod.EVIDENCE_DIR / f"{case_id}.json").write_text(json.dumps({"records": records}), encoding="utf-8")


def test_freeze_then_check_passes_with_valid_evidence(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a")])
    assert gate.check() == 0


def test_freeze_refuses_to_overwrite_existing_lock(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    assert gate.freeze(add_only=False) == 1


def test_rust_round_skip_is_rejected_even_when_registered(gate) -> None:
    _write_map(gate, [_case("sc-a", skip_registered={"python": "rust-only"})])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", result="SKIP", reason="x", artifact_sha256=None)])
    assert gate.check() == 1


def _py_skip(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "round": "python",
        "platform": "linux",
        "commit": HEAD,
        "artifact_sha256": None,
        "result": "SKIP",
        "reason": "rust-only",
    }


def test_python_skip_is_accepted_when_registered(gate) -> None:
    _write_map(gate, [_case("sc-a", skip_registered={"python": "rust-only"})])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_py_skip("sc-a"), _pass_record(gate, "sc-a")])
    assert gate.check() == 0


def test_python_skip_is_rejected_when_not_registered(gate) -> None:
    _write_map(gate, [_case("sc-b")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-b", [_py_skip("sc-b"), _pass_record(gate, "sc-b")])
    assert gate.check() == 1


def test_stale_commit_does_not_satisfy_required_pass(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", commit=OLD)])
    assert gate.check() == 1


def test_pass_requires_matching_artifact_digest(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", artifact_sha256="0" * 64)])
    assert gate.check() == 1


def test_pass_record_for_every_platform_is_required(gate) -> None:
    _write_map(gate, [_case("sc-a", platforms=["linux", "windows"])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", platform="linux")])
    assert gate.check() == 1


def test_unfrozen_required_case_fails_check(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_map(gate, [_case("sc-a"), _case("sc-new")])
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a")])
    _write_evidence(gate, "sc-new", [_pass_record(gate, "sc-new")])
    assert gate.check() == 1
    assert gate.freeze(add_only=True) == 0
    assert gate.check() == 0


def test_add_only_rejects_removed_or_downgraded_case(gate) -> None:
    _write_map(gate, [_case("sc-a"), _case("sc-b")])
    assert gate.freeze(add_only=False) == 0
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=True) == 1
    _write_map(gate, [_case("sc-a"), _case("sc-b", necessity="optional")])
    assert gate.freeze(add_only=True) == 1


def test_placeholder_only_case_is_not_evidence(gate) -> None:
    _write_map(gate, [_case("sc-a", tests=["sw-cli::p4_case_placeholders::sc_a"])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a")])
    assert gate.check() == 1


POSIX_NODE = "sw-pal::process_tunnel::posix_terminate_kills_whole_group_including_grandchild"
# 兩個平台的凍結 selector 皆不收集的節點：無平台 marker 的 contract_blackbox 節點（test_daemon_start_ab_conflict）。
# 原用 test_daemon_restart_ab，該節點補上 posix_only 後已被 Linux s1b_20groups 收集（S1X G5），故改用仍未收集者。
UNSELECTED_NODE = "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_start_ab_conflict"


def test_pass_on_platform_outside_cargo_selector_does_not_count(gate) -> None:
    """平台適用性（G4-(e)）：cfg posix 節點的 windows PASS 不計入；linux PASS 足以放行 linux。"""
    _write_map(gate, [_case("sc-a", platforms=["linux", "windows"], tests=[POSIX_NODE])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [
        _pass_record(gate, "sc-a", platform="linux", test_id=f"cargo:{POSIX_NODE}"),
        _pass_record(gate, "sc-a", platform="windows", test_id=f"cargo:{POSIX_NODE}"),
    ])
    assert gate.check() == 1


def test_pass_on_applicable_platform_only_is_accepted(gate) -> None:
    _write_map(gate, [_case("sc-a", platforms=["linux"], tests=[POSIX_NODE])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", platform="linux", test_id=f"cargo:{POSIX_NODE}")])
    assert gate.check() == 0


def test_pass_for_node_outside_every_selector_is_rejected(gate) -> None:
    """兩個平台的凍結 selector 都不收集的 contract_blackbox 節點即使有 PASS 亦 FAIL。"""
    _write_map(gate, [_case("sc-a", platforms=["linux"], tests=[UNSELECTED_NODE])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", platform="linux", test_id=UNSELECTED_NODE)])
    assert gate.check() == 1


def test_junit_counts_are_testcase_level_not_suite_attributes(tmp_path) -> None:
    """P4 junit 計數與 P2 `check_evidence_index._junit_counts`、stage1a gate 同一慣例：tests 屬性含 subtests（6）
    但 testcase 元素僅 3 個 → 合計 3；failure／skipped 依 testcase 子元素計算。"""
    gate = _load_gate()
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite tests="6" failures="0" errors="0" skipped="1">'
        '<testcase classname="tests.a" name="t1"/>'
        '<testcase classname="tests.a" name="t2"><failure message="x"/></testcase>'
        '<testcase classname="tests.a" name="t3"><skipped message="x"/></testcase>'
        '</testsuite></testsuites>',
        encoding="utf-8",
    )
    assert gate._junit_counts(path) == {"tests": 3, "failures": 1, "errors": 0, "skipped": 1}
    assert gate._junit_counts(tmp_path / "missing.xml") is None


# ── OD-18 (1)(2)：平台不適用核准清單（CHG-S1X-P4-APPL-01，15 列）────────────────────────────────
# justification：OD-18 (1) 核准 CHG-S1X-P4-APPL-01 之不適用組合不產生紀錄、不要求該平台 rust PASS；
# OD-18 (2) 明列 sc20 與 bc05-three-topologies 之 windows 列依 (1) 記為 not_applicable。
# 本段測試以合成 applicability 與 monkeypatch 的核准清單驗證 fail-closed 行為，不依賴真實收集結果。
BC05_THREE = "bc05-three-topologies"
BC05_THREE_NODE = "tests/contract_blackbox/test_cli_mgmt_remote.py::test_remote_topologies_docker"
SC20_NODE = "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_restart_ab"


def _bc05_case(**over) -> dict:
    base = _case(
        BC05_THREE,
        platforms=["linux", "windows"],
        layer="L1.5",
        capabilities=["docker"],
        artifact="rust-cli-bundle",
        tests=[BC05_THREE_NODE],
    )
    base.update(over)
    return base


def test_od18_approval_list_is_the_fifteen_listed_rows(gate) -> None:
    """OD-18 (1)(2) 對帳：核准清單恰為 CHG-S1X-P4-APPL-01 逐列之 15 列（含 sc20／bc05 windows 與 sc36 六筆）。"""
    rows = gate.APPROVED_NOT_APPLICABLE
    assert len(rows) == 15
    assert (BC05_THREE, "windows", BC05_THREE_NODE) in rows
    assert ("sc20-daemon-restart", "windows", SC20_NODE) in rows
    assert len([r for r in rows if r[0] == "sc36-win-installer-com0com-optional"]) == 6


def test_od18_bc05_windows_not_required_when_node_only_applies_to_linux(gate) -> None:
    """OD-18 (1)：bc05-three-topologies 唯一節點僅適用 linux，windows 不要求 rust PASS（已核准列）。

    修改前此案例 windows 必缺 rust PASS → FAIL（PENDING-OD-S1X-P4-BC05-WIN）。"""
    case = _bc05_case()
    _write_evidence(gate, BC05_THREE, [_pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE)])
    applicability = {BC05_THREE_NODE: frozenset({"linux"})}

    problems, passing, _ = gate.check_case_evidence(case, HEAD, gate.EVIDENCE_DIR, None, applicability)

    assert problems == []
    assert [r["platform"] for r in passing] == ["linux"]


def test_od18_skip_requires_each_pair_to_be_approved(gate, monkeypatch) -> None:
    """未列入核准清單的 (案例, 平台, 節點) 不得略過該平台 rust PASS 要求（fail-closed）。"""
    monkeypatch.setattr(gate, "APPROVED_NOT_APPLICABLE", frozenset())
    case = _bc05_case()
    _write_evidence(gate, BC05_THREE, [_pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE)])
    applicability = {BC05_THREE_NODE: frozenset({"linux"})}

    problems, _, _ = gate.check_case_evidence(case, HEAD, gate.EVIDENCE_DIR, None, applicability)

    assert any("windows" in p and "缺少" in p for p in problems), problems


def test_od18_skip_refused_when_node_absent_from_applicability(gate) -> None:
    """適用性未對節點計算（例如收集失敗回傳空 dict）時不得視為不適用而略過（guard，fail-closed）。"""
    case = _bc05_case()
    _write_evidence(gate, BC05_THREE, [_pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE)])

    problems, _, _ = gate.check_case_evidence(case, HEAD, gate.EVIDENCE_DIR, None, {})

    assert any("windows" in p for p in problems), problems


def test_od18_node_not_applicable_on_any_platform_still_fails(gate) -> None:
    """兩平台皆不適用的節點不在核准清單內，任一平台皆不得放行（guard）。"""
    case = _bc05_case()
    _write_evidence(gate, BC05_THREE, [_pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE)])

    problems, passing, _ = gate.check_case_evidence(case, HEAD, gate.EVIDENCE_DIR, None, {BC05_THREE_NODE: frozenset()})

    assert passing == []
    assert any("一律 FAIL" in p for p in problems), problems
    assert any("缺少 linux 的 rust 輪 PASS" in p for p in problems), problems


def test_od18_all_platforms_skipped_still_requires_a_rust_pass(gate, monkeypatch) -> None:
    """所有平台皆經核准略過時，仍須至少一個平台有 rust PASS（另加檢查；無任一 PASS 即 FAIL）。"""
    monkeypatch.setattr(gate, "APPROVED_NOT_APPLICABLE", frozenset({
        (BC05_THREE, "linux", BC05_THREE_NODE),
        (BC05_THREE, "windows", BC05_THREE_NODE),
    }))
    case = _bc05_case()
    _write_evidence(gate, BC05_THREE, [])

    problems, passing, _ = gate.check_case_evidence(case, HEAD, gate.EVIDENCE_DIR, None, {BC05_THREE_NODE: frozenset()})

    assert passing == []
    assert any("無任一平台" in p for p in problems), problems


def test_od18_not_applicable_row_with_record_is_rejected(gate) -> None:
    """不適用組合不產生紀錄；若仍出現 windows PASS 紀錄，不計入且 FAIL（guard）。"""
    case = _bc05_case()
    _write_evidence(gate, BC05_THREE, [
        _pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE),
        _pass_record(gate, BC05_THREE, platform="windows", test_id=BC05_THREE_NODE),
    ])
    applicability = {BC05_THREE_NODE: frozenset({"linux"})}

    problems, _, _ = gate.check_case_evidence(case, HEAD, gate.EVIDENCE_DIR, None, applicability)

    assert any("不適用於 windows" in p for p in problems), problems


def test_od18_unapproved_or_missing_not_applicable_row_is_owner_pending(gate) -> None:
    """實算 not_applicable 與核准清單不一致（新增或缺少任一列）一律 FAIL，訊息為待擁有者核准。"""
    cases = [_bc05_case()]
    approved = {"case_id": BC05_THREE, "platform": "windows", "node": BC05_THREE_NODE, "reason": "x"}
    extra = {"case_id": BC05_THREE, "platform": "linux", "node": BC05_THREE_NODE, "reason": "x"}

    assert gate.not_applicable_approval_problems(cases, [approved]) == []
    added = gate.not_applicable_approval_problems(cases, [approved, extra])
    missing = gate.not_applicable_approval_problems(cases, [])

    assert added and all("待擁有者核准" in p for p in added), added
    assert missing and all("待擁有者核准" in p for p in missing), missing


def _patch_applicability(gate, monkeypatch, mapping: dict[str, frozenset[str]]) -> None:
    """以合成適用性取代真實 pytest 收集（測試不依賴收集結果）。"""
    monkeypatch.setattr(
        gate,
        "platform_applicability",
        lambda nodes, **_kw: {n: frozenset(mapping.get(n, frozenset())) for n in nodes},
    )


def test_od18_check_passes_with_bc05_windows_not_applicable(gate, monkeypatch) -> None:
    """整體 gate：bc05-three-topologies 僅 linux PASS、windows 為核准之不適用列 → 本案例通過。"""
    _write_map(gate, [_bc05_case()])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, BC05_THREE, [_pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE)])
    _patch_applicability(gate, monkeypatch, {BC05_THREE_NODE: frozenset({"linux"})})

    assert gate.check() == 0


def test_od18_check_fails_when_a_not_applicable_row_is_unapproved(gate, monkeypatch, capsys) -> None:
    """整體 gate：核准清單缺少 bc05 windows 列時，實算之不適用列未核准 → FAIL，訊息為待擁有者核准。"""
    _write_map(gate, [_bc05_case()])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, BC05_THREE, [_pass_record(gate, BC05_THREE, platform="linux", test_id=BC05_THREE_NODE)])
    _patch_applicability(gate, monkeypatch, {BC05_THREE_NODE: frozenset({"linux"})})
    monkeypatch.setattr(gate, "APPROVED_NOT_APPLICABLE", frozenset())

    assert gate.check() == 1
    assert "待擁有者核准" in capsys.readouterr().out
