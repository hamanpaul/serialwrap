"""reporter 單測（stub RunResult／stub plugin）——不 import testpilot。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import core  # noqa: E402
from serialwrap_reliability.reporter import ReliabilityReporter  # noqa: E402


def _mk_run(tmp_path: Path):
    core.ensure_realhw_importable()
    from realhw import harness

    report_dir = tmp_path / "b-log-run"
    artifact_dir = tmp_path / "tp-artifacts"
    plugin = SimpleNamespace(
        run_results=[
            ("p0-doctor", harness.CaseResult("PASS")),
            (
                "p0-cmd-async",
                harness.CaseResult(
                    "FAIL",
                    reason="marker 未見",
                    category="test",
                    reason_code="marker_missing",
                    evidence={"cmd": "p0-cmd-async/cmd.json"},
                ),
            ),
        ],
        run_meta={
            "version": "serialwrap 0.2.3",
            "git": "abc1234",
            "tiers": "plugin",
            "started_at": "260720-120000",
            "preflight_notes": [],
        },
        ctx=SimpleNamespace(report_dir=report_dir),
    )
    run_result = SimpleNamespace(
        run_id="20260720T120000000000",
        fw_ver="serialwrap 0.2.3",
        artifact_dir=artifact_dir,
        artifacts={"realhw_meta": dict(plugin.run_meta)},
        cases=[
            SimpleNamespace(retry=SimpleNamespace(diagnostic_status="Pass")),
            SimpleNamespace(retry=SimpleNamespace(diagnostic_status="FailTest")),
        ],
    )
    return plugin, run_result, report_dir, artifact_dir


def test_build_reports_writes_md_json_and_copies(tmp_path):
    plugin, run_result, report_dir, artifact_dir = _mk_run(tmp_path)
    payload = ReliabilityReporter(plugin=plugin).build_reports(run_result)

    assert (report_dir / "report.md").is_file()
    assert (report_dir / "report.json").is_file()
    assert (artifact_dir / "report.md").is_file()
    assert (artifact_dir / "report.json").is_file()

    data = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert data["meta"]["version"] == "serialwrap 0.2.3"
    assert data["meta"]["fw_ver"] == "serialwrap 0.2.3"
    assert data["meta"]["run_id"] == "20260720T120000000000"
    ids = [result["id"] for result in data["results"]]
    assert ids == ["p0-doctor", "p0-cmd-async"]

    assert payload["plugin"] == "serialwrap_reliability"
    assert payload["diagnostic_counts"] == {"Pass": 1, "FailTest": 1}
    assert payload["cases"] == 2
    assert payload["reports"]["report.md"].endswith("report.md")


def test_build_reports_falls_back_to_plugin_meta_and_artifact_dir(tmp_path):
    plugin, run_result, report_dir, artifact_dir = _mk_run(tmp_path)
    plugin.ctx = None
    plugin.run_meta["version"] = "serialwrap 0.2.4"
    run_result.artifacts = {}
    payload = ReliabilityReporter(plugin=plugin).build_reports(run_result)

    data = json.loads((artifact_dir / "report.json").read_text(encoding="utf-8"))
    assert data["meta"]["version"] == "serialwrap 0.2.4"
    assert payload["deployed_version"] == "serialwrap 0.2.4"
    assert payload["report_dir"] == str(artifact_dir)
    assert payload["reports"]["report.json"] == str(artifact_dir / "report.json")
