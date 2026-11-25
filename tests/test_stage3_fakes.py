"""Stage 3 共用基礎替身的紅燈測試。"""

from __future__ import annotations

import hashlib
import importlib
import ast
import json
import re
import shutil
import stat
import tarfile
from datetime import datetime
from pathlib import Path
import zipfile

import pytest
import yaml

from tools.stage3 import candidate_context, canary_policy, jsonio, required_cases


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_DIR = REPO_ROOT / "tests" / "acceptance"
LONGRUN_ACCEPTANCE = REPO_ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml"
LONGRUN_SCHEMA = REPO_ROOT / "realhw" / "acceptance" / "longrun_acceptance.schema.json"
LONGRUN_HISTORY = REPO_ROOT / "realhw" / "acceptance" / "candidate_longrun_starts.ndjson"
DEFERRED_KEYS = ("resources", "min_effective_load", "comparison_uncertainty")
FREEZE_FIELDS = (
    "frozen_at",
    "decision_id",
    "approved_by",
    "overrides",
    "proposal_sha256",
    "baseline_evidence",
)


def _fakes():
    """延遲載入新替身，讓紅燈明確落在缺少的 stage3_fakes 模組。"""
    return importlib.import_module("stage3_fakes")


def _leaf_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _leaf_values(child)
    else:
        yield value


def _protected_source_bytes() -> dict[Path, bytes]:
    paths = (LONGRUN_ACCEPTANCE, LONGRUN_SCHEMA, LONGRUN_HISTORY)
    return {path: path.read_bytes() for path in paths}


def test_wal_record_fields():
    fakes = _fakes()
    assert set(fakes.wal_record(1)) == {
        "cmd_id",
        "com",
        "crc32",
        "dir",
        "len",
        "loss_flag",
        "meta",
        "mono_ts_ns",
        "payload_b64",
        "seq",
        "source",
        "wall_ts",
    }
    assert fakes.wal_record(1, payload=b"abc")["len"] == 3


def test_write_wal_no_trailing_newline(tmp_path):
    fakes = _fakes()
    path = tmp_path / "raw.wal.ndjson"
    fakes.write_wal(path, [fakes.wal_record(1), fakes.wal_record(2)], trailing_newline=False)
    data = path.read_bytes()
    assert data, "寫出的 WAL 檔不應為空"
    assert not data.endswith(b"\n")


def test_make_bundle_hashes_real(tmp_path):
    fakes = _fakes()

    rust = fakes.make_bundle(tmp_path / "rust")
    rust_manifest = json.loads((rust / "release-manifest.json").read_text(encoding="utf-8"))
    rust_daemon = rust_manifest["components"]["daemon"]
    assert rust_daemon["launcher"]["sha256"] == jsonio.sha256_file(rust / "bin" / "serialwrapd")
    assert "path" not in rust_daemon

    py = fakes.make_bundle(tmp_path / "py", daemon_impl="python")
    py_manifest = json.loads((py / "release-manifest.json").read_text(encoding="utf-8"))
    expected_package_tree = _independent_package_tree_sha256(py / "py")
    assert fakes.tree_sha256(py / "py") == _independent_tree_sha256(py / "py")
    assert py_manifest["components"]["daemon"]["package"]["tree_sha256"] == expected_package_tree


def test_make_bundle_stage3_marker(tmp_path):
    fakes = _fakes()

    s3 = fakes.make_bundle(tmp_path / "s3", stage="3")
    m3 = json.loads((s3 / "release-manifest.json").read_text(encoding="utf-8"))
    assert m3["retirement"] == {"python_daemon": "removed"}
    assert "python_cli_delegate" not in m3["components"]

    s24 = fakes.make_bundle(tmp_path / "s24", stage="2.4")
    m24 = json.loads((s24 / "release-manifest.json").read_text(encoding="utf-8"))
    assert "retirement" not in m24


