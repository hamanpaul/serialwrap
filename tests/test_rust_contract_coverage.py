from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

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
        "native_windows": label == "rust-windows",
        "native_build_link": label == "rust-windows",
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
    if label.startswith("rust-") and platform == "windows":
        # Windows 原生 runner 匯入時會把 SHA256SUMS 一併放到 run directory（P8X-24c6b39-5）。
        manifest = run / "artifacts" / "windows" / "SHA256SUMS"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        listed = manifest.read_text(encoding="utf-8") if manifest.exists() else ""
        for key in ("cli_sha256", "daemon_sha256"):
            digest = doc.get(key)
            if isinstance(digest, str) and digest not in listed:
                listed += f"{digest}  {key.removesuffix('_sha256')}-{label}.exe\n"
        manifest.write_text(listed, encoding="utf-8")
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
    cases = cc._read_yaml(ROOT / "tests/acceptance/cases.yaml")["cases"]
    gates_case = next(case for case in cases if case["case_id"] == "s24-xfer-session-gates")
    assert "SC-03" in gates_case["behavior_changes"] and "SC-33" not in gates_case["behavior_changes"]
    assert "case:s24-flash-sync-replay" in universe
    assert universe["error_code:ECHO_ACK_UNSUPPORTED"]["l1_only"] is True
    extracted = {
        contract_id
        for source_name, extractor in cc.EXTRACTORS.items()
        for contract_id in extractor(
            {
                "error_codes.yaml": ROOT / "contracts/error_codes.yaml",
                "session_last_errors.yaml": ROOT / "contracts/session_last_errors.yaml",
                "rpc_schema.json": ROOT / "contracts/rpc_schema.json",
                "cli_schema.json": ROOT / "contracts/cli_schema.json",
                "behavior_changes.yaml": ROOT / "contracts/behavior_changes.yaml",
                "wal_record.schema.json": ROOT / "contracts/wal_record.schema.json",
                "state_file.schema.json": ROOT / "contracts/state_file.schema.json",
                "cases.lock.json": ROOT / "tests/acceptance/cases.lock.json",
                "coverage_map.yaml": ROOT / "contracts/coverage_map.yaml",
            }[source_name]
        )
    }
    assert extracted == set(universe)


def test_empty_cross_phase_map_overrides_derived_case_evidence(tmp_path):
    import yaml

    cc = _module()
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "error_codes.yaml").write_text(
        yaml.safe_dump({"codes": [{"code": "TEST", "reachable": True, "behavior_change": ["SC-03"], "scope": {}}]}),
        encoding="utf-8",
    )
    (contracts / "session_last_errors.yaml").write_text(yaml.safe_dump({"values": []}), encoding="utf-8")
    (contracts / "behavior_changes.yaml").write_text(
        yaml.safe_dump({"changes": [{"id": "SC-03", "phases": ["P5"], "case_ids": ["source-case"]}]}),
        encoding="utf-8",
    )
    (contracts / "rpc_schema.json").write_text(json.dumps({"x-methods": {}}), encoding="utf-8")
    (contracts / "cli_schema.json").write_text(json.dumps({"x-groups": []}), encoding="utf-8")
    (contracts / "wal_record.schema.json").write_text("{}", encoding="utf-8")
    (contracts / "state_file.schema.json").write_text("{}", encoding="utf-8")
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        yaml.safe_dump({"cases": [{"case_id": "source-case", "behavior_changes": ["SC-03"], "evidence": ["test_source.py::test_old_phase"]}]}),
        encoding="utf-8",
    )
    lock = tmp_path / "cases.lock.json"
    lock.write_text(json.dumps({"sets": {"slice_2.4": []}}), encoding="utf-8")
    coverage_map = contracts / "coverage_map.yaml"
    coverage_map.write_text(
        yaml.safe_dump({"entries": {"error_code:TEST": {"nodeids": [], "owner_phase": "P5"}}}),
        encoding="utf-8",
    )
    universe = cc.universe(contracts, cases, lock, coverage_map)
    assert universe["error_code:TEST"]["owner_phase"] == "P5"
    assert universe["error_code:TEST"]["nodeids"] == []


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


