"""OD-93：跨 Slice gate 的共用證據必須綁定候選與鎖定 selector。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
from urllib.parse import quote

import pytest

pytestmark = [pytest.mark.process_rpc]

ROOT = pathlib.Path(__file__).resolve().parents[1]
CURRENT_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
TOOL = ROOT / "scripts/rust/gate_shared_evidence.py"
GATES = ("slice21", "s22", "p7", "slice24")
GATE_SETS = {
    "slice21": "s21_gate:pytest_full",
    "s22": "s22_gate:cargo_workspace",
    "p7": "p7_gate:python3-m-pytest-q-tests",
    "slice24": "s21_gate:pytest_full",
}
ARTIFACT_PATHS = {
    "debug_cli": "debug/serialwrap",
    "debug_daemon": "debug/serialwrapd",
    "release_cli": "release/serialwrap",
    "release_daemon": "release/serialwrapd",
}


def _module():
    spec = importlib.util.spec_from_file_location("gate_shared_evidence_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _windows_l1_input(root: pathlib.Path, required_set: str) -> pathlib.Path:
    """測試來源目錄採 Windows 可建立的集合 ID 編碼格式。"""
    return root / quote(required_set, safe="") / "l1-results.json"


def _fixture(tmp_path: pathlib.Path, gate: str):
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    set_id = GATE_SETS[gate]
    selector = lock["sets"][set_id]["linux"]["selector"]
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifacts = {}
    for name in ("debug_cli", "debug_daemon", "release_cli", "release_daemon"):
        path = artifacts_dir / name
        path.write_bytes(f"candidate:{name}".encode())
        artifacts[name] = path

    shared = tmp_path / "shared"
    files_dir = shared / "sections" / "pytest_full"
    files_dir.mkdir(parents=True)
    log = files_dir / "pytest.log"
    log.write_text("pytest result\n", encoding="utf-8")
    section = {
        "argv": selector["argv"],
        "selectors": {f"{set_id}/linux": selector["argv"]},
        "exit_code": 0,
        "files": {"log": {"path": "sections/pytest_full/pytest.log", "sha256": _sha(log)}},
    }
    manifest = {
        "schema": "serialwrap-gate-shared-evidence.v1",
        "provenance": {
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                        text=True, check=True).stdout.strip(),
            "worktree_clean": True,
            "artifacts": {},
        },
        "sections": {"pytest_full": section},
    }
    # Artifacts live under the shared evidence directory so import verification can hash the source copy.
    for name, path in artifacts.items():
        dest = shared / "artifacts" / ARTIFACT_PATHS[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
        manifest["provenance"]["artifacts"][name] = {"path": dest.relative_to(shared).as_posix(), "sha256": _sha(dest)}
        artifacts[name] = dest
    (shared / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return shared, set_id, artifacts, selector["argv"]


@pytest.mark.core_portable
@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("mismatch", ("head", "dirty", "artifact", "argv"))
def test_shared_evidence_provenance_mismatch_is_missing_for_every_gate(tmp_path, gate, mismatch):
    shared, set_id, artifacts, expected_argv = _fixture(tmp_path, gate)
    manifest_path = shared / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mismatch == "head":
        manifest["provenance"]["git_head"] = "0" * 40
    elif mismatch == "dirty":
        manifest["provenance"]["worktree_clean"] = False
    elif mismatch == "artifact":
        artifact = shared / manifest["provenance"]["artifacts"]["debug_cli"]["path"]
        artifact.write_bytes(b"tampered candidate")
    else:
        manifest["sections"]["pytest_full"]["argv"] = [*expected_argv, "--maxfail=1"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    result = _module().validate_section(
        shared,
        gate=gate,
        section="pytest_full",
        required_set=set_id,
        platform="linux",
        expected_artifacts=artifacts,
        current_head=manifest["provenance"]["git_head"] if mismatch != "head" else "1" * 40,
        worktree_clean=mismatch != "dirty",
    )
    assert result["status"] == "EVIDENCE_MISSING"
    assert result["reasons"]


@pytest.mark.core_portable
def test_shared_failed_section_remains_fail_after_import(tmp_path, monkeypatch):
    shared, set_id, artifacts, _ = _fixture(tmp_path, "p7")
    manifest_path = shared / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sections"]["pytest_full"]["exit_code"] = 1
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    imported = _module().import_section(
        shared,
        gate="p7",
        section="pytest_full",
        required_set=set_id,
        platform="linux",
        expected_artifacts=artifacts,
        current_head=manifest["provenance"]["git_head"],
        worktree_clean=True,
        destination=tmp_path / "p7-import",
    )
    assert imported["status"] == "FAIL"
    assert imported["exit_code"] == 1

    manifest["sections"]["pytest_full"]["exit_code"] = False
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    malformed_exit = _module().import_section(
        shared,
        gate="p7",
        section="pytest_full",
        required_set=set_id,
        platform="linux",
        expected_artifacts=artifacts,
        current_head=manifest["provenance"]["git_head"],
        worktree_clean=True,
        destination=tmp_path / "p7-import-bool-exit",
    )
    assert malformed_exit["status"] == "EVIDENCE_MISSING"

    manifest["sections"]["p7_ab"] = {
        "argv": ["bash", "scripts/rust/run_p7_ab.sh", "--unexpected", "--od59-contract"],
        "exit_code": 0,
        "files": {"log": {"path": "sections/pytest_full/pytest.log",
                           "sha256": _sha(shared / "sections/pytest_full/pytest.log")}},
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    invalid_p7 = _module().validate_section(
        shared, gate="p7", section="p7_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=manifest["provenance"]["git_head"],
        worktree_clean=True, expected_command="p7_ab",
    )
    assert invalid_p7["status"] == "EVIDENCE_MISSING"
    manifest["sections"]["p7_ab"]["argv"] = [
        "bash", "scripts/rust/run_p7_ab.sh", "--run-id", shared.name,
        "--run-dir", str(shared / "work/p7-ab"), "--out", str(shared / "work/p7-ab.md"),
        "--rust-cli", str(shared / "artifacts/debug/serialwrap"),
        "--rust-daemon", str(shared / "artifacts/debug/serialwrapd"), "--od59-contract",
    ]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    valid_p7 = _module().validate_section(
        shared, gate="p7", section="p7_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=manifest["provenance"]["git_head"],
        worktree_clean=True, expected_command="p7_ab",
    )
    assert valid_p7["status"] == "PASS"

    api, s24_shared, _bundle, s24_artifacts, head = _s24_candidate_fixture(tmp_path / "s24", monkeypatch)
    s24_manifest_path = s24_shared / "manifest.json"
    s24_manifest = json.loads(s24_manifest_path.read_text(encoding="utf-8"))
    s24_argv = s24_manifest["sections"]["s24_ab"]["argv"]
    s24_manifest["sections"]["s24_ab"]["argv"] = [
        "-q", "tests/contract_blackbox", "tests/test_rust_contract_coverage.py", "tests/unlisted.py",
    ]
    s24_manifest_path.write_text(json.dumps(s24_manifest, sort_keys=True), encoding="utf-8")
    invalid_s24 = api.validate_section(
        s24_shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=s24_artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert invalid_s24["status"] == "EVIDENCE_MISSING"
    s24_manifest["sections"]["s24_ab"]["argv"] = s24_argv
    s24_manifest_path.write_text(json.dumps(s24_manifest, sort_keys=True), encoding="utf-8")
    valid_s24 = api.validate_section(
        s24_shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=s24_artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert valid_s24["status"] == "PASS"


def _windows_l1_report(head: str, selector: list[str]) -> dict:
    nodeid = "cargo:sw-daemon::dispatch::one_required_case"
    return {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "cargo",
        "platform": "windows",
        "argv": selector,
        "markexpr": "",
        "counts": {"all": {"passed": 1, "failed": 0, "skipped": 0}},
        "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [],
        "core_portable_platform_skips": [],
        "collected": [nodeid],
        "deselected": [],
        "results": [{"nodeid": nodeid, "outcome": "passed", "skip_kind": None,
                      "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}],
        "exit_code": 0,
        "finished": True,
        "provenance": {
            "schema": "cargo-l1-provenance.v1",
            "platform": "windows",
            "build_target": "x86_64-pc-windows-gnu",
            "git": {"head": head, "clean": True, "end": {"head": head, "clean": True}},
            "executables": [],
        },
        "deployed_content": None,
    }


@pytest.mark.core_portable
def test_windows_l1_import_records_locked_selector_and_provenance(tmp_path):
    shared, _, artifacts, _ = _fixture(tmp_path, "p7")
    head = _module().get_current_head()
    selector = ["--package", "sw-daemon", "--features", "test-fakes"]
    required_set = "test:windows_l1"
    failed_set = "test:windows_l1_failed_case"
    nodeid = "cargo:sw-daemon::dispatch::one_required_case"
    lock = tmp_path / "windows-lock.json"
    lock.write_text(json.dumps({"sets": {
        set_id: {"windows": {"selector": {"argv": selector}, "nodeids": [nodeid]}}
        for set_id in (required_set, failed_set)
    }}), encoding="utf-8")
    source = _windows_l1_input(tmp_path / "windows", required_set)
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(_windows_l1_report(head, selector)), encoding="utf-8")

    result = _module().record_windows_l1(
        shared,
        source_root=tmp_path / "windows",
        required_set=required_set,
        lock_path=lock,
        current_head=head,
        worktree_clean=True,
    )

    assert result["status"] == "PASS"
    manifest = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
    section = manifest["sections"][f"windows_l1:{required_set}"]
    assert section["argv"] == selector
    assert section["selectors"] == {f"{required_set}/windows": selector}
    assert section["files"]["l1-results.json"]["sha256"]

    failed_report = _windows_l1_report(head, selector)
    failed_report["counts"]["all"] = {"passed": 0, "failed": 1, "skipped": 0}
    failed_report["results"][0]["outcome"] = "failed"
    failed_report["results"][0]["phases"]["call"] = "failed"
    failed_source = _windows_l1_input(tmp_path / "windows", failed_set)
    failed_source.parent.mkdir(parents=True)
    failed_source.write_text(json.dumps(failed_report), encoding="utf-8")
    recorded_failure = _module().record_windows_l1(
        shared, source_root=tmp_path / "windows", required_set=failed_set,
        lock_path=lock, current_head=head, worktree_clean=True,
    )
    assert recorded_failure["status"] == "FAIL"
    imported_failure = _module().import_section(
        shared, gate="p7", section=f"windows_l1:{failed_set}", required_set=failed_set,
        platform="windows", expected_artifacts=artifacts, current_head=head, worktree_clean=True,
        destination=tmp_path / "windows-failed-import", lock_path=lock,
    )
    assert imported_failure["status"] == "FAIL"


@pytest.mark.core_portable
@pytest.mark.parametrize("mismatch", ("head", "dirty", "argv"))
def test_windows_l1_provenance_or_selector_mismatch_is_not_recorded(tmp_path, mismatch):
    shared, _, _, _ = _fixture(tmp_path, "p7")
    head = _module().get_current_head()
    selector = ["--package", "sw-daemon", "--features", "test-fakes"]
    required_set = "test:windows_l1"
    lock = tmp_path / "windows-lock.json"
    lock.write_text(json.dumps({"sets": {required_set: {"windows": {
        "selector": {"argv": selector}, "nodeids": ["cargo:sw-daemon::dispatch::one_required_case"]
    }}}}), encoding="utf-8")
    source = _windows_l1_input(tmp_path / "windows", required_set)
    source.parent.mkdir(parents=True)
    report_head = "0" * 40 if mismatch == "head" else head
    report = _windows_l1_report(report_head, selector)
    if mismatch == "dirty":
        report["provenance"]["git"]["clean"] = False
        report["provenance"]["git"]["end"]["clean"] = False
    elif mismatch == "argv":
        report["argv"] = [*selector, "--no-run"]
    source.write_text(json.dumps(report), encoding="utf-8")

    result = _module().record_windows_l1(
        shared,
        source_root=tmp_path / "windows",
        required_set=required_set,
        lock_path=lock,
        current_head=head,
        worktree_clean=True,
    )

    assert result["status"] == "EVIDENCE_MISSING"
    assert result["reasons"]
    manifest = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
    assert f"windows_l1:{required_set}" not in manifest["sections"]

    for outcome, collected, phases, counts in (
        ("passed", ["cargo:sw-daemon::dispatch::unexpected_case"],
         {"setup": "passed", "call": "passed", "teardown": "passed"},
         {"passed": 1, "failed": 0, "skipped": 0}),
        ("skipped", ["cargo:sw-daemon::dispatch::one_required_case"],
         {"setup": "passed", "call": "skipped", "teardown": "passed"},
         {"passed": 0, "failed": 0, "skipped": 1}),
    ):
        report = _windows_l1_report(head, selector)
        report["collected"] = collected
        report["counts"]["all"] = counts
        report["results"][0]["outcome"] = outcome
        report["results"][0]["phases"] = phases
        source.write_text(json.dumps(report), encoding="utf-8")
        invalid_result = _module().record_windows_l1(
            shared,
            source_root=tmp_path / "windows",
            required_set=required_set,
            lock_path=lock,
            current_head=head,
            worktree_clean=True,
        )
        assert invalid_result["status"] == "EVIDENCE_MISSING"
        manifest = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
        assert f"windows_l1:{required_set}" not in manifest["sections"]


def _windows_pytest_l1_report(head: str, selector: list[str], nodeid: str, markexpr: str) -> dict:
    """Stage 0 Windows pytest L1 與 Cargo L1 共用結果格式，但使用獨立的 pytest provenance。"""
    return {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "pytest",
        "platform": "windows",
        "argv": ["-c", r"--junitxml=C:\\Temp\\junit.xml", *selector],
        "markexpr": markexpr,
        "counts": {},
        "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [],
        "core_portable_platform_skips": [],
        "collected": [nodeid],
        "deselected": [],
        "results": [{"nodeid": nodeid, "outcome": "passed", "skip_kind": None,
                      "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}],
        "exit_code": 0,
        "finished": True,
        "provenance": {
            "schema": "pytest-l1-provenance.v1",
            "platform": "windows",
            "git": {"head": head, "clean": True, "end": {"head": head, "clean": True}},
        },
        "deployed_content": None,
    }


@pytest.mark.core_portable
def test_windows_native_pytest_l1_import_checks_selector_and_git_provenance(tmp_path):
    """Windows pytest 只能以專屬鎖列匯入，並核對平台、selector、HEAD 起訖與乾淨狀態。"""
    shared, _, artifacts, _ = _fixture(tmp_path, "slice21")
    api = _module()
    head = api.get_current_head()
    required_set = "s21_gate:py_parity"
    nodeid = (
        "tests/test_p1oc_drain_stalled.py::TestWindowsNativeDrainStalled::"
        "test_windows_guarded_write_stall_drain_stalled_native"
    )
    markexpr = "windows_native and windows_com"
    selector = ["-q", nodeid, "-m", markexpr]
    lock = tmp_path / "python-windows-lock.json"
    lock.write_text(json.dumps({"sets": {required_set: {"windows": {
        "nodeids": [nodeid],
        "selector": {"argv": selector, "markexpr": markexpr, "paths": [nodeid]},
    }}}}), encoding="utf-8")
    source_root = tmp_path / "windows"
    source = _windows_l1_input(source_root, required_set)
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(_windows_pytest_l1_report(head, selector, nodeid, markexpr)), encoding="utf-8")

    recorded = api.record_windows_l1(
        shared, source_root=source_root, required_set=required_set, lock_path=lock,
        current_head=head, worktree_clean=True,
    )

    assert recorded["status"] == "PASS", recorded
    imported = api.import_section(
        shared, gate="slice21", section=f"windows_l1:{required_set}", required_set=required_set,
        platform="windows", expected_artifacts=artifacts, current_head=head, worktree_clean=True,
        destination=tmp_path / "windows-pytest-import", lock_path=lock,
    )
    assert imported["status"] == "PASS", imported
    assert imported["argv"] == selector


@pytest.mark.core_portable
@pytest.mark.parametrize("mismatch", [
    "start_head", "end_head", "start_dirty", "end_dirty", "argv", "markexpr", "platform", "runner",
    "missing_provenance",
])
def test_windows_native_pytest_l1_provenance_or_selector_mismatch_is_not_recorded(tmp_path, mismatch):
    """pytest L1 的任何 provenance／selector 偏差均須拒收，不能沿用 Cargo 的較寬路徑。"""
    shared, _, _, _ = _fixture(tmp_path, "slice21")
    api = _module()
    head = api.get_current_head()
    required_set = "s21_gate:py_parity"
    nodeid = (
        "tests/test_p1oc_drain_stalled.py::TestWindowsNativeDrainStalled::"
        "test_windows_guarded_write_stall_drain_stalled_native"
    )
    markexpr = "windows_native and windows_com"
    selector = ["-q", nodeid, "-m", markexpr]
    lock = tmp_path / "python-windows-lock.json"
    lock.write_text(json.dumps({"sets": {required_set: {"windows": {
        "nodeids": [nodeid],
        "selector": {"argv": selector, "markexpr": markexpr, "paths": [nodeid]},
    }}}}), encoding="utf-8")
    source_root = tmp_path / "windows"
    source = _windows_l1_input(source_root, required_set)
    source.parent.mkdir(parents=True)
    report = _windows_pytest_l1_report(head, selector, nodeid, markexpr)
    if mismatch == "start_head":
        report["provenance"]["git"]["head"] = "0" * 40
    elif mismatch == "end_head":
        report["provenance"]["git"]["end"]["head"] = "0" * 40
    elif mismatch == "start_dirty":
        report["provenance"]["git"]["clean"] = False
    elif mismatch == "end_dirty":
        report["provenance"]["git"]["end"]["clean"] = False
    elif mismatch == "argv":
        report["argv"].append("--maxfail=1")
    elif mismatch == "markexpr":
        report["markexpr"] = "windows_native"
    elif mismatch == "platform":
        report["platform"] = "linux"
    elif mismatch == "runner":
        report["runner"] = "cargo"
        report["provenance"] = {
            "schema": "cargo-l1-provenance.v1", "platform": "windows",
            "build_target": "x86_64-pc-windows-gnu",
            "git": {"head": head, "clean": True, "end": {"head": head, "clean": True}},
            "executables": [],
        }
    else:
        del report["provenance"]
    source.write_text(json.dumps(report), encoding="utf-8")

    result = api.record_windows_l1(
        shared, source_root=source_root, required_set=required_set, lock_path=lock,
        current_head=head, worktree_clean=True,
    )

    assert result["status"] == "EVIDENCE_MISSING", result
    assert result["reasons"]
    manifest = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
    assert f"windows_l1:{required_set}" not in manifest["sections"]


@pytest.mark.core_portable
def test_windows_native_pytest_l1_failure_is_preserved_as_fail(tmp_path):
    """Windows pytest 必要列失敗須沿用唯一 L1 judge 判 FAIL，而非改成缺證或通過。"""
    shared, _, _, _ = _fixture(tmp_path, "slice21")
    api = _module()
    head = api.get_current_head()
    required_set = "s21_gate:py_parity"
    nodeid = (
        "tests/test_p1oc_drain_stalled.py::TestWindowsNativeDrainStalled::"
        "test_windows_guarded_write_stall_drain_stalled_native"
    )
    markexpr = "windows_native and windows_com"
    selector = ["-q", nodeid, "-m", markexpr]
    lock = tmp_path / "python-windows-lock.json"
    lock.write_text(json.dumps({"sets": {required_set: {"windows": {
        "nodeids": [nodeid],
        "selector": {"argv": selector, "markexpr": markexpr, "paths": [nodeid]},
    }}}}), encoding="utf-8")
    source_root = tmp_path / "windows"
    source = _windows_l1_input(source_root, required_set)
    source.parent.mkdir(parents=True)
    report = _windows_pytest_l1_report(head, selector, nodeid, markexpr)
    report["results"][0]["outcome"] = "failed"
    report["results"][0]["phases"]["call"] = "failed"
    report["exit_code"] = 1
    source.write_text(json.dumps(report), encoding="utf-8")

    result = api.record_windows_l1(
        shared, source_root=source_root, required_set=required_set, lock_path=lock,
        current_head=head, worktree_clean=True,
    )

    assert result["status"] == "FAIL", result
    assert result["exit_code"] == 1


def _make_release_bundle(root: pathlib.Path, *, head: str, payload: str) -> pathlib.Path:
    version_dir = root / "versions" / CURRENT_VERSION
    bin_dir = version_dir / "bin"
    bin_dir.mkdir(parents=True)
    cli_bytes = f"cli:{payload}\n".encode("utf-8")
    daemon_bytes = f"daemon:{payload}\n".encode("utf-8")
    (bin_dir / "serialwrap").write_bytes(cli_bytes)
    (bin_dir / "serialwrapd").write_bytes(daemon_bytes)
    manifest = {
        "schema_version": 1,
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
    }
    (version_dir / "release-manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    top_bin = root / "bin"
    top_bin.mkdir()
    for name in ("serialwrap", "serialwrapd"):
        (top_bin / name).write_bytes((bin_dir / name).read_bytes())
    return root


def _s24_candidate_fixture(tmp_path: pathlib.Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    api = _module()
    head = api.get_current_head()
    assert head
    monkeypatch.setattr(api, "get_current_head", lambda root=ROOT: head)
    monkeypatch.setattr(api, "is_worktree_clean", lambda root=ROOT: True)
    shared = tmp_path / "shared"
    shared.mkdir()
    bundle = _make_release_bundle(shared / "candidate-bundle", head=head, payload="candidate-a")
    source_artifacts = {}
    for name in ("debug_cli", "debug_daemon"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        source_artifacts[name] = path
    for artifact_name, executable_name in (("release_cli", "serialwrap"), ("release_daemon", "serialwrapd")):
        path = bundle / "versions" / CURRENT_VERSION / "bin" / executable_name
        source_artifacts[artifact_name] = path
    api.init_manifest(shared, source_artifacts, head=head, clean=True, candidate_bundle=bundle)
    log = tmp_path / "s24.log"
    junit = tmp_path / "junit.xml"
    log.write_text("22 passed\\n", encoding="utf-8")
    junit.write_text("<testsuite tests=\"22\" failures=\"0\"/>\\n", encoding="utf-8")
    argv = [
        "-q", "tests/contract_blackbox", "tests/acceptance/l15/test_slice21_l15.py",
        "tests/acceptance/l15/test_slice23_l15.py", "tests/test_multiagent_e2e.py",
        "tests/test_human_agent_coexist.py", "tests/test_rust_contract_coverage.py",
        "tests/test_transfer_golden_fresh.py",
    ]
    api.record_section(
        shared, name="s24_ab", argv=argv, exit_code=0,
        files={"log": log, "slice24/s24-ab/junit.xml": junit},
    )
    return api, shared, bundle, source_artifacts, head


@pytest.mark.core_portable
@pytest.mark.parametrize(
    "script,args",
    [
        ("slice21_gate.sh", ["--run-id", "shared-plan", "--out"]),
        ("s22_gate.sh", ["--run-id", "shared-plan", "--out"]),
        ("p7_gate.sh", ["--run-id", "shared-plan", "--out-md"]),
        ("slice24_exit.sh", ["--run-id", "shared-plan"]),
    ],
)
def test_gate_dry_run_with_shared_evidence_exits_zero_and_skips_shared_commands(tmp_path, script, args):
    shared, _, _, _ = _fixture(tmp_path, "p7")
    command = ["bash", str(ROOT / "scripts/rust" / script), "--dry-run", "--shared-evidence", str(shared)]
    if "--out" in args:
        out = (ROOT / "target" / "s2-gfx" / "tests" / tmp_path.parent.name / tmp_path.name / "s22-shared-plan"
               if script == "s22_gate.sh" else tmp_path / script.removesuffix(".sh"))
        command.extend([*args, str(out)])
    elif "--out-md" in args:
        command.extend([*args, str(tmp_path / "p7.md")])
    else:
        command.extend(args)
    proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    output = proc.stdout + proc.stderr
    assert "shared-evidence" in output
    assert "import" in output.lower()
    assert "artifact provenance 不符：EVIDENCE_MISSING，不啟動本地建置。" in output
    if script == "slice24_exit.sh":
        assert "env CARGO_INCREMENTAL=0 cargo check --workspace --target x86_64-pc-windows-gnu" in output
        assert "候選 bundle" in output
    if script == "s22_gate.sh":
        shutil.rmtree(out, ignore_errors=True)


@pytest.mark.core_portable
def test_shared_driver_dry_run_lists_commands_without_running_them(tmp_path):
    out_dir = "target/s2-par/gpz/i21/shared-dry-run"
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/rust/gate_shared.sh"), "--dry-run", "--out", out_dir, "--run-id", "dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "source scripts/rust/gate_prereqs.sh --gate" in proc.stdout
    assert "cargo test --workspace" in proc.stdout
    assert "run_p7_ab.sh" in proc.stdout
    assert "--od59-contract" in proc.stdout
    assert "mkbundle.sh --release-bundle --stage 2 --profile release" in proc.stdout
    assert "SW_BUNDLE" in proc.stdout
    assert "pytest -q tests/" in proc.stdout
    assert str(ROOT / out_dir / "dry-run") in proc.stdout


@pytest.mark.core_portable
def test_shared_driver_stops_and_records_gate_prereq_failure_before_cargo(tmp_path):
    fixture = tmp_path / "repo"
    scripts = fixture / "scripts/rust"
    scripts.mkdir(parents=True)
    for name in ("gate_shared.sh", "p7_gate.sh", "slice24_exit.sh"):
        shutil.copy2(ROOT / "scripts/rust" / name, scripts / name)
    shutil.copy2(ROOT / "scripts/rust/gate_shared_evidence.py", scripts / "gate_shared_evidence.py")
    (fixture / ".gitignore").write_text("target/\n__pycache__/\n", encoding="utf-8")
    (scripts / "gate_prereqs.sh").write_text(
        "printf '%s\\n' \"${PREREQ_DIAG:-stub gate prerequisite failure}\"\n"
        "return \"${PREREQ_RC:-37}\"\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init", "--quiet", str(fixture)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(fixture), "add", "."], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(fixture), "-c", "user.name=Gate Test", "-c", "user.email=gate-test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture"],
        check=True,
        capture_output=True,
        text=True,
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    cargo_log = tmp_path / "cargo.log"
    cargo = fake_bin / "cargo"
    cargo.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$CARGO_CALL_LOG\"\nexit 86\n", encoding="utf-8"
    )
    cargo.chmod(0o755)
    base_env = os.environ.copy()
    base_env["PATH"] = f"{fake_bin}{os.pathsep}{base_env['PATH']}"
    base_env["CARGO_CALL_LOG"] = str(cargo_log)
    base_env["CARGO_TARGET_DIR"] = str(fixture / "target")
    base_env["CARGO_INCREMENTAL"] = "0"

    drivers = {
        "shared": {
            "argv": ["--out", str(fixture / "target/s2-shared-evidence")],
            "log": lambda run_id: fixture / f"target/s2-shared-evidence/{run_id}/logs/gate-prereqs.log",
            "driver_tsv": lambda run_id: fixture / f"target/s2-par/gpz/gate-shared/{run_id}/driver.tsv",
        },
        "p7": {
            "argv": [],
            "log": lambda run_id: fixture / f"target/p7-gate/{run_id}/logs/gate-prereqs.log",
        },
        "slice24": {
            "argv": [],
            "log": lambda run_id: fixture / f"target/contract-coverage/{run_id}/logs/gate-prereqs.log",
            "driver_tsv": lambda run_id: fixture / f"target/contract-coverage/{run_id}/driver.tsv",
        },
    }

    def invoke(name, run_id, env):
        script_name = {"shared": "gate_shared.sh", "p7": "p7_gate.sh", "slice24": "slice24_exit.sh"}[name]
        command = ["bash", str(scripts / script_name), *drivers[name]["argv"], "--run-id", run_id]
        return subprocess.run(command, cwd=fixture, env=env, capture_output=True, text=True,
                              check=False, timeout=60)

    # 髒樹必須在任何前置／建置前拒絕，並列出實際髒檔；參數錯誤仍保留 exit 2。
    dirty_file = fixture / "dirty sentinel.txt"
    dirty_file.write_text("uncommitted\n", encoding="utf-8")
    dirty_env = base_env | {"PREREQ_DIAG": "stub prerequisite must not run", "PREREQ_RC": "37"}
    for name in drivers:
        run_id = f"dirty-{name}"
        proc = invoke(name, run_id, dirty_env)
        assert proc.returncode == 3, proc.stdout + proc.stderr
        output = proc.stdout + proc.stderr
        assert "工作樹不乾淨" in output and "dirty sentinel.txt" in output, output
        assert "stub prerequisite must not run" not in output, output
        assert not drivers[name]["log"](run_id).exists(), "髒樹拒絕時不得啟動 gate_prereqs"
        script_name = {"shared": "gate_shared.sh", "p7": "p7_gate.sh", "slice24": "slice24_exit.sh"}[name]
        bad_args = subprocess.run(
            ["bash", str(scripts / script_name), "--unknown-option"],
            cwd=fixture, env=dirty_env, capture_output=True, text=True, check=False, timeout=30,
        )
        assert bad_args.returncode == 2, bad_args.stdout + bad_args.stderr
    dirty_file.unlink()

    # 每種 gate 前置失敗都要留明確原因；三個 driver 統一用 exit 4，與參數錯誤 exit 2 分開。
    failure_cases = (
        ("gate_prereqs.sh: loadavg 99 不低於核心數一半（24/2）", 3),
        ("gate_prereqs.sh: D 槽可用空間 99G，小於 100G", 3),
        ("gate_prereqs.sh: 殘留 worktree 拋棄式 serialwrapd：123", 3),
        ("gate_prereqs.sh: WSL interop 無法啟動 cmd.exe", 3),
        ("gate_prereqs.sh: 候選版 Stage 2 bundle 建置失敗", 2),
    )
    for index, (diagnostic, source_rc) in enumerate(failure_cases):
        for name in drivers:
            run_id = f"prereq-{index}-{name}"
            env = base_env | {"PREREQ_DIAG": diagnostic, "PREREQ_RC": str(source_rc)}
            proc = invoke(name, run_id, env)
            assert proc.returncode == 4, proc.stdout + proc.stderr
            log = drivers[name]["log"](run_id).read_text(encoding="utf-8")
            assert diagnostic in log, log
            assert "gate_prereqs --gate 失敗" in log, log
            assert "gate_prereqs --gate 失敗" in (proc.stdout + proc.stderr)
            if "driver_tsv" in drivers[name]:
                assert drivers[name]["driver_tsv"](run_id).read_text(encoding="utf-8") == f"gate_prereqs\t{source_rc}\n"
    assert not cargo_log.exists(), "髒樹或前置失敗後不得啟動 build 或 cargo L1"


@pytest.mark.core_portable
def test_shared_artifact_init_and_import_preserve_executable_names(tmp_path, monkeypatch):
    api = _module()
    sources = {}
    for name in ARTIFACT_PATHS:
        source = tmp_path / "candidate" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"candidate:{name}".encode())
        sources[name] = source

    shared = tmp_path / "shared"
    manifest = api.init_manifest(shared, sources, head="candidate-head", clean=True)
    imported_dir = tmp_path / "imported"
    imported = api.import_artifacts(
        shared, imported_dir, current_head="candidate-head", worktree_clean=True,
    )

    assert imported["status"] == "PASS", imported
    for name, relative in ARTIFACT_PATHS.items():
        row = manifest["provenance"]["artifacts"][name]
        assert row["path"] == f"artifacts/{relative}"
        source_path = shared / row["path"]
        imported_path = pathlib.Path(imported["artifacts"][name])
        assert source_path.is_file() and source_path.name in ("serialwrap", "serialwrapd")
        assert imported_path == imported_dir / relative
        assert imported_path.name == source_path.name
        assert _sha(imported_path) == row["sha256"]

    api, shared, bundle, artifacts, head = _s24_candidate_fixture(tmp_path / "s24", monkeypatch)
    valid = api.validate_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert valid["status"] == "PASS", valid
    resolved = api.check_candidate_bundle(shared)
    assert resolved["status"] == "PASS", resolved
    assert pathlib.Path(resolved["path"]) == bundle
    cli_check = subprocess.run(
        [sys.executable, str(TOOL), "check-candidate-bundle", "--shared-evidence", str(shared)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert cli_check.returncode == 0, cli_check.stdout + cli_check.stderr
    assert json.loads(cli_check.stdout)["path"] == str(bundle)

    different = _make_release_bundle(tmp_path / "other-bundle", head=head, payload="candidate-b")
    rejected = api.validate_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
        candidate_bundle=different,
    )
    assert rejected["status"] == "EVIDENCE_MISSING"
    assert rejected["reasons"]
    external_mismatch = api.check_candidate_bundle(shared, candidate_bundle=different)
    assert external_mismatch["status"] == "EVIDENCE_MISSING", external_mismatch
    cli_mismatch = subprocess.run(
        [sys.executable, str(TOOL), "check-candidate-bundle", "--shared-evidence", str(shared),
         "--candidate-bundle", str(different)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert cli_mismatch.returncode == 3, cli_mismatch.stdout + cli_mismatch.stderr
    assert json.loads(cli_mismatch.stdout)["status"] == "EVIDENCE_MISSING"

    dev_bundle = _make_release_bundle(tmp_path / "dev-bundle", head=head, payload="dev")
    dev_manifest = dev_bundle / "versions" / CURRENT_VERSION / "release-manifest.json"
    dev_info = json.loads(dev_manifest.read_text(encoding="utf-8"))
    dev_info["stage"] = "rust-2-dev"
    dev_manifest.write_text(json.dumps(dev_info, sort_keys=True), encoding="utf-8")
    dev_rejected = api.check_candidate_bundle(shared, candidate_bundle=dev_bundle)
    assert dev_rejected["status"] == "EVIDENCE_MISSING", dev_rejected

    (bundle / "versions" / CURRENT_VERSION / "bin" / "serialwrapd").write_text("tampered\\n", encoding="utf-8")
    tampered = api.validate_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert tampered["status"] == "EVIDENCE_MISSING"
    assert tampered["reasons"]


@pytest.mark.core_portable
def test_cargo_results_and_s24_ab_use_the_same_candidate_bundle_sha256(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace

    api, shared, bundle, _artifacts, _head = _s24_candidate_fixture(tmp_path / "s24", monkeypatch)
    script = (ROOT / "scripts/rust/gate_shared.sh").read_text(encoding="utf-8")
    cargo_block = script.split("CARGO_RESULTS_DIR=", 1)[1].split("P7_AB=", 1)[0]
    s24_block = script.split('run_capture "$LOG_DIR/s24-ab.log"', 1)[1].split(
        'if [[ -n "$WINDOWS_L1" ]]', 1
    )[0]
    cli_assignment = 'SERIALWRAP_CLI_EXE="$S24_BUNDLE_VERSION_ROOT/bin/serialwrap"'
    daemon_assignment = 'SERIALWRAP_DAEMON_EXE="$S24_BUNDLE_VERSION_ROOT/bin/serialwrapd"'
    assert cli_assignment in cargo_block
    assert daemon_assignment in cargo_block
    assert cli_assignment in s24_block
    assert daemon_assignment in s24_block

    version_root = bundle / "versions" / CURRENT_VERSION
    env = os.environ.copy()
    env.update({
        "S24_BUNDLE_VERSION_ROOT": str(version_root),
        "SERIALWRAP_CLI_EXE": str(version_root / "bin/serialwrap"),
        "SERIALWRAP_DAEMON_EXE": str(version_root / "bin/serialwrapd"),
    })
    fixture = tmp_path / "cargo-fixture.json"
    fixture.write_text(json.dumps({"targets": [], "exit_code": 0, "finished": True}), encoding="utf-8")
    cargo_out = tmp_path / "cargo-results"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/rust/import_cargo_results.py"),
         "--label", "rust-linux", "--platform", "linux", "--out-dir", str(cargo_out),
         "--fixture", str(fixture)],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cargo_result = json.loads((cargo_out / "results-rust-linux.json").read_text(encoding="utf-8"))

    plugin_path = ROOT / "tests/contract_blackbox/coverage_plugin.py"
    plugin_spec = importlib.util.spec_from_file_location("coverage_plugin_identity_under_test", plugin_path)
    assert plugin_spec is not None and plugin_spec.loader is not None
    plugin = importlib.util.module_from_spec(plugin_spec)
    plugin_spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "_ab_env", lambda: {
        name: env[name] for name in ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE")
    } | {"SERIALWRAP_AB_LABEL": "rust-linux"})
    monkeypatch.setattr(plugin, "_harness", lambda: SimpleNamespace(
        cli_prefix=lambda: [env["SERIALWRAP_CLI_EXE"]],
        daemon_prefix=lambda: [env["SERIALWRAP_DAEMON_EXE"]],
    ))
    cli, daemon, errors = plugin._artifact_identity("rust-linux")
    assert errors == []
    assert cargo_result["cli_sha256"] == plugin._sha256(pathlib.Path(cli))
    assert cargo_result["daemon_sha256"] == plugin._sha256(pathlib.Path(daemon))
    assert cargo_result["cli_sha256"] == _sha(version_root / "bin/serialwrap")
    assert cargo_result["daemon_sha256"] == _sha(version_root / "bin/serialwrapd")
    assert cargo_result["cli_sha256"] != _sha(shared / "artifacts/debug/serialwrap")
    assert cargo_result["daemon_sha256"] != _sha(shared / "artifacts/debug/serialwrapd")


@pytest.mark.core_portable
def test_s24_bundle_check_failure_is_recorded_missing_and_does_not_skip_windows_l1(tmp_path, monkeypatch):
    api, shared, bundle, artifacts, head = _s24_candidate_fixture(tmp_path / "s24", monkeypatch)
    daemon = bundle / "versions" / CURRENT_VERSION / "bin" / "serialwrapd"
    daemon.write_bytes(b"tampered after the S24 test\n")
    checked = api.check_candidate_bundle(shared, current_head=head)
    assert checked["status"] == "EVIDENCE_MISSING", checked

    check_log = tmp_path / "s24-bundle-check.log"
    check_log.write_text("candidate bundle check failed\n", encoding="utf-8")
    recorded = subprocess.run(
        [sys.executable, str(TOOL), "record-missing-section",
         "--shared-evidence", str(shared), "--section", "s24_ab",
         "--argv-json", json.dumps(["-q", "tests/contract_blackbox"]),
         "--exit-code", "3", "--reason", "candidate bundle check failed",
         "--file", f"logs/s24-bundle-check.log={check_log}"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    manifest = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sections"]["s24_ab"]["evidence_status"] == "EVIDENCE_MISSING"
    imported = api.validate_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert imported["status"] == "EVIDENCE_MISSING", imported
    daemon.write_bytes(b"daemon:candidate-a\n")
    restored_bundle_import = api.validate_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert restored_bundle_import["status"] == "EVIDENCE_MISSING", restored_bundle_import

    script = (ROOT / "scripts/rust/gate_shared.sh").read_text(encoding="utf-8")
    s24_block = script.split("S24_TARGETS=(", 1)[1].split('if [[ -n "$WINDOWS_L1" ]]', 1)[0]
    assert 'check_s24_candidate_bundle before' in s24_block
    assert 'check_s24_candidate_bundle after' in s24_block
    assert "record-missing-section" in s24_block
    assert script.index('if [[ -n "$WINDOWS_L1" ]]') > script.index("S24_TARGETS=(")

    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    windows_set = next(set_id for set_id, platforms in sorted(lock["sets"].items()) if "windows" in platforms)
    real_python = sys.executable
    for fail_at in (1, 2):
        run_root = tmp_path / f"driver-failure-{fail_at}"
        run_dir = run_root / "run"
        log_dir = run_root / "logs"
        windows_l1 = run_root / "windows-l1"
        (windows_l1 / windows_set).mkdir(parents=True)
        (windows_l1 / windows_set / "l1-results.json").write_text("{}\n", encoding="utf-8")
        stub_bin = run_root / "bin"
        stub_bin.mkdir(parents=True)
        events = run_root / "events.log"
        wrapper = stub_bin / "python3"
        wrapper.write_text(r'''#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *"gate_shared_evidence.py check-candidate-bundle"* ]]; then
  count=0; [[ ! -f "$SW_TEST_CHECK_COUNT" ]] || count=$(<"$SW_TEST_CHECK_COUNT")
  count=$((count + 1)); printf '%s' "$count" > "$SW_TEST_CHECK_COUNT"
  echo "bundle-check-$count" >> "$SW_TEST_EVENTS"
  echo "candidate check $count"
  [[ "$count" != "$SW_TEST_FAIL_AT" ]] || exit 17
  exit 0
fi
if [[ " $* " == *"record-missing-section"* ]]; then echo record-missing-section >> "$SW_TEST_EVENTS"; exit 0; fi
if [[ " $* " == *"record-windows-l1"* ]]; then echo record-windows-l1 >> "$SW_TEST_EVENTS"; exit 0; fi
if [[ "${1:-}" == -m && "${2:-}" == pytest ]]; then
  echo pytest >> "$SW_TEST_EVENTS"
  for arg in "$@"; do case "$arg" in --junitxml=*) mkdir -p "$(dirname "${arg#*=}")"; echo '<testsuite/>' > "${arg#*=}";; esac; done
  exit 0
fi
exec "$SW_TEST_REAL_PYTHON" "$@"
''', encoding="utf-8")
        wrapper.chmod(0o755)
        run_dir.mkdir(parents=True)
        (run_dir / "coverage").mkdir()
        harness = run_root / "exercise-s24-driver.sh"
        harness_prefix = r'''#!/usr/bin/env bash
set -euo pipefail
set -o pipefail
RUN_DIR="$SW_TEST_RUN_DIR"
LOG_DIR="$SW_TEST_LOG_DIR"
WINDOWS_L1="$SW_TEST_WINDOWS_L1"
S24_BUNDLE="$SW_TEST_BUNDLE"
S24_BUNDLE_VERSION_ROOT="$SW_TEST_VERSION_ROOT"
mkdir -p "$LOG_DIR"
FAILURES=0
LAST_RC=0
run_capture() { local log="$1"; shift; set +e; "$@" 2>&1 | tee "$log"; local statuses=("${PIPESTATUS[@]}"); set -e; LAST_RC="${statuses[0]}"; [[ "$LAST_RC" == 0 ]] || FAILURES=1; }
'''
        harness.write_text(harness_prefix + script[script.index("S24_TARGETS=("):], encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "SW_TEST_REAL_PYTHON": real_python,
            "SW_TEST_EVENTS": str(events),
            "SW_TEST_CHECK_COUNT": str(run_root / "check-count"),
            "SW_TEST_FAIL_AT": str(fail_at),
            "SW_TEST_RUN_DIR": str(run_dir),
            "SW_TEST_LOG_DIR": str(log_dir),
            "SW_TEST_WINDOWS_L1": str(windows_l1),
            "SW_TEST_BUNDLE": str(run_root / "candidate-bundle"),
            "SW_TEST_VERSION_ROOT": str(run_root / "candidate-bundle" / "versions" / CURRENT_VERSION),
        })
        proc = subprocess.run(["bash", str(harness)], cwd=ROOT, env=env,
                              capture_output=True, text=True, check=False)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        observed = events.read_text(encoding="utf-8").splitlines()
        assert observed.count("record-missing-section") == 1, observed
        assert observed.count("record-windows-l1") == 1, observed
        assert observed.index("record-missing-section") < observed.index("record-windows-l1"), observed
        assert "Windows L1 匯入：1 個鎖定集合" in proc.stdout
        assert observed.count("bundle-check-1") == 1
        if fail_at == 1:
            assert observed == ["bundle-check-1", "record-missing-section", "record-windows-l1"]
        else:
            assert observed == ["bundle-check-1", "pytest", "bundle-check-2",
                                "record-missing-section", "record-windows-l1"]


@pytest.mark.core_portable
def test_g5_bundle_tempdir_uses_short_system_path(tmp_path, monkeypatch):
    """長 worktree 下的 G5 bundle 暫存根也不得沿用 ROOT/target/g5-tmp。"""
    import importlib.util
    import tempfile
    from types import SimpleNamespace

    module_path = ROOT / "tests/contract_blackbox/bb_l15.py"
    monkeypatch.syspath_prepend(str(module_path.parent))
    spec = importlib.util.spec_from_file_location("bb_l15_short_tmp_test", module_path)
    assert spec is not None and spec.loader is not None
    bb_l15 = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, bb_l15)
    spec.loader.exec_module(bb_l15)

    long_worktree = tmp_path / ("worktree-" + "x" * 120)
    long_worktree.mkdir()
    monkeypatch.setattr(bb_l15, "ROOT", long_worktree)
    monkeypatch.setattr(bb_l15, "_bundle_work", None)
    monkeypatch.setattr(bb_l15, "_bundle_pair", None)

    real_temp_directory = tempfile.TemporaryDirectory
    tempdir_args = {}

    def make_temp_directory(*, prefix, dir, **kwargs):
        tempdir_args["dir"] = pathlib.Path(dir)
        return real_temp_directory(prefix=prefix, dir=dir, **kwargs)

    monkeypatch.setattr(bb_l15, "tempfile", SimpleNamespace(
        TemporaryDirectory=make_temp_directory, gettempdir=tempfile.gettempdir,
    ))

    def fake_mkbundle(argv, **_kwargs):
        version = argv[argv.index("--version-override") + 1]
        out = pathlib.Path(argv[argv.index("--out") + 1])
        cli = out / "bin" / "serialwrap"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        cli.chmod(0o755)
        (out / "release-manifest.json").write_text(
            json.dumps({"bundle_version": version}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bb_l15, "subprocess", SimpleNamespace(run=fake_mkbundle))
    bb_l15.update_bundles()
    bundle_work = bb_l15._bundle_work
    assert bundle_work is not None
    work_path = pathlib.Path(bundle_work.name)
    expected_temp_root = pathlib.Path(tempfile.gettempdir())
    assert tempdir_args["dir"] == expected_temp_root
    assert work_path.parent == expected_temp_root
    bb_l15._cleanup_bundles()
    assert not work_path.exists(), f"G5 暫存 bundle 目錄未清理：{work_path}"


@pytest.mark.core_portable
def test_gate_shared_artifact_paths_keep_cli_and_daemon_basenames():
    required = {
        "gate_shared.sh": (
            'SERIALWRAP_CLI_EXE="$S24_BUNDLE_VERSION_ROOT/bin/serialwrap"',
            'SERIALWRAP_DAEMON_EXE="$S24_BUNDLE_VERSION_ROOT/bin/serialwrapd"',
            '--rust-cli "$RUN_DIR/artifacts/debug/serialwrap"',
            '--rust-daemon "$RUN_DIR/artifacts/debug/serialwrapd"',
            'scripts/rust/mkbundle.sh \\\n  --release-bundle --stage 2 --profile release',
            '--candidate-bundle "$S24_BUNDLE"',
            'SW_BUNDLE="$S24_BUNDLE"',
        ),
        "slice21_gate.sh": (
            'RUST_CLI="$SHARED_ARTIFACT_DIR/release/serialwrap"',
            'RUST_D="$SHARED_ARTIFACT_DIR/release/serialwrapd"',
            '--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug/serialwrap"',
            '--artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug/serialwrapd"',
            '--artifact "release_cli=$RUST_CLI"',
            '--artifact "release_daemon=$RUST_D"',
        ),
        "s22_gate.sh": (
            'DAEMON_PATH="$SHARED_ARTIFACT_DIR/release/serialwrapd"',
            '--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug/serialwrap"',
            '--artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug/serialwrapd"',
            '--artifact "release_cli=$SHARED_ARTIFACT_DIR/release/serialwrap"',
            '--artifact "release_daemon=$SHARED_ARTIFACT_DIR/release/serialwrapd"',
        ),
        "p7_gate.sh": (
            'RUST_CLI="$RUN/artifacts/serialwrap"',
            'RUST_D="$RUN/artifacts/serialwrapd"',
            'cp -p "$RUN/shared-artifact-import/debug/serialwrap" "$RUST_CLI"',
            'cp -p "$RUN/shared-artifact-import/debug/serialwrapd" "$RUST_D"',
            'cp -p "$RUN/shared-artifact-import/release/serialwrapd" "$PRIOR_RELEASE_D"',
            '--artifact "debug_cli=$RUN/shared-artifact-import/debug/serialwrap"',
            '--artifact "debug_daemon=$RUN/shared-artifact-import/debug/serialwrapd"',
            '--artifact "release_cli=$RUN/shared-artifact-import/release/serialwrap"',
            '--artifact "release_daemon=$RUN/shared-artifact-import/release/serialwrapd"',
        ),
        "slice24_exit.sh": (
            'RUST_CLI="$RUN_DIR/artifacts/serialwrap"',
            'RUST_DAEMON="$RUN_DIR/artifacts/serialwrapd"',
            'cp -p "$SHARED_ARTIFACT_DIR/debug/serialwrap" "$RUST_CLI"',
            'cp -p "$SHARED_ARTIFACT_DIR/debug/serialwrapd" "$RUST_DAEMON"',
            '--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug/serialwrap"',
            '--artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug/serialwrapd"',
            '--artifact "release_cli=$SHARED_ARTIFACT_DIR/release/serialwrap"',
            '--artifact "release_daemon=$SHARED_ARTIFACT_DIR/release/serialwrapd"',
            'check-candidate-bundle "${BUNDLE_CHECK_ARGS[@]}"',
            'S24_BUNDLE_ARGS+=(--candidate-bundle "$CANDIDATE_BUNDLE")',
            'S24_BUNDLE_ENV+=("SW_BUNDLE=$CANDIDATE_BUNDLE")',
        ),
        "run_p7_ab.sh": (
            'RUST_CLI="$ROOT/target/debug/serialwrap"',
            'RUST_D="$ROOT/target/debug/serialwrapd"',
        ),
    }
    for filename, required_paths in required.items():
        source = (ROOT / "scripts/rust" / filename).read_text(encoding="utf-8")
        for path in required_paths:
            assert path in source, f"{filename} 缺少具 serialwrap/serialwrapd basename 的接線：{path}"
        for stale in ("/artifacts/debug_cli", "/artifacts/debug_daemon",
                      "/artifacts/release_cli", "/artifacts/release_daemon"):
            assert stale not in source, f"{filename} 仍讀取舊共用 artifact 路徑：{stale}"


def _init_with_extra(tmp_path: pathlib.Path, monkeypatch, extra) -> None:
    api = _module()
    head = api.get_current_head()
    assert head
    monkeypatch.setattr(api, "get_current_head", lambda root=ROOT: head)
    monkeypatch.setattr(api, "is_worktree_clean", lambda root=ROOT: True)
    shared = tmp_path / "shared"
    shared.mkdir()
    extra(shared)
    bundle = _make_release_bundle(shared / "candidate-bundle", head=head, payload="candidate-a")
    sources = {}
    for name in ("debug_cli", "debug_daemon"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        sources[name] = path
    for artifact_name, executable_name in (("release_cli", "serialwrap"), ("release_daemon", "serialwrapd")):
        sources[artifact_name] = bundle / "versions" / CURRENT_VERSION / "bin" / executable_name
    api.init_manifest(shared, sources, head=head, clean=True, candidate_bundle=bundle)


@pytest.mark.core_portable
def test_init_manifest_accepts_driver_prereq_log_next_to_candidate_bundle(tmp_path, monkeypatch):
    """候選版 36ef2d7b 實跑：gate_shared.sh 先建 RUN_DIR/logs/gate-prereqs.log 才 init，init 卻拒絕整個目錄。"""
    def prereq_log(shared: pathlib.Path) -> None:
        (shared / "logs").mkdir()
        (shared / "logs" / "gate-prereqs.log").write_text("ok\\n", encoding="utf-8")

    _init_with_extra(tmp_path, monkeypatch, prereq_log)


@pytest.mark.core_portable
@pytest.mark.parametrize(
    "extra_kind", ["stray_file", "stray_dir", "extra_log", "prereq_log_is_dir"],
)
def test_init_manifest_still_rejects_other_content(tmp_path, monkeypatch, extra_kind):
    def extra(shared: pathlib.Path) -> None:
        if extra_kind == "stray_file":
            (shared / "old.json").write_text("{}", encoding="utf-8")
        elif extra_kind == "stray_dir":
            (shared / "coverage").mkdir()
        elif extra_kind == "extra_log":
            (shared / "logs").mkdir()
            (shared / "logs" / "gate-prereqs.log").write_text("ok\\n", encoding="utf-8")
            (shared / "logs" / "other.log").write_text("x\\n", encoding="utf-8")
        else:
            (shared / "logs" / "gate-prereqs.log").mkdir(parents=True)

    with pytest.raises(ValueError, match="候選 bundle 以外的輸出目錄內容不允許"):
        _init_with_extra(tmp_path, monkeypatch, extra)
