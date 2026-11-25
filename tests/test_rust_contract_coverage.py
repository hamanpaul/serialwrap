from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
TOOL = ROOT / "scripts" / "rust" / "contract_coverage.py"
REV = {
    "contract_revision": "r",
    "cases_yaml_sha256": "c" * 64,
    "cases_lock_sha256": "d" * 64,
    "tool": {"name": "contract_coverage", "revision": "t"},
    "started_at": "x",
    "finished_at": "y",
}

# 0.5.1 依 OD-111 新增、owner_phase 在 P0～P7 的契約 ID（例如 bc:BC-06）；各工作包只加入自己的 ID，多包合併取聯集。
_V051_ADDED_CONTRACT_IDS: frozenset[str] = frozenset({"bc:BC-06", "bc:BC-07", "bc:BC-10", "bc:BC-13", "bc:BC-18"})


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
    """結果檔真正重複寫入仍依 R5-F3 判為不完整；只有 plugin 彙整 subTest 報告。"""
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


def test_t9_and_known_low_are_present_in_gate_report(tmp_path, monkeypatch):
    cc = _module()
    from types import SimpleNamespace

    sections = {
        "windows_l1:s21_core": {"status": "PASS", "exit_code": 0, "reasons": []},
        "windows_l1:s22_gate:win_test": {
            "status": "FAIL", "exit_code": 1, "reasons": ["Windows 原生列未通過"],
        },
    }
    calls = []

    def validate_section(shared_dir, *, gate, section, required_set, platform, current_head):
        calls.append((gate, section, required_set, platform, current_head))
        return sections[section]

    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    expected_sections = sorted(
        f"windows_l1:{set_id}" for set_id, platforms in lock["sets"].items() if "windows" in platforms
    )
    missing_windows = cc._shared_windows_l1_verdicts(None, expected_head="a" * 40)
    assert [row["section"] for row in missing_windows] == expected_sections
    assert all(row["status"] == "EVIDENCE_MISSING" for row in missing_windows)
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "manifest.json").write_text("[]", encoding="utf-8")
    malformed_windows = cc._shared_windows_l1_verdicts(malformed, expected_head="a" * 40)
    assert [row["section"] for row in malformed_windows] == expected_sections
    assert all(row["status"] == "EVIDENCE_MISSING" for row in malformed_windows)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "manifest.json").write_text(json.dumps({
        "sections": {name: {} for name in sections},
    }), encoding="utf-8")
    monkeypatch.setattr(
        cc, "_repo_module", lambda name: SimpleNamespace(validate_section=validate_section),
    )
    windows_l1 = cc._shared_windows_l1_verdicts(shared, expected_head="a" * 40)
    by_section = {row["section"]: row for row in windows_l1}
    assert sorted(by_section) == expected_sections
    for name in expected_sections:
        if name in sections:
            assert by_section[name] == {"section": name, **sections[name]}
        else:
            assert by_section[name]["status"] == "EVIDENCE_MISSING"
            assert by_section[name]["exit_code"] is None
    assert calls == [
        ("slice24", name, name.removeprefix("windows_l1:"), "windows", "a" * 40)
        for name in sorted(sections)
    ]
    mcu_case_ids = (
        "s24-flash-baud-mirror", "s24-flash-holder-gone", "s24-flash-silent-unbridged",
        "s24-flash-sync-replay", "s24-mcu-rpc-shapes",
    )
    cases = [
        {"case_id": "lr-mixed", "platforms": ["linux", "windows"]},
        *({"case_id": case_id, "platforms": ["linux"]} for case_id in mcu_case_ids),
    ]
    report = cc.render_gate_report(
        {
            "case_verdicts": {
                "lr-mixed": "PASS",  # 模擬錯誤輸入；報告 renderer 必須強制顯示 BLOCKED-OWNER。
                "f7-binary-roundtrip-md5": "BLOCKED-OWNER",
                "f7-larger-file-not-truncated": "BLOCKED-OWNER",
                "sc14-flash-probe-eligibility": "PASS",
                "sc35-passthrough-command-completion": "PASS",
                "sc35-passthrough-quiet-ready": "PASS",
                "sc36-win-installer-com0com-optional": "BLOCKED-OWNER",
                "s24-flash-baud-mirror": "PASS",
                "s24-flash-holder-gone": "PASS",
                "s24-flash-silent-unbridged": "PASS",
                "s24-flash-sync-replay": "PASS",
                "s24-mcu-rpc-shapes": "PASS",
            },
            "covered": [
                "case:sc14-flash-probe-eligibility",
                "case:sc35-passthrough-command-completion",
                "case:sc35-passthrough-quiet-ready",
                "case:s24-flash-baud-mirror",
                "case:s24-flash-holder-gone",
                "case:s24-flash-silent-unbridged",
                "case:s24-flash-sync-replay",
                "case:s24-mcu-rpc-shapes",
            ],
            "windows_verdicts": {"rpc_method:service.status": "BLOCKED-OWNER"},
            "blocked_owner": {
                "case:lr-mixed": "長跑證據另出報告；依 OD-94 維持 BLOCKED-OWNER",
                "case:f7-binary-roundtrip-md5": "需要擁有者 TestPilot 實機證據",
                "case:f7-larger-file-not-truncated": "需要擁有者裁決與實機證據",
                "case:sc36-win-installer-com0com-optional": "缺少 Windows 原生 L1 證據",
            },
        },
        cases,
        {"windows_l1": windows_l1},
    )
    assert "endpoint_reopen_after_flasher_set_excl_recorded" in report
    assert "EVIDENCE_MISSING" in report
    assert "P8X-9e51abd-1" in report
    assert "coverage_map 另加案例（凍結分母外，6 案）" in report
    for case_id, status in (
        ("f7-binary-roundtrip-md5", "BLOCKED-OWNER"),
        ("f7-larger-file-not-truncated", "BLOCKED-OWNER"),
        ("sc14-flash-probe-eligibility", "PASS"),
        ("sc35-passthrough-command-completion", "PASS"),
        ("sc35-passthrough-quiet-ready", "PASS"),
        ("sc36-win-installer-com0com-optional", "BLOCKED-OWNER"),
    ):
        assert f"`{case_id}`" in report and f"**{status}**" in report
    assert "Windows 全部契約 ID" in report
    assert "rpc_method:service.status" in report and "BLOCKED-OWNER" in report
    assert "windows_l1:s21_core" in report and "**PASS**" in report
    assert "windows_l1:s22_gate:win_test" in report and "**FAIL**" in report
    missing_name = next(name for name in expected_sections if name not in sections)
    assert f"`{missing_name}` | **EVIDENCE_MISSING**" in report
    assert "BO #5" in report and "正式流程不帶 `--windows-run`" in report
    assert "OD-94" in report and "lr-mixed" in report and "BLOCKED-OWNER" in report
    assert "| `lr-mixed` | linux, windows | **BLOCKED-OWNER** |" in report
    assert "OD-91" in report and "FakeMCU" in report and "v0.3.2" in report
    assert "| `s24-mcu-rpc-shapes` | **PASS** |" in report
    assert "不代表 MCU 真機燒錄" in report and "COM20:COM21" in report
    assert "OD-96" in report and "attached_real_path" in report and "code_fingerprint" in report
    assert "ba29434c" in report and "47097715" in report and "Python 3.12.3" in report
    assert "test_rollback_refuses_while_push[after_probe]" in report
    assert "test_rollback_force_refuses_while_push[rollback-after-probe]" in report
    assert "dev" in report and "候選 bundle" in report
    assert "P8X-9e51abd-1" in report
    assert "T6 SPEC-GAP 4" in report
    assert "cargo:sw-core::file_transfer_session::non_ready_states_rejected_with_session_zero_tx" in report
    assert "crates/sw-core/tests/file_transfer_session.rs:451-470" in report
    assert "crates/sw-core/src/session_core/file_xfer.rs:104-125" in report


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
LONGRUN_S = 86400
_NA = "NOT_APPLICABLE_BASELINE"
_BUNDLE_BINARIES = {"linux": ("serialwrap", "serialwrapd"), "windows": ("serialwrap.exe", "serialwrapd.exe")}


def _gate():
    from realhw.acceptance import gate

    return gate


def _formal_checks(platform: str, mode: str) -> list[dict]:
    """正式 P2 gate 在該平台／模式產出的完整檢查列（以空輸入跑正式判定器取得；ID 集合與輸入無關）。"""
    import tempfile

    gate = _gate()
    with tempfile.TemporaryDirectory() as tmp:
        result = gate.evaluate(gate.GateInputs(
            report_dir=Path(tmp), case_dir=Path(tmp),
            acceptance_path=ROOT / "realhw/acceptance/longrun_acceptance.yaml",
            platform=platform, mode=mode, observer_path=None, wal_dir=None, baseline_gate_path=None,
        ))
    return [{"id": check["id"], "group": check["group"], "result": check["result"]} for check in result["checks"]]