def test_write_ab_manifest_validates_schema(tmp_path):
    import jsonschema

    fakes = _fakes()
    schema = json.loads((ACCEPTANCE_DIR / "ab_manifest.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for layer in ("l1.5", "l2", "l3"):
        path = fakes.write_ab_manifest(tmp_path / layer.replace(".", "_"), layer=layer, platform="linux")
        doc = json.loads(path.read_text(encoding="utf-8"))
        errors = [error.message for error in validator.iter_errors(doc)]
        assert errors == [], f"{layer}: {errors}"


def test_fake_repo_preserves_current_acceptance_bytes(tmp_path):
    fakes = _fakes()
    source_bytes = _protected_source_bytes()
    repo = fakes.write_fake_repo(tmp_path / "current")

    assert (repo / "realhw/acceptance/longrun_acceptance.yaml").read_bytes() == source_bytes[LONGRUN_ACCEPTANCE]
    assert (repo / "realhw/acceptance/longrun_acceptance.schema.json").read_bytes() == source_bytes[LONGRUN_SCHEMA]
    current = yaml.safe_load((repo / "realhw/acceptance/longrun_acceptance.yaml").read_text(encoding="utf-8"))
    for key in DEFERRED_KEYS:
        item = current["thresholds"][key]
        assert item["status"] == "frozen"
        assert all(value is not None for value in _leaf_values(item["per_platform"]))
    assert _protected_source_bytes() == source_bytes


def test_historical_deferred_fixture_is_isolated(tmp_path):
    import jsonschema

    fakes = _fakes()
    source_bytes = _protected_source_bytes()
    source = yaml.safe_load(source_bytes[LONGRUN_ACCEPTANCE].decode("utf-8"))

    historical_repo = fakes.write_fake_repo(tmp_path / "historical", acceptance_state="historical-deferred")
    historical_path = historical_repo / "realhw/acceptance/longrun_acceptance.yaml"
    historical = yaml.safe_load(historical_path.read_text(encoding="utf-8"))
    schema = json.loads((historical_repo / "realhw/acceptance/longrun_acceptance.schema.json").read_text(encoding="utf-8"))

    assert historical["revision"] == source["revision"]
    for key, value in source.items():
        if key != "thresholds":
            assert historical[key] == value
    for key, source_item in source["thresholds"].items():
        item = historical["thresholds"][key]
        if key not in DEFERRED_KEYS:
            assert item == source_item
            continue
        assert item["status"] == "deferred"
        assert all(value is None for value in _leaf_values(item["per_platform"]))
        assert not (set(item) & set(FREEZE_FIELDS))
        for field in FREEZE_FIELDS:
            assert field not in item
        assert item["decision"] == source_item["decision"]
        assert item["freeze_deadline"] == source_item["freeze_deadline"]
        assert set(item["per_platform"]) == set(source_item["per_platform"])

    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(historical))
    assert errors == []

    current_repo = fakes.write_fake_repo(tmp_path / "still-current")
    assert (current_repo / "realhw/acceptance/longrun_acceptance.yaml").read_bytes() == source_bytes[LONGRUN_ACCEPTANCE]
    assert (current_repo / "realhw/acceptance/longrun_acceptance.schema.json").read_bytes() == source_bytes[LONGRUN_SCHEMA]
    assert _protected_source_bytes() == source_bytes


def test_historical_acceptance_is_frozen_into_fake_manifest(tmp_path):
    fakes = _fakes()
    path = fakes.write_ab_manifest(
        tmp_path / "historical-manifest",
        layer="l3",
        platform="linux",
        acceptance_state="historical-deferred",
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    repo = path.parent
    acceptance_ref = doc["acceptance"]
    acceptance_path = repo / acceptance_ref["path"]

    assert acceptance_ref["sha256"] == jsonio.sha256_file(acceptance_path)
    realhw_tree = next(tool for tool in doc["tools"] if tool["path"] == "realhw")
    assert realhw_tree["tree_sha256"] == fakes.tree_sha256(repo / "realhw")
    acceptance = yaml.safe_load(acceptance_path.read_text(encoding="utf-8"))
    assert all(acceptance["thresholds"][key]["status"] == "deferred" for key in DEFERRED_KEYS)


def test_unknown_acceptance_state_rejected(tmp_path):
    fakes = _fakes()
    with pytest.raises(ValueError):
        fakes.write_fake_repo(tmp_path / "unknown-repo", acceptance_state="unknown")
    with pytest.raises(ValueError):
        fakes.write_ab_manifest(tmp_path / "unknown-manifest", acceptance_state="unknown")


def test_write_ab_manifest_required_cases_sha256_matches_t1_lock(tmp_path):
    fakes = _fakes()
    manifest_path = fakes.write_ab_manifest(tmp_path / "locked-manifest", layer="l1.5", platform="linux")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo = manifest_path.parent

    lock_ref = manifest["required_cases_lock"]
    lock_path = repo / lock_ref["path"]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_sha256 = jsonio.sha256_file(lock_path)
    assert manifest["required_cases_sha256"] == lock_sha256
    assert lock_ref["sha256"] == lock_sha256
    assert set(manifest["case_ids"]) == required_cases.required_for_exit(
        lock, manifest["exit_item_id"], manifest["platform"]
    )

    evidence_root = repo / "docs/rust-refactor/evidence"
    for field in ("cases", "case_lock"):
        seed_ref = lock["seed"][field]
        seed_path = evidence_root / seed_ref["path"]
        assert seed_path.is_file()
        assert jsonio.sha256_file(seed_path) == seed_ref["sha256"]

    context_ref = manifest["candidate_context"]
    context = candidate_context.load_run_context(repo / context_ref["path"])
    assert context["required_cases_sha256"] == lock_sha256


def test_write_ab_manifest_uses_shared_candidate_round(tmp_path):
    fakes = _fakes()
    default_rounds = set()
    for layer in ("l1.5", "l2", "l3"):
        for platform in ("linux", "windows"):
            manifest_path = fakes.write_ab_manifest(
                tmp_path / f"default-{layer}-{platform}",
                layer=layer,
                platform=platform,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            context_ref = manifest["candidate_context"]
            context_path = manifest_path.parent / context_ref["path"]
            assert jsonio.sha256_file(context_path) == context_ref["sha256"]
            context = candidate_context.load_run_context(context_path)
            default_rounds.add(context["candidate_round_id"])
    assert len(default_rounds) == 1

    selected_round = "a" * 32
    manifest_path = fakes.write_ab_manifest(
        tmp_path / "override-round",
        layer="l2",
        platform="windows",
        candidate_round_id=selected_round,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    context_ref = manifest["candidate_context"]
    context_path = manifest_path.parent / context_ref["path"]
    assert jsonio.sha256_file(context_path) == context_ref["sha256"]
    context = candidate_context.load_run_context(context_path)
    assert context["candidate_round_id"] == selected_round
    assert manifest["required_cases_sha256"] == context["required_cases_sha256"]


def _independent_tree_sha256(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        assert not path.is_symlink(), relative.as_posix()
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{relative.as_posix()}\0{digest}\n")
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


def _independent_package_tree_sha256(py_dir: Path) -> str:
    """依 release_manifest.package_tree_sha256 契約獨立重算 sw_core 子樹。"""
    package_root = Path(py_dir) / "sw_core"
    rows = []
    paths = sorted(
        package_root.rglob("*"),
        key=lambda item: ("sw_core/" + item.relative_to(package_root).as_posix()).encode("utf-8"),
    )
    for path in paths:
        relative = path.relative_to(package_root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        assert not path.is_symlink(), relative.as_posix()
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"sw_core/{relative.as_posix()}\0{digest}\n")
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


def _independent_task_xml_template_sha256() -> str:
    """依 mkbundle.sh 從 Rust 樣板字串逐段還原後計算 SHA-256。"""
    source_path = REPO_ROOT / "crates/sw-pal/src/service_ctl.rs"
    source = source_path.read_text(encoding="utf-8")
    start = source.index('pub const TASK_XML_TEMPLATE: &str = concat!(')
    match = re.search(r"concat!\((.*?)\n\s*\);", source[start:], re.S)
    assert match is not None
    parts = re.findall(r'^\s*("(?:\\.|[^"\\])*")\s*,?\s*$', match.group(1), re.M)
    assert parts
    template = "".join(ast.literal_eval(part) for part in parts)
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def _assert_bundle_sums_match_files(bundle: Path) -> None:
    expected = []
    for path in sorted(bundle.rglob("*"), key=lambda item: item.relative_to(bundle).as_posix()):
        rel = path.relative_to(bundle).as_posix()
        if path.is_file() and rel not in {"SHA256SUMS", "release-manifest.json"}:
            expected.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}\n")
    assert (bundle / "SHA256SUMS").read_text(encoding="utf-8") == "".join(expected)


@pytest.mark.parametrize(
    ("target", "suffix"),
    [("x86_64-unknown-linux-gnu", ""), ("x86_64-pc-windows-gnu", ".exe")],
)
@pytest.mark.parametrize("daemon_impl", ["rust", "python"])
def test_make_bundle_nonretired_manifest_summaries_recompute_from_real_paths(
    tmp_path, daemon_impl, target, suffix
):
    fakes = _fakes()
    bundle = fakes.make_bundle(
        tmp_path / daemon_impl,
        stage="2.4",
        daemon_impl=daemon_impl,
        target=target,
    )
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    components = manifest["components"]
    cli = components["cli"]
    daemon = components["daemon"]
    runtime = daemon["runtime"]
    package = daemon["package"]
    delegate = components["python_cli_delegate"]

    assert cli["path"] == f"bin/serialwrap{suffix}"
    assert hashlib.sha256((bundle / cli["path"]).read_bytes()).hexdigest() == cli["sha256"]
    assert daemon["launcher"]["path"] == f"bin/serialwrapd{suffix}"
    assert hashlib.sha256((bundle / daemon["launcher"]["path"]).read_bytes()).hexdigest() == daemon["launcher"]["sha256"]
    assert runtime["path"] == "py/bin/python3"
    assert hashlib.sha256((bundle / runtime["path"]).read_bytes()).hexdigest() == runtime["sha256"]
    expected_daemon_entry = (
        ["py/bin/python3", "-I", "-B", "py/serialwrapd.py"]
        if daemon_impl == "python"
        else [daemon["launcher"]["path"]]
    )
    assert daemon["entry"] == expected_daemon_entry
    assert (bundle / daemon["entry"][0]).is_file()
    if daemon_impl == "python":
        assert (bundle / daemon["entry"][-1]).is_file()
    assert package["wheel_sha256"] is None
    assert package["tree_sha256"] == _independent_package_tree_sha256(bundle / "py")
    assert delegate["entry"] == ["py/bin/python3", "-I", "-B", "-m", "sw_core.cli"]
    assert (bundle / delegate["entry"][0]).is_file()
    assert (bundle / "py/sw_core/cli.py").is_file()
    assert delegate["tree_sha256"] == _independent_package_tree_sha256(bundle / "py")

    asset_root = bundle / "py/sw_core/assets"
    skill = asset_root / "skill/SKILL.md"
    skill_windows = asset_root / "skill/SKILL_WINDOWS.md"
    profiles = asset_root / "profiles"
    systemd_units = bundle / "py/sw_core/systemd_units.py"
    assert hashlib.sha256(skill.read_bytes()).hexdigest() == components["assets"]["skill_sha256"]
    assert hashlib.sha256(skill_windows.read_bytes()).hexdigest() == components["assets"]["skill_windows_sha256"]
    assert components["assets"]["profiles_sha256"] == _independent_tree_sha256(profiles)
    assert hashlib.sha256(systemd_units.read_bytes()).hexdigest() == components["supervisor"]["definition_sha256"]
    assert components["supervisor"]["task_xml_template_sha256"] == _independent_task_xml_template_sha256()
    _assert_bundle_sums_match_files(bundle)


@pytest.mark.parametrize(
    ("target", "suffix"),
    [("x86_64-unknown-linux-gnu", ""), ("x86_64-pc-windows-gnu", ".exe")],
)
def test_make_bundle_retired_manifest_summaries_recompute_from_real_paths(tmp_path, target, suffix):
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "retired", stage="3", daemon_impl="rust", target=target)
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    components = manifest["components"]

    assert manifest["retirement"] == {"python_daemon": "removed"}
    assert not (bundle / "py").exists()
    assert "package" not in components["daemon"]
    assert "python_cli_delegate" not in components
    cli = components["cli"]
    daemon = components["daemon"]
    assert cli["path"] == f"bin/serialwrap{suffix}"
    assert hashlib.sha256((bundle / cli["path"]).read_bytes()).hexdigest() == cli["sha256"]
    assert daemon["launcher"]["path"] == f"bin/serialwrapd{suffix}"
    assert hashlib.sha256((bundle / daemon["launcher"]["path"]).read_bytes()).hexdigest() == daemon["launcher"]["sha256"]

    # Task 16 尚未定案退役 bundle 的資產位置；fixture 暫放 assets/pending-t16 的實檔，待 Task 16 定案。
    asset_root = bundle / "assets" / "pending-t16"
    skill = asset_root / "skill/SKILL.md"
    skill_windows = asset_root / "skill/SKILL_WINDOWS.md"
    profiles = asset_root / "profiles"
    systemd_units = asset_root / "systemd_units.py"
    assert hashlib.sha256(skill.read_bytes()).hexdigest() == components["assets"]["skill_sha256"]
    assert hashlib.sha256(skill_windows.read_bytes()).hexdigest() == components["assets"]["skill_windows_sha256"]
    assert components["assets"]["profiles_sha256"] == _independent_tree_sha256(profiles)
    assert hashlib.sha256(systemd_units.read_bytes()).hexdigest() == components["supervisor"]["definition_sha256"]
    assert components["supervisor"]["task_xml_template_sha256"] == _independent_task_xml_template_sha256()
    _assert_bundle_sums_match_files(bundle)


@pytest.mark.parametrize(
    ("relative", "manifest_group", "field"),
    [
        ("py/sw_core/assets/profiles/default.yaml", "assets", "profiles_sha256"),
        ("py/sw_core/assets/skill/SKILL.md", "assets", "skill_sha256"),
        ("py/sw_core/systemd_units.py", "supervisor", "definition_sha256"),
    ],
)
def test_make_bundle_nonretired_asset_mutations_change_declared_summaries(
    tmp_path, relative, manifest_group, field
):
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="2.4", daemon_impl="rust")
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    components = manifest["components"]
    old_package_tree = components["daemon"]["package"]["tree_sha256"]
    old_delegate_tree = components["python_cli_delegate"]["tree_sha256"]
    path = bundle / relative
    original = path.read_bytes()
    path.write_bytes(original + b"modified fixture content\n")

    if field == "profiles_sha256":
        recalculated = _independent_tree_sha256(path.parent)
    else:
        recalculated = hashlib.sha256(path.read_bytes()).hexdigest()
    assert recalculated != components[manifest_group][field]
    new_package_tree = _independent_package_tree_sha256(bundle / "py")
    assert new_package_tree != old_package_tree
    assert new_package_tree != old_delegate_tree


def test_make_bundle_python_delegate_tree_is_real_before_retirement(tmp_path):
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "rust-stage-24", stage="2.4", daemon_impl="rust")
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    delegate = manifest["components"]["python_cli_delegate"]
    runtime = bundle / "py"
    assert runtime.is_dir()
    assert (bundle / delegate["entry"][0]).is_file()
    assert delegate["tree_sha256"] == _independent_package_tree_sha256(runtime)

    retired = fakes.make_bundle(tmp_path / "rust-stage-3", stage="3", daemon_impl="rust")
    retired_manifest = json.loads((retired / "release-manifest.json").read_text(encoding="utf-8"))
    assert retired_manifest["retirement"] == {"python_daemon": "removed"}
    assert "python_cli_delegate" not in retired_manifest["components"]


def test_python_package_tree_hash_covers_only_sw_core(tmp_path):
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "python-package", stage="2.4", daemon_impl="python")
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    runtime = bundle / "py"
    interpreter = runtime / "bin" / "python3"
    daemon_entry = runtime / "serialwrapd.py"
    package = runtime / "sw_core"
    assert interpreter.is_file()
    assert daemon_entry.is_file()
    assert package.is_dir()
    assert manifest["components"]["daemon"]["entry"] == [
        "py/bin/python3", "-I", "-B", "py/serialwrapd.py"
    ]
    assert manifest["components"]["daemon"]["runtime"]["path"] == "py/bin/python3"

    expected_before = _independent_package_tree_sha256(runtime)
    daemon_tree = manifest["components"]["daemon"]["package"]["tree_sha256"]
    delegate_tree = manifest["components"]["python_cli_delegate"]["tree_sha256"]
    assert daemon_tree == delegate_tree == expected_before
    assert fakes.package_tree_sha256(runtime) == expected_before

    package_file = package / "cli.py"
    package_file.write_text("# sw_core tree changed\n", encoding="utf-8", newline="\n")
    expected_after_package_change = _independent_package_tree_sha256(runtime)
    assert expected_after_package_change != expected_before
    assert fakes.package_tree_sha256(runtime) == expected_after_package_change
    assert expected_after_package_change != daemon_tree
    assert expected_after_package_change != delegate_tree

    interpreter.write_bytes(b"changed interpreter bytes\n")
    runtime_sha_before = manifest["components"]["daemon"]["runtime"]["sha256"]
    assert hashlib.sha256(interpreter.read_bytes()).hexdigest() != runtime_sha_before
    assert fakes.package_tree_sha256(runtime) == expected_after_package_change

    daemon_entry.write_text("# changed daemon entry\n", encoding="utf-8", newline="\n")
    assert fakes.package_tree_sha256(runtime) == expected_after_package_change


