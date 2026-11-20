"""OD-60／P5 第 5 輪 OWNER-BATCH #8：gate 的 `case_verdicts.json` 彙整與斷路器每次正式實跑快照（純函式＋CLI；不啟動 daemon）。

規則（rulings/p5.md）：每個分母案例取其所有證據中最差判定（FAIL ＞ EVIDENCE_MISSING ＞ PASS），缺證據即 EVIDENCE_MISSING；
證據來源為 Task 20／21 的逐案結果——l15_ab（Python 基線與 Rust 兩邊）、移出 l15_ab 者的承擔測試、func-test runner。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("slice21_gate_judge", ROOT / "scripts" / "rust" / "slice21_gate_judge.py")
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)

L15 = "tests/acceptance/l15/test_slice21_l15.py::test_case"


def _l15(case: str, py: str = "passed", rs: str = "passed") -> list[dict]:
    return [{"nodeid": f"{L15}[python-SERIALWRAP_L15_PY_DAEMON-{case}]", "outcome": py},
            {"nodeid": f"{L15}[rust-SERIALWRAP_L15_RUST_DAEMON-{case}]", "outcome": rs}]


def test_worst_verdict_order():
    assert judge.worst_verdict(["PASS", "PASS"]) == "PASS"
    assert judge.worst_verdict(["PASS", "EVIDENCE_MISSING"]) == "EVIDENCE_MISSING"
    assert judge.worst_verdict(["EVIDENCE_MISSING", "FAIL", "PASS"]) == "FAIL"
    assert judge.worst_verdict([]) == "EVIDENCE_MISSING"          # 缺證據即 EVIDENCE_MISSING


def test_aggregate_takes_worst_of_all_evidence_per_case():
    rows = _l15("l15-half-prompt") + _l15("l15-exclusive-open", rs="failed") + _l15("l15-reject-zero-tx", py="skipped")
    rows += [_l15("l15-release-inflight")[0]]                     # 只有 Python 一邊 → 證據不足
    excluded = [{"case_id": "l15-wal-interference", "decision": "OD-40",
                 "covered_by": [{"section": "blackbox_rust", "nodeid": "tests/x.py::test_a"},
                                {"section": "pytest_full", "nodeid": "tests/y.py::test_b"}]}]
    sections = {"blackbox_rust": [{"nodeid": "tests/x.py::test_a", "outcome": "passed"}],
                "pytest_full": [{"nodeid": "tests/y.py::test_b", "outcome": "passed"}]}
    ids = ["l15-half-prompt", "l15-exclusive-open", "l15-reject-zero-tx", "l15-release-inflight",
           "l15-wal-interference", "ha-05-concurrent-submit", "sm-04-device-unplug", "sm-01-attach-to-ready",
           "p0-doctor"]
    verdicts, detail = judge.aggregate_case_verdicts(
        ids, l15_rows=rows, func_checks={"ha-05-concurrent-submit": 0, "sm-04-device-unplug": 1},
        excluded=excluded, section_rows=sections)
    assert verdicts == {
        "l15-half-prompt": "PASS",
        "l15-exclusive-open": "FAIL",               # Rust 失敗
        "l15-reject-zero-tx": "EVIDENCE_MISSING",   # Python skip
        "l15-release-inflight": "EVIDENCE_MISSING", # 缺 Rust 證據
        "l15-wal-interference": "PASS",             # OD-40 承擔測試皆 passed
        "ha-05-concurrent-submit": "PASS",
        "sm-04-device-unplug": "FAIL",
        "sm-01-attach-to-ready": "EVIDENCE_MISSING",  # func-test 未執行
        "p0-doctor": "EVIDENCE_MISSING",              # 實機案例：雲端無證據
    }
    assert set(detail) == set(ids) and detail["l15-exclusive-open"]
    sections["pytest_full"] = [{"nodeid": "tests/y.py::test_b", "outcome": "failed"}]
    v2, _ = judge.aggregate_case_verdicts(["l15-wal-interference"], l15_rows=[], func_checks={}, excluded=excluded,
                                          section_rows=sections)
    assert v2 == {"l15-wal-interference": "FAIL"}
    del sections["blackbox_rust"]
    sections["pytest_full"] = [{"nodeid": "tests/y.py::test_b", "outcome": "passed"}]
    v3, _ = judge.aggregate_case_verdicts(["l15-wal-interference"], l15_rows=[], func_checks={}, excluded=excluded,
                                          section_rows=sections)
    assert v3 == {"l15-wal-interference": "EVIDENCE_MISSING"}


def test_duplicate_l15_rows_are_evidence_missing():
    """同一案例同一 label 出現兩列（重跑或合併錯誤）不得取其一，記證據不足。"""
    rows = _l15("l15-half-prompt") + [_l15("l15-half-prompt")[1]]
    v, _ = judge.aggregate_case_verdicts(["l15-half-prompt"], l15_rows=rows, func_checks={}, excluded=[], section_rows={})
    assert v == {"l15-half-prompt": "EVIDENCE_MISSING"}


def _run_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    run = tmp_path / "run"
    (run / "sections").mkdir(parents=True)
    (run / "l15_ab").mkdir()
    (run / "l15_ab" / "l1-results.json").write_text(json.dumps({"results": _l15("l15-half-prompt")}), encoding="utf-8")
    (run / "sections" / "l15_ab.json").write_text(json.dumps({"name": "l15_ab", "evidence": "l15_ab/l1-results.json"}),
                                                   encoding="utf-8")
    (run / "sections" / "existing_suites_rust.json").write_text(json.dumps(
        {"name": "existing_suites_rust", "extra_checks": [{"name": "func-test:ha-05-concurrent-submit", "exit": 0}]}),
        encoding="utf-8")
    return run


def test_cli_case_verdicts_writes_files_from_run_dir(tmp_path):
    run = _run_dir(tmp_path)
    assert judge.main(["case-verdicts", "--run-dir", str(run)]) == 0
    out = json.loads((run / "case_verdicts.json").read_text(encoding="utf-8"))
    freeze = judge._load_module("freeze_slice_cases", ROOT / "scripts" / "rust" / "freeze_slice_cases.py") \
        .load_effective_freeze(ROOT / "tests" / "acceptance" / "breaker" / "slice_2.1.json")
    assert set(out) == set(freeze["required_case_ids"])
    assert out["l15-half-prompt"] == "PASS" and out["ha-05-concurrent-submit"] == "PASS"
    assert out["p0-doctor"] == "EVIDENCE_MISSING"
    assert (run / "case_verdicts.detail.json").is_file()


def test_breaker_snapshot_appended_per_formal_run(tmp_path):
    """OD-60：斷路器每週快照由 gate 每次正式實跑附加（含 passed／denominator 使 rate 可驗證）；dry-run 不附加。"""
    snaps = tmp_path / "snaps.ndjson"
    snaps.write_text("", encoding="utf-8")
    freeze = {"denominator": 4, "required_case_ids": ["a", "b", "c", "d"], "window": {"start": "2026-10-21", "weeks": 3},
              "stage2_started_at": "2026-10-21"}
    snap = judge.append_breaker_snapshot(snaps, freeze, {"a": "PASS", "b": "FAIL"}, run_id="r1", today="2026-10-29")
    assert snap == {"date": "2026-10-29", "rate": 0.25, "passed": 1, "denominator": 4, "run_id": "r1",
                    "results_sha256": snap["results_sha256"]}
    lines = [json.loads(x) for x in snaps.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines == [snap] and len(snap["results_sha256"]) == 64
    sb = judge._load_module("slice_breaker", ROOT / "scripts" / "rust" / "slice_breaker.py")
    sb._check_snapshot_denominators(lines, freeze)            # 快照格式通過斷路器自身驗證


def test_expect_yaml_has_od60_estimate():
    import yaml

    doc = yaml.safe_load((ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml").read_text(encoding="utf-8"))
    assert doc["stage2_estimate_weeks"] == 6                      # OD-60（2026-10-29 填；起算 2026-10-21）


def test_gate_script_aggregates_after_pytest_full_and_appends_snapshot():
    text = (ROOT / "scripts" / "rust" / "slice21_gate.sh").read_text(encoding="utf-8")
    agg = text.index('"$JUDGE" case-verdicts')
    assert agg > text.index("record pytest_full"), "case_verdicts 須在承擔測試（pytest_full）之後彙整"
    assert "--append-breaker-snapshot" in text
    assert 'SW_RESOURCE_REPORT="$RUN_DIR/l15_ab/l1-results.json"' in text