def test_windows_cross_build_is_not_native_evidence(tmp_path):
    run = tmp_path / "run7-cross"
    _results(run, "rust-windows", "windows", [_row("t::w")], native_windows=False, native_build_link=False)
    _results(run, "python-windows", "windows", [_row("t::w")])
    result = _run(
        "--run-dir", str(run), "--json", str(tmp_path / "o.json"),
        "--only", "error_code:X_WIN_ONLY",
        "--map", "error_code:X_WIN_ONLY=t::w",
        "--platforms", "error_code:X_WIN_ONLY=windows",
    )
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 0
    assert "error_code:X_WIN_ONLY" in output["blocked_owner"]
    assert "error_code:X_WIN_ONLY" not in output["covered"]
    assert output["windows_verdicts"]["error_code:X_WIN_ONLY"] == "BLOCKED-OWNER"


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


def test_cargo_capability_tests_require_explicit_opt_in(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/rust"))
    import import_cargo_results

    assert import_cargo_results._capability("cap_physical_uart__opens_device") == "physical_uart"
    assert import_cargo_results._should_execute("regular_unit_test", set())
    assert not import_cargo_results._should_execute("cap_physical_uart__opens_device", set())
    assert import_cargo_results._should_execute("cap_posix_pty__creates_pty", {"posix_pty"})


def test_failed_case_is_reported_as_fail_in_incomplete_run(tmp_path):
    run = tmp_path / "failed-run"
    _results(run, "rust-linux", "linux", [_row("t::failed", "failed")], exit_code=1, finished=False)
    _results(run, "python-linux", "linux", [_row("t::failed")])
    result = _run(
        "--run-dir", str(run), "--json", str(tmp_path / "o.json"),
        "--only", "case:failed-case", "--map", "case:failed-case=t::failed",
    )
    output = json.loads((tmp_path / "o.json").read_text())
    assert result.returncode == 1
    assert output["case_verdicts"]["failed-case"] == "FAIL"
    assert "case:failed-case" in output["failed"]


def test_t9_and_known_low_are_present_in_gate_report():
    cc = _module()
    report = cc.render_gate_report(
        {
            "case_verdicts": {},
            "windows_verdicts": {"rpc_method:service.status": "BLOCKED-OWNER"},
            "blocked_owner": {"case:f7-binary-roundtrip-md5": "需要擁有者 TestPilot 實機證據"},
        },
        [],
        {},
    )
    assert "endpoint_reopen_after_flasher_set_excl_recorded" in report
    assert "EVIDENCE_MISSING" in report
    assert "P8X-9e51abd-1" in report
    assert "凍結分母外的 BLOCKED-OWNER 案例" in report
    assert "f7-binary-roundtrip-md5" in report
    assert "Windows 全部契約 ID" in report
    assert "rpc_method:service.status" in report and "BLOCKED-OWNER" in report


def test_t9_recorded_outcome_is_written_to_pending_column():
    cc = _module()
    report = cc.render_gate_report(
        {"case_verdicts": {}},
        [],
        {
            "results": [{
                "label": "rust-linux",
                "results": [{
                    "nodeid": "cargo:sw-core::flash_coordinator::cap_posix_pty__endpoint_reopen_after_flasher_set_excl_recorded",
                    "phase": "call",
                    "outcome": "passed",
                }],
            }],
        },
    )
    assert "待裁決" in report
    assert "rust-linux" in report and "call=passed" in report



# ---------------------------------------------------------------------------
# T15 第 2 輪審查（P8X-24c6b39-1～7）回歸
# ---------------------------------------------------------------------------
EXIT_SCRIPT = ROOT / "scripts" / "rust" / "slice24_exit.sh"
LONGRUN_S = 172800
_LONGRUN_REQUIRED_CHECKS = (
    "pinned_cli", "engine_verdict", "fault_retention_attested", "baseline_compatible",
    "continuous_duration", "safety_double_holder",
)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contracts_tree() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD:contracts"], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _longrun_source(base: Path, *, platforms=("linux", "windows"), results=None) -> Path:
    """合成一份格式完整的長跑證據目錄（結構即 slice24_exit.sh --longrun 的輸入契約）。"""
    results = results or {}
    src = base / "longrun-src"
    acceptance_sha = _sha_bytes((ROOT / "realhw/acceptance/longrun_acceptance.yaml").read_bytes())
    cases_sha = _sha_bytes((ROOT / "tests/acceptance/cases.yaml").read_bytes())
    tree = _contracts_tree()
    workload = _sha_bytes(b"workload-contract")
    engine_files = {"realhw/cases/longrun.py": _sha_bytes(b"engine")}
    manifest = {"schema_version": "slice24-longrun.v1", "case_id": "lr-mixed", "platforms": {}}
    for platform in platforms:
        root = src / platform
        (root / "wal").mkdir(parents=True)
        attest = {"mode": "systemd-user" if platform == "linux" else "windows-task",
                  "auto_restart_disabled": True, "evidence": "Restart=no"}
        files = {
            "report.json": json.dumps({"meta": {"platform": platform}}) + "\n",
            "events.ndjson": '{"kind":"start","t":0}\n{"kind":"stop","t":172810}\n',
            "snapshots.ndjson": '{"t":0,"rss_kb":1}\n',
            "observer.ndjson": json.dumps({"kind": "header", "supervision": attest}) + "\n",
        }
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        (root / "wal" / "raw.wal.ndjson").write_text('{"seq":1}\n', encoding="utf-8")
        excl_hashes = {}
        if platform == "windows":
            (root / "excl").mkdir()
            for name in ("probes.ndjson", "handoff.ndjson"):
                (root / "excl" / name).write_text('{"verdict":"exclusive"}\n', encoding="utf-8")
                excl_hashes[name] = _sha_bytes((root / "excl" / name).read_bytes())
        (root / "supervision-attest.json").write_text(json.dumps(attest), encoding="utf-8")
        (root / "bench-lock.json").write_text(json.dumps({
            "path": "/home/owner/.local/state/serialwrap/bench.lock",
            "acquired_at_utc": "2026-10-31T23:59:00+00:00",
            "released_at_utc": "2026-11-03T00:01:00+00:00",
        }), encoding="utf-8")
        bundle = _sha_bytes(f"bundle-{platform}".encode())
        cli_sha = _sha_bytes(f"cli-{platform}".encode())
        verdict = results.get(platform, "PASS")
        checks = [
            {"id": check_id, "group": "evidence", "status": "evaluated", "result": "PASS", "detail": {}}
            for check_id in _LONGRUN_REQUIRED_CHECKS
        ]
        if verdict != "PASS":
            checks[-1]["result"] = verdict
        identity = {"error": None, "bundle_tree_sha256": bundle, "cli_sha256": cli_sha}
        candidate = {
            "schema_version": "release-gate.v1", "platform": platform, "mode": "candidate",
            "engine_result": {"verdict": "PASS"}, "release_gate_result": verdict, "baseline_result": None,
            "acceptance": {"sha256": acceptance_sha, "workload_contract_sha256": workload},
            "run": {
                "started_at_utc": "2026-11-01T00:00:00+00:00", "ended_at_utc": "2026-11-03T00:00:10+00:00",
                "duration_s": 172810, "cli_identity": {"pinned": True, "sha256": cli_sha},
                "deployed_content": {
                    "start": dict(identity), "end": dict(identity),
                    "daemon_live": [{"in_install": True, "image_sha256": _sha_bytes(f"d-{platform}".encode()), "error": None}],
                },
                "engine_files_sha256": engine_files, "contract_revision": tree, "cases_yaml_sha256": cases_sha,
            },
            "checks": checks,
            "evidence_hashes": {
                **{name: _sha_bytes((root / name).read_bytes()) for name in files},
                "wal": {"raw.wal.ndjson": _sha_bytes((root / "wal" / "raw.wal.ndjson").read_bytes())},
            },
            "excl_evidence_hashes": excl_hashes,
        }
        baseline = {
            "schema_version": "release-gate.v1", "platform": platform, "mode": "baseline",
            "release_gate_result": "NOT_APPLICABLE_BASELINE", "baseline_result": "PASS",
            "acceptance": {"sha256": acceptance_sha, "workload_contract_sha256": workload},
            "run": {"duration_s": 172900, "engine_files_sha256": engine_files,
                    "started_at_utc": "2026-10-20T00:00:00+00:00", "ended_at_utc": "2026-10-22T00:01:40+00:00"},
            "checks": [{"id": "engine_verdict", "group": "engine", "status": "evaluated", "result": "PASS", "detail": {}}],
        }
        (root / "release-gate.json").write_text(json.dumps(candidate), encoding="utf-8")
        (root / "baseline-release-gate.json").write_text(json.dumps(baseline), encoding="utf-8")
        manifest["platforms"][platform] = {"bundle_tree_sha256": bundle}
    (src / "longrun-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _import_longrun(run: Path, src: Path):
    return _run("--run-dir", str(run), "--import-longrun", str(src))


def _judge_lr_mixed(run: Path, out: Path):
    result = _run("--run-dir", str(run), "--json", str(out), "--only", "case:lr-mixed")
    assert out.is_file(), result.stderr
    return result, json.loads(out.read_text(encoding="utf-8"))


def test_longrun_valid_evidence_unblocks_lr_mixed(tmp_path):
    """P8X-24c6b39-1：有效長跑證據經匯入與重新驗證後才解除 lr-mixed 的擁有者阻擋。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["accepted"] is True
    assert {p: v["release_gate_result"] for p, v in record["platforms"].items()} == {"linux": "PASS", "windows": "PASS"}
    result, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "PASS"
    assert "case:lr-mixed" in output["covered"] and "case:lr-mixed" not in output["blocked_owner"]
    assert result.returncode == 0, result.stderr


def _drop_baseline(src: Path) -> None:
    (src / "linux" / "baseline-release-gate.json").unlink()


def _tamper_events(src: Path) -> None:
    with (src / "linux" / "events.ndjson").open("a", encoding="utf-8") as stream:
        stream.write('{"kind":"error"}\n')


def _restart_enabled(src: Path) -> None:
    path = src / "linux" / "supervision-attest.json"
    attest = json.loads(path.read_text(encoding="utf-8"))
    attest["auto_restart_disabled"] = False
    path.write_text(json.dumps(attest), encoding="utf-8")


def _drop_bench_lock(src: Path) -> None:
    (src / "linux" / "bench-lock.json").unlink()


def _other_bundle(src: Path) -> None:
    path = src / "longrun-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["platforms"]["linux"]["bundle_tree_sha256"] = "f" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _drop_windows_excl(src: Path) -> None:
    (src / "windows" / "excl" / "handoff.ndjson").unlink()


def _short_baseline(src: Path) -> None:
    path = src / "linux" / "baseline-release-gate.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    baseline["run"]["duration_s"] = 3600
    path.write_text(json.dumps(baseline), encoding="utf-8")


def _claims_pass_with_failed_check(src: Path) -> None:
    path = src / "linux" / "release-gate.json"
    gate = json.loads(path.read_text(encoding="utf-8"))
    gate["checks"][0]["result"] = "FAIL"
    path.write_text(json.dumps(gate), encoding="utf-8")


@pytest.mark.parametrize(
    "mutate,platform,needle",
    [
        (_drop_baseline, "linux", "baseline-release-gate.json"),
        (_tamper_events, "linux", "events.ndjson"),
        (_restart_enabled, "linux", "auto_restart_disabled"),
        (_drop_bench_lock, "linux", "bench-lock.json"),
        (_other_bundle, "linux", "bundle_tree_sha256"),
        (_drop_windows_excl, "windows", "handoff.ndjson"),
        (_short_baseline, "linux", "duration_s"),
        (_claims_pass_with_failed_check, "linux", "release_gate_result"),
    ],
)
def test_longrun_incomplete_or_tampered_evidence_is_rejected(tmp_path, mutate, platform, needle):
    """P8X-24c6b39-1：缺漏或竄改的長跑證據必須拒收，lr-mixed 維持 BLOCKED-OWNER。"""
    run = tmp_path / "run"
    src = _longrun_source(tmp_path)
    mutate(src)
    imported = _import_longrun(run, src)
    assert imported.returncode == 1, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["accepted"] is False
    assert record["platforms"][platform]["accepted"] is False
    assert any(needle in error for error in record["platforms"][platform]["errors"]), record
    _, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"
    assert "case:lr-mixed" not in output["covered"]


def test_longrun_valid_fail_verdict_is_reported_as_fail(tmp_path):
    """P8X-24c6b39-1：證據有效但 release gate 判 FAIL 時，案例為 FAIL，不得藏成 BLOCKED-OWNER 或 PASS。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path, results={"linux": "FAIL"}))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    result, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "FAIL"
    assert "case:lr-mixed" in output["failed"] and result.returncode == 1


