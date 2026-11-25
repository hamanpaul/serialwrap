"""Stage 3 T3：以隔離 Git repo 驗證原始 blob 位元組。"""

from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

import stage3_fakes as fakes
from test_stage3_ab_manifest import (
    _install_test_schemas,
    _read_json,
    _trusted_source_reader,
    _windows_l15_transport_fixture,
)
from tools.stage3 import ab_manifest
from tools.stage3.proc import CmdResult, run_cmd

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def test_tool_revision_mapping_check_compares_raw_bytes_under_autocrlf(tmp_path):
    manifest_path = _windows_l15_transport_fixture(tmp_path)
    _install_test_schemas(tmp_path)
    manifest = ab_manifest.load_manifest(manifest_path, repo_root=tmp_path)
    map_ref = manifest.platform_settings["case_transport_map"]
    map_path = tmp_path / map_ref["path"]

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t3-test@example.invalid")
    _git(tmp_path, "config", "user.name", "T3 test")
    _git(tmp_path, "config", "core.autocrlf", "true")
    _git(tmp_path, "add", "--", map_ref["path"])
    _git(tmp_path, "commit", "-q", "-m", "synthetic tool revision")
    revision = _git(tmp_path, "rev-parse", "HEAD")

    assert ab_manifest._tool_revision_input_errors(
        repo_root=tmp_path, relative=map_ref["path"], revision=revision,
        run=run_cmd, map_path=map_ref["path"],
    ) == []

    raw_lf = map_path.read_bytes()
    assert b"\r\n" not in raw_lf
    map_path.write_bytes(raw_lf.replace(b"\n", b"\r\n"))
    assert ab_manifest._tool_revision_input_errors(
        repo_root=tmp_path, relative=map_ref["path"], revision=revision,
        run=run_cmd, map_path=map_ref["path"],
    ) == [f"case transport map tool revision mismatch: {map_ref['path']}"]


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _make_committed_fake_repo(root: Path) -> tuple[Path, str]:
    manifest_path = fakes.write_ab_manifest(root, platform="linux", layer="l1.5", stage="3")
    _install_test_schemas(root)
    schema_source = Path(__file__).resolve().parent / "acceptance" / "stage3_required_cases.schema.json"
    schema_target = root / "tests" / "acceptance" / schema_source.name
    schema_target.parent.mkdir(parents=True, exist_ok=True)
    schema_target.write_bytes(schema_source.read_bytes())
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t3-test@example.invalid")
    _git(root, "config", "user.name", "T3 test")
    _git(root, "add", "-A")
    _git(root, "rm", "--cached", "-q", "--", "ab-manifest.json")
    _git(root, "commit", "-q", "-m", "trusted tool revision")
    revision = _git(root, "rev-parse", "HEAD")
    doc = _read_json(manifest_path)
    doc["contract_revision"] = _git(root, "rev-parse", f"{revision}:contracts")
    _write_json(manifest_path, doc)
    return manifest_path, revision


def _verify_with_revision(path: Path, root: Path, revision: str, *, runner=run_cmd) -> list[str]:
    _install_test_schemas(root)
    manifest = ab_manifest.load_manifest(path, repo_root=root)
    return ab_manifest.verify_frozen(
        manifest,
        repo_root=root,
        run=runner,
        tool_revision=revision,
        source_reader=_trusted_source_reader(root),
    )


def test_tool_revision_rejects_staged_new_file_used_as_frozen_selftest_report(tmp_path):
    manifest_path, revision = _make_committed_fake_repo(tmp_path)
    relative = "reports/staged-uncommitted-selftest.json"
    staged = tmp_path / relative
    staged.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", relative)
    doc = _read_json(manifest_path)
    doc["suite"]["selftest_report"] = {"path": relative, "sha256": fakes.sha256_file(staged)}
    _write_json(manifest_path, doc)
    violations = _verify_with_revision(manifest_path, tmp_path, revision)
    assert f"tool revision mismatch: {relative}" in violations


def test_tool_revision_rejects_index_tracked_file_missing_from_selected_old_revision(tmp_path):
    manifest_path, old_revision = _make_committed_fake_repo(tmp_path)
    relative = "reports/selftest-added-in-later-commit.json"
    added = tmp_path / relative
    added.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", relative)
    _git(tmp_path, "commit", "-q", "-m", "add tracked profile")
    doc = _read_json(manifest_path)
    doc["suite"]["selftest_report"] = {"path": relative, "sha256": fakes.sha256_file(added)}
    _write_json(manifest_path, doc)
    violations = _verify_with_revision(manifest_path, tmp_path, old_revision)
    assert f"tool revision mismatch: {relative}" in violations


