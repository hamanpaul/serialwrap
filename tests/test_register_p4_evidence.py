from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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
        'import pytest\n\npytestmark = [pytest.mark.core_portable]\n\n@pytest.mark.case("sc18-example")\ndef test_case():\n    pass\n',
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
    platform: str = "linux",
) -> Path:
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    set_name = "s1b_20groups" if runner == "pytest" else "s1b_cargo"
    selector = lock["sets"][set_name][platform]["selector"]
    required = list(lock["sets"][set_name][platform]["nodeids"])
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
        "platform": platform,
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

    report_dir = l1_dir / platform / ("20groups" if runner == "pytest" else "cargo")
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


def _run(tmp_path: Path, inputs: dict[str, Path], *, extra: list[str] | None = None, repo_root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(repo_root),
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
        "pytestmark = [pytest.mark.core_portable]\n\n"
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


FAKE_CARGO = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["FAKE_CARGO_ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
key = "windows" if "--target" in sys.argv else "linux"
table = json.loads(Path(os.environ["FAKE_CARGO_TABLE"]).read_text(encoding="utf-8"))
for artifact in table.get(key, []):
    print(json.dumps({"reason": "compiler-artifact", **artifact}))
print(json.dumps({"reason": "build-finished", "success": True}))
sys.exit(int(os.environ.get("FAKE_CARGO_RC", "0")))
"""

FAKE_RUNNER = '#!/bin/sh\nexec "$@"\n'


def _fake_test_exe(path: Path, test_names: list[str]) -> Path:
    """可執行的假測試 executable：`--list --format terse` 只列出 test_names（與 cargo test 二進位行為相同的最小替身）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\nimport sys\nNAMES = %r\nif '--list' in sys.argv:\n"
        "    for name in NAMES:\n        print(name + ': test')\n" % (test_names,),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _artifact(package: str, target: str, executable: Path) -> dict:
    return {
        "package_id": f"path+file:///repo/crates/{package}#0.1.0",
        "target": {"name": target, "kind": ["test"]},
        "profile": {"test": True},
        "executable": str(executable),
    }


def _fake_cargo(tmp_path: Path, table: dict, *, rc: int = 0) -> tuple[dict[str, str], Path, Path]:
    """建立假 cargo（置於 PATH 最前）與 runner；回傳 (環境變數, argv 紀錄檔, runner 路徑)。"""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    cargo = fake_bin / "cargo"
    cargo.write_text(FAKE_CARGO, encoding="utf-8")
    cargo.chmod(0o755)
    runner = fake_bin / "win-runner"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")
    runner.chmod(0o755)
    table_path = tmp_path / "cargo-table.json"
    table_path.write_text(json.dumps(table), encoding="utf-8")
    argv_log = tmp_path / "cargo-argv.json"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_CARGO_TABLE": str(table_path),
        "FAKE_CARGO_ARGV_LOG": str(argv_log),
        "FAKE_CARGO_RC": str(rc),
    }
    return env, argv_log, runner


def _register_cargo(
    tmp_path: Path,
    case_map_cases: list[dict],
    l1_dir: Path,
    env: dict[str, str],
    runner: Path,
    out_dir: Path,
) -> subprocess.CompletedProcess[str]:
    case_map = tmp_path / "p4_case_map.yaml"
    case_map.write_text(yaml.safe_dump({"schema_version": 1, "cases": case_map_cases}, sort_keys=False), encoding="utf-8")
    return subprocess.run(
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
            str(tmp_path / "cargo-target"),
            "--windows-runner",
            str(runner),
            "--out-dir",
            str(out_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _cargo_records_of(out_dir: Path, case_id: str) -> list[dict]:
    return json.loads((out_dir / f"{case_id}.json").read_text(encoding="utf-8"))["records"]


def test_cargo_case_uses_the_matching_test_executable_sha256(tmp_path: Path) -> None:
    """官方 selector（`cargo test --no-run --message-format=json --workspace`）建出的執行檔，依 package＋target 唯一對應。

    同名 target 的他套件執行檔（decoy）不得被選中；selector argv 必須逐字使用凍結鎖的 argv。
    """
    node = "sw-cli::cli_trace::trace_line_exact"
    trace_exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-aaa", ["trace_line_exact"])
    decoy_exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-decoy", ["trace_line_exact"])
    env, argv_log, runner = _fake_cargo(tmp_path, {"linux": [
        _artifact("sw-other", "cli_trace", decoy_exe),
        _artifact("sw-cli", "cli_trace", trace_exe),
    ]})
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo")
    out_dir = tmp_path / "out"
    case = {"case_id": "sc20-example", "layer": "L1", "platforms": ["linux"], "artifact": "sw-pal-test-bin", "tests": [node]}

    result = _register_cargo(tmp_path, [case], l1_dir, env, runner, out_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert [record["result"] for record in rust] == ["PASS"], rust
    assert rust[0]["artifact_path"] == str(trace_exe.resolve())
    assert rust[0]["artifact_sha256"] == hashlib.sha256(trace_exe.read_bytes()).hexdigest()
    argv = json.loads(argv_log.read_text(encoding="utf-8"))
    selector = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    selector_argv = selector["sets"]["s1b_cargo"]["linux"]["selector"]["argv"]
    assert argv[:3] == ["test", "--no-run", "--message-format=json"]
    assert argv[-len(selector_argv):] == selector_argv
    assert "--target" not in argv


def test_cargo_executable_must_be_unique_for_crate_and_target(tmp_path: Path) -> None:
    """同一 package＋target 出現兩個 test 執行檔即無法唯一對應 → FAIL，不得以先到者放行。"""
    node = "sw-cli::cli_trace::trace_line_exact"
    first = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    second = _fake_test_exe(tmp_path / "exe" / "cli_trace-2", ["trace_line_exact"])
    env, _, runner = _fake_cargo(tmp_path, {"linux": [
        _artifact("sw-cli", "cli_trace", first),
        _artifact("sw-cli", "cli_trace", second),
    ]})
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo")
    out_dir = tmp_path / "out"
    case = {"case_id": "sc20-example", "layer": "L1", "platforms": ["linux"], "artifact": "sw-pal-test-bin", "tests": [node]}

    result = _register_cargo(tmp_path, [case], l1_dir, env, runner, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust), rust


def test_cargo_no_run_failure_fails_closed(tmp_path: Path) -> None:
    node = "sw-cli::cli_trace::trace_line_exact"
    exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    env, _, runner = _fake_cargo(tmp_path, {"linux": [_artifact("sw-cli", "cli_trace", exe)]}, rc=1)
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo")
    out_dir = tmp_path / "out"
    case = {"case_id": "sc20-example", "layer": "L1", "platforms": ["linux"], "artifact": "sw-pal-test-bin", "tests": [node]}

    result = _register_cargo(tmp_path, [case], l1_dir, env, runner, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert rust and not any(record["result"] == "PASS" for record in rust), rust


def test_cargo_nodes_apply_per_platform_only_for_bc05_style_case(tmp_path: Path) -> None:
    """bc05 型態：POSIX 與 Windows 各自只有一個平台適用的 cfg 節點；不適用者不產生記錄，各平台至少一筆 PASS。"""
    posix_node = "sw-pal::process_tunnel::posix_terminate_kills_whole_group_including_grandchild"
    windows_node = "sw-pal::process_tunnel::windows_terminate_tree_kills_child_and_grandchild"
    posix_exe = _fake_test_exe(tmp_path / "exe" / "process_tunnel-posix", ["posix_terminate_kills_whole_group_including_grandchild"])
    windows_exe = _fake_test_exe(
        tmp_path / "exe" / "process_tunnel-windows.exe", ["windows_terminate_tree_kills_child_and_grandchild"]
    )
    env, _, runner = _fake_cargo(tmp_path, {
        "linux": [_artifact("sw-pal", "process_tunnel", posix_exe)],
        "windows": [_artifact("sw-pal", "process_tunnel", windows_exe)],
    })
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, posix_node, runner="cargo", platform="linux")
    _write_l1_report(l1_dir, windows_node, runner="cargo", platform="windows")
    out_dir = tmp_path / "out"
    case = {
        "case_id": "bc05-process-lifecycle-stop-restart", "layer": "L1",
        "platforms": ["linux", "windows"], "artifact": "sw-pal-test-bin",
        "tests": [posix_node, windows_node],
    }

    result = _register_cargo(tmp_path, [case], l1_dir, env, runner, out_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    records = _cargo_records_of(out_dir, "bc05-process-lifecycle-stop-restart")
    by_platform = {
        platform: sorted((record["test_id"], record["result"]) for record in records if record["platform"] == platform)
        for platform in ("linux", "windows")
    }
    assert by_platform["linux"] == [(f"cargo:{posix_node}", "PASS")], records
    assert by_platform["windows"] == [(f"cargo:{windows_node}", "PASS")], records
    assert not any("必要 Cargo 節點缺失" in str(record.get("reason")) for record in records)


def test_cargo_node_in_neither_platform_lock_fails_on_every_case_platform(tmp_path: Path) -> None:
    """兩個平台的 s1b_cargo 鎖都不含的節點一律 FAIL（每個 case 平台各一筆），不呼叫 cargo。"""
    node = "sw-cli::p4_case_placeholders::df07_bad_config_yaml"
    env, argv_log, runner = _fake_cargo(tmp_path, {})
    l1_dir = tmp_path / "l1"
    out_dir = tmp_path / "out"
    case = {"case_id": "df07-example", "layer": "L1", "platforms": ["linux", "windows"], "artifact": "sw-pal-test-bin", "tests": [node]}

    result = _register_cargo(tmp_path, [case], l1_dir, env, runner, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    records = _cargo_records_of(out_dir, "df07-example")
    assert sorted(record["platform"] for record in records) == ["linux", "windows"]
    assert all(record["result"] == "FAIL" and "凍結" in str(record.get("reason")) for record in records), records
    assert not argv_log.exists()


def _pytest_fixture_with_marker(tmp_path: Path, marker: str | None) -> dict[str, Path]:
    """以指定 pytestmark（None 表示無任何平台標記）重建 `_make_inputs` 的來源檔，並同步 L1 報告。"""
    inputs = _make_inputs(tmp_path)
    test_file = inputs["source_root"] / "tests" / "contract_blackbox" / "test_registration.py"
    body = 'import pytest\n\n' + (f"pytestmark = [pytest.mark.{marker}]\n\n" if marker else "") + (
        '@pytest.mark.case("sc18-example")\ndef test_case():\n    pass\n'
    )
    test_file.write_text(body, encoding="utf-8")
    return inputs


def test_pytest_node_outside_platform_selector_is_not_registered_there(tmp_path: Path) -> None:
    """posix_only 節點：Linux 凍結 selector 收集、Windows 不收集 → Linux 記錄、Windows 不產生任何記錄也不執行。"""
    inputs = _pytest_fixture_with_marker(tmp_path, "posix_only")
    case_map = yaml.safe_load(inputs["case_map"].read_text(encoding="utf-8"))
    case_map["cases"][0]["platforms"] = ["linux", "windows"]
    inputs["case_map"].write_text(yaml.safe_dump(case_map, sort_keys=False), encoding="utf-8")

    result = _run(tmp_path, inputs)

    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    assert [(record["platform"], record["round"], record["result"]) for record in records if record["round"] == "rust"] == [
        ("linux", "rust", "PASS")
    ]
    assert not any(record["platform"] == "windows" for record in records), records


def test_pytest_node_collected_by_no_platform_selector_is_fail(tmp_path: Path) -> None:
    """兩個平台的凍結 selector（markexpr）都不收集的節點（無平台標記）一律 FAIL（同 sc20 的 test_daemon_restart_ab 型態）。"""
    inputs = _pytest_fixture_with_marker(tmp_path, None)

    result = _run(tmp_path, inputs)

    assert result.returncode != 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    rust = [record for record in records if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust), rust
    assert all("凍結" in str(record.get("reason")) for record in rust), rust


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


def test_dirty_worktree_is_refused_before_any_record(tmp_path: Path) -> None:
    """髒樹證據會被綁到 HEAD；登記前即須拒絕（rc=2、stderr 說明原因、不寫任何證據）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_id = ["-c", "user.email=register-test@example.invalid", "-c", "user.name=register-test"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", *git_id, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")

    inputs = _make_inputs(tmp_path)
    result = _run(tmp_path, inputs, repo_root=repo)

    assert result.returncode == 2, result.stderr
    assert "工作樹不乾淨" in result.stderr
    out_dir = inputs["out_dir"]
    assert not out_dir.exists() or not any(out_dir.glob("*.json"))


def _load_register():
    spec = importlib.util.spec_from_file_location("register_p4_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_pointer_dir_resolves_from_install_root_not_bin(tmp_path: Path) -> None:
    """Windows 指標 `dir`（plan 04 Task 17 的 `versions\\<V>`）相對安裝根（cli.parent.parent）解析，不得以 bin\\ 為基準。"""
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": "versions\\0.4.0"}), encoding="utf-8"
    )

    candidates, error = register._manifest_candidates(cli)

    assert error is None
    assert candidates[0] == root / "versions" / "0.4.0" / "release-manifest.json"
    assert root / "bin" / "versions" / "0.4.0" / "release-manifest.json" not in candidates
    assert root / "release-manifest.json" in candidates


def test_manifest_pointer_absolute_dir_is_used_as_is(tmp_path: Path) -> None:
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    absolute = tmp_path / "elsewhere" / "0.4.0"
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": str(absolute)}), encoding="utf-8"
    )

    candidates, error = register._manifest_candidates(cli)

    assert error is None
    assert candidates[0] == absolute / "release-manifest.json"


def test_manifest_pointer_unparseable_is_reported_not_ignored(tmp_path: Path) -> None:
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    (root / "bin" / "current-release.json").write_text("{not json", encoding="utf-8")

    candidates, error = register._manifest_candidates(cli)

    assert candidates == []
    assert error == "current-release.json 無法解析"


def _windows_bundle_layout(tmp_path: Path, commit: str, *, entry_bytes: bytes | None = None) -> tuple[Path, Path, Path]:
    """Windows dev bundle 佈局：`bin\\serialwrap.exe` 為 `versions\\<V>\\bin\\serialwrap.exe` 的 byte 複本（無 symlink）。

    回傳 (入口 bin\\serialwrap.exe, manifest 路徑, manifest 所指的 versions\\<V>\\bin\\serialwrap.exe)。
    """
    root = tmp_path / "bundle"
    version_dir = root / "versions" / "0.4.0"
    (version_dir / "bin").mkdir(parents=True)
    (root / "bin").mkdir(parents=True)
    payload = b"rust-cli-windows"
    versioned_cli = version_dir / "bin" / "serialwrap.exe"
    versioned_cli.write_bytes(payload)
    entry = root / "bin" / "serialwrap.exe"
    entry.write_bytes(payload if entry_bytes is None else entry_bytes)
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": "versions\\0.4.0"}), encoding="utf-8"
    )
    manifest = version_dir / "release-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "git_revision": commit,
        "components": {"cli": {
            "implementation": "rust", "git_revision": commit,
            "path": "bin/serialwrap.exe", "sha256": hashlib.sha256(payload).hexdigest(),
        }},
    }), encoding="utf-8")
    return entry, manifest, versioned_cli


def test_windows_stable_entry_copy_is_the_manifest_cli(tmp_path: Path) -> None:
    """register 的 `--windows-cli` 為 `bundle\\bin\\serialwrap.exe`（驅動腳本與 G2／G3 的約定）：
    與 manifest 所指 versions\\<V>\\bin\\serialwrap.exe 同 byte（sha256 相符）的穩定入口複本即為受測 artifact。"""
    register = _load_register()
    commit = _head()
    entry, manifest, _ = _windows_bundle_layout(tmp_path, commit)

    bundle, error = register._rust_manifest(entry, commit)

    assert error is None, error
    assert bundle is not None and bundle["path"] == manifest


def test_windows_stable_entry_with_different_bytes_is_rejected(tmp_path: Path) -> None:
    """入口複本內容與 manifest 的 cli sha256 不符即拒絕（複本放寬不得放寬內容比對）。"""
    register = _load_register()
    commit = _head()
    entry, _, _ = _windows_bundle_layout(tmp_path, commit, entry_bytes=b"tampered")

    bundle, error = register._rust_manifest(entry, commit)

    assert bundle is None
    assert error == "release manifest CLI sha256 與實際 artifact 不符"


def test_unrelated_copy_outside_bundle_bin_is_still_rejected(tmp_path: Path) -> None:
    """不在 bundle\\bin\\ 的同 byte 複本不得冒充 manifest 的 CLI（路徑比對維持）。"""
    register = _load_register()
    commit = _head()
    _, _, versioned_cli = _windows_bundle_layout(tmp_path, commit)
    # 置於版本目錄下（manifest 可被找到），但不是 bin\\ 入口：路徑比對必須拒絕
    elsewhere = versioned_cli.parent.parent / "copy" / "serialwrap.exe"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(versioned_cli.read_bytes())

    bundle, error = register._rust_manifest(elsewhere, commit)

    assert bundle is None
    assert error == "release manifest CLI path 與受測 artifact 不符"
