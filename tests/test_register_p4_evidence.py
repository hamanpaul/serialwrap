from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml


pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "register_p4_evidence.py"


def _head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _make_inputs(tmp_path: Path, *, case_id: str = "sc18-example") -> dict[str, Path]:
    source_root = tmp_path / "source"
    test_file = source_root / "tests" / "contract_blackbox" / "test_registration.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'import pytest\n\n@pytest.mark.case("sc18-example")\ndef test_case():\n    pass\n',
        encoding="utf-8",
    )

    case_map = tmp_path / "p4_case_map.yaml"
    case_map.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": case_id,
                        "layer": "L1",
                        "platforms": ["linux"],
                        "artifact": "rust-cli-bundle",
                        "tests": ["tests/contract_blackbox/test_registration.py::test_case"],
                        "skip_registered": {},
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    l1_dir = tmp_path / "l1"
    bundle = tmp_path / "bundle" / "bin" / "serialwrap"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"actual-rust-cli-bundle")
    manifest_path = bundle.parent.parent / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stage": "rust-1b-dev",
                "git_revision": _head(),
                "target": "x86_64-unknown-linux-gnu",
                "components": {
                    "cli": {
                        "implementation": "rust",
                        "git_revision": _head(),
                        "path": "bin/serialwrap",
                        "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    python_cli = tmp_path / "serialwrap.py"
    python_cli.write_text("# python baseline\n", encoding="utf-8")
    nodeid = "tests/contract_blackbox/test_registration.py::test_case"
    junit = _write_l1_report(l1_dir, nodeid, bundle, runner="pytest")
    return {
        "case_map": case_map,
        "source_root": source_root,
        "l1_dir": l1_dir,
        "junit": junit,
        "python_junit": junit,
        "out_dir": tmp_path / "out",
        "bundle": bundle,
        "python_cli": python_cli,
    }


def _write_l1_report(
    l1_dir: Path,
    nodeid: str,
    bundle: Path | None = None,
    *,
    runner: str = "pytest",
) -> Path:
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    set_name = "s1b_20groups" if runner == "pytest" else "s1b_cargo"
    selector = lock["sets"][set_name]["linux"]["selector"]
    required = list(lock["sets"][set_name]["linux"]["nodeids"])
    collected = list(dict.fromkeys([*required, nodeid if runner == "pytest" else f"cargo:{nodeid}"]))
    results = [
        {
            "nodeid": current,
            "outcome": "passed",
            "skip_kind": None,
            "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        }
        for current in collected
    ]
    report: dict[str, object] = {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": runner,
        "platform": "linux",
        "argv": (["pytest", *selector["argv"], "--junitxml=fixture.xml"] if runner == "pytest" else selector["argv"]),
        "markexpr": selector["markexpr"],
        "counts": {"all": {"passed": len(results), "failed": 0, "skipped": 0}},
        "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [],
        "core_portable_platform_skips": [],
        "collected": collected,
        "deselected": [],
        "results": results,
        "exit_code": 0,
        "finished": True,
        "deployed_content": None,
    }
    if bundle is not None:
        manifest_path = bundle.parent.parent / "release-manifest.json"
        cli_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        deployed = {
            "schema_version": "deployed-content.v1",
            "error": None,
            "bundle_tree_sha256": "a" * 64,
            "release_manifest_sha256": manifest_sha256,
            "cli_sha256": cli_sha256,
        }
        report["deployed_content"] = {"start": deployed, "end": dict(deployed)}

    report_dir = l1_dir / "linux" / ("20groups" if runner == "pytest" else "cargo")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "resource-report.json").write_text(json.dumps(report), encoding="utf-8")

    junit_path = report_dir / "junit.xml"
    if runner == "pytest":
        suite = ET.Element(
            "testsuite",
            tests=str(len(collected)),
            failures="0",
            errors="0",
            skipped="0",
        )
        for index, current in enumerate(collected):
            if current == nodeid:
                classname = "tests.contract_blackbox.test_registration"
                name = "test_case"
            else:
                classname = "fixture.required"
                name = f"test_{index}"
            ET.SubElement(suite, "testcase", classname=classname, name=name)
        ET.ElementTree(suite).write(junit_path, encoding="utf-8", xml_declaration=True)
    return junit_path


def _run(tmp_path: Path, inputs: dict[str, Path], *, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(ROOT),
        "--case-map",
        str(inputs["case_map"]),
        "--source-root",
        str(inputs["source_root"]),
        "--l1-dir",
        str(inputs["l1_dir"]),
        "--out-dir",
        str(inputs["out_dir"]),
        "--linux-cli",
        str(inputs["bundle"]),
        "--python-cli",
        str(inputs["python_cli"]),
        "--linux-python-junit",
        str(inputs.get("python_junit", inputs["junit"])),
    ]
    if extra:
        argv.extend(extra)
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)


def test_registers_only_reported_passes_with_head_and_actual_artifact_hash(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))
    records = data["records"]
    rust = next(item for item in records if item["round"] == "rust")
    python = next(item for item in records if item["round"] == "python")
    assert rust["case_id"] == "sc18-example"
    assert rust["commit"] == _head()
    assert rust["platform"] == "linux"
    assert rust["result"] == "PASS"
    assert rust["artifact_path"] == str(inputs["bundle"])
    assert rust["artifact_sha256"] == hashlib.sha256(inputs["bundle"].read_bytes()).hexdigest()
    assert rust["test_id"] == "tests/contract_blackbox/test_registration.py::test_case"
    assert python["result"] == "PASS"
    assert python["artifact_path"] == str(inputs["python_cli"])
    assert python["artifact_sha256"] == hashlib.sha256(inputs["python_cli"].read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["missing", "failed", "junit-failed", "unfinished", "duplicate"])
