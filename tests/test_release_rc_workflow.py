"""Stage 2 RC／正式版資產驗證回歸測試。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "rust"))

import release_assets  # noqa: E402

pytestmark = [pytest.mark.posix_only]
VERSION = "0.5.0"
COMMIT = "a" * 40


def _bundle(root: Path, platform: str, *, stage: str = "rust-2", revision: str = COMMIT) -> Path:
    name = f"serialwrap-{VERSION}-{platform}-x86_64"
    version_root = root / name / "versions" / VERSION
    (version_root / "bin").mkdir(parents=True)
    manifest = {
        "bundle_version": VERSION,
        "stage": stage,
        "git_revision": revision,
        "components": {
            "cli": {"implementation": "rust", "version": VERSION},
            "daemon": {"implementation": "rust", "version": VERSION},
        },
    }
    (version_root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (version_root / "bin" / "serialwrap").write_bytes(b"cli")
    (version_root / "bin" / "serialwrapd").write_bytes(b"daemon")
    root_bin = root / name / "bin"
    root_bin.mkdir()
    if platform == "linux":
        (root_bin / "serialwrap").symlink_to(f"../versions/{VERSION}/bin/serialwrap")
        (root_bin / "serialwrapd").symlink_to(f"../versions/{VERSION}/bin/serialwrapd")
    else:
        (root_bin / "serialwrap.exe").write_bytes(b"stable-cli")
        (root_bin / "serialwrapd.exe").write_bytes(b"stable-daemon")
        (root_bin / "current-release.json").write_text(
            json.dumps({"bundle_version": VERSION, "dir": f"versions/{VERSION}"}), encoding="utf-8"
        )
    return root / name


def _assets(tmp_path: Path, *, stage: str = "rust-2", revision: str = COMMIT) -> Path:
    linux = _bundle(tmp_path / "input", "linux", stage=stage, revision=revision)
    windows = _bundle(tmp_path / "input", "windows", stage=stage, revision=revision)
    guide = tmp_path / "guide.md"
    guide.write_text("試裝說明\n", encoding="utf-8")
    out = tmp_path / "assets"
    release_assets.prepare_rc_assets(
        tag="v0.5.0-rc.1",
        version_file=ROOT / "VERSION",
        tag_commit=COMMIT,
        linux_bundle=linux,
        windows_bundle=windows,
        guide=guide,
        out_dir=out,
    )
    return out


@pytest.mark.memory_only
@pytest.mark.parametrize(
    ("tag", "kind", "rc_number"),
    [
        ("v0.5.0", "release", None),
        ("v0.5.0-rc.1", "rc", 1),
        ("v0.5.0-rc.27", "rc", 27),
    ],
)
def test_tag_parser_accepts_release_and_rc_tags(tag: str, kind: str, rc_number: int | None) -> None:
    parsed = release_assets.parse_tag(tag)
    assert (parsed.kind, parsed.version, parsed.rc_number) == (kind, VERSION, rc_number)


@pytest.mark.memory_only
@pytest.mark.parametrize("tag", ["0.5.0", "v0.5.0-rc.0", "v0.5.0-rc.01", "v0.5.0-rc.x", "v0.5.0-beta.1"])
def test_tag_parser_rejects_non_release_tag_forms(tag: str) -> None:
    with pytest.raises(release_assets.ReleaseValidationError):
        release_assets.parse_tag(tag)


@pytest.mark.memory_only
def test_rc_tag_must_match_repository_version(tmp_path: Path) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.5.1\n", encoding="utf-8")
    with pytest.raises(release_assets.ReleaseValidationError, match="VERSION"):
        release_assets.check_tag_version("v0.5.0-rc.1", version_file)


@pytest.mark.memory_only
def test_prepared_rc_assets_include_stage2_bundles_guide_and_matching_sums(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    report = release_assets.validate_assets(
        tag="v0.5.0-rc.1", version_file=ROOT / "VERSION", tag_commit=COMMIT, assets_dir=assets
    )
    assert report["kind"] == "rc"
    assert report["version"] == VERSION
    assert report["git_revision"] == COMMIT
    assert set(report["assets"]) == {
        f"serialwrap-{VERSION}-linux-x86_64.tar.gz",
        f"serialwrap-{VERSION}-windows-x86_64.zip",
        "RC-INSTALL.md",
    }


@pytest.mark.memory_only
def test_rc_asset_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    windows = assets / f"serialwrap-{VERSION}-windows-x86_64.zip"
    windows.write_bytes(windows.read_bytes() + b"tamper")
    with pytest.raises(release_assets.ReleaseValidationError, match="SHA256"):
        release_assets.validate_assets(
            tag="v0.5.0-rc.1", version_file=ROOT / "VERSION", tag_commit=COMMIT, assets_dir=assets
        )


@pytest.mark.memory_only
def test_rc_asset_with_non_stage2_manifest_is_rejected(tmp_path: Path) -> None:
    linux = _bundle(tmp_path / "input", "linux", stage="1A")
    windows = _bundle(tmp_path / "input", "windows")
    guide = tmp_path / "guide.md"
    guide.write_text("試裝說明\n", encoding="utf-8")
    with pytest.raises(release_assets.ReleaseValidationError, match="Stage 2"):
        release_assets.prepare_rc_assets(
            tag="v0.5.0-rc.1",
            version_file=ROOT / "VERSION",
            tag_commit=COMMIT,
            linux_bundle=linux,
            windows_bundle=windows,
            guide=guide,
            out_dir=tmp_path / "assets",
        )


@pytest.mark.memory_only
def test_rc_bundle_manifest_must_match_tag_commit(tmp_path: Path) -> None:
    linux = _bundle(tmp_path / "input", "linux", revision="b" * 40)
    windows = _bundle(tmp_path / "input", "windows")
    guide = tmp_path / "guide.md"
    guide.write_text("試裝說明\n", encoding="utf-8")
    with pytest.raises(release_assets.ReleaseValidationError, match="git_revision"):
        release_assets.prepare_rc_assets(
            tag="v0.5.0-rc.1",
            version_file=ROOT / "VERSION",
            tag_commit=COMMIT,
            linux_bundle=linux,
            windows_bundle=windows,
            guide=guide,
            out_dir=tmp_path / "assets",
        )


@pytest.mark.memory_only
def test_promoted_assets_must_keep_the_validated_rc_sha256(tmp_path: Path) -> None:
    rc_assets = _assets(tmp_path)
    guide = tmp_path / "release-install.md"
    guide.write_text("正式版安裝說明\n", encoding="utf-8")
    assets = tmp_path / "formal-assets"
    release_assets.prepare_formal_assets(
        rc_tag="v0.5.0-rc.1", version_file=ROOT / "VERSION", rc_commit=COMMIT,
        release_tag="v0.5.0", assets_dir=rc_assets, guide=guide, out_dir=assets,
    )
    linux = assets / f"serialwrap-{VERSION}-linux-x86_64.tar.gz"
    linux.write_bytes(linux.read_bytes() + b"changed after rc verification")
    with pytest.raises(release_assets.ReleaseValidationError, match="SHA256"):
        release_assets.validate_assets(
            tag="v0.5.0", version_file=ROOT / "VERSION", tag_commit=COMMIT, assets_dir=assets
        )


@pytest.mark.memory_only
def test_formal_release_can_promote_the_same_verified_rc_assets(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    bundle_names = {
        f"serialwrap-{VERSION}-linux-x86_64.tar.gz",
        f"serialwrap-{VERSION}-windows-x86_64.zip",
    }
    bundle_bytes = {name: (assets / name).read_bytes() for name in bundle_names}
    guide = tmp_path / "release-install.md"
    guide.write_text("正式版安裝說明，不含 RC 試裝警語。\n", encoding="utf-8")
    formal_assets = tmp_path / "formal-assets"

    report = release_assets.prepare_formal_assets(
        rc_tag="v0.5.0-rc.1",
        version_file=ROOT / "VERSION",
        rc_commit=COMMIT,
        release_tag="v0.5.0",
        assets_dir=assets,
        guide=guide,
        out_dir=formal_assets,
    )

    assert report["kind"] == "release"
    assert {path.name for path in formal_assets.iterdir()} == {*bundle_names, "RELEASE-INSTALL.md", "SHA256SUMS"}
    assert not (formal_assets / "RC-INSTALL.md").exists()
    assert (formal_assets / "RELEASE-INSTALL.md").read_text(encoding="utf-8").startswith("正式版安裝說明")
    assert {name: (formal_assets / name).read_bytes() for name in bundle_names} == bundle_bytes
    assert release_assets.validate_assets(
        tag="v0.5.0", version_file=ROOT / "VERSION", tag_commit=COMMIT, assets_dir=formal_assets
    )["kind"] == "release"


@pytest.mark.process_rpc
def test_rc_publish_tool_requires_explicit_owner_confirmation() -> None:
    tool = ROOT / "scripts" / "rust" / "publish_rc_assets.sh"
    result = subprocess.run(
        ["bash", str(tool), "--tag", "v0.5.0-rc.1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "--owner-confirmed" in result.stderr


@pytest.mark.memory_only
def test_rc_selector_returns_same_version_prereleases_newest_first() -> None:
    releases = [
        {"tagName": "v0.5.0-rc.2", "isPrerelease": True, "isDraft": False},
        {"tagName": "v0.5.0-rc.12", "isPrerelease": True, "isDraft": False},
        {"tagName": "v0.5.0-rc.3", "isPrerelease": False, "isDraft": False},
        {"tagName": "v0.4.1-rc.99", "isPrerelease": True, "isDraft": False},
        {"tagName": "v0.5.0-rc.20", "isPrerelease": True, "isDraft": True},
    ]
    assert release_assets.rc_candidates(releases, VERSION) == ["v0.5.0-rc.12", "v0.5.0-rc.2"]


@pytest.mark.memory_only
def test_workflow_separates_rc_validation_and_formal_promotion_from_build_jobs() -> None:
    with (ROOT / ".github/workflows/release.yml").open(encoding="utf-8") as handle:
        workflow = yaml.safe_load(handle)
    jobs = workflow["jobs"]
    rc_job = jobs["validate-rc-assets"]
    rc_run = "\n".join(step.get("run", "") for step in rc_job["steps"])
    assert "release_assets.py" in rc_run and "validate-assets" in rc_run
    assert "cargo build" not in rc_run and "mkbundle" not in rc_run and "gh release upload" not in rc_run
    promote = jobs["promote-rc-assets"]
    promote_steps = promote["steps"]
    create_run = next(step["run"] for step in promote_steps if step["name"].startswith("Create formal draft"))
    verify_rc_run = next(step["run"] for step in promote_steps if step.get("id") == "verify-rc")
    verify_run = next(step["run"] for step in promote_steps if step.get("id") == "verify-promoted")
    publish_run = next(step["run"] for step in promote_steps if step["name"] == "Publish validated formal release")
    assert "gh release create" in create_run and "--draft" in create_run
    assert "gh release upload" in create_run and "--clobber" not in create_run
    assert "gh release download" in verify_run and "validate-assets" in verify_run
    assert "release_promote_guard.py" in verify_rc_run
    assert "refs/tags/${TAG_NAME}^{commit}" in verify_rc_run
    assert "release_promote_guard.py" in verify_rc_run and '--rc-commit "$rc_commit"' in verify_rc_run
    assert "RC_COMMIT" in create_run
    assert "prepare-formal-assets" in create_run and "RELEASE-INSTALL.md" in create_run
    assert "RC-INSTALL.md" not in create_run
    assert "gh release edit" not in verify_run
    assert "gh release edit \"$TAG_NAME\" --draft=false" in publish_run
    assert "cargo build" not in create_run + verify_run + publish_run
    assert "mkbundle" not in create_run + verify_run + publish_run
    assert "--prerelease" not in create_run + verify_run + publish_run
    assert promote_steps.index(next(step for step in promote_steps if step["name"].startswith("Create formal draft"))) < promote_steps.index(next(step for step in promote_steps if step.get("id") == "verify-promoted")) < promote_steps.index(next(step for step in promote_steps if step["name"] == "Publish validated formal release"))


@pytest.mark.memory_only
def test_rc_publish_tool_binds_linux_bundle_to_gate_candidate_evidence() -> None:
    tool = (ROOT / "scripts" / "rust" / "publish_rc_assets.sh").read_text(encoding="utf-8")
    assert "--shared-evidence" in tool
    assert "gate_shared_evidence.py\" check-candidate-bundle" in tool
    assert "--candidate-bundle \"$LINUX_BUNDLE\"" in tool
    assert "--p7-run" in tool
    assert "p7_gate_report.verify_report" in tool
    assert "_windows_ev01_row_status" in tool
    assert "windows-ev01/bundle" in tool
    assert "指定 Windows bundle 與 P7 ev-01 gate 實際驗證的 bundle bytes 不同" in tool
    assert "結論：PASS（含 BLOCKED-OWNER）" in tool
    assert "release_gate_evidence.py" in tool
    for argument in ("--slice21-run", "--slice22-report", "--slice24-run", "--head \"$TAG_COMMIT\""):
        assert argument in tool


@pytest.mark.memory_only
def test_rc_tag_does_not_enter_any_build_or_fallback_job() -> None:
    with (ROOT / ".github/workflows/release.yml").open(encoding="utf-8") as handle:
        jobs = yaml.safe_load(handle)["jobs"]
    for name in ("project-policy", "find-rc-assets", "publish", "publish-windows-exe", "publish-linux-bundle"):
        condition = str(jobs[name].get("if", ""))
        assert "github.event_name == 'push'" in condition
        if name != "project-policy":
            assert "needs.classify-tag.outputs.kind == 'release'" in condition
    windows_bundle = str(jobs["publish-windows-bundle"].get("if", ""))
    assert "needs.classify-tag.outputs.kind == 'release'" in windows_bundle
    assert "workflow_dispatch" in windows_bundle


@pytest.mark.memory_only
def test_every_release_bundle_build_explicitly_selects_stage2() -> None:
    with (ROOT / ".github/workflows/release.yml").open(encoding="utf-8") as handle:
        jobs = yaml.safe_load(handle)["jobs"]
    linux = "\n".join(step.get("run", "") for step in jobs["publish-linux-bundle"]["steps"])
    windows = "\n".join(step.get("run", "") for step in jobs["publish-windows-bundle"]["steps"])
    assert "mkbundle.sh --release-bundle --stage 2" in linux
    assert "-Stage 2" in windows


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=測試", "-c", "user.email=test@example.invalid", "commit", "-m", message],
        cwd=repo, check=True, capture_output=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _promote_guard(repo: Path, rc_commit: str, release_commit: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/rust/release_promote_guard.py"),
            "--repo", str(repo), "--rc-commit", rc_commit, "--release-commit", release_commit,
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )


@pytest.mark.process_rpc
def test_promote_guard_requires_rc_ancestor_and_allows_only_status_or_document_changes(tmp_path: Path) -> None:
    newline = chr(10)

    def behavior_contract(status: str, title: str = "example") -> str:
        return newline.join([
            "schema_version: 1", "revision: 1", "changes:",
            '  - id: "BC-01"', f'    title: "{title}"', f'    status: "{status}"', "",
        ])

    repo = tmp_path / "repo"
    (repo / "contracts").mkdir(parents=True)
    (repo / "crates/sw-core").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "contracts/behavior_changes.yaml").write_text(behavior_contract("APPROVED"), encoding="utf-8")
    (repo / "crates/sw-core/runtime.rs").write_text("// RC bytes" + newline, encoding="utf-8")
    (repo / "docs/exit-report.md").write_text("RC gate report" + newline, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    rc_commit = _commit(repo, "建立 RC 基線")

    (repo / "contracts/behavior_changes.yaml").write_text(behavior_contract("IMPLEMENTED"), encoding="utf-8")
    (repo / "docs/exit-report.md").write_text("更新出口報告" + newline, encoding="utf-8")
    release_commit = _commit(repo, "只更新允許的狀態與文件")
    allowed = _promote_guard(repo, rc_commit, release_commit)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr

    (repo / "contracts/behavior_changes.yaml").write_text(
        behavior_contract("IMPLEMENTED", "changed executable contract"), encoding="utf-8"
    )
    metadata_commit = _commit(repo, "變更非 status 欄位")
    metadata = _promote_guard(repo, rc_commit, metadata_commit)
    assert metadata.returncode != 0

    (repo / "crates/sw-core/runtime.rs").write_text("// changed executable bytes" + newline, encoding="utf-8")
    executable_commit = _commit(repo, "改動執行檔來源")
    executable = _promote_guard(repo, rc_commit, executable_commit)
    assert executable.returncode != 0
    assert "crates/sw-core/runtime.rs" in executable.stdout + executable.stderr

    subprocess.run(["git", "checkout", "--orphan", "unrelated"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "rm", "-rf", "."], cwd=repo, check=True, capture_output=True)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/unrelated.md").write_text("另一條歷史" + newline, encoding="utf-8")
    unrelated_commit = _commit(repo, "建立無關歷史")
    unrelated = _promote_guard(repo, rc_commit, unrelated_commit)
    assert unrelated.returncode != 0


def _find_rc_run_step() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    return next(step["run"] for step in workflow["jobs"]["find-rc-assets"]["steps"] if step.get("id") == "search")


@pytest.mark.process_rpc
def test_latest_invalid_same_version_rc_fails_without_falling_back(tmp_path: Path) -> None:
    newline = chr(10)
    newest = _assets(tmp_path / "newest")
    older = _assets(tmp_path / "older")
    invalid_bundle = newest / f"serialwrap-{VERSION}-windows-x86_64.zip"
    invalid_bundle.write_bytes(invalid_bundle.read_bytes() + b"invalid newer RC")
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    shutil.copytree(newest, asset_root / "v0.5.0-rc.2")
    shutil.copytree(older, asset_root / "v0.5.0-rc.1")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    gh_log = tmp_path / "gh.log"
    fake_gh = fakebin / "gh"
    fake_gh.write_text(newline.join([
        f"#!{sys.executable}",
        "import json, os, shutil, sys",
        "from pathlib import Path",
        "args = sys.argv[1:]",
        "with open(os.environ['GH_LOG'], 'a', encoding='utf-8') as log: log.write(' '.join(args) + chr(10))",
        "if args[:2] == ['release', 'list']:",
        "    print(json.dumps([{'tagName': 'v0.5.0-rc.2', 'isPrerelease': True, 'isDraft': False}, {'tagName': 'v0.5.0-rc.1', 'isPrerelease': True, 'isDraft': False}]))",
        "elif args[:2] == ['release', 'view']:",
        "    print('true')",
        "elif args[:2] == ['release', 'download']:",
        "    destination = Path(args[args.index('--dir') + 1]); destination.mkdir(parents=True, exist_ok=True)",
        "    shutil.copytree(Path(os.environ['ASSET_ROOT']) / args[2], destination, dirs_exist_ok=True)",
        "else:",
        "    raise SystemExit(9)",
        "",
    ]), encoding="utf-8")
    fake_gh.chmod(0o755)
    fake_git = fakebin / "git"
    fake_git.write_text("#!/bin/sh" + newline + "echo " + COMMIT + newline, encoding="utf-8")
    fake_git.chmod(0o755)

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _find_rc_run_step()],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fakebin}:{os.environ['PATH']}",
            "GH_TOKEN": "test-token",
            "GH_LOG": str(gh_log),
            "ASSET_ROOT": str(asset_root),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(output),
            "TAG_NAME": "v0.5.0",
        },
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    calls = gh_log.read_text(encoding="utf-8").splitlines()
    assert any("release download v0.5.0-rc.2" in call for call in calls)
    assert not any("release download v0.5.0-rc.1" in call for call in calls), calls
    assert not output.exists() or "has_rc=true" not in output.read_text(encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + chr(10), encoding="utf-8")


@pytest.mark.process_rpc
def test_rc_gate_evidence_requires_21_22_23_24_ready_for_the_same_head(tmp_path: Path) -> None:
    newline = chr(10)
    head = COMMIT
    s21_run = tmp_path / "s21"
    s22_report = tmp_path / "s22-gate.json"
    s24_run = tmp_path / "s24"
    s24_artifacts = s24_run / "artifacts/linux"
    s24_logs = s24_run / "logs"
    s24_artifacts.mkdir(parents=True)
    s24_logs.mkdir()
    _write_json(s21_run / "report.json", {
        "git_revision": head, "dry_run": False, "verdict": "READY_FOR_OWNER_EVIDENCE",
        "identity_problems": {"missing": [], "mismatch": []},
    })
    _write_json(s22_report, {
        "result": "PASS", "dry_run": False, "identity": {"git_revision": head},
        "identity_problems": {"missing": [], "mismatch": []},
    })
    _write_json(s24_run / "slice24.json", {
        "run_dir": str(s24_run), "missing": {}, "failed": [], "ab_mismatch": [],
        "identity_errors": [], "incomplete_runs": {}, "blocked_owner": {"case:longrun": "OD-94"},
    })
    (s24_run / "slice24.md").write_text(newline.join([
        "## 判定彙總", "", "- missing IDs：0", "- failed IDs：0", "- BLOCKED-OWNER：1",
        "- A/B mismatch：0", "- identity errors：0", "- incomplete runs：0", "",
    ]), encoding="utf-8")
    _write_json(s24_artifacts / "candidate-bundle.json", {
        "schema_version": "slice24-candidate-bundle.v1", "platform": "linux",
        "bundle_tree_sha256": "b" * 64, "files": [["release-manifest.json", "c" * 64]],
    })
    (s24_run / "driver.tsv").write_text(newline.join([
        "linux-candidate-source" + chr(9) + "0", "contract-coverage" + chr(9) + "0", "",
    ]), encoding="utf-8")
    (s24_logs / "linux-candidate-source.log").write_text(f"候選 bundle git_revision={head}" + newline, encoding="utf-8")

    command = [
        sys.executable, str(ROOT / "scripts/rust/release_gate_evidence.py"), "check",
        "--repo", str(ROOT), "--head", head, "--slice21-run", str(s21_run),
        "--slice22-report", str(s22_report), "--slice24-run", str(s24_run),
    ]

    def check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)

    valid = check()
    assert valid.returncode == 0, valid.stdout + valid.stderr

    _write_json(s21_run / "report.json", {
        "git_revision": head, "dry_run": True, "verdict": "READY_FOR_OWNER_EVIDENCE",
        "identity_problems": {"missing": [], "mismatch": []},
    })
    assert check().returncode != 0
    _write_json(s21_run / "report.json", {
        "git_revision": head, "dry_run": False, "verdict": "INCOMPLETE",
        "identity_problems": {"missing": [], "mismatch": []},
    })
    assert check().returncode != 0
    _write_json(s21_run / "report.json", {"git_revision": "d" * 40, "dry_run": False, "verdict": "READY_FOR_OWNER_EVIDENCE"})
    assert check().returncode != 0
    _write_json(s21_run / "report.json", {
        "git_revision": head, "dry_run": False, "verdict": "READY_FOR_OWNER_EVIDENCE",
        "identity_problems": {"missing": [], "mismatch": []},
    })
    _write_json(s22_report, {
        "result": "PASS", "dry_run": False, "identity": {"git_revision": "d" * 40},
        "identity_problems": {"missing": [], "mismatch": []},
    })
    assert check().returncode != 0
    _write_json(s22_report, {
        "result": "PASS", "dry_run": True, "identity": {"git_revision": head},
        "identity_problems": {"missing": [], "mismatch": []},
    })
    assert check().returncode != 0
    _write_json(s22_report, {
        "result": "PASS", "dry_run": False, "identity": {"git_revision": head},
        "identity_problems": {"missing": [], "mismatch": []},
    })
    (s24_run / "driver.tsv").write_text(newline.join([
        "linux-candidate-source" + chr(9) + "0", "contract-coverage" + chr(9) + "1", "",
    ]), encoding="utf-8")
    assert check().returncode != 0
    (s24_run / "driver.tsv").write_text(newline.join([
        "linux-candidate-source" + chr(9) + "0", "contract-coverage" + chr(9) + "0", "",
    ]), encoding="utf-8")
    _write_json(s24_run / "slice24.json", {
        "run_dir": str(s24_run), "missing": {}, "failed": [], "ab_mismatch": [],
        "identity_errors": ["identity mismatch"], "incomplete_runs": {}, "blocked_owner": {},
    })
    assert check().returncode != 0
    _write_json(s24_run / "slice24.json", {
        "run_dir": str(s24_run), "missing": {}, "failed": [], "ab_mismatch": [],
        "identity_errors": [], "incomplete_runs": {}, "blocked_owner": {},
    })
    (s24_logs / "linux-candidate-source.log").write_text("候選 bundle git_revision=" + "d" * 40 + newline, encoding="utf-8")
    assert check().returncode != 0
