"""Stage 2 RC／正式版資產驗證回歸測試。"""
from __future__ import annotations

import json
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
    assets = _assets(tmp_path)
    linux = assets / f"serialwrap-{VERSION}-linux-x86_64.tar.gz"
    linux.write_bytes(linux.read_bytes() + b"changed after rc verification")
    with pytest.raises(release_assets.ReleaseValidationError, match="SHA256"):
        # 正式 tag 使用 RC 自己的 tag commit 驗 manifest，並非要求 release tag 與 RC tag 同 commit。
        release_assets.validate_assets(
            tag="v0.5.0", version_file=ROOT / "VERSION", tag_commit=COMMIT, assets_dir=assets
        )


@pytest.mark.memory_only
def test_formal_release_can_promote_the_same_verified_rc_assets(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    before = {path.name: path.read_bytes() for path in assets.iterdir()}
    report = release_assets.validate_assets(
        tag="v0.5.0", version_file=ROOT / "VERSION", tag_commit=COMMIT, assets_dir=assets
    )
    assert report["kind"] == "release"
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == before


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
    verify_run = next(step["run"] for step in promote_steps if step.get("id") == "verify-promoted")
    publish_run = next(step["run"] for step in promote_steps if step["name"] == "Publish validated formal release")
    assert "gh release create" in create_run and "--draft" in create_run
    assert "gh release upload" in create_run and "--clobber" not in create_run
    assert "gh release download" in verify_run and "validate-assets" in verify_run
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
