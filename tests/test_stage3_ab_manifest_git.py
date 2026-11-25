"""Stage 3 T3：以隔離 Git repo 驗證原始 blob 位元組。"""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

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


def _make_real_git_fake_repo(root: Path, *, untracked_bundles: bool = False) -> tuple[Path, str]:
    """將 fake repo 的版本受控輸入提交到隔離真 Git repo。"""
    manifest_path = fakes.write_ab_manifest(root, platform="linux", layer="l1.5", stage="3")
    _install_test_schemas(root)
    schema_source = Path(__file__).resolve().parent / "acceptance" / "stage3_required_cases.schema.json"
    schema_target = root / "tests" / "acceptance" / schema_source.name
    schema_target.parent.mkdir(parents=True, exist_ok=True)
    schema_target.write_bytes(schema_source.read_bytes())

    source_tools = Path(__file__).resolve().parents[1] / "tools" / "stage3"
    target_tools = root / "tools" / "stage3"
    shutil.rmtree(target_tools)
    shutil.copytree(source_tools, target_tools, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    doc = _read_json(manifest_path)
    stage3_tree = next(item for item in doc["tools"] if item["path"] == "tools/stage3")
    stage3_tree["tree_sha256"] = fakes.tree_sha256(target_tools)
    _write_json(manifest_path, doc)

    bundle_backup = None
    if untracked_bundles:
        bundle_backup = root.parent / f"{root.name}-untracked-bundles"
        shutil.move(root / "bundles", bundle_backup)

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t3-test@example.invalid")
    _git(root, "config", "user.name", "T3 test")
    _git(root, "add", "-A")
    _git(root, "rm", "--cached", "-q", "--", "ab-manifest.json")
    _git(root, "commit", "-q", "-m", "trusted tool revision")
    if bundle_backup is not None:
        shutil.move(bundle_backup, root / "bundles")
    revision = _git(root, "rev-parse", "HEAD")
    doc["contract_revision"] = _git(root, "rev-parse", f"{revision}:contracts")
    _write_json(manifest_path, doc)
    return manifest_path, revision


def _verify_with_revision(
    path: Path, root: Path, revision: str, *, runner=run_cmd,
    executing_tools_root: Path | None = None,
) -> list[str]:
    _install_test_schemas(root)
    manifest = ab_manifest.load_manifest(path, repo_root=root)
    options = dict(
        m=manifest,
        repo_root=root,
        run=runner,
        tool_revision=revision,
        source_reader=_trusted_source_reader(root),
    )
    identity_root = Path(executing_tools_root or root)
    try:
        identity_sha = ab_manifest._tree_sha256(identity_root / "tools" / "stage3")
    except (OSError, ab_manifest.HarnessError):
        identity_sha = None
    with patch.object(ab_manifest, "_executing_tools_identity", lambda: (identity_root, identity_sha)):
        return ab_manifest.verify_frozen(**options)


def test_real_git_accepts_complete_frozen_manifest(tmp_path):
    """真 Git 的合法 manifest 會通過完整工具樹與所有凍結輸入檢查。"""
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root)
    manifest = ab_manifest.load_manifest(manifest_path, repo_root=root)
    assert _verify_with_revision(manifest_path, root, revision) == []


def test_real_git_resolves_tool_revision_from_head(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t3-test@example.invalid")
    _git(root, "config", "user.name", "T3 test")
    (root / "tracked.txt").write_text("frozen\n", encoding="utf-8")
    _git(root, "add", "--", "tracked.txt")
    _git(root, "commit", "-q", "-m", "head revision")

    assert ab_manifest._tool_revision_for_checks(root, run_cmd, None) == _git(root, "rev-parse", "HEAD")


def test_real_git_rejects_missing_frozen_tools_stage3_tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root)
    manifest = ab_manifest.load_manifest(manifest_path, repo_root=root)
    shutil.rmtree(root / "tools" / "stage3")

    violations = _verify_with_revision(manifest_path, root, revision)
    assert "tool revision tree mismatch: tools/stage3 missing tree" in violations


def test_real_git_rejects_modified_frozen_tools_stage3_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root)
    manifest = ab_manifest.load_manifest(manifest_path, repo_root=root)
    target = root / "tools" / "stage3" / "ab_manifest.py"
    target.write_bytes(target.read_bytes() + b"\n# post-commit drift\n")

    violations = _verify_with_revision(manifest_path, root, revision)
    assert "tool revision mismatch: tools/stage3/ab_manifest.py" in violations


