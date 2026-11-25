"""Stage 3 T3：A/B manifest 載入、凍結與案例涵蓋紅燈測試。"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
from dataclasses import FrozenInstanceError, is_dataclass, replace
from pathlib import Path
from typing import Mapping

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
    return ab_manifest.load_manifest(
        path, repo_root=repo_root or Path(path).parent, trusted_pair_map=trusted_pair_map,
    )


def _verify(manifest, repo_root: Path):
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
    return ab_manifest.verify_frozen(
        manifest,
        repo_root=repo_root,
        run=runner,
        tool_revision="1" * 40,
        base_revision="t3-trusted-base",
        source_reader=_trusted_source_reader(repo_root),
        trusted_pair_map=trusted_pair_map,
    )


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


def test_windows_l15_manifest_rejects_changed_frozen_com_requirement(tmp_path):
    path = _windows_l15_transport_fixture(tmp_path)
    doc = _read_json(path)
    map_path = tmp_path / doc["platform_settings"]["case_transport_map"]["path"]
    mapping = _read_json(map_path)
    native_id = next(case_id for case_id, row in mapping["cases"].items() if row["requires_native_com"])
    mapping["cases"][native_id]["requires_native_com"] = False
    _write_json(map_path, mapping)
    manifest = _load(path)
    assert any(value.startswith("tool sha256 mismatch:") for value in _verify(manifest, tmp_path))


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


def _repo_snapshot(root: Path) -> dict[str, bytes]:
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

    def __call__(self, argv, *, env=None, timeout=None, cwd=None):
        args = list(argv)
        if args and args[0] == "git":
            if "rev-parse" in args and args[-1] == "HEAD:contracts":
                stdout, rc = self.contract_revision + "\n", 0
            elif "rev-parse" in args and args[-1] == "HEAD":
                stdout, rc = self.tool_revision + "\n", 0
            elif "rev-parse" in args and args[-1].endswith("^{commit}"):
                revision = args[-1][:-len("^{commit}")]
                stdout, rc = (revision + "\n", 0) if revision == self.tool_revision else ("", 1)
            elif "rev-parse" in args and args[-1].endswith("^{tree}") and ":" in args[-1]:
                tree = args[-1].split(":", 1)[1][:-len("^{tree}")]
                members = [relative for relative in self.blobs if relative.startswith(tree.rstrip("/") + "/")]
                stdout, rc = (_git_blob_oid("\n".join(sorted(members)).encode("utf-8")) + "\n", 0) if members else ("", 1)
            elif "rev-parse" in args and ":" in args[-1]:
                relative = args[-1].split(":", 1)[1]
                data = self.blobs.get(relative)
                stdout, rc = (_git_blob_oid(data) + "\n", 0) if data is not None else ("", 1)
            elif "ls-tree" in args:
                revision = args[args.index("-z") + 1]
                tree = args[-1].removeprefix(":(literal)").rstrip("/")
                if revision != self.tool_revision:
                    stdout, rc = "", 1
                else:
                    members = sorted(
                        relative for relative in self.blobs
                        if relative.startswith(tree + "/")
                        and "__pycache__" not in relative.split("/")
                        and not relative.endswith(".pyc")
                    )
                    stdout, rc = ("\0".join(members) + ("\0" if members else ""), 0)
            elif "ls-files" in args:
                relative = args[-1].removeprefix(":(literal)")
                if relative in self.blobs:
                    stdout, rc = (relative + "\0", 0) if "-z" in args else (relative + "\n", 0)
                else:
                    stdout, rc = "", 1
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
                stdout, rc = "{}", 0
            return fakes.CmdResult(rc=rc, stdout=stdout, stderr="", elapsed_s=0.0, timed_out=False)
        if args and args[0] == "docker":
            return fakes.CmdResult(
                rc=int(self.docker_result.get("rc", 0)),
                stdout=str(self.docker_result.get("stdout", "")),
                stderr=str(self.docker_result.get("stderr", "")),
                elapsed_s=0.0,
                timed_out=bool(self.docker_result.get("timed_out", False)),
            )
        return super().__call__(args, env=env, timeout=timeout, cwd=cwd)


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
    manifest = ab_manifest.load_manifest(path, repo_root=tmp_path, trusted_pair_map=trusted)
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
        ab_manifest.load_manifest(changed_path, repo_root=changed_path.parent, trusted_pair_map=trusted)
    assert caught.value.code == "HARNESS_MANIFEST_INVALID"

    swapped_path = _windows_l15_transport_fixture(tmp_path / "swapped")
    swapped_doc = _read_json(swapped_path)
    swapped_doc["dut"]["com_pair"] = {"daemon_side": "COM20", "bridge_side": "COM21"}
    _write_json(swapped_path, swapped_doc)
    with pytest.raises(HarnessError):
        ab_manifest.load_manifest(swapped_path, repo_root=swapped_path.parent, trusted_pair_map=trusted)

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
        ab_manifest.load_manifest(alternate_path, repo_root=alternate_path.parent, trusted_pair_map=trusted)


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
    violations = ab_manifest.verify_frozen(
        manifest, repo_root=tmp_path, run=runner, tool_revision="1" * 40,
        source_reader=_trusted_source_reader(tmp_path),
    )
    assert f"tool revision tree mismatch: {tree} missing {relative}" in violations


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
    violations = ab_manifest._tool_revision_frozen_errors(
        manifest, repo_root=tmp_path, run=_ToolRevisionRecordingRunner(tmp_path, revision_blobs),
        tool_revision="1" * 40,
    )
    assert f"tool revision tree mismatch: tools/stage3 extra {relative}" in violations


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
        ab_manifest.load_manifest(path, repo_root=tmp_path, trusted_pair_map=trusted)
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

