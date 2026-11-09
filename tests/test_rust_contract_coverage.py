from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
    pytest.mark.xfail(strict=True, reason="P8-T15 階段 A：契約覆蓋工具尚未實作"),
]

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "rust" / "contract_coverage.py"
REV = {
    "contract_revision": "r",
    "cases_yaml_sha256": "c" * 64,
    "cases_lock_sha256": "d" * 64,
    "tool": {"name": "contract_coverage", "revision": "t"},
    "started_at": "x",
    "finished_at": "y",
}


def _module():
    spec = importlib.util.spec_from_file_location("contract_coverage_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def _results(run: Path, label: str, platform: str, rows: list[dict], **over) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    cli = run / "cli-artifact"
    daemon = run / "daemon-artifact"
    cli.write_bytes(b"synthetic cli artifact")
    daemon.write_bytes(b"synthetic daemon artifact")
    doc = {
        "label": label,
        "platform": platform,
        "cli_exe": str(cli),
        "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        "daemon_exe": str(daemon),
        "daemon_sha256": hashlib.sha256(daemon.read_bytes()).hexdigest(),
        **REV,
        "contract_revision": _module()._contract_revision(ROOT / "contracts"),
        "cases_yaml_sha256": hashlib.sha256((ROOT / "tests/acceptance/cases.yaml").read_bytes()).hexdigest(),
        "cases_lock_sha256": hashlib.sha256((ROOT / "tests/acceptance/cases.lock.json").read_bytes()).hexdigest(),
        "collected": sorted({row["nodeid"] for row in rows}),
        "exit_code": 0,
        "finished": True,
        "results": rows,
    }
    doc.update(over)
    path = run / f"results-{label}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _row(nodeid, outcome="passed", phase="call"):
    return {"nodeid": nodeid, "outcome": outcome, "phase": phase, "skip_reason": None, "evidence": []}


def _item(*, kind="rpc_method", platforms=("linux",), nodeids=(), **over):
    return {
        "kind": kind,
        "platforms": list(platforms),
        "rust_only": False,
        "l1_only": False,
        "owner_phase": "P8",
        "nodeids": list(nodeids),
        "blocked_caps": [],
        **over,
    }


def test_unmapped_contract_id_is_reported():
    cc = _module()
    report = cc.evaluate({"rpc_method:unmapped": _item()}, [])
    assert "rpc_method:unmapped" in report["missing"].get("P8", [])
    assert report["missing_reasons"]["rpc_method:unmapped"] == "coverage_map has no nodeid"


def test_missing_run_dir_is_rejected(tmp_path):
    result = _run(
        "--run-dir", str(tmp_path / "missing-run"),
        "--only", "rpc_method:mcu.status",
        "--json", str(tmp_path / "o.json"),
    )
    output = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert output["missing"], "沒有結果檔時不得產生空缺口的成功報告"


def test_empty_nodeid_is_not_coverage():
    cc = _module()
    report = cc.evaluate({"rpc_method:empty": _item(nodeids=[""])}, [])
    assert "rpc_method:empty" not in report["covered"]
    assert report["missing_reasons"]["rpc_method:empty"] == "coverage_map contains an empty nodeid"


def test_blocked_owner_case_never_counts_as_pass():
    cc = _module()
    item = _item(
        kind="case",
        platforms=("windows",),
        nodeids=("t::owner_case",),
        blocked_caps=["windows_com"],
        blocked_reason="需要擁有者 Windows COM 證據",
    )
    results = [
        {
            "label": label,
            "platform": "windows",
            "finished": True,
            "exit_code": 0,
            "collected": ["t::owner_case"],
            "results": [_row("t::owner_case")],
            **REV,
            "cli_sha256": "0" * 64,
            "daemon_sha256": "1" * 64,
        }
        for label in ("rust-windows", "python-windows")
    ]
    report = cc.evaluate({"case:owner_case": item}, results)
    assert "case:owner_case" in report["blocked_owner"]
    assert "case:owner_case" not in report["covered"]


def test_extractors_match_real_contracts():
    cc = _module()
    universe = cc.universe(
        ROOT / "contracts",
        ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json",
        ROOT / "contracts/coverage_map.yaml",
    )
    kinds = {value["kind"] for value in universe.values()}
    assert {"error_code", "last_error", "rpc_method", "cli_cmd", "bc", "schema", "case"} <= kinds
    rpc_ids = [item for item in universe if item.startswith("rpc_method:")]
    assert len(rpc_ids) == 55 and "rpc_method:daemon.handoff_stop" in universe
    assert "rpc_method:mcu.status" in universe and "error_code:FLASHING_BUSY" in universe
    assert "bc:SC-32" in universe
    assert "bc:SC-33" not in universe, "OD-79 已撤回 SC-33"
    assert "bc:SC-03" in universe
    cases = _cc._read_yaml(ROOT / "tests/acceptance/cases.yaml")["cases"]
    gates_case = next(case for case in cases if case["case_id"] == "s24-xfer-session-gates")
    assert "SC-03" in gates_case["behavior_changes"] and "SC-33" not in gates_case["behavior_changes"]
    assert "case:s24-flash-sync-replay" in universe
    assert universe["error_code:ECHO_ACK_UNSUPPORTED"]["l1_only"] is True


def test_skipped_and_teardown_error_never_count(tmp_path):
    run = tmp_path / "run1"
    _results(run, "rust-linux", "linux", [_row("t::a", "skipped"), _row("t::b", "error", "teardown")])
    _results(run, "python-linux", "linux", [_row("t::a"), _row("t::b")])
    result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "rpc_method:mcu.status", "--map", "rpc_method:mcu.status=t::a,t::b")
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1
    assert "rpc_method:mcu.status" in sum(output["missing"].values(), [])


