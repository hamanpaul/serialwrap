"""Stage 3 共用基礎替身的紅燈測試。"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from datetime import datetime
from pathlib import Path

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
    assert py_manifest["components"]["daemon"]["package"]["tree_sha256"] == fakes.tree_sha256(py / "py")


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
    archive_b = fakes.rc_archive(bundle_b).read_bytes()

    assert manifest_a == manifest_b
    assert fakes.tree_sha256(bundle_a) != fakes.tree_sha256(bundle_b)
    assert hashlib.sha256(archive_a).digest() != hashlib.sha256(archive_b).digest()
    assert archive_a != archive_b


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