def test_tool_revision_fails_closed_when_git_tracking_query_fails(tmp_path):
    manifest_path, revision = _make_committed_fake_repo(tmp_path)
    relative = "reports/query-failure-selftest.json"
    added = tmp_path / relative
    added.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", relative)
    doc = _read_json(manifest_path)
    doc["suite"]["selftest_report"] = {"path": relative, "sha256": fakes.sha256_file(added)}
    _write_json(manifest_path, doc)

    def fail_ls_files(argv, *, env=None, timeout=None, cwd=None):
        if "ls-files" in argv:
            return CmdResult(rc=128, stdout="", stderr="simulated Git query failure", elapsed_s=0.0,
                             timed_out=False)
        return run_cmd(argv, env=env, timeout=timeout, cwd=cwd)

    violations = _verify_with_revision(manifest_path, tmp_path, revision, runner=fail_ls_files)
    assert any("tool revision unavailable" in item and relative in item for item in violations)


def test_tool_revision_rejects_revision_that_git_cannot_resolve(tmp_path):
    _manifest_path, _revision = _make_committed_fake_repo(tmp_path)
    assert ab_manifest._tool_revision_for_checks(tmp_path, run_cmd, "f" * 40) is None


def test_tool_revision_allows_genuinely_untracked_candidate_context_artifact(tmp_path):
    manifest_path, revision = _make_committed_fake_repo(tmp_path)
    doc = _read_json(manifest_path)
    original = tmp_path / doc["candidate_context"]["path"]
    relative = "runs/generated-candidate-context.json"
    generated = tmp_path / relative
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(original.read_bytes())
    doc["candidate_context"] = {"path": relative, "sha256": fakes.sha256_file(generated)}
    _write_json(manifest_path, doc)
    assert relative not in _git(tmp_path, "ls-files")
    violations = _verify_with_revision(manifest_path, tmp_path, revision)
    assert not any("tool revision" in item and relative in item for item in violations)


def _execution_path_errors(root: Path, relative: str, revision: str) -> list[str]:
    return ab_manifest._tool_revision_input_errors(
        repo_root=root, relative=relative, revision=revision, run=run_cmd, execution_artifact=True,
    )


def test_casefold_alias_of_staged_execution_file_is_rejected(tmp_path):
    _manifest_path, revision = _make_committed_fake_repo(tmp_path)
    tracked = tmp_path / "reports" / "Tracked.json"
    tracked.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "reports/Tracked.json")

    assert _execution_path_errors(tmp_path, "reports/tracked.json", revision) == [
        "tool revision path case mismatch: reports/tracked.json (tracked as reports/Tracked.json)"
    ]


def test_casefold_alias_of_committed_execution_file_is_rejected(tmp_path):
    _manifest_path, _old_revision = _make_committed_fake_repo(tmp_path)
    tracked = tmp_path / "reports" / "Tracked.json"
    tracked.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "reports/Tracked.json")
    _git(tmp_path, "commit", "-q", "-m", "add case-sensitive report")
    revision = _git(tmp_path, "rev-parse", "HEAD")

    assert _execution_path_errors(tmp_path, "reports/tracked.json", revision) == [
        "tool revision path case mismatch: reports/tracked.json (tracked as reports/Tracked.json)"
    ]


def test_ambiguous_casefold_execution_paths_fail_closed(tmp_path):
    _manifest_path, _old_revision = _make_committed_fake_repo(tmp_path)
    _git(tmp_path, "config", "core.ignorecase", "false")
    for name in ("Tracked.json", "tracked.JSON"):
        (tmp_path / "reports" / name).write_text(name + "\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "reports/Tracked.json", "reports/tracked.JSON")
    _git(tmp_path, "commit", "-q", "-m", "add ambiguous case aliases")
    revision = _git(tmp_path, "rev-parse", "HEAD")

    assert _execution_path_errors(tmp_path, "reports/tracked.json", revision) == [
        "tool revision path case ambiguous: reports/tracked.json"
    ]


def test_correct_case_and_genuinely_untracked_execution_paths_remain_legal(tmp_path):
    _manifest_path, revision = _make_committed_fake_repo(tmp_path)
    assert _execution_path_errors(tmp_path, "reports/bridge-selftest.json", revision) == []

    untracked = tmp_path / "reports" / "untracked-report.json"
    untracked.write_text("{}\n", encoding="utf-8")
    assert _execution_path_errors(tmp_path, "reports/untracked-report.json", revision) == []


def test_repo_profile_staged_new_file_remains_tool_revision_controlled(tmp_path):
    manifest_path, revision = _make_committed_fake_repo(tmp_path)
    profile = tmp_path / "profiles" / "Private.yaml"
    profile.write_text("profile: staged\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "profiles/Private.yaml")
    doc = _read_json(manifest_path)
    doc["profile_files"] = [{
        "path": "profiles/Private.yaml",
        "sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
    }]
    _write_json(manifest_path, doc)

    assert f"tool revision mismatch: profiles/Private.yaml" in _verify_with_revision(
        manifest_path, tmp_path, revision,
    )
