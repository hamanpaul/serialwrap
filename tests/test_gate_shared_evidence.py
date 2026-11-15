"""OD-93：跨 Slice gate 的共用證據必須綁定候選與鎖定 selector。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
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
    for name in ("debug_cli", "debug_daemon", "release_daemon"):
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
        dest = shared / "artifacts" / name
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


def test_shared_failed_section_remains_fail_after_import(tmp_path):
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
    assert "shared-evidence" in (proc.stdout + proc.stderr)
    assert "import" in (proc.stdout + proc.stderr).lower()


def test_shared_driver_dry_run_lists_commands_without_running_them(tmp_path):
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/rust/gate_shared.sh"), "--dry-run", "--out", str(tmp_path / "shared")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cargo test --workspace" in proc.stdout
    assert "run_p7_ab.sh --od59-contract" in proc.stdout
    assert "pytest -q tests/" in proc.stdout