def _candidate_bundle(root: Path, platform: str, *, cli: bytes, daemon: bytes, extra: dict | None = None) -> Path:
    """合成一份候選 bundle 版本目錄（release-manifest.json＋bin/ 內 CLI／daemon）。"""
    cli_name, daemon_name = _BUNDLE_BINARIES[platform]
    (root / "bin").mkdir(parents=True)
    (root / "bin" / cli_name).write_bytes(cli)
    (root / "bin" / daemon_name).write_bytes(daemon)
    (root / "release-manifest.json").write_text('{"version":"0.0.0-test"}\n', encoding="utf-8")
    for rel, data in (extra or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def _bundle_tree(bundle: Path) -> str:
    from realhw import deployed_content

    return deployed_content.tree_digests(bundle)[0]


def _candidate_record(bundle: Path, platform: str) -> dict:
    files = sorted(
        (path.relative_to(bundle).as_posix(), _sha_bytes(path.read_bytes()))
        for path in bundle.rglob("*") if path.is_file()
    )
    files.sort(key=lambda item: item[0].encode("utf-8"))
    return {
        "schema_version": "slice24-candidate-bundle.v1",
        "platform": platform,
        "bundle_tree_sha256": _bundle_tree(bundle),
        "files": [list(item) for item in files],
    }


def _bind_bundle(run: Path, platform: str, bundle: Path, *, record: bool = True, results: bool = True) -> None:
    """把本輪 rust-<platform> 受測 artifact 指向該 bundle 的 CLI／daemon，並寫入候選 bundle 紀錄。"""
    cli_name, daemon_name = _BUNDLE_BINARIES[platform]
    if results:
        cli, daemon = bundle / "bin" / cli_name, bundle / "bin" / daemon_name
        _results(
            run, f"rust-{platform}", platform, [],
            cli_exe=str(cli), cli_sha256=_sha_bytes(cli.read_bytes()),
            daemon_exe=str(daemon), daemon_sha256=_sha_bytes(daemon.read_bytes()),
        )
    if record:
        path = run / "artifacts" / platform / "candidate-bundle.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_candidate_record(bundle, platform)), encoding="utf-8")


def _bind_candidate(run: Path, base: Path, platforms=("linux", "windows")) -> None:
    """OD-94 獨立報告：驗證長跑候選 bundle 與本輪 RC identity 相同。"""
    for platform in platforms:
        _bind_bundle(run, platform, base / "bundles" / platform)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contracts_tree() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD:contracts"], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _longrun_source(base: Path, *, platforms=("linux", "windows"), results=None) -> Path:
    """合成一份格式完整的 OD-94 長跑側檔證據目錄。"""
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
            "events.ndjson": f'{{"kind":"start","t":0}}\n{{"kind":"stop","t":{LONGRUN_S + 10}}}\n',
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
            "path": "~/.local/state/serialwrap/bench.lock",
            "acquired_at_utc": "2026-10-31T23:59:00+00:00",
            "released_at_utc": "2026-11-03T00:01:00+00:00",
        }), encoding="utf-8")
        candidate_bundle = base / "bundles" / platform
        if not candidate_bundle.exists():
            _candidate_bundle(
                candidate_bundle, platform, cli=f"cli-{platform}".encode(), daemon=f"d-{platform}".encode(),
            )
        bundle = _bundle_tree(candidate_bundle)
        cli_sha = _sha_bytes(f"cli-{platform}".encode())
        verdict = results.get(platform, "PASS")
        checks = [
            {"id": check["id"], "group": check["group"], "status": "evaluated", "result": "PASS", "detail": {}}
            for check in _formal_checks(platform, "candidate")
        ]
        if verdict != "PASS":
            checks[-1]["result"] = verdict
        identity = {"error": None, "bundle_tree_sha256": bundle, "cli_sha256": cli_sha}
        candidate = {
            "schema_version": "release-gate.v1", "platform": platform, "mode": "candidate",
            "engine_result": {"verdict": "PASS"}, "release_gate_result": verdict, "baseline_result": None,
            "acceptance": {"sha256": acceptance_sha, "workload_contract_sha256": workload},
            "run": {
                "started_at_utc": "2026-11-01T00:00:00+00:00", "ended_at_utc": "2026-11-02T00:00:10+00:00",
                "duration_s": LONGRUN_S + 10, "cli_identity": {"pinned": True, "sha256": cli_sha},
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
            "run": {"duration_s": LONGRUN_S + 100, "engine_files_sha256": engine_files,
                    "started_at_utc": "2026-10-20T00:00:00+00:00", "ended_at_utc": "2026-10-21T00:01:40+00:00"},
            "checks": [
                {"id": check["id"], "group": check["group"], "status": "evaluated",
                 "result": _NA if check["result"] == _NA else "PASS", "detail": {}}
                for check in _formal_checks(platform, "baseline")
            ],
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


def _independent_longrun_verdict(run: Path):
    cc = _module()
    universe = cc.universe(cc.ROOT / "contracts", cc.DEFAULT_CASES, cc.DEFAULT_CASES_LOCK, cc.DEFAULT_MAP)
    evidence = cc.load_owner_evidence(run, cc.load_results(run))[cc.LONGRUN_CASE]
    return cc._owner_evidence_verdict(universe[cc.LONGRUN_CASE], evidence)


def test_longrun_valid_evidence_unblocks_lr_mixed(tmp_path):
    """OD-94：24 小時門檻一致；有效長跑另出報告，不解除 Slice 2.4 的擁有者阻擋。"""
    cc = _module()
    import yaml

    acceptance = yaml.safe_load((ROOT / "realhw/acceptance/longrun_acceptance.yaml").read_text(encoding="utf-8"))
    assert acceptance["workload"]["min_continuous_duration_s"] == LONGRUN_S
    assert cc._LONGRUN_MIN_DURATION_S == LONGRUN_S
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["accepted"] is True
    assert {p: v["release_gate_result"] for p, v in record["platforms"].items()} == {"linux": "PASS", "windows": "PASS"}
    _bind_candidate(run, tmp_path)
    assert _independent_longrun_verdict(run)[0] == "PASS"
    result, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"
    assert "case:lr-mixed" not in output["covered"] and "case:lr-mixed" in output["blocked_owner"]
    assert "OD-94" in output["blocked_owner"]["case:lr-mixed"]
    assert output["owner_evidence"] == {}
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
    """OD-94：缺漏或竄改的長跑證據不得掩蓋拒收；lr-mixed 維持 BLOCKED-OWNER。"""
    run = tmp_path / "run"
    src = _longrun_source(tmp_path)
    mutate(src)
    imported = _import_longrun(run, src)
    assert imported.returncode == 1, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["accepted"] is False
    assert record["platforms"][platform]["accepted"] is False
    assert any(needle in error for error in record["platforms"][platform]["errors"]), record
    _bind_candidate(run, tmp_path)
    _, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"
    assert "case:lr-mixed" not in output["covered"]


def test_longrun_valid_fail_verdict_is_reported_as_fail(tmp_path):
    """OD-94：獨立長跑保留 FAIL；該證據不改寫 Slice 2.4 的 BLOCKED-OWNER。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path, results={"linux": "FAIL"}))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    _bind_candidate(run, tmp_path)
    independent_verdict, independent_reason = _independent_longrun_verdict(run)
    assert independent_verdict == "FAIL" and "release_gate_result=FAIL" in independent_reason
    result, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"
    assert "OD-94" in output["blocked_owner"]["case:lr-mixed"]
    assert output["owner_evidence"] == {}
    assert "case:lr-mixed" not in output["failed"] and result.returncode == 0


def test_longrun_partial_platforms_stay_blocked(tmp_path):
    """OD-94：獨立長跑報告要求兩平台；缺少 Windows 仍維持 BLOCKED-OWNER。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path, platforms=("linux",)))
    assert imported.returncode == 1, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["platforms"]["linux"]["accepted"] is True
    assert record["platforms"]["windows"]["accepted"] is False
    _, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"