def test_blocked_owner_cases_are_reported_not_covered(tmp_path):
    run = tmp_path / "run2"
    _results(run, "rust-linux", "linux", [_row("t::b")])
    _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "case:f7-binary-roundtrip-md5", "--map", "case:f7-binary-roundtrip-md5=t::b")
    output = json.loads((tmp_path / "o.json").read_text())
    assert "case:f7-binary-roundtrip-md5" in output["blocked_owner"]
    assert "case:f7-binary-roundtrip-md5" not in output["covered"]


def test_ab_mismatch_detected(tmp_path):
    run = tmp_path / "run3"
    _results(run, "rust-linux", "linux", [_row("t::c")])
    _results(run, "python-linux", "linux", [_row("t::c", "failed")])
    result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY", "--map", "error_code:SESSION_BUSY=t::c")
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1
    assert "error_code:SESSION_BUSY" in output["ab_mismatch"]


def test_identity_mismatch_rejected(tmp_path):
    run = tmp_path / "run4"
    _results(run, "rust-linux", "linux", [_row("t::d")])
    _results(run, "python-linux", "linux", [_row("t::d")], cases_yaml_sha256="e" * 64)
    result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"))
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1 and output["identity_errors"]


def test_stale_artifact_hash_rejected(tmp_path):
    run = tmp_path / "run5"
    _results(run, "rust-linux", "linux", [_row("t::e")])
    result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--expect-daemon-sha256", "2" * 64)
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1 and output["identity_errors"]


def test_evidence_hash_mismatch_rejected(tmp_path):
    run = tmp_path / "run6"
    run.mkdir()
    evidence = run / "ev.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _row("t::f")
    row["evidence"] = [{"path": str(evidence), "sha256": "0" * 64}]
    _results(run, "rust-linux", "linux", [row])
    result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"))
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1 and output["identity_errors"]


def test_windows_only_id_does_not_require_linux(tmp_path):
    run = tmp_path / "run7"
    _results(run, "rust-windows", "windows", [_row("t::w")])
    _results(run, "python-windows", "windows", [_row("t::w")])
    result = _run(
        "--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:X_WIN_ONLY",
        "--map", "error_code:X_WIN_ONLY=t::w", "--platforms", "error_code:X_WIN_ONLY=windows",
    )
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 0 and "error_code:X_WIN_ONLY" in output["covered"]


def test_all_mapped_nodeids_required_not_any(tmp_path):
    run = tmp_path / "run8"
    _results(run, "rust-linux", "linux", [_row("t::g1"), _row("t::g2", "skipped")])
    _results(run, "python-linux", "linux", [_row("t::g1"), _row("t::g2")])
    result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "rpc_method:mcu.status", "--map", "rpc_method:mcu.status=t::g1,t::g2")
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1
    assert "rpc_method:mcu.status" in sum(output["missing"].values(), [])


def test_incomplete_run_cannot_satisfy(tmp_path):
    for index, override in enumerate(({"exit_code": 2}, {"finished": False}, {"collected": ["t::h", "t::h"]})):
        run = tmp_path / f"run9{index}"
        _results(run, "rust-linux", "linux", [_row("t::h")], **override)
        _results(run, "python-linux", "linux", [_row("t::h")])
        result = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY", "--map", "error_code:SESSION_BUSY=t::h")
        output = json.loads((tmp_path / "o.json").read_text())
        assert result.returncode == 1 and "rust-linux" in output["incomplete_runs"]
        assert "error_code:SESSION_BUSY" not in output["covered"]


def test_duplicate_result_rows_not_best_of(tmp_path):
    run = tmp_path / "run10"
    _results(run, "rust-linux", "linux", [_row("t::k", "failed"), _row("t::k")])
    _results(run, "python-linux", "linux", [_row("t::k")])
    _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY", "--map", "error_code:SESSION_BUSY=t::k")
    output = json.loads((tmp_path / "o.json").read_text())
    assert "rust-linux" in output["incomplete_runs"]


def test_mapped_nodeid_not_collected_is_missing(tmp_path):
    run = tmp_path / "run11"
    _results(run, "rust-linux", "linux", [_row("t::m")])
    _results(run, "python-linux", "linux", [_row("t::m")])
    _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY", "--map", "error_code:SESSION_BUSY=t::m,t::renamed")
    output = json.loads((tmp_path / "o.json").read_text())
    assert "error_code:SESSION_BUSY" in sum(output["missing"].values(), [])


def test_cargo_results_import_roundtrip(tmp_path):
    importer = ROOT / "scripts" / "rust" / "import_cargo_results.py"
    result = subprocess.run(
        [sys.executable, str(importer), "--fixture", str(ROOT / "tests/fixtures/cargo_results_sample.json"), "--label", "rust-linux", "--out-dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads((tmp_path / "results-rust-linux.json").read_text())
    assert all(row["nodeid"].startswith("cargo:") for row in document["results"])
    assert document["finished"] is True and isinstance(document["exit_code"], int)
    assert set(document["collected"]) >= {row["nodeid"] for row in document["results"]}


def test_t9_and_known_low_are_present_in_gate_report():
    cc = _module()
    report = cc.render_gate_report({"case_verdicts": {}}, [], {})
    assert "endpoint_reopen_after_flasher_set_excl_recorded" in report
    assert "P8X-9e51abd-1" in report

