"""OD-87 斷路器豁免稽核與實際逐案結果快照記錄。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_od87_exemption_preserves_actual_s21_and_s22_breaker_states() -> None:
    freeze_module = _load("freeze_for_od87", "scripts/rust/freeze_slice_cases.py")
    breaker = _load("slice_breaker_od87", "scripts/rust/slice_breaker.py")
    breaker_dir = ROOT / "tests/acceptance/breaker"

    freeze21_path = breaker_dir / "slice_2.1.json"
    freeze21 = freeze_module.load_effective_freeze(freeze21_path)
    snapshots21 = [
        json.loads(line)
        for line in freeze21_path.with_name("slice_2.1.snapshots.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    state21 = breaker.gate_assessment(
        freeze21, snapshots21, today="2026-11-14", stage2_estimate_weeks=0.0,
    )
    assert state21["actual_verdict"] == "FAIL", state21
    assert state21["assessment"]["tripped"] is True, state21
    assert state21["gate_verdict"] == "OD-87_EXEMPT", state21
    assert state21["exemption"]["decision"] == "OD-87", state21

    freeze22_path = breaker_dir / "slice_2.2.json"
    freeze22 = freeze_module.load_effective_freeze(freeze22_path)
    snapshots22 = [
        json.loads(line)
        for line in freeze22_path.with_name("slice_2.2.snapshots.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    state22 = breaker.gate_assessment(
        freeze22, snapshots22, today="2026-11-14", stage2_estimate_weeks=0.0,
    )
    assert state22["actual_verdict"] == "INSUFFICIENT", state22
    assert state22["assessment"]["insufficient_evidence"] is True, state22
    assert state22["gate_verdict"] == "OD-87_EXEMPT", state22

    od02 = breaker.gate_assessment(
        freeze21, snapshots21, today="2026-11-14", stage2_estimate_weeks=30.0,
    )
    assert od02["actual_verdict"] == "FAIL", od02
    assert od02["gate_verdict"] == "FAIL", od02


def test_record_snapshot_computes_frozen_rate_and_results_hash(tmp_path: pathlib.Path, capsys, monkeypatch) -> None:
    breaker = _load("slice_breaker_snapshot_record", "scripts/rust/slice_breaker.py")
    monkeypatch.setattr(breaker, "_today_utc", lambda: "2026-11-14")
    freeze = {
        "schema_version": 1,
        "slice": "2.4",
        "denominator": 3,
        "required_case_ids": ["case-a", "case-b", "case-c"],
        "window": {"start": "2026-11-08", "weeks": 3},
        "stage2_started_at": "2026-10-21",
    }
    freeze_path = tmp_path / "slice_2.4.json"
    freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
    results_path = tmp_path / "case-verdicts.json"
    results_path.write_text(json.dumps({
        "rows": [
            {"case_id": "case-a", "status": "PASS"},
            {"case_id": "case-b", "status": "FAIL"},
        ],
        "rate": 1.0,
    }), encoding="utf-8")
    snapshots_path = tmp_path / "slice_2.4.snapshots.ndjson"

    args = [
        "record-snapshot", "--freeze", str(freeze_path), "--results", str(results_path),
        "--run-id", "gpx-final2-20261114", "--snapshots", str(snapshots_path),
    ]
    assert breaker.main(args) == 0
    capsys.readouterr()
    snapshot = json.loads(snapshots_path.read_text(encoding="utf-8"))
    assert snapshot["denominator"] == 3
    assert snapshot["passed"] == 1
    assert snapshot["rate"] == pytest.approx(1 / 3)
    assert snapshot["results_sha256"] == hashlib.sha256(results_path.read_bytes()).hexdigest()
    assert snapshot["run_id"] == "gpx-final2-20261114"
    assert snapshot["date"] == "2026-11-14"

    with pytest.raises(SystemExit) as exc:
        breaker.main(args + ["--rate", "1.0"])
    assert exc.value.code == 2