def test_longrun_partial_platforms_stay_blocked(tmp_path):
    """P8X-24c6b39-1：只交 Linux 長跑時，Windows 缺件仍阻擋 lr-mixed。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path, platforms=("linux",)))
    assert imported.returncode == 1, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["platforms"]["linux"]["accepted"] is True
    assert record["platforms"]["windows"]["accepted"] is False
    _, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"


def test_longrun_copy_is_revalidated_at_judgement(tmp_path):
    """P8X-24c6b39-1：判定時重新驗證 run directory 內的副本，匯入後被改動即不得 PASS。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    with (run / "longrun" / "linux" / "observer.ndjson").open("a", encoding="utf-8") as stream:
        stream.write('{"kind":"sample"}\n')
    _, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"
    assert "observer.ndjson" in output["blocked_owner"]["case:lr-mixed"]


def test_slice24_exit_routes_longrun_through_importer():
    """P8X-24c6b39-1：出口腳本的 --longrun 必須交給匯入驗證，不得只檢查目錄存在。"""
    script = EXIT_SCRIPT.read_text(encoding="utf-8")
    assert '--import-longrun "$LONGRUN_DIR"' in script


class _FakeItem:
    def __init__(self, nodeid):
        self.nodeid = nodeid


class _FakeSession:
    def __init__(self, nodeids):
        self.items = [_FakeItem(nodeid) for nodeid in nodeids]
        self.shouldstop = False
        self.shouldfail = False
        self.exitstatus = 0


