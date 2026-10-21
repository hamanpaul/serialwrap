"""OD-09 凍結結果的 Stage 3 消費端回歸測試。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _freeze_p2_fixture(tmp_path: Path):
    """透過 P2 凍結流程建立隔離的 acceptance 與報告。"""
    import fakes_od09

    from realhw.acceptance import od09_freeze

    env = fakes_od09.od09_env(tmp_path)
    proposal = od09_freeze.propose(
        env.linux_stats,
        env.windows_stats,
        baseline_gates=env.baseline_gates,
    )
    report_doc = od09_freeze.freeze(
        env.acceptance,
        proposal,
        decision_id="OD-09-F1",
        approved_by="owner",
        overrides={},
        stage0_index=env.stage0_index,
        evidence_root=env.evidence_root,
        ledger_path=env.ledger,
        addenda_path=env.addenda,
        now=fakes_od09.ts(20, 9),
    )
    report = tmp_path / "od09-freeze.json"
    report.write_text(json.dumps(report_doc), encoding="utf-8")
    return od09_freeze, fakes_od09, env, report, report_doc


def test_p9_has_no_od09_writer(capsys):
    # R4-F2：延後項唯一寫入者是 P2 Task 16；P9 不得出現第二個寫入端。
    assert not (ROOT / "tools" / "stage3" / "od09_freeze.py").exists()
    for p in (ROOT / "tools" / "stage3").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert not (
            "longrun_acceptance" in src
            and (
                "safe_dump" in src
                or "atomic_write_text" in src
                or "write_text" in src
            )
        ), p
        assert "def freeze(" not in src and "def propose(" not in src and "def register_candidate_start(" not in src, p
    from tools.stage3.__main__ import build_parser

    build_parser().parse_args(
        [
            "od09",
            "check",
            "--acceptance",
            "a.yaml",
            "--run-started-at",
            "2026-11-21T00:00:00+00:00",
        ]
    )
    for sub in ("stats", "propose", "freeze", "register-candidate"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["od09", sub])
        assert "invalid choice" in capsys.readouterr().err


def test_p2_od09_api_consumed():
    from realhw.acceptance import od09_freeze

    cat = json.loads(
        (ROOT / "tests" / "acceptance" / "stage_exits.json").read_text(encoding="utf-8")
    )["items"]
    by_id = {item["item_id"]: item for item in cat}
    assert {
        key: value["platform"]
        for key, value in by_id.items()
        if value["candidate_longrun"]
    } == dict(od09_freeze.CANDIDATE_LONGRUN_ITEMS)
    for item in (*od09_freeze.BASELINE_ITEMS.values(), od09_freeze.FREEZE_ITEM):
        assert by_id[item]["stage"] == "0"
    assert by_id[od09_freeze.FREEZE_ITEM]["evidence_kind"] == "od09-freeze"
    codes = json.loads(
        (ROOT / "tools" / "stage3" / "stage3_reason_codes.json").read_text(encoding="utf-8")
    )["codes"]
    assert set(od09_freeze.OD09_CODES) <= set(codes)


def test_derive_pass_after_stage0_freeze(tmp_path):
    from tools.stage3 import od09_consume

    od09_freeze, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    od09_freeze.register_candidate_start(
        env.acceptance,
        item_id="S1B-L3-MIXED-48H-WINDOWS",
        platform="windows",
        run_id="s1b-1",
        started_at=fakes_od09.ts(21, 0),
        ledger_path=env.ledger,
    )
    import stage3_fakes as fakes

    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=fakes.empty_index(),
        ledger_path=env.ledger,
    )
    assert derived.result == "PASS"
    assert od09_consume.earliest_candidate_start(
        fakes.empty_index(), ledger_path=env.ledger
    ) == fakes_od09.ts(21, 0)


def test_derive_fail_when_candidate_entry_predates_freeze(tmp_path):
    from tools.stage3 import od09_consume

    od09_freeze, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    import stage3_fakes as fakes

    idx = fakes.index_with_entry(
        "S1B-L3-MIXED-48H-WINDOWS",
        platform="windows",
        started_at=fakes_od09.ts(19, 0),
    )
    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=idx,
        ledger_path=env.ledger,
    )
    assert derived.result == "FAIL"
    assert sorted(derived.detail) == sorted(
        f"{key} frozen after run start" for key in od09_freeze.DEFERRED_KEYS
    )


def test_derive_invalid_when_acceptance_changed_after_report(tmp_path):
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    _, _, env, report, _ = _freeze_p2_fixture(tmp_path)
    env.acceptance.write_bytes(env.acceptance.read_bytes() + b"\n")
    import stage3_fakes as fakes

    with pytest.raises(HarnessError) as exc_info:
        od09_consume.derive_freeze_report(
            report,
            acceptance_path=env.acceptance,
            index=fakes.empty_index(),
            ledger_path=env.ledger,
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_derive_pass_without_candidates(tmp_path):
    from tools.stage3 import od09_consume

    _, _, env, report, _ = _freeze_p2_fixture(tmp_path)
    import stage3_fakes as fakes

    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=fakes.empty_index(),
        ledger_path=env.ledger,
    )
    assert derived.result == "PASS"


def test_derive_fail_when_report_claims_frozen_but_values_null(tmp_path):
    import yaml

    from tools.stage3 import od09_consume

    _, _, env, report, report_doc = _freeze_p2_fixture(tmp_path)
    acceptance = yaml.safe_load(env.acceptance.read_text(encoding="utf-8"))
    acceptance["thresholds"]["comparison_uncertainty"]["per_platform"]["windows"][
        "max_reruns"
    ] = None
    env.acceptance.write_text(
        yaml.safe_dump(acceptance, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    report_doc["acceptance_sha256"] = hashlib.sha256(env.acceptance.read_bytes()).hexdigest()
    report.write_text(json.dumps(report_doc), encoding="utf-8")
    import stage3_fakes as fakes

    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=fakes.empty_index(),
        ledger_path=env.ledger,
    )
    assert derived.result == "FAIL"
    assert "comparison_uncertainty.per_platform.windows.max_reruns missing" in derived.detail


def test_earliest_candidate_start_rejects_malformed_ledger(tmp_path):
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    ledger = tmp_path / "candidate_longrun_starts.ndjson"
    ledger.write_text("{bad\n", encoding="utf-8")
    import stage3_fakes as fakes

    with pytest.raises(HarnessError) as exc_info:
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger)
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_od09_check_delegates_to_p2(monkeypatch, tmp_path):
    from realhw.acceptance import od09_freeze
    from tools.stage3.__main__ import main

    acceptance = tmp_path / "longrun_acceptance.yaml"
    run_started_at = "2026-11-21T00:00:00+00:00"
    calls = []

    def check_frozen_before(path, started_at):
        calls.append((path, started_at))
        return [] if len(calls) == 1 else ["x"]

    monkeypatch.setattr(od09_freeze, "check_frozen_before", check_frozen_before)
    argv = [
        "od09",
        "check",
        "--acceptance",
        str(acceptance),
        "--run-started-at",
        run_started_at,
    ]
    assert main(argv) == 0
    assert main(argv) == 1
    assert len(calls) == 2
    assert Path(calls[0][0]) == acceptance
    assert calls[0][1] == run_started_at
    assert Path(calls[1][0]) == acceptance
    assert calls[1][1] == run_started_at


def test_ingest_od09_freeze_kind_uses_consumer(monkeypatch, tmp_path):
    from tools.stage3 import evidence_index, evidence_ingest, od09_consume

    import fakes_evidence

    index_path = fakes_evidence.evidence_index(
        tmp_path, stage="0", contract="c" * 40, platforms=("linux",)
    )
    catalog = evidence_index.load_exit_catalog()
    report = tmp_path / "od09-freeze.json"
    report.write_text("{}", encoding="utf-8")
    environment = tmp_path / "environment.json"
    environment.write_text(
        json.dumps(
            {
                "os_build": "test",
                "cpu": "test",
                "ram_mb": 1,
                "filesystem": "test",
                "av_product": "test",
                "drivers": "test",
                "board_firmware": "test",
                "toolchains": "test",
            }
        ),
        encoding="utf-8",
    )
    derived = evidence_ingest.Derived(
        result="PASS",
        artifact_id=None,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=None,
        runtime_tree_sha256=None,
        xfail_cases=(),
        started_at="2026-11-20T00:00:00+00:00",
        files=(report,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note="",
    )
    monkeypatch.setattr(od09_consume, "derive_freeze_report", lambda *args, **kwargs: derived)
    entry = evidence_ingest.ingest(
        index_path,
        kind="od09-freeze",
        item_id="S0-OD09-DEFERRED-FROZEN",
        platform="linux",
        report=report,
        evidence_root=tmp_path / "evidence",
        catalog=catalog,
        cases={},
        lock={},
        environment=environment,
        now="2026-11-21T00:00:00+00:00",
    )
    assert entry.result == "PASS"