def test_longrun_copy_is_revalidated_at_judgement(tmp_path):
    """OD-94：獨立長跑副本竄改仍被重新驗證，且 Slice 2.4 不消費該證據。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    with (run / "longrun" / "linux" / "observer.ndjson").open("a", encoding="utf-8") as stream:
        stream.write('{"kind":"sample"}\n')
    _bind_candidate(run, tmp_path)
    independent_verdict, independent_reason = _independent_longrun_verdict(run)
    assert independent_verdict == "BLOCKED-OWNER" and "observer.ndjson" in independent_reason
    _, output = _judge_lr_mixed(run, tmp_path / "o.json")
    assert output["case_verdicts"]["lr-mixed"] == "BLOCKED-OWNER"
    assert output["owner_evidence"] == {}
    assert "OD-94" in output["blocked_owner"]["case:lr-mixed"]


def test_slice24_exit_routes_longrun_through_importer():
    """OD-94：沿用凍結 nodeid；正式出口不將獨立長跑側檔匯入 Slice 2.4。"""
    script = EXIT_SCRIPT.read_text(encoding="utf-8")
    assert "--longrun" not in script
    assert "--import-longrun" not in script
    assert "正式流程不帶 --windows-run" in script
    assert "BO #5" in script


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


def test_coverage_plugin_aggregates_actual_unittest_subtests(tmp_path):
    """pytest 的 unittest subTest 多份 call report 應合併，結果檔仍通過嚴格驗證。"""
    run = tmp_path / "subtest-run"
    env = {**os.environ, "SERIALWRAP_COVERAGE_RUN_DIR": str(run), "SERIALWRAP_COVERAGE_LABEL": "python-linux"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_brcm_prompt_regex.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    document = json.loads((run / "results-python-linux.json").read_text(encoding="utf-8"))
    assert not _module()._validate_run(document), document


def test_coverage_plugin_coalesces_subtest_failure_as_failed(tmp_path):
    """真實 pytest unittest.subTest 中任一子測試失敗時，彙整列必須是 failed。"""
    run = tmp_path / "subtest-failure"
    test_file = tmp_path / "test_subtest_failure.py"
    test_file.write_text(
        "import unittest\n"
        "class TestSubtestFailure(unittest.TestCase):\n"
        "    def test_children(self):\n"
        "        for value in range(3):\n"
        "            with self.subTest(value=value):\n"
        "                self.assertNotEqual(value, 1)\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "tests/contract_blackbox"), os.environ.get("PYTHONPATH")])),
        "SERIALWRAP_COVERAGE_RUN_DIR": str(run),
        "SERIALWRAP_COVERAGE_LABEL": "python-linux",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "coverage_plugin", str(test_file)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    document = json.loads((run / "results-python-linux.json").read_text(encoding="utf-8"))
    nodeid = next(nodeid for nodeid in document["collected"] if nodeid.endswith("::test_children"))
    calls = [row for row in document["results"] if row["nodeid"] == nodeid and row["phase"] == "call"]
    assert len(calls) == 1 and calls[0]["outcome"] == "failed", proc.stdout + proc.stderr
    assert _module()._nodeid_failure(document, nodeid) == f"call outcome is failed: {nodeid}"


def test_coverage_plugin_reads_artifacts_from_ab_env_snapshot(tmp_path, monkeypatch):
    """P8X-24c6b39-2：根 conftest 清掉 SERIALWRAP_CLI_EXE 後，plugin 仍以清除前快照記錄受測 artifact。"""
    import ab_env

    cli = tmp_path / "serialwrap"
    daemon = tmp_path / "serialwrapd"
    cli.write_bytes(b"\x7fELF cli")
    daemon.write_bytes(b"\x7fELF daemon")
    label = "rust-windows" if os.name == "nt" else "rust-linux"
    monkeypatch.setattr(ab_env, "AB_ENV", {
        "SERIALWRAP_CLI_EXE": str(cli), "SERIALWRAP_DAEMON_EXE": str(daemon), "SERIALWRAP_AB_LABEL": label,
    })
    monkeypatch.delenv("SERIALWRAP_CLI_EXE", raising=False)
    monkeypatch.delenv("SERIALWRAP_DAEMON_EXE", raising=False)
    run = tmp_path / "run"
    monkeypatch.setenv("SERIALWRAP_COVERAGE_RUN_DIR", str(run))
    monkeypatch.setenv("SERIALWRAP_COVERAGE_LABEL", label)
    if label == "rust-windows":
        windows_artifacts = run / "artifacts" / "windows"
        windows_artifacts.mkdir(parents=True)
        (windows_artifacts / "SHA256SUMS").write_text(
            f"{_sha_bytes(cli.read_bytes())} {cli.name}\n{_sha_bytes(daemon.read_bytes())} {daemon.name}\n",
            encoding="utf-8",
        )
    _plugin_run(_plugin_module(), _FakeSession(["tests/contract_blackbox/test_x.py::test_y"]))
    document = json.loads((run / f"results-{label}.json").read_text(encoding="utf-8"))
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


def test_platform_exclusions_filter_nodes_without_ignoring_unlisted_skips(tmp_path):
    """OD-99：逐平台排除明列 nodeid；未列入排除的 SKIP 仍是缺證，不得算 PASS。"""
    cc = _module()
    cargo, portable, posix, windows = "cargo:sw-core::x::filter", "t::portable", "t::posix", "t::windows"
    run = tmp_path / "run-filtered"
    rust_linux = [
        _row(cargo), _row(portable), _row(posix), _row(windows, "skipped"),
    ]
    python_linux = [_row(portable), _row(posix), _row(windows, "skipped")]
    rust_windows = [_row(cargo), _row(portable), _row(posix, "skipped"), _row(windows)]
    python_windows = [_row(portable), _row(posix, "skipped"), _row(windows)]
    docs = [
        json.loads(_results(run, "rust-linux", "linux", rust_linux).read_text(encoding="utf-8")),
        json.loads(_results(run, "python-linux", "linux", python_linux).read_text(encoding="utf-8")),
        json.loads(_results(run, "rust-windows", "windows", rust_windows).read_text(encoding="utf-8")),
        json.loads(_results(run, "python-windows", "windows", python_windows).read_text(encoding="utf-8")),
    ]
    item = _item(
        platforms=("linux", "windows"), nodeids=[cargo, portable, posix, windows],
        exclude_nodeids={"linux": [windows], "windows": [posix]},
    )
    report = cc.evaluate({"rpc_method:filtered": item}, docs, run_dir=run)
    assert report["covered"] == ["rpc_method:filtered"], report["missing_reasons"]

    unfiltered = _item(platforms=("linux",), nodeids=[cargo, portable, windows])
    report2 = cc.evaluate({"rpc_method:unfiltered-skip": unfiltered}, docs[:2], run_dir=run)
    assert "rpc_method:unfiltered-skip" not in report2["covered"]
    assert "windows" in report2["missing_reasons"]["rpc_method:unfiltered-skip"]


def test_platform_exclusions_filter_python_contract_nodes(tmp_path):
    """OD-99：exclude_nodeids 同時適用於 contract_tests，未列名 Python SKIP 仍拒絕。"""
    cc = _module()
    cargo, portable, windows = "cargo:sw-core::x::contract", "t::portable", "t::windows"
    run, docs = _rust_python_docs(
        tmp_path,
        [_row(cargo)],
        [_row(portable), _row(windows, "skipped")],
        rust_collected=[cargo], python_collected=[portable, windows],
    )
    item = _item(
        kind="error_code", nodeids=[cargo], python_nodeids=[portable, windows],
        exclude_nodeids={"linux": [windows]},
    )
    report = cc.evaluate({"error_code:filtered": item}, docs, run_dir=run)
    assert report["covered"] == ["error_code:filtered"], report["missing_reasons"]

    unfiltered = _item(kind="error_code", nodeids=[cargo], python_nodeids=[portable, windows])
    report2 = cc.evaluate({"error_code:unfiltered-skip": unfiltered}, docs, run_dir=run)
    assert "error_code:unfiltered-skip" not in report2["covered"]
    assert "windows" in report2["missing_reasons"]["error_code:unfiltered-skip"]


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
    posix_only_declared: set[str] = set()
    for base in declared:
        module_path = ROOT / base.partition("::")[0]
        if not module_path.is_file():
            continue
        module_ast = ast.parse(module_path.read_text(encoding="utf-8"))
        for statement in module_ast.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in statement.targets):
                continue
            if any(isinstance(node, ast.Attribute) and node.attr == "posix_only" for node in ast.walk(statement.value)):
                posix_only_declared.add(base)
                break
    expected_windows_exclusions = posix_only_declared if os.name == "nt" else set()
    missing_bases = set(declared) - set(variants)
    assert missing_bases == expected_windows_exclusions, (missing_bases, expected_windows_exclusions)
    gate_base = "tests/contract_blackbox/test_s24_file_transfer.py::test_gate_resolve_quiet"
    assert gate_base in declared, "s24-xfer-session-gates 必須保留完整參數化宣告"
    if os.name == "nt":
        assert gate_base in expected_windows_exclusions, "Windows 排除只能來自模組層 posix_only 標記"
    else:
        assert gate_base in referenced, "Linux collection 必須包含 s24-xfer-session-gates 參數"
    for base in sorted(referenced):
        assert sorted(declared.get(base) or []) == sorted(variants[base]), base


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
    """OD-99：只接受列名的 l1_only；未裁決的 l1_only／rust_only 仍須拒絕。"""
    import yaml

    cc = _module()
    cargo = "cargo:sw-core::x::only_rust"
    run, docs = _rust_python_docs(tmp_path, [_row(cargo)], [_row("t::unrelated")])
    report = cc.evaluate({"error_code:X": _item(kind="error_code", nodeids=[cargo])}, docs, run_dir=run)
    assert "error_code:X" not in report["covered"]
    assert "Python" in report["missing_reasons"]["error_code:X"]

    real = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    approved = sorted(
        contract_id for contract_id, item in real.items()
        if item.get("l1_only") and contract_id != "error_code:ECHO_ACK_UNSUPPORTED"
    )
    assert approved, "OD-99 核准的 l1_only 清單不可為空"
    approved_id = next(
        contract_id for contract_id in approved
        if real[contract_id].get("nodeids")
        and all(nodeid.startswith("cargo:") for nodeid in real[contract_id]["nodeids"])
    )
    approved_item = {**real[approved_id], "platforms": ["linux"]}
    run2, docs2 = _rust_python_docs(
        tmp_path / "approved",
        [_row(nodeid) for nodeid in approved_item["nodeids"]],
        [],
    )
    report2 = cc.evaluate({approved_id: approved_item}, docs2, run_dir=run2)
    assert report2["covered"] == [approved_id], report2["missing_reasons"]

    for flag in ("l1_only", "rust_only"):
        coverage_map = tmp_path / f"unapproved-{flag}.yaml"
        coverage_map.write_text(
            yaml.safe_dump({"entries": {"error_code:DEVICE_STILL_HELD": {flag: True}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=flag):
            cc.universe(
                ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
                ROOT / "tests/acceptance/cases.lock.json", coverage_map,
            )


def _node_platforms(nodeid: str) -> set[str]:
    """從測試節點的 pytest marker 判定可執行平台；沒有平台專屬 marker 視為雙平台。"""
    path_text, _, qualified = nodeid.partition("::")
    path = ROOT / path_text
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    marks: set[str] = set()

    def marker_names(node: ast.AST) -> set[str]:
        return {
            child.attr for child in ast.walk(node)
            if isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Attribute)
            and child.value.attr == "mark"
            and isinstance(child.value.value, ast.Name)
            and child.value.value.id == "pytest"
        }

    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in statement.targets
        ):
            marks.update(marker_names(statement.value))

    body = tree.body
    for part in qualified.split("::"):
        name = part.split("[", 1)[0]
        found = next((
            node for node in body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name
        ), None)
        if found is None:
            break
        marks.update(marker_names(ast.Module(body=found.decorator_list, type_ignores=[])))
        if isinstance(found, ast.ClassDef):
            for statement in found.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "pytestmark" for target in statement.targets
                ):
                    marks.update(marker_names(statement.value))
            body = found.body
        else:
            body = []

    if "windows_native" in marks:
        return {"windows"}
    if "posix_only" in marks or "cap_posix_pty" in marks:
        return {"linux"}
    return {"linux", "windows"}


def test_od99_real_universe_has_adjudicated_l1_or_python_nodes(tmp_path):
    """OD-99：真實 universe 逐 ID 有 Python 節點或明確例外，且平台排除不把 skip 算成 PASS。"""
    import yaml

    cc = _module()
    map_path = ROOT / "contracts/coverage_map.yaml"
    map_doc = cc._read_yaml(map_path)
    approvals = map_doc.get("l1_only_approvals")
    assert isinstance(approvals, dict) and approvals, "缺少 OD-99 l1_only 明文核准清單"
    ab_nodeids = map_doc.get("od99_ab_nodeids")
    assert isinstance(ab_nodeids, dict) and ab_nodeids, "缺少 OD-99 對外介面 A/B nodeid 清單"
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", map_path,
    )
    base_map = {key: value for key, value in map_doc.items() if key not in {"od99_ab_nodeids", "l1_only_approvals"}}
    od99_exclusions = {
        (contract_id, platform, nodeid)
        for contract_id, row in ab_nodeids.items()
        for platform, nodeids in (row.get("exclude_nodeids") or {}).items()
        for nodeid in nodeids
    }
    base_approvals = {}
    for contract_id, by_platform in map_doc.get("exclusion_approvals", {}).items():
        for platform, rows in by_platform.items():
            kept = [row for row in rows if (contract_id, platform, row.get("nodeid")) not in od99_exclusions]
            if kept:
                base_approvals.setdefault(contract_id, {})[platform] = kept
    base_map["exclusion_approvals"] = base_approvals
    base_map_path = tmp_path / "coverage_map-before-od99.yaml"
    base_map_path.write_text(yaml.safe_dump(base_map, allow_unicode=True, sort_keys=False), encoding="utf-8")
    base = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", base_map_path,
    )

    expected_api_ids = {
        contract_id for contract_id, item in base.items()
        if item.get("kind") in {"cli_cmd", "rpc_method"}
        and "linux" in item.get("platforms", [])
        and not cc._is_owner_blocked(item)
        and item.get("nodeids")
        and all(nodeid.startswith("cargo:") for nodeid in item["nodeids"])
        and not item.get("python_nodeids")
    }
    assert set(ab_nodeids) == expected_api_ids, (
        f"OD-99 A/B 對應必須且只能涵蓋未有 Python 節點的 API ID；"
        f"missing={sorted(expected_api_ids - set(ab_nodeids))}, extra={sorted(set(ab_nodeids) - expected_api_ids)}"
    )
    for contract_id, row in ab_nodeids.items():
        assert isinstance(row, dict), contract_id
        nodeids = row.get("nodeids", [])
        assert nodeids and all(nodeid.startswith("tests/contract_blackbox/") for nodeid in nodeids), contract_id
        assert set(nodeids) <= set(universe[contract_id]["nodeids"]), contract_id
        for platform, excluded in (row.get("exclude_nodeids") or {}).items():
            assert platform in universe[contract_id]["platforms"], (contract_id, platform)
            assert set(excluded) <= set(nodeids), contract_id
        for nodeid in nodeids:
            platforms = _node_platforms(nodeid)
            for platform in universe[contract_id]["platforms"]:
                excluded = nodeid in row.get("exclude_nodeids", {}).get(platform, [])
                assert excluded == (platform not in platforms), (contract_id, platform, nodeid, excluded)

    expected_l1 = {
        contract_id for contract_id, item in base.items()
        if item.get("kind") in {"bc", "schema", "case"}
        and "linux" in item.get("platforms", [])
        and not cc._is_owner_blocked(item)
        and item.get("nodeids")
        and all(nodeid.startswith("cargo:") for nodeid in item["nodeids"])
        and not item.get("python_nodeids")
        and not item.get("rust_only")
    }
    assert set(approvals) == expected_l1, (
        f"OD-99 l1_only 清單須精確對齊 69 個無 Python 節點的 BC/schema/case；"
        f"missing={sorted(expected_l1 - set(approvals))}, extra={sorted(set(approvals) - expected_l1)}"
    )
    for contract_id, approval in approvals.items():
        assert isinstance(approval, dict) and approval.get("l1_only") is True, contract_id
        reason = approval.get("reason")
        assert isinstance(reason, str) and reason.startswith("OD-99") and reason.endswith("。"), contract_id
        assert "\n" not in reason and "\r" not in reason, contract_id
        assert universe[contract_id]["l1_only"] is True, contract_id
        assert universe[contract_id]["l1_only_reason"] == reason, contract_id

    cases_doc = cc._read_yaml(ROOT / "tests/acceptance/cases.yaml")
    cases = {row["case_id"]: row for row in cases_doc["cases"]}
    case_lock = cc._read_json(ROOT / "tests/acceptance/cases.lock.json")
    prior_membership = {
        "2.1": set(cc._read_json(ROOT / "tests/acceptance/breaker/slice_2.1.json")["required_case_ids"]),
    }
    prior_membership["2.2"] = set(
        cc._read_yaml(ROOT / "tests/acceptance/slice22_evidence_map.yaml")["cases"]
    )
    prior_membership["2.3"] = set(case_lock["sets"]["slice_2.3"])
    required_lock = cc._read_json(ROOT / "tests/acceptance/l1_required.lock.json")
    linux_locked = set(required_lock["sets"]["s21_gate:pytest_full"]["linux"]["nodeids"])
    parametrized = map_doc.get("parametrized", {})
    missing: list[str] = []
    for contract_id, item in universe.items():
        if "linux" not in item.get("platforms", []):
            continue
        if cc._is_owner_blocked(item) or item.get("l1_only") or item.get("rust_only"):
            continue
        if item.get("kind") == "case" and not item.get("nodeids") and not item.get("python_nodeids"):
            case_id = contract_id.removeprefix("case:")
            case = cases.get(case_id, {})
            slice_id = str(case.get("daemon_slice"))
            assert slice_id in prior_membership and case_id in prior_membership[slice_id], (
                f"無本輪節點且無對應先前切片逐案來源：{contract_id}"
            )
            continue
        excluded = set(item.get("exclude_nodeids", {}).get("linux", []))
        python_nodes = [
            nodeid for nodeid in [*item.get("nodeids", []), *item.get("python_nodeids", [])]
            if not nodeid.startswith("cargo:") and nodeid not in excluded
        ]
        if not python_nodes:
            missing.append(contract_id)
            continue
        for nodeid in python_nodes:
            assert "linux" in _node_platforms(nodeid), f"Linux 上不得使用單平台 skip 節點：{contract_id}: {nodeid}"
            params = parametrized.get(nodeid)
            required_nodeids = [f"{nodeid}[{param}]" for param in params] if params is not None else [nodeid]
            for required_nodeid in required_nodeids:
                assert required_nodeid in linux_locked, (
                    f"A/B nodeid 未納入 Linux pytest 凍結集合：{contract_id}: {required_nodeid}"
                )
    assert not missing, f"非擁有者阻擋 ID 缺少 Python 節點或裁決例外：{sorted(missing)}"

    legacy_l1 = map_doc.get("legacy_l1_only_approvals", {})
    legacy_rust = map_doc.get("legacy_rust_only_approvals", {})
    assert set(legacy_l1) == {"error_code:ECHO_ACK_UNSUPPORTED"}
    assert set(legacy_rust) == {"error_code:ECHO_ACK_UNSUPPORTED", "case:sc36-win-installer-com0com-optional"}
    assert universe["error_code:ECHO_ACK_UNSUPPORTED"]["l1_only_reason"] == legacy_l1["error_code:ECHO_ACK_UNSUPPORTED"]
    assert universe["case:sc36-win-installer-com0com-optional"]["rust_only_reason"] == legacy_rust[
        "case:sc36-win-installer-com0com-optional"
    ]

    windows_only = "tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown"
    posix_attach_nodes = {
        "tests/contract_blackbox/test_s24_file_transfer.py::test_release_during_push_aborts_without_resend",
        "tests/contract_blackbox/test_s24_device_handoff.py::test_external_tool_blocks_attach_until_it_exits",
        "tests/contract_blackbox/test_s24_device_handoff.py::test_second_daemon_takes_device_then_returns",
        "tests/contract_blackbox/test_s24_supervision_systemd.py::test_sigterm_ordered_shutdown",
        "tests/contract_blackbox/test_s24_supervision_systemd.py::test_restart_on_failure_keeps_released_and_wal_seq",
    }
    expected_exclusions = {
        "cli_cmd:device attach": {"linux": {windows_only}, "windows": posix_attach_nodes},
        "rpc_method:device.attach": {"linux": {windows_only}, "windows": posix_attach_nodes},
        "error_code:DEVICE_STILL_HELD": {
            "linux": {windows_only},
            "windows": {
                "tests/contract_blackbox/test_s24_device_handoff.py::test_external_tool_blocks_attach_until_it_exits",
                "tests/contract_blackbox/test_s24_device_handoff.py::test_second_daemon_takes_device_then_returns",
                "tests/test_stage0_uart_exclusive.py::TestAttachProbeDrain::test_attach_device_force_rejects_plain_external_holder",
            },
        },
        "error_code:DAEMON_ALREADY_RUNNING": {
            "linux": {"tests/test_lock_win.py::test_two_locks_same_path_second_raises"},
        },
    }
    for contract_id, by_platform in expected_exclusions.items():
        actual = universe[contract_id].get("exclude_nodeids", {})
        for platform, nodeids in by_platform.items():
            assert set(actual.get(platform, [])) == nodeids, (contract_id, platform, actual)
            referenced = set(universe[contract_id].get("nodeids", [])) | set(universe[contract_id].get("python_nodeids", []))
            assert nodeids <= referenced, (contract_id, platform)

    for contract_id in expected_exclusions:
        item = universe[contract_id]
        references = set(item.get("nodeids", [])) | set(item.get("python_nodeids", []))
        for platform in item["platforms"]:
            excluded = set(item.get("exclude_nodeids", {}).get(platform, []))
            for nodeid in references:
                if nodeid.startswith("cargo:"):
                    continue
                if platform not in _node_platforms(nodeid):
                    assert nodeid in excluded, (contract_id, platform, nodeid)


def test_od101_exclusion_set_and_approvals_are_complete():
    """OD101-02：凍結完整平台排除集合；每筆都須有 OD-99／slice24-05 逐筆核准。"""
    import yaml

    cc = _module()
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    expected = {
        ("error_code:DAEMON_ALREADY_RUNNING", "linux", "tests/test_lock_win.py::test_two_locks_same_path_second_raises"),
        ("error_code:DEVICE_STILL_HELD", "linux", "tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown"),
        ("error_code:DEVICE_STILL_HELD", "windows", "tests/contract_blackbox/test_s24_device_handoff.py::test_external_tool_blocks_attach_until_it_exits"),
        ("error_code:DEVICE_STILL_HELD", "windows", "tests/contract_blackbox/test_s24_device_handoff.py::test_second_daemon_takes_device_then_returns"),
        ("error_code:DEVICE_STILL_HELD", "windows", "tests/test_stage0_uart_exclusive.py::TestAttachProbeDrain::test_attach_device_force_rejects_plain_external_holder"),
        ("rpc_method:alias.list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_alias_roundtrip"),
        ("rpc_method:daemon.handoff_stop", "windows", "tests/contract_blackbox/test_s24_update_rollback.py::test_rollback_refuses_while_push[after_probe]"),
        ("rpc_method:device.attach", "linux", "tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown"),
        ("rpc_method:device.attach", "windows", "tests/contract_blackbox/test_s24_file_transfer.py::test_release_during_push_aborts_without_resend"),
        ("rpc_method:device.attach", "windows", "tests/contract_blackbox/test_s24_device_handoff.py::test_external_tool_blocks_attach_until_it_exits"),
        ("rpc_method:device.attach", "windows", "tests/contract_blackbox/test_s24_device_handoff.py::test_second_daemon_takes_device_then_returns"),
        ("rpc_method:device.attach", "windows", "tests/contract_blackbox/test_s24_supervision_systemd.py::test_sigterm_ordered_shutdown"),
        ("rpc_method:device.attach", "windows", "tests/contract_blackbox/test_s24_supervision_systemd.py::test_restart_on_failure_keeps_released_and_wal_seq"),
        ("rpc_method:device.list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_device_mcu_health"),
        ("rpc_method:event.com_status", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_status_lists_enabled_daemon_selector"),
        ("rpc_method:event.rule_get", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_show_returns_registered_rule"),
        ("rpc_method:event.rule_list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_rule_roundtrip"),
        ("rpc_method:event.tail", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_tail_reads_matcher_log"),
        ("rpc_method:log.tail_raw", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_tail_raw_reads_daemon_wal"),
        ("rpc_method:log.tail_text", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_and_wal"),
        ("rpc_method:session.activity", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_activity_reports_live_session"),
        ("rpc_method:session.console_list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_console_attach_detach"),
        ("rpc_method:session.get_state", "windows", "tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_near_limit_no_logout"),
        ("rpc_method:session.list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready"),
        ("rpc_method:session.log_status", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_log_capture_lifecycle"),
        ("rpc_method:wal.current_seq", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_and_wal"),
        ("rpc_method:wal.range", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_and_wal"),
        ("cli_cmd:daemon start", "windows", "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_start_ab_conflict"),
        ("cli_cmd:daemon restart", "windows", "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_restart_ab"),
        ("cli_cmd:device list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_device_mcu_health"),
        ("cli_cmd:device attach", "linux", "tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown"),
        ("cli_cmd:device attach", "windows", "tests/contract_blackbox/test_s24_file_transfer.py::test_release_during_push_aborts_without_resend"),
        ("cli_cmd:device attach", "windows", "tests/contract_blackbox/test_s24_device_handoff.py::test_external_tool_blocks_attach_until_it_exits"),
        ("cli_cmd:device attach", "windows", "tests/contract_blackbox/test_s24_device_handoff.py::test_second_daemon_takes_device_then_returns"),
        ("cli_cmd:device attach", "windows", "tests/contract_blackbox/test_s24_supervision_systemd.py::test_sigterm_ordered_shutdown"),
        ("cli_cmd:device attach", "windows", "tests/contract_blackbox/test_s24_supervision_systemd.py::test_restart_on_failure_keeps_released_and_wal_seq"),
        ("cli_cmd:session list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready"),
        ("cli_cmd:session activity", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_activity_reports_live_session"),
        ("cli_cmd:session console-list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_console_attach_detach"),
        ("cli_cmd:session log-status", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_log_capture_lifecycle"),
        ("cli_cmd:alias list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_alias_roundtrip"),
        ("cli_cmd:log tail-raw", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_tail_raw_reads_daemon_wal"),
        ("cli_cmd:log tail-text", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_and_wal"),
        ("cli_cmd:wal export", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_and_wal"),
        ("cli_cmd:wal current-seq", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_log_and_wal"),
        ("cli_cmd:event list", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_rule_roundtrip"),
        ("cli_cmd:event show", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_show_returns_registered_rule"),
        ("cli_cmd:event status", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_status_lists_enabled_daemon_selector"),
        ("cli_cmd:event tail", "windows", "tests/contract_blackbox/test_bb_daemon_e2e.py::test_event_tail_reads_matcher_log"),
    }
    actual = {
        (contract_id, platform, nodeid)
        for contract_id, item in universe.items()
        for platform, nodeids in item.get("exclude_nodeids", {}).items()
        for nodeid in nodeids
    }
    assert actual == expected, f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"

    map_doc = yaml.safe_load((ROOT / "contracts/coverage_map.yaml").read_text(encoding="utf-8"))
    approvals = map_doc.get("exclusion_approvals")
    assert isinstance(approvals, dict), "每筆平台排除都必須有逐筆核准與理由"
    approved = {
        (contract_id, platform, row.get("nodeid"))
        for contract_id, by_platform in approvals.items()
        for platform, rows in by_platform.items()
        for row in rows
        if isinstance(row, dict)
    }
    assert approved == expected, f"approval missing={sorted(expected - approved)}, extra={sorted(approved - expected)}"
    assert len({contract_id for contract_id, platform, _ in expected if platform == "linux"}) == 4
    assert len({contract_id for contract_id, platform, _ in expected if platform == "windows"}) == 35
    assert len({contract_id for contract_id, platform, _ in expected
                if platform == "windows" and contract_id in map_doc["od99_ab_nodeids"]}) == 32
    for contract_id, by_platform in approvals.items():
        for platform, rows in by_platform.items():
            for row in rows:
                reason = row.get("reason") if isinstance(row, dict) else None
                assert isinstance(reason, str) and reason.strip() and "\n" not in reason and "\r" not in reason
                assert "OD-99" in reason or "slice24-05" in reason, (contract_id, platform, row)


def test_od101_exclusion_approval_and_nonempty_platform_gate(tmp_path):
    """OD101-02：缺核准／理由、孤兒核准及排除後空集合均須拒絕。"""
    import copy
    import yaml

    cc = _module()
    source = ROOT / "contracts/coverage_map.yaml"
    map_doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    source_universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", source,
    )
    approvals = {}
    for contract_id, item in source_universe.items():
        for platform, nodeids in item.get("exclude_nodeids", {}).items():
            approvals.setdefault(contract_id, {}).setdefault(platform, []).extend(
                {"nodeid": nodeid, "reason": "OD-99 測試夾具逐筆核准。"} for nodeid in nodeids
            )
    map_doc["exclusion_approvals"] = approvals

    def evaluate(doc, name):
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=True), encoding="utf-8")
        return cc.universe(
            ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
            ROOT / "tests/acceptance/cases.lock.json", path,
        )

    missing = copy.deepcopy(map_doc)
    missing["exclusion_approvals"]["error_code:DAEMON_ALREADY_RUNNING"]["linux"].clear()
    no_reason = copy.deepcopy(map_doc)
    no_reason["exclusion_approvals"]["error_code:DAEMON_ALREADY_RUNNING"]["linux"][0]["reason"] = ""
    orphan = copy.deepcopy(map_doc)
    orphan["exclusion_approvals"]["error_code:DAEMON_ALREADY_RUNNING"]["linux"].append(
        {"nodeid": "tests/test_lock_win.py::test_not_an_exclusion", "reason": "OD-99 測試夾具逐筆核准。"}
    )
    for name, bad_doc in (("missing", missing), ("no-reason", no_reason), ("orphan", orphan)):
        with pytest.raises(ValueError):
            evaluate(bad_doc, name)

    empty = copy.deepcopy(map_doc)
    nodeid = "tests/contract_blackbox/test_s24_mcu_rpc.py::test_patterns_exact_bytes"
    empty["entries"]["rpc_method:mcu.patterns"].setdefault("exclude_nodeids", {})["linux"] = [nodeid]
    empty["exclusion_approvals"].setdefault("rpc_method:mcu.patterns", {}).setdefault("linux", []).append(
        {"nodeid": nodeid, "reason": "slice24-05 測試夾具逐筆核准。"}
    )
    with pytest.raises(ValueError, match="排除後.*必要節點為空"):
        evaluate(empty, "empty-platform")


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
        "--only", "error_code:X_XPLAT", "--map", "error_code:X_XPLAT=t::x",
        "--platforms", "error_code:X_XPLAT=linux,windows",
        "--expect-platform", "linux",
        "--expect-cli-sha256", linux_doc["cli_sha256"], "--expect-daemon-sha256", linux_doc["daemon_sha256"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert output["identity_errors"] == []
    assert "error_code:X_XPLAT" in output["covered"]


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


def test_shared_coverage_package_import_merges_same_label_before_contract_judgement(tmp_path):
    """OD-88／OD-93：兩層 coverage 封裝扁平匯入；重複 Python baseline 只計一次，Rust Cargo／pytest 同標籤結果仍合併。"""
    cc = _module()
    contract_id = "case:s24-xfer-templates-golden"
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    item = universe[contract_id]
    assert item["rust_only"] is True
    cargo_nodeids = [nodeid for nodeid in item["nodeids"] if nodeid.startswith("cargo:")]
    pytest_nodeids = [nodeid for nodeid in item["nodeids"] if not nodeid.startswith("cargo:")]
    assert cargo_nodeids and pytest_nodeids

    cargo_dir = tmp_path / "cargo-results"
    shared_run = tmp_path / "gate-shared-run"
    coverage_dir = shared_run / "coverage"
    python_nodeid = "tests/test_rust_contract_coverage.py::test_nested_pytest_does_not_write_into_outer_coverage_run"
    _results(cargo_dir, "rust-linux", "linux", [_row(nodeid) for nodeid in cargo_nodeids])
    _results(coverage_dir, "python-linux", "linux", [_row(python_nodeid)])
    cargo_archive = tmp_path / "cargo-results.tar.gz"
    with tarfile.open(cargo_archive, "w:gz") as archive:
        archive.add(cargo_dir, arcname=".")
    python_archive = tmp_path / "python-baseline-coverage.tar.gz"
    packed = subprocess.run(
        ["tar", "-czf", str(python_archive), "-C", str(shared_run), "coverage"],
        capture_output=True, text=True, check=False,
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    _results(coverage_dir, "rust-linux", "linux", [_row(nodeid) for nodeid in pytest_nodeids])
    coverage_archive = tmp_path / "coverage.tar.gz"
    packed = subprocess.run(
        ["tar", "-czf", str(coverage_archive), "-C", str(shared_run), "coverage"],
        capture_output=True, text=True, check=False,
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    with tarfile.open(python_archive, "r:gz") as archive:
        assert any(member.name == "coverage/results-python-linux.json" for member in archive.getmembers())
    with tarfile.open(coverage_archive, "r:gz") as archive:
        members = {member.name for member in archive.getmembers()}
        assert "coverage/results-python-linux.json" in members
        assert "coverage/results-rust-linux.json" in members

    shared_api_path = ROOT / "scripts/rust/gate_shared_evidence.py"
    shared_spec = importlib.util.spec_from_file_location("gate_shared_evidence_s24_test", shared_api_path)
    assert shared_spec is not None and shared_spec.loader is not None
    shared_api = importlib.util.module_from_spec(shared_spec)
    shared_spec.loader.exec_module(shared_api)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    shared_api.is_worktree_clean = lambda root=ROOT: True
    shared_api.get_current_head = lambda root=ROOT: head
    shared = tmp_path / "shared-evidence"
    bundle = shared / "candidate-bundle"
    version_bin = bundle / "versions" / CURRENT_VERSION / "bin"
    version_bin.mkdir(parents=True)
    cli_bytes = b"candidate Rust CLI"
    daemon_bytes = b"candidate Rust daemon"
    (version_bin / "serialwrap").write_bytes(cli_bytes)
    (version_bin / "serialwrapd").write_bytes(daemon_bytes)
    (version_bin.parent / "release-manifest.json").write_text(
        json.dumps({
            "bundle_version": CURRENT_VERSION,
            "stage": "rust-2",
            "target": "x86_64-unknown-linux-gnu",
            "git_revision": head,
            "components": {
                "cli": {"implementation": "rust", "sha256": hashlib.sha256(cli_bytes).hexdigest()},
                "daemon": {
                    "implementation": "rust",
                    "code_fingerprint": hashlib.sha256(daemon_bytes).hexdigest()[:12],
                    "launcher": {"sha256": hashlib.sha256(daemon_bytes).hexdigest()},
                },
            },
        }),
        encoding="utf-8",
    )
    top_bin = bundle / "bin"
    top_bin.mkdir()
    (top_bin / "serialwrap").write_bytes(cli_bytes)
    (top_bin / "serialwrapd").write_bytes(daemon_bytes)

    artifacts = {}
    for name in shared_api.REQUIRED_ARTIFACTS:
        if name == "release_cli":
            source = version_bin / "serialwrap"
        elif name == "release_daemon":
            source = version_bin / "serialwrapd"
        else:
            source = tmp_path / "artifact-sources" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"candidate:{name}".encode())
        artifacts[name] = source
    shared_api.init_manifest(shared, artifacts, head=head, clean=True, candidate_bundle=bundle)
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    pytest_argv = lock["sets"]["s21_gate:pytest_full"]["linux"]["selector"]["argv"]
    baseline_l1 = tmp_path / "pytest-full" / "l1-results.json"
    baseline_l1.parent.mkdir(parents=True)
    baseline_l1.write_text("{}", encoding="utf-8")
    baseline_log = tmp_path / "pytest-full.log"
    baseline_log.write_text("pytest baseline passed\\n", encoding="utf-8")
    shared_api.record_section(
        shared, name="pytest_full", argv=pytest_argv, exit_code=0,
        files={
            "slice24/python-baseline-coverage.tar.gz": python_archive,
            "logs/pytest-full.log": baseline_log,
            "p7_gate/pytest-full/l1-results.json": baseline_l1,
        },
        required_sets=["s21_gate:pytest_full", "p7_gate:python3-m-pytest-q-tests"],
    )
    cargo_argv = ["--include-cap", "posix_pty", "--include-cap", "process_rpc", "--workspace", "--features", "test-fakes"]
    shared_api.record_section(
        shared, name="cargo_contract_results", argv=cargo_argv, exit_code=0,
        files={"slice24/cargo-results.tar.gz": cargo_archive}, platform="linux",
    )
    s24_argv = [
        "-q", "tests/contract_blackbox", "tests/acceptance/l15/test_slice21_l15.py",
        "tests/acceptance/l15/test_slice23_l15.py", "tests/test_multiagent_e2e.py",
        "tests/test_human_agent_coexist.py", "tests/test_rust_contract_coverage.py",
        "tests/test_transfer_golden_fresh.py",
    ]
    shared_api.record_section(
        shared, name="s24_ab", argv=s24_argv, exit_code=0,
        files={"slice24/coverage.tar.gz": coverage_archive}, platform="linux",
    )
    imported_cargo = shared_api.import_section(
        shared, gate="slice24", section="cargo_contract_results", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True,
        destination=tmp_path / "imported-cargo", expected_command="cargo_contract_results",
    )
    assert imported_cargo["status"] == "PASS", imported_cargo
    imported_coverage = shared_api.import_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True,
        destination=tmp_path / "imported-pytest", expected_command="s24_ab",
    )
    assert imported_coverage["status"] == "PASS", imported_coverage
    imported_baseline = shared_api.import_section(
        shared, gate="slice24", section="pytest_full", required_set="p7_gate:python3-m-pytest-q-tests",
        platform="linux", expected_artifacts=artifacts, current_head=head, worktree_clean=True,
        destination=tmp_path / "imported-python-baseline",
    )
    assert imported_baseline["status"] == "PASS", imported_baseline

    shared_script = (ROOT / "scripts/rust/gate_shared.sh").read_text(encoding="utf-8")
    assert 'python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section pytest_full' in shared_script
    assert 'python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section s24_ab' in shared_script
    assert 'tar -czf "$RUN_DIR/work/python-baseline-coverage.tar.gz" -C "$RUN_DIR" coverage' in shared_script
    assert 'S24_COVERAGE_TAR="$RUN_DIR/work/s24-coverage.tar.gz"' in shared_script
    assert 'tar -czf "$S24_COVERAGE_TAR" -C "$RUN_DIR" coverage' in shared_script
    slice24_script = (ROOT / "scripts/rust/slice24_exit.sh").read_text(encoding="utf-8")
    assert 'tar -xzf "$RUN_DIR/shared-import/slice24/python-baseline-coverage.tar.gz"' in slice24_script
    assert 'tar -xzf "$RUN_DIR/shared-import/slice24/coverage.tar.gz"' in slice24_script
    assert 'CONTRACT_COVERAGE_ARGS+=(--merge-results-from "$PYTHON_BASELINE_COVERAGE")' in slice24_script
    assert 'CONTRACT_COVERAGE_ARGS+=(--merge-results-from "$RUST_BLACKBOX_COVERAGE")' in slice24_script
    assert 'PYTHON_BASELINE_COVERAGE="$RUN_DIR/shared-import/python-baseline-coverage"' in slice24_script
    assert 'RUST_BLACKBOX_COVERAGE="$RUN_DIR/shared-import/rust-blackbox-coverage"' in slice24_script
    assert slice24_script.count("--strip-components=1") >= 2

    run = tmp_path / "slice24-run"
    run.mkdir()
    cargo_import = subprocess.run(
        ["tar", "-xzf", imported_cargo["files"]["slice24/cargo-results.tar.gz"], "-C", str(run)],
        capture_output=True, text=True, check=False,
    )
    assert cargo_import.returncode == 0, cargo_import.stdout + cargo_import.stderr
    python_layer = tmp_path / "flattened-python-baseline"
    python_layer.mkdir()
    python_import = subprocess.run(
        ["tar", "-xzf", imported_baseline["files"]["slice24/python-baseline-coverage.tar.gz"],
         "-C", str(python_layer), "--strip-components=1"],
        capture_output=True, text=True, check=False,
    )
    assert python_import.returncode == 0, python_import.stdout + python_import.stderr
    rust_layer = tmp_path / "flattened-rust-blackbox-coverage"
    rust_layer.mkdir()
    rust_import = subprocess.run(
        ["tar", "-xzf", imported_coverage["files"]["slice24/coverage.tar.gz"], "-C", str(rust_layer),
         "--strip-components=1"],
        capture_output=True, text=True, check=False,
    )
    assert rust_import.returncode == 0, rust_import.stdout + rust_import.stderr
    judgement_path = tmp_path / "contract-coverage.json"
    judged = _run(
        "--run-dir", str(run), "--merge-results-from", str(python_layer),
        "--merge-results-from", str(rust_layer), "--only", contract_id,
        "--platforms", f"{contract_id}=linux", "--json", str(judgement_path),
    )
    assert judged.returncode == 0, judged.stdout + judged.stderr
    verdict = json.loads(judgement_path.read_text(encoding="utf-8"))
    merged_python = json.loads((run / "results-python-linux.json").read_text(encoding="utf-8"))
    merged_rust = json.loads((run / "results-rust-linux.json").read_text(encoding="utf-8"))
    assert merged_python["collected"].count(python_nodeid) == 1, merged_python["collected"]
    assert set(cargo_nodeids + pytest_nodeids) <= set(merged_rust["collected"]), merged_rust["collected"]
    assert len(merged_rust["collected"]) == len(set(merged_rust["collected"]))
    assert contract_id in verdict["covered"], verdict


# ---------------------------------------------------------------------------
# T15 第 3 輪審查（P8X-e038dda-1～3）回歸
# ---------------------------------------------------------------------------
def _judge_reason(run: Path, out: Path) -> tuple[str, str]:
    _, output = _judge_lr_mixed(run, out)
    return output["case_verdicts"]["lr-mixed"], str(output["blocked_owner"].get("case:lr-mixed", ""))


def _other_candidate(base: Path) -> Path:
    return _candidate_bundle(base / "candidate-b", "linux", cli=b"cli-linux-b", daemon=b"d-linux-b")


def _same_bins_other_bundle(base: Path) -> Path:
    return _candidate_bundle(
        base / "candidate-c", "linux", cli=b"cli-linux", daemon=b"d-linux", extra={"py/extra.py": b"changed"},
    )


@pytest.mark.parametrize(
    "scenario,needle",
    [
        ("other_candidate", "本輪候選"),
        ("same_bins_other_bundle", "bundle"),
        ("no_bundle_record", "candidate-bundle.json"),
        ("no_rust_results", "rust-linux"),
        ("record_not_tested_artifact", "受測"),
    ],
    ids=["other_candidate", "same_bins_other_bundle", "no_bundle_record", "no_rust_results", "record_not_tested_artifact"],
)
def test_longrun_must_bind_current_candidate(tmp_path, scenario, needle):
    """OD-94：獨立長跑仍須綁定候選 bundle；其判定不進 Slice 2.4。"""
    run = tmp_path / "run"
    imported = _import_longrun(run, _longrun_source(tmp_path))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    _bind_candidate(run, tmp_path, platforms=("windows",))
    same = tmp_path / "bundles" / "linux"
    if scenario == "other_candidate":
        _bind_bundle(run, "linux", _other_candidate(tmp_path))
    elif scenario == "same_bins_other_bundle":
        _bind_bundle(run, "linux", _same_bins_other_bundle(tmp_path))
    elif scenario == "no_bundle_record":
        _bind_bundle(run, "linux", same, record=False)
    elif scenario == "no_rust_results":
        _bind_bundle(run, "linux", same, results=False)
    elif scenario == "record_not_tested_artifact":
        # 候選 bundle 紀錄是 A，但本輪 rust-linux 實際受測的是另一份 artifact。
        _results(run, "rust-linux", "linux", [])
        _bind_bundle(run, "linux", same, results=False)
    bound_verdict, bound_reason = _independent_longrun_verdict(run)
    assert bound_verdict == "BLOCKED-OWNER", bound_reason
    assert "linux" in bound_reason and needle in bound_reason, bound_reason
    verdict, reason = _judge_reason(run, tmp_path / "o.json")
    assert verdict == "BLOCKED-OWNER" and "OD-94" in reason


def test_record_candidate_bundle_matches_deployed_content_digest(tmp_path):
    """P8X-e038dda-1：候選 bundle 紀錄的 tree 雜湊與 realhw deployed_content 量測同算法；不覆寫、拒收 symlink。"""
    bundle = _candidate_bundle(tmp_path / "b", "linux", cli=b"cli-linux", daemon=b"d-linux")
    run = tmp_path / "run"
    result = _run("--run-dir", str(run), "--record-candidate-bundle", f"linux={bundle}")
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((run / "artifacts" / "linux" / "candidate-bundle.json").read_text(encoding="utf-8"))
    assert record == _candidate_record(bundle, "linux")
    assert record["bundle_tree_sha256"] == _bundle_tree(bundle)
    again = _run("--run-dir", str(run), "--record-candidate-bundle", f"linux={bundle}")
    assert again.returncode == 2, again.stdout + again.stderr

    linked = _candidate_bundle(tmp_path / "linked", "linux", cli=b"cli-linux", daemon=b"d-linux")
    (linked / "py").mkdir()
    (linked / "py" / "link").symlink_to(bundle / "release-manifest.json")
    rejected = _run("--run-dir", str(tmp_path / "run2"), "--record-candidate-bundle", f"linux={linked}")
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert not (tmp_path / "run2" / "artifacts" / "linux" / "candidate-bundle.json").exists()


def test_slice24_exit_binds_candidate_bundle():
    """P8X-e038dda-1：出口腳本可指定候選 bundle，受測 artifact 取自該 bundle 並寫入候選 bundle 紀錄。"""
    script = EXIT_SCRIPT.read_text(encoding="utf-8")
    assert "--candidate-bundle)" in script
    assert '--record-candidate-bundle "linux=$CANDIDATE_ROOT"' in script
    assert 'RUST_CLI="$CANDIDATE_ROOT/bin/serialwrap"' in script
    assert 'RUST_DAEMON="$CANDIDATE_ROOT/bin/serialwrapd"' in script
    assert '--record-candidate-bundle "windows=$WINDOWS_RUN/bundle"' in script
    assert "tests/acceptance/l15/test_slice21_l15.py" in script
    assert "tests/acceptance/l15/test_slice23_l15.py" in script
    assert "--slice21-run)" in script and "--slice22-gate)" in script and "--slice23-run)" in script
    assert 'PRIOR_CASE_ARGS+=(--slice21-run "$PRIOR_SLICE21")' in script
    assert 'PRIOR_CASE_ARGS+=(--slice22-gate "$PRIOR_SLICE22")' in script
    assert 'PRIOR_CASE_ARGS+=(--slice23-run "$PRIOR_SLICE23")' in script
    assert 'CONTRACT_COVERAGE_ARGS+=("${PRIOR_CASE_ARGS[@]}")' in script
    assert '--expect-git-revision "$(git rev-parse HEAD)"' in script
    assert 'run_logged policy-check python3 -m policy_check --repo . --pr-labels "$POLICY_LABEL"' in script
    assert '實際帶入的 label：`%s`' in script


def _rewrite_gate(src: Path, platform: str, name: str, mutate) -> None:
    path = src / platform / name
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _resummarize(doc: dict) -> None:
    doc["release_gate_result"] = _gate()._summarize(doc["checks"], True)


def _set_result(doc: dict, check_id: str, result: str) -> None:
    for check in doc["checks"]:
        if check["id"] == check_id:
            check["result"] = result


def _drop(doc: dict, check_id: str) -> None:
    doc["checks"] = [check for check in doc["checks"] if check["id"] != check_id]


def test_longrun_dropping_safety_fail_row_is_rejected(tmp_path):
    """OD-94：安全檢查 FAIL 照實留在獨立報告；刪除 FAIL 列仍拒收，2.4 維持 BLOCKED-OWNER。"""
    def fail_safety(doc):
        _set_result(doc, "safety_unauthorized_tx", "FAIL")
        _resummarize(doc)

    src = _longrun_source(tmp_path)
    _rewrite_gate(src, "linux", "release-gate.json", fail_safety)
    run = tmp_path / "run"
    imported = _import_longrun(run, src)
    assert imported.returncode == 0, imported.stdout + imported.stderr
    _bind_candidate(run, tmp_path)
    assert _independent_longrun_verdict(run)[0] == "FAIL"
    verdict, reason = _judge_reason(run, tmp_path / "o.json")
    assert verdict == "BLOCKED-OWNER" and "OD-94" in reason

    def drop_row(doc):
        _drop(doc, "safety_unauthorized_tx")
        _resummarize(doc)

    _rewrite_gate(src, "linux", "release-gate.json", drop_row)
    run2 = tmp_path / "run2"
    imported2 = _import_longrun(run2, src)
    assert imported2.returncode == 1, imported2.stdout + imported2.stderr
    record = json.loads((run2 / "longrun-evidence.json").read_text(encoding="utf-8"))
    assert record["platforms"]["linux"]["accepted"] is False
    assert any("safety_unauthorized_tx" in error for error in record["platforms"]["linux"]["errors"]), record
    _bind_candidate(run2, tmp_path)
    verdict2, reason2 = _judge_reason(run2, tmp_path / "o2.json")
    assert verdict2 == "BLOCKED-OWNER" and "OD-94" in reason2


def _cand(mutate):
    def apply(src: Path) -> None:
        def wrapped(doc):
            mutate(doc)
            _resummarize(doc)
        _rewrite_gate(src, "linux", "release-gate.json", wrapped)
    return apply


def _base(mutate):
    def apply(src: Path) -> None:
        _rewrite_gate(src, "linux", "baseline-release-gate.json", mutate)
    return apply


def _duplicate(doc: dict, check_id: str) -> None:
    doc["checks"].append(dict(next(check for check in doc["checks"] if check["id"] == check_id)))


def _regroup(doc: dict, check_id: str, group: str) -> None:
    for check in doc["checks"]:
        if check["id"] == check_id:
            check["group"] = group


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (_cand(lambda doc: _drop(doc, "resources")), "resources"),
        (_cand(lambda doc: _drop(doc, "liveness_daemon_exit")), "liveness_daemon_exit"),
        (_cand(lambda doc: _drop(doc, "error_rate_relative")), "error_rate_relative"),
        (_cand(lambda doc: _duplicate(doc, "safety_double_holder")), "重複"),
        (_cand(lambda doc: doc["checks"].append(
            {"id": "custom_extra", "group": "evidence", "status": "evaluated", "result": "PASS", "detail": {}})),
         "custom_extra"),
        (_cand(lambda doc: _set_result(doc, "error_rate_relative", _NA)), _NA),
        (_cand(lambda doc: _regroup(doc, "liveness_restart", "evidence")), "group"),
        (_base(lambda doc: _drop(doc, "safety_unauthorized_tx")), "baseline-release-gate.json"),
        (_base(lambda doc: _set_result(doc, "safety_unauthorized_tx", "FAIL")), "baseline_result"),
    ],
    ids=[
        "cand_drop_resources", "cand_drop_liveness_exit", "cand_drop_relative", "cand_duplicate_id",
        "cand_extra_id", "cand_not_applicable", "cand_group_mismatch", "base_drop_safety", "base_result_mismatch",
    ],
)
def test_longrun_requires_formal_gate_check_set(tmp_path, mutate, needle):
    """OD-94：獨立長跑仍要求正式 P2 gate 的完整檢查集合、ID 唯一、group 與適用性一致。"""
    src = _longrun_source(tmp_path)
    mutate(src)
    run = tmp_path / "run"
    imported = _import_longrun(run, src)
    assert imported.returncode == 1, imported.stdout + imported.stderr
    record = json.loads((run / "longrun-evidence.json").read_text(encoding="utf-8"))
    errors = record["platforms"]["linux"]["errors"]
    assert record["platforms"]["linux"]["accepted"] is False
    assert any(needle in error for error in errors), errors
    _bind_candidate(run, tmp_path)
    verdict, _ = _judge_reason(run, tmp_path / "o.json")
    assert verdict == "BLOCKED-OWNER"


def test_parametrized_union_requires_every_node_on_both_sides(tmp_path):
    """P8X-e038dda-3：雙邊參數聯集的每個節點都要逐側收集且通過；雙邊皆未通過的節點不得漏判。"""
    cc = _module()
    base = "tests/t.py::test_gate"
    item = _item(nodeids=[base], parametrized={base: ["quiet"]})
    rust_rows = [_row(f"{base}[quiet]")]
    python_rows = [_row(f"{base}[quiet]"), _row(f"{base}[extra]", outcome="skipped", phase="setup")]
    run, docs = _rust_python_docs(tmp_path, rust_rows, python_rows)
    report = cc.evaluate({"rpc_method:params": item}, docs, run_dir=run)
    assert "rpc_method:params" not in report["covered"]
    assert f"{base}[extra]" in report["missing_reasons"].get("rpc_method:params", ""), report["missing_reasons"]

    both_skip = [_row(f"{base}[quiet]"), _row(f"{base}[extra]", outcome="skipped", phase="setup")]
    run2, docs2 = _rust_python_docs(tmp_path / "skip", both_skip, both_skip)
    report2 = cc.evaluate({"rpc_method:params": item}, docs2, run_dir=run2)
    assert "rpc_method:params" not in report2["covered"]
    assert f"{base}[extra]" in report2["missing_reasons"].get("rpc_method:params", ""), report2["missing_reasons"]

    rust_fail = [_row(f"{base}[quiet]"), _row(f"{base}[extra]", outcome="failed")]
    run3, docs3 = _rust_python_docs(tmp_path / "fail", rust_fail, python_rows)
    report3 = cc.evaluate({"rpc_method:params": item}, docs3, run_dir=run3)
    assert "rpc_method:params" in report3["failed"], report3


def _prior_slice_fixture(tmp_path):
    import yaml

    cc = _module()
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "error_codes.yaml").write_text("codes: []\n", encoding="utf-8")
    (contracts / "session_last_errors.yaml").write_text("values: []\n", encoding="utf-8")
    (contracts / "behavior_changes.yaml").write_text("changes: []\n", encoding="utf-8")
    (contracts / "rpc_schema.json").write_text('{"x-methods": {}}\n', encoding="utf-8")
    (contracts / "cli_schema.json").write_text('{"x-groups": []}\n', encoding="utf-8")
    (contracts / "wal_record.schema.json").write_text('{}\n', encoding="utf-8")
    (contracts / "state_file.schema.json").write_text('{}\n', encoding="utf-8")
    map_file = contracts / "coverage_map.yaml"
    map_file.write_text("entries: {}\n", encoding="utf-8")

    ids_by_slice = {
        "2.1": ["s21-pass", "s21-fail", "s21-absent", "s21-owner"],
        "2.2": ["s22-pass", "s22-absent", "s22-invalid"],
        "2.3": ["s23-pass", "s23-gap", "s23-invalid"],
    }
    cases = tmp_path / "cases.yaml"
    cases.write_text(yaml.safe_dump({"cases": [
        {"case_id": case_id, "daemon_slice": slice_id, "suite": "contract-blackbox", "layer": "L1",
         "platforms": ["linux"], "evidence": []}
        for slice_id, case_ids in ids_by_slice.items() for case_id in case_ids
    ]}, allow_unicode=True), encoding="utf-8")
    cases_lock = tmp_path / "cases.lock.json"
    cases_lock.write_text(json.dumps({"sets": {"slice_2.4": [case_id for rows in ids_by_slice.values() for case_id in rows]}}), encoding="utf-8")

    run = tmp_path / "run"
    run.mkdir()
    cli = run / "cli-artifact"
    daemon = run / "daemon-artifact"
    cli.write_bytes(b"candidate cli")
    daemon.write_bytes(b"candidate daemon")
    daemon_sha = hashlib.sha256(daemon.read_bytes()).hexdigest()
    prior_daemon_sha = daemon_sha
    slice23_debug_daemon_sha = hashlib.sha256(b"slice 2.3 debug daemon").hexdigest()
    revision = "a" * 40
    (run / "results-rust-linux.json").write_text(json.dumps({
        "label": "rust-linux", "platform": "linux", "cli_exe": str(cli),
        "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(), "daemon_exe": str(daemon),
        "daemon_sha256": daemon_sha, **REV,
        "contract_revision": cc._contract_revision(contracts),
        "cases_yaml_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
        "cases_lock_sha256": hashlib.sha256(cases_lock.read_bytes()).hexdigest(),
        "collected": [], "exit_code": 0, "finished": True, "results": [],
    }), encoding="utf-8")

    slice21 = tmp_path / "slice21"
    slice21.mkdir()
    (slice21 / "report.json").write_text(json.dumps({
        "run_id": "s21-fixture", "git_revision": revision,
        "identity": {"daemon_sha256": prior_daemon_sha},
    }), encoding="utf-8")
    (slice21 / "case_verdicts.json").write_text(json.dumps({
        "s21-pass": "PASS", "s21-fail": "FAIL", "s21-owner": "BLOCKED-OWNER",
    }), encoding="utf-8")

    slice22 = tmp_path / "s22-gate.json"
    slice22.write_text(json.dumps({
        "identity": {"git_revision": revision, "daemon_sha256": prior_daemon_sha},
        "cases": [{"case_id": "s22-pass", "status": "PASS"}, {"case_id": "s22-invalid", "status": []}],
    }), encoding="utf-8")

    slice23 = tmp_path / "slice23"
    slice23.mkdir()
    (slice23 / "results.json").write_text(json.dumps({
        "git_head": revision,
        "artifacts": {"target/debug/serialwrap": hashlib.sha256(cli.read_bytes()).hexdigest(),
                      "target/debug/serialwrapd": slice23_debug_daemon_sha},
        "prior_slice_artifacts": {"serialwrapd_release": prior_daemon_sha},
    }), encoding="utf-8")
    (slice23 / "coverage.json").write_text(json.dumps({
        "set": "slice_2.3", "rows": [
            {"case_id": "s23-pass", "status": "PASS", "detail": "s23 gate PASS"},
            {"case_id": "s23-gap", "status": "GAP", "detail": "缺少逐案證據"},
            {"case_id": "s23-invalid", "status": {}, "detail": "格式錯誤"},
        ],
    }), encoding="utf-8")

    return {
        "cc": cc, "contracts": contracts, "cases": cases, "cases_lock": cases_lock,
        "map_file": map_file, "run": run, "slice21": slice21, "slice22": slice22,
        "slice23": slice23, "revision": revision, "daemon_sha": daemon_sha,
        "prior_daemon_sha": prior_daemon_sha, "slice23_debug_daemon_sha": slice23_debug_daemon_sha,
        "ids": [f"case:{case_id}" for values in ids_by_slice.values() for case_id in values],
    }


def _prior_slice_command(fixture, output: Path, selected: list[str]) -> list[str]:
    args = [
        "--run-dir", str(fixture["run"]), "--contracts-dir", str(fixture["contracts"]),
        "--cases", str(fixture["cases"]), "--cases-lock", str(fixture["cases_lock"]),
        "--map-file", str(fixture["map_file"]), "--json", str(output),
        "--expect-git-revision", fixture["revision"], "--expect-daemon-sha256", fixture["daemon_sha"],
        "--slice21-run", str(fixture["slice21"]), "--slice22-gate", str(fixture["slice22"]),
        "--slice23-run", str(fixture["slice23"]),
    ]
    for contract_id in selected:
        args.extend(["--only", contract_id])
    return args


def test_prior_slice_universe_lists_seventy_two_unmapped_cases():
    """OD-88：靜態凍結 universe 的先前切片缺口為 2.1／2.2／2.3 的 13／18／41 案。"""
    from collections import Counter
    import yaml

    cc = _module()
    cases_doc = yaml.safe_load((ROOT / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))
    cases = {row["case_id"]: row for row in cases_doc["cases"]}
    frozen = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    gaps = [
        case_id for case_id in frozen["sets"]["slice_2.4"]
        if cases[case_id].get("daemon_slice") in {"2.1", "2.2", "2.3"}
        and not universe[f"case:{case_id}"]["nodeids"]
        and not cc._is_owner_blocked(universe[f"case:{case_id}"])
    ]
    assert len(gaps) == 72
    assert Counter(cases[case_id]["daemon_slice"] for case_id in gaps) == {"2.1": 13, "2.2": 18, "2.3": 41}

    coverage = yaml.safe_load((ROOT / "contracts/coverage_map.yaml").read_text(encoding="utf-8"))["entries"]
    expected_nodes = {
        "error_code:DEVICE_NOT_BOUND": "cargo:sw-core::session_manager::device_not_bound_synthetic_profile_returns_device_not_bound",
        "error_code:DRAIN_STALLED": "cargo:sw-core::drain_release_e2e::drain_timeout_marks_stalled_and_release_reports_drain_stalled",
        "error_code:EMPTY_RESPONSE": "cargo:sw-cli::client_retry::whitespace_line_empty",
        "error_code:EXCEPTION": "cargo:sw-daemon::framing::framing_byte_table",
        "error_code:FLUSHED_BY_SHUTDOWN": "cargo:sw-daemon::handoff_stop::handoff_frozen_rejects_mutating_methods",
        "error_code:METHOD_NOT_FOUND": "cargo:sw-daemon::dispatch::unknown_method_not_found",
        "error_code:REMOTE_NOT_SUPPORTED": "cargo:sw-cli::mgmt_daemon::start_endpoint_rejected_posix",
        "error_code:SAMPLE_READ_FAILED": "cargo:sw-cli::profile_checks::sample_read_failure_returns_error_code_without_cli_failure",
        "error_code:TIMEOUT": "cargo:sw-cli::client_retry::etimedout_is_timeout",
        "last_error:TRANSPORT_STALL": "cargo:sw-core::transport_stall_wiring_p7::reprobe_zero_rx_after_30s_flips_to_transport_stall_once",
        "last_error:LOGIN_USER_REQUIRED": "cargo:sw-core::login_fsm_full::login_user_env_missing_does_not_fall_back_to_profile_username",
        "last_error:REBOOTING": "cargo:sw-core::session_core_lease::wp61_core_trusted_rpc_reboot_promotes_with_source",
        "last_error:RELEASED": "cargo:sw-core::drain_release_e2e::release_waits_for_receipt_and_aborts_partial_in_flight",
    }
    for contract, nodeid in expected_nodes.items():
        assert coverage[contract]["nodeids"] == [nodeid], (contract, coverage[contract])


def test_prior_slice_gate_verdicts_import_pass_fail_and_missing(tmp_path):
    """OD-88：三個先前 gate 有逐案證據才沿用 PASS／FAIL；缺列或 GAP 維持 EVIDENCE_MISSING。"""
    fixture = _prior_slice_fixture(tmp_path)
    assert fixture["daemon_sha"] == fixture["prior_daemon_sha"]
    assert fixture["slice23_debug_daemon_sha"] != fixture["daemon_sha"]
    output_path = tmp_path / "coverage.json"
    result = _run(*_prior_slice_command(fixture, output_path, fixture["ids"]))
    assert result.returncode == 1, result.stdout + result.stderr
    assert output_path.is_file(), result.stdout + result.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["case_verdicts"] == {
        "s21-pass": "PASS", "s21-fail": "FAIL", "s21-absent": "EVIDENCE_MISSING", "s21-owner": "BLOCKED-OWNER",
        "s22-pass": "PASS", "s22-absent": "EVIDENCE_MISSING", "s22-invalid": "EVIDENCE_MISSING",
        "s23-pass": "PASS", "s23-gap": "EVIDENCE_MISSING", "s23-invalid": "EVIDENCE_MISSING",
    }
    assert report["prior_sources"]["2.1"]["accepted"] is True
    assert report["prior_sources"]["2.2"]["accepted"] is True
    assert report["prior_sources"]["2.3"]["accepted"] is True
    assert report["prior_sources"]["2.3"]["daemon_sha256"] == fixture["daemon_sha"]
    assert report["prior_sources"]["2.3"]["release_daemon_sha256"] == fixture["daemon_sha"]
    assert report["prior_sources"]["2.3"]["daemon_sha256"] != fixture["slice23_debug_daemon_sha"]
    rendered = fixture["cc"].render_gate_report(report, [], {"run_id": "prior-slice-fixture"})
    assert "2.3 的黑箱使用 debug daemon" in rendered
    assert "2.4 的候選是 release bundle" in rendered
    assert report["prior_sources"]["2.1"]["evidence_sha256"]["report.json"] == hashlib.sha256(
        (fixture["slice21"] / "report.json").read_bytes()
    ).hexdigest()
    assert report["prior_sources"]["2.3"]["evidence_sha256"]["coverage.json"] == hashlib.sha256(
        (fixture["slice23"] / "coverage.json").read_bytes()
    ).hexdigest()

    # OD-88：同一案例出現在 slice 2.1 與 P7 時，後匯入的 PASS 不得覆蓋權威 FAIL。
    overlap_root = tmp_path / "overlap"
    overlap_root.mkdir()
    overlap = _prior_slice_fixture(overlap_root)
    coverage_path = overlap["slice23"] / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["rows"].append({"case_id": "s21-fail", "status": "PASS", "detail": "P7 pass"})
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    overlap_output = tmp_path / "overlap-coverage.json"
    overlap_result = _run(*_prior_slice_command(overlap, overlap_output, ["case:s21-fail"]))
    assert overlap_result.returncode == 1, overlap_result.stdout + overlap_result.stderr
    overlap_report = json.loads(overlap_output.read_text(encoding="utf-8"))
    assert overlap_report["case_verdicts"]["s21-fail"] == "FAIL", overlap_report["case_verdicts"]


def test_prior_slice_missing_source_is_gap_not_identity_error(tmp_path):
    """整份先前 gate 證據缺失時列缺漏，不混報為來源身分不符。"""
    fixture = _prior_slice_fixture(tmp_path)
    (fixture["slice21"] / "report.json").unlink()
    (fixture["slice21"] / "case_verdicts.json").unlink()
    output_path = tmp_path / "coverage.json"
    result = _run(*_prior_slice_command(fixture, output_path, ["case:s21-pass"]))
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["case_verdicts"]["s21-pass"] == "EVIDENCE_MISSING"
    assert not report["identity_errors"]
    assert report["prior_sources"]["2.1"]["accepted"] is False

    anchor_root = tmp_path / "missing-anchor"
    anchor_root.mkdir()
    missing_anchor = _prior_slice_fixture(anchor_root)
    anchor_output = tmp_path / "missing-anchor-coverage.json"
    anchor_args = _prior_slice_command(missing_anchor, anchor_output, ["case:s21-pass", "case:s22-pass"])
    anchor_arg = anchor_args.index("--slice23-run")
    del anchor_args[anchor_arg:anchor_arg + 2]
    anchor_result = _run(*anchor_args)
    assert anchor_result.returncode == 1, anchor_result.stdout + anchor_result.stderr
    anchor_report = json.loads(anchor_output.read_text(encoding="utf-8"))
    assert anchor_report["case_verdicts"] == {
        "s21-pass": "EVIDENCE_MISSING", "s22-pass": "EVIDENCE_MISSING",
    }
    assert not anchor_report["identity_errors"]


@pytest.mark.parametrize("mismatch", [
    "slice21_revision", "slice21_daemon", "slice22_revision", "slice22_daemon", "slice23_revision", "slice23_daemon",
])
def test_prior_slice_identity_mismatch_is_reported_and_rejected(tmp_path, mismatch):
    """來源 HEAD 或 daemon SHA 不符時，該來源不得覆蓋靜態缺口，且須列 identity error。"""
    fixture = _prior_slice_fixture(tmp_path)
    if mismatch.startswith("slice21"):
        report_path = fixture["slice21"] / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        field = "git_revision" if mismatch.endswith("revision") else "identity"
        if field == "identity":
            report["identity"]["daemon_sha256"] = "0" * 64
        else:
            report[field] = "b" * 40
        report_path.write_text(json.dumps(report), encoding="utf-8")
        selected = ["case:s21-pass"]
    elif mismatch.startswith("slice22"):
        report_path = fixture["slice22"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        field = "git_revision" if mismatch.endswith("revision") else "daemon_sha256"
        report["identity"][field] = "b" * 40 if field == "git_revision" else "0" * 64
        report_path.write_text(json.dumps(report), encoding="utf-8")
        selected = ["case:s22-pass"]
    else:
        report_path = fixture["slice23"] / "results.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if mismatch.endswith("revision"):
            report["git_head"] = "b" * 40
        else:
            report["prior_slice_artifacts"]["serialwrapd_release"] = "0" * 64
        report_path.write_text(json.dumps(report), encoding="utf-8")
        selected = ["case:s23-pass"]

    output_path = tmp_path / "coverage.json"
    result = _run(*_prior_slice_command(fixture, output_path, selected))
    assert result.returncode == 1, result.stdout + result.stderr
    output = json.loads(output_path.read_text(encoding="utf-8"))
    source_key = mismatch.split("_", 1)[0]
    case_id = {"slice21": "s21-pass", "slice22": "s22-pass", "slice23": "s23-pass"}[source_key]
    assert output["case_verdicts"][case_id] == "EVIDENCE_MISSING"
    assert output["identity_errors"]
    source = {"slice21": "2.1", "slice22": "2.2", "slice23": "2.3"}[source_key]
    assert output["prior_sources"][source]["accepted"] is False


def test_stage2_contract_ids_have_locked_rust_coverage_not_stage1_python():
    """OD-88：P0～P7 契約須對應鎖定的 Rust 節點，不得沿用 Stage 1 Python 節點。"""
    cc = _module()
    universe = cc.universe(
        ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
        ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml",
    )
    contract_ids = {
        contract_id: item for contract_id, item in universe.items()
        if not contract_id.startswith("case:")
    }
    stage2_targets = {
        contract_id: item for contract_id, item in contract_ids.items()
        if item.get("owner_phase") in {f"P{phase}" for phase in range(8)}
        # OD-88 的 269 個靜態缺口含 DF-26；保留其既有 P8 phase 歸屬。
        or contract_id == "bc:DF-26"
    }
    # 0.5.1 依 OD-111 新增的契約 ID 以 _V051_ADDED_CONTRACT_IDS 逐項 add-only 計入；269 不變（rulings/v051.md 登錄規則第 4 點）。
    assert len(stage2_targets) == 269 + len(_V051_ADDED_CONTRACT_IDS), f"OD-88 靜態 universe 數量改變：{len(stage2_targets)}"
    assert _V051_ADDED_CONTRACT_IDS <= set(stage2_targets), sorted(_V051_ADDED_CONTRACT_IDS - set(stage2_targets))
    missing = sorted(
        contract_id for contract_id, item in stage2_targets.items()
        if not item.get("nodeids") and not cc._is_owner_blocked(item)
    )
    assert not missing, f"未映射的 P0～P7 契約 ID：{missing}"
    owner_blocked_missing = sorted(
        contract_id for contract_id, item in stage2_targets.items()
        if not item.get("nodeids") and cc._is_owner_blocked(item)
    )
    assert owner_blocked_missing == ["bc:SC-26"], (
        f"未映射項目須逐一維持 BLOCKED-OWNER：{owner_blocked_missing}"
    )

    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    sets = lock.get("sets", {})
    locked_nodeids = {
        nodeid
        for platforms in sets.values()
        if isinstance(platforms, dict)
        for rows in platforms.values()
        if isinstance(rows, dict)
        for nodeid in rows.get("nodeids", [])
        if isinstance(nodeid, str)
    }
    stage1_python_sets = {"s1_pytest_full", "s1a_blackbox", "s1a_delegation", "s1b_20groups"}
    stage1_python_nodeids = {
        nodeid
        for set_name in stage1_python_sets
        for rows in sets.get(set_name, {}).values()
        if isinstance(rows, dict)
        for nodeid in rows.get("nodeids", [])
        if isinstance(nodeid, str)
    }
    map_doc = cc._read_yaml(ROOT / "contracts/coverage_map.yaml")
    parametrized = map_doc.get("parametrized", {})
    od99_ab_nodes = {
        nodeid
        for row in (map_doc.get("od99_ab_nodeids") or {}).values()
        if isinstance(row, dict)
        for nodeid in row.get("nodeids", [])
        if isinstance(nodeid, str)
    }
    # OD-105 分類 3：此 Windows COM 節點以 S24 凍結 acceptance case 承擔，
    # 不屬 Linux L1 required set；只核准這個與 case lock 精確綁定的 nodeid。
    case_only_windows_nodes = {
        "tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown":
            "case:s24-ho-windows-com",
    }
    case_lock = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    for nodeid, case_id in case_only_windows_nodes.items():
        case_row = universe.get(case_id)
        case_key = case_id.removeprefix("case:")
        locked_case = case_lock.get("case_attrs", {}).get(case_key)
        assert case_row is not None, f"OD-105 承擔 case 不存在：{case_id}"
        assert case_key in case_lock.get("sets", {}).get("slice_2.4", []), f"OD-105 case 未列入 S24 凍結集合：{case_id}"
        assert locked_case is not None, f"OD-105 承擔 case 未凍結：{case_id}"
        assert locked_case.get("necessity") == "required"
        assert locked_case.get("layer") == "L1.5"
        assert locked_case.get("platforms") == ["windows"]
        assert case_row.get("kind") == "case"
        assert case_row.get("status") == "L1.5"
        assert case_row.get("nodeids") == [nodeid], f"OD-105 case/nodeid 對應改變：{case_id}"
        assert case_row.get("owner_phase") == "P8"
        assert case_row.get("platforms") == ["windows"]
        assert "windows_com" in case_row.get("blocked_caps", [])

    referenced_rust_nodes = set()
    unlocked = set()
    for item in stage2_targets.values():
        for nodeid in item.get("nodeids", []):
            if nodeid in od99_ab_nodes:
                continue
            params = parametrized.get(nodeid)
            expanded = [f"{nodeid}[{param}]" for param in params] if params is not None else [nodeid]
            for executable_nodeid in expanded:
                if executable_nodeid in case_only_windows_nodes:
                    continue
                if executable_nodeid in locked_nodeids:
                    referenced_rust_nodes.add(executable_nodeid)
                else:
                    unlocked.add(executable_nodeid)
    unlocked = sorted(unlocked)
    stage1_python = sorted((referenced_rust_nodes & stage1_python_nodeids) - od99_ab_nodes)
    assert not unlocked, f"coverage_map 引用了未登記的 nodeid：{unlocked}"
    assert not stage1_python, f"coverage_map 沿用未經 OD-99 核准的 Stage 1 Python nodeid：{stage1_python}"

    capability_nodeids = {
        nodeid for nodeid in referenced_rust_nodes
        if nodeid.startswith("cargo:") and nodeid.rsplit("::", 1)[-1].startswith("cap_")
    }
    capability_set_nodeids = set()
    non_capability_memberships = set()
    wrong_capability_selectors = set()
    malformed_capability_nodeids = set()
    for nodeid in capability_nodeids:
        test_name = nodeid.rsplit("::", 1)[-1]
        capability_prefix, separator, _test_suffix = test_name.partition("__")
        if not separator or not capability_prefix.startswith("cap_"):
            malformed_capability_nodeids.add(nodeid)
            continue
        required_selector_arg = f"{capability_prefix}__"
        for platforms in sets.values():
            if not isinstance(platforms, dict):
                continue
            for rows in platforms.values():
                if not isinstance(rows, dict) or nodeid not in rows.get("nodeids", []):
                    continue
                argv = rows.get("selector", {}).get("argv", [])
                if "--ignored" not in argv:
                    non_capability_memberships.add(nodeid)
                elif required_selector_arg not in argv:
                    wrong_capability_selectors.add(nodeid)
                else:
                    capability_set_nodeids.add(nodeid)
    missing_capability_sets = sorted(capability_nodeids - capability_set_nodeids)
    assert not non_capability_memberships, (
        f"能力 nodeid 混入一般鎖集合：{sorted(non_capability_memberships)}"
    )
    assert not wrong_capability_selectors, (
        f"能力 nodeid 登記在錯誤的 --ignored selector：{sorted(wrong_capability_selectors)}"
    )
    assert not malformed_capability_nodeids, (
        f"能力 nodeid 格式錯誤：{sorted(malformed_capability_nodeids)}"
    )
    assert not missing_capability_sets, (
        f"能力 nodeid 未登記在 --ignored 能力集合：{missing_capability_sets}"
    )
