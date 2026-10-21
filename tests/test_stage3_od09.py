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


# --- 以下為 P9-T15 審查修正的補充測試（不改動上方既有測試） ---


def test_earliest_candidate_start_rejects_missing_ledger(tmp_path):
    # P2 ledger 為受追蹤的固定檔；缺漏代表刪除或路徑錯誤，不得視為「無候選」（fail closed，與 P2 freeze 一致）
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    import stage3_fakes as fakes

    with pytest.raises(HarnessError) as exc_info:
        od09_consume.earliest_candidate_start(
            fakes.empty_index(), ledger_path=tmp_path / "missing.ndjson"
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_earliest_candidate_start_rejects_ledger_directory(tmp_path):
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    import stage3_fakes as fakes

    ledger_dir = tmp_path / "ledger_dir"
    ledger_dir.mkdir()
    with pytest.raises(HarnessError) as exc_info:
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger_dir)
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_earliest_candidate_start_rejects_index_without_entries(tmp_path):
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    ledger = tmp_path / "candidate_longrun_starts.ndjson"
    ledger.write_bytes(b"")
    with pytest.raises(HarnessError) as exc_info:
        od09_consume.earliest_candidate_start({}, ledger_path=ledger)
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_derive_rejects_missing_ledger(tmp_path):
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    _, _, env, report, _ = _freeze_p2_fixture(tmp_path)
    import stage3_fakes as fakes

    with pytest.raises(HarnessError) as exc_info:
        od09_consume.derive_freeze_report(
            report,
            acceptance_path=env.acceptance,
            index=fakes.empty_index(),
            ledger_path=tmp_path / "missing.ndjson",
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_earliest_candidate_start_normalizes_z_suffix(tmp_path):
    # P2 check_frozen_before 於 Python 3.10 不接受 `Z`，故本模組須先正規化為 +00:00
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    ledger = tmp_path / "candidate_longrun_starts.ndjson"
    row = {
        "item_id": "S1B-L3-MIXED-48H-WINDOWS",
        "platform": "windows",
        "run_id": "s1b-1",
        "started_at": "2026-11-21T00:00:00Z",
    }
    ledger.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    assert (
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger)
        == "2026-11-21T00:00:00+00:00"
    )


def test_ledger_row_with_unicode_line_separator_is_not_split(tmp_path):
    # ensure_ascii=False 的合法紀錄可含 U+0085（NEL）；以 splitlines() 切行會誤判為壞行（U+2028／U+2029 見下方參數化測試）
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    ledger = tmp_path / "candidate_longrun_starts.ndjson"
    row = {
        "item_id": "S1B-L3-MIXED-48H-WINDOWS",
        "platform": "windows",
        "run_id": "run x\u0085y",
        "started_at": "2026-11-21T00:00:00+00:00",
    }
    ledger.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert (
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger)
        == "2026-11-21T00:00:00+00:00"
    )


def test_derive_note_carries_violations_and_hides_sentinel(tmp_path):
    from tools.stage3 import od09_consume

    _, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    import stage3_fakes as fakes

    passed = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=fakes.empty_index(),
        ledger_path=env.ledger,
    )
    assert passed.result == "PASS"
    assert "9999" not in passed.note
    assert passed.detail == ()

    idx = fakes.index_with_entry(
        "S1B-L3-MIXED-48H-WINDOWS",
        platform="windows",
        started_at=fakes_od09.ts(19, 0),
    )
    failed = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=idx,
        ledger_path=env.ledger,
    )
    assert failed.result == "FAIL"
    # note 只記數量；違規清單放在 detail，由 ingest 併入 entry note 一次（不得在 note 內重複）。
    assert f"違規 {len(failed.detail)} 項" in failed.note
    assert not any(item in failed.note for item in failed.detail)
    merged = failed.note + "；" + "；".join(failed.detail)
    assert all(merged.count(item) == 1 for item in failed.detail)


