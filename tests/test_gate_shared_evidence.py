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

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = pathlib.Path(__file__).resolve().parents[1]
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
        manifest["provenance"]["artifacts"][name] = {"path": str(dest.relative_to(shared)), "sha256": _sha(dest)}
        artifacts[name] = dest
    (shared / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return shared, set_id, artifacts, selector["argv"]


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
    source = tmp_path / "windows" / required_set / "l1-results.json"
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
    failed_source = tmp_path / "windows" / failed_set / "l1-results.json"
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
    source = tmp_path / "windows" / required_set / "l1-results.json"
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


def _make_release_bundle(root: pathlib.Path, *, head: str, payload: str) -> pathlib.Path:
    version_dir = root / "versions" / "0.4.1"
    bin_dir = version_dir / "bin"
    bin_dir.mkdir(parents=True)
    cli_bytes = f"cli:{payload}\n".encode("utf-8")
    daemon_bytes = f"daemon:{payload}\n".encode("utf-8")
    (bin_dir / "serialwrap").write_bytes(cli_bytes)
    (bin_dir / "serialwrapd").write_bytes(daemon_bytes)
    manifest = {
        "schema_version": 1,
        "bundle_version": "0.4.1",
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
        path = bundle / "versions" / "0.4.1" / "bin" / executable_name
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
        command.extend([*args, str(tmp_path / script.removesuffix(".sh"))])
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


def test_shared_driver_stops_and_records_gate_prereq_failure_before_cargo(tmp_path):
    fixture = tmp_path / "repo"
    scripts = fixture / "scripts/rust"
    scripts.mkdir(parents=True)
    driver = scripts / "gate_shared.sh"
    shutil.copy2(ROOT / "scripts/rust/gate_shared.sh", driver)
    shutil.copy2(ROOT / "scripts/rust/gate_shared_evidence.py", scripts / "gate_shared_evidence.py")
    (fixture / ".gitignore").write_text("target/\n__pycache__/\n", encoding="utf-8")
    (scripts / "gate_prereqs.sh").write_text(
        "printf 'stub gate prerequisite failure\\n'\nreturn 37\n", encoding="utf-8"
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
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CARGO_CALL_LOG"] = str(cargo_log)
    env["CARGO_TARGET_DIR"] = str(fixture / "target")
    env["CARGO_INCREMENTAL"] = "0"

    proc = subprocess.run(
        ["bash", str(driver), "--out", str(tmp_path / "shared-evidence"), "--run-id", "prereq-fail"],
        cwd=fixture,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    log_dir = fixture / "target/s2-par/gpz/gate-shared/prereq-fail"
    assert proc.returncode == 37, proc.stdout + proc.stderr
    assert (log_dir / "gate-prereqs.log").read_text(encoding="utf-8") == "stub gate prerequisite failure\n"
    assert (log_dir / "driver.tsv").read_text(encoding="utf-8") == "gate_prereqs\t37\n"
    assert not cargo_log.exists(), "前置失敗後不得啟動 build 或 cargo L1"
    assert not (log_dir / "cargo-fakes-filtered.log").exists()


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
    dev_manifest = dev_bundle / "versions" / "0.4.1" / "release-manifest.json"
    dev_info = json.loads(dev_manifest.read_text(encoding="utf-8"))
    dev_info["stage"] = "rust-2-dev"
    dev_manifest.write_text(json.dumps(dev_info, sort_keys=True), encoding="utf-8")
    dev_rejected = api.check_candidate_bundle(shared, candidate_bundle=dev_bundle)
    assert dev_rejected["status"] == "EVIDENCE_MISSING", dev_rejected

    (bundle / "versions" / "0.4.1" / "bin" / "serialwrapd").write_text("tampered\\n", encoding="utf-8")
    tampered = api.validate_section(
        shared, gate="slice24", section="s24_ab", required_set=None, platform="linux",
        expected_artifacts=artifacts, current_head=head, worktree_clean=True, expected_command="s24_ab",
    )
    assert tampered["status"] == "EVIDENCE_MISSING"
    assert tampered["reasons"]


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

    monkeypatch.setattr(bb_l15, "tempfile", SimpleNamespace(TemporaryDirectory=make_temp_directory))

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
    assert tempdir_args["dir"] == pathlib.Path("/tmp")
    assert work_path.parent == pathlib.Path("/tmp")
    bb_l15._cleanup_bundles()
    assert not work_path.exists(), f"G5 暫存 bundle 目錄未清理：{work_path}"


def test_gate_shared_artifact_paths_keep_cli_and_daemon_basenames():
    required = {
        "gate_shared.sh": (
            'SERIALWRAP_CLI_EXE="$RUN_DIR/artifacts/debug/serialwrap"',
            'SERIALWRAP_DAEMON_EXE="$RUN_DIR/artifacts/debug/serialwrapd"',
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
            '--artifact "release_daemon=$DAEMON_PATH"',
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
