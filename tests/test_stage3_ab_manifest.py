"""Stage 3 T3：A/B manifest 載入、凍結與案例涵蓋紅燈測試。"""

from __future__ import annotations

import ast
import builtins
import copy
import contextlib
import hashlib
import io
import json
import os
import shutil
import threading
from collections import Counter
from dataclasses import FrozenInstanceError, is_dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Mapping
from unittest.mock import patch

import pytest
import yaml

import stage3_fakes as fakes
from tools.stage3 import ab_manifest
from tools.stage3.reason_codes import HarnessError, load_reason_codes

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

GIT_KEY = "git:rev-parse HEAD:contracts"
EXIT_WINDOWS_L15 = "S3-L15-RELEASE-AB-WINDOWS"
REQUIRED_LOCK_PATH = "tests/acceptance/stage3_required_cases.lock.json"


@pytest.fixture(autouse=True)
def _pin_seed_source_for_fake_repositories(monkeypatch):
    """讓 T1 在記憶體假 repo 中讀到已釘定的公開種子 blob。"""
    from tools.stage3 import required_cases

    monkeypatch.setattr(
        required_cases,
        "_git_show",
        lambda root, revision, repo_path: fakes._seed_source_reader(root, revision, repo_path),
    )


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _install_test_schemas(root: Path) -> None:
    """將本 checkout 的 schema 複製到隔離 repo，模擬受控版本檔。"""
    source_root = Path(__file__).resolve().parent / "acceptance"
    for name in ("ab_manifest.schema.json", "stage3_trusted_pair_map.schema.json"):
        target = root / "tests" / "acceptance" / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source_root / name).read_bytes())


def _edit_manifest(path: Path, mutate) -> dict:
    doc = _read_json(path)
    mutate(doc)
    _write_json(path, doc)
    return doc


def _runner(revision: str, docker_id: str | None = None):
    script = {GIT_KEY: [{"rc": 0, "stdout": revision + "\n", "stderr": ""}]}
    if docker_id is not None:
        script["docker:image inspect"] = [{"rc": 0, "stdout": docker_id + "\n", "stderr": ""}]
    return fakes.RecordingRunner(script)


def _load(
    path: Path, *, repo_root: Path | None = None, trusted_pair_map: Mapping | None = None,
):
    root = Path(repo_root or Path(path).parent)
    _install_test_schemas(root)
    return ab_manifest.load_manifest(
        path, repo_root=root, trusted_pair_map=trusted_pair_map,
    )


def _verify(
    manifest, repo_root: Path, *, executing_tools_root: Path | None = None,
    executing_tools_identity: tuple[Path, str | None] | None = None,
):
    runner = _ToolRevisionRecordingRunner(
        repo_root,
        _repo_snapshot(repo_root),
        contract_revision=manifest.contract_revision,
        tool_revision="1" * 40,
    )
    trusted_pair_map = (
        _synthetic_trusted_pair_map(repo_root)
        if manifest.exit_item_id == EXIT_WINDOWS_L15 else None
    )
    options = dict(
        m=manifest,
        repo_root=repo_root,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(repo_root),
        trusted_pair_map=trusted_pair_map,
    )
    identity_root = Path(executing_tools_root or repo_root)
    if executing_tools_identity is not None:
        identity_root, identity_sha = executing_tools_identity
    else:
        try:
            identity_sha = ab_manifest._tree_sha256(identity_root / "tools" / "stage3")
        except (OSError, HarnessError):
            identity_sha = None
    with patch.object(ab_manifest, "_executing_tools_identity", lambda: (identity_root, identity_sha)):
        return ab_manifest.verify_frozen(**options)


@pytest.fixture(autouse=True)
def _pin_tools_identity_for_direct_frozen_checks(monkeypatch, tmp_path):
    """假 repo 測試將私有執行身分指向該 fixture 的工具樹。"""
    def identity():
        tree = tmp_path / "tools" / "stage3"
        try:
            digest = ab_manifest._tree_sha256(tree)
        except (OSError, HarnessError):
            digest = None
        return tmp_path, digest

    monkeypatch.setattr(ab_manifest, "_executing_tools_identity", identity)


def _coverage(manifest, repo_root: Path):
    return ab_manifest.verify_case_coverage(
        manifest, repo_root=repo_root, base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(repo_root),
    )


def _refresh_file_ref(doc: dict, root: Path, field: str) -> None:
    ref = doc[field]
    ref["sha256"] = fakes.sha256_file(root / ref["path"])


def _refresh_candidate_lock_binding(doc: dict, root: Path) -> None:
    """同步更新逐出口鎖、執行前 context 與兩者在 manifest 的 FrozenFile。"""
    lock_path = root / doc["required_cases_lock"]["path"]
    lock_sha = fakes.sha256_file(lock_path)
    doc["required_cases_lock"]["sha256"] = lock_sha
    doc["required_cases_sha256"] = lock_sha
    context_ref = doc["candidate_context"]
    context_path = root / context_ref["path"]
    context = _read_json(context_path)
    context["required_cases_sha256"] = lock_sha
    _write_json(context_path, context)
    context_ref["sha256"] = fakes.sha256_file(context_path)


def _case_catalog(root: Path) -> tuple[dict, Path]:
    path = root / "tests" / "acceptance" / "cases.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")), path


def _required_ids(root: Path, exit_item_id: str, platform: str) -> set[str]:
    lock = _read_json(root / REQUIRED_LOCK_PATH)
    from tools.stage3 import required_cases

    return required_cases.required_for_exit(lock, exit_item_id, platform)


