"""P9-T7B L1.5 A/B 差分工具的失敗測試（TDD red）。"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "contracts" / "ab_normalization.yaml"
SC15_CASE_ID = "sc15-console-ring-drop-oldest"


def _load_ab_diff():
    """延遲匯入待實作的差分工具，讓整套測試仍可完成 collection。"""
    return importlib.import_module("tools.stage3.ab_diff")


def _prepare_reports(tmp_path: Path):
    """用共用 suite fake 產生兩側報告樹。"""
    ab_diff = _load_ab_diff()
    fakes_suites = importlib.import_module("fakes_suites")
    env = fakes_suites.ab_env(tmp_path, layer="l1.5")
    report_dirs: dict[str, Path] = {}

    for side in env.m.sides:
        report_dir = tmp_path / "reports" / side.label
        report_dirs[side.label] = report_dir
        env.runner(
            [
                sys.executable,
                str(env.repo / "func-test" / "runner.py"),
                "--mode",
                "bridge",
                "--dut-mode",
                "local-process",
                "--report-dir",
                str(report_dir),
                "--selftest-report",
                str(env.m.suite.selftest_report.path),
            ],
            env={
                "SW_STAGE3_SIDE": side.label,
                "SERIALWRAP_CLI_EXE": str(side.cli_exe),
                "SERIALWRAP_DAEMON_EXE": str(side.daemon_exe),
                "SERIALWRAP_RUN_DIR": str(tmp_path / "instances" / side.label),
            },
        )

    return ab_diff, report_dirs["A"], report_dirs["B"]


def _case_ids(report_dir: Path) -> list[str]:
    return sorted({path.parent.parent.parent.name for path in report_dir.glob("*/bridge/*/manifest.json")})


def _case_runs(report_dir: Path, case_id: str) -> list[Path]:
    return sorted(path for path in (report_dir / case_id / "bridge").iterdir() if path.is_dir())


def _equalize_reports(a_dir: Path, b_dir: Path) -> None:
    """以 A 側證據統一兩側的同名報告項目，建立差分測試的共同基準。"""
    for case_id in _case_ids(a_dir):
        for a_run in _case_runs(a_dir, case_id):
            b_run = b_dir / a_run.relative_to(a_dir)
            for name in ("manifest.json", "bridge-manifest.json", "comparison.json", "cli.ndjson"):
                shutil.copyfile(a_run / name, b_run / name)


def _first_run(report_dir: Path, case_id: str) -> Path:
    return _case_runs(report_dir, case_id)[0]


def _set_comparison(
    report_dir: Path,
    case_id: str,
    *,
    verdict: str | None = None,
    invariants: list[dict[str, str]] | None = None,
) -> None:
    path = _first_run(report_dir, case_id) / "comparison.json"
    comparison = json.loads(path.read_text(encoding="utf-8"))
    if verdict is not None:
        comparison["verdict"] = verdict
    if invariants is not None:
        comparison["invariants"] = invariants
    path.write_text(json.dumps(comparison, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _set_cli_calls(report_dir: Path, case_id: str, calls: list[dict[str, Any]]) -> None:
    path = _first_run(report_dir, case_id) / "cli.ndjson"
    with path.open("w", encoding="utf-8") as stream:
        for index, call in enumerate(calls, start=1):
            response = call["response"]
            record = {
                "argv": call.get("argv", ["session", "list"]),
                "mono_start_ns": index,
                "rc": call.get("rc", 0),
                "response": response,
                "stderr": "",
                "stdout": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            }
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _ensure_case(report_dir: Path, case_id: str, template_case_id: str) -> None:
    if (report_dir / case_id).exists():
        return
    shutil.copytree(report_dir / template_case_id, report_dir / case_id)
    for run_dir in _case_runs(report_dir, case_id):
        for name in ("manifest.json", "comparison.json"):
            path = run_dir / name
            document = json.loads(path.read_text(encoding="utf-8"))
            document["case_id"] = case_id
            path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _diff(
    ab_diff,
    a_dir: Path,
    b_dir: Path,
    *,
    required_case_ids: set[str],
    case_changes: dict[str, str] | None = None,
) -> dict[str, Any]:
    return ab_diff.diff_l15(
        a_dir,
        b_dir,
        registered={"SC-15": "absolute_only"},
        case_changes=case_changes or {},
        required_case_ids=required_case_ids,
        rules_path=RULES_PATH,
    )


def test_identical_reports_ok(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["ok"] is True
    assert result["differences"] == []


def test_missing_required_case_breaks_integrity(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    missing_case_id = _case_ids(a_dir)[0]
    shutil.rmtree(b_dir / missing_case_id)

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["integrity"]["b"] is False
    assert any(missing_case_id in str(problem) for problem in result["integrity_problems"]["b"])


def test_insufficient_comparison_breaks_integrity(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_comparison(a_dir, case_id, verdict="INSUFFICIENT_EVIDENCE")

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["integrity"]["a"] is False


def test_invariant_fail_both_sides_reported(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    failed = [{"id": "zero_tx_on_reject", "verdict": "FAIL"}]
    _set_comparison(a_dir, case_id, verdict="FAIL", invariants=failed)
    _set_comparison(b_dir, case_id, verdict="FAIL", invariants=failed)

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert f"A:{case_id}:zero_tx_on_reject" in result["safety_violations"]
    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]


def test_exception_message_ignored_g1(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(
        a_dir,
        case_id,
        [
            {
                "argv": ["session", "list"],
                "response": {
                    "id": 1,
                    "ok": False,
                    "error_code": "EXCEPTION",
                    "message": "Python detail",
                },
            }
        ],
    )
    _set_cli_calls(
        b_dir,
        case_id,
        [
            {
                "argv": ["session", "list"],
                "response": {
                    "id": 1,
                    "ok": False,
                    "error_code": "EXCEPTION",
                    "message": "Rust detail",
                },
            }
        ],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_set_path_order_ignored_g2(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(
        a_dir,
        case_id,
        [
            {"argv": ["event", "reload"], "response": {"ok": True, "added": ["alpha", "beta"]}},
            {"argv": ["event", "tail"], "response": {"ok": True, "entries": ["alpha", "beta"]}},
        ],
    )
    _set_cli_calls(
        b_dir,
        case_id,
        [
            {"argv": ["event", "reload"], "response": {"ok": True, "added": ["beta", "alpha"]}},
            {"argv": ["event", "tail"], "response": {"ok": True, "entries": ["beta", "alpha"]}},
        ],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert len(result["differences"]) == 1
    assert result["differences"][0]["kind"] == "value"


def test_volatile_and_root_normalized(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(
        a_dir,
        case_id,
        [{"response": {"pid": 4101, "state_path": f"{a_dir}/instance/state.json"}}],
    )
    _set_cli_calls(
        b_dir,
        case_id,
        [{"response": {"pid": 4202, "state_path": f"{b_dir}/instance/state.json"}}],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_difference_attributed_to_case_change(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    template_case_id = _case_ids(a_dir)[0]
    _ensure_case(a_dir, SC15_CASE_ID, template_case_id)
    _ensure_case(b_dir, SC15_CASE_ID, template_case_id)
    _equalize_reports(a_dir, b_dir)
    _set_cli_calls(a_dir, SC15_CASE_ID, [{"response": {"value": "python"}}])
    _set_cli_calls(b_dir, SC15_CASE_ID, [{"response": {"value": "rust"}}])

    result = _diff(
        ab_diff,
        a_dir,
        b_dir,
        required_case_ids={SC15_CASE_ID},
        case_changes={SC15_CASE_ID: "SC-15"},
    )

    assert len(result["differences"]) == 1
    assert result["differences"][0]["registered_change"] == "SC-15"