def test_check_main_accepts_full_prefix_and_normalizes_z(monkeypatch, tmp_path):
    from realhw.acceptance import od09_freeze
    from tools.stage3 import od09_consume

    seen: list[str] = []

    def check_frozen_before(path, started_at):
        seen.append(started_at)
        return []

    monkeypatch.setattr(od09_freeze, "check_frozen_before", check_frozen_before)
    rc = od09_consume.check_main(
        [
            "od09",
            "check",
            "--acceptance",
            str(tmp_path / "longrun_acceptance.yaml"),
            "--run-started-at",
            "2026-11-21T00:00:00Z",
        ]
    )
    assert rc == 0
    assert seen == ["2026-11-21T00:00:00+00:00"]


# --- P9-T15 第二輪審查修正：候選選取、時序、fail-closed 與 CLI 路徑的補充測試 ---


def _write_ledger(path: Path, started_values: list[str]) -> Path:
    rows = [
        {
            "item_id": "S1B-L3-MIXED-48H-WINDOWS",
            "platform": "windows",
            "run_id": f"ledger-{position}",
            "started_at": value,
        }
        for position, value in enumerate(started_values, start=1)
    ]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _candidate_items() -> list[tuple[str, str]]:
    from realhw.acceptance import od09_freeze

    return sorted(od09_freeze.CANDIDATE_LONGRUN_ITEMS.items())


@pytest.mark.parametrize(
    ("ledger_days", "index_day", "expected_day"),
    [
        ((25,), 22, 22),  # 索引 entry 較早
        ((22,), 25, 22),  # ledger 較早
        ((25, 24, 22), None, 22),  # 遞減排列的 ledger 仍取最小值
    ],
)
def test_earliest_candidate_start_returns_minimum_across_sources(
    tmp_path, ledger_days, index_day, expected_day
):
    from tools.stage3 import od09_consume

    import fakes_od09
    import stage3_fakes as fakes

    ledger = _write_ledger(
        tmp_path / "candidate_longrun_starts.ndjson",
        [fakes_od09.ts(day, 0) for day in ledger_days],
    )
    if index_day is None:
        index = fakes.empty_index()
    else:
        index = fakes.index_with_entry(
            "S3-L3-AB-48H-LINUX",
            platform="linux",
            started_at=fakes_od09.ts(index_day, 0),
        )
    assert od09_consume.earliest_candidate_start(index, ledger_path=ledger) == fakes_od09.ts(
        expected_day, 0
    )


def test_earliest_candidate_start_compares_instants_not_strings(tmp_path):
    # 字串字典序會判 01:00Z 較早，但 08:00+08:00 即 00:00Z，真正最早的是後者。
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    ledger = _write_ledger(
        tmp_path / "candidate_longrun_starts.ndjson",
        ["2026-11-21T01:00:00+00:00", "2026-11-21T08:00:00+08:00"],
    )
    assert (
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger)
        == "2026-11-21T08:00:00+08:00"
    )


def test_non_candidate_entries_are_ignored_by_timeline(tmp_path):
    # S0 基線等非候選項目不參與時序；其 started_at 即使無法解析也不得擋下判定（先過濾再解析）。
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    _, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    index = fakes.empty_index()
    index["entries"].extend(
        [
            {
                "entry_id": "baseline-linux",
                "item_id": "S0-L3-BASELINE-LINUX-48H",
                "platform": "linux",
                "result": "PASS",
                "started_at": fakes_od09.ts(1, 0),
            },
            {
                "entry_id": "non-candidate-bad-time",
                "item_id": "S0-OD09-DEFERRED-FROZEN",
                "platform": "linux",
                "result": "PASS",
                "started_at": "not-a-time",
            },
        ]
    )
    assert od09_consume.earliest_candidate_start(index, ledger_path=env.ledger) is None
    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=index,
        ledger_path=env.ledger,
    )
    assert derived.result == "PASS"
    assert derived.detail == ()


@pytest.mark.parametrize(("item_id", "platform"), _candidate_items())
def test_every_candidate_entry_before_freeze_fails(tmp_path, item_id, platform):
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    od09_freeze, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    idx = fakes.index_with_entry(item_id, platform=platform, started_at=fakes_od09.ts(19, 0))
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