def test_missing_or_nonpassing_test_never_gets_a_pass(tmp_path: Path, failure: str) -> None:
    inputs = _make_inputs(tmp_path)
    report_path = inputs["l1_dir"] / "linux" / "20groups" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    nodeid = "tests/contract_blackbox/test_registration.py::test_case"
    if failure == "missing":
        report["results"] = [row for row in report["results"] if row["nodeid"] != nodeid]
    elif failure == "failed":
        row = next(row for row in report["results"] if row["nodeid"] == nodeid)
        row["outcome"] = "failed"
        row["phases"]["call"] = "failed"
    elif failure == "junit-failed":
        junit = ET.parse(inputs["junit"])
        testcase = next(
            item
            for item in junit.getroot().findall("testcase")
            if item.get("classname") == "tests.contract_blackbox.test_registration"
            and item.get("name") == "test_case"
        )
        ET.SubElement(testcase, "failure", message="fixture failure")
        junit.getroot().set("failures", "1")
        junit.write(inputs["junit"], encoding="utf-8", xml_declaration=True)
    elif failure == "duplicate":
        report["collected"].append(report["collected"][0])
    else:
        report["finished"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode != 0
    data = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))
    rust = [item for item in data["records"] if item["round"] == "rust"]
    assert rust
    assert all(item["result"] == "FAIL" for item in rust)
    assert not any(item["result"] == "PASS" for item in rust)
    assert all(item["test_id"] == nodeid for item in rust)


def test_case_marker_must_match_the_frozen_case_id(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path, case_id="different-case")

    result = _run(tmp_path, inputs)

    assert result.returncode != 0
    data = json.loads((inputs["out_dir"] / "different-case.json").read_text(encoding="utf-8"))
    assert all(item["result"] == "FAIL" for item in data["records"])
    assert not any(item["result"] == "PASS" for item in data["records"])