class _FakeReport:
    def __init__(self, nodeid, when):
        self.nodeid = nodeid
        self.when = when
        self.skipped = False
        self.failed = False
        self.passed = True
        self.longreprtext = ""


def _plugin_module():
    path = ROOT / "tests" / "contract_blackbox" / "coverage_plugin.py"
    spec = importlib.util.spec_from_file_location("coverage_plugin_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_run(plugin, session) -> None:
    plugin.pytest_collection_finish(session)
    for item in session.items:
        for when in ("setup", "call", "teardown"):
            plugin.pytest_runtest_logreport(_FakeReport(item.nodeid, when))
    plugin.pytest_sessionfinish(session, 0)


def test_coverage_plugin_reads_artifacts_from_ab_env_snapshot(tmp_path, monkeypatch):
    """P8X-24c6b39-2：根 conftest 清掉 SERIALWRAP_CLI_EXE 後，plugin 仍以清除前快照記錄受測 artifact。"""
    import ab_env

    cli = tmp_path / "serialwrap"
    daemon = tmp_path / "serialwrapd"
    cli.write_bytes(b"\x7fELF cli")
    daemon.write_bytes(b"\x7fELF daemon")
    monkeypatch.setattr(ab_env, "AB_ENV", {
        "SERIALWRAP_CLI_EXE": str(cli), "SERIALWRAP_DAEMON_EXE": str(daemon), "SERIALWRAP_AB_LABEL": "rust-linux",
    })
    monkeypatch.delenv("SERIALWRAP_CLI_EXE", raising=False)
    monkeypatch.delenv("SERIALWRAP_DAEMON_EXE", raising=False)
    run = tmp_path / "run"
    monkeypatch.setenv("SERIALWRAP_COVERAGE_RUN_DIR", str(run))
    monkeypatch.setenv("SERIALWRAP_COVERAGE_LABEL", "rust-linux")
    _plugin_run(_plugin_module(), _FakeSession(["tests/contract_blackbox/test_x.py::test_y"]))
    document = json.loads((run / "results-rust-linux.json").read_text(encoding="utf-8"))
    assert document["cli_sha256"] == _sha_bytes(cli.read_bytes())
    assert document["daemon_sha256"] == _sha_bytes(daemon.read_bytes())
    assert not document.get("identity_errors")
    errors = _module()._identity_checks([document], run, None, None)
    assert not [error for error in errors if "artifact" in error], errors


def test_coverage_plugin_flags_rust_label_without_artifact(tmp_path, monkeypatch):
    """P8X-24c6b39-2：Rust label 沒有可交叉核對的受測入口時，結果檔須自帶身分錯誤。"""
    import ab_env

    monkeypatch.setattr(ab_env, "AB_ENV", {})
    run = tmp_path / "run"
    monkeypatch.setenv("SERIALWRAP_COVERAGE_RUN_DIR", str(run))
    monkeypatch.setenv("SERIALWRAP_COVERAGE_LABEL", "rust-linux")
    _plugin_run(_plugin_module(), _FakeSession(["tests/contract_blackbox/test_x.py::test_y"]))
    document = json.loads((run / "results-rust-linux.json").read_text(encoding="utf-8"))
    assert document.get("identity_errors"), document


def test_coverage_plugin_records_items_outside_contract_blackbox(tmp_path):
    """P8X-24c6b39-6：結果檔須含 contract_blackbox 以外測項的各 phase 結果，而不只出現在 collected。"""
    run = tmp_path / "run"
    nodeid = "tests/test_rust_contract_coverage.py::test_unmapped_contract_id_is_reported"
    env = {**os.environ, "SERIALWRAP_COVERAGE_RUN_DIR": str(run), "SERIALWRAP_COVERAGE_LABEL": "python-linux"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", nodeid],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path = run / "results-python-linux.json"
    assert path.is_file(), proc.stdout
    document = json.loads(path.read_text(encoding="utf-8"))
    phases = {(row["nodeid"], row["phase"], row["outcome"]) for row in document["results"]}
    assert (nodeid, "call", "passed") in phases
    assert document["collected"] == [nodeid] and document["finished"] is True


def _rust_python_docs(tmp_path, rust_rows, python_rows, *, rust_collected=None, python_collected=None):
    run = tmp_path / "run-ab"
    rust_over = {"collected": rust_collected} if rust_collected is not None else {}
    python_over = {"collected": python_collected} if python_collected is not None else {}
    paths = [
        _results(run, "rust-linux", "linux", rust_rows, **rust_over),
        _results(run, "python-linux", "linux", python_rows, **python_over),
    ]
    return run, [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def test_parametrized_base_requires_every_declared_parameter(tmp_path):
    """P8X-24c6b39-3：參數化基底宣告展開為完整必要參數節點集合，逐項檢查收集與通過。"""
    cc = _module()
    base = "tests/t.py::test_gate"
    params = ["quiet", "rpc_args", "busy"]
    rows = [_row(f"{base}[{param}]") for param in params]
    item = _item(nodeids=[base], parametrized={base: params})
    run, docs = _rust_python_docs(tmp_path, rows, rows)
    report = cc.evaluate({"rpc_method:gate": item}, docs, run_dir=run)
    assert report["covered"] == ["rpc_method:gate"], report["missing_reasons"]

    run2, docs2 = _rust_python_docs(tmp_path / "drop", rows[:2], rows)
    report2 = cc.evaluate({"rpc_method:gate": item}, docs2, run_dir=run2)
    assert "rpc_method:gate" not in report2["covered"]
    assert f"{base}[busy]" in report2["missing_reasons"]["rpc_method:gate"]

    undeclared = _item(nodeids=[base])
    report3 = cc.evaluate({"rpc_method:gate": undeclared}, docs, run_dir=run)
    assert "rpc_method:gate" not in report3["covered"]
    assert "parametrized" in report3["missing_reasons"]["rpc_method:gate"]


def test_parametrized_declarations_match_real_collection(tmp_path):
    """P8X-24c6b39-3：宣告的必要參數集合須與 pytest 實際收集完全一致，不得縮小分母。"""
    cc = _module()
    declared = cc._read_yaml(ROOT / "contracts/coverage_map.yaml").get("parametrized") or {}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", "tests/"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    collected = {line.strip() for line in proc.stdout.splitlines() if "::" in line}
    variants: dict[str, set[str]] = {}
    for nodeid in collected:
        if "[" in nodeid:
            base, _, rest = nodeid.partition("[")
            variants.setdefault(base, set()).add(rest[:-1])
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    referenced = {
        nodeid
        for item in universe.values()
        for nodeid in [*item.get("nodeids", []), *item.get("python_nodeids", [])]
        if not nodeid.startswith("cargo:") and nodeid not in collected and nodeid in variants
    }
    assert referenced, "應至少有 s24-xfer-session-gates 的參數化基底"
    assert "tests/contract_blackbox/test_s24_file_transfer.py::test_gate_resolve_quiet" in referenced
    for base in sorted(referenced):
        assert sorted(declared.get(base) or []) == sorted(variants[base]), base
    assert set(declared) <= set(variants), "宣告了不存在的參數化基底"


def test_rust_l1_nodeids_are_not_required_in_python_results(tmp_path):
    """P8X-24c6b39-4：Cargo 節點只對 Rust 驗證；雙邊黑箱節點與 Python contract_tests 照常驗證。"""
    cc = _module()
    cargo = "cargo:sw-core::session_core::attached_passthrough_submit_not_ready_with_ready_in"
    item = _item(kind="error_code", nodeids=[cargo, "t::bb"], python_nodeids=["t::py"])
    run, docs = _rust_python_docs(tmp_path, [_row(cargo), _row("t::bb")], [_row("t::bb"), _row("t::py")])
    report = cc.evaluate({"error_code:SESSION_NOT_READY": item}, docs, run_dir=run)
    assert report["covered"] == ["error_code:SESSION_NOT_READY"], report["missing_reasons"]

    run2, docs2 = _rust_python_docs(tmp_path / "ab", [_row(cargo), _row("t::bb")], [_row("t::bb", "failed"), _row("t::py")])
    report2 = cc.evaluate({"error_code:SESSION_NOT_READY": item}, docs2, run_dir=run2)
    assert "error_code:SESSION_NOT_READY" in report2["ab_mismatch"]


def test_rust_l1_only_mapping_still_requires_python_side(tmp_path):
    """P8X-24c6b39-4：外部契約只有 Cargo 節點、又沒有 Python 證據時不得成立（不可整項改 rust_only 迴避）。"""
    cc = _module()
    cargo = "cargo:sw-core::x::only_rust"
    run, docs = _rust_python_docs(tmp_path, [_row(cargo)], [_row("t::unrelated")])
    report = cc.evaluate({"error_code:X": _item(kind="error_code", nodeids=[cargo])}, docs, run_dir=run)
    assert "error_code:X" not in report["covered"]
    assert "Python" in report["missing_reasons"]["error_code:X"]


def test_platform_artifacts_are_checked_per_platform(tmp_path):
    """P8X-24c6b39-5：Linux ELF 與 Windows PE 各自雜湊正確時不得因跨平台不同而拒收。"""
    run = tmp_path / "run-xplat"
    linux_dir = run / "linux-art"
    linux_dir.mkdir(parents=True)
    _results(run, "rust-linux", "linux", [_row("t::x")])
    linux_doc = json.loads((run / "results-rust-linux.json").read_text(encoding="utf-8"))
    windows_cli = run / "win-cli.exe"
    windows_daemon = run / "win-daemon.exe"
    windows_cli.write_bytes(b"MZ windows cli")
    windows_daemon.write_bytes(b"MZ windows daemon")
    _results(
        run, "rust-windows", "windows", [_row("t::x")],
        cli_exe=str(windows_cli), cli_sha256=_sha_bytes(windows_cli.read_bytes()),
        daemon_exe=str(windows_daemon), daemon_sha256=_sha_bytes(windows_daemon.read_bytes()),
    )
    _results(run, "python-linux", "linux", [_row("t::x")])
    _results(run, "python-windows", "windows", [_row("t::x")])
    result = _run(
        "--run-dir", str(run), "--json", str(tmp_path / "o.json"),
        "--only", "error_code:SESSION_BUSY", "--map", "error_code:SESSION_BUSY=t::x",
        "--expect-platform", "linux",
        "--expect-cli-sha256", linux_doc["cli_sha256"], "--expect-daemon-sha256", linux_doc["daemon_sha256"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert output["identity_errors"] == []
    assert "error_code:SESSION_BUSY" in output["covered"]


def test_windows_artifact_must_be_listed_in_runner_manifest(tmp_path):
    """P8X-24c6b39-5：Windows 結果的 artifact 須對照其 runner SHA256SUMS。"""
    run = tmp_path / "run-manifest"
    _results(run, "rust-windows", "windows", [_row("t::w")])
    _results(run, "python-windows", "windows", [_row("t::w")])
    (run / "artifacts" / "windows" / "SHA256SUMS").write_text(f"{'a' * 64}  other.exe\n", encoding="utf-8")
    result = _run(
        "--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:X_WIN_ONLY",
        "--map", "error_code:X_WIN_ONLY=t::w", "--platforms", "error_code:X_WIN_ONLY=windows",
    )
    output = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert any("SHA256SUMS" in error for error in output["identity_errors"]), output["identity_errors"]
    assert "error_code:X_WIN_ONLY" not in output["covered"]


def test_slice24_exit_scopes_artifact_checks_per_platform():
    """P8X-24c6b39-5：出口腳本只把 Linux 預期雜湊套用到 Linux，Windows 以 runner manifest 匯入。"""
    script = EXIT_SCRIPT.read_text(encoding="utf-8")
    assert "--expect-platform linux" in script
    assert 'artifacts_dir / "SHA256SUMS"' in script


def _rust_pytest_targets() -> list[str]:
    import re

    script = EXIT_SCRIPT.read_text(encoding="utf-8")
    matched = re.search(r"^RUST_PYTEST_TARGETS=\((.*?)\)", script, flags=re.M | re.S)
    assert matched, "slice24_exit.sh 須以 RUST_PYTEST_TARGETS 陣列宣告 Rust label 的 pytest 範圍"
    assert '"${RUST_PYTEST_TARGETS[@]}"' in script
    return matched.group(1).split()


def test_rust_label_runs_every_mapped_pytest_file():
    """P8X-24c6b39-6：凡需 rust-linux 證據的 pytest nodeid，其檔案都要在 Rust label 的實際收集範圍內。"""
    cc = _module()
    targets = _rust_pytest_targets()
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    uncovered = set()
    for contract_id, item in universe.items():
        if cc._is_owner_blocked(item) or "linux" not in item.get("platforms", []):
            continue
        for nodeid in item.get("nodeids", []):
            if nodeid.startswith("cargo:"):
                continue
            path = nodeid.split("::", 1)[0]
            if not any(path == target or path.startswith(target.rstrip("/") + "/") for target in targets):
                uncovered.add((path, contract_id))
    assert not uncovered, sorted(uncovered)[:20]
    assert "tests/test_rust_contract_coverage.py" in targets


def test_p8_mcu_contracts_are_mapped_to_formal_call_paths():
    """P8X-24c6b39-7：mcu.patterns／mcu.status 的 RPC 與 CLI 契約由 P8 自身測試映射，且測項真的走該呼叫路徑。"""
    import ast

    cc = _module()
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    source_path = ROOT / "tests/contract_blackbox/test_s24_mcu_rpc.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    functions = {node.name: ast.get_source_segment(source_path.read_text(encoding="utf-8"), node)
                 for node in tree.body if isinstance(node, ast.FunctionDef)}
    expected_calls = {
        "rpc_method:mcu.patterns": 'rpc("mcu.patterns"',
        "rpc_method:mcu.status": 'rpc("mcu.status"',
        "cli_cmd:mcu patterns": 'cli("mcu", "patterns")',
        "cli_cmd:mcu status": 'cli("mcu", "status")',
    }
    case_nodes = set(universe["case:s24-mcu-rpc-shapes"]["nodeids"])
    mapped = set()
    for contract_id, call in expected_calls.items():
        item = universe[contract_id]
        assert item["owner_phase"] == "P8", contract_id
        assert item["nodeids"], contract_id
        for nodeid in item["nodeids"]:
            path, _, name = nodeid.partition("::")
            assert path == "tests/contract_blackbox/test_s24_mcu_rpc.py", nodeid
            assert call in functions[name], (contract_id, nodeid)
            mapped.add(nodeid)
    assert case_nodes <= mapped


def test_nested_pytest_does_not_write_into_outer_coverage_run(tmp_path):
    """P8X-24c6b39-6 附帶：受測項內的巢狀 pytest（會收集並執行子測項）不得把自身結果併入外層 run。"""
    run = tmp_path / "run"
    nodeid = "tests/test_rust_p4_conftest.py::test_ab_label_survives_root_conftest"
    env = {**os.environ, "SERIALWRAP_COVERAGE_RUN_DIR": str(run), "SERIALWRAP_COVERAGE_LABEL": "python-linux"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", nodeid],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    document = json.loads((run / "results-python-linux.json").read_text(encoding="utf-8"))
    assert document["collected"] == [nodeid], document["collected"][:5]
    assert document["finished"] is True and not document.get("identity_errors")
    assert [row["phase"] for row in document["results"]] == ["setup", "call", "teardown"]