def test_real_git_rejects_deleted_file_inside_frozen_tools_stage3_tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root)
    manifest = ab_manifest.load_manifest(manifest_path, repo_root=root)
    relative = "tools/stage3/ab_manifest.py"
    (root / relative).unlink()

    violations = _verify_with_revision(manifest_path, root, revision)
    assert f"tool revision tree mismatch: tools/stage3 missing {relative}" in violations


def test_untracked_bundle_sidecar_drift_is_rejected_when_restored_after_load(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root, untracked_bundles=True)
    top_doc = _read_json(manifest_path)
    side = top_doc["sides"][1]
    sidecar = root / side["release_manifest"]["path"]
    original_sidecar = sidecar.read_bytes()
    release = json.loads(original_sidecar.decode("utf-8"))
    launcher = root / side["bundle_dir"] / release["components"]["daemon"]["launcher"]["path"]
    launcher.write_bytes(launcher.read_bytes() + b"\n# component matches temporary sidecar\n")
    release["components"]["daemon"]["launcher"]["sha256"] = hashlib.sha256(
        launcher.read_bytes()
    ).hexdigest()
    _write_json(sidecar, release)

    try:
        manifest = ab_manifest.load_manifest(manifest_path, repo_root=root)
    except Exception as exc:
        assert "release manifest sha256 mismatch" in str(exc)
        return

    sidecar.write_bytes(original_sidecar)
    identity_sha = ab_manifest._tree_sha256(root / "tools" / "stage3")
    with patch.object(ab_manifest, "_executing_tools_identity", lambda: (root, identity_sha)):
        violations = ab_manifest.verify_frozen(
            manifest, repo_root=root, run=run_cmd, tool_revision=revision,
            source_reader=_trusted_source_reader(root),
        )
    assert any("release manifest sha256 mismatch: side B" in item for item in violations)


def _refresh_sidecar_and_manifest(root: Path, manifest_path: Path, side_index: int = 0) -> tuple[str, Path]:
    """重算指定 bundle 套件樹與 sidecar 引用，讓測試只檢驗路徑分類。"""
    top_doc = _read_json(manifest_path)
    side = top_doc["sides"][side_index]
    bundle = root / side["bundle_dir"]
    sidecar = root / side["release_manifest"]["path"]
    release = _read_json(sidecar)
    components = release["components"]
    daemon = components.get("daemon", {})
    package = daemon.get("package")
    delegate = components.get("python_cli_delegate")
    package_root = bundle / "py"
    if isinstance(package, dict):
        package["tree_sha256"] = fakes.package_tree_sha256(package_root)
    if isinstance(delegate, dict):
        delegate["tree_sha256"] = fakes.package_tree_sha256(package_root)
    _write_json(sidecar, release)
    side["release_manifest"]["sha256"] = fakes.sha256_file(sidecar)
    _write_json(manifest_path, top_doc)
    return side["bundle_dir"], package_root / "sw_core"


@pytest.mark.parametrize(
    ("relative_name", "expected"),
    [
        ("staged_extra.py", "tool revision mismatch"),
        ("éxtra.py", "tool revision path contains non-ASCII characters"),
    ],
    ids=["staged-ascii-package-member", "staged-non-ascii-package-member"],
)
def test_repo_package_tree_members_use_execution_path_classifier(tmp_path, relative_name, expected):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root, untracked_bundles=True)
    bundle_rel, package = _refresh_sidecar_and_manifest(root, manifest_path)
    member = package / relative_name
    member.write_text("# staged package member\n", encoding="utf-8")
    relative = f"{bundle_rel}/py/sw_core/{relative_name}"
    _git(root, "add", "--", relative)
    _refresh_sidecar_and_manifest(root, manifest_path)

    violations = _verify_with_revision(manifest_path, root, revision)

    assert any(expected in item and relative in item for item in violations)


def test_repo_package_tree_case_alias_of_committed_member_is_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, _revision = _make_real_git_fake_repo(root, untracked_bundles=True)
    bundle_rel, package = _refresh_sidecar_and_manifest(root, manifest_path)
    committed_name = "Committed.py"
    committed = package / committed_name
    committed.write_text("# committed package member\n", encoding="utf-8")
    committed_relative = f"{bundle_rel}/py/sw_core/{committed_name}"
    _git(root, "add", "--", committed_relative)
    _git(root, "commit", "-q", "-m", "commit package tree member")
    revision = _git(root, "rev-parse", "HEAD")

    alias_name = "committed.py"
    alias = package / alias_name
    alias.write_text("# case alias package member\n", encoding="utf-8")
    relative = f"{bundle_rel}/py/sw_core/{alias_name}"
    _refresh_sidecar_and_manifest(root, manifest_path)

    violations = _verify_with_revision(manifest_path, root, revision)

    assert f"tool revision path case mismatch: {relative} (tracked as {committed_relative})" in violations