_BAD_FREEZE_REPORTS = {
    "non_json": lambda sha: "{bad",
    "not_object": lambda sha: "[]",
    "empty_object": lambda sha: "{}",
    "sha_not_string": lambda sha: json.dumps(
        {"acceptance_sha256": 1, "frozen_at": "2026-11-20T09:00:00+00:00"}
    ),
    "missing_frozen_at": lambda sha: json.dumps({"acceptance_sha256": sha}),
    "naive_frozen_at": lambda sha: json.dumps(
        {"acceptance_sha256": sha, "frozen_at": "2026-11-20T09:00:00"}
    ),
    "unparsable_frozen_at": lambda sha: json.dumps(
        {"acceptance_sha256": sha, "frozen_at": "not-a-time"}
    ),
}


@pytest.mark.parametrize("case", sorted(_BAD_FREEZE_REPORTS))
def test_derive_rejects_malformed_freeze_report(tmp_path, case):
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    import stage3_fakes as fakes

    _, _, env, report, _ = _freeze_p2_fixture(tmp_path)
    sha = hashlib.sha256(env.acceptance.read_bytes()).hexdigest()
    report.write_text(_BAD_FREEZE_REPORTS[case](sha), encoding="utf-8")
    with pytest.raises(HarnessError) as exc_info:
        od09_consume.derive_freeze_report(
            report,
            acceptance_path=env.acceptance,
            index=fakes.empty_index(),
            ledger_path=env.ledger,
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_derive_pass_reports_source_file_and_frozen_at(tmp_path):
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    _, _, env, report, report_doc = _freeze_p2_fixture(tmp_path)
    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=fakes.empty_index(),
        ledger_path=env.ledger,
    )
    assert derived.result == "PASS"
    assert derived.files == (report,)
    assert derived.started_at == report_doc["frozen_at"]