def _make_class_test(inputs: dict[str, Path], *, class_marker: bool) -> str:
    nodeid = "tests/contract_blackbox/test_registration.py::TestSuite::test_case"
    class_decorator = '@pytest.mark.case("sc18-example")\n' if class_marker else ""
    source = (
        "import pytest\n\n"
        f"{class_decorator}class TestSuite:\n"
        "    def test_case(self):\n"
        "        pass\n\n"
        '    @pytest.mark.case("sc18-example")\n'
        "    def test_other(self):\n"
        "        pass\n"
    )
    (inputs["source_root"] / "tests/contract_blackbox/test_registration.py").write_text(source, encoding="utf-8")
    case_map = yaml.safe_load(inputs["case_map"].read_text(encoding="utf-8"))
    case_map["cases"][0]["tests"] = [nodeid]
    inputs["case_map"].write_text(yaml.safe_dump(case_map, sort_keys=False), encoding="utf-8")
    report_path = inputs["l1_dir"] / "linux" / "20groups" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    old_nodeid = "tests/contract_blackbox/test_registration.py::test_case"
    report["collected"] = [nodeid if item == old_nodeid else item for item in report["collected"]]
    for row in report["results"]:
        if row["nodeid"] == old_nodeid:
            row["nodeid"] = nodeid
    report_path.write_text(json.dumps(report), encoding="utf-8")
    suite = ET.Element(
        "testsuite",
        tests=str(len(report["collected"])),
        failures="0",
        errors="0",
        skipped="0",
    )
    ET.SubElement(
        suite,
        "testcase",
        classname="tests.contract_blackbox.test_registration.TestSuite",
        name="test_case",
    )
    for index, _ in enumerate(report["collected"][1:], 1):
        ET.SubElement(suite, "testcase", classname="fixture.required", name=f"test_{index}")
    ET.ElementTree(suite).write(inputs["junit"], encoding="utf-8", xml_declaration=True)
    return nodeid