def test_genuinely_untracked_package_tree_member_is_classified_and_allowed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root, untracked_bundles=True)
    bundle_rel, package = _refresh_sidecar_and_manifest(root, manifest_path)
    member = package / "untracked_extra.py"
    member.write_text("# untracked package member\n", encoding="utf-8")
    relative = f"{bundle_rel}/py/sw_core/untracked_extra.py"
    _refresh_sidecar_and_manifest(root, manifest_path)
    commands = []

    def tracing_runner(argv, *, env=None, timeout=None, cwd=None):
        commands.append(list(argv))
        return run_cmd(argv, env=env, timeout=timeout, cwd=cwd)

    classified = []
    original_classifier = ab_manifest._tool_revision_input_errors

    def tracing_classifier(**kwargs):
        classified.append(kwargs["relative"])
        return original_classifier(**kwargs)

    with patch.object(ab_manifest, "_tool_revision_input_errors", tracing_classifier):
        violations = _verify_with_revision(manifest_path, root, revision, runner=tracing_runner)

    assert not any("tool revision" in item and relative in item for item in violations)
    assert relative in classified
    assert any("ls-files" in command and "--" not in command for command in commands)


def test_external_bundle_members_are_not_classified_as_repo_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest_path, revision = _make_real_git_fake_repo(root, untracked_bundles=True)
    external_bundles = tmp_path / "external-bundles"
    shutil.move(root / "bundles", external_bundles)
    top_doc = _read_json(manifest_path)
    for side in top_doc["sides"]:
        bundle = external_bundles / side["label"]
        release = bundle / "release-manifest.json"
        release_doc = _read_json(release)
        side["bundle_dir"] = str(bundle)
        side["release_manifest"]["path"] = str(release)
        side["cli_exe"] = str(bundle / release_doc["components"]["cli"]["path"])
        daemon = release_doc["components"]["daemon"]
        daemon_path = daemon.get("launcher", daemon.get("runtime"))["path"]
        side["daemon_exe"] = str(bundle / daemon_path)
    _write_json(manifest_path, top_doc)
    commands = []

    def tracing_runner(argv, *, env=None, timeout=None, cwd=None):
        commands.append(list(argv))
        return run_cmd(argv, env=env, timeout=timeout, cwd=cwd)

    violations = _verify_with_revision(manifest_path, root, revision, runner=tracing_runner)

    assert not any("external-bundles" in " ".join(command) for command in commands)
    assert not any("external-bundles" in item for item in violations)


def test_func_test_checkout_preserves_git_blob_bytes_with_autocrlf_true(tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "func-test-checkout"
    repo.mkdir()
    shutil.copy2(source_root / ".gitattributes", repo / ".gitattributes")
    shutil.copytree(source_root / "func-test", repo / "func-test")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t3-test@example.invalid")
    _git(repo, "config", "user.name", "T3 test")
    _git(repo, "config", "core.autocrlf", "true")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "func-test raw bytes")
    revision = _git(repo, "rev-parse", "HEAD")
    members = _git(repo, "ls-tree", "-r", "--name-only", revision, "--", "func-test").splitlines()
    assert members

    shutil.rmtree(repo / "func-test")
    _git(repo, "checkout", "--force", revision, "--", "func-test")
    for relative in members:
        expected = _git(repo, "rev-parse", f"{revision}:{relative}")
        actual = _git(repo, "hash-object", "--no-filters", "--", relative)
        assert actual == expected, relative


@pytest.mark.parametrize("relative", ["reports/Évidence.json", "reports/évidence.json"])
def test_non_ascii_staged_execution_alias_is_rejected(tmp_path, relative):
    _manifest_path, revision = _make_committed_fake_repo(tmp_path)
    tracked = tmp_path / "reports" / "Évidence.json"
    tracked.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "reports/Évidence.json")

    violations = _execution_path_errors(tmp_path, relative, revision)
    assert violations == [f"tool revision path contains non-ASCII characters: {relative}"]


@pytest.mark.parametrize("relative", ["reports/Évidence.json", "reports/évidence.json"])
def test_non_ascii_committed_execution_alias_is_rejected(tmp_path, relative):
    _manifest_path, _old_revision = _make_committed_fake_repo(tmp_path)
    tracked = tmp_path / "reports" / "Évidence.json"
    tracked.write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "reports/Évidence.json")
    _git(tmp_path, "commit", "-q", "-m", "add non-ASCII report")
    revision = _git(tmp_path, "rev-parse", "HEAD")

    violations = _execution_path_errors(tmp_path, relative, revision)
    assert violations == [f"tool revision path contains non-ASCII characters: {relative}"]


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