def test_frozen_manifest_has_no_violations(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    assert _verify(manifest, tmp_path) == []


def test_loaded_manifest_is_frozen_and_anchors_its_source(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    manifest = _load(path)
    assert is_dataclass(manifest)
    assert manifest.source_path == path
    assert manifest.source_sha256 == fakes.sha256_file(path)
    assert isinstance(manifest.case_list, ab_manifest.FrozenFile)
    assert isinstance(manifest.tools[0], ab_manifest.FrozenTree)
    with pytest.raises(FrozenInstanceError):
        manifest.run_id = "changed-run"
    with pytest.raises(FrozenInstanceError):
        manifest.case_list.path = "changed.json"


@pytest.mark.parametrize("operation", ["rewrite", "delete"], ids=["changed", "missing"])
def test_manifest_source_sha_is_rechecked_after_load(tmp_path, operation):
    path = fakes.write_ab_manifest(tmp_path)
    manifest = _load(path)
    if operation == "delete":
        path.unlink()
        assert f"file missing: {path.resolve()}" in _verify(manifest, tmp_path)
    else:
        doc = _read_json(path)
        doc["run_id"] = "changed-after-load"
        _write_json(path, doc)
        assert f"manifest sha256 mismatch: {path.resolve()}" in _verify(manifest, tmp_path)


def test_manifest_source_sha_uses_the_bytes_that_were_parsed(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path)
    original = path.read_bytes()
    original_doc = json.loads(original.decode("utf-8"))
    changed_doc = dict(original_doc, run_id="changed-after-source-read")
    changed = json.dumps(changed_doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    original_read_bytes = Path.read_bytes
    switched = False

    def read_then_change(candidate: Path) -> bytes:
        nonlocal switched
        data = original_read_bytes(candidate)
        if candidate.resolve() == path.resolve() and not switched:
            switched = True
            path.write_bytes(changed)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_change)
    manifest = _load(path, repo_root=tmp_path)

    assert manifest.run_id == original_doc["run_id"]
    assert manifest.source_sha256 == hashlib.sha256(original).hexdigest()


def test_stage3_source_manifest_loaded_bytes_are_cached_and_rechecked(tmp_path):
    manifest_path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(manifest_path, repo_root=tmp_path)
    source_manifest_path = (
        tmp_path / "docs" / "rust-refactor" / "evidence" / "denominators" / "source-manifest.json"
    )
    loaded_bytes = source_manifest_path.read_bytes()

    assert manifest.stage3_source_manifest_loaded_sha256 == hashlib.sha256(loaded_bytes).hexdigest()
    assert manifest.stage3_source_manifest_doc["sources"]
    files, _trees, _execution_files, _path_errors = ab_manifest._frozen_repo_paths(manifest, tmp_path)
    for source in manifest.stage3_source_manifest_doc["sources"]:
        attachment = source.get("attachment")
        if isinstance(attachment, str):
            assert f"docs/rust-refactor/evidence/{attachment}" in files

    changed_doc = json.loads(loaded_bytes.decode("utf-8"))
    changed_doc["sources"].append({"attachment": "unlisted-after-load.md"})
    _write_json(source_manifest_path, changed_doc)

    violations = _verify(manifest, tmp_path)

    assert "stage3 source-manifest sha256 mismatch" in violations


def test_release_sidecar_loaded_digest_matches_declaration_for_both_sides(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))

    assert [side.release_manifest_loaded_sha256 for side in manifest.sides] == [
        side.release_manifest.sha256 for side in manifest.sides
    ]
    assert _verify(manifest, tmp_path) == []


def test_release_sidecar_load_rejects_bytes_different_from_declared_sha(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    doc = _read_json(path)
    sidecar = tmp_path / doc["sides"][1]["release_manifest"]["path"]
    sidecar_doc = _read_json(sidecar)
    sidecar_doc["created_at"] = "2031-01-01T00:00:00Z"
    _write_json(sidecar, sidecar_doc)

    with pytest.raises(HarnessError) as caught:
        _load(path)

    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "release manifest sha256 mismatch" in caught.value.detail


def test_verify_frozen_rejects_tools_stage3_import_snapshot_from_different_content(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    tree = tmp_path / "tools" / "stage3"
    target = tree / "__init__.py"
    original = target.read_bytes()
    target.write_bytes(original + b"\n# import-time content drift\n")
    import_snapshot = ab_manifest._tree_sha256(tree)
    target.write_bytes(original)
    violations = _verify(
        manifest, tmp_path, executing_tools_identity=(tmp_path, import_snapshot),
    )

    assert any("executing tools/stage3 import snapshot mismatch" in item for item in violations)


def test_verify_frozen_rejects_disk_and_import_snapshot_that_differ_from_manifest(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    tree = tmp_path / "tools" / "stage3"
    disk_and_import = ab_manifest._tree_sha256(tree)
    tools = tuple(
        replace(item, tree_sha256="0" * 64)
        if isinstance(item, ab_manifest.FrozenTree) and item.path == "tools/stage3" else item
        for item in manifest.tools
    )
    manifest = replace(manifest, tools=tools)

    violations = _verify(manifest, tmp_path, executing_tools_identity=(tmp_path, disk_and_import))

    assert "tool tree mismatch: tools/stage3" in violations


def test_verify_frozen_accepts_equal_tools_stage3_disk_import_and_manifest(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    digest = ab_manifest._tree_sha256(tmp_path / "tools" / "stage3")
    assert _verify(manifest, tmp_path, executing_tools_identity=(tmp_path, digest)) == []


def test_load_manifest_uses_repo_root_when_manifest_is_nested(tmp_path):
    repo_root = tmp_path / "repo"
    source = fakes.write_ab_manifest(repo_root)
    nested = repo_root / "private" / "run" / "ab-manifest.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(source.read_bytes())
    manifest = _load(nested, repo_root=repo_root)
    assert manifest.source_path == nested.resolve()
    assert _verify(manifest, repo_root) == []


def test_load_manifest_uses_repo_root_when_manifest_is_outside_repo(tmp_path):
    repo_root = tmp_path / "repo"
    source = fakes.write_ab_manifest(repo_root)
    private_path = tmp_path / "private-run" / "ab-manifest.json"
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(source.read_bytes())
    manifest = _load(private_path, repo_root=repo_root)
    assert manifest.source_path == private_path.resolve()
    assert _verify(manifest, repo_root) == []


@pytest.mark.parametrize("operation", ["verify", "required_cases"])
def test_loaded_repo_root_is_shared_by_frozen_checks(tmp_path, operation):
    repo_root = tmp_path / "repo"
    path = fakes.write_ab_manifest(repo_root)
    manifest = _load(path, repo_root=repo_root)
    other_root = tmp_path / "other-repo"
    other_root.mkdir()
    if operation == "verify":
        assert ab_manifest.verify_frozen(manifest, repo_root=other_root) == [
            f"manifest repo_root mismatch: loaded {repo_root.resolve()} supplied {other_root.resolve()}"
        ]
    else:
        with pytest.raises(HarnessError, match="manifest repo_root mismatch") as caught:
            ab_manifest.required_cases(
                manifest, repo_root=other_root, base_revision="t3-trusted-base",
                source_reader=_trusted_source_reader(repo_root),
            )
        assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_case_list_drift_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    path = tmp_path / manifest.case_list.path
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    assert f"case_list sha256 mismatch: {manifest.case_list.path}" in _verify(manifest, tmp_path)


def test_case_lock_drift_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    path = tmp_path / manifest.case_lock.path
    path.write_bytes(path.read_bytes() + b"\n")
    assert f"case_lock sha256 mismatch: {manifest.case_lock.path}" in _verify(manifest, tmp_path)


@pytest.mark.parametrize(
    "source",
    ["invariants", "destructive", "expected", "case-lock"],
    ids=["invariants-copy", "destructive-copy", "expected-copy", "lock-copy"],
)
def test_case_inputs_reject_self_hashed_noncanonical_copies(tmp_path, source):
    path = fakes.write_ab_manifest(tmp_path)
    doc = _read_json(path)
    if source == "case-lock":
        original = tmp_path / doc["case_lock"]["path"]
        value = _read_json(original)
        value["sets"]["slice_2.4"].pop()
        relative = "tests/acceptance/shadow-cases.lock.json"
        copied = _write_json(tmp_path / relative, value)
        doc["case_lock"] = {"path": relative, "sha256": fakes.sha256_file(copied)}
    else:
        original = tmp_path / doc["case_list"]["path"]
        value = yaml.safe_load(original.read_text(encoding="utf-8"))
        row = value["cases"][0]
        if source == "invariants":
            row["invariants"] = []
        elif source == "destructive":
            row["destructive"] = not row.get("destructive", False)
        else:
            row["expected"] = "XFAIL" if row.get("expected") == "PASS" else "PASS"
        relative = "tests/acceptance/shadow-cases.yaml"
        copied = tmp_path / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
        doc["case_list"] = {"path": relative, "sha256": fakes.sha256_file(copied)}
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "canonical" in caught.value.detail


def test_required_cases_lock_drift_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    path = tmp_path / manifest.required_cases_lock.path
    path.write_bytes(path.read_bytes() + b"\n")
    assert f"required cases lock sha256 mismatch: {manifest.required_cases_lock.path}" in _verify(manifest, tmp_path)


def test_candidate_context_sha_is_checked_before_load(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    doc = _read_json(path)
    context_path = tmp_path / doc["candidate_context"]["path"]
    context = _read_json(context_path)
    context["candidate_round_id"] = "00000000000000000000000000000002"
    _write_json(context_path, context)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "candidate context sha256 mismatch" in caught.value.detail


@pytest.mark.parametrize("operation", ["rewrite", "delete"], ids=["changed", "missing"])
def test_candidate_context_drift_after_load_is_detected(tmp_path, operation):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    context_path = tmp_path / manifest.candidate_context.path
    if operation == "delete":
        context_path.unlink()
        assert f"file missing: {context_path}" in _verify(manifest, tmp_path)
    else:
        context = _read_json(context_path)
        context["candidate_round_id"] = "00000000000000000000000000000002"
        _write_json(context_path, context)
        assert f"candidate_context sha256 mismatch: {manifest.candidate_context.path}" in _verify(manifest, tmp_path)


def test_candidate_context_round_cannot_change_between_sides(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    assert _verify(manifest, tmp_path) == []
    context_path = tmp_path / manifest.candidate_context.path
    context = _read_json(context_path)
    context["candidate_round_id"] = "00000000000000000000000000000002"
    _write_json(context_path, context)
    violations = _verify(manifest, tmp_path)
    assert f"candidate_context sha256 mismatch: {manifest.candidate_context.path}" in violations


def test_case_list_missing_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    path = tmp_path / manifest.case_list.path
    path.unlink()
    assert f"file missing: {path}" in _verify(manifest, tmp_path)


def test_candidate_launcher_swapped_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    launcher = manifest.sides[1].bundle_dir / "bin" / "serialwrapd"
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    assert "release component sha256 mismatch: side B daemon.launcher" in _verify(manifest, tmp_path)


@pytest.mark.parametrize(
    ("component", "mutation"),
    [
        (component, mutation)
        for component in ("cli", "daemon")
        for mutation in ("missing-sha", "wrong-sha-type", "missing-path", "wrong-path-type")
    ],
    ids=[f"{component}-{mutation}" for component in ("cli", "daemon")
         for mutation in ("missing-sha", "wrong-sha-type", "missing-path", "wrong-path-type")],
)
def test_stage3_candidate_requires_typed_cli_and_daemon_component_identity(tmp_path, component, mutation):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    doc = _read_json(path)
    sidecar = tmp_path / doc["sides"][1]["release_manifest"]["path"]
    release = _read_json(sidecar)
    target = (release["components"]["cli"] if component == "cli"
              else release["components"]["daemon"]["launcher"])
    if mutation == "missing-sha":
        target.pop("sha256")
    elif mutation == "wrong-sha-type":
        target["sha256"] = 123
    elif mutation == "missing-path":
        target.pop("path")
    else:
        target["path"] = ["bin", "serialwrapd"]
    _write_json(sidecar, release)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_stage3_candidate_component_delete_is_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path, stage="3"))
    launcher = manifest.sides[1].bundle_dir / "bin" / "serialwrapd"
    launcher.unlink()
    assert f"file missing: {launcher}" in _verify(manifest, tmp_path)


@pytest.mark.parametrize(
    ("component_path", "field", "value"),
    [
        ("daemon.runtime", "sha256", None),
        ("daemon.runtime", "sha256", 17),
        ("daemon.package", "tree_sha256", None),
        ("daemon.package", "tree_sha256", "bad-tree-digest"),
        ("python_cli_delegate", "tree_sha256", None),
        ("python_cli_delegate", "tree_sha256", "bad-tree-digest"),
    ],
    ids=["runtime-sha-missing", "runtime-sha-wrong-type", "package-tree-missing", "package-tree-malformed",
         "delegate-tree-missing", "delegate-tree-malformed"],
)
def test_stage3_python_baseline_requires_typed_runtime_component_identities(
    tmp_path, component_path, field, value,
):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    doc = _read_json(path)
    sidecar = tmp_path / doc["sides"][0]["release_manifest"]["path"]
    release = _read_json(sidecar)
    target = release["components"]
    for name in component_path.split("."):
        target = target[name]
    if value is None:
        target.pop(field)
    else:
        target[field] = value
    _write_json(sidecar, release)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_baseline_python_package_tree_tamper_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    victim = manifest.sides[0].bundle_dir / "py" / "sw_core" / "cli.py"
    assert victim.is_file()
    victim.write_bytes(victim.read_bytes() + b"\n# tamper\n")
    assert "release tree mismatch: side A daemon.package" in _verify(manifest, tmp_path)


def test_baseline_python_entry_script_is_outside_package_tree(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    victim = manifest.sides[0].bundle_dir / "py" / "serialwrapd.py"
    assert victim.is_file()
    victim.write_bytes(victim.read_bytes() + b"\n# entry change\n")
    assert "release tree mismatch: side A daemon.package" not in _verify(manifest, tmp_path)


def test_trimmed_case_list_rejected(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    doc = _read_json(path)
    dropped = doc["case_ids"].pop()
    _write_json(path, doc)
    manifest = _load(path)
    assert f"required case missing: {dropped}" in _coverage(manifest, tmp_path)


def test_unknown_case_rejected(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    doc = _read_json(path)
    doc["case_ids"].append("t3-unknown-case")
    _write_json(path, doc)
    manifest = _load(path)
    assert "unknown case: t3-unknown-case" in _coverage(manifest, tmp_path)


def test_stage3_requires_python_baseline_daemon(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    doc = _read_json(path)
    baseline_manifest = tmp_path / doc["sides"][0]["release_manifest"]["path"]
    release_doc = _read_json(baseline_manifest)
    release_doc["components"]["daemon"]["implementation"] = "rust"
    _write_json(baseline_manifest, release_doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("stage", ["1A", "1B"])
def test_early_pairing_keeps_python_cli_baseline_without_release_schema_validation(tmp_path, stage):
    manifest = _load(fakes.write_ab_manifest(tmp_path, stage=stage))
    assert manifest.sides[0].manifest_doc["components"]["cli"]["implementation"] == "python"
    assert manifest.sides[1].manifest_doc["components"]["cli"]["implementation"] == "rust"
    assert all(side.manifest_doc["components"]["daemon"]["implementation"] == "python" for side in manifest.sides)


def test_stage3_bundle_is_retired_candidate_paired_to_baseline_cli(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    manifest = _load(path)
    baseline, candidate = manifest.sides
    assert baseline.manifest_doc["components"]["cli"]["sha256"] == candidate.manifest_doc["components"]["cli"]["sha256"]
    assert candidate.manifest_doc["stage"] == "3"
    assert candidate.manifest_doc["retirement"]["python_daemon"] == "removed"
    assert manifest.required_case_set == "slice_2.4"
    context = _read_json(tmp_path / manifest.candidate_context.path)
    assert context["candidate_round_id"] == "00000000000000000000000000000001"
    assert context["required_cases_sha256"] == manifest.required_cases_lock.sha256
    assert context["artifact_id"] == fakes.sha256_file(tmp_path / candidate.release_manifest.path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("exit_item_id", None), ("required_cases_lock", None)],
    ids=["missing-exit-item-id", "missing-required-cases-lock"],
)
def test_stage3_identity_and_required_lock_are_mandatory(tmp_path, field, value):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    _edit_manifest(path, lambda doc: doc.__setitem__(field, value))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_stage3_exit_item_must_match_current_platform_and_layer(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="windows", stage="3")
    _edit_manifest(path, lambda doc: doc.__setitem__("exit_item_id", "S3-L2-AB-WINDOWS"))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("field", ["cli_exe", "daemon_exe"], ids=["cli-copy", "daemon-copy"])
def test_side_executable_must_resolve_to_declared_component(tmp_path, field):
    path = fakes.write_ab_manifest(tmp_path, platform="windows", stage="3")
    doc = _read_json(path)
    side = doc["sides"][1]
    original = tmp_path / side[field]
    copied = original.with_name("copied-" + original.name)
    copied.write_bytes(original.read_bytes())
    side[field] = copied.relative_to(tmp_path).as_posix()
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_side_release_manifest_must_be_the_bundle_root_sidecar(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="windows", stage="3")
    doc = _read_json(path)
    side = doc["sides"][1]
    original = tmp_path / side["release_manifest"]["path"]
    copied = original.parent / "release-manifest.sidecar.json"
    copied.write_bytes(original.read_bytes())
    side["release_manifest"] = {
        "path": copied.relative_to(tmp_path).as_posix(),
        "sha256": fakes.sha256_file(copied),
    }
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_side_bundle_paths_must_share_one_version_root(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="windows", stage="3")
    _edit_manifest(path, lambda doc: doc["sides"][1].__setitem__("bundle_dir", "bundles/other-version"))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_windows_manifest_uses_executable_suffixes_in_declared_bundle_paths(tmp_path):
    manifest = _load(_windows_l15_transport_fixture(tmp_path))
    for side in manifest.sides:
        assert side.cli_exe.name == "serialwrap.exe"
        assert side.daemon_exe.name == "serialwrapd.exe"


@pytest.mark.parametrize("field", ["device_open_probe", "accept_unreadable_holders"])
def test_load_rejects_non_boolean_release_flags(tmp_path, field):
    path = fakes.write_ab_manifest(tmp_path)
    _edit_manifest(path, lambda doc: doc["release"].__setitem__(field, "false"))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("field", ["device_open_probe", "accept_unreadable_holders"])
def test_load_rejects_missing_release_flags(tmp_path, field):
    path = fakes.write_ab_manifest(tmp_path)
    _edit_manifest(path, lambda doc: doc["release"].pop(field))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


LOAD_REJECT_CASES = [
    ("a_not_baseline", {"sides.0.role": "candidate"}),
    ("l3_missing_acceptance", {"layer": "l3", "acceptance": None}),
    ("l3_missing_endpoint", {"layer": "l3", "endpoint": None}),
    ("l3_failure_retention_systemd", {"layer": "l3", "failure_retention": "systemd-user"}),
    ("l2_without_activate", {"layer": "l2", "sides.0.activate_argv": []}),
    ("l15_with_hardware", {"dut.kind": "hardware"}),
    ("adapter_layer_mismatch", {"suite.adapter": "l2_regression"}),
    ("docker_without_digest", {"dut.kind": "docker", "dut.image_digest": None}),
    ("cli_exe_not_component", {"sides.1.cli_exe": "/usr/bin/serialwrap"}),
    ("reverify_wrong_order", {"reverify.enabled": True, "reverify.order": ["A", "B"]}),
    ("unknown_template_key", {"suite.extra_args": ["{python}"]}),
    ("stage2_cli_differs", {"stage": "2.1", "sides.1.cli_sha_override": "0" * 64}),
    ("stage3_wrong_case_set", {"required_case_set": "slice_2.1"}),
]


def _build_manifest(tmp_path: Path, overrides: dict) -> Path:
    return fakes.write_ab_manifest(tmp_path, **overrides)


@pytest.mark.parametrize("overrides", [item[1] for item in LOAD_REJECT_CASES], ids=[item[0] for item in LOAD_REJECT_CASES])
def test_load_rejects(tmp_path, overrides):
    with pytest.raises(HarnessError) as caught:
        _load(_build_manifest(tmp_path, overrides))
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_render_argv_substitutes():
    assert ab_manifest.render_argv(["{cli_exe}", "x"], {"cli_exe": "bundles/B/bin/serialwrap"}) == [
        "bundles/B/bin/serialwrap",
        "x",
    ]


@pytest.mark.parametrize("template,values", [(["{not_allowed}"], {}), (["{cli_exe}"], {})])
def test_render_argv_rejects_unknown_or_missing_values(template, values):
    with pytest.raises(HarnessError) as caught:
        ab_manifest.render_argv(template, values)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize(
    ("field", "template"),
    [
        ("dut.reset_argv", ["{unknown_dut_key}"]),
        ("dut.fingerprint_argv", ["{unknown_fingerprint_key}"]),
        ("suite.extra_args", ["{"]),
        ("suite.extra_args", ["{cli_exe.x}"]),
        ("suite.extra_args", ["{cli_exe[0]}"]),
    ],
    ids=["unknown-reset-key", "unknown-fingerprint-key", "unmatched-brace", "attribute-access", "index-access"],
)
def test_load_rejects_invalid_argv_template_fields(tmp_path, field, template):
    path = fakes.write_ab_manifest(tmp_path, **{field: template})
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("template", [["{cli_exe.x}"], ["{cli_exe[0]}"], ["{"]],
                         ids=["attribute-access", "index-access", "unmatched-brace"])
def test_render_argv_wraps_all_template_errors(template):
    with pytest.raises(HarnessError) as caught:
        ab_manifest.render_argv(template, {"cli_exe": "/bundle/serialwrap"})
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_contract_revision_mismatch(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    other = "1" * 40 if manifest.contract_revision != "1" * 40 else "2" * 40
    runner = _ToolRevisionRecordingRunner(
        tmp_path, _repo_snapshot(tmp_path), contract_revision=other, tool_revision="1" * 40,
    )
    violations = ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
        base_revision="t3-trusted-base", source_reader=_trusted_source_reader(tmp_path),
    )
    assert f"contract_revision mismatch: manifest {manifest.contract_revision} repo {other}" in violations


def test_docker_digest_mismatch(tmp_path):
    path = fakes.write_ab_manifest(
        tmp_path,
        **{"dut.kind": "docker", "dut.image": "serialwrap-dut:test", "dut.image_digest": "sha256:" + "a" * 64},
    )
    manifest = _load(path)
    runner = _ToolRevisionRecordingRunner(
        tmp_path,
        _repo_snapshot(tmp_path),
        contract_revision=manifest.contract_revision,
        tool_revision="1" * 40,
        docker_result={"rc": 0, "stdout": "sha256:" + "b" * 64 + "\n", "stderr": ""},
    )
    assert "dut image digest mismatch" in ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )


def test_tool_file_sha_drift_detected(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    extra = tmp_path / "tools" / "t3-fixture-revision.txt"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"revision one\n")
    doc = _read_json(path)
    doc["tools"].append({"path": "tools/t3-fixture-revision.txt", "sha256": fakes.sha256_file(extra)})
    _write_json(path, doc)
    manifest = _load(path)
    extra.write_bytes(b"revision two\n")
    assert "tool sha256 mismatch: tools/t3-fixture-revision.txt" in _verify(manifest, tmp_path)


def test_tool_tree_drift(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    tool_file = tmp_path / "func-test" / "runner.py"
    tool_file.write_bytes(tool_file.read_bytes() + b"\n# drift\n")
    assert "tool tree mismatch: func-test" in _verify(manifest, tmp_path)


def test_allowed_differences_drift_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    path = tmp_path / manifest.allowed_differences.path
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    assert f"allowed_differences sha256 mismatch: {manifest.allowed_differences.path}" in _verify(manifest, tmp_path)


def test_acceptance_drift_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path, layer="l3"))
    path = tmp_path / manifest.acceptance.path
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    assert f"acceptance sha256 mismatch: {manifest.acceptance.path}" in _verify(manifest, tmp_path)


def test_profile_drift_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    profile = manifest.profile_files[0]
    path = tmp_path / profile.path
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    assert f"profile sha256 mismatch: {profile.path}" in _verify(manifest, tmp_path)


@pytest.mark.parametrize(
    ("layer", "field"),
    [("l1.5", "selftest_report"), ("l2", "testbed_template"), ("l3", "realhw_config_template")],
    ids=["selftest-report", "testbed-template", "realhw-config-template"],
)
def test_suite_input_drift_detected(tmp_path, layer, field):
    manifest = _load(fakes.write_ab_manifest(tmp_path, layer=layer))
    frozen = getattr(manifest.suite, field)
    assert frozen is not None
    path = tmp_path / frozen.path
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    assert f"suite input sha256 mismatch: {frozen.path}" in _verify(manifest, tmp_path)


@pytest.mark.parametrize("side_index", [0, 1], ids=["side-A", "side-B"])
def test_release_manifest_sidecar_drift_detected(tmp_path, side_index):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    side = manifest.sides[side_index]
    path = tmp_path / side.release_manifest.path
    release_doc = _read_json(path)
    release_doc["created_at"] = "2030-01-01T00:00:00Z"
    _write_json(path, release_doc)
    assert f"release manifest sha256 mismatch: side {side.label}" in _verify(manifest, tmp_path)


def test_release_manifest_missing_detected(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    path = tmp_path / manifest.sides[1].release_manifest.path
    path.unlink()
    assert f"file missing: {path}" in _verify(manifest, tmp_path)


def test_invariant_not_covered(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    manifest = _load(path)
    cases, _ = _case_catalog(tmp_path)
    invariant = "no_auto_resend"
    references = [
        row["case_id"]
        for row in cases["cases"]
        if row["case_id"] in manifest.case_ids and invariant in (row.get("invariants") or [])
    ]
    assert references
    _edit_manifest(path, lambda doc: doc.__setitem__("case_ids", [case_id for case_id in doc["case_ids"] if case_id not in references]))
    manifest = _load(path)
    assert f"invariant not covered: {invariant}" in _coverage(manifest, tmp_path)


def test_destructive_needs_authorization(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, layer="l2", authorization=None)
    cases, _ = _case_catalog(tmp_path)
    target = next(
        row["case_id"] for row in cases["cases"]
        if row.get("layer", "").casefold() == "l2" and row.get("destructive") is True
    )
    doc = _read_json(path)
    if target not in doc["case_ids"]:
        doc["case_ids"].append(target)
    _write_json(path, doc)
    manifest = _load(path)
    assert f"destructive case without authorization: {target}" in _coverage(manifest, tmp_path)


def test_authorization_board_must_belong_to_dut(tmp_path):
    path = fakes.write_ab_manifest(
        tmp_path,
        layer="l2",
        **{"authorization.boards": ["UNDECLARED-BOARD"]},
    )
    manifest = _load(path)
    assert "authorization board not in dut: UNDECLARED-BOARD" in _coverage(manifest, tmp_path)


def test_windows_com0com_requires_allowlist(tmp_path):
    path = fakes.write_ab_manifest(
        tmp_path,
        platform="windows",
        stage="2.4",
        **{
            "dut.transport": "com0com-pair",
            "dut.com_pair": {"daemon_side": "COM20", "bridge_side": "COM21"},
            "test_device_allowlist": ["COM20", "COM21"],
        },
    )
    manifest = replace(_load(path), test_device_allowlist=())
    assert "windows com0com instance without test_device_allowlist" in _coverage(manifest, tmp_path)


@pytest.mark.parametrize(
    ("layer", "overrides"),
    [
        ("l1.5", {"suite.selftest_report": None}),
        ("l2", {"suite.testbed_template": None}),
        ("l2", {"dut.boards": []}),
        ("l2", {"sides.1.deactivate_argv": []}),
        ("l3", {"suite.realhw_config_template": None}),
        ("l3", {"suite.tier": None}),
        ("l3", {"dut.boards": []}),
        ("l3", {"sides.1.deactivate_argv": []}),
        ("l1.5", {"dut.transport": "com0com-pair", "dut.com_pair": None}),
    ],
    ids=[
        "l15-selftest-required",
        "l2-testbed-required",
        "l2-hardware-boards-required",
        "l2-deactivate-required",
        "l3-realhw-config-required",
        "l3-tier-required",
        "l3-hardware-boards-required",
        "l3-deactivate-required",
        "com0com-pair-required",
    ],
)
def test_load_rejects_missing_layer_specific_inputs(tmp_path, layer, overrides):
    path = fakes.write_ab_manifest(tmp_path, layer=layer, **overrides)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_required_case_set_comes_from_exit_lock_not_report_tier(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, layer="l3", platform="windows", stage="3")
    locked = _required_ids(tmp_path, "S3-L3-P0P1-REMOTE-WINDOWS", "windows")
    _edit_manifest(path, lambda doc: doc["suite"].__setitem__("tier", "P0"))
    manifest = _load(path)
    required, _rows = ab_manifest.required_cases(
        manifest, repo_root=tmp_path, base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert required == locked
    assert required <= set(manifest.case_ids)


def test_non_stage3_required_cases_use_legacy_set_layer_and_platform(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path, layer="l1.5", platform="windows", stage="2.4"))
    cases_doc, _ = _case_catalog(tmp_path)
    case_lock = _read_json(tmp_path / manifest.case_lock.path)
    by_id = {row["case_id"]: row for row in cases_doc["cases"]}
    expected = {
        case_id
        for case_id in case_lock["sets"][manifest.required_case_set]
        if case_id in by_id
        and by_id[case_id].get("layer", "").casefold() == manifest.layer
        and manifest.platform in by_id[case_id].get("platforms", [])
    }
    required, _rows = _call_required_cases(manifest, tmp_path, _trusted_source_reader(tmp_path))
    assert required == expected


def test_exit_catalog_digest_is_frozen(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    manifest = _load(path)
    catalog = tmp_path / "tests" / "acceptance" / "stage_exits.json"
    catalog.write_bytes(catalog.read_bytes() + b"\n")
    violations = _verify(manifest, tmp_path)
    assert any(value.startswith("exit catalog sha256 mismatch:") for value in violations)


def test_required_case_ids_frozen_across_family_change(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    cases_path = tmp_path / doc["case_list"]["path"]
    raw = cases_path.read_bytes()
    newline = b"\r\n" if b"\r\n" in raw else b"\n"
    before = newline.join(
        (
            b"- case_id: win-com-open-exclusive",
            b"  suite: windows-native",
            b"  source_ref: func-test/scenarios/win-com-open-exclusive.yaml",
            b"  family: WIN",
            b"",
        )
    )
    after = before.replace(b"  family: WIN" + newline, b"  family: L15" + newline)
    assert raw.count(before) == 1
    cases_path.write_bytes(raw.replace(before, after, 1))
    doc["case_list"]["sha256"] = fakes.sha256_file(cases_path)
    _write_json(path, doc)
    manifest = _load(path)
    violations = _verify(manifest, tmp_path)
    assert not any(value.startswith("case_list sha256 mismatch:") for value in violations)
    assert any(value.startswith("required cases invalid:") for value in violations)


def _base_files_from(root: Path) -> dict[str, bytes]:
    paths = (
        "tests/acceptance/stage3_required_cases.lock.json",
        "tests/acceptance/cases.yaml",
        "docs/rust-refactor/evidence/denominators/source-manifest.json",
    )
    return {path: (root / path).read_bytes() for path in paths}


def _trusted_source_reader(root: Path, base_files: dict[str, bytes] | None = None):
    from tools.stage3 import required_cases

    selected = base_files or _base_files_from(fakes.REPO_ROOT)

    def read(revision: str, path: str) -> bytes | None:
        if revision == "t3-trusted-base":
            return selected.get(path)
        if revision == required_cases.SEED_REVISION:
            return fakes._seed_source_reader(root, revision, path)
        return None

    return read


def _call_required_cases(manifest, root: Path, source_reader):
    return ab_manifest.required_cases(
        manifest, repo_root=root, base_revision="t3-trusted-base", source_reader=source_reader,
    )


def test_required_cases_direct_call_rejects_family_reclassification(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    cases_path = tmp_path / doc["case_list"]["path"]
    cases_doc = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    row = next(case for case in cases_doc["cases"] if case["case_id"] == "win-com-open-exclusive")
    row["family"] = "L15"
    cases_path.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _refresh_file_ref(doc, tmp_path, "case_list")
    _write_json(path, doc)
    manifest = _load(path)
    with pytest.raises(HarnessError) as caught:
        _call_required_cases(manifest, tmp_path, _trusted_source_reader(tmp_path))
    assert caught.value.code == "HARNESS_CASE_SET_INCOMPLETE"
    assert "family" in caught.value.detail


def test_required_cases_direct_call_rejects_deleted_seed_id(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    required_lock = _read_json(tmp_path / doc["required_cases_lock"]["path"])
    required_ids = required_lock["exits"][EXIT_WINDOWS_L15]["windows"]["case_ids"]
    removed = required_ids.pop()
    _write_json(tmp_path / doc["required_cases_lock"]["path"], required_lock)
    doc["case_ids"].remove(removed)
    _refresh_candidate_lock_binding(doc, tmp_path)
    map_ref = doc["platform_settings"]["case_transport_map"]
    map_path = tmp_path / map_ref["path"]
    mapping = _read_json(map_path)
    mapping["cases"].pop(removed)
    _write_json(map_path, mapping)
    map_sha = fakes.sha256_file(map_path)
    map_ref["sha256"] = map_sha
    next(item for item in doc["tools"] if item["path"] == map_ref["path"])["sha256"] = map_sha
    doc["platform_settings"]["case_transports"].pop(removed)
    _write_json(path, doc)
    manifest = _load(path)
    with pytest.raises(HarnessError) as caught:
        _call_required_cases(manifest, tmp_path, _trusted_source_reader(tmp_path))
    assert caught.value.code == "HARNESS_CASE_SET_INCOMPLETE"
    assert "seed" in caught.value.detail


def test_required_cases_direct_call_rejects_deletion_after_prior_addition(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    added = _append_new_required_case(tmp_path, path, register_in_exit_lock=True)
    base_files = _base_files_from(tmp_path)
    doc = _read_json(path)
    cases_doc, cases_path = _case_catalog(tmp_path)
    cases_doc["cases"] = [row for row in cases_doc["cases"] if row["case_id"] != added]
    cases_path.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    case_lock_path = tmp_path / doc["case_lock"]["path"]
    case_lock = _read_json(case_lock_path)
    case_lock["sets"]["slice_2.4"].remove(added)
    case_lock["case_attrs"].pop(added)
    _write_json(case_lock_path, case_lock)
    required_lock_path = tmp_path / doc["required_cases_lock"]["path"]
    required_lock = _read_json(required_lock_path)
    required_lock["exits"][EXIT_WINDOWS_L15]["windows"]["case_ids"].remove(added)
    _write_json(required_lock_path, required_lock)
    doc["case_ids"].remove(added)
    _refresh_file_ref(doc, tmp_path, "case_list")
    _refresh_file_ref(doc, tmp_path, "case_lock")
    _refresh_candidate_lock_binding(doc, tmp_path)
    map_ref = doc["platform_settings"]["case_transport_map"]
    map_path = tmp_path / map_ref["path"]
    mapping = _read_json(map_path)
    mapping["cases"].pop(added)
    _write_json(map_path, mapping)
    map_sha = fakes.sha256_file(map_path)
    map_ref["sha256"] = map_sha
    next(item for item in doc["tools"] if item["path"] == map_ref["path"])["sha256"] = map_sha
    doc["platform_settings"]["case_transports"].pop(added)
    _write_json(path, doc)
    manifest = _load(path)
    with pytest.raises(HarnessError) as caught:
        _call_required_cases(manifest, tmp_path, _trusted_source_reader(tmp_path, base_files))
    assert caught.value.code == "HARNESS_CASE_SET_INCOMPLETE"
    assert "不可刪除既有案例" in caught.value.detail


def test_required_cases_direct_call_accepts_legal_add_only_expansion(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    added = _append_new_required_case(tmp_path, path, register_in_exit_lock=True)
    manifest = _load(path)
    required, _rows = _call_required_cases(manifest, tmp_path, _trusted_source_reader(tmp_path))
    assert added in required


def test_required_case_incompleteness_uses_existing_reason_code():
    assert "HARNESS_CASE_SET_INCOMPLETE" in load_reason_codes()


def _append_new_required_case(root: Path, manifest_path: Path, *, register_in_exit_lock: bool) -> str:
    """同步目前目錄與 cases.lock；可選擇將新 ID 加入逐出口分母鎖。"""
    from tools.stage3 import required_cases

    doc = _read_json(manifest_path)
    cases_doc, cases_path = _case_catalog(root)
    source = next(
        row for row in cases_doc["cases"]
        if row.get("layer", "").casefold() == "l1.5"
        and row.get("necessity") == "required"
        and "windows" in row.get("platforms", [])
        and row.get("suite") == "func-test"
    )
    new_id = "t3-added-required-windows-case"
    assert all(row.get("case_id") != new_id for row in cases_doc["cases"])
    new_case = copy.deepcopy(source)
    new_case["case_id"] = new_id
    new_case["platforms"] = ["windows"]
    cases_doc["cases"].append(new_case)
    cases_path.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")

    case_lock_path = root / doc["case_lock"]["path"]
    case_lock = _read_json(case_lock_path)
    case_lock["sets"]["slice_2.4"].append(new_id)
    case_lock["sets"]["slice_2.4"].sort()
    case_lock["case_attrs"][new_id] = {
        key: new_case.get(key) for key in required_cases.CASE_ATTR_KEYS
    }
    _write_json(case_lock_path, case_lock)

    _refresh_file_ref(doc, root, "case_list")
    _refresh_file_ref(doc, root, "case_lock")
    if register_in_exit_lock:
        required_lock_path = root / doc["required_cases_lock"]["path"]
        required_lock = _read_json(required_lock_path)
        locked_ids = required_lock["exits"][EXIT_WINDOWS_L15]["windows"]["case_ids"]
        before = set(locked_ids)
        locked_ids.append(new_id)
        locked_ids.sort()
        assert before <= set(locked_ids)
        _write_json(required_lock_path, required_lock)
        doc["case_ids"] = sorted(set(doc["case_ids"]) | {new_id})
        _refresh_candidate_lock_binding(doc, root)
        map_ref = doc.get("platform_settings", {}).get("case_transport_map")
        if isinstance(map_ref, dict):
            map_path = root / map_ref["path"]
            transport_map = _read_json(map_path)
            transport_map["cases"][new_id] = {
                "requires_native_com": False,
                "runner": new_case["suite"],
                "platform": "windows",
                "allowed_transports": ["local-process"],
                "pair_id": None,
            }
            doc["platform_settings"]["case_transports"][new_id] = {
                "transport": "local-process",
                "port_id": None,
                "decision_ref": None,
            }
            _write_json(map_path, transport_map)
            map_sha = fakes.sha256_file(map_path)
            map_ref["sha256"] = map_sha
            for tool in doc["tools"]:
                if tool.get("path") == map_ref["path"] and "sha256" in tool:
                    tool["sha256"] = map_sha
    _write_json(manifest_path, doc)
    return new_id


def test_required_case_lock_rejects_new_applicable_case_until_registered(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    _append_new_required_case(tmp_path, path, register_in_exit_lock=False)
    manifest = _load(path)
    violations = _verify(manifest, tmp_path)
    assert any(value.startswith("required cases invalid:") for value in violations)


def test_required_case_lock_allows_add_only_case_with_complete_coverage(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    prior = _required_ids(tmp_path, EXIT_WINDOWS_L15, "windows")
    added = _append_new_required_case(tmp_path, path, register_in_exit_lock=True)
    manifest = _load(path)
    current = _required_ids(tmp_path, EXIT_WINDOWS_L15, "windows")
    assert prior < current
    assert added in current
    assert current <= set(manifest.case_ids)
    assert _verify(manifest, tmp_path) == []


@pytest.mark.parametrize(
    "mutation",
    ["missing-lock", "wrong-lock-sha", "context-points-to-other-lock"],
    ids=["missing-lock", "wrong-lock-sha", "context-other-lock-sha"],
)
def test_required_case_lock_context_binding_rejects_mismatch(tmp_path, mutation):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    doc = _read_json(path)
    if mutation == "missing-lock":
        doc["required_cases_lock"] = None
    elif mutation == "wrong-lock-sha":
        doc["required_cases_lock"]["sha256"] = "0" * 64
    else:
        other_lock_path = tmp_path / "tests" / "acceptance" / "stage3_required_cases.other.json"
        current_lock_path = tmp_path / doc["required_cases_lock"]["path"]
        other_lock_path.write_bytes(current_lock_path.read_bytes() + b"\n")
        context_path = tmp_path / doc["candidate_context"]["path"]
        context = _read_json(context_path)
        context["required_cases_sha256"] = fakes.sha256_file(other_lock_path)
        _write_json(context_path, context)
        doc["candidate_context"]["sha256"] = fakes.sha256_file(context_path)
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_tampered_required_lock_cannot_delete_seeded_id(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    lock_path = tmp_path / doc["required_cases_lock"]["path"]
    lock = _read_json(lock_path)
    locked_ids = lock["exits"][EXIT_WINDOWS_L15]["windows"]["case_ids"]
    old_id = "win-com-open-exclusive"
    assert old_id in locked_ids
    locked_ids.remove(old_id)
    doc["case_ids"].remove(old_id)
    _write_json(lock_path, lock)
    map_ref = doc["platform_settings"]["case_transport_map"]
    map_path = tmp_path / map_ref["path"]
    transport_map = _read_json(map_path)
    transport_map["cases"].pop(old_id)
    doc["platform_settings"]["case_transports"].pop(old_id)
    _write_json(map_path, transport_map)
    map_sha = fakes.sha256_file(map_path)
    map_ref["sha256"] = map_sha
    for tool in doc["tools"]:
        if tool.get("path") == map_ref["path"] and "sha256" in tool:
            tool["sha256"] = map_sha
    _refresh_candidate_lock_binding(doc, tmp_path)
    _write_json(path, doc)
    manifest = _load(path)
    violations = _verify(manifest, tmp_path)
    assert any(value.startswith("required cases invalid:") for value in violations)


def test_runtime_digest_ignores_stage_version_created_at_and_retirement(tmp_path):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="2.4")
    original = _read_json(bundle / "release-manifest.json")
    changed = copy.deepcopy(original)
    changed["stage"] = "3"
    changed["bundle_version"] = "1.0.0"
    changed["created_at"] = "2030-01-01T00:00:00Z"
    changed["components"]["cli"]["version"] = "1.0.0"
    changed["components"]["daemon"]["version"] = "1.0.0"
    changed["pairing"]["cli_version"] = "1.0.0"
    changed["pairing"]["daemon_version"] = "1.0.0"
    changed["retirement"] = {"python_daemon": "removed"}
    assert ab_manifest.runtime_digest(original) == ab_manifest.runtime_digest(changed)


def test_runtime_digest_includes_actual_nonretired_runtime_and_package_components(tmp_path):
    nonretired = fakes.make_bundle(tmp_path / "nonretired", stage="2.4")
    retired = fakes.make_bundle(tmp_path / "retired", stage="3")
    nonretired_doc = _read_json(nonretired / "release-manifest.json")
    retired_doc = _read_json(retired / "release-manifest.json")
    assert "runtime" in nonretired_doc["components"]["daemon"]
    assert "package" in nonretired_doc["components"]["daemon"]
    assert "runtime" not in retired_doc["components"]["daemon"]
    assert "package" not in retired_doc["components"]["daemon"]
    assert ab_manifest.runtime_digest(nonretired_doc) != ab_manifest.runtime_digest(retired_doc)


def test_runtime_digest_changes_when_cli_binary_changes(tmp_path):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="2.4")
    manifest_doc = _read_json(bundle / "release-manifest.json")
    before = ab_manifest.runtime_digest(manifest_doc)
    cli = bundle / manifest_doc["components"]["cli"]["path"]
    cli.write_bytes(cli.read_bytes() + b"\n# different CLI\n")
    changed = copy.deepcopy(manifest_doc)
    changed["components"]["cli"]["sha256"] = fakes.sha256_file(cli)
    assert ab_manifest.runtime_digest(changed) != before


def test_verify_bundle_strings_match_verify_frozen(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    bundle = manifest.sides[1].bundle_dir
    launcher = bundle / "bin" / "serialwrapd"
    launcher.write_bytes(b"#!/bin/sh\nexit 1\n")
    expected = "release component sha256 mismatch: side B daemon.launcher"
    assert ab_manifest.verify_bundle(bundle, manifest.sides[1].manifest_doc, label="side B") == [expected]
    assert expected in _verify(manifest, tmp_path)


@pytest.mark.parametrize(
    ("platform", "executable"),
    [("linux", "serialwrap"), ("windows", "serialwrap.exe")],
    ids=["posix-name", "windows-extension"],
)
def test_verify_bundle_missing_cli(tmp_path, platform, executable):
    bundle = fakes.make_bundle(
        tmp_path / "bundle",
        stage="3",
        target="x86_64-pc-windows-gnu" if platform == "windows" else "x86_64-unknown-linux-gnu",
    )
    manifest_doc = _read_json(bundle / "release-manifest.json")
    cli = bundle / "bin" / executable
    cli.unlink()
    assert ab_manifest.verify_bundle(bundle, manifest_doc, label="side A") == [f"file missing: {cli}"]


@pytest.mark.parametrize("path_kind", ["relative", "absolute"], ids=["relative-escape", "absolute-path"])
def test_verify_bundle_path_escape(tmp_path, path_kind):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="3")
    manifest_doc = _read_json(bundle / "release-manifest.json")
    manifest_doc["components"]["cli"]["path"] = (
        "../outside" if path_kind == "relative" else str((tmp_path / "outside").resolve())
    )
    assert "release component path escapes bundle: side A cli" in ab_manifest.verify_bundle(
        bundle,
        manifest_doc,
        label="side A",
    )


def test_verify_bundle_reports_python_package_and_delegate_tree_mismatch(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path))
    baseline = manifest.sides[0]
    victim = baseline.bundle_dir / "py" / "sw_core" / "cli.py"
    victim.write_bytes(victim.read_bytes() + b"\n# tree change\n")
    violations = ab_manifest.verify_bundle(baseline.bundle_dir, baseline.manifest_doc, label="side A")
    assert "release tree mismatch: side A daemon.package" in violations
    assert "release tree mismatch: side A python_cli_delegate" in violations


def test_verify_bundle_reads_a_shared_package_tree_member_once(tmp_path, monkeypatch):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="2.4")
    manifest_doc = _read_json(bundle / "release-manifest.json")
    target = bundle / "py" / "sw_core" / "cli.py"
    original_open = Path.open
    reads = 0

    def counting_open(path, *args, **kwargs):
        nonlocal reads
        mode = args[0] if args else kwargs.get("mode")
        if Path(path).resolve() == target.resolve() and mode == "rb":
            reads += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    assert ab_manifest.verify_bundle(bundle, manifest_doc, label="side A") == []
    assert reads == 1


def test_bundle_tree_sha256_covers_every_file_and_sorts_paths(tmp_path):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="3")
    skill = bundle / "assets" / "skill" / "SKILL.md"
    pyc = bundle / "bin" / "__pycache__" / "x.pyc"
    sums = bundle / "SHA256SUMS"

    def add_skill():
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_bytes(b"v1\n")

    def change_skill():
        skill.write_bytes(b"v2\n")

    def remove_skill():
        skill.unlink()

    def add_pyc():
        pyc.parent.mkdir(parents=True, exist_ok=True)
        pyc.write_bytes(b"\x00pyc")

    def change_sums():
        sums.write_bytes(sums.read_bytes() + b"# edited\n")

    previous = ab_manifest.bundle_tree_sha256(bundle)
    for mutation in (add_skill, change_skill, remove_skill, add_pyc, change_sums):
        mutation()
        current = ab_manifest.bundle_tree_sha256(bundle)
        assert current != previous, f"digest 未因 {mutation.__name__} 改變"
        previous = current

    content = {"a.txt": b"A", "b.txt": b"B", "sub/c.txt": b"C"}
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, order in (
        (first, ["a.txt", "sub/c.txt", "b.txt"]),
        (second, ["b.txt", "sub/c.txt", "a.txt"]),
    ):
        for relative in order:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content[relative])
    assert ab_manifest.bundle_tree_sha256(first) == ab_manifest.bundle_tree_sha256(second)


def test_runtime_tree_sha256_ignores_only_root_manifest_metadata(tmp_path):
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="3")
    manifest_path = bundle / "release-manifest.json"
    sums_path = bundle / "SHA256SUMS"
    original_manifest = manifest_path.read_bytes()
    original_sums = sums_path.read_bytes()
    manifest_doc = json.loads(original_manifest.decode("utf-8"))
    digest_before = ab_manifest.runtime_digest(manifest_doc)
    runtime_before = ab_manifest.runtime_tree_sha256(bundle)
    bundle_before = ab_manifest.bundle_tree_sha256(bundle)

    changed = copy.deepcopy(manifest_doc)
    changed["created_at"] = "2030-01-01T00:00:00Z"
    changed["stage"] = "2.4"
    manifest_path.write_bytes((json.dumps(changed, sort_keys=True) + "\n").encode("utf-8"))
    sums_path.write_bytes(b"0" * 64 + b"  changed\n")
    assert ab_manifest.runtime_tree_sha256(bundle) == runtime_before
    assert ab_manifest.bundle_tree_sha256(bundle) != bundle_before

    manifest_path.write_bytes(original_manifest)
    sums_path.write_bytes(original_sums)
    sidecar = bundle / "bin" / "version.dll"
    sidecar.write_bytes(b"MZ")
    assert ab_manifest.verify_bundle(bundle, manifest_doc, label="side B") == []
    assert ab_manifest.runtime_tree_sha256(bundle) != runtime_before
    assert ab_manifest.bundle_tree_sha256(bundle) != bundle_before
    assert ab_manifest.runtime_digest(manifest_doc) == digest_before
    runtime_after_sidecar = ab_manifest.runtime_tree_sha256(bundle)
    (bundle / "bin" / "SHA256SUMS").write_bytes(b"nested\n")
    assert ab_manifest.runtime_tree_sha256(bundle) != runtime_after_sidecar


def _native_com_required(case_id: str, row: dict) -> bool:
    """僅供測試，不是權威規則：合成映射依 cases.yaml 的 capabilities 是否含 windows_com 判定。

    權威映射由後續 Task 依同一來源建立，不得以案例 ID 前綴推斷是否需要 COM。
    """
    del case_id
    return "windows_com" in (row.get("capabilities") or ())


def _windows_l15_transport_fixture(root: Path, *, transport: str = "windows-native-com") -> Path:
    """T3 專用正向建構：映射凍結於 tools，逐案 ID 完整對齊 T1 的 Windows 出口鎖。"""
    manifest_path = fakes.write_ab_manifest(root, platform="windows", layer="l1.5", stage="3")
    doc = _read_json(manifest_path)
    cases_doc, _ = _case_catalog(root)
    case_rows = {row["case_id"]: row for row in cases_doc["cases"]}
    required = set(doc["case_ids"])
    mapping_cases = {}
    case_transports = {}
    for case_id in sorted(required):
        row = case_rows[case_id]
        needs_com = _native_com_required(case_id, row)
        allowed = ["windows-native-com", "od08-physical"] if needs_com else ["local-process"]
        mapping_cases[case_id] = {
            "requires_native_com": needs_com,
            "runner": row["suite"],
            "platform": "windows",
            "allowed_transports": allowed,
            "pair_id": "T3-COM0COM-PAIR" if needs_com else None,
        }
        selected = transport if needs_com else "local-process"
        case_transports[case_id] = {
            "transport": selected,
            "port_id": "T3-PAIR-DUT" if needs_com and selected == "windows-native-com" else (
                "T3-PHYSICAL-DUT" if needs_com else None
            ),
            "decision_ref": "OD-08-T3-APPROVED" if needs_com and selected == "od08-physical" else (
                "T3-COM-PAIR-AUTH" if needs_com else None
            ),
        }
    mapping_doc = {
        "schema_version": 1,
        "exit_item_id": EXIT_WINDOWS_L15,
        "platform": "windows",
        "cases": mapping_cases,
        "approved_pairs": {
            "T3-COM0COM-PAIR": {
                "dut_side_port_id": "T3-PAIR-DUT",
                "daemon_side_port_id": "T3-PAIR-DAEMON",
                "decision_ref": "T3-COM-PAIR-AUTH",
            }
        },
        "physical_substitutes": {
            "T3-PHYSICAL-DUT": {
                "platform": "windows",
                "decision_ref": "OD-08-T3-APPROVED",
            }
        },
    }
    mapping_path = root / "tests" / "acceptance" / "windows_l15_case_transport_map.json"
    _write_json(mapping_path, mapping_doc)
    frozen_map = {
        "path": mapping_path.relative_to(root).as_posix(),
        "sha256": fakes.sha256_file(mapping_path),
    }
    doc["platform_settings"] = {
        "case_transport_map": frozen_map,
        "case_transports": case_transports,
    }
    doc["tools"].append(dict(frozen_map))
    if transport == "od08-physical":
        doc["dut"]["kind"] = "physical-substitute"
        doc["dut"]["transport"] = "physical-usb-uart"
        doc["dut"]["boards"] = ["T3-DUT"]
        doc["authorization"] = {
            "allow_destructive": True,
            "decision_ref": "OD-08-T3-APPROVED",
            "boards": ["T3-DUT"],
        }
    else:
        doc["dut"]["kind"] = "local-process"
        doc["dut"]["transport"] = "com0com-pair"
        doc["dut"]["com_pair"] = {"daemon_side": "COM21", "bridge_side": "COM20"}
        doc["test_device_allowlist"] = ["COM20", "COM21"]
        doc["authorization"] = {
            "allow_destructive": True,
            "decision_ref": "T3-COM-PAIR-AUTH",
            "boards": [],
        }
    _write_json(manifest_path, doc)
    return manifest_path


@pytest.mark.parametrize("transport", ["windows-native-com", "od08-physical"], ids=["approved-pair", "od08-substitute"])
def test_windows_l15_manifest_accepts_native_or_od08_transport_and_full_denominator(tmp_path, transport):
    manifest = _load(_windows_l15_transport_fixture(tmp_path, transport=transport))
    map_ref = manifest.platform_settings["case_transport_map"]
    assert manifest.case_transport_map_loaded_sha256 == map_ref["sha256"]
    assert manifest.case_transport_map_doc is not None
    locked = _required_ids(tmp_path, EXIT_WINDOWS_L15, "windows")
    assert set(manifest.case_ids) == locked
    required, _rows = _call_required_cases(manifest, tmp_path, _trusted_source_reader(tmp_path))
    assert required == locked
    native_ids = {case_id for case_id, row in _windows_map_rows(tmp_path).items() if row["requires_native_com"]}
    mapping_doc = _read_json(tmp_path / manifest.platform_settings["case_transport_map"]["path"])
    assert native_ids
    for case_id in native_ids:
        transport_record = manifest.platform_settings["case_transports"][case_id]
        assert transport_record["transport"] == transport
        assert transport_record["port_id"]
        assert transport_record["decision_ref"]
        if transport == "windows-native-com":
            case_map = mapping_doc["cases"][case_id]
            pair = mapping_doc["approved_pairs"][case_map["pair_id"]]
            assert transport_record["port_id"] == pair["dut_side_port_id"]
    non_com_id = next(case_id for case_id in manifest.case_ids if case_id not in native_ids)
    non_com = manifest.platform_settings["case_transports"][non_com_id]
    assert non_com == {"transport": "local-process", "port_id": None, "decision_ref": None}
    if transport == "windows-native-com":
        assert manifest.dut.com_pair == {"daemon_side": "COM21", "bridge_side": "COM20"}
        assert set(manifest.test_device_allowlist) == {"COM20", "COM21"}
        assert manifest.authorization.decision_ref == "T3-COM-PAIR-AUTH"
    else:
        assert manifest.dut.kind == "physical-substitute"
        assert manifest.dut.transport == "physical-usb-uart"
        assert manifest.authorization.decision_ref == "OD-08-T3-APPROVED"
        assert manifest.authorization.boards == manifest.dut.boards


def _windows_map_rows(root: Path) -> dict:
    manifest_doc = _read_json(root / "ab-manifest.json")
    map_path = root / manifest_doc["platform_settings"]["case_transport_map"]["path"]
    return _read_json(map_path)["cases"]


def test_windows_l15_manifest_rejects_map_bytes_different_from_declared_sha(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    manifest_doc = _read_json(path)
    map_ref = manifest_doc["platform_settings"]["case_transport_map"]
    map_path = tmp_path / map_ref["path"]
    mapping = _read_json(map_path)
    first_case = next(iter(mapping["cases"].values()))
    first_case["allowed_transports"].append("docker")
    _write_json(map_path, mapping)

    with pytest.raises(HarnessError) as caught:
        _load(path)

    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "case transport map sha256 mismatch" in caught.value.detail


@pytest.mark.parametrize(
    "bad_transport",
    ["docker", "pty", "local-process"],
    ids=["docker", "pty", "local-process"],
)
def test_windows_l15_manifest_rejects_non_native_transport(tmp_path, bad_transport):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    native_id = next(case_id for case_id, row in _windows_map_rows(tmp_path).items() if row["requires_native_com"])
    doc["platform_settings"]["case_transports"][native_id]["transport"] = bad_transport
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize(
    "missing",
    ["transport", "case_transports", "case_transport_map", "port_id", "decision_ref"],
    ids=["transport-field", "outer-windows-only", "missing-frozen-map", "missing-port-id", "missing-decision-ref"],
)
def test_windows_l15_manifest_rejects_missing_transport_context(tmp_path, missing):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    native_id = next(case_id for case_id, row in _windows_map_rows(tmp_path).items() if row["requires_native_com"])
    if missing == "transport":
        doc["platform_settings"]["case_transports"][native_id].pop("transport")
    elif missing == "case_transports":
        doc["platform_settings"].pop("case_transports")
    elif missing == "case_transport_map":
        doc["platform_settings"].pop("case_transport_map")
    elif missing == "port_id":
        doc["platform_settings"]["case_transports"][native_id].pop("port_id")
    else:
        doc["platform_settings"]["case_transports"][native_id].pop("decision_ref")
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("shape", ["missing", "extra"], ids=["missing-id", "extra-id"])
def test_windows_l15_manifest_rejects_non_exact_case_ids(tmp_path, shape):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    if shape == "missing":
        doc["platform_settings"]["case_transports"].pop(doc["case_ids"][-1])
    else:
        doc["platform_settings"]["case_transports"]["t3-unknown-case"] = {
            "transport": "local-process",
            "port_id": None,
            "decision_ref": None,
        }
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_windows_l15_manifest_rejects_map_tool_hash_mismatch(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_path = doc["platform_settings"]["case_transport_map"]["path"]
    tool = next(item for item in doc["tools"] if item["path"] == map_path)
    tool["sha256"] = "f" * 64
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "case_transport_map sha256 must match" in caught.value.detail


def test_windows_l15_manifest_requires_map_in_frozen_tools(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_path = doc["platform_settings"]["case_transport_map"]["path"]
    doc["tools"] = [item for item in doc["tools"] if item["path"] != map_path]
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_windows_l15_manifest_rejects_mapping_reference_hash_mismatch(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    doc["platform_settings"]["case_transport_map"]["sha256"] = "f" * 64
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_windows_l15_manifest_rejects_reference_and_tool_sha_disagreement(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_ref = doc["platform_settings"]["case_transport_map"]
    tool = next(item for item in doc["tools"] if item["path"] == map_ref["path"])
    tool["sha256"] = "f" * 64
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_windows_l15_mapping_cannot_replace_the_tool_revision_with_self_filled_shas(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_ref = doc["platform_settings"]["case_transport_map"]
    map_path = tmp_path / map_ref["path"]
    revision_bytes = map_path.read_bytes()
    revision_blobs = _repo_snapshot(tmp_path)
    mapping = json.loads(revision_bytes)
    native_id = next(case_id for case_id, row in mapping["cases"].items() if row["requires_native_com"])
    mapping["cases"][native_id]["requires_native_com"] = False
    mapping["cases"][native_id]["allowed_transports"] = ["local-process"]
    mapping["cases"][native_id]["pair_id"] = None
    doc["platform_settings"]["case_transports"][native_id] = {
        "transport": "local-process", "port_id": None, "decision_ref": None,
    }
    _write_json(map_path, mapping)
    map_sha = fakes.sha256_file(map_path)
    map_ref["sha256"] = map_sha
    next(item for item in doc["tools"] if item["path"] == map_ref["path"])["sha256"] = map_sha
    _write_json(path, doc)

    manifest = _load(path)
    runner = _ToolRevisionRecordingRunner(tmp_path, revision_blobs)
    violations = ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
    )
    assert any("case transport map" in value and "tool revision" in value for value in violations)


def test_windows_l15_manifest_rejects_changed_frozen_com_requirement_during_load(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_path = tmp_path / doc["platform_settings"]["case_transport_map"]["path"]
    mapping = _read_json(map_path)
    native_id = next(case_id for case_id, row in mapping["cases"].items() if row["requires_native_com"])
    mapping["cases"][native_id]["requires_native_com"] = False
    _write_json(map_path, mapping)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "case transport map sha256 mismatch" in caught.value.detail


def test_windows_l15_manifest_rejects_mismatched_com_pair_and_allowlist(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    doc["dut"]["com_pair"]["daemon_side"] = "COM99"
    doc["test_device_allowlist"] = ["COM20"]
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("port_id", ["UNRELATED-PORT-ID", "T3-OTHER-PAIR-DUT-ID"],
                         ids=["arbitrary-id", "other-approved-pair-id"])
def test_windows_l15_manifest_rejects_port_id_not_from_its_approved_pair(tmp_path, port_id):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_ref = doc["platform_settings"]["case_transport_map"]
    map_path = tmp_path / map_ref["path"]
    mapping = _read_json(map_path)
    native_id = next(case_id for case_id, row in mapping["cases"].items() if row["requires_native_com"])
    if port_id == "T3-OTHER-PAIR-DUT-ID":
        mapping["approved_pairs"]["T3-OTHER-PAIR"] = {
            "dut_side_port_id": "T3-OTHER-PAIR-DUT-ID",
            "daemon_side_port_id": "T3-OTHER-PAIR-DAEMON-ID",
            "decision_ref": "T3-OTHER-PAIR-AUTH",
        }
    _write_json(map_path, mapping)
    digest = fakes.sha256_file(map_path)
    map_ref["sha256"] = digest
    next(item for item in doc["tools"] if item["path"] == map_ref["path"])["sha256"] = digest
    doc["platform_settings"]["case_transports"][native_id]["port_id"] = port_id
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_windows_l15_manifest_rejects_missing_od08_authorization(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path, transport="od08-physical")
    _edit_manifest(path, lambda doc: doc.__setitem__("authorization", None))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize(
    "authorization",
    [None, {"allow_destructive": False, "decision_ref": None, "boards": []}],
    ids=["missing", "not-approved"],
)
def test_windows_l15_manifest_rejects_unapproved_pair(tmp_path, authorization):
    path = _windows_l15_transport_fixture(tmp_path)
    _edit_manifest(path, lambda doc: doc.__setitem__("authorization", authorization))
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_non_windows_l15_manifest_does_not_require_transport_map(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3"))
    assert manifest.platform_settings.get("case_transport_map") is None
    assert manifest.platform_settings.get("case_transports") is None


def test_windows_non_l15_exit_does_not_require_l15_transport_map(tmp_path):
    manifest = _load(fakes.write_ab_manifest(tmp_path, platform="windows", layer="l2", stage="3"))
    assert manifest.exit_item_id == "S3-L2-AB-WINDOWS"
    assert manifest.platform_settings.get("case_transport_map") is None


def _git_blob_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _git_tree_oid(members: list[str]) -> str:
    payload = "\0".join(sorted(members)).encode("utf-8")
    return hashlib.sha1(b"tree-members\0" + payload).hexdigest()


def _repo_snapshot(root: Path) -> dict[str, bytes]:
    _install_test_schemas(root)
    schema = root / "tests" / "acceptance" / "stage3_required_cases.schema.json"
    if not schema.exists():
        schema.parent.mkdir(parents=True, exist_ok=True)
        schema.write_bytes((Path(__file__).resolve().parent / "acceptance" / schema.name).read_bytes())
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


class _ToolRevisionRecordingRunner(fakes.RecordingRunner):
    """以記憶體中的 Git blob 快照模擬可信版本與工作樹位元組。"""

    def __init__(self, root: Path, blobs: Mapping[str, bytes], *, contract_revision: str = "0" * 40,
                 tool_revision: str = "1" * 40, docker_result: dict | None = None) -> None:
        super().__init__()
        self.root = root.resolve()
        self.blobs = dict(blobs)
        self.contract_revision = contract_revision
        self.tool_revision = tool_revision
        self.docker_result = docker_result or {"rc": 0, "stdout": "", "stderr": ""}
        self.object_types: dict[str, str] = {}

    def __call__(self, argv, *, env=None, timeout=None, cwd=None, bytes_output=False):
        args = list(argv)
        if args and args[0] == "git":
            if "rev-parse" in args and args[-1] == "HEAD:contracts":
                stdout, rc = self.contract_revision + "\n", 0
            elif "rev-parse" in args and args[-1] == "HEAD":
                stdout, rc = self.tool_revision + "\n", 0
            elif "rev-parse" in args and args[-1].endswith("^{commit}"):
                revision = args[-1][:-len("^{commit}")]
                stdout, rc = (revision + "\n", 0) if revision == self.tool_revision else ("", 1)
            elif "cat-file" in args and "-t" in args:
                object_type = self.object_types.get(args[-1])
                stdout, rc = (object_type + "\n", 0) if object_type else ("", 128)
            elif "rev-parse" in args and ":" in args[-1]:
                revision, relative = args[-1].split(":", 1)
                if revision != self.tool_revision:
                    stdout, rc = "", 1
                elif relative in self.blobs:
                    oid = _git_blob_oid(self.blobs[relative])
                    self.object_types[oid] = "blob"
                    stdout, rc = oid + "\n", 0
                else:
                    members = [
                        path for path in self.blobs
                        if path.startswith(relative.rstrip("/") + "/")
                    ]
                    if members:
                        oid = _git_tree_oid(members)
                        self.object_types[oid] = "tree"
                        stdout, rc = oid + "\n", 0
                    else:
                        stdout, rc = "", 1
            elif "ls-tree" in args:
                revision = args[args.index("-z") + 1]
                if revision != self.tool_revision:
                    stdout, rc = "", 1
                elif "--" not in args:
                    members = sorted(self.blobs)
                    stdout, rc = ("\0".join(members) + ("\0" if members else ""), 0)
                else:
                    tree = args[-1].removeprefix(":(literal)").rstrip("/")
                    members = sorted(
                        relative for relative in self.blobs
                        if relative.startswith(tree + "/")
                        and "__pycache__" not in relative.split("/")
                        and not relative.endswith(".pyc")
                    )
                    stdout, rc = ("\0".join(members) + ("\0" if members else ""), 0)
            elif "ls-files" in args:
                if "--" not in args:
                    matches = sorted(self.blobs)
                elif args[-1].startswith(":(icase,literal)"):
                    requested = args[-1].removeprefix(":(icase,literal)")
                    ascii_fold = lambda value: "".join(
                        chr(ord(char) + 32) if "A" <= char <= "Z" else char for char in value
                    )
                    matches = sorted(relative for relative in self.blobs
                                     if ascii_fold(relative) == ascii_fold(requested))
                else:
                    relative = args[-1].removeprefix(":(literal)")
                    matches = [relative] if relative in self.blobs else []
                stdout, rc = ("\0".join(matches) + ("\0" if matches else ""), 0)
            elif "hash-object" in args:
                candidate = Path(args[-1])
                if not candidate.is_absolute():
                    candidate = self.root / candidate
                try:
                    data = candidate.read_bytes()
                except OSError:
                    stdout, rc = "", 1
                else:
                    stdout, rc = _git_blob_oid(data) + "\n", 0
            else:
                stdout, rc = "", 128
            stderr = "unsupported or unresolved test Git command" if rc == 128 else ""
            if bytes_output:
                stdout = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
                stderr = stderr.encode("utf-8")
            return fakes.CmdResult(rc=rc, stdout=stdout, stderr=stderr, elapsed_s=0.0, timed_out=False)
        if args and args[0] == "docker":
            return fakes.CmdResult(
                rc=int(self.docker_result.get("rc", 0)),
                stdout=str(self.docker_result.get("stdout", "")),
                stderr=str(self.docker_result.get("stderr", "")),
                elapsed_s=0.0,
                timed_out=bool(self.docker_result.get("timed_out", False)),
            )
        return super().__call__(args, env=env, timeout=timeout, cwd=cwd, bytes_output=bytes_output)


def _synthetic_trusted_pair_map(root: Path, *, include_alternate: bool = False) -> dict:
    manifest_doc = _read_json(root / "ab-manifest.json")
    map_ref = manifest_doc["platform_settings"]["case_transport_map"]
    mapping = _read_json(root / map_ref["path"])
    pair_id, public_pair = next(iter(mapping["approved_pairs"].items()))
    pairs = {
        pair_id: {
            "dut_port": "COM20",
            "daemon_port": "COM21",
            "dut_side_port_id": public_pair["dut_side_port_id"],
            "daemon_side_port_id": public_pair["daemon_side_port_id"],
            "decision_ref": public_pair["decision_ref"],
        }
    }
    if include_alternate:
        pairs["T3-OTHER-APPROVED-PAIR"] = {
            "dut_port": "COM400",
            "daemon_port": "COM401",
            "dut_side_port_id": "T3-OTHER-DUT-ID",
            "daemon_side_port_id": "T3-OTHER-DAEMON-ID",
            "decision_ref": "T3-OTHER-PAIR-AUTH",
        }
    return {
        "schema_version": 1,
        "exit_item_id": EXIT_WINDOWS_L15,
        "platform": "windows",
        "selected_pair_id": pair_id,
        "pairs": pairs,
    }


@pytest.mark.parametrize("attribute", ["invariants", "destructive"], ids=["invariants", "destructive"])
def test_tool_revision_rejects_self_hashed_canonical_case_attribute_changes(tmp_path, attribute):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest_doc = _read_json(path)
    case_path = tmp_path / manifest_doc["case_list"]["path"]
    cases_doc = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    if attribute == "invariants":
        target = next(row for row in cases_doc["cases"] if row["case_id"] == "cowork-01-agent-bad-cmd")
        target[attribute] = []
    else:
        target = next(
            row for row in cases_doc["cases"]
            if row.get("destructive") is True
        )
        target[attribute] = False
    case_path.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    manifest_doc["case_list"]["sha256"] = fakes.sha256_file(case_path)
    _write_json(path, manifest_doc)
    manifest = _load(path)
    revision_blobs = _repo_snapshot(tmp_path)
    revision_blobs["tests/acceptance/cases.yaml"] = b"trusted canonical cases bytes\n"
    runner = _ToolRevisionRecordingRunner(tmp_path, revision_blobs)
    violations = ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert "tool revision mismatch: tests/acceptance/cases.yaml" in violations


def test_tool_revision_accepts_repo_frozen_inputs_when_raw_bytes_match(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    assert ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    ) == []


def test_required_cases_and_verify_frozen_keep_plan_interface_without_base_revision(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    from tools.stage3 import required_cases

    required, _rows = ab_manifest.required_cases(
        manifest,
        repo_root=tmp_path,
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert required == set(manifest.case_ids)
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    assert ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        source_reader=_trusted_source_reader(tmp_path),
    ) == []


@pytest.mark.parametrize("stage", ["1A", "1B"], ids=["stage-1a", "stage-1b"])
@pytest.mark.parametrize("component", ["cli", "daemon.runtime"], ids=["python-cli", "python-daemon-runtime"])
@pytest.mark.parametrize(
    "defect",
    ["missing_sha", "sha_type", "path_type"],
    ids=["missing-sha", "sha-wrong-type", "path-wrong-type"],
)
def test_python_stage1_baseline_still_requires_component_file_identity(tmp_path, stage, component, defect):
    path = fakes.write_ab_manifest(tmp_path, stage=stage)
    doc = _read_json(path)
    release_path = tmp_path / doc["sides"][0]["release_manifest"]["path"]
    release_doc = _read_json(release_path)
    selected = release_doc["components"]["cli"] if component == "cli" else release_doc["components"]["daemon"]["runtime"]
    if defect == "missing_sha":
        selected.pop("sha256")
    elif defect == "sha_type":
        selected["sha256"] = 17
    else:
        selected["path"] = 17
    _write_json(release_path, release_doc)
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


@pytest.mark.parametrize("component", ["cli", "daemon.runtime"], ids=["python-cli", "python-daemon-runtime"])
def test_python_stage1_baseline_verify_bundle_rehashes_selected_files(tmp_path, component):
    path = fakes.write_ab_manifest(tmp_path, stage="1A")
    manifest = _load(path)
    baseline = manifest.sides[0]
    component_doc = baseline.manifest_doc
    if component == "cli":
        selected = component_doc["components"]["cli"]
    else:
        selected = component_doc["components"]["daemon"]["runtime"]
    executable = baseline.bundle_dir / selected["path"]
    executable.write_bytes(executable.read_bytes() + b"\n# replaced after manifest freeze\n")
    violations = ab_manifest.verify_bundle(baseline.bundle_dir, component_doc, label="side A")
    assert f"release component sha256 mismatch: side A {component if component == 'cli' else 'daemon.runtime'}" in violations


def test_python_stage1_baseline_verify_bundle_accepts_valid_component_identities(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, stage="1B")
    manifest = _load(path)
    assert ab_manifest.verify_bundle(
        manifest.sides[0].bundle_dir,
        manifest.sides[0].manifest_doc,
        label="side A",
    ) == []


@pytest.mark.parametrize("image", [None, ""], ids=["null-image", "empty-image"])
def test_docker_dut_requires_nonempty_image_at_load(tmp_path, image):
    path = fakes.write_ab_manifest(
        tmp_path,
        **{"dut.kind": "docker", "dut.image": image, "dut.image_digest": "sha256:" + "a" * 64},
    )
    with pytest.raises(HarnessError) as caught:
        _load(path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "docker" in caught.value.detail.casefold() and "image" in caught.value.detail.casefold()


def test_verify_frozen_reports_missing_docker_inspect_input(tmp_path):
    path = fakes.write_ab_manifest(
        tmp_path,
        **{"dut.kind": "docker", "dut.image": "serialwrap-dut:test", "dut.image_digest": "sha256:" + "a" * 64},
    )
    manifest = _load(path)
    manifest = replace(manifest, dut=replace(manifest.dut, image=None))
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    violations = ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert any("docker" in item.casefold() and "image" in item.casefold() for item in violations)


def test_verify_frozen_rejects_docker_inspect_failure_and_accepts_matching_digest(tmp_path):
    digest = "sha256:" + "a" * 64
    path = fakes.write_ab_manifest(
        tmp_path,
        **{"dut.kind": "docker", "dut.image": "serialwrap-dut:test", "dut.image_digest": digest},
    )
    manifest = _load(path)
    failed_runner = _ToolRevisionRecordingRunner(
        tmp_path,
        _repo_snapshot(tmp_path),
        docker_result={"rc": 1, "stdout": "", "stderr": "not found"},
    )
    violations = ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=failed_runner,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert "dut image digest mismatch" in violations

    good_runner = _ToolRevisionRecordingRunner(
        tmp_path,
        _repo_snapshot(tmp_path),
        docker_result={"rc": 0, "stdout": digest + "\n", "stderr": ""},
    )
    assert ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=good_runner,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    ) == []


def test_windows_l15_verify_frozen_requires_injected_trusted_pair_map(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    manifest = _load(path)
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    violations = ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert "trusted pair map missing: BLOCKED-OWNER BO-P9-01" in violations


def test_windows_l15_trusted_pair_map_accepts_exact_pair_and_rejects_untrusted_changes(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    trusted = _synthetic_trusted_pair_map(tmp_path, include_alternate=True)
    manifest = _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    assert ab_manifest.verify_frozen(
        manifest,
        repo_root=tmp_path,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
        trusted_pair_map=trusted,
    ) == []

    changed_path = _windows_l15_transport_fixture(tmp_path / "changed")
    changed_doc = _read_json(changed_path)
    changed_doc["dut"]["com_pair"] = {"daemon_side": "COM400", "bridge_side": "COM401"}
    changed_doc["test_device_allowlist"] = ["COM400", "COM401"]
    _write_json(changed_path, changed_doc)
    with pytest.raises(HarnessError) as caught:
        _load(changed_path, repo_root=changed_path.parent, trusted_pair_map=trusted)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"

    swapped_path = _windows_l15_transport_fixture(tmp_path / "swapped")
    swapped_doc = _read_json(swapped_path)
    swapped_doc["dut"]["com_pair"] = {"daemon_side": "COM20", "bridge_side": "COM21"}
    _write_json(swapped_path, swapped_doc)
    with pytest.raises(HarnessError):
        _load(swapped_path, repo_root=swapped_path.parent, trusted_pair_map=trusted)

    alternate_path = _windows_l15_transport_fixture(tmp_path / "alternate")
    alternate_doc = _read_json(alternate_path)
    map_ref = alternate_doc["platform_settings"]["case_transport_map"]
    map_path = alternate_path.parent / map_ref["path"]
    mapping = _read_json(map_path)
    native_id = next(case_id for case_id, row in mapping["cases"].items() if row["requires_native_com"])
    mapping["approved_pairs"]["T3-OTHER-APPROVED-PAIR"] = {
        "dut_side_port_id": "T3-OTHER-DUT-ID",
        "daemon_side_port_id": "T3-OTHER-DAEMON-ID",
        "decision_ref": "T3-OTHER-PAIR-AUTH",
    }
    mapping["cases"][native_id]["pair_id"] = "T3-OTHER-APPROVED-PAIR"
    _write_json(map_path, mapping)
    map_sha = fakes.sha256_file(map_path)
    map_ref["sha256"] = map_sha
    next(item for item in alternate_doc["tools"] if item["path"] == map_ref["path"])["sha256"] = map_sha
    alternate_doc["platform_settings"]["case_transports"][native_id]["port_id"] = "T3-OTHER-DUT-ID"
    alternate_doc["platform_settings"]["case_transports"][native_id]["decision_ref"] = "T3-OTHER-PAIR-AUTH"
    _write_json(alternate_path, alternate_doc)
    with pytest.raises(HarnessError):
        _load(alternate_path, repo_root=alternate_path.parent, trusted_pair_map=trusted)


def test_windows_l15_trusted_pair_map_schema_is_closed_and_validates_synthetic_pair():
    import jsonschema

    schema_path = Path(__file__).resolve().parent / "acceptance" / "stage3_trusted_pair_map.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    value = {
        "schema_version": 1,
        "exit_item_id": EXIT_WINDOWS_L15,
        "platform": "windows",
        "selected_pair_id": "T3-COM0COM-PAIR",
        "pairs": {
            "T3-COM0COM-PAIR": {
                "dut_port": "COM20",
                "daemon_port": "COM21",
                "dut_side_port_id": "T3-PAIR-DUT",
                "daemon_side_port_id": "T3-PAIR-DAEMON",
                "decision_ref": "T3-COM-PAIR-AUTH",
            }
        },
    }
    jsonschema.validate(value, schema)
    invalid = copy.deepcopy(value)
    invalid["pairs"]["T3-COM0COM-PAIR"].pop("daemon_port")
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(invalid))


@pytest.mark.parametrize(
    "relative",
    ["tools/stage3/__init__.py", "tools/uart_stdio_bridge/bridge.py"],
    ids=["stage3-module", "uart-bridge-module"],
)
def test_tool_revision_rejects_deleted_file_from_frozen_tool_tree(tmp_path, relative):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    revision_blobs = _repo_snapshot(tmp_path)
    (tmp_path / relative).unlink()
    tree = "tools/stage3" if relative.startswith("tools/stage3/") else "tools/uart_stdio_bridge"
    changed_tree = fakes.tree_sha256(tmp_path / tree)
    manifest = replace(
        manifest,
        tools=tuple(
            replace(item, tree_sha256=changed_tree)
            if isinstance(item, ab_manifest.FrozenTree) and item.path == tree else item
            for item in manifest.tools
        ),
    )
    runner = _ToolRevisionRecordingRunner(tmp_path, revision_blobs)
    classified = []
    original_classifier = ab_manifest._tool_revision_input_errors

    def tracing_classifier(**kwargs):
        classified.append(kwargs["relative"])
        return original_classifier(**kwargs)

    with patch.object(ab_manifest, "_tool_revision_input_errors", tracing_classifier):
        violations = ab_manifest.verify_frozen(
            manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
            source_reader=_trusted_source_reader(tmp_path),
        )
    assert f"tool revision tree mismatch: {tree} missing {relative}" in violations
    assert relative in classified


def test_tool_revision_rejects_deleted_entire_frozen_tool_tree(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    revision_blobs = _repo_snapshot(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "tools/stage3")
    violations = ab_manifest._tool_revision_frozen_errors(
        manifest, repo_root=tmp_path, run=_ToolRevisionRecordingRunner(tmp_path, revision_blobs),
        tool_revision="1" * 40,
    )
    assert "tool revision tree mismatch: tools/stage3 missing tree" in violations


def test_tool_revision_rejects_deleted_contract_tree_file(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    extra = tmp_path / "contracts" / "extra-versioned-input.yaml"
    extra.write_text("schema: frozen\n", encoding="utf-8")
    revision_blobs = _repo_snapshot(tmp_path)
    relative = "contracts/extra-versioned-input.yaml"
    (tmp_path / relative).unlink()
    violations = ab_manifest._tool_revision_frozen_errors(
        manifest, repo_root=tmp_path, run=_ToolRevisionRecordingRunner(tmp_path, revision_blobs),
        tool_revision="1" * 40,
    )
    assert f"tool revision tree mismatch: contracts missing {relative}" in violations


def test_tool_revision_rejects_extra_file_in_frozen_tool_tree(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    revision_blobs = _repo_snapshot(tmp_path)
    relative = "tools/stage3/unreviewed-extra.py"
    (tmp_path / relative).write_text("# extra\n", encoding="utf-8")
    classified = []
    original_classifier = ab_manifest._tool_revision_input_errors

    def tracing_classifier(**kwargs):
        classified.append(kwargs["relative"])
        return original_classifier(**kwargs)

    with patch.object(ab_manifest, "_tool_revision_input_errors", tracing_classifier):
        violations = ab_manifest._tool_revision_frozen_errors(
            manifest, repo_root=tmp_path, run=_ToolRevisionRecordingRunner(tmp_path, revision_blobs),
            tool_revision="1" * 40,
        )
    assert f"tool revision tree mismatch: tools/stage3 extra {relative}" in violations
    assert relative in classified


def test_tool_revision_accepts_complete_frozen_trees(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    assert ab_manifest._tool_revision_frozen_errors(
        manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
    ) == []


@pytest.mark.parametrize("path_kind", ["absolute", "relative-escape"], ids=["absolute-outside", "relative-escape"])
def test_windows_l15_mapping_outside_repo_is_rejected_even_with_refreshed_hashes(tmp_path, path_kind):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    ref = doc["platform_settings"]["case_transport_map"]
    original_path = tmp_path / ref["path"]
    outside = tmp_path.parent / f"{tmp_path.name}-external-map.json"
    outside.write_bytes(original_path.read_bytes())
    map_value = str(outside) if path_kind == "absolute" else f"../{outside.name}"
    ref["path"] = map_value
    ref["sha256"] = fakes.sha256_file(outside)
    tool = next(item for item in doc["tools"] if item["path"] == "tests/acceptance/windows_l15_case_transport_map.json")
    tool["path"] = map_value
    tool["sha256"] = ref["sha256"]
    _write_json(path, doc)
    trusted = _synthetic_trusted_pair_map(tmp_path)
    try:
        manifest = _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    except HarnessError as exc:
        assert "case transport map" in exc.detail and "repo" in exc.detail
        return
    runner = _ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path))
    violations = ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
        trusted_pair_map=trusted,
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert any("case transport map" in item and "repo" in item for item in violations)


def test_windows_l15_mapping_content_cannot_be_self_hashed_outside_repo(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    ref = doc["platform_settings"]["case_transport_map"]
    original_path = tmp_path / ref["path"]
    outside = tmp_path.parent / f"{tmp_path.name}-mutated-map.json"
    changed = _read_json(original_path)
    non_native_case = next(case_id for case_id, row in changed["cases"].items()
                           if not row["requires_native_com"])
    changed["cases"][non_native_case]["allowed_transports"].append("pty")
    _write_json(outside, changed)
    ref["path"] = str(outside)
    ref["sha256"] = fakes.sha256_file(outside)
    tool = next(item for item in doc["tools"] if item["path"] == "tests/acceptance/windows_l15_case_transport_map.json")
    tool["path"] = str(outside)
    tool["sha256"] = ref["sha256"]
    _write_json(path, doc)
    trusted = _synthetic_trusted_pair_map(tmp_path)
    try:
        manifest = _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    except HarnessError as exc:
        assert "case transport map" in exc.detail and "repo" in exc.detail
        return
    violations = ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path,
        run=_ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path)),
        tool_revision="1" * 40, trusted_pair_map=trusted,
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert any("case transport map" in item and "repo" in item for item in violations)


def test_windows_l15_mapping_inside_repo_is_version_controlled(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    trusted = _synthetic_trusted_pair_map(tmp_path)
    manifest = _load(path, trusted_pair_map=trusted)
    assert ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path,
        run=_ToolRevisionRecordingRunner(tmp_path, _repo_snapshot(tmp_path)),
        tool_revision="1" * 40, trusted_pair_map=trusted,
        source_reader=_trusted_source_reader(tmp_path),
    ) == []


@pytest.mark.parametrize("defect", ["same", "casefold-alias", "device-path-alias", "duplicate-id"],
                         ids=["same-port", "case-insensitive-port", "device-path-alias", "duplicate-port-id"])
def test_windows_l15_trusted_pair_requires_distinct_endpoint_names_and_ids(tmp_path, defect):
    path = _windows_l15_transport_fixture(tmp_path)
    trusted = _synthetic_trusted_pair_map(tmp_path)
    pair_id = trusted["selected_pair_id"]
    pair = trusted["pairs"][pair_id]
    doc = _read_json(path)
    if defect == "same":
        pair["daemon_port"] = pair["dut_port"]
        manifest_ports = ("COM20", "COM20")
    elif defect == "casefold-alias":
        pair["daemon_port"] = "com20"
        manifest_ports = ("COM20", "com20")
    elif defect == "device-path-alias":
        pair["daemon_port"] = "\\\\.\\com20"
        manifest_ports = ("COM20", "\\\\.\\com20")
    else:
        pair["daemon_side_port_id"] = pair["dut_side_port_id"]
        map_ref = doc["platform_settings"]["case_transport_map"]
        map_path = tmp_path / map_ref["path"]
        mapping = _read_json(map_path)
        mapping["approved_pairs"][pair_id]["daemon_side_port_id"] = pair["dut_side_port_id"]
        _write_json(map_path, mapping)
        digest = fakes.sha256_file(map_path)
        map_ref["sha256"] = digest
        next(item for item in doc["tools"] if item["path"] == map_ref["path"])["sha256"] = digest
        manifest_ports = ("COM20", "COM21")
    doc["dut"]["com_pair"] = {"bridge_side": manifest_ports[0], "daemon_side": manifest_ports[1]}
    doc["test_device_allowlist"] = list(manifest_ports)
    _write_json(path, doc)
    with pytest.raises(HarnessError) as caught:
        _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"


def test_trusted_pair_schema_documents_loader_distinctness_validation():
    schema_path = Path(__file__).resolve().parent / "acceptance" / "stage3_trusted_pair_map.schema.json"
    schema = _read_json(schema_path)
    description = schema["$defs"]["TrustedPair"].get("description", "")
    assert "loader" in description.casefold()
    assert "不重複" in description


def test_verify_bundle_reports_component_with_sha256_but_no_path(tmp_path):
    violations = ab_manifest.verify_bundle(
        tmp_path,
        {"components": {"cli": {"sha256": "a" * 64}}},
        label="side A",
    )
    assert "release component identity invalid: side A cli" in violations


def test_verify_bundle_reports_component_with_path_but_no_sha256(tmp_path):
    violations = ab_manifest.verify_bundle(
        tmp_path,
        {"components": {"cli": {"path": "bin/cli"}}},
        label="side A",
    )
    assert "release component identity invalid: side A cli" in violations


def test_verify_bundle_ignores_valid_digest_only_named_assets(tmp_path):
    doc = {"components": {"assets": {
        "skill_sha256": "a" * 64,
        "skill_windows_sha256": "b" * 64,
        "profiles_sha256": "c" * 64,
    }}}
    assert ab_manifest.verify_bundle(tmp_path, doc, label="side A") == []


def test_verify_bundle_rechecks_complete_component_file_identity(tmp_path):
    component = tmp_path / "bin" / "serialwrap"
    component.parent.mkdir()
    component.write_bytes(b"release component\n")
    doc = {"components": {"cli": {
        "path": "bin/serialwrap",
        "sha256": fakes.sha256_file(component),
    }}}
    assert ab_manifest.verify_bundle(tmp_path, doc, label="side A") == []


def test_stage3_t3_changelog_has_only_current_optional_base_behavior():
    repo = Path(__file__).resolve().parents[1]
    assert not (repo / "changelog.d" / "stage3-t3-ab-manifest-r1.md").exists()
    fragment = (repo / "changelog.d" / "rust-p9-03-ab-manifest.md").read_text(encoding="utf-8")
    assert "`base_revision` 是選用" in fragment
    assert "要求可信 base revision" not in fragment


@pytest.mark.parametrize(
    ("damage", "expected_stderr"),
    [
        (
            "schema-seed-family-lock",
            "Stage 3 分母鎖檢查失敗：分母鎖根物件 的 schema 欄位集合不符（缺少欄位：exits）\n",
        ),
        (
            "seed-family-lock",
            "Stage 3 分母鎖檢查失敗：seed.cases SHA 與來源 manifest 不同\n",
        ),
        (
            "family-lock",
            "Stage 3 分母鎖檢查失敗：分母鎖小於固定 seed 基準 S3-L3-P0P1-REMOTE-WINDOWS/windows：p0-blog-clean；案例 family 不可重分類（固定 seed）：win-task-logon-trigger；S3-L3-P0P1-REMOTE-WINDOWS/windows 缺少適用案例：p0-blog-clean\n",
        ),
        (
            "lock-only",
            "Stage 3 分母鎖檢查失敗：分母鎖小於固定 seed 基準 S3-L3-P0P1-REMOTE-WINDOWS/windows：p0-blog-clean；S3-L3-P0P1-REMOTE-WINDOWS/windows 缺少適用案例：p0-blog-clean\n",
        ),
    ],
    ids=["schema-short-circuits", "seed-short-circuits", "family-before-lock-diagnostic", "lock-diagnostic"],
)
def test_required_cases_check_preserves_4b71d2db_complete_output_and_exit_code(tmp_path, damage, expected_stderr):
    from tools.stage3 import required_cases

    root = tmp_path / "repo"
    fakes.write_fake_repo(root)
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    lock = _read_json(lock_path)
    if damage == "schema-seed-family-lock":
        lock.pop("exits")
        lock["seed"]["cases"]["sha256"] = "0" * 64
    elif damage == "seed-family-lock":
        lock["seed"]["cases"]["sha256"] = "0" * 64
    if damage in {"schema-seed-family-lock", "seed-family-lock", "family-lock"}:
        cases_path = root / "tests" / "acceptance" / "cases.yaml"
        cases_doc = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
        row = next(item for item in cases_doc["cases"] if item.get("case_id") == "win-task-logon-trigger")
        row["family"] = "P0"
        cases_path.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lock_ids = lock.get("exits", {}).get("S3-L3-P0P1-REMOTE-WINDOWS", {}).get("windows", {}).get("case_ids", [])
    if "p0-blog-clean" in lock_ids:
        lock_ids.remove("p0-blog-clean")
    _write_json(lock_path, lock)
    stdout, stderr = io.StringIO(), io.StringIO()
    reader = _trusted_source_reader(root)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = required_cases.main(["--check"], repo_root=root, source_reader=reader)
    assert rc == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == expected_stderr


def test_load_manifest_reads_schema_from_the_supplied_repo_root(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    _install_test_schemas(tmp_path)
    schema_path = tmp_path / "tests" / "acceptance" / "ab_manifest.schema.json"
    schema = _read_json(schema_path)
    schema["properties"]["run_id"]["minLength"] = 1000
    _write_json(schema_path, schema)

    with pytest.raises(HarnessError) as caught:
        _load(path, repo_root=tmp_path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "schema validation failed" in caught.value.detail


def test_loader_independently_requires_the_stage3_tools_tree_after_schema_weakening(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    _install_test_schemas(tmp_path)
    schema_path = tmp_path / "tests" / "acceptance" / "ab_manifest.schema.json"
    schema = _read_json(schema_path)
    schema["allOf"] = []
    schema["properties"]["tools"]["minItems"] = 1
    _write_json(schema_path, schema)

    attacker_tree = tmp_path / "tools" / "attacker"
    attacker_tree.mkdir(parents=True)
    (attacker_tree / "replacement.py").write_text("# forged tool tree\n", encoding="utf-8")
    (tmp_path / "tools" / "stage3" / "__init__.py").write_text("# modified required tools\n", encoding="utf-8")
    doc = _read_json(path)
    doc["tools"] = [{"path": "tools/attacker", "tree_sha256": ab_manifest._tree_sha256(attacker_tree)}]
    _write_json(path, doc)

    with pytest.raises(HarnessError) as caught:
        _load(path, repo_root=tmp_path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "tools/stage3" in caught.value.detail


def test_manifest_and_trusted_pair_schemas_are_checked_against_tool_revision(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    trusted = _synthetic_trusted_pair_map(tmp_path)
    manifest = _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    revision_blobs = _repo_snapshot(tmp_path)
    schema_paths = (
        "tests/acceptance/ab_manifest.schema.json",
        "tests/acceptance/stage3_trusted_pair_map.schema.json",
    )
    for relative in schema_paths:
        schema_path = tmp_path / relative
        schema_path.write_bytes(schema_path.read_bytes() + b"\n")

    runner = _ToolRevisionRecordingRunner(
        tmp_path, revision_blobs, contract_revision=manifest.contract_revision,
    )
    violations = ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
        trusted_pair_map=trusted, source_reader=_trusted_source_reader(tmp_path),
    )
    for relative in schema_paths:
        assert f"tool revision mismatch: {relative}" in violations


@pytest.mark.parametrize(
    "schema_relative",
    [
        "tests/acceptance/ab_manifest.schema.json",
        "tests/acceptance/stage3_trusted_pair_map.schema.json",
    ],
    ids=["manifest-schema", "trusted-pair-schema"],
)
def test_schema_changed_during_load_then_restored_is_rejected(tmp_path, schema_relative):
    if schema_relative.endswith("stage3_trusted_pair_map.schema.json"):
        path = _windows_l15_transport_fixture(tmp_path)
        trusted = _synthetic_trusted_pair_map(tmp_path)
    else:
        path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
        trusted = None
    _install_test_schemas(tmp_path)
    schema_path = tmp_path / schema_relative
    trusted_bytes = schema_path.read_bytes()
    schema_path.write_bytes(b"{}\n")
    manifest = _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    schema_path.write_bytes(trusted_bytes)

    violations = _verify(manifest, tmp_path)
    assert f"schema sha256 mismatch: {schema_relative}" in violations


@pytest.mark.parametrize(
    "schema_relative",
    [
        "tests/acceptance/ab_manifest.schema.json",
        "tests/acceptance/stage3_trusted_pair_map.schema.json",
    ],
    ids=["manifest-schema", "trusted-pair-schema"],
)
def test_schema_drift_after_load_is_rejected(tmp_path, schema_relative):
    if schema_relative.endswith("stage3_trusted_pair_map.schema.json"):
        path = _windows_l15_transport_fixture(tmp_path)
        trusted = _synthetic_trusted_pair_map(tmp_path)
    else:
        path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
        trusted = None
    manifest = _load(path, repo_root=tmp_path, trusted_pair_map=trusted)
    schema_path = tmp_path / schema_relative
    schema_path.write_bytes(b"{}\n")

    violations = _verify(manifest, tmp_path)
    assert f"schema sha256 mismatch: {schema_relative}" in violations


def test_schema_loaded_bytes_match_current_file_and_tool_revision(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    assert _verify(manifest, tmp_path) == []


def _copy_fake_stage3_checkout(source_root: Path, destination_root: Path) -> Path:
    import shutil

    destination = destination_root / "tools" / "stage3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root / "tools" / "stage3", destination)
    return destination


def test_verify_frozen_rejects_executing_tools_stage3_changed_file(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    executing_root = tmp_path / "other-checkout"
    executing_tree = _copy_fake_stage3_checkout(tmp_path, executing_root)
    (executing_tree / "__init__.py").write_text("# changed executor\n", encoding="utf-8")

    violations = _verify(manifest, tmp_path, executing_tools_root=executing_root)
    assert any(item.startswith("executing tools/stage3 tree mismatch:") for item in violations)


def test_verify_frozen_rejects_executing_tools_stage3_extra_file(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    executing_root = tmp_path / "other-checkout"
    executing_tree = _copy_fake_stage3_checkout(tmp_path, executing_root)
    (executing_tree / "unreviewed.py").write_text("# extra executor code\n", encoding="utf-8")

    violations = _verify(manifest, tmp_path, executing_tools_root=executing_root)
    assert any(item.startswith("executing tools/stage3 tree mismatch:") for item in violations)


def test_verify_frozen_accepts_identical_executing_tools_stage3_checkout(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path, repo_root=tmp_path)
    executing_root = tmp_path / "same-checkout"
    _copy_fake_stage3_checkout(tmp_path, executing_root)

    assert _verify(manifest, tmp_path, executing_tools_root=executing_root) == []


def test_task6_original_load_order_cannot_verify_another_repo_root(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    try:
        manifest = ab_manifest.load_manifest(path)
    except HarnessError as exc:
        assert exc.code == "HARNESS_MANIFEST_INVALID"
        return

    violations = ab_manifest.verify_frozen(manifest, repo_root=tmp_path)
    assert violations == [f"manifest repo_root mismatch: loaded {manifest.repo_root} supplied {tmp_path.resolve()}"]


def test_absolute_external_profile_is_sha_frozen_and_rechecked(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    private_profile = tmp_path.parent / f"{tmp_path.name}-private-profile.yaml"
    private_profile.write_text("profile: private\n", encoding="utf-8")
    doc = _read_json(path)
    doc["profile_files"] = [{"path": str(private_profile), "sha256": fakes.sha256_file(private_profile)}]
    _write_json(path, doc)

    manifest = _load(path, repo_root=tmp_path)
    assert _verify(manifest, tmp_path) == []
    private_profile.write_text("profile: changed\n", encoding="utf-8")
    assert f"profile sha256 mismatch: {private_profile}" in _verify(manifest, tmp_path)


def test_external_profile_with_stale_declared_sha_is_rejected_during_load(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    private_profile = tmp_path.parent / f"{tmp_path.name}-stale-profile.yaml"
    private_profile.write_text("profile: changed\n", encoding="utf-8")
    doc = _read_json(path)
    doc["profile_files"] = [{"path": str(private_profile), "sha256": "0" * 64}]
    _write_json(path, doc)

    with pytest.raises(HarnessError) as caught:
        _load(path, repo_root=tmp_path)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"
    assert "profile sha256 mismatch" in caught.value.detail


@pytest.mark.parametrize(
    ("platform", "layer", "suite_field"),
    [
        ("linux", "l1.5", "selftest_report"),
        ("linux", "l2", "testbed_template"),
        ("linux", "l3", "realhw_config_template"),
    ],
    ids=["selftest", "testbed", "realhw-config"],
)
def test_external_suite_inputs_remain_sha_frozen_execution_artifacts(tmp_path, platform, layer, suite_field):
    path = fakes.write_ab_manifest(tmp_path, platform=platform, layer=layer)
    private_input = tmp_path.parent / f"{tmp_path.name}-{suite_field}.json"
    private_input.write_text("private suite input\n", encoding="utf-8")
    doc = _read_json(path)
    doc["suite"][suite_field] = {"path": str(private_input), "sha256": fakes.sha256_file(private_input)}
    _write_json(path, doc)

    manifest = _load(path, repo_root=tmp_path)
    violations = _verify(manifest, tmp_path)
    assert not any(suite_field in item or "outside repo" in item for item in violations)


@pytest.mark.parametrize(
    ("value", "windows", "expected"),
    [
        (r"C:\repo\tests\acceptance\cases.yaml", True, "tests/acceptance/cases.yaml"),
        ("C:/repo/tests/acceptance/cases.yaml", True, "tests/acceptance/cases.yaml"),
        (r"D:\outside\cases.yaml", True, None),
        (r"C:\repo\..\outside\cases.yaml", True, None),
    ],
    ids=["windows-backslash-absolute", "windows-forward-slash-absolute", "windows-outside", "windows-dotdot-escape"],
)
def test_repo_relative_path_normalizes_windows_absolute_paths_on_any_host(value, windows, expected):
    root = PureWindowsPath("C:/repo") if windows else Path("/repo")
    assert ab_manifest._repo_relative_path(root, value, windows=windows) == expected


def _track_open_reads(monkeypatch, roots: list[Path]) -> dict[Path, int]:
    """以底層開檔入口追蹤指定 repo／bundle 內成功的讀取開檔。"""
    counts: dict[Path, int] = {}
    allowed_roots = tuple(Path(root).resolve() for root in roots)
    depth = threading.local()

    def record(file, mode) -> None:
        if not isinstance(mode, str) or not ("r" in mode or "+" in mode):
            return
        try:
            candidate = Path(os.fsdecode(os.fspath(file))).resolve()
        except (OSError, TypeError, ValueError):
            return
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            return
        counts[candidate] = counts.get(candidate, 0) + 1

    def wrap(original):
        def tracked(file, *args, **kwargs):
            mode = kwargs.get("mode", args[0] if args else "r")
            nested = getattr(depth, "value", 0) > 0
            depth.value = getattr(depth, "value", 0) + 1
            try:
                opened = original(file, *args, **kwargs)
            finally:
                depth.value -= 1
            if not nested:
                record(file, mode)
            return opened

        return tracked

    # Python 3.12 的 pathlib.Path.open/read_bytes/read_text 會呼叫 io.open；
    # builtins.open 是另一個可直接使用的入口。巢狀入口由 thread-local 深度去重。
    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    monkeypatch.setattr(io, "open", wrap(io.open))
    return counts


def _frozen_read_audit_paths(manifest, root: Path) -> list[Path]:
    paths = [
        manifest.source_path,
        ab_manifest._path(root, manifest.case_list.path),
        ab_manifest._path(root, manifest.case_lock.path),
        root / "tests" / "acceptance" / "stage_exits.json",
        root / "docs" / "rust-refactor" / "evidence" / "denominators" / "source-manifest.json",
        root / "tests" / "acceptance" / "ab_manifest.schema.json",
    ]
    if manifest.required_cases_lock is not None:
        paths.append(ab_manifest._path(root, manifest.required_cases_lock.path))
    paths.extend(ab_manifest._path(root, side.release_manifest.path) for side in manifest.sides)
    map_ref = manifest.platform_settings.get("case_transport_map")
    if isinstance(map_ref, Mapping):
        paths.append(root / map_ref["path"])
        paths.append(root / "tests" / "acceptance" / "stage3_trusted_pair_map.schema.json")
    return list(dict.fromkeys(Path(path).resolve() for path in paths))


def _case_coverage_read_audit_paths(manifest, root: Path) -> list[Path]:
    return [
        ab_manifest._path(root, manifest.case_list.path),
        ab_manifest._path(root, manifest.case_lock.path),
        ab_manifest._path(root, manifest.required_cases_lock.path),
        root / "tests" / "acceptance" / "stage_exits.json",
        root / "docs" / "rust-refactor" / "evidence" / "denominators" / "source-manifest.json",
    ]


def _manifest_bundle_roots(path: Path, root: Path) -> list[Path]:
    doc = _read_json(path)
    roots = [root]
    for side in doc.get("sides", []):
        bundle = Path(side["bundle_dir"])
        roots.append(bundle if bundle.is_absolute() else root / bundle)
    return roots


def _assert_open_read_audit(violations, counts: dict[Path, int], *, required_paths=()) -> None:
    assert violations == [], violations
    repeated = {str(path): count for path, count in counts.items() if count > 1}
    assert not repeated, repeated
    missing = [str(Path(path).resolve()) for path in required_paths if counts.get(Path(path).resolve(), 0) < 1]
    assert not missing, f"必要輸入未經讀取：{missing}"


def _verify_with_read_tracking(manifest, root: Path, monkeypatch, *, first_read_overrides=None):
    """只在 verify_frozen 期間追蹤 repo／bundle 開檔，並可注入首次讀取內容。"""
    runner = _ToolRevisionRecordingRunner(
        root,
        _repo_snapshot(root),
        contract_revision=manifest.contract_revision,
        tool_revision="1" * 40,
    )
    try:
        identity_sha = ab_manifest._tree_sha256(root / "tools" / "stage3")
    except (OSError, HarnessError):
        identity_sha = None
    trusted_pair_map = (
        _synthetic_trusted_pair_map(root)
        if manifest.exit_item_id == EXIT_WINDOWS_L15 else None
    )
    base_source_reader = _trusted_source_reader(root)
    from tools.stage3 import required_cases

    seed_sources = {
        path: fakes._seed_source_reader(root, required_cases.SEED_REVISION, path)
        for path in required_cases.SEED_FILES
    }

    def source_reader(revision: str, path: str) -> bytes | None:
        if revision == required_cases.SEED_REVISION:
            return seed_sources.get(path)
        return base_source_reader(revision, path)

    overrides = {
        Path(path).resolve(): content
        for path, content in (first_read_overrides or {}).items()
    }
    counts = _track_open_reads(
        monkeypatch, [root, *(side.bundle_dir for side in manifest.sides)],
    )

    # 保持現有內容漂移測試可對快照 reader 注入首次內容；計數本身在 open/io.open。
    original_snapshot_read = ab_manifest._InputSnapshot.read_bytes

    def tracked_snapshot_read(snapshot, path):
        key = ab_manifest._InputSnapshot._key(path)
        data = original_snapshot_read(snapshot, path)
        if key in overrides:
            snapshot._values[key] = overrides[key]
            return overrides[key]
        return data

    monkeypatch.setattr(ab_manifest._InputSnapshot, "read_bytes", tracked_snapshot_read)
    options = dict(
        m=manifest,
        repo_root=root,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=source_reader,
        trusted_pair_map=trusted_pair_map,
    )
    with patch.object(ab_manifest, "_executing_tools_identity", lambda: (root, identity_sha)):
        violations = ab_manifest.verify_frozen(**options)
    return violations, counts


@pytest.mark.parametrize(
    "entry_point",
    ["read_bytes", "read_text", "path_open", "builtin_open", "io_open", "yaml_safe_load_path_open"],
    ids=["Path-read_bytes", "Path-read_text", "Path-open", "open", "io-open", "yaml-safe_load-Path-open"],
)
def test_open_read_tracker_counts_all_supported_entry_points_once(tmp_path, monkeypatch, entry_point):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "input.yaml"
    # 以 bytes 寫入，避免 Windows 文字模式把 LF 轉成 CRLF，影響下方的位元組比對。
    target.write_bytes(b"value: tracked\n")
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"value: ignored\n")
    counts = _track_open_reads(monkeypatch, [root])

    if entry_point == "read_bytes":
        assert target.read_bytes() == b"value: tracked\n"
    elif entry_point == "read_text":
        assert target.read_text(encoding="utf-8") == "value: tracked\n"
    elif entry_point == "path_open":
        with target.open("rb") as stream:
            assert stream.read() == b"value: tracked\n"
    elif entry_point == "builtin_open":
        with open(target, "rb") as stream:
            assert stream.read() == b"value: tracked\n"
    elif entry_point == "io_open":
        with io.open(target, "rb") as stream:
            assert stream.read() == b"value: tracked\n"
    else:
        with target.open(encoding="utf-8") as stream:
            assert yaml.safe_load(stream) == {"value": "tracked"}

    outside.read_bytes()
    (root / "write-only.bin").write_bytes(b"write mode is excluded")
    assert counts == {target.resolve(): 1}


def test_load_manifest_reads_each_frozen_input_at_most_once(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    _install_test_schemas(tmp_path)
    counts = _track_open_reads(monkeypatch, _manifest_bundle_roots(path, tmp_path))

    manifest = ab_manifest.load_manifest(path, repo_root=tmp_path)

    _assert_open_read_audit([], counts, required_paths=_frozen_read_audit_paths(manifest, tmp_path))


def test_verify_frozen_coverage_uses_first_case_catalog_read(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    case_path = tmp_path / manifest.case_list.path
    original = case_path.read_bytes()
    cases_doc = yaml.safe_load(original.decode("utf-8"))
    target = next(row for row in cases_doc["cases"] if row["case_id"] in manifest.case_ids)
    target["destructive"] = not bool(target.get("destructive"))
    drifted = yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False).encode("utf-8")

    violations, counts = _verify_with_read_tracking(
        manifest, tmp_path, monkeypatch, first_read_overrides={case_path: drifted},
    )

    assert counts.get(case_path.resolve(), 0) == 1
    assert f"case_list sha256 mismatch: {manifest.case_list.path}" in violations


def test_required_cases_uses_one_lock_read_for_validation_and_selection(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    lock_path = tmp_path / manifest.required_cases_lock.path
    original = lock_path.read_bytes()
    changed_doc = json.loads(original.decode("utf-8"))
    ids = changed_doc["exits"][manifest.exit_item_id][manifest.platform]["case_ids"]
    ids.pop()
    drifted = json.dumps(changed_doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    reads = 0

    def drift_after_first_read(candidate: Path) -> bytes:
        nonlocal reads
        if Path(candidate).resolve() == lock_path.resolve():
            reads += 1
            return original if reads == 1 else drifted
        return Path(candidate).read_bytes()

    required, _rows = ab_manifest.required_cases(
        manifest,
        repo_root=tmp_path,
        source_reader=_trusted_source_reader(tmp_path),
        read_bytes=drift_after_first_read,
    )

    assert reads == 1
    assert required == set(manifest.case_ids)


@pytest.mark.parametrize(
    ("input_kind", "platform", "relative", "error"),
    [
        ("catalog", "linux", "tests/acceptance/stage_exits.json", "exit catalog sha256 mismatch"),
        ("case-lock", "linux", "tests/acceptance/cases.lock.json", "case_lock sha256 mismatch"),
        (
            "source-manifest", "linux",
            "docs/rust-refactor/evidence/denominators/source-manifest.json",
            "stage3 source-manifest sha256 mismatch",
        ),
        (
            "seed-attachment", "linux", "docs/rust-refactor/evidence/denominators/cases.yaml", "種子附件",
        ),
        (
            "windows-runner-map", "windows",
            "tests/acceptance/windows_l15_case_transport_map.json", "tool sha256 mismatch",
        ),
    ],
    ids=["catalog", "case-lock", "source-manifest", "seed-attachment", "windows-runner-map"],
)
def test_verify_frozen_reuses_first_read_for_each_indirect_input(
    tmp_path, monkeypatch, input_kind, platform, relative, error,
):
    if input_kind == "windows-runner-map":
        path = _windows_l15_transport_fixture(tmp_path)
    else:
        path = fakes.write_ab_manifest(tmp_path, platform=platform, layer="l1.5", stage="3")
    manifest = _load(path)
    target = tmp_path / relative
    original = target.read_bytes()
    violations, counts = _verify_with_read_tracking(
        manifest, tmp_path, monkeypatch,
        first_read_overrides={target: original + b"\n"},
    )

    assert counts.get(target.resolve(), 0) == 1
    assert any(error in item for item in violations)


def test_verify_frozen_reads_each_path_at_most_once(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    violations, counts = _verify_with_read_tracking(manifest, tmp_path, monkeypatch)

    _assert_open_read_audit(
        violations, counts, required_paths=_frozen_read_audit_paths(manifest, tmp_path),
    )


@pytest.mark.parametrize(
    "read_mode",
    ["builtin", "pathlib"],
    ids=["duplicate-open", "duplicate-Path-open"],
)
def test_verify_frozen_read_audit_rejects_second_case_open(
    tmp_path, monkeypatch, read_mode,
):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    case_path = ab_manifest._path(tmp_path, manifest.case_list.path)
    original_read_cases = ab_manifest._read_cases
    injected = False

    def read_cases_with_second_open(repo_root, selected_manifest, *, read_bytes=None):
        nonlocal injected
        if not injected:
            injected = True
            if read_mode == "builtin":
                with open(case_path, "rb") as stream:
                    stream.read()
            else:
                with case_path.open("rb") as stream:
                    stream.read()
        return original_read_cases(repo_root, selected_manifest, read_bytes=read_bytes)

    monkeypatch.setattr(ab_manifest, "_read_cases", read_cases_with_second_open)
    violations, counts = _verify_with_read_tracking(manifest, tmp_path, monkeypatch)

    assert injected
    assert violations == []
    assert counts.get(case_path.resolve()) == 2
    with pytest.raises(AssertionError):
        _assert_open_read_audit(
            violations, counts, required_paths=_frozen_read_audit_paths(manifest, tmp_path),
        )


def test_windows_l15_case_mapping_reads_each_path_at_most_once(tmp_path, monkeypatch):
    path = _windows_l15_transport_fixture(tmp_path)
    manifest = _load(path, repo_root=tmp_path)
    violations, counts = _verify_with_read_tracking(manifest, tmp_path, monkeypatch)

    _assert_open_read_audit(
        violations, counts, required_paths=_frozen_read_audit_paths(manifest, tmp_path),
    )


def test_verify_case_coverage_entry_reads_each_path_at_most_once(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    counts = _track_open_reads(monkeypatch, [tmp_path, *(side.bundle_dir for side in manifest.sides)])
    violations = ab_manifest.verify_case_coverage(
        manifest, repo_root=tmp_path, base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )

    _assert_open_read_audit(
        violations, counts, required_paths=_case_coverage_read_audit_paths(manifest, tmp_path),
    )


def test_required_cases_entry_reads_each_path_at_most_once(tmp_path, monkeypatch):
    path = fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    manifest = _load(path)
    counts = _track_open_reads(monkeypatch, [tmp_path, *(side.bundle_dir for side in manifest.sides)])
    required, _rows = ab_manifest.required_cases(
        manifest, repo_root=tmp_path, base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(tmp_path),
    )

    assert required == set(manifest.case_ids)
    _assert_open_read_audit(
        [], counts, required_paths=_case_coverage_read_audit_paths(manifest, tmp_path),
    )


def test_validate_current_lock_standalone_reads_each_path_at_most_once(tmp_path, monkeypatch):
    """T1 的獨立入口未注入讀取器時，同一次呼叫內每個路徑也只讀一次。"""
    from tools.stage3 import required_cases as required_cases_module

    fakes.write_ab_manifest(tmp_path, platform="linux", layer="l1.5", stage="3")
    counts = _track_open_reads(monkeypatch, [tmp_path])
    errors = required_cases_module.validate_current_lock(
        tmp_path, base_revision="t3-trusted-base", source_reader=_trusted_source_reader(tmp_path),
    )

    evidence = tmp_path / "docs" / "rust-refactor" / "evidence"
    _assert_open_read_audit(
        errors, counts,
        required_paths=[
            tmp_path / "tests" / "acceptance" / "cases.yaml",
            tmp_path / "tests" / "acceptance" / "stage3_required_cases.lock.json",
            evidence / "denominators" / "source-manifest.json",
        ],
    )


def _write_case_catalog(path: Path, catalog: dict) -> None:
    cases_path = path.parent / "tests" / "acceptance" / "cases.yaml"
    cases_path.write_text(
        yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    manifest_doc = _read_json(path)
    manifest_doc["case_list"]["sha256"] = fakes.sha256_file(cases_path)
    _write_json(path, manifest_doc)


def test_r9_selected_cross_layer_destructive_case_requires_authorization(tmp_path):
    target = "f7-push-stop-midline-clean-restart"
    path = fakes.write_ab_manifest(
        tmp_path, platform="linux", layer="l1.5", stage="3", authorization=None,
    )
    cases, _ = _case_catalog(tmp_path)
    row = next(case for case in cases["cases"] if case["case_id"] == target)
    assert row["layer"].casefold() == "l2"
    assert row["destructive"] is True
    assert "linux" in row["platforms"]
    doc = _read_json(path)
    assert target not in doc["case_ids"]
    doc["case_ids"].append(target)
    _write_json(path, doc)

    manifest = _load(path)
    violations = _verify(manifest, tmp_path)

    assert f"destructive case without authorization: {target}" in violations


def test_r9_selected_cross_platform_destructive_case_requires_authorization(tmp_path):
    target = "f7-push-stop-midline-clean-restart"
    path = fakes.write_ab_manifest(
        tmp_path, platform="windows", layer="l2", stage="2.1", authorization=None,
    )
    cases, _ = _case_catalog(tmp_path)
    row = next(case for case in cases["cases"] if case["case_id"] == target)
    assert row["layer"].casefold() == "l2"
    assert row["destructive"] is True
    assert "windows" not in row["platforms"]
    doc = _read_json(path)
    doc["case_ids"] = [target]
    _write_json(path, doc)

    manifest = _load(path)
    violations = _verify(manifest, tmp_path)

    assert f"destructive case without authorization: {target}" in violations


def test_r9_selected_cross_layer_destructive_case_with_authorization_passes(tmp_path):
    target = "f7-push-stop-midline-clean-restart"
    path = fakes.write_ab_manifest(
        tmp_path, platform="linux", layer="l1.5", stage="3", authorization=None,
    )
    doc = _read_json(path)
    doc["case_ids"].append(target)
    doc["authorization"] = {
        "allow_destructive": True,
        "decision_ref": "R9-APPROVED-DECISION",
        "boards": [],
    }
    _write_json(path, doc)

    manifest = _load(path)
    violations = _verify(manifest, tmp_path)

    assert f"destructive case without authorization: {target}" not in violations
    assert not any(item.startswith("destructive case without authorization:") for item in violations)


def test_r9_windows_l2_invariant_without_windows_cases_is_not_required(tmp_path):
    invariant = "no_auto_resend"
    path = fakes.write_ab_manifest(
        tmp_path, platform="windows", layer="l2", stage="2.1",
    )
    cases_path = tmp_path / "tests" / "acceptance" / "cases.yaml"
    catalog = yaml.safe_load(cases_path.read_bytes())
    changed = []
    for row in catalog["cases"]:
        if (str(row.get("layer", "")).casefold() == "l2"
                and invariant in (row.get("invariants") or ())):
            row["platforms"] = ["linux"]
            changed.append(row["case_id"])
    assert changed
    assert all(
        "linux" in row["platforms"] and "windows" not in row["platforms"]
        for row in catalog["cases"] if row.get("case_id") in changed
    )
    _write_case_catalog(path, catalog)

    manifest = _load(path)
    violations = _coverage(manifest, tmp_path)

    assert f"invariant not covered: {invariant}" not in violations


def test_r9_windows_l2_invariant_referenced_but_only_linux_case_selected_is_uncovered(tmp_path):
    invariant = "no_reopen_before_handoff"
    linux_case = "f8-legacy-attached-upgrade"
    path = fakes.write_ab_manifest(
        tmp_path, platform="windows", layer="l2", stage="2.1",
    )
    doc = _read_json(path)
    doc["case_ids"] = [linux_case]
    _write_json(path, doc)
    cases, _ = _case_catalog(tmp_path)
    assert "linux" in next(row for row in cases["cases"] if row["case_id"] == linux_case)["platforms"]
    assert any(
        "windows" in row.get("platforms", [])
        and invariant in (row.get("invariants") or ())
        and str(row.get("layer", "")).casefold() == "l2"
        for row in cases["cases"]
    )

    manifest = _load(path)
    violations = _coverage(manifest, tmp_path)

    assert f"invariant not covered: {invariant}" in violations


def test_r9_windows_l2_invariant_selected_for_matching_platform_is_covered(tmp_path):
    invariant = "no_reopen_before_handoff"
    windows_case = "f8-tty-holders-reported"
    path = fakes.write_ab_manifest(
        tmp_path, platform="windows", layer="l2", stage="2.1",
    )
    doc = _read_json(path)
    doc["case_ids"] = [windows_case]
    _write_json(path, doc)
    cases, _ = _case_catalog(tmp_path)
    row = next(row for row in cases["cases"] if row["case_id"] == windows_case)
    assert "windows" in row["platforms"]
    assert invariant in row["invariants"]

    manifest = _load(path)
    violations = _coverage(manifest, tmp_path)

    assert f"invariant not covered: {invariant}" not in violations


_STATIC_READ_ALLOWLIST = {
    "ab_manifest:_InputSnapshot.read_bytes": {
        "apis": {"snapshot-reader:call": 1},
        "reason": "快照唯一的底層讀取呼叫，快取同一 resolved path 的位元組或讀取錯誤。",
    },
    "ab_manifest:_InputSnapshot.__init__": {
        "apis": {"Path.read_bytes:reference": 1},
        "reason": "只將 Path.read_bytes 設為快照預設 reader；實際讀取由 read_bytes 的單路徑快取控制。",
    },
    "ab_manifest:_read_bytes": {
        "apis": {"Path.read_bytes:reference": 1},
        "reason": "獨立 helper 保留預設讀檔行為；load_manifest、verify_frozen 與 coverage 呼叫鏈會注入快照 reader。",
    },
    "required_cases:_read_bytes": {
        "apis": {"Path.read_bytes:reference": 1},
        "reason": "T1 CLI 與獨立公開函式保留預設 reader；T3 驗證呼叫鏈會注入同一份輸入快照。",
    },
    "candidate_context:load_run_context": {
        "apis": {"Path.read_bytes:call": 1},
        "reason": "獨立檔案 API；A/B loader 使用 load_run_context_bytes 解析快照位元組，不呼叫此入口。",
    },
    "reason_codes:_load_registry": {
        "apis": {"resource.read_text:call": 1},
        "reason": "透過 importlib.resources 讀取套件內固定原因碼資源，供例外驗證，並非 manifest 凍結輸入。",
    },
    "jsonio:sha256_file": {
        "apis": {"Path.open:call": 1},
        "reason": "通用單檔雜湊 helper；A/B manifest 的凍結路徑改由入口快照提供位元組。",
    },
    "jsonio:append_ndjson": {
        "apis": {"Path.open:call": 2},
        "reason": "一般 WAL/NDJSON 輸出 append；不是 A/B manifest 輸入讀取。",
    },
    "jsonio:read_ndjson": {
        "apis": {"Path.open:call": 1},
        "reason": "通用 WAL/NDJSON 讀取 helper，不讀取 A/B manifest 凍結輸入。",
    },
    "jsonio:atomic_write_text": {
        "apis": {"os.fdopen:call": 1},
        "reason": "以 fdopen 包裝新建的暫存輸出描述子，原子寫入文字檔。",
    },
    "proc:_SpawnedProc.__init__": {
        "apis": {"Path.open:call": 1},
        "reason": "為子行程設定 stdin/stdout/stderr redirection，不讀取 manifest 輸入。",
    },
}
_STATIC_DYNAMIC_ALLOWLIST = {
    "reason_codes:_load_registry": {
        "apis": {"dynamic:importlib-import": 1, "dynamic:importlib-reference": 4},
        "reason": "使用 importlib.resources 讀取套件資源，避免依賴執行目錄並支援 zipapp。",
    },
}
_STATIC_PARSER_ALLOWLIST = {
    "required_cases:_base_errors": {
        "parsers": {"yaml.safe_load"},
        "reason": "base_cases_raw 來自 SourceReader 的 bytes 回傳契約，且在解析前排除 None；AST 無法追蹤 Protocol 回傳型別。",
    },
}
_STATIC_FORBIDDEN_READS = {
    "builtins.open", "open", "io.open", "io.FileIO", "os.open", "os.read",
    "os.fdopen", "codecs.open", "mmap", "mmap.mmap",
}
_STATIC_PATH_READ_METHODS = {"open", "read_bytes", "read_text"}
_STATIC_GETATTR_FORBIDDEN_NAMES = {
    "open", "FileIO", "fdopen", "read", "read_bytes", "read_text", "mmap",
    "load", "safe_load", "__import__", "importlib", "exec", "eval", "compile",
}
_STATIC_DYNAMIC_NAMES = {"__import__", "importlib", "exec", "eval", "compile"}
_STATIC_PARSER_NAMES = {"yaml.safe_load", "yaml.load", "json.load"}


def _static_from_module(module: str, node: ast.ImportFrom, *, is_package: bool = False) -> str | None:
    """將相對匯入解析成完整模組名稱；套件 `__init__.py` 的相對匯入以套件本身為基準。"""
    if node.level == 0:
        return node.module
    parts = module.split(".")
    package_parts = parts if is_package or module == "tools.stage3" else parts[:-1]
    remove = max(node.level - 1, 0)
    if remove > len(package_parts):
        return None
    base = package_parts[:len(package_parts) - remove]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _static_import_aliases(tree: ast.AST, module: str) -> dict[str, str]:
    """解析模組匯入與簡單名稱賦值別名；任意執行期反射不在此解析範圍。"""
    aliases: dict[str, str] = {}
    import_nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in import_nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".", 1)[0]
                # `import a.b` 綁定 a；保留此語意後可由 Attribute 鏈還原 a.b。
                aliases[local] = item.name if item.asname else item.name.split(".", 1)[0]
        else:
            imported_from = _static_from_module(module, node)
            for item in node.names:
                if item.name == "*":
                    continue
                local = item.asname or item.name
                if imported_from:
                    aliases[local] = f"{imported_from}.{item.name}"
                else:
                    aliases[local] = item.name

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            prefix = dotted(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    # 一般賦值別名的來源名稱仍會先被記錄；固定點解析也讓後續呼叫可還原 API。
    for _ in range(len(aliases) + 1):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                target_name = dotted(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases and target_name:
                        aliases[target.id] = target_name
                        changed = True
            elif isinstance(node, ast.AnnAssign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                target_name = dotted(node.value)
                if isinstance(node.target, ast.Name) and node.target.id not in aliases and target_name:
                    aliases[node.target.id] = target_name
                    changed = True
        if not changed:
            break
    return aliases


def _static_function_owners(tree: ast.AST, module: str):
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    function_names: dict[ast.AST, str] = {}

    class _FunctionOwners(ast.NodeVisitor):
        def __init__(self):
            self.scopes: list[str] = []

        def visit_ClassDef(self, node):
            self.scopes.append(node.name)
            self.generic_visit(node)
            self.scopes.pop()

        def visit_FunctionDef(self, node):
            self.scopes.append(node.name)
            function_names[node] = ".".join(self.scopes)
            self.generic_visit(node)
            self.scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    _FunctionOwners().visit(tree)

    def owner(node: ast.AST) -> str:
        current = node
        while current in parents:
            current = parents[current]
            if current in function_names:
                return f"{module}:{function_names[current]}"
        return f"{module}:<module>"

    return parents, function_names, owner


def _static_read_inventory(
    source: str, module: str, *, functions: set[str] | None = None,
) -> tuple[Counter, list[str]]:
    """以有限 AST 規則稽核快照讀取入口；此檢查是回歸防護，不是任意 Python 動態語義證明。

    檢查會解析匯入別名、簡單賦值別名、指定讀取 API 的所有 Name／Attribute 引用、
    `getattr` 與禁止的動態執行入口，以及 YAML／JSON parser 的輸入形狀。它不承諾
    覆蓋任意自訂 wrapper、跨函式資料流、反射組合或其他刻意隱藏 API 的動態程式；
    repo 程式碼仍須經一般審查。此為協調者核定的靜態稽核範圍。
    """
    tree = ast.parse(source)
    import_aliases = _static_import_aliases(tree, module)
    parents, function_names, owner = _static_function_owners(tree, module)

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            value = import_aliases.get(node.id, node.id)
            if value in {"open", "getattr", "exec", "eval", "compile", "__import__", "bytes", "str"}:
                return f"builtins.{value}"
            return value
        if isinstance(node, ast.Attribute):
            prefix = dotted(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        if isinstance(node, ast.Call):
            return dotted(node.func)
        return ""

    def selected(current_owner: str) -> bool:
        if functions is None:
            return True
        function_owner = current_owner.split(":", 1)[-1]
        return any(function_owner == name or function_owner.startswith(name + ".") for name in functions)

    def annotation_kind(annotation: ast.AST | None) -> str | None:
        if annotation is None:
            return None
        if isinstance(annotation, ast.Name) and annotation.id in {"bytes", "str"}:
            return annotation.id
        if isinstance(annotation, ast.Attribute) and dotted(annotation) in {"builtins.bytes", "builtins.str"}:
            return dotted(annotation).split(".")[-1]
        if isinstance(annotation, ast.Subscript):
            return annotation_kind(annotation.value)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            left = annotation_kind(annotation.left)
            right = annotation_kind(annotation.right)
            if isinstance(annotation.left, ast.Constant) and annotation.left.value is None:
                return right
            if isinstance(annotation.right, ast.Constant) and annotation.right.value is None:
                return left
            return left if left is not None and left == right else None
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            return annotation.value if annotation.value in {"bytes", "str"} else None
        return None

    variable_kinds: dict[tuple[str, str], str] = {}

    def expression_kind(expression: ast.AST | None, current_owner: str) -> str | None:
        if expression is None:
            return None
        if isinstance(expression, ast.Constant) and isinstance(expression.value, (bytes, str)):
            return "bytes" if isinstance(expression.value, bytes) else "str"
        if isinstance(expression, ast.Name):
            return variable_kinds.get((current_owner, expression.id))
        if isinstance(expression, ast.Call):
            callee = dotted(expression.func)
            if callee in {"builtins.bytes", "builtins.str"}:
                return callee.rsplit(".", 1)[-1]
            if callee in {"_read_bytes", "read_bytes"} or callee.endswith("._read_bytes") or callee.endswith(".read_bytes"):
                receiver = dotted(expression.func.value) if isinstance(expression.func, ast.Attribute) else ""
                if callee in {"_read_bytes", "read_bytes"} or callee.endswith("._read_bytes") or receiver in {"snapshot", "input_snapshot", "self._snapshot"}:
                    return "bytes"
            if isinstance(expression.func, ast.Attribute) and expression.func.attr == "decode":
                if expression_kind(expression.func.value, current_owner) == "bytes":
                    return "str"
        if isinstance(expression, ast.IfExp):
            left = expression_kind(expression.body, current_owner)
            right = expression_kind(expression.orelse, current_owner)
            return left if left == right else None
        return None

    # 收集函式參數與可直接判定的局部指派型別；未能判定者不會被推定為安全文字。
    # 同一變數的每一次綁定（參數註記、指派、增量指派、for／with／walrus 目標、解構）都要記錄；
    # 只要有一次綁定的來源無法判定，或各次綁定的型別不一致，就不採信該變數的型別。
    def bound_names(target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [name for element in target.elts for name in bound_names(element)]
        if isinstance(target, ast.Starred):
            return bound_names(target.value)
        return []

    for _ in range(10):
        bindings: dict[tuple[str, str], set[str | None]] = {}

        def bind(key: tuple[str, str], kind: str | None) -> None:
            bindings.setdefault(key, set()).add(kind)

        for node in ast.walk(tree):
            current_owner = owner(node)
            if not selected(current_owner):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    bind((f"{module}:{node.name}", argument.arg), annotation_kind(argument.annotation))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is None:
                    kind = annotation_kind(node.annotation)
                else:
                    kind = expression_kind(node.value, current_owner)
                bind((current_owner, node.target.id), kind)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    kind = expression_kind(node.value, current_owner) if isinstance(target, ast.Name) else None
                    for name in bound_names(target):
                        bind((current_owner, name), kind)
            elif isinstance(node, ast.AugAssign):
                for name in bound_names(node.target):
                    bind((current_owner, name), None)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                for name in bound_names(node.target):
                    bind((current_owner, name), None)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for name in bound_names(item.optional_vars):
                            bind((current_owner, name), None)
            elif isinstance(node, ast.NamedExpr):
                bind((current_owner, node.target.id), expression_kind(node.value, current_owner))

        resolved_kinds = {
            key: next(iter(kinds)) for key, kinds in bindings.items()
            if len(kinds) == 1 and None not in kinds
        }
        if resolved_kinds == variable_kinds:
            break
        variable_kinds.clear()
        variable_kinds.update(resolved_kinds)

    observations: Counter = Counter()
    violations: list[str] = []
    call_targets = {node.func for node in ast.walk(tree) if isinstance(node, ast.Call)}

    def add(current_owner: str, api: str) -> None:
        observations[(current_owner, api)] += 1

    for node in ast.walk(tree):
        current_owner = owner(node)
        if not selected(current_owner):
            continue

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                imported_from = _static_from_module(module, node) or ""
                for item in node.names:
                    if item.name == "*":
                        # 萬用匯入可能帶入任何禁止的讀取 API，無法靜態列舉，一律保守拒絕。
                        add(current_owner, "dynamic:wildcard-import")
                        continue
                    imported_api = f"{imported_from}.{item.name}" if imported_from else item.name
                    if imported_api in _STATIC_FORBIDDEN_READS or imported_api == "mmap.mmap":
                        api = "builtins.open" if imported_api == "open" else imported_api
                        add(current_owner, f"{api}:reference")
            else:
                for item in node.names:
                    if item.name == "mmap":
                        add(current_owner, "mmap:reference")
            imports_importlib = (
                any(item.name == "importlib" or item.name.startswith("importlib.") for item in node.names)
                if isinstance(node, ast.Import)
                else bool(node.module and (node.module == "importlib" or node.module.startswith("importlib.")))
            )
            if imports_importlib:
                if isinstance(node, ast.Import):
                    local_names = {item.asname or item.name.split(".", 1)[0] for item in node.names}
                else:
                    local_names = {item.asname or item.name for item in node.names}
                usage_owners = {
                    owner(reference) for reference in ast.walk(tree)
                    if isinstance(reference, ast.Name)
                    and reference.id in local_names
                    and owner(reference) != current_owner
                }
                import_owner = next(iter(usage_owners)) if len(usage_owners) == 1 else current_owner
                add(import_owner, "dynamic:importlib-import")

        if isinstance(node, ast.Name):
            resolved = dotted(node)
            if node.id == "importlib" or resolved == "importlib" or resolved.startswith("importlib."):
                add(current_owner, "dynamic:importlib-reference")
            if resolved in _STATIC_FORBIDDEN_READS or node.id == "mmap":
                api = "Path.read_bytes" if resolved.endswith(".read_bytes") else None
                if api is None:
                    if resolved in {"builtins.open", "open"}:
                        api = "builtins.open"
                    elif resolved in {"io.FileIO", "io.open", "os.open", "os.read", "os.fdopen", "codecs.open", "mmap", "mmap.mmap"}:
                        api = resolved
                    elif node.id == "mmap":
                        api = "mmap"
                if api:
                    add(current_owner, f"{api}:{'call' if node in call_targets else 'reference'}")
            dynamic_name = node.id if node.id in _STATIC_DYNAMIC_NAMES else resolved.rsplit(".", 1)[-1]
            if dynamic_name in {"__import__", "exec", "eval", "compile"} and (
                node.id in _STATIC_DYNAMIC_NAMES or resolved == f"builtins.{dynamic_name}"
            ):
                add(current_owner, f"dynamic:{dynamic_name}")

        elif isinstance(node, ast.Attribute):
            resolved = dotted(node)
            if resolved == "importlib" or resolved.startswith("importlib."):
                add(current_owner, "dynamic:importlib-reference")
            api = None
            if resolved in _STATIC_FORBIDDEN_READS:
                api = resolved
            elif node.attr in _STATIC_PATH_READ_METHODS:
                receiver = dotted(node.value)
                receiver_is_snapshot = receiver in {"self._reader", "snapshot", "input_snapshot", "self._snapshot", "_snapshot"}
                receiver_is_own_snapshot = receiver == "self" and current_owner.startswith(f"{module}:_InputSnapshot.")
                if node.attr == "read_bytes" and (receiver_is_snapshot or receiver_is_own_snapshot):
                    if receiver == "self._reader" and current_owner == f"{module}:_InputSnapshot.read_bytes":
                        api = "snapshot-reader"
                    # 快照方法的引用與呼叫都走入口建立的單路徑快取。
                elif node.attr == "read_bytes":
                    api = "Path.read_bytes"
                elif node.attr == "read_text" and ".resources.files" in resolved:
                    api = "resource.read_text"
                else:
                    if not (node.attr == "read_bytes" and (receiver_is_snapshot or receiver_is_own_snapshot)):
                        api = f"Path.{node.attr}"
            if api:
                add(current_owner, f"{api}:{'call' if node in call_targets else 'reference'}")
            if resolved in {f"builtins.{name}" for name in ("__import__", "exec", "eval", "compile")}:
                add(current_owner, f"dynamic:{resolved.rsplit('.', 1)[-1]}")

        if isinstance(node, ast.Call):
            callee = dotted(node.func)
            if callee == "self._reader":
                add(current_owner, "snapshot-reader:call")
            if callee in {"builtins.getattr", "getattr"}:
                attribute_name = node.args[1] if len(node.args) > 1 else None
                if not isinstance(attribute_name, ast.Constant) or not isinstance(attribute_name.value, str):
                    add(current_owner, "dynamic:getattr-nonliteral")
                elif attribute_name.value in _STATIC_GETATTR_FORBIDDEN_NAMES:
                    add(current_owner, f"dynamic:getattr-{attribute_name.value}")

            parser_name = next((name for name in _STATIC_PARSER_NAMES if callee == name or callee.endswith("." + name)), None)
            if parser_name:
                argument = node.args[0] if node.args else None
                kind = expression_kind(argument, current_owner)
                parser_variable = (
                    isinstance(argument, ast.Name)
                    and kind in {"bytes", "str"}
                    and not argument.id.casefold().endswith(("path", "file", "stream", "handle"))
                )
                decoded_content = (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Attribute)
                    and argument.func.attr == "decode"
                    and expression_kind(argument.func.value, current_owner) == "bytes"
                )
                snapshot_bytes_call = (
                    isinstance(argument, ast.Call)
                    and (
                        (isinstance(argument.func, ast.Attribute)
                         and argument.func.attr == "read_bytes"
                         and dotted(argument.func.value) in {"snapshot", "input_snapshot", "self._snapshot"})
                        or dotted(argument.func) in {"_read_bytes", "read_bytes"}
                    )
                )
                is_snapshot_call = (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Attribute)
                    and argument.func.attr == "read_bytes"
                    and dotted(argument.func.value) in {"snapshot", "input_snapshot", "self._snapshot"}
                )
                if not (parser_variable or decoded_content or snapshot_bytes_call or is_snapshot_call):
                    parser_key = current_owner
                    allowed = _STATIC_PARSER_ALLOWLIST.get(parser_key, {})
                    if parser_name not in allowed.get("parsers", set()):
                        violations.append(
                            f"{current_owner}: {parser_name} input is not a statically known bytes/str snapshot value"
                        )
                    else:
                        add(current_owner, f"parser:{parser_name}:unresolved-input")
        elif isinstance(node, (ast.Name, ast.Attribute)):
            resolved = dotted(node)
            parser_name = next((name for name in _STATIC_PARSER_NAMES if resolved == name or resolved.endswith("." + name)), None)
            if parser_name and node not in call_targets:
                violations.append(f"{current_owner}: {parser_name} reference escapes a checked parser call")

    expected_by_owner: dict[str, Counter] = {}
    for allowlist in (_STATIC_READ_ALLOWLIST, _STATIC_DYNAMIC_ALLOWLIST):
        for entry, definition in allowlist.items():
            if entry.startswith(f"{module}:"):
                expected_by_owner.setdefault(entry, Counter()).update(definition["apis"])
    for entry, definition in _STATIC_PARSER_ALLOWLIST.items():
        if entry.startswith(f"{module}:"):
            expected_by_owner.setdefault(entry, Counter()).update(
                {f"parser:{name}:unresolved-input": 1 for name in definition["parsers"]}
            )
    for (current_owner, api), count in observations.items():
        expected = expected_by_owner.get(current_owner)
        if expected is None or expected.get(api, 0) < count:
            violations.append(f"{current_owner}: unapproved {api} x{count}")
    for current_owner, expected in expected_by_owner.items():
        actual = Counter({api: count for (site, api), count in observations.items() if site == current_owner})
        if actual != expected:
            violations.append(f"{current_owner}: expected {dict(expected)}, observed {dict(actual)}")
    return observations, violations


def _assert_static_read_policy(source: str, module: str) -> None:
    _observations, violations = _static_read_inventory(source, module)
    assert not violations, "\n".join(violations)


def _stage3_module_path(repo_root: Path, module: str) -> Path | None:
    prefix = "tools.stage3"
    if module != prefix and not module.startswith(prefix + "."):
        return None
    relative = Path(*module.split("."))
    candidate = repo_root / relative.with_suffix(".py")
    package_init = repo_root / relative / "__init__.py"
    if candidate.is_file():
        return candidate
    if package_init.is_file():
        return package_init
    return None


def _stage3_dependency_closure(repo_root: Path, entry_module: str) -> dict[str, Path]:
    """遞迴解析 AST 中的絕對、別名與相對 `tools.stage3` 匯入。"""
    modules: dict[str, Path] = {}
    pending = [entry_module]
    while pending:
        module = pending.pop()
        if module in modules:
            continue
        path = _stage3_module_path(repo_root, module)
        if path is None:
            continue
        modules[module] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            found: set[str] = set()
            if isinstance(node, ast.Import):
                found.update(
                    alias.name for alias in node.names
                    if alias.name == "tools.stage3" or alias.name.startswith("tools.stage3.")
                )
            elif isinstance(node, ast.ImportFrom):
                base = _static_from_module(module, node, is_package=path.name == "__init__.py")
                if base and (base == "tools.stage3" or base.startswith("tools.stage3.")):
                    found.add(base)
                    for alias in node.names:
                        if alias.name != "*":
                            found.add(f"{base}.{alias.name}")
            for dependency in found:
                if dependency not in modules and _stage3_module_path(repo_root, dependency) is not None:
                    pending.append(dependency)
    return modules


def _assert_stage3_dependency_read_policy(repo_root: Path, entry_module: str = "tools.stage3.ab_manifest") -> set[str]:
    modules = _stage3_dependency_closure(repo_root, entry_module)
    violations = []
    for module, path in sorted(modules.items()):
        short_name = module.rsplit(".", 1)[-1]
        _observations, module_violations = _static_read_inventory(
            path.read_text(encoding="utf-8"), short_name,
        )
        violations.extend(module_violations)
    assert not violations, "\n".join(violations)
    return set(modules)


def test_frozen_input_read_apis_are_static_allowlisted():
    """稽核快照呼叫鏈及遞迴匯入依賴；此 AST 防護不證明任意 Python 動態語義。"""
    repo_root = Path(ab_manifest.__file__).resolve().parents[2]
    modules = _assert_stage3_dependency_read_policy(repo_root)
    assert modules == {
        "tools.stage3", "tools.stage3.ab_manifest", "tools.stage3.candidate_context",
        "tools.stage3.jsonio", "tools.stage3.proc", "tools.stage3.reason_codes",
        "tools.stage3.required_cases",
    }


@pytest.mark.parametrize(
    ("injected", "message"),
    [
        (
            "def injected_reader(path):\n    reader = open\n    return reader(path, 'rb')\n",
            "builtins.open",
        ),
        (
            "def injected_reader(path):\n    return getattr(os, 'open')(path, os.O_RDONLY)\n",
            "dynamic:getattr-open",
        ),
        (
            "def injected_reader(os, name):\n    return getattr(os, name)\n",
            "dynamic:getattr-nonliteral",
        ),
        (
            "import io\\nimport yaml\\ndef injected_parser(stream: io.BufferedReader):\\n    return yaml.safe_load(stream)\\n",
            "yaml.safe_load input is not a statically known bytes/str snapshot value",
        ),
        (
            "from yaml import safe_load as parse_yaml\\nimport io\\ndef injected_parser(stream: io.BufferedReader):\\n    return parse_yaml(stream)\\n",
            "yaml.safe_load input is not a statically known bytes/str snapshot value",
        ),
        (
            "from yaml import load as parse_yaml\\nimport io\\ndef injected_parser(stream: io.BufferedReader):\\n    return parse_yaml(stream)\\n",
            "yaml.load input is not a statically known bytes/str snapshot value",
        ),
        (
            "import json as json_module\\nimport io\\ndef injected_parser(stream: io.BufferedReader):\\n    return json_module.load(stream)\\n",
            "json.load input is not a statically known bytes/str snapshot value",
        ),
        (
            "import yaml\\ndef injected_parser(path: str):\\n    return yaml.safe_load(path)\\n",
            "yaml.safe_load input is not a statically known bytes/str snapshot value",
        ),
        (
            "import yaml\\ndef injected_parser(payload: bytes, stream):\\n    payload = stream\\n    return yaml.safe_load(payload)\\n",
            "yaml.safe_load input is not a statically known bytes/str snapshot value",
        ),
        (
            "import yaml\\ndef injected_parser(payload: bytes, streams):\\n    for payload in streams:\\n        pass\\n    return yaml.safe_load(payload)\\n",
            "yaml.safe_load input is not a statically known bytes/str snapshot value",
        ),
    ],
    ids=[
        "assigned-open-alias", "getattr-open-literal", "getattr-variable", "yaml-file-object",
        "yaml-safe-load-alias", "yaml-load-alias", "json-load-alias", "yaml-path-string",
        "parser-arg-reassigned-unknown", "parser-arg-rebound-by-for",
    ],
)
def test_static_read_audit_rejects_temporary_reader_patterns(tmp_path, injected, message):
    source_path = Path(ab_manifest.__file__)
    temporary_copy = tmp_path / source_path.name
    temporary_copy.write_text(
        source_path.read_text(encoding="utf-8") + "\n" + injected.replace("\\n", "\n"),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match=message):
        _assert_static_read_policy(temporary_copy.read_text(encoding="utf-8"), "ab_manifest")


@pytest.mark.parametrize(
    "source",
    [
        "from yaml import safe_load as parse_yaml\\ndef parse(payload: bytes):\\n    return parse_yaml(payload)\\n",
        "import json as json_module\\ndef parse(payload: str):\\n    return json_module.load(payload)\\n",
        "import yaml\\ndef parse(snapshot, path):\\n    return yaml.safe_load(snapshot.read_bytes(path))\\n",
        "import yaml\\ndef parse(payload: bytes):\\n    return yaml.safe_load(payload.decode('utf-8'))\\n",
    ],
    ids=["yaml-import-alias-bytes", "json-module-alias-str", "snapshot-return", "decoded-snapshot-bytes"],
)
def test_static_parser_audit_accepts_bytes_str_and_snapshot_inputs(source):
    _assert_static_read_policy(source.replace("\\n", "\n"), "parser_probe")


@pytest.mark.parametrize(
    "injected",
    [
        "reader = open\\n",
        "import io\\nreader = io.open\\n",
        "import io\\nreader = io.FileIO\\n",
        "import os\\nreader = os.open\\n",
        "import os\\nreader = os.read\\n",
        "import os\\nreader = os.fdopen\\n",
        "import codecs\\nreader = codecs.open\\n",
        "import mmap\\nreader = mmap\\n",
        "from pathlib import Path\\nreader = Path.open\\n",
        "from pathlib import Path\\nreader = Path.read_bytes\\n",
        "from pathlib import Path\\nreader = Path.read_text\\n",
    ],
    ids=[
        "builtins-open", "io-open", "io-fileio", "os-open", "os-read", "os-fdopen",
        "codecs-open", "mmap", "path-open", "path-read-bytes", "path-read-text",
    ],
)
def test_static_read_audit_rejects_any_forbidden_api_reference(tmp_path, injected):
    source_path = Path(ab_manifest.__file__)
    temporary_copy = tmp_path / source_path.name
    temporary_copy.write_text(
        source_path.read_text(encoding="utf-8") + "\n" + injected.replace("\\n", "\n"),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="unapproved"):
        _assert_static_read_policy(temporary_copy.read_text(encoding="utf-8"), "ab_manifest")


@pytest.mark.parametrize(
    "injected",
    [
        "def injected(fd):\\n    return os.open(fd, os.O_RDONLY)\\n",
        "def injected(fd):\\n    return os.read(fd, 1)\\n",
        "def injected(path):\\n    from os import open as fd_open\\n    return fd_open(path, os.O_RDONLY)\\n",
        "def injected(fd):\\n    from os import read as fd_read\\n    return fd_read(fd, 1)\\n",
    ],
    ids=["os-open", "os-read", "os-open-import-alias", "os-read-import-alias"],
)
def test_static_read_audit_rejects_temporary_os_reader(tmp_path, injected):
    source_path = Path(ab_manifest.__file__)
    temporary_copy = tmp_path / source_path.name
    temporary_copy.write_text(
        source_path.read_text(encoding="utf-8") + "\n" + injected.replace("\\n", "\n"),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="unapproved (os\\.|)read|unapproved os\\.open"):
        _assert_static_read_policy(temporary_copy.read_text(encoding="utf-8"), "ab_manifest")


@pytest.mark.parametrize(
    "injected",
    [
        "import builtins\\ndef injected(code):\\n    return builtins.compile(code, '<x>', 'exec')\\n",
        "def injected(code):\\n    return eval(code)\\n",
        "def injected(code):\\n    exec(code)\\n",
        "def injected(name):\\n    return __import__(name)\\n",
        "import importlib\\n",
        "from os import *\\ndef injected(fd):\\n    return read(fd, 100)\\n",
    ],
    ids=["compile", "eval", "exec", "dunder-import", "importlib-import", "wildcard-import"],
)
def test_static_read_audit_rejects_dynamic_execution_apis(tmp_path, injected):
    source_path = Path(ab_manifest.__file__)
    temporary_copy = tmp_path / source_path.name
    temporary_copy.write_text(
        source_path.read_text(encoding="utf-8") + "\n" + injected.replace("\\n", "\n"),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="unapproved dynamic:"):
        _assert_static_read_policy(temporary_copy.read_text(encoding="utf-8"), "ab_manifest")


def test_static_read_audit_follows_imported_stage3_module_into_temporary_copy(tmp_path):
    repo_root = tmp_path / "repo"
    stage3_copy = repo_root / "tools" / "stage3"
    shutil.copytree(Path(ab_manifest.__file__).resolve().parent, stage3_copy, ignore=shutil.ignore_patterns("__pycache__"))
    entry = stage3_copy / "ab_manifest.py"
    with entry.open("a", encoding="utf-8") as stream:
        stream.write("\nimport tools.stage3.injected_reader\n")
    (stage3_copy / "injected_reader.py").write_text(
        "def read_external(path):\n    return open(path, 'rb').read()\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="injected_reader:read_external: unapproved builtins.open"):
        _assert_stage3_dependency_read_policy(repo_root)


@pytest.mark.parametrize(
    "init_source",
    ["from . import reader\n", "from .reader import read_external\n"],
    ids=["from-dot-import-module", "from-dot-module-import-name"],
)
def test_stage3_dependency_scan_follows_relative_imports_inside_nested_package(tmp_path, init_source):
    repo_root = tmp_path / "repo"
    stage3_copy = repo_root / "tools" / "stage3"
    shutil.copytree(Path(ab_manifest.__file__).resolve().parent, stage3_copy, ignore=shutil.ignore_patterns("__pycache__"))
    with (stage3_copy / "ab_manifest.py").open("a", encoding="utf-8") as stream:
        stream.write("\nimport tools.stage3.subpkg\n")
    subpkg = stage3_copy / "subpkg"
    subpkg.mkdir()
    (subpkg / "__init__.py").write_text(init_source, encoding="utf-8")
    (subpkg / "reader.py").write_text(
        "def read_external(path):\n    return open(path, 'rb').read()\n",
        encoding="utf-8",
    )

    modules = _stage3_dependency_closure(repo_root, "tools.stage3.ab_manifest")
    assert {"tools.stage3.subpkg", "tools.stage3.subpkg.reader"} <= set(modules)
    with pytest.raises(AssertionError, match="reader:read_external: unapproved builtins.open"):
        _assert_stage3_dependency_read_policy(repo_root)


def test_stage3_dependency_scan_resolves_absolute_aliased_from_and_relative_imports(tmp_path):
    stage3_copy = tmp_path / "tools" / "stage3"
    stage3_copy.mkdir(parents=True)
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (stage3_copy / "__init__.py").write_text("", encoding="utf-8")
    (stage3_copy / "ab_manifest.py").write_text(
        "import tools.stage3.alpha\n"
        "import tools.stage3.beta as beta_alias\n"
        "from tools.stage3 import gamma\n"
        "from tools.stage3.delta import value\n"
        "from . import epsilon\n"
        "from .zeta import value as zeta_value\n",
        encoding="utf-8",
    )
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
        (stage3_copy / f"{name}.py").write_text("VALUE = 1\n", encoding="utf-8")

    modules = _stage3_dependency_closure(tmp_path, "tools.stage3.ab_manifest")

    assert set(modules) == {
        "tools.stage3", "tools.stage3.ab_manifest", "tools.stage3.alpha", "tools.stage3.beta",
        "tools.stage3.gamma", "tools.stage3.delta", "tools.stage3.epsilon", "tools.stage3.zeta",
    }
