from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFERRED = ("resources", "min_effective_load", "comparison_uncertainty")
P2_DEFERRED_FIELDS = {   # P2 Task 15「延後項讀寫契約」逐字；P2 Task 16 realhw.acceptance.od09_freeze.PER_PLATFORM_FIELDS 必須相等（test_p2_od09_freeze_api）
    "resources": ("rss_peak_kb", "rss_steady_kb", "rss_slope_kb_per_h"),
    "min_effective_load": ("min_completed_per_board_source", "max_idle_gap_s"),
    "comparison_uncertainty": ("error_rate_abs_tolerance", "throughput_ratio_tolerance", "stuck_s_tolerance", "max_reruns"),
}

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_p2_gate_api():
    from realhw.acceptance import gate
    # 依 plan 第 51 行 P2 介面：第 9 欄 excl_evidence_path=None（Windows 原生 COM 獨占證據，R1-F7）為 P2 既定介面，本測試原稿漏列
    assert [f.name for f in dataclasses.fields(gate.GateInputs)] == [
        "report_dir", "case_dir", "acceptance_path", "platform", "mode", "observer_path", "wal_dir", "baseline_gate_path",
        "excl_evidence_path"]
    assert dataclasses.fields(gate.GateInputs)[-1].default is None
    assert callable(gate.evaluate) and callable(gate.main)
    assert gate.RESULTS == ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED", "NOT_APPLICABLE_BASELINE")
    doc, meta = gate.load_acceptance(ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml")
    assert {"path", "revision", "sha256"} <= set(meta)


def test_p2_acceptance_deferred_and_workload_keys():
    import yaml
    doc = yaml.safe_load((ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml").read_text(encoding="utf-8"))
    assert "revision" in doc
    assert {"min_continuous_duration_s", "snapshot_interval_s", "max_observation_gap_factor", "engine_files"} <= set(doc["workload"])
    for k in DEFERRED:
        assert doc["thresholds"][k]["status"] in ("deferred", "frozen")
        # OD-09 延後項讀寫契約（P2 Task 15）：數值只在 per_platform.<os> 之下，欄位名即 P2 Task 16 freeze 的寫入介面
        assert set(doc["thresholds"][k]["per_platform"]) == {"linux", "windows"}
        for os_name in ("linux", "windows"):
            assert set(doc["thresholds"][k]["per_platform"][os_name]) == set(P2_DEFERRED_FIELDS[k])
        assert not (set(doc["thresholds"][k]) & {f for fs in P2_DEFERRED_FIELDS.values() for f in fs})   # 無直接掛在 key 下的數值欄位
        assert not ({"tolerance", "rerun_rule", "deferred"} & set(doc["thresholds"][k]))
    assert "deferred" not in doc and "deferred" not in doc["thresholds"]


def test_p2_od09_freeze_api():
    # R4-F2：OD-09 延後項的唯一寫入者是 P2 Task 16（Stage 0），本計畫只消費
    from realhw.acceptance import od09_freeze
    assert {k: tuple(v) for k, v in od09_freeze.PER_PLATFORM_FIELDS.items()} == P2_DEFERRED_FIELDS
    assert od09_freeze.DEFERRED_KEYS == DEFERRED
    for name in ("check_frozen_before", "register_candidate_start", "freeze", "propose"):
        assert callable(getattr(od09_freeze, name))
    assert od09_freeze.LEDGER_PATH == ROOT / "realhw" / "acceptance" / "candidate_longrun_starts.ndjson"
    assert (ROOT / "docs" / "rust-refactor" / "owner-decisions-addenda.md").is_file()             # P2 Task 16 建立
    assert (ROOT / "docs" / "rust-refactor" / "procedures" / "od09-deferred-freeze.md").is_file()  # P2 Task 16 建立
    assert not (ROOT / "tools" / "stage3" / "od09_freeze.py").exists()


def test_p2_cases_yaml_and_lock():
    import yaml
    cases = yaml.safe_load((ROOT / "tests" / "acceptance" / "cases.yaml").read_text(encoding="utf-8"))
    assert len(cases["invariant_ids"]) == 6
    assert {"case_id", "invariants", "destructive", "expected", "sentinel", "necessity", "layer", "platforms"} <= set(cases["cases"][0])
    lock = json.loads((ROOT / "tests" / "acceptance" / "cases.lock.json").read_text(encoding="utf-8"))
    assert "slice_2.4" in lock["sets"]


def test_p0_behavior_changes_scope():
    import yaml
    doc = yaml.safe_load((ROOT / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8"))
    entries = doc["changes"] if isinstance(doc, dict) else doc
    assert entries and all(e["scope"] in ("both", "rust_only", "python_when_manifest_present") for e in entries)
    assert all(e["ab_participation"] == ("diff" if e["scope"] == "both" else "absolute_only") for e in entries)


def test_p0_markers_on_stage3_files():
    import ast
    platform = {"core_portable", "posix_only", "windows_native"}
    for p in (ROOT / "tests").glob("test_stage3_*.py"):
        names = {n.attr for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
        assert names & platform, p.name   # 平台 marker 細部規則由 P0 tests/test_resource_markers.py 稽核
    assert not (ROOT / "tests" / "platform_resources.yaml").exists()   # P2 03 Task 13：不建此檔