@pytest.mark.parametrize("stage", ["1A", "1B", "2.1", "2.2", "2.3", "2.4", "3"])
def test_write_ab_manifest_uses_stage_case_lock_and_side_identities(tmp_path, stage):
    fakes = _fakes()
    manifest_path = fakes.write_ab_manifest(
        tmp_path / f"stage-{stage.replace('.', '-')}",
        layer="l1.5",
        platform="linux",
        stage=stage,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo = manifest_path.parent

    case_lock = json.loads((repo / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    case_rows = yaml.safe_load((repo / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))["cases"]
    if stage == "3":
        required_lock = json.loads(
            (repo / "tests/acceptance/stage3_required_cases.lock.json").read_text(encoding="utf-8")
        )
        exit_id = "S3-L15-RELEASE-AB-LINUX"
        expected_cases = sorted(required_lock["exits"][exit_id]["linux"]["case_ids"])
        expected_set = required_lock["exits"][exit_id]["linux"]["required_case_set"]
    else:
        expected_set = {"1A": "cli_1A", "1B": "cli_1B"}.get(stage, f"slice_{stage}")
        case_by_id = {row["case_id"]: row for row in case_rows}
        expected_cases = sorted(
            case_id
            for case_id in case_lock["sets"][expected_set]
            if case_id in case_by_id
            and str(case_by_id[case_id].get("layer", "")).casefold() == "l1.5"
            and "linux" in case_by_id[case_id].get("platforms", [])
        )
    assert manifest["required_case_set"] == expected_set
    assert manifest["case_ids"] == expected_cases

    release_manifests = [
        json.loads((repo / side["release_manifest"]["path"]).read_text(encoding="utf-8"))
        for side in manifest["sides"]
    ]
    a_components, b_components = [item["components"] for item in release_manifests]
    if stage in ("1A", "1B"):
        assert (a_components["cli"]["implementation"], b_components["cli"]["implementation"]) == (
            "python",
            "rust",
        )
        assert a_components["daemon"]["implementation"] == b_components["daemon"]["implementation"] == "python"
    else:
        assert a_components["cli"]["implementation"] == b_components["cli"]["implementation"]
        assert a_components["cli"]["sha256"] == b_components["cli"]["sha256"]
        assert a_components["daemon"]["implementation"] == "python"
        assert b_components["daemon"]["implementation"] == "rust"
        if stage == "3":
            assert release_manifests[0]["stage"] == "2.4"
            assert release_manifests[1]["stage"] == "3"
            assert release_manifests[0]["bundle_version"] == release_manifests[1]["bundle_version"] == "1.0.0"
            assert release_manifests[1]["retirement"] == {"python_daemon": "removed"}
            assert "python_cli_delegate" not in b_components
            a_cli_path = repo / manifest["sides"][0]["bundle_dir"] / a_components["cli"]["path"]
            b_cli_path = repo / manifest["sides"][1]["bundle_dir"] / b_components["cli"]["path"]
            assert a_cli_path.read_bytes() == b_cli_path.read_bytes()


def test_stage3_pair_release_manifests_follow_pairing_and_retirement_rules(tmp_path):
    """A 配對包通過現行 release manifest schema；B 依 Task 16 退役規則檢查結構。

    現行 contracts/release_manifest.schema.json 還沒有 Stage 3 退役分支（由 Task 16 新增），
    仍強制 python_cli_delegate 與 Python daemon 欄位，因此本 Task 不以該 schema 驗 B，
    也不預先堆放 Task 16 的紅燈；B 的 schema 驗證隨 Task 16 加入。
    """
    import jsonschema

    fakes = _fakes()
    manifest_path = fakes.write_ab_manifest(tmp_path / "stage3-schema-pair", stage="3", platform="linux")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo = manifest_path.parent
    schema = json.loads((REPO_ROOT / "contracts/release_manifest.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    release_manifests = [
        json.loads((repo / side["release_manifest"]["path"]).read_text(encoding="utf-8"))
        for side in manifest["sides"]
    ]
    a_manifest, b_manifest = release_manifests
    assert a_manifest["stage"] == "2.4"
    assert a_manifest["bundle_version"] == b_manifest["bundle_version"] == "1.0.0"
    assert list(validator.iter_errors(a_manifest)) == []

    assert a_manifest["components"]["daemon"]["implementation"] == "python"
    assert b_manifest["stage"] == "3"
    assert b_manifest["retirement"]["python_daemon"] == "removed"
    assert b_manifest["components"]["daemon"]["implementation"] == "rust"
    assert "python_cli_delegate" not in b_manifest["components"]

    a_cli = a_manifest["components"]["cli"]
    b_cli = b_manifest["components"]["cli"]
    a_cli_path = repo / manifest["sides"][0]["bundle_dir"] / a_cli["path"]
    b_cli_path = repo / manifest["sides"][1]["bundle_dir"] / b_cli["path"]
    assert a_cli["sha256"] == b_cli["sha256"]
    assert a_cli_path.read_bytes() == b_cli_path.read_bytes()


def test_write_ab_manifest_cli_sha_override_changes_only_b_artifact(tmp_path):
    import jsonschema

    fakes = _fakes()
    manifest_path = fakes.write_ab_manifest(
        tmp_path / "cli-diff",
        stage="2.1",
        platform="linux",
        **{"sides.1.cli_sha_override": "0" * 64},
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads((ACCEPTANCE_DIR / "ab_manifest.schema.json").read_text(encoding="utf-8"))
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(manifest)) == []

    repo = manifest_path.parent
    components = []
    cli_paths = []
    for side in manifest["sides"]:
        release_path = repo / side["release_manifest"]["path"]
        release_manifest = json.loads(release_path.read_text(encoding="utf-8"))
        cli_rel = release_manifest["components"]["cli"]["path"]
        cli_path = release_path.parent / cli_rel
        components.append(release_manifest["components"]["cli"])
        cli_paths.append(cli_path)
        assert side["release_manifest"]["sha256"] == hashlib.sha256(release_path.read_bytes()).hexdigest()
        assert components[-1]["sha256"] == hashlib.sha256(cli_path.read_bytes()).hexdigest()
    assert cli_paths[0].read_bytes() != cli_paths[1].read_bytes()
    assert components[0]["sha256"] != components[1]["sha256"]

    def verify_frozen_files(value):
        if isinstance(value, dict):
            if set(value) == {"path", "sha256"}:
                path = repo / value["path"]
                assert path.is_file()
                assert hashlib.sha256(path.read_bytes()).hexdigest() == value["sha256"]
            for child in value.values():
                verify_frozen_files(child)
        elif isinstance(value, list):
            for child in value:
                verify_frozen_files(child)

    verify_frozen_files(manifest)
    for tool in manifest["tools"]:
        assert fakes.tree_sha256(repo / tool["path"]) == tool["tree_sha256"]


def test_recording_runner_script_uses_exact_command_key():
    fakes = _fakes()
    first = "git:rev-parse HEAD"
    second = "git:rev-parse HEAD:contracts"
    sequences = {
        first: [
            {"rc": 11, "stdout": "head-1"},
            {"rc": 12, "stdout": "head-2"},
        ],
        second: [
            {"rc": 21, "stdout": "contracts-1"},
            {"rc": 22, "stdout": "contracts-2"},
        ],
    }
    for script in (sequences, dict(reversed(list(sequences.items())))):
        runner = fakes.RecordingRunner(script=script)
        results = [
            runner(["git", "rev-parse", "HEAD:contracts"]),
            runner(["git", "rev-parse", "HEAD"]),
            runner(["git", "rev-parse", "HEAD"]),
            runner(["git", "rev-parse", "HEAD:contracts"]),
        ]
        assert [(result.rc, result.stdout) for result in results] == [
            (21, "contracts-1"),
            (11, "head-1"),
            (12, "head-2"),
            (22, "contracts-2"),
        ]
        fallback = runner(["git", "status"])
        assert (fallback.rc, fallback.stdout) == (0, "{}")

    failure = RuntimeError("prefix exception")
    runner = fakes.RecordingRunner()
    runner.raise_on("git:rev-parse", failure)
    with pytest.raises(RuntimeError, match="prefix exception"):
        runner(["git", "rev-parse", "HEAD:contracts"])


def test_recording_runner_uses_only_declared_value_flags_and_cli_program(tmp_path):
    fakes = _fakes()
    runner = fakes.RecordingRunner(
        script={
            "git:status": [{"rc": 1, "stdout": "listed flag"}],
            "git:30 status": [{"rc": 2, "stdout": "timeout remains positional"}],
            "git:2 status": [{"rc": 3, "stdout": "retries remains positional"}],
            "cli:daemon status": [{"rc": 5, "stdout": "serialwrap.exe is cli"}],
            "other:daemon status": [{"rc": 4, "stdout": "serialwrapd is other"}],
        }
    )

    listed_flag = runner(["git", "--selector", "COM4", "status"])
    timeout_flag = runner(["git", "--timeout", "30", "status"])
    retries_flag = runner(["git", "--retries", "2", "status"])
    cli_program = runner([r"C:\tools\serialwrap.exe", "daemon", "status"])
    daemon_program = runner(["serialwrapd", "daemon", "status"])

    assert [(result.rc, result.stdout) for result in (listed_flag, timeout_flag, retries_flag, cli_program, daemon_program)] == [
        (1, "listed flag"),
        (2, "timeout remains positional"),
        (3, "retries remains positional"),
        (5, "serialwrap.exe is cli"),
        (4, "serialwrapd is other"),
    ]
    assert [(call["kind"], call["sub"]) for call in runner.calls] == [
        ("git", "status"),
        ("git", "30 status"),
        ("git", "2 status"),
        ("cli", "daemon status"),
        ("other", "daemon status"),
    ]


def test_rc_archive_is_reproducible(tmp_path):
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "bundle")

    first = fakes.rc_archive(bundle).read_bytes()
    second = fakes.rc_archive(bundle).read_bytes()

    assert first == second


def test_rc_archive_changes_for_same_manifest_different_tree(tmp_path):
    fakes = _fakes()
    bundle_a = fakes.make_bundle(tmp_path / "a" / "bundle")
    bundle_b = tmp_path / "b" / "bundle"
    bundle_b.parent.mkdir(parents=True)
    shutil.copytree(bundle_a, bundle_b)

    manifest_a = (bundle_a / "release-manifest.json").read_bytes()
    manifest_b = (bundle_b / "release-manifest.json").read_bytes()
    archive_a = fakes.rc_archive(bundle_a).read_bytes()
    sidecar = bundle_b / "assets" / "unlisted-sidecar.bin"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"real fixture content\n")
    # 依 Task 16 順序：新增檔案後先重建 SHA256SUMS 再打包；release manifest 不變。
    fakes.refresh_bundle_sums(bundle_b)
    archive_b = fakes.rc_archive(bundle_b).read_bytes()

    assert manifest_a == manifest_b
    assert fakes.tree_sha256(bundle_a) != fakes.tree_sha256(bundle_b)
    assert hashlib.sha256(archive_a).digest() != hashlib.sha256(archive_b).digest()
    assert archive_a != archive_b


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_rc_archive_layout_matches_bundle_bytes_and_fixed_metadata(tmp_path, platform):
    fakes = _fakes()
    target = "x86_64-unknown-linux-gnu" if platform == "linux" else "x86_64-pc-windows-gnu"
    version = "0.9.0"
    bundle = fakes.make_bundle(tmp_path / f"bundle-{platform}", version=version, stage="2.4", target=target)
    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundle_version"] == version
    outer = f"serialwrap-{version}-{platform}-x86_64"
    version_root = f"{outer}/versions/{version}"
    archive = fakes.rc_archive(bundle)
    bundle_files = {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    bundle_dirs = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_dir()
    }
    version_names = {f"{version_root}/", *(f"{version_root}/{name}/" for name in bundle_dirs)}
    version_names.update(f"{version_root}/{name}" for name in bundle_files)
    outer_names = {f"{outer}/", f"{outer}/versions/", f"{version_root}/", f"{outer}/bin/"}
    suffix = ".exe" if platform == "windows" else ""
    cli_rel = f"bin/serialwrap{suffix}"
    daemon_rel = f"bin/serialwrapd{suffix}"
    assert manifest["components"]["cli"]["path"] == cli_rel
    assert manifest["components"]["daemon"]["launcher"]["path"] == daemon_rel
    cli_entry = f"{outer}/{cli_rel}"
    daemon_entry = f"{outer}/{daemon_rel}"
    expected_names = version_names | outer_names | {cli_entry, daemon_entry}

    if platform == "linux":
        assert archive.suffixes == [".tar", ".gz"]
        assert archive.read_bytes()[4:8] == b"\0\0\0\0"
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            canonical_names = [member.name + "/" if member.isdir() else member.name for member in members]
            assert canonical_names == sorted(expected_names)
            assert len({member.name for member in members}) == len(members)
            by_name = dict(zip(canonical_names, members))
            for name, data in bundle_files.items():
                member = by_name[f"{version_root}/{name}"]
                assert member.isfile()
                stream = tar.extractfile(member)
                assert stream is not None and stream.read() == data
                expected_mode = 0o755 if name.startswith("bin/") or name == "py/bin/python3" else 0o644
                assert member.mode == expected_mode
            for name in bundle_dirs:
                assert by_name[f"{version_root}/{name}/"].isdir()
            for name in (f"{outer}/", f"{outer}/versions/", f"{version_root}/", f"{outer}/bin/"):
                assert by_name[name].isdir() and by_name[name].mode == 0o755
            for rel, entry in ((cli_rel, cli_entry), (daemon_rel, daemon_entry)):
                member = by_name[entry]
                assert member.issym()
                assert member.linkname == f"../versions/{version}/{rel}"
                assert member.mode == 0o777
            for member in members:
                assert member.mtime == member.uid == member.gid == 0
                assert member.uname == member.gname == ""
                assert "\\" not in member.name and ".." not in Path(member.name).parts
    else:
        assert archive.suffix == ".zip"
        with zipfile.ZipFile(archive, "r") as zipped:
            members = zipped.infolist()
            assert [member.filename for member in members] == sorted(expected_names)
            assert len({member.filename for member in members}) == len(members)
            for name, data in bundle_files.items():
                member = zipped.getinfo(f"{version_root}/{name}")
                assert zipped.read(member) == data
                mode = member.external_attr >> 16
                assert stat.S_IFMT(mode) == stat.S_IFREG
                assert stat.S_IMODE(mode) == 0o644
            for name in bundle_dirs:
                member = zipped.getinfo(f"{version_root}/{name}/")
                mode = member.external_attr >> 16
                assert stat.S_IFMT(mode) == stat.S_IFDIR
                assert stat.S_IMODE(mode) == 0o755
            for name in (f"{outer}/", f"{outer}/versions/", f"{version_root}/", f"{outer}/bin/"):
                member = zipped.getinfo(name)
                mode = member.external_attr >> 16
                assert stat.S_IFMT(mode) == stat.S_IFDIR
                assert stat.S_IMODE(mode) == 0o755
            for rel, entry in ((cli_rel, cli_entry), (daemon_rel, daemon_entry)):
                assert zipped.read(entry) == bundle_files[rel]
                member = zipped.getinfo(entry)
                mode = member.external_attr >> 16
                assert stat.S_IFMT(mode) == stat.S_IFREG
                assert stat.S_IMODE(mode) == 0o644
            for member in members:
                assert member.date_time == (1980, 1, 1, 0, 0, 0)
                assert member.create_system == 3
                assert "\\" not in member.filename and ".." not in Path(member.filename).parts


def test_fake_probe_sequences():
    fakes = _fakes()
    probe = fakes.FakeProbe(pid_alive={7: [True, False]})
    assert [probe.pid_alive(7) for _ in range(3)] == [True, False, False]


def test_recording_runner_kind_and_side():
    fakes = _fakes()
    runner = fakes.RecordingRunner()

    runner(["/x/serialwrap", "daemon", "status"], env={"SW_STAGE3_SIDE": "A"})
    call = runner.calls[-1]
    assert call["kind"] == "cli"
    assert call["sub"] == "daemon status"
    assert call["side"] == "A"

    runner(["python3", "-m", "realhw", "--tier", "longrun", "--report-dir", "/r"])
    assert runner.calls[-1]["kind"] == "realhw"
    assert runner.calls[-1]["sub"] == ""

    runner(["python3", "func-test/runner.py", "--mode", "bridge"])
    assert runner.calls[-1]["kind"] == "functest"


def test_recording_runner_on_effect():
    fakes = _fakes()
    runner = fakes.RecordingRunner(script={"functest:": [{"rc": 3, "stdout": "ok", "stderr": ""}]})
    seen: list[tuple[list[str], dict[str, str]]] = []
    runner.on("functest:", lambda argv, env: seen.append((list(argv), dict(env))))

    argv = ["python3", "func-test/runner.py", "--mode", "bridge"]
    result = runner(argv, env={"K": "V"})

    assert seen == [(argv, {"K": "V"})]
    assert result.rc == 3
    assert result.stdout == "ok"


def test_fake_clock_sleep_advances_both():
    fakes = _fakes()
    clock = fakes.FakeClock(start_mono=1000.0, start_wall="2026-11-02T00:00:00+00:00")
    mono0 = clock.now()
    wall0 = datetime.fromisoformat(clock.iso())

    clock.sleep(30)

    assert clock.now() - mono0 == 30
    assert (datetime.fromisoformat(clock.iso()) - wall0).total_seconds() == 30


def test_snapshot_record_shape():
    import jsonschema

    fakes = _fakes()
    record = fakes.snapshot(fakes.ts(2, 1))
    assert record["type"] == "snapshot"

    schema = json.loads((ACCEPTANCE_DIR / "canary_ledger_record.schema.json").read_text(encoding="utf-8"))
    errors = [error.message for error in jsonschema.Draft202012Validator(schema).iter_errors(record)]
    assert errors == []


def test_policy_dict_loads(tmp_path):
    fakes = _fakes()
    policy = canary_policy.load_policy(fakes.write_policy(tmp_path / "p.json"))

    assert policy.status == "frozen"
    assert policy.platforms["linux"].utc_offset_minutes == 0
    assert policy.platforms["windows"].utc_offset_minutes == 0


@pytest.mark.parametrize(
    ("stage", "overrides"),
    [
        ("1A", {}),
        ("1B", {}),
        ("2.1", {}),
        ("2.4", {}),
        ("2.1", {"sides.1.cli_sha_override": "0" * 64}),
    ],
)
def test_pre_stage3_release_manifests_follow_current_contract(tmp_path, stage, overrides):
    """Stage 3 以前的 A/B release manifest 都必須通過現行 schema，且 component 雜湊可獨立重算。

    現行 contracts/release_manifest.schema.json 對任何 daemon 實作都要求 entry／runtime／package；
    非退役 Rust daemon 比照 scripts/rust/mkbundle.sh 的 Stage 2 相容欄位。CLI SHA 負向案例除了
    兩側 CLI 身分不同外，其餘 release manifest 也必須合法。

    1A／1B 的 A 側依 Task 3 load_manifest 為 Python CLI 基線；現行 schema 只接受 Rust CLI，
    因此這兩個階段只以 schema 驗 B 側，A 側仍核對 component 雜湊。
    """
    import jsonschema

    fakes = _fakes()
    manifest_path = fakes.write_ab_manifest(tmp_path / f"pre-{stage}", stage=stage, platform="linux", **overrides)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo = manifest_path.parent
    schema = json.loads((REPO_ROOT / "contracts/release_manifest.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    for side in manifest["sides"]:
        bundle_dir = repo / side["bundle_dir"]
        release = json.loads((repo / side["release_manifest"]["path"]).read_text(encoding="utf-8"))
        if not (stage in {"1A", "1B"} and side["label"] == "A"):
            errors = [error.message for error in validator.iter_errors(release)]
            assert errors == [], f"{side['label']} release manifest schema errors: {errors}"
        daemon = release["components"]["daemon"]
        assert "retirement" not in release
        assert daemon["runtime"]["sha256"] == hashlib.sha256(
            (bundle_dir / daemon["runtime"]["path"]).read_bytes()
        ).hexdigest()
        assert daemon["package"]["tree_sha256"] == fakes.package_tree_sha256(bundle_dir / "py")
        assert release["components"]["python_cli_delegate"]["tree_sha256"] == fakes.package_tree_sha256(bundle_dir / "py")
        if daemon["implementation"] == "rust":
            assert daemon["entry"] == [daemon["launcher"]["path"]], "非退役 Rust daemon 的 entry 指向 Rust launcher"
        else:
            assert daemon["entry"] == ["py/bin/python3", "-I", "-B", "py/serialwrapd.py"]


def test_bundle_sums_exclude_only_root_metadata_files(tmp_path):
    """巢狀的 SHA256SUMS／release-manifest.json 必須列入根 SHA256SUMS（只豁免根目錄兩檔）。"""
    fakes = _fakes()
    root = tmp_path / "bundle"
    nested = {"bin/SHA256SUMS": b"nested sums\n", "bin/release-manifest.json": b"{}\n"}
    for rel, data in nested.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    bundle = fakes.make_bundle(root, stage="2.4")

    lines = (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    listed = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
    for rel, data in nested.items():
        assert listed[rel] == hashlib.sha256(data).hexdigest()
    assert "SHA256SUMS" not in listed and "release-manifest.json" not in listed
    _assert_bundle_sums_match_files(bundle)


@pytest.mark.parametrize("target", ["x86_64-unknown-linux-gnu", "x86_64-pc-windows-gnu"])
def test_retired_bundle_leaves_task_asset_path_absent(tmp_path, target):
    """Task 3 樹摘要測試與 Task 16 正向 fixture 都在建立後新增 assets/skill/SKILL.md，該路徑不得預先存在。"""
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="3", target=target)

    assert not (bundle / "assets" / "skill" / "SKILL.md").exists()
    before = fakes.tree_sha256(bundle)
    added = bundle / "assets" / "skill" / "SKILL.md"
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("# 新增的資產\n", encoding="utf-8", newline="\n")
    assert fakes.tree_sha256(bundle) != before, "新增資產必須改變樹摘要（不是覆寫既有檔）"


@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("layer", ["l1.5", "l2", "l3"])
@pytest.mark.parametrize("stage", ["2.4", "3"])
def test_positive_manifest_authorizes_every_destructive_case(tmp_path, stage, layer, platform):
    """正向 manifest 選入 destructive 必要案例時，須帶 Task 3 認可的授權（R6-01），分母不得縮減。"""
    fakes = _fakes()
    manifest_path = fakes.write_ab_manifest(tmp_path / "auth", stage=stage, layer=layer, platform=platform)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases_doc = yaml.safe_load((REPO_ROOT / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))
    destructive = {case["case_id"] for case in cases_doc["cases"] if case.get("destructive") is True}
    selected = set(manifest["case_ids"]) & destructive
    auth = manifest["authorization"]
    if selected:
        assert auth is not None
        assert auth["allow_destructive"] is True
        assert isinstance(auth["decision_ref"], str) and auth["decision_ref"].startswith("FAKE-")
        assert set(auth["boards"]) <= set(manifest["dut"]["boards"])
    if stage == "3":
        lock = json.loads((manifest_path.parent / manifest["required_cases_lock"]["path"]).read_text(encoding="utf-8"))
        assert set(manifest["case_ids"]) == required_cases.required_for_exit(lock, manifest["exit_item_id"], platform)


@pytest.mark.parametrize("target", ["x86_64-unknown-linux-gnu", "x86_64-pc-windows-gnu"])
def test_added_asset_requires_refreshed_sums_before_archive(tmp_path, target):
    """Task 16 順序：make_bundle 後新增資產 → refresh_bundle_sums → rc_archive；未重建清單時 archive 拒絕（R6-02）。"""
    fakes = _fakes()
    bundle = fakes.make_bundle(tmp_path / "bundle", stage="3", target=target)
    added = bundle / "assets" / "skill" / "SKILL.md"
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("# 新增的資產\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError):
        fakes.rc_archive(bundle)

    fakes.refresh_bundle_sums(bundle)
    _assert_bundle_sums_match_files(bundle)
    archive = fakes.rc_archive(bundle)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            sums_name = next(n for n in zf.namelist() if n.endswith("/SHA256SUMS") and "/versions/" in n)
            sums_text = zf.read(sums_name).decode("utf-8")
    else:
        with tarfile.open(archive, "r:gz") as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/SHA256SUMS") and "/versions/" in m.name)
            sums_text = tf.extractfile(member).read().decode("utf-8")
    assert sums_text == (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    assert "  assets/skill/SKILL.md\n" in sums_text


_SEED_ATTACHMENTS = (
    "docs/rust-refactor/evidence/denominators/cases.yaml",
    "docs/rust-refactor/evidence/denominators/cases.lock.json",
)


def test_fake_repo_seed_attachments_are_byte_identical(tmp_path):
    """seed 附件逐位元組複製，不改寫行尾（R7-01）。"""
    fakes = _fakes()
    repo = fakes.write_fake_repo(tmp_path / "repo")
    for rel in _SEED_ATTACHMENTS:
        assert (repo / rel).read_bytes() == (REPO_ROOT / rel).read_bytes(), rel


def _copy_repo_subset(dest: Path) -> Path:
    """複製 write_fake_repo 會讀取的 repo 檔案到隔離根目錄，不改動正式來源。"""
    files = [
        "contracts/behavior_changes.yaml",
        "tests/acceptance/cases.yaml",
        "tests/acceptance/cases.lock.json",
        "tests/acceptance/stage_exits.json",
        "tests/acceptance/stage3_required_cases.lock.json",
        "docs/rust-refactor/evidence/denominators/source-manifest.json",
        "realhw/acceptance/longrun_acceptance.yaml",
        "realhw/acceptance/longrun_acceptance.schema.json",
        "crates/sw-pal/src/service_ctl.rs",
        "tools/uart_stdio_bridge/APPROVED_REVISION.json",
        *_SEED_ATTACHMENTS,
    ]
    for rel in files:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dest / rel)
    shutil.copytree(REPO_ROOT / "tools/uart_stdio_bridge/src", dest / "tools/uart_stdio_bridge/src")
    shutil.copytree(REPO_ROOT / "tests/acceptance/examples", dest / "tests/acceptance/examples")
    return dest


def test_fake_repo_rejects_seed_source_with_changed_line_endings(tmp_path, monkeypatch):
    """只改 seed 來源的行尾、保留原 manifest／鎖時，fixture 構建必須拒絕，而不是自動修好（R7-01）。"""
    fakes = _fakes()
    source_root = _copy_repo_subset(tmp_path / "source")
    monkeypatch.setattr(fakes, "REPO_ROOT", source_root)
    # 對照組：未改動的隔離來源可正常構建，證明拒絕不是因為缺檔。
    fakes.write_fake_repo(tmp_path / "control")

    seed = source_root / _SEED_ATTACHMENTS[0]
    original = seed.read_bytes()
    assert b"\r\n" not in original
    seed.write_bytes(original.replace(b"\n", b"\r\n"))

    with pytest.raises(ValueError, match="seed"):
        fakes.write_fake_repo(tmp_path / "repo")