def test_class_junit_node_and_class_case_marker_are_resolved(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    nodeid = _make_class_test(inputs, class_marker=True)

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    assert all(record["test_id"] == nodeid for record in records)
    assert all(record["result"] == "PASS" for record in records)


def test_case_marker_on_sibling_test_does_not_mark_target(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    _make_class_test(inputs, class_marker=False)

    result = _run(tmp_path, inputs)

    assert result.returncode != 0
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    assert records
    assert all(record["result"] == "FAIL" for record in records)
    assert not any(record["result"] == "PASS" for record in records)


def test_later_pass_does_not_overwrite_same_head_fail(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    report_path = inputs["l1_dir"] / "linux" / "20groups" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["results"][0]["outcome"] = "failed"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    first = _run(tmp_path, inputs)
    assert first.returncode != 0

    report["results"][0]["outcome"] = "passed"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    second = _run(tmp_path, inputs)

    assert second.returncode != 0
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    rust = [record for record in records if record["round"] == "rust"]
    assert {record["result"] for record in rust} == {"FAIL", "PASS"}


def test_malformed_existing_case_file_is_not_rewritten(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    evidence_path = inputs["out_dir"] / "sc18-example.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"case_id":"other-case","records":[null]}\n'
    evidence_path.write_text(original, encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode == 2
    assert evidence_path.read_text(encoding="utf-8") == original


def test_existing_case_metadata_and_records_are_preserved(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    evidence_path = inputs["out_dir"] / "sc18-example.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    original_record = {"case_id": "sc18-example", "round": "python", "result": "SKIP"}
    evidence_path.write_text(
        json.dumps({"case_id": "sc18-example", "prior_metadata": {"owner": "keep"}, "records": [original_record]}),
        encoding="utf-8",
    )

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert data["prior_metadata"] == {"owner": "keep"}
    assert original_record in data["records"]


def test_registered_python_skip_with_absent_node_emits_no_python_pass(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    case_map = yaml.safe_load(inputs["case_map"].read_text(encoding="utf-8"))
    case_map["cases"][0]["skip_registered"] = {"python": "Rust-only case"}
    inputs["case_map"].write_text(yaml.safe_dump(case_map, sort_keys=False), encoding="utf-8")
    python_junit = tmp_path / "python-empty-junit.xml"
    suite = ET.Element("testsuite", tests="0", failures="0", errors="0", skipped="0")
    ET.ElementTree(suite).write(python_junit, encoding="utf-8", xml_declaration=True)
    inputs["python_junit"] = python_junit

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    assert [record["round"] for record in records] == ["rust"]


def test_cargo_case_uses_the_matching_test_executable_sha256(tmp_path: Path) -> None:
    case_map = tmp_path / "p4_case_map.yaml"
    case_map.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": "sc20-example",
                        "layer": "L1",
                        "platforms": ["linux"],
                        "artifact": "sw-pal-test-bin",
                        "tests": ["sw-cli::mgmt_remote::test_remote"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    l1_dir = tmp_path / "l1"
    report_dir = l1_dir / "linux" / "cargo"
    report_dir.mkdir(parents=True)
    nodeid = "cargo:sw-cli::mgmt_remote::test_remote"
    _write_l1_report(l1_dir, "sw-cli::mgmt_remote::test_remote", runner="cargo")

    cargo_target = tmp_path / "cargo-target"
    deps = cargo_target / "debug" / "deps"
    deps.mkdir(parents=True)
    test_exe = deps / "mgmt_remote-abc123"
    test_exe.write_text(
        "#!/usr/bin/env python3\nprint('test_remote: test')\n",
        encoding="utf-8",
    )
    test_exe.chmod(0o755)
    out_dir = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(ROOT),
            "--case-map",
            str(case_map),
            "--l1-dir",
            str(l1_dir),
            "--cargo-target-dir",
            str(cargo_target),
            "--out-dir",
            str(out_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads((out_dir / "sc20-example.json").read_text(encoding="utf-8"))
    rust = next(item for item in data["records"] if item["round"] == "rust")
    assert rust["result"] == "PASS"
    assert rust["artifact_path"] == str(test_exe)
    assert rust["artifact_sha256"] == hashlib.sha256(test_exe.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ["wrong-selector", "nonzero-exit", "wrong-cli-digest"])
def test_invalid_l1_or_bundle_identity_never_registers_a_rust_pass(tmp_path: Path, mutation: str) -> None:
    inputs = _make_inputs(tmp_path)
    report_path = inputs["l1_dir"] / "linux" / "20groups" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest_path = inputs["bundle"].parent.parent / "release-manifest.json"
    if mutation == "wrong-selector":
        report["argv"].insert(-1, "-q")
    elif mutation == "nonzero-exit":
        report["exit_code"] = 1
    else:
        report["deployed_content"]["end"]["cli_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode != 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    rust = [record for record in records if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust)
    assert not any(record["result"] == "PASS" for record in rust)
    assert manifest_path.is_file()


def test_bundle_revision_must_equal_tested_head(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    manifest_path = inputs["bundle"].parent.parent / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["git_revision"] = "f" * 40
    manifest["components"]["cli"]["git_revision"] = "f" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = inputs["l1_dir"] / "linux" / "20groups" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    report["deployed_content"]["start"]["release_manifest_sha256"] = digest
    report["deployed_content"]["end"]["release_manifest_sha256"] = digest
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode != 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    rust = [record for record in records if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust)


def test_generic_registrar_does_not_create_owner_layer_evidence(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path, case_id="owner-l2-example")
    case_map = yaml.safe_load(inputs["case_map"].read_text(encoding="utf-8"))
    case_map["cases"][0]["layer"] = "L2"
    case_map["cases"][0]["evidence_status"] = "BLOCKED-OWNER"
    inputs["case_map"].write_text(yaml.safe_dump(case_map, sort_keys=False), encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (inputs["out_dir"] / "owner-l2-example.json").exists()


@pytest.mark.parametrize("case_id", ["win-task-72h", "p1-rst-daemon", "rm-live-e2e"])
def test_judge_spike2_and_owner_cases_are_not_rewritten(tmp_path: Path, case_id: str) -> None:
    inputs = _make_inputs(tmp_path, case_id=case_id)
    evidence_path = inputs["out_dir"] / f"{case_id}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('{"owner":"keep"}\n', encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    assert evidence_path.read_text(encoding="utf-8") == '{"owner":"keep"}\n'
