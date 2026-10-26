from __future__ import annotations

import hashlib
import importlib.util
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
TEST_NODE = "tests/contract_blackbox/test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_cases_lock_present"
LINUX_ONLY_NODE = "tests/contract_blackbox/test_cli_mgmt_remote.py::test_remote_sigint_cleans_spawned_tunnel"


def _head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


GIT_ID = ["-c", "user.email=register-test@example.invalid", "-c", "user.name=register-test"]


def _clean_repo(tmp_path: Path) -> Path:
    """每個測試自備的乾淨 git repo。登錄器的髒樹守門只檢查受測 repo；以獨立 repo 取代開發中的工作樹，
    使登錄結果不受同時進行中的其他變更影響（工作樹不乾淨時仍可重現）。"""
    repo = tmp_path / "clean-repo"
    if not (repo / ".git").is_dir():
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "README.md").write_text("clean fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", *GIT_ID, "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    return repo


def _repo_head(repo: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _make_inputs(
    tmp_path: Path, *, case_id: str = "sc18-example", nodeid: str = TEST_NODE
) -> dict[str, Path]:
    source_root = tmp_path / "source"
    module, test_name = nodeid.split("::", maxsplit=1)
    test_name = test_name.split("[", maxsplit=1)[0]
    test_file = source_root / module
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'import pytest\n\npytestmark = [pytest.mark.core_portable]\n\n'
        f'@pytest.mark.case("sc18-example")\ndef {test_name}():\n    pass\n',
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
                        "tests": [nodeid],
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
                "git_revision": _repo_head(_clean_repo(tmp_path)),
                "target": "x86_64-unknown-linux-gnu",
                "components": {
                    "cli": {
                        "implementation": "rust",
                        "git_revision": _repo_head(_clean_repo(tmp_path)),
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
    provenance: dict | None = None,
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

    if provenance is not None:
        report["provenance"] = provenance

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
                module, selected_name = nodeid.split("::", maxsplit=1)
                classname = module[:-3].replace("/", ".")
                name = selected_name.split("[", maxsplit=1)[0]
            else:
                classname = "fixture.required"
                name = f"test_{index}"
            ET.SubElement(suite, "testcase", classname=classname, name=name)
        ET.ElementTree(suite).write(junit_path, encoding="utf-8", xml_declaration=True)
    return junit_path


def _run(tmp_path: Path, inputs: dict[str, Path], *, extra: list[str] | None = None, repo_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(_clean_repo(tmp_path) if repo_root is None else repo_root),
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
    # 受測 HEAD 為登錄所針對的乾淨 repo（與 _make_inputs 的 manifest revision 相同）。
    assert rust["commit"] == _repo_head(_clean_repo(tmp_path))
    assert rust["platform"] == "linux"
    assert rust["result"] == "PASS"
    assert rust["artifact_path"] == str(inputs["bundle"])
    assert rust["artifact_sha256"] == hashlib.sha256(inputs["bundle"].read_bytes()).hexdigest()
    assert rust["test_id"] == TEST_NODE
    assert python["result"] == "PASS"
    assert python["artifact_path"] == str(inputs["python_cli"])
    assert python["artifact_sha256"] == hashlib.sha256(inputs["python_cli"].read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["missing", "failed", "junit-failed", "unfinished", "duplicate"])
def test_missing_or_nonpassing_test_never_gets_a_pass(tmp_path: Path, failure: str) -> None:
    inputs = _make_inputs(tmp_path)
    report_path = inputs["l1_dir"] / "linux" / "20groups" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    nodeid = TEST_NODE
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
            if item.get("classname") == TEST_NODE.split("::", maxsplit=1)[0][:-3].replace("/", ".")
            and item.get("name") == TEST_NODE.split("::", maxsplit=1)[1]
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
    junit = ET.Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
    ET.SubElement(
        junit,
        "testcase",
        classname="tests.contract_blackbox.test_registration.TestSuite",
        name="test_case",
    )
    ET.ElementTree(junit).write(inputs["junit"], encoding="utf-8", xml_declaration=True)
    return nodeid


def test_class_junit_node_and_class_case_marker_are_resolved(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    nodeid = _make_class_test(inputs, class_marker=True)
    register = _load_register()

    assert register._junit_rows(inputs["junit"], inputs["source_root"]) == {nodeid: ["passed"]}
    assert register._case_marker_matches(inputs["source_root"], nodeid, "sc18-example") == (True, None)


def test_case_marker_on_sibling_test_does_not_mark_target(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    nodeid = _make_class_test(inputs, class_marker=False)
    register = _load_register()

    assert register._case_marker_matches(inputs["source_root"], nodeid, "sc18-example")[0] is False


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


def _fake_test_exe(path: Path, test_names: list[str]) -> Path:
    """測試執行檔的替身：只是內容固定的檔案，供 sha256 核對使用。

    登錄器不重建、不重跑、不執行任何測試執行檔（只核對 L1 報告內的紀錄），故替身不需可執行，
    也因此本測試模組不依賴 shebang 腳本，可在原生 Windows 執行（core_portable）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("fake test executable: " + ",".join(test_names) + "\n").encode("utf-8"))
    return path


def _exe_row(exe: Path, nodeids: list[str], *, crate: str = "sw-cli", target: str = "cli_trace") -> dict:
    """L1 報告 provenance 的一列：執行檔路徑、報告產生當下的 sha256，以及其所含的 cargo nodeid。"""
    return {
        "crate": crate,
        "target": target,
        "path": str(exe.resolve()),
        "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "nodeids": [f"cargo:{node}" for node in nodeids],
    }


def _cargo_provenance(commit: str, rows: list[dict], *, platform: str = "linux", clean: bool = True) -> dict:
    """與 cargo_l1_results.py 同形狀的 provenance（cargo-l1-provenance.v1）；開始與結束的 git 狀態相同。"""
    state = {"head": commit, "clean": clean}
    return {
        "schema": "cargo-l1-provenance.v1",
        "platform": platform,
        "build_target": None,
        "git": {**state, "end": dict(state)},
        "toolchain": {
            "rustc_vv": "rustc 1.90.0 (test)\nhost: x86_64-unknown-linux-gnu\n",
            "rustc_vv_sha256": "0" * 64,
            "error": None,
        },
        "rustflags": {
            "RUSTFLAGS": {"set": False, "sha256": None},
            "CARGO_ENCODED_RUSTFLAGS": {"set": False, "sha256": None},
        },
        "executables": rows,
    }


def _register_cargo(
    tmp_path: Path,
    case_map_cases: list[dict],
    l1_dir: Path,
    out_dir: Path,
    *,
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    case_map = tmp_path / "p4_case_map.yaml"
    case_map.write_text(yaml.safe_dump({"schema_version": 1, "cases": case_map_cases}, sort_keys=False), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(_clean_repo(tmp_path) if repo_root is None else repo_root),
            "--case-map",
            str(case_map),
            "--l1-dir",
            str(l1_dir),
            "--out-dir",
            str(out_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _cargo_records_of(out_dir: Path, case_id: str) -> list[dict]:
    return json.loads((out_dir / f"{case_id}.json").read_text(encoding="utf-8"))["records"]


def _cargo_case(case_id: str, nodes: list[str], platforms: list[str] | None = None) -> dict:
    return {
        "case_id": case_id,
        "layer": "L1",
        "platforms": platforms or ["linux"],
        "artifact": "sw-pal-test-bin",
        "tests": nodes,
    }


def test_cargo_case_uses_the_matching_test_executable_sha256(tmp_path: Path) -> None:
    """L1 報告 provenance 內，依 crate＋target 唯一對應的執行檔；同 target 名的他套件執行檔（decoy）不得被選中。

    登錄以報告內所列執行檔的 sha256 為準（即實際受測的 binary），且記錄的 artifact 即該檔。
    """
    node = "sw-cli::cli_trace::trace_line_exact"
    decoy_node = "sw-other::cli_trace::trace_line_exact"
    trace_exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-aaa", ["trace_line_exact"])
    decoy_exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-decoy", ["trace_line_exact"])
    commit = _repo_head(_clean_repo(tmp_path))
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo", provenance=_cargo_provenance(commit, [
        _exe_row(decoy_exe, [decoy_node], crate="sw-other"),
        _exe_row(trace_exe, [node]),
    ]))
    out_dir = tmp_path / "out"

    result = _register_cargo(tmp_path, [_cargo_case("sc20-example", [node])], l1_dir, out_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert [record["result"] for record in rust] == ["PASS"], rust
    assert rust[0]["artifact_path"] == str(trace_exe.resolve())
    assert rust[0]["artifact_sha256"] == hashlib.sha256(trace_exe.read_bytes()).hexdigest()


def test_cargo_executable_must_be_unique_for_crate_and_target(tmp_path: Path) -> None:
    """同一 package＋target 在報告內出現兩個含此測試的執行檔即無法唯一對應 → FAIL，不得以先到者放行。"""
    node = "sw-cli::cli_trace::trace_line_exact"
    first = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    second = _fake_test_exe(tmp_path / "exe" / "cli_trace-2", ["trace_line_exact"])
    commit = _repo_head(_clean_repo(tmp_path))
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo", provenance=_cargo_provenance(commit, [
        _exe_row(first, [node]),
        _exe_row(second, [node]),
    ]))
    out_dir = tmp_path / "out"

    result = _register_cargo(tmp_path, [_cargo_case("sc20-example", [node])], l1_dir, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust), rust


def test_cargo_no_run_failure_fails_closed(tmp_path: Path) -> None:
    """建置失敗（cargo test --no-run 失敗）時報告為 finished=false、無結果列 → FAIL，不得以舊報告或重建補上。"""
    node = "sw-cli::cli_trace::trace_line_exact"
    exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    commit = _repo_head(_clean_repo(tmp_path))
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo", provenance=_cargo_provenance(commit, [_exe_row(exe, [node])]))
    report_path = l1_dir / "linux" / "cargo" / "resource-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(finished=False, exit_code=101, results=[])
    report_path.write_text(json.dumps(report), encoding="utf-8")
    out_dir = tmp_path / "out"

    result = _register_cargo(tmp_path, [_cargo_case("sc20-example", [node])], l1_dir, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert rust and not any(record["result"] == "PASS" for record in rust), rust


def test_cargo_executable_rebuilt_after_report_is_rejected(tmp_path: Path) -> None:
    """報告產生後改寫（或重建）執行檔、未重跑即登錄：sha256 與報告不符 → FAIL，理由要求重跑，不得以新 binary 記為 PASS。"""
    node = "sw-cli::cli_trace::trace_line_exact"
    exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    commit = _repo_head(_clean_repo(tmp_path))
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo", provenance=_cargo_provenance(commit, [_exe_row(exe, [node])]))
    exe.write_bytes(b"rebuilt executable with different bytes\n")
    out_dir = tmp_path / "out"

    result = _register_cargo(tmp_path, [_cargo_case("sc20-example", [node])], l1_dir, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust), rust
    assert all(record.get("artifact_path") is None for record in rust), rust
    assert all("重跑" in str(record.get("reason")) for record in rust), rust


def test_cargo_report_executable_without_sha256_is_rejected(tmp_path: Path) -> None:
    """報告的 executable 缺 sha256 紀錄 → 拒收（不得退回以磁碟上目前的檔案補算後放行）。"""
    node = "sw-cli::cli_trace::trace_line_exact"
    exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    commit = _repo_head(_clean_repo(tmp_path))
    row = _exe_row(exe, [node])
    del row["sha256"]
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo", provenance=_cargo_provenance(commit, [row]))
    out_dir = tmp_path / "out"

    result = _register_cargo(tmp_path, [_cargo_case("sc20-example", [node])], l1_dir, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    rust = [record for record in _cargo_records_of(out_dir, "sc20-example") if record["round"] == "rust"]
    assert rust and all(record["result"] == "FAIL" for record in rust), rust
    assert all("sha256" in str(record.get("reason")) for record in rust), rust


@pytest.mark.parametrize(
    "mutation",
    ["head-mismatch", "dirty-tree", "end-head-changed", "no-provenance", "no-rustc-vv"],
)
def test_cargo_report_provenance_must_match_tested_revision(tmp_path: Path, mutation: str) -> None:
    """報告的 git HEAD、工作樹乾淨狀態、執行期間 HEAD、provenance 本身或 rustc -vV 紀錄不符／缺席 → 全數 FAIL。"""
    node = "sw-cli::cli_trace::trace_line_exact"
    exe = _fake_test_exe(tmp_path / "exe" / "cli_trace-1", ["trace_line_exact"])
    commit = _repo_head(_clean_repo(tmp_path))
    rows = [_exe_row(exe, [node])]
    if mutation == "head-mismatch":
        provenance: dict | None = _cargo_provenance("f" * 40, rows)
    elif mutation == "dirty-tree":
        provenance = _cargo_provenance(commit, rows, clean=False)
    elif mutation == "end-head-changed":
        provenance = _cargo_provenance(commit, rows)
        provenance["git"]["end"]["head"] = "e" * 40
    elif mutation == "no-rustc-vv":
        provenance = _cargo_provenance(commit, rows)
        provenance["toolchain"]["rustc_vv"] = None
    else:
        provenance = None
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, node, runner="cargo", provenance=provenance)
    out_dir = tmp_path / "out"

    result = _register_cargo(tmp_path, [_cargo_case("sc20-example", [node])], l1_dir, out_dir)

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
    commit = _repo_head(_clean_repo(tmp_path))
    l1_dir = tmp_path / "l1"
    _write_l1_report(l1_dir, posix_node, runner="cargo", platform="linux", provenance=_cargo_provenance(
        commit, [_exe_row(posix_exe, [posix_node], crate="sw-pal", target="process_tunnel")], platform="linux",
    ))
    _write_l1_report(l1_dir, windows_node, runner="cargo", platform="windows", provenance=_cargo_provenance(
        commit, [_exe_row(windows_exe, [windows_node], crate="sw-pal", target="process_tunnel")], platform="windows",
    ))
    out_dir = tmp_path / "out"
    case = _cargo_case(
        "bc05-process-lifecycle-stop-restart", [posix_node, windows_node], ["linux", "windows"],
    )

    result = _register_cargo(tmp_path, [case], l1_dir, out_dir)

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
    """兩個平台的 s1b_cargo 鎖都不含的節點一律 FAIL（每個 case 平台各一筆），不讀取任何 L1 報告。"""
    node = "sw-cli::p4_case_placeholders::df07_bad_config_yaml"
    l1_dir = tmp_path / "l1"
    out_dir = tmp_path / "out"
    case = _cargo_case("df07-example", [node], ["linux", "windows"])

    result = _register_cargo(tmp_path, [case], l1_dir, out_dir)

    assert result.returncode != 0, result.stdout + result.stderr
    records = _cargo_records_of(out_dir, "df07-example")
    assert sorted(record["platform"] for record in records) == ["linux", "windows"]
    assert all(record["result"] == "FAIL" and "凍結" in str(record.get("reason")) for record in records), records
    assert not l1_dir.exists()


def _pytest_fixture_with_marker(tmp_path: Path, marker: str | None) -> dict[str, Path]:
    """以指定 pytestmark（None 表示無任何平台標記）重建 `_make_inputs` 的來源檔，並同步 L1 報告。"""
    nodeid = LINUX_ONLY_NODE if marker == "posix_only" else TEST_NODE
    inputs = _make_inputs(tmp_path, nodeid=nodeid)
    test_file = inputs["source_root"] / nodeid.split("::", maxsplit=1)[0]
    platform_mark = f"pytestmark = [pytest.mark.{marker}]\n\n" if marker else ""
    body = (
        'import pytest\n\n'
        f"{platform_mark}"
        f'@pytest.mark.case("sc18-example")\ndef {nodeid.split("::", maxsplit=1)[1]}():\n    pass\n'
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
    """正斜線指標相對安裝根解析；Rust launcher 拒絕的反斜線形式亦須拒收。"""
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": "versions/0.4.0"}), encoding="utf-8"
    )

    candidates, error = register._manifest_candidates(cli)

    assert error is None
    assert candidates[0] == root / "versions" / "0.4.0" / "release-manifest.json"
    assert root / "bin" / "versions" / "0.4.0" / "release-manifest.json" not in candidates
    assert root / "release-manifest.json" in candidates

    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": "versions\\0.4.0"}), encoding="utf-8"
    )
    candidates, error = register._manifest_candidates(cli)
    assert candidates == []
    assert error == "current-release.json 的 dir 必須是安全的 bundle 相對路徑"


def test_manifest_pointer_absolute_dir_is_used_as_is(tmp_path: Path) -> None:
    """保留既有案例 nodeid；絕對 dir 依 Rust launcher／OD-18 (3) 規則拒絕。"""
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    outside = tmp_path / "outside" / "versions" / "0.4.0"
    outside.mkdir(parents=True)
    (outside / "release-manifest.json").write_text("{}", encoding="utf-8")
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": str(outside)}), encoding="utf-8"
    )

    candidates, error = register._manifest_candidates(cli)

    assert candidates == []
    assert error == "current-release.json 的 dir 必須是安全的 bundle 相對路徑"


@pytest.mark.parametrize(
    "dir_value",
    ["../../outside", "versions/../../outside", "versions//0.4.0", "versions/0.4.0/", ".", "C:/outside", "/abs/outside"],
)
def test_manifest_pointer_dir_must_stay_inside_bundle(tmp_path: Path, dir_value: str) -> None:
    """指標 `dir` 含 `..`、空段、`.`、磁碟代號或為絕對路徑即拒收，與 Rust launcher 同一規則。"""
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": dir_value}), encoding="utf-8"
    )

    candidates, error = register._manifest_candidates(cli)

    assert candidates == []
    assert error == "current-release.json 的 dir 必須是安全的 bundle 相對路徑"


def test_manifest_pointer_absolute_dir_is_rejected(tmp_path: Path) -> None:
    """絕對路徑 `dir` 不得把 manifest 導到 bundle 之外（原『絕對路徑照用』行為已依 (4) 收緊為拒收）。"""
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

    assert candidates == []
    assert error == "current-release.json 的 dir 必須是安全的 bundle 相對路徑"


def test_manifest_pointer_escape_never_accepts_outside_manifest(tmp_path: Path) -> None:
    """指標 `dir` 指向 bundle 外、且該處 manifest 內容完全相符時，仍須拒收（不得因內容相符而放行）。"""
    register = _load_register()
    commit = _head()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    payload = b"rust-cli"
    cli.write_bytes(payload)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "release-manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "git_revision": commit,
        "components": {"cli": {
            "implementation": "rust", "git_revision": commit,
            "path": "bin/serialwrap.exe", "sha256": hashlib.sha256(payload).hexdigest(),
        }},
    }), encoding="utf-8")
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": "../outside"}), encoding="utf-8"
    )

    bundle, error = register._rust_manifest(cli, commit)

    assert bundle is None
    assert error == "current-release.json 的 dir 必須是安全的 bundle 相對路徑"


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


def _windows_bundle_layout(
    tmp_path: Path,
    commit: str,
    *,
    entry_bytes: bytes | None = None,
    pointer_dir: str = "versions/0.4.0",
) -> tuple[Path, Path, Path]:
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
        json.dumps({"bundle_version": "0.4.0", "dir": pointer_dir}), encoding="utf-8"
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


def test_register_junit_counts_are_testcase_level_not_suite_attributes(tmp_path: Path, monkeypatch) -> None:
    """F6：登記路徑之 junit 計數與檢查路徑同一（testcase 層）；tests 屬性含 subtests（6）但 testcase 僅 3 個 → 合計 3。"""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("register_p4_evidence_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite tests="6" failures="0" errors="0" skipped="1">'
        '<testcase classname="tests.a" name="t1"/>'
        '<testcase classname="tests.a" name="t2"><failure message="x"/></testcase>'
        '<testcase classname="tests.a" name="t3"><skipped message="x"/></testcase>'
        '</testsuite></testsuites>',
        encoding="utf-8",
    )
    assert module._junit_counts(path) == {"tests": 3, "failures": 1, "errors": 0, "skipped": 1}
    assert module._junit_counts(tmp_path / "missing.xml") is None


def test_manifest_pointer_forward_slash_dir_resolves_from_install_root(tmp_path: Path) -> None:
    """OD-18 (3)：現行指標 `dir` 為正斜線 `versions/<V>`，與舊版反斜線寫法相同以安裝根（cli.parent.parent）為基準解析。"""
    register = _load_register()
    root = tmp_path / "install"
    cli = root / "bin" / "serialwrap.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"rust-cli")
    (root / "bin" / "current-release.json").write_text(
        json.dumps({"bundle_version": "0.4.0", "dir": "versions/0.4.0"}), encoding="utf-8"
    )

    candidates, error = register._manifest_candidates(cli)

    assert error is None
    assert candidates[0] == root / "versions" / "0.4.0" / "release-manifest.json"
    assert root / "bin" / "versions" / "0.4.0" / "release-manifest.json" not in candidates


def test_windows_stable_entry_with_forward_slash_pointer_is_the_manifest_cli(tmp_path: Path) -> None:
    """OD-18 (3)：指標 `dir` 為 `versions/<V>` 時，穩定入口複本與 manifest 所指 CLI 同 byte 即為受測 artifact。"""
    register = _load_register()
    commit = _head()
    entry, manifest, _ = _windows_bundle_layout(tmp_path, commit, pointer_dir="versions/0.4.0")

    bundle, error = register._rust_manifest(entry, commit)

    assert error is None, error
    assert bundle is not None and bundle["path"] == manifest


@pytest.mark.parametrize("key", ["failures", "errors"])
def test_suite_declared_failures_above_testcase_level_fail_both_pytest_rounds(tmp_path: Path, key: str) -> None:
    """testsuite 宣告的 failures／errors 大於 testcase 層計數（例如 subtests 失敗而無 testcase 元素）→ rust 與 python 兩輪皆 FAIL。"""
    inputs = _make_inputs(tmp_path)
    junit = inputs["junit"]
    tree = ET.parse(junit)
    tree.getroot().set(key, "1")
    tree.write(junit, encoding="utf-8", xml_declaration=True)

    result = _run(tmp_path, inputs)

    assert result.returncode != 0, result.stdout + result.stderr
    records = json.loads((inputs["out_dir"] / "sc18-example.json").read_text(encoding="utf-8"))["records"]
    rust = [record for record in records if record["round"] == "rust"]
    python = [record for record in records if record["round"] == "python"]
    assert rust and python, records
    assert all(record["result"] == "FAIL" for record in rust + python), records
    assert not any(record["result"] == "PASS" for record in records), records


def test_register_junit_helpers_reject_suite_declared_failures_above_testcase_level(tmp_path: Path) -> None:
    """登錄端 junit 計數與列表皆 fail-closed：宣告的 failures 大於 testcase 層即整份不可用（None）。"""
    register = _load_register()
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0">'
        '<testcase classname="tests.a" name="t1"/>'
        '<testcase classname="tests.a" name="t2"/>'
        '</testsuite></testsuites>',
        encoding="utf-8",
    )

    assert register._junit_counts(path) is None
    assert register._junit_rows(path) is None