def test_derive_invalid_when_threshold_value_changed_after_report(tmp_path):
    import yaml

    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    import stage3_fakes as fakes

    _, _, env, report, _ = _freeze_p2_fixture(tmp_path)
    acceptance = yaml.safe_load(env.acceptance.read_text(encoding="utf-8"))
    acceptance["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"] += 1
    env.acceptance.write_text(
        yaml.safe_dump(acceptance, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(HarnessError) as exc_info:
        od09_consume.derive_freeze_report(
            report,
            acceptance_path=env.acceptance,
            index=fakes.empty_index(),
            ledger_path=env.ledger,
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_check_main_missing_acceptance_fails_closed(capsys, tmp_path):
    from tools.stage3 import od09_consume

    rc = od09_consume.check_main(
        [
            "--acceptance",
            str(tmp_path / "missing_longrun_acceptance.yaml"),
            "--run-started-at",
            "2026-11-21T00:00:00+00:00",
        ]
    )
    assert rc == 1
    assert json.loads(capsys.readouterr().out.strip())["result"] == "FAIL"


def test_earliest_candidate_start_rejects_naive_ledger_timestamp(tmp_path):
    # 缺時區的紀錄不得被當成 UTC 而放行（fail closed）；P2 目前仍會接受 naive 登記，本測試鎖住消費端的嚴格行為。
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    import stage3_fakes as fakes

    ledger = _write_ledger(tmp_path / "candidate_longrun_starts.ndjson", ["2026-11-21T00:00:00"])
    with pytest.raises(HarnessError) as exc_info:
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger)
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


@pytest.mark.parametrize("separator", [" ", " "])
def test_ledger_row_with_line_separator_is_not_split(tmp_path, separator):
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    ledger = tmp_path / "candidate_longrun_starts.ndjson"
    row = {
        "item_id": "S1B-L3-MIXED-48H-WINDOWS",
        "platform": "windows",
        "run_id": f"run x{separator}y",
        "started_at": "2026-11-21T00:00:00+00:00",
    }
    ledger.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert (
        od09_consume.earliest_candidate_start(fakes.empty_index(), ledger_path=ledger)
        == "2026-11-21T00:00:00+00:00"
    )


def test_derive_od09_freeze_requires_index_doc(monkeypatch, tmp_path):
    # `derive` 的 index_doc 預設為 None；缺索引不得退化為「無候選長跑」而 PASS（fail closed）。
    from tools.stage3 import evidence_ingest
    from tools.stage3.reason_codes import HarnessError

    _, _, env, report, _ = _freeze_p2_fixture(tmp_path)
    monkeypatch.setattr(evidence_ingest, "_ACCEPTANCE_PATH", env.acceptance)
    with pytest.raises(HarnessError) as exc_info:
        evidence_ingest.derive(
            "od09-freeze",
            report,
            item=None,
            cases={},
            lock={},
            release_manifest=None,
            platform="linux",
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"
    assert "index_doc" in exc_info.value.detail


@pytest.mark.parametrize("started", ["garbage", "2026-11-21T00:00:00"])
def test_od09_check_malformed_timestamp_reports_cli_error(capsys, tmp_path, started):
    # 時間格式錯誤須經 tools.stage3.main 轉為 HARNESS_CLI_ERROR（exit 2 與一行 JSON），不得以 SystemExit 穿出。
    from tools.stage3.__main__ import main

    rc = main(
        [
            "od09",
            "check",
            "--acceptance",
            str(tmp_path / "longrun_acceptance.yaml"),
            "--run-started-at",
            started,
        ]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["reason_code"] == "HARNESS_CLI_ERROR"


# --- P9-T15 第三輪審查修正：ledger 時序 FAIL、凍結邊界、報告來源綁定與 ingest 掛載點的補充測試 ---


def test_derive_fail_when_ledger_row_predates_freeze(tmp_path):
    # 候選開跑 ledger 的紀錄早於凍結 → FAIL，且三個延後項皆列為違規（與索引 entry 路徑一致）
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    od09_freeze, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    _write_ledger(env.ledger, [fakes_od09.ts(19, 0)])
    derived = od09_consume.derive_freeze_report(
        report,
        acceptance_path=env.acceptance,
        index=fakes.empty_index(),
        ledger_path=env.ledger,
    )
    assert derived.result == "FAIL"
    assert sorted(derived.detail) == sorted(
        f"{key} frozen after run start" for key in od09_freeze.DEFERRED_KEYS
    )


def test_candidate_start_boundary_is_strict_at_freeze_instant(tmp_path):
    # check_frozen_before 以 `frozen_at >= started` 判違規：起點等於凍結時刻即 FAIL，晚一秒才 PASS
    from datetime import datetime, timedelta

    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    _, _, env, report, report_doc = _freeze_p2_fixture(tmp_path)
    frozen = datetime.fromisoformat(report_doc["frozen_at"])
    _write_ledger(env.ledger, [frozen.isoformat()])
    equal = od09_consume.derive_freeze_report(
        report, acceptance_path=env.acceptance, index=fakes.empty_index(), ledger_path=env.ledger
    )
    assert equal.result == "FAIL"
    _write_ledger(env.ledger, [(frozen + timedelta(seconds=1)).isoformat()])
    later = od09_consume.derive_freeze_report(
        report, acceptance_path=env.acceptance, index=fakes.empty_index(), ledger_path=env.ledger
    )
    assert later.result == "PASS"
    assert later.detail == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: {**doc, "frozen_at": "2000-01-01T00:00:00+00:00"},
        lambda doc: {**doc, "revision": "0000.0"},
        lambda doc: {k: v for k, v in doc.items() if k != "revision"},
        lambda doc: {
            **doc,
            "thresholds": {k: v for k, v in doc["thresholds"].items() if k != "resources"},
        },
        lambda doc: {**doc, "thresholds": {**doc["thresholds"], "extra": {}}},
    ],
    ids=["forged_frozen_at", "revision_mismatch", "revision_missing",
         "thresholds_missing_key", "thresholds_extra_key"],
)
def test_derive_invalid_when_report_not_bound_to_live_acceptance(tmp_path, mutate):
    # 只附正確 acceptance_sha256 的偽造報告不得寫入 started_at（provenance）；三個延後項皆已凍結時一律 INVALID
    from tools.stage3 import od09_consume
    from tools.stage3.reason_codes import HarnessError

    import stage3_fakes as fakes

    _, _, env, report, report_doc = _freeze_p2_fixture(tmp_path)
    report.write_text(json.dumps(mutate(report_doc)), encoding="utf-8")
    with pytest.raises(HarnessError) as exc_info:
        od09_consume.derive_freeze_report(
            report,
            acceptance_path=env.acceptance,
            index=fakes.empty_index(),
            ledger_path=env.ledger,
        )
    assert exc_info.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_derive_accepts_equivalent_utc_spelling_of_frozen_at(tmp_path):
    # 同一瞬間的 `Z` 寫法與 `+00:00` 等價，不得因字串不同而誤拒
    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    _, _, env, report, report_doc = _freeze_p2_fixture(tmp_path)
    report.write_text(
        json.dumps({**report_doc, "frozen_at": report_doc["frozen_at"].replace("+00:00", "Z")}),
        encoding="utf-8",
    )
    derived = od09_consume.derive_freeze_report(
        report, acceptance_path=env.acceptance, index=fakes.empty_index(), ledger_path=env.ledger
    )
    assert derived.result == "PASS"


def test_derive_unfrozen_acceptance_stays_fail_not_invalid(tmp_path):
    # 現行 acceptance 尚未全部凍結時不做來源綁定，判定由 check_frozen_before 給出 FAIL（與 T14B 既有 od09 ingest 路徑一致）
    import yaml

    from tools.stage3 import od09_consume

    import stage3_fakes as fakes

    _, _, env, report, report_doc = _freeze_p2_fixture(tmp_path)
    acceptance = yaml.safe_load(env.acceptance.read_text(encoding="utf-8"))
    acceptance["thresholds"]["resources"]["status"] = "deferred"
    env.acceptance.write_text(
        yaml.safe_dump(acceptance, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    report_doc["acceptance_sha256"] = hashlib.sha256(env.acceptance.read_bytes()).hexdigest()
    report.write_text(json.dumps(report_doc), encoding="utf-8")
    derived = od09_consume.derive_freeze_report(
        report, acceptance_path=env.acceptance, index=fakes.empty_index(), ledger_path=env.ledger
    )
    assert derived.result == "FAIL"
    assert "resources not frozen" in derived.detail


def test_evidence_ingest_od09_hook_uses_module_ledger_and_acceptance(monkeypatch, tmp_path):
    # `derive` 的 od09-freeze 掛載點須同時使用 evidence_ingest 模組的 ledger 與 acceptance 路徑（可被測試隔離）
    from realhw.acceptance import od09_freeze
    from tools.stage3 import evidence_ingest

    import stage3_fakes as fakes

    _, fakes_od09, env, report, _ = _freeze_p2_fixture(tmp_path)
    assert evidence_ingest._LEDGER_PATH == od09_freeze.LEDGER_PATH
    monkeypatch.setattr(evidence_ingest, "_ACCEPTANCE_PATH", env.acceptance)
    monkeypatch.setattr(evidence_ingest, "_LEDGER_PATH", env.ledger)
    _write_ledger(env.ledger, [fakes_od09.ts(19, 0)])
    failed = evidence_ingest.derive(
        "od09-freeze",
        report,
        item=None,
        cases={},
        lock={},
        release_manifest=None,
        platform="linux",
        index_doc=fakes.empty_index(),
    )
    assert failed.result == "FAIL"
    assert sorted(failed.detail) == sorted(
        f"{key} frozen after run start" for key in od09_freeze.DEFERRED_KEYS
    )
    env.ledger.write_bytes(b"")
    passed = evidence_ingest.derive(
        "od09-freeze",
        report,
        item=None,
        cases={},
        lock={},
        release_manifest=None,
        platform="linux",
        index_doc=fakes.empty_index(),
    )
    assert passed.result == "PASS"
