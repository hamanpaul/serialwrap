"""T7B：L1.5 A/B 報告差分的紅燈契約。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

import yaml

import fakes_ab_diff as fakes
from tools.stage3.ab_diff import DIFF_SCHEMA, diff_l15, normalize_response


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "contracts" / "ab_normalization.yaml"
BEHAVIOR_CHANGES_PATH = ROOT / "contracts" / "behavior_changes.yaml"


def _pair(tmp_path: Path, case_id: str, *, runner: str = "bridge", **kwargs):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner, **kwargs)
    fakes.write_run(b_dir, case_id, side="b", runner=runner, **kwargs)
    return a_dir, b_dir


def _diff(a_dir: Path, b_dir: Path, required: set[str], *, registered=None, case_changes=None):
    return diff_l15(
        a_dir,
        b_dir,
        registered=registered or {},
        case_changes=case_changes or {},
        required_case_ids=required,
        rules_path=RULES_PATH,
    )


def _call(argv, response, *, rc=0):
    return {"argv": list(argv), "rc": rc, "response": response}


def _has_problem(result, side: str, fragment: str) -> bool:
    return any(fragment in str(problem) for problem in result["integrity_problems"][side])


def _source_ref_exists(report_dir: Path, source_ref: str) -> bool:
    rel = source_ref.split("#", 1)[0]
    path = Path(rel)
    return not path.is_absolute() and (report_dir / path).is_file()


def test_identical_reports_ok(tmp_path: Path):
    case_id = fakes.CASE_ID
    a_dir, b_dir = _pair(tmp_path, case_id)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["schema_version"] == DIFF_SCHEMA
    assert result["ok"] is True
    assert result["differences"] == []


def test_missing_required_case_breaks_integrity(tmp_path: Path):
    case_id = "required-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", case_id)


def test_insufficient_comparison_breaks_integrity(tmp_path: Path):
    case_id = "insufficient-case"
    a_dir, b_dir = _pair(tmp_path, case_id, verdict="INSUFFICIENT_EVIDENCE")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["a"] is False
    assert _has_problem(result, "a", "INSUFFICIENT_EVIDENCE")
    assert result["product_failures"] == []


def test_bridge_unclean_shutdown_breaks_integrity(tmp_path: Path):
    case_id = "bridge-unclean-shutdown"
    a_dir, b_dir = _pair(tmp_path, case_id, complete=False)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["a"] is False
    assert _has_problem(result, "a", "closed_cleanly")
    assert _has_problem(result, "a", "bridge-manifest.failure")


def test_bridge_declared_source_hash_mismatch_breaks_integrity(tmp_path: Path):
    case_id = "bridge-source-hash-mismatch"
    a_dir, b_dir = _pair(tmp_path, case_id)
    wire = b_dir / case_id / "bridge" / "1" / "wire.ndjson"
    wire.write_bytes(wire.read_bytes() + b" ")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "source hash mismatch: wire.ndjson")


@pytest.mark.parametrize(
    "missing_file",
    ["manifest.json", "comparison.json", "bridge-manifest.json", "daemon.log", "wal"],
)
def test_bridge_required_evidence_file_missing_breaks_integrity(tmp_path: Path, missing_file: str):
    case_id = f"bridge-missing-{missing_file.replace('/', '-') }"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    fakes.write_run(b_dir, case_id, side="b", missing=(missing_file,))

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", missing_file)


def test_invariant_fail_both_sides_reported(tmp_path: Path):
    case_id = "safety-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    for report_dir, side in ((a_dir, "a"), (b_dir, "b")):
        fakes.write_run(report_dir, case_id, side=side, verdict="FAIL",
                        invariants=[("zero_tx_on_reject", "FAIL")])

    result = _diff(a_dir, b_dir, {case_id})

    assert f"A:{case_id}:zero_tx_on_reject" in result["safety_violations"]
    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]


def test_verdict_difference_is_reported(tmp_path: Path):
    case_id = "verdict-difference"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", verdict="PASS")
    fakes.write_run(b_dir, case_id, side="b", verdict="FAIL", reason="B verdict failure")

    result = _diff(a_dir, b_dir, {case_id})

    assert any(item["case"] == case_id and item["path"] == "comparison.verdict" and item["kind"] == "verdict"
               for item in result["differences"])
    assert result["ok"] is False


def test_cli_return_code_difference_is_reported(tmp_path: Path):
    case_id = "rc-difference"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["session", "list"], {"ok": True}, rc=0)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["session", "list"], {"ok": True}, rc=1)])

    result = _diff(a_dir, b_dir, {case_id})

    assert any(item["case"] == case_id and item["kind"] == "rc" and item["path"].endswith(".rc")
               for item in result["differences"])


def test_repeated_runs_are_all_compared(tmp_path: Path):
    case_id = "repeated-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    for run_index in (1, 2):
        fakes.write_run(a_dir, case_id, side="a", run_index=run_index,
                        calls=[_call(["session", "list"], {"state": "READY"})])
        b_state = "READY" if run_index == 1 else "ATTACHED"
        fakes.write_run(b_dir, case_id, side="b", run_index=run_index,
                        calls=[_call(["session", "list"], {"state": b_state})])

    result = _diff(a_dir, b_dir, {case_id})

    assert any(item["case"] == case_id and "state" in item["path"]
               and item["source_refs"]["b"].startswith(f"{case_id}/bridge/2/")
               for item in result["differences"])


def test_exception_message_ignored_g1(tmp_path: Path):
    case_id = "g1-exception-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(
        ["session", "list"], {"ok": False, "error_code": "EXCEPTION", "message": "Python detail"}, rc=1)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(
        ["session", "list"], {"ok": False, "error_code": "EXCEPTION", "message": "Rust detail"}, rc=1)])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []


def test_set_path_order_ignored_g2(tmp_path: Path):
    case_id = "g2-order-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[
        _call(["event", "reload"], {"ok": True, "added": ["alpha", "beta"], "removed": [], "changed": []}),
        _call(["session", "list"], {"ok": True, "sessions": ["alpha", "beta"]}),
    ])
    fakes.write_run(b_dir, case_id, side="b", calls=[
        _call(["event", "reload"], {"ok": True, "added": ["beta", "alpha"], "removed": [], "changed": []}),
        _call(["session", "list"], {"ok": True, "sessions": ["beta", "alpha"]}),
    ])

    result = _diff(a_dir, b_dir, {case_id})

    assert len(result["differences"]) == 1
    assert result["differences"][0]["kind"] == "value"
    assert "sessions" in result["differences"][0]["path"]


def test_volatile_and_root_normalized(tmp_path: Path):
    case_id = "volatile-root-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(
        ["session", "list"], {"pid": 4101, "state_path": "A_INSTANCE_ROOT/state.json"})])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(
        ["session", "list"], {"pid": 4202, "state_path": "B_INSTANCE_ROOT/state.json"})])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []


def test_difference_attributed_to_case_change(tmp_path: Path):
    case_id = "sc15-console-ring-drop-oldest"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["session", "list"], {"state": "ATTACHED"})])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["session", "list"], {"state": "READY"})])

    result = _diff(a_dir, b_dir, {case_id}, registered={"SC-15": "diff"}, case_changes={case_id: "SC-15"})

    assert result["differences"]
    assert result["differences"][0]["registered_change"] == "SC-15"


def test_mixed_runner_reports_preserve_sources(tmp_path: Path):
    case_ids = {runner: f"mixed-{runner}" for runner in fakes.RUNNERS}
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    for runner, case_id in case_ids.items():
        a_calls = [_call(["session", "list"], {"state": "READY"})]
        b_calls = [_call(["session", "list"], {"state": "READY"})]
        verdict = "PASS"
        invariants = ()
        reason = "fixture result"
        if runner == "contract-blackbox":
            b_calls = [_call(["session", "list"], {"state": "ATTACHED"})]
        if runner == "windows-native":
            verdict = "FAIL"
            reason = "native case rejected product operation"
            invariants = [("zero_tx_on_reject", "FAIL")]
        fakes.write_run(a_dir, case_id, side="a", runner=runner, calls=a_calls)
        fakes.write_run(b_dir, case_id, side="b", runner=runner, calls=b_calls,
                        verdict=verdict, reason=reason, invariants=invariants)

    result = _diff(a_dir, b_dir, set(case_ids.values()))

    assert result["integrity"] == {"a": True, "b": False}
    assert _has_problem(result, "b", "runner source exit code nonzero: windows-native")
    blackbox_diff = next(item for item in result["differences"]
                         if item["case"] == case_ids["contract-blackbox"] and item["kind"] == "value")
    assert _source_ref_exists(a_dir, blackbox_diff["source_refs"]["a"])
    assert _source_ref_exists(b_dir, blackbox_diff["source_refs"]["b"])
    assert f"B:{case_ids['windows-native']}:zero_tx_on_reject" in result["safety_violations"]
    failure = next(item for item in result["product_failures"] if item["case"] == case_ids["windows-native"])
    assert failure["side"] == "b"
    assert _source_ref_exists(b_dir, failure["source_ref"])
    assert "/source/l1-results.json" in failure["source_ref"]
    failure_source = b_dir / failure["source_ref"].split("#", 1)[0]
    failure_report = fakes.read_json(failure_source)
    assert any(row["outcome"] == "failed" for row in failure_report["results"])
    for runner, case_id in case_ids.items():
        run_dir = b_dir / case_id / runner / "1"
        if runner == "bridge":
            manifest = fakes.read_json(run_dir / "manifest.json")
            assert manifest["schema_version"] == "l15-manifest.v1"
            assert manifest["artifacts"]["cli"]["sha256"] == "1" * 64
            assert manifest["artifacts"]["daemon"]["implementation"] == "rust"
            assert manifest["artifacts"]["daemon"]["sha256"] == "3" * 64
            assert manifest["artifacts"]["daemon"]["package_tree_sha256"] is None
            continue
        envelope = fakes.read_json(run_dir / "runner-observation.json")
        assert envelope["schema_version"] == fakes.OBSERVATION_SCHEMA
        release_manifest = run_dir / "source" / "release-manifest.json"
        release_document = fakes.read_json(release_manifest)
        assert release_document["schema_version"] == 1
        assert envelope["artifacts"]["release_manifest_sha256"] == fakes.source_sha256(release_manifest)
        assert envelope["artifacts"]["cli"]["sha256"] == release_document["components"]["cli"]["sha256"]
        assert envelope["artifacts"]["daemon"]["sha256"] == release_document["components"]["daemon"]["launcher"]["sha256"]
        assert envelope["artifacts"]["daemon"]["tree_sha256"] == release_document["components"]["daemon"]["package"]["tree_sha256"]
        for source in envelope["source_files"]:
            source_file = run_dir / source["path"]
            assert source_file.is_file()
            assert fakes.source_sha256(source_file) == source["sha256"]


@pytest.mark.parametrize(
    "runner,loss,expected_problem",
    [
        ("bridge", "source", "wire.ndjson"),
        ("bridge", "observation", "cli.ndjson"),
        ("contract-blackbox", "source", "source/l1-results.json"),
        ("contract-blackbox", "observation", "cli_observations"),
        ("windows-native", "source", "source/windows-transport-context.json"),
        ("windows-native", "observation", "cli_observations"),
        ("rust-l1", "source", "source/libtest.stdout.txt"),
        ("rust-l1", "observation", "cli_observations"),
    ],
    ids=["bridge-wire", "bridge-cli-observation", "blackbox-source", "blackbox-observation",
         "native-source", "native-observation", "rust-source", "rust-observation"],
)
def test_missing_runner_observation_breaks_integrity(tmp_path: Path, runner: str, loss: str, expected_problem: str):
    case_id = f"missing-{runner}-{loss}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner)
    missing = (expected_problem,) if loss == "source" else ()
    observations = [] if loss == "observation" else None
    fakes.write_run(b_dir, case_id, side="b", runner=runner, missing=missing, observations=observations)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", expected_problem)


@pytest.mark.parametrize("runner", fakes.RUNNERS, ids=fakes.RUNNERS)
@pytest.mark.parametrize(
    "identity_field",
    ["cli.sha256", "daemon.implementation", "daemon.sha256", "release_manifest_sha256"],
)
def test_missing_runner_artifact_identity_breaks_integrity(tmp_path: Path, runner: str, identity_field: str):
    case_id = f"identity-{runner}-{identity_field.replace('.', '-') }"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner)
    fakes.write_run(b_dir, case_id, side="b", runner=runner, identity_missing=(identity_field,))

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", f"missing artifact identity: artifacts.{identity_field}")


@pytest.mark.parametrize("runner", fakes.RUNNERS, ids=fakes.RUNNERS)
def test_cli_artifact_must_match_across_sides(tmp_path: Path, runner: str):
    case_id = f"cli-identity-mismatch-{runner}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner)
    fakes.write_run(b_dir, case_id, side="b", runner=runner,
                    artifact_overrides={"cli": {"sha256": "f" * 64}})

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "artifact identity mismatch: artifacts.cli.sha256")


@pytest.mark.parametrize("runner", fakes.RUNNERS, ids=fakes.RUNNERS)
@pytest.mark.parametrize("field", ["argv", "rc", "stdout"])
def test_missing_cli_observation_field_breaks_integrity(tmp_path: Path, runner: str, field: str):
    case_id = f"missing-{runner}-{field}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    good_call = {"argv": ["session", "list"], "rc": 0, "stdout": "{\"ok\":true}"}
    bad_call = {key: value for key, value in good_call.items() if key != field}
    fakes.write_run(a_dir, case_id, side="a", runner=runner, observations=[good_call])
    fakes.write_run(b_dir, case_id, side="b", runner=runner, observations=[bad_call])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", f"cli_observations[0].{field}")


def test_runner_source_hash_mismatch_breaks_integrity(tmp_path: Path):
    case_id = "source-hash-mismatch"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox")
    source = b_run / "source" / "l1-results.json"
    source.write_bytes(source.read_bytes() + b" ")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "source hash mismatch: source/l1-results.json")


@pytest.mark.parametrize("runner", fakes.RUNNERS, ids=fakes.RUNNERS)
def test_unfinished_runner_breaks_integrity(tmp_path: Path, runner: str):
    case_id = f"unfinished-{runner}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner)
    fakes.write_run(b_dir, case_id, side="b", runner=runner, complete=False)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "incomplete")


def test_runner_exit_code_alone_is_not_product_failure(tmp_path: Path):
    case_id = "runner-exit-code-only"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox", runner_exit_code=1)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["product_failures"] == []


def test_product_failure_survives_missing_required_case(tmp_path: Path):
    failed_case = "b-product-failure"
    missing_case = "b-missing-required"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, failed_case, side="a")
    fakes.write_run(b_dir, failed_case, side="b", verdict="FAIL", reason="candidate command failed")
    fakes.write_run(a_dir, missing_case, side="a")

    result = _diff(a_dir, b_dir, {failed_case, missing_case})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", missing_case)
    failure = next(item for item in result["product_failures"] if item["case"] == failed_case)
    assert failure == {
        "side": "b",
        "case": failed_case,
        "source_ref": f"{failed_case}/bridge/1/comparison.json",
        "reason": "candidate command failed",
    }
    assert _source_ref_exists(b_dir, failure["source_ref"])


def test_product_failure_survives_other_side_integrity_problem(tmp_path: Path):
    case_id = "b-fail-a-integrity"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", missing=("cli.ndjson",))
    fakes.write_run(b_dir, case_id, side="b", verdict="FAIL", reason="candidate command failed")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["a"] is False
    assert _has_problem(result, "a", "cli.ndjson")
    assert any(item["side"] == "b" and item["case"] == case_id for item in result["product_failures"])


@pytest.mark.parametrize("b_verdict", ["PASS", "FAIL"], ids=["absolute-pass", "absolute-fail"])
def test_rust_only_uses_absolute_acceptance_and_keeps_b_failure(tmp_path: Path, b_verdict: str):
    case_id = "bc06-shutdown-self-test-flush"
    change_id = "BC-06"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["session", "self-test"], {"result": "python-only"})])
    fakes.write_run(b_dir, case_id, side="b", verdict=b_verdict,
                    reason="Rust absolute contract failed" if b_verdict == "FAIL" else "fixture result",
                    calls=[_call(["session", "self-test"], {"result": "rust-implementation"})])

    result = _diff(a_dir, b_dir, {case_id}, registered={change_id: "absolute_only"},
                   case_changes={case_id: change_id})

    # T7B 不過濾 absolute_only 差異：照實列出並標註 registered_change，
    # 由 T7 combine 依 registered 的 absolute_only 略過差異並以 B 側絕對 verdict 判定。
    case_differences = [item for item in result["differences"] if item["case"] == case_id]
    assert case_differences
    assert all(item["registered_change"] == change_id for item in case_differences)
    if b_verdict == "FAIL":
        failure = next(item for item in result["product_failures"] if item["case"] == case_id)
        assert failure["side"] == "b"
        assert failure["reason"] == "Rust absolute contract failed"
        assert _source_ref_exists(b_dir, failure["source_ref"])
    else:
        assert result["product_failures"] == []


@pytest.mark.parametrize(
    "command,argv",
    [("daemon-status", ["daemon", "status"]), ("health-status", ["health.status"])],
    ids=["daemon-status", "health-status"],
)
def test_foreign_holders_success_top_level_snapshot_is_ignored(tmp_path: Path, command: str, argv: list[str]):
    case_id = f"foreign-holders-{command}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(argv, {
        "ok": True, "pid": 4101, "multi_open": False,
        "foreign_holders": {"/dev/pts/7": 4101},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(argv, {
        "ok": True, "pid": 4202, "multi_open": False,
        "foreign_holders": {"/dev/pts/8": 4202},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []


def test_foreign_holders_empty_success_snapshot_is_ignored(tmp_path: Path):
    case_id = "foreign-holders-empty"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv = ["daemon", "status"]
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(argv, {
        "ok": True, "pid": 4101, "foreign_holders": {},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(argv, {
        "ok": True, "pid": 4202, "foreign_holders": {},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []
    assert result["ok"] is True


@pytest.mark.parametrize(
    "holder_key",
    ["holder_unknown", "device", "relative/tty0", r"\\.\COM3"],
    ids=["holder-unknown", "device", "relative-path", "windows-device-path"],
)
def test_foreign_holders_non_dev_key_matching_own_pid_is_compared(
    tmp_path: Path, holder_key: str,
):
    case_id = "foreign-holders-non-dev-key"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv = ["daemon", "status"]
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(argv, {
        "ok": True, "pid": 4101, "foreign_holders": {},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(argv, {
        "ok": True, "pid": 4202, "foreign_holders": {holder_key: 4202},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in item["path"] for item in result["differences"])
    assert result["ok"] is False


def test_foreign_holders_dangerous_holder_is_not_masked(tmp_path: Path):
    case_id = "foreign-holders-dangerous"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_response = {"ok": True, "pid": 4101, "multi_open": False, "foreign_holders": {}}
    b_response = {"ok": True, "pid": 4202, "multi_open": True,
                  "foreign_holders": {"/dev/pts/8": {"device": "/dev/pts/8", "pids": [9999]}}}
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], a_response)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], b_response)])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"]
    assert any("multi_open" in item["path"] for item in result["differences"])
    assert any("foreign_holders" in item["path"] for item in result["differences"])


def test_device_attach_foreign_holder_is_compared_strictly(tmp_path: Path):
    case_id = "foreign-holders-device-attach"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv = ["device", "attach", "--selector", "COM0"]
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(argv, {
        "ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [4101], "holder_unknown": False,
        "foreign_holders": {"fixture-device": 4101},
    }, rc=1)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(argv, {
        "ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [9999], "holder_unknown": False,
        "foreign_holders": {"fixture-device": 9999},
    }, rc=1)])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in item["path"] for item in result["differences"])
    assert any("pids" in item["path"] for item in result["differences"])


def test_foreign_holders_success_snapshot_with_unknown_is_not_ignored(tmp_path: Path):
    case_id = "foreign-holders-status-unknown"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4101, "multi_open": False,
        "foreign_holders": {"/dev/pts/7": {"device": "/dev/pts/7", "holder_unknown": False, "pids": [4101]}},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4202, "multi_open": False,
        "foreign_holders": {"/dev/pts/7": {"device": "/dev/pts/7", "holder_unknown": True, "pids": [4202]}},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("holder_unknown" in item["path"] for item in result["differences"])


def test_foreign_holders_other_successful_command_is_not_ignored(tmp_path: Path):
    case_id = "foreign-holders-other-command"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["session", "list"], {
        "ok": True, "foreign_holders": {"fixture-device": 4101},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["session", "list"], {
        "ok": True, "foreign_holders": {"fixture-device": 4202},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in item["path"] for item in result["differences"])


def test_foreign_holders_holder_unknown_is_not_masked(tmp_path: Path):
    case_id = "foreign-holders-unknown"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["device", "attach", "--selector", "COM0"], {
        "ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [], "holder_unknown": False,
        "foreign_holders": {},
    }, rc=1)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["device", "attach", "--selector", "COM0"], {
        "ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [], "holder_unknown": True,
        "foreign_holders": {},
    }, rc=1)])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("holder_unknown" in item["path"] for item in result["differences"])


def test_foreign_holders_error_response_is_not_masked(tmp_path: Path):
    case_id = "foreign-holders-error"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], {
        "ok": False, "error_code": "EXCEPTION", "message": "same", "foreign_holders": {},
    }, rc=1)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], {
        "ok": False, "error_code": "EXCEPTION", "message": "same",
        "foreign_holders": {"fixture-device": {"device": "fixture-device", "holder_unknown": True}},
    }, rc=1)])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in item["path"] for item in result["differences"])


def test_foreign_holders_nested_same_name_is_not_masked(tmp_path: Path):
    case_id = "foreign-holders-nested"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4101, "foreign_holders": {"/dev/pts/7": 4101},
        "metadata": {"foreign_holders": {"origin": "A"}},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4202, "foreign_holders": {"/dev/pts/8": 4202},
        "metadata": {"foreign_holders": {"origin": "B"}},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("metadata.foreign_holders" in item["path"] for item in result["differences"])


def test_foreign_holders_does_not_hide_independent_safety_invariant(tmp_path: Path):
    case_id = "foreign-holders-independent-safety"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4101, "foreign_holders": {"/dev/pts/7": 4101},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4202, "foreign_holders": {"/dev/pts/8": 4202},
    })], invariants=[("zero_tx_on_reject", "FAIL")])

    result = _diff(a_dir, b_dir, {case_id})

    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]


@pytest.mark.parametrize(
    "field,value",
    [("state", "READY"), ("holder_unknown", True), ("error_code", "DEVICE_STILL_HELD")],
    ids=["state", "holder-unknown", "error-code"],
)
def test_null_and_value_are_not_normalized_to_each_other(field: str, value, tmp_path: Path):
    rules = yaml.safe_load(RULES_PATH.read_bytes())
    absent_value = normalize_response({field: None}, rules=rules, instance_root="A_INSTANCE_ROOT")
    present_value = normalize_response({field: value}, rules=rules, instance_root="B_INSTANCE_ROOT")

    assert absent_value != present_value


def test_non_g2_list_order_is_preserved_by_normalizer():
    rules = yaml.safe_load(RULES_PATH.read_bytes())

    a_value = normalize_response({"sessions": ["alpha", "beta"]}, rules=rules, instance_root="A_INSTANCE_ROOT")
    b_value = normalize_response({"sessions": ["beta", "alpha"]}, rules=rules, instance_root="B_INSTANCE_ROOT")

    assert a_value != b_value


def test_ab_normalization_contract_appends_required_keys_without_changing_mappings():
    rules = yaml.safe_load(RULES_PATH.read_bytes())

    assert rules.get("g1_exception_message") is True
    g2_paths = {(tuple(item["command"]), item["json_path"]) for item in rules.get("g2_set_paths", [])}
    assert (("event", "reload"), "added") in g2_paths
    assert (("event", "reload"), "removed") in g2_paths
    assert (("event", "reload"), "changed") in g2_paths
    assert rules.get("volatile_keys") == [
        "pid", "cmd_id", "wall_ts", "elapsed_ms", "socket", "created_at", "started_at", "finished_at",
        "last_rx_at", "last_tx_at", "last_probe_at", "last_state_change_at", "last_ready_at", "detached_at",
    ]
    assert rules.get("instance_root_placeholder") == "<ROOT>"
    mappings_digest = hashlib.sha256(json.dumps(
        rules["mappings"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    assert mappings_digest == "7431e796655f1a6279d0c81f0302c383c5b746ead07ba27c6c39e02c520c6558"


def test_rust_only_case_basis_is_current_behavior_contract():
    contract = yaml.safe_load(BEHAVIOR_CHANGES_PATH.read_bytes())
    row = next(item for item in contract["changes"] if item["id"] == "BC-06")

    assert row["scope"] == "rust_only"
    assert row["ab_participation"] == "absolute_only"
    assert "bc06-shutdown-self-test-flush" in row["case_ids"]


@pytest.mark.parametrize(
    ("side", "implementation", "sha256", "tree_sha256"),
    [
        ("a", "python", None, "4" * 64),
        ("b", "rust", "3" * 64, None),
    ],
)
@pytest.mark.parametrize("runner", ["bridge", "contract-blackbox"])
def test_artifact_identity_is_implementation_specific(
    tmp_path: Path, side: str, implementation: str, sha256: str | None,
    tree_sha256: str | None, runner: str,
):
    case_id = f"identity-{runner}-{side}"
    report_dir = tmp_path / side
    fakes.write_run(report_dir, case_id, side=side, runner=runner)
    run_dir = report_dir / case_id / runner / "1"
    if runner == "bridge":
        raw_identity = fakes.read_json(run_dir / "manifest.json")["artifacts"]["daemon"]
        observed_tree = raw_identity["package_tree_sha256"]
    else:
        envelope = fakes.read_json(run_dir / "runner-observation.json")
        raw_identity = envelope["artifacts"]["daemon"]
        observed_tree = raw_identity["tree_sha256"]
    assert raw_identity["implementation"] == implementation
    assert raw_identity["sha256"] == sha256
    assert observed_tree == tree_sha256

    result = _diff(
        report_dir if side == "a" else tmp_path / "empty-a",
        report_dir if side == "b" else tmp_path / "empty-b",
        set(),
    )
    identity = result["a_cases"] if side == "a" else result["b_cases"]
    assert case_id in identity
    assert not _has_problem(result, side, "missing artifact identity: artifacts.daemon.sha256")
    assert not _has_problem(result, side, "missing artifact identity: artifacts.daemon.tree_sha256")
    assert implementation in {"python", "rust"}
    assert sha256 is None or len(sha256) == 64
    assert tree_sha256 is None or len(tree_sha256) == 64


@pytest.mark.parametrize("runner", ["bridge", "contract-blackbox"])
@pytest.mark.parametrize(
    ("side", "identity_field"),
    [("a", "daemon.tree_sha256"), ("b", "daemon.sha256")],
)
def test_missing_implementation_specific_artifact_identity_breaks_integrity(
    tmp_path: Path, runner: str, side: str, identity_field: str,
):
    case_id = f"missing-applicable-identity-{runner}-{side}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner)
    fakes.write_run(b_dir, case_id, side="b", runner=runner)
    report_dir = a_dir if side == "a" else b_dir
    run_dir = report_dir / case_id / runner / "1"
    if runner == "bridge":
        manifest = fakes.read_json(run_dir / "manifest.json")
        raw_field = "package_tree_sha256" if identity_field.endswith("tree_sha256") else "sha256"
        del manifest["artifacts"]["daemon"][raw_field]
        fakes.write_json(run_dir / "manifest.json", manifest)
    else:
        envelope = fakes.read_json(run_dir / "runner-observation.json")
        del envelope["artifacts"]["daemon"][identity_field.rsplit(".", 1)[1]]
        fakes.write_json(run_dir / "runner-observation.json", envelope)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"][side] is False
    assert _has_problem(result, side, f"artifact identity: artifacts.{identity_field}")


def test_a_side_failures_are_separated_from_candidate_product_failures(tmp_path: Path):
    case_id = "baseline-failure"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", verdict="FAIL", reason="A observation failed")
    fakes.write_run(b_dir, case_id, side="b", verdict="PASS")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["a_cases"][case_id] == "FAIL"
    assert result["baseline_failures"] == [{
        "side": "a", "case": case_id, "source_ref": f"{case_id}/bridge/1/comparison.json",
        "reason": "A observation failed",
    }]
    assert result["product_failures"] == []


def test_failures_on_both_sides_keep_separate_ownership(tmp_path: Path):
    case_id = "two-sided-failure"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", verdict="FAIL", reason="A failed")
    fakes.write_run(b_dir, case_id, side="b", verdict="FAIL", reason="B failed")

    result = _diff(a_dir, b_dir, {case_id})

    assert [row["side"] for row in result["baseline_failures"]] == ["a"]
    assert [row["side"] for row in result["product_failures"]] == ["b"]


def test_b_failure_outside_required_case_is_not_a_product_failure(tmp_path: Path):
    required_case = "required-case"
    extra_case = "extra-case"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, required_case, side="a")
    fakes.write_run(b_dir, required_case, side="b")
    fakes.write_run(b_dir, extra_case, side="b", verdict="FAIL")

    result = _diff(a_dir, b_dir, {required_case})

    assert result["product_failures"] == []
    assert any(row["case"] == extra_case and row["side"] == "b" for row in result["unverified_failures"])


def test_a_side_safety_failure_is_not_a_candidate_product_failure(tmp_path: Path):
    case_id = "baseline-safety-failure"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", invariants=[("zero_tx_on_reject", "FAIL")])
    fakes.write_run(b_dir, case_id, side="b")

    result = _diff(a_dir, b_dir, {case_id})

    assert f"A:{case_id}:zero_tx_on_reject" in result["safety_violations"]
    assert result["product_failures"] == []
    assert result["baseline_failures"] == []


@pytest.mark.parametrize("outcome", ["skipped", "error"], ids=["skipped", "error"])
def test_unexecuted_required_runner_case_breaks_integrity_and_keeps_other_failures(
    tmp_path: Path, outcome: str,
):
    skipped_case = f"required-{outcome}"
    failed_case = "independent-product-failure"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, skipped_case, side="a", runner="contract-blackbox")
    row = {
        "nodeid": "tests/contract_blackbox/test_l15_observation.py::test_required_observation",
        "outcome": outcome,
        "skip_kind": "pytest_skip" if outcome == "skipped" else None,
        "phases": {"setup": outcome, "call": outcome, "teardown": "passed"},
    }
    fakes.write_run(
        b_dir, skipped_case, side="b", runner="contract-blackbox", l1_results=[row],
        case_results=[{"nodeid": row["nodeid"], "source_ref": f"{skipped_case}/contract-blackbox/1/source/l1-results.json#results[0]"}],
        runner_exit_code=0,
    )
    fakes.write_run(a_dir, failed_case, side="a", runner="contract-blackbox")
    fakes.write_run(b_dir, failed_case, side="b", runner="contract-blackbox", verdict="FAIL")

    result = _diff(a_dir, b_dir, {skipped_case, failed_case})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", outcome)
    assert any(row["case"] == failed_case and row["side"] == "b" for row in result["product_failures"])


def test_nonzero_runner_exit_breaks_integrity_without_inventing_product_failure(tmp_path: Path):
    case_id = "runner-nonzero-exit"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox", runner_exit_code=1)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "exit")
    assert result["product_failures"] == []


def test_nonzero_runner_exit_can_coexist_with_a_verified_product_failure(tmp_path: Path):
    case_id = "runner-nonzero-with-product-failure"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL", runner_exit_code=1)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert any(row["case"] == case_id for row in result["product_failures"])


def test_raw_runner_exit_is_checked_even_when_envelope_is_missing(tmp_path: Path):
    case_id = "raw-exit-without-envelope"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", runner_exit_code=1,
    )
    (b_run / "runner-observation.json").unlink()

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "runner source exit code nonzero")
    assert result["product_failures"] == []


def test_envelope_and_raw_runner_exit_codes_must_match(tmp_path: Path):
    case_id = "runner-exit-code-mismatch"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox")
    l1_path = b_run / "source/l1-results.json"
    l1_document = fakes.read_json(l1_path)
    l1_document["exit_code"] = 2
    fakes.write_json(l1_path, l1_document)
    envelope_path = b_run / "runner-observation.json"
    envelope = fakes.read_json(envelope_path)
    for source in envelope["source_files"]:
        if source["path"] == "source/l1-results.json":
            source["sha256"] = fakes.source_sha256(l1_path)
    fakes.write_json(envelope_path, envelope)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "runner exit code mismatch")


@pytest.mark.parametrize(
    ("source_result", "envelope_result", "should_report_safety", "should_report_unverified"),
    [("FAIL", "PASS", True, False), ("PASS", "FAIL", False, True)],
)
def test_raw_invariant_evidence_is_authoritative_and_independently_loaded(
    tmp_path: Path, source_result: str, envelope_result: str, should_report_safety: bool,
    should_report_unverified: bool,
):
    case_id = "invariant-source-mismatch"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox",
        invariants=[("zero_tx_on_reject", envelope_result)],
        source_invariants=[{"id": "zero_tx_on_reject", "result": source_result}],
    )

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "invariant evidence")
    expected = f"B:{case_id}:zero_tx_on_reject"
    assert (expected in result["safety_violations"]) is should_report_safety
    assert any(
        row["case"] == case_id and row["side"] == "b" and "invariant" in row["reason"]
        for row in result["unverified_failures"]
    ) is should_report_unverified
    assert fakes.read_json(b_run / "source/invariant-evidence.json")["invariants"][0]["result"] == source_result


def test_raw_invariant_failure_is_kept_when_envelope_omits_it(tmp_path: Path):
    case_id = "invariant-source-omitted"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox",
        source_invariants=[{"id": "zero_tx_on_reject", "result": "FAIL"}],
    )
    envelope = fakes.read_json(b_run / "runner-observation.json")
    envelope["invariants"] = []
    fakes.write_json(b_run / "runner-observation.json", envelope)

    result = _diff(a_dir, b_dir, {case_id})

    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]
    assert result["integrity"]["b"] is False


def test_raw_invariant_failure_survives_missing_envelope_as_unverified(tmp_path: Path):
    case_id = "invariant-without-envelope"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox",
        source_invariants=[{"id": "zero_tx_on_reject", "result": "FAIL"}],
    )
    (b_run / "runner-observation.json").unlink()

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert any(
        row["case"] == case_id and row["side"] == "b" and "invariant" in row["reason"]
        for row in result["unverified_failures"]
    )


def test_raw_invariant_failure_survives_other_integrity_problem(tmp_path: Path):
    case_id = "invariant-with-cli-missing"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox",
        source_invariants=[{"id": "zero_tx_on_reject", "result": "FAIL"}],
        missing=("source/cli-observations.ndjson",),
    )

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]


def test_nonbridge_case_result_maps_exact_nodeid_without_case_id_substring(tmp_path: Path):
    case_id = "required-case-without-nodeid-token"
    nodeid = "tests/test_behavior.py::test_observed_cli_contract"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL",
        l1_results=[{"nodeid": nodeid, "outcome": "failed", "skip_kind": None,
                     "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}],
        case_results=[{"nodeid": nodeid, "source_ref": f"{case_id}/contract-blackbox/1/source/l1-results.json#results[0]"}],
    )

    result = _diff(a_dir, b_dir, {case_id})

    failure = next(row for row in result["product_failures"] if row["case"] == case_id)
    assert failure["source_ref"] == f"{case_id}/contract-blackbox/1/source/l1-results.json#results[0]"
    assert _source_ref_exists(b_dir, failure["source_ref"])


def test_nonbridge_case_mapping_does_not_use_overlapping_nodeid_substring(tmp_path: Path):
    case_id = "case-alpha"
    passing_nodeid = "tests/test_behavior.py::test_target"
    overlapping_failed_nodeid = "tests/test_behavior.py::test_case_alpha_extra"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="PASS", runner_exit_code=1,
        l1_results=[
            {"nodeid": passing_nodeid, "outcome": "passed", "skip_kind": None,
             "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
            {"nodeid": overlapping_failed_nodeid, "outcome": "failed", "skip_kind": None,
             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}},
        ],
        case_results=[{"nodeid": passing_nodeid, "source_ref": f"{case_id}/contract-blackbox/1/source/l1-results.json#results[0]"}],
    )

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert result["product_failures"] == []


def test_nonbridge_empty_case_results_break_integrity(tmp_path: Path):
    case_id = "empty-case-map"
    a_dir, b_dir = _pair(tmp_path, case_id, runner="contract-blackbox")
    b_run = b_dir / case_id / "contract-blackbox" / "1"
    envelope = fakes.read_json(b_run / "runner-observation.json")
    envelope["case_results"] = []
    fakes.write_json(b_run / "runner-observation.json", envelope)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "case_results")


def test_nonbridge_multiple_mapped_nodeids_keep_failure_source(tmp_path: Path):
    case_id = "multiple-mapped-results"
    nodeids = ["tests/test_behavior.py::test_first", "tests/test_behavior.py::test_second"]
    refs = [f"{case_id}/contract-blackbox/1/source/l1-results.json#results[{index}]" for index in range(2)]
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL",
        l1_results=[
            {"nodeid": nodeids[0], "outcome": "failed", "skip_kind": None,
             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}},
            {"nodeid": nodeids[1], "outcome": "failed", "skip_kind": None,
             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}},
        ],
        case_results=[{"nodeid": nodeid, "source_ref": source_ref} for nodeid, source_ref in zip(nodeids, refs)],
    )

    result = _diff(a_dir, b_dir, {case_id})

    failures = [row for row in result["product_failures"] if row["case"] == case_id]
    assert [row["source_ref"] for row in failures] == refs


def test_case_map_source_is_preserved_in_diff_output(tmp_path: Path):
    case_id = "case-map-provenance"
    a_dir, b_dir = _pair(tmp_path, case_id, runner="contract-blackbox")
    result = _diff(a_dir, b_dir, {case_id})

    assert result["case_maps"] == [
        {"side": "a", "case": case_id, "runner": "contract-blackbox", "path": "tests/acceptance/cases.yaml", "sha256": "e" * 64},
        {"side": "b", "case": case_id, "runner": "contract-blackbox", "path": "tests/acceptance/cases.yaml", "sha256": "e" * 64},
    ]


def test_volatile_pid_and_instance_root_rules_do_not_enter_foreign_holders(tmp_path: Path):
    case_id = "foreign-holder-object-pid"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4101, "foreign_holders": {"/dev/ttyX": {"pid": 12, "path": "A_INSTANCE_ROOT/ttyX"}},
    })])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], {
        "ok": True, "pid": 4202, "foreign_holders": {"/dev/ttyX": {"pid": 34, "path": "B_INSTANCE_ROOT/ttyX"}},
    })])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in row["path"] for row in result["differences"])


def test_foreign_holders_null_and_value_are_compared_strictly(tmp_path: Path):
    case_id = "foreign-holder-null-value"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["daemon", "status"], {"ok": True, "pid": 4101, "foreign_holders": None})])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], {"ok": True, "pid": 4202, "foreign_holders": {"/dev/ttyX": {"pid": 34}}})])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in row["path"] for row in result["differences"])


def test_error_response_foreign_holder_pid_is_not_volatile(tmp_path: Path):
    case_id = "foreign-holder-error-pid"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["device", "attach"], {
        "ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [12], "holder_unknown": False,
        "foreign_holders": {"/dev/ttyX": {"pid": 12}},
    }, rc=1)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["device", "attach"], {
        "ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [34], "holder_unknown": False,
        "foreign_holders": {"/dev/ttyX": {"pid": 34}},
    }, rc=1)])

    result = _diff(a_dir, b_dir, {case_id})

    assert any("foreign_holders" in row["path"] for row in result["differences"])
    assert any("pids" in row["path"] for row in result["differences"])


def test_bridge_case_identity_mismatch_makes_failure_unverified(tmp_path: Path):
    case_id = "bridge-case-identity"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b", verdict="FAIL")
    manifest = fakes.read_json(b_run / "manifest.json")
    comparison = fakes.read_json(b_run / "comparison.json")
    manifest["case_id"] = "another-case"
    comparison["case_id"] = "another-case"
    fakes.write_json(b_run / "manifest.json", manifest)
    fakes.write_json(b_run / "comparison.json", comparison)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert result["product_failures"] == []
    assert any(row["case"] == case_id and row["side"] == "b" for row in result["unverified_failures"])


def test_bridge_run_identity_mismatch_makes_failure_unverified(tmp_path: Path):
    case_id = "bridge-run-identity"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b", verdict="FAIL")
    comparison = fakes.read_json(b_run / "comparison.json")
    comparison["run_id"] = "other-run"
    fakes.write_json(b_run / "comparison.json", comparison)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert any(row["case"] == case_id for row in result["unverified_failures"])
    assert result["product_failures"] == []


def test_bridge_missing_artifact_identity_does_not_create_verified_failure(tmp_path: Path):
    case_id = "bridge-missing-identity-fail"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b", verdict="FAIL")
    manifest = fakes.read_json(b_run / "manifest.json")
    del manifest["artifacts"]["daemon"]["sha256"]
    fakes.write_json(b_run / "manifest.json", manifest)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert result["product_failures"] == []
    assert any(row["case"] == case_id for row in result["unverified_failures"])


def test_bridge_missing_required_evidence_declaration_breaks_integrity(tmp_path: Path):
    case_id = "bridge-evidence-declaration-missing"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b")
    manifest = fakes.read_json(b_run / "manifest.json")
    manifest["evidence"].pop("wire")
    fakes.write_json(b_run / "manifest.json", manifest)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "evidence")


def test_bridge_actual_wire_and_cli_bytes_are_checked_against_declared_hashes(tmp_path: Path):
    case_id = "bridge-actual-evidence-hashes"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b")
    wire = b_run / "wire.ndjson"
    cli = b_run / "cli.ndjson"
    wire.write_bytes(wire.read_bytes() + b" ")
    cli.write_bytes(cli.read_bytes() + b" ")
    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "source hash mismatch: wire.ndjson")
    assert _has_problem(result, "b", "source hash mismatch: cli.ndjson")


def test_bridge_wire_tamper_matching_manifest_must_still_match_comparison(tmp_path: Path):
    case_id = "bridge-wire-tamper-manifest-updated"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b")
    wire = b_run / "wire.ndjson"
    wire.write_bytes(wire.read_bytes() + b" ")
    manifest = fakes.read_json(b_run / "manifest.json")
    manifest["evidence"]["wire"]["sha256"] = hashlib.sha256(wire.read_bytes()).hexdigest()
    fakes.write_json(b_run / "manifest.json", manifest)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "comparison integrity mismatch: wire_sha256")


def test_bridge_comparison_and_manifest_wire_hashes_must_match_actual_bytes(tmp_path: Path):
    case_id = "bridge-wire-hash-record-mismatch"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b")
    comparison = fakes.read_json(b_run / "comparison.json")
    comparison["integrity"]["wire_sha256"] = "0" * 64
    comparison["integrity"]["wire_sha256_expected"] = "0" * 64
    fakes.write_json(b_run / "comparison.json", comparison)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "wire_sha256")


@pytest.mark.parametrize(
    ("malformed", "location"),
    [("verdict-list", "bridge"), ("invariant-object", "runner")],
)
def test_malformed_report_values_do_not_stop_scanning_other_failures(
    tmp_path: Path, malformed: str, location: str,
):
    malformed_case = f"malformed-{malformed}"
    trusted_case = "trusted-failure-and-safety"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, malformed_case, side="a", runner="bridge" if location == "bridge" else "contract-blackbox")
    malformed_run = b_dir / malformed_case / ("bridge" if location == "bridge" else "contract-blackbox") / "1"
    fakes.write_run(b_dir, malformed_case, side="b", runner="bridge" if location == "bridge" else "contract-blackbox")
    if location == "bridge":
        comparison = fakes.read_json(malformed_run / "comparison.json")
        comparison["verdict"] = []
        fakes.write_json(malformed_run / "comparison.json", comparison)
    else:
        envelope = fakes.read_json(malformed_run / "runner-observation.json")
        envelope["invariants"] = [{"id": "bad-shape", "result": {"unexpected": "object"}}]
        fakes.write_json(malformed_run / "runner-observation.json", envelope)
    fakes.write_run(a_dir, trusted_case, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, trusted_case, side="b", runner="contract-blackbox", verdict="FAIL",
        invariants=[("zero_tx_on_reject", "FAIL")],
    )

    result = _diff(a_dir, b_dir, {malformed_case, trusted_case})

    assert result["integrity"]["b"] is False
    assert any(row["case"] == trusted_case and row["side"] == "b" for row in result["product_failures"])
    assert f"B:{trusted_case}:zero_tx_on_reject" in result["safety_violations"]


def _refresh_nonbridge_source_hash(run_dir: Path, relative: str) -> None:
    """同步 fixture envelope 中指定原始來源的位元組雜湊。"""
    envelope_path = run_dir / "runner-observation.json"
    envelope = fakes.read_json(envelope_path)
    digest = hashlib.sha256((run_dir / relative).read_bytes()).hexdigest()
    row = next(item for item in envelope["source_files"] if item["path"] == relative)
    row["sha256"] = digest
    fakes.write_json(envelope_path, envelope)


@pytest.mark.parametrize("runner", ["contract-blackbox", "windows-native"])
def test_pytest_runner_l1_fixture_matches_collector_shape(tmp_path: Path, runner: str):
    case_id = f"pytest-l1-shape-{runner}"
    a_dir, b_dir = _pair(tmp_path, case_id, runner=runner)
    report = fakes.read_json(b_dir / case_id / runner / "1" / "source/l1-results.json")

    assert report["schema_version"] == 2
    assert report["format"] == "l1-results.v1"
    assert report["runner"] == "pytest"
    assert report["finished"] is True
    if runner == "windows-native":
        assert report["provenance"]["schema"] == "pytest-l1-provenance.v1"
        assert report["provenance"]["platform"] == "windows"
        assert set(report["provenance"]) == {"schema", "platform", "git"}
    else:
        assert "provenance" not in report

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"] == {"a": True, "b": True}


def test_rust_l1_accepts_unmodified_cargo_collector_report_shape(tmp_path: Path):
    case_id = "cargo-l1-original-shape"
    a_dir, b_dir = _pair(tmp_path, case_id, runner="rust-l1")
    report = fakes.read_json(b_dir / case_id / "rust-l1" / "1" / "source/l1-results.json")

    assert report["runner"] == "cargo"
    assert report["finished"] is True
    assert report["provenance"]["schema"] == "cargo-l1-provenance.v1"
    assert "runner" not in report["provenance"]
    assert "complete" not in report["provenance"]

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"] == {"a": True, "b": True}


def test_rust_l1_uses_top_level_finished_for_completion(tmp_path: Path):
    case_id = "cargo-l1-not-finished"
    a_dir, b_dir = _pair(tmp_path, case_id, runner="rust-l1")
    b_run = b_dir / case_id / "rust-l1" / "1"
    report_path = b_run / "source/l1-results.json"
    report = fakes.read_json(report_path)
    report["finished"] = False
    fakes.write_json(report_path, report)
    _refresh_nonbridge_source_hash(b_run, "source/l1-results.json")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "runner incomplete: rust-l1")


def test_rust_l1_checks_top_level_runner_value(tmp_path: Path):
    case_id = "cargo-l1-runner-mismatch"
    a_dir, b_dir = _pair(tmp_path, case_id, runner="rust-l1")
    b_run = b_dir / case_id / "rust-l1" / "1"
    report_path = b_run / "source/l1-results.json"
    report = fakes.read_json(report_path)
    report["runner"] = "pytest"
    fakes.write_json(report_path, report)
    _refresh_nonbridge_source_hash(b_run, "source/l1-results.json")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "runner source identity mismatch: source/l1-results.json")


@pytest.mark.parametrize("identity_issue", ["missing", "invalid-json", "unlisted", "hash-mismatch", "artifact-mismatch"])
def test_unverified_release_identity_keeps_general_and_safety_failures(
    tmp_path: Path, identity_issue: str,
):
    case_id = f"unverified-release-{identity_issue}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL",
        runner_exit_code=0, invariants=[("zero_tx_on_reject", "FAIL")],
    )
    release_path = b_run / "source/release-manifest.json"
    envelope_path = b_run / "runner-observation.json"
    if identity_issue == "missing":
        release_path.unlink()
    elif identity_issue == "invalid-json":
        release_path.write_bytes(b"{invalid json\n")
        _refresh_nonbridge_source_hash(b_run, "source/release-manifest.json")
    elif identity_issue == "unlisted":
        envelope = fakes.read_json(envelope_path)
        envelope["source_files"] = [
            row for row in envelope["source_files"] if row["path"] != "source/release-manifest.json"
        ]
        fakes.write_json(envelope_path, envelope)
    elif identity_issue == "hash-mismatch":
        release_path.write_bytes(release_path.read_bytes() + b" ")
    elif identity_issue == "artifact-mismatch":
        release = fakes.read_json(release_path)
        release["components"]["cli"]["sha256"] = "f" * 64
        fakes.write_json(release_path, release)
        _refresh_nonbridge_source_hash(b_run, "source/release-manifest.json")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert result["product_failures"] == []
    assert result["safety_violations"] == []
    assert next(row for row in result["artifact_identity"] if row["side"] == "b")["verified"] is False
    assert any(row["side"] == "b" and row["case"] == case_id
               and row["source_ref"].endswith("source/l1-results.json#results[0]")
               for row in result["unverified_failures"])
    assert any(row["side"] == "b" and row["case"] == case_id and "invariant" in row["reason"]
               for row in result["unverified_failures"])
    assert result["integrity_problems"]["b"]


def test_cli_pairing_mismatch_does_not_downgrade_verified_b_failures(tmp_path: Path):
    case_id = "cli-mismatch-keeps-failures"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL", runner_exit_code=0,
        invariants=[("zero_tx_on_reject", "FAIL")],
        artifact_overrides={"cli": {"sha256": "f" * 64}},
    )

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert _has_problem(result, "b", "artifact identity mismatch: artifacts.cli.sha256")
    assert next(row for row in result["artifact_identity"] if row["side"] == "b")["verified"] is True
    assert result["cli_pairing"] == [{
        "case": case_id,
        "runner": "contract-blackbox",
        "run_index": "1",
        "matched": False,
        "source_refs": {
            "a": f"{case_id}/contract-blackbox/1/runner-observation.json",
            "b": f"{case_id}/contract-blackbox/1/runner-observation.json",
        },
    }]
    assert any(row["side"] == "b" and row["case"] == case_id for row in result["product_failures"])
    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("l1_state", ["missing", "invalid"])
def test_nonbridge_envelope_fail_survives_missing_or_invalid_l1(
    tmp_path: Path, side: str, l1_state: str,
):
    case_id = f"envelope-fail-with-{l1_state}-l1-{side}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox")
    report_dir = a_dir if side == "a" else b_dir
    run_dir = report_dir / case_id / "contract-blackbox" / "1"
    envelope_path = run_dir / "runner-observation.json"
    envelope = fakes.read_json(envelope_path)
    envelope["verdict"] = "FAIL"
    envelope["reason"] = "fixture envelope failure"
    fakes.write_json(envelope_path, envelope)
    l1_path = run_dir / "source/l1-results.json"
    if l1_state == "missing":
        l1_path.unlink()
    else:
        l1_path.write_bytes(b"{invalid json\n")
        _refresh_nonbridge_source_hash(run_dir, "source/l1-results.json")

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"][side] is False
    assert any(row["side"] == side and row["case"] == case_id
               and row["source_ref"].endswith("runner-observation.json")
               for row in result["unverified_failures"])


def test_raw_l1_failure_survives_missing_envelope_as_unverified(tmp_path: Path):
    case_id = "raw-l1-failure-without-envelope"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    b_run = fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL", runner_exit_code=0,
    )
    (b_run / "runner-observation.json").unlink()

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert any(row["side"] == "b" and row["case"] == case_id
               and row["source_ref"] == f"{case_id}/contract-blackbox/1/source/l1-results.json#results[0]"
               for row in result["unverified_failures"])


def test_bridge_verdict_fail_survives_missing_manifest(tmp_path: Path):
    case_id = "bridge-fail-without-manifest"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a")
    b_run = fakes.write_run(b_dir, case_id, side="b", verdict="FAIL")
    (b_run / "manifest.json").unlink()

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"]["b"] is False
    assert next(row for row in result["artifact_identity"] if row["side"] == "b")["verified"] is False
    assert any(row["side"] == "b" and row["case"] == case_id
               and row["source_ref"] == f"{case_id}/bridge/1/comparison.json"
               for row in result["unverified_failures"])


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("runner", ["bridge", "contract-blackbox"])
@pytest.mark.parametrize("id_problem", ["missing", "wrong-type"])
def test_malformed_invariant_id_preserves_raw_fail_by_index(
    tmp_path: Path, side: str, runner: str, id_problem: str,
):
    case_id = f"malformed-invariant-{runner}-{side}-{id_problem}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner=runner)
    fakes.write_run(b_dir, case_id, side="b", runner=runner)
    report_dir = a_dir if side == "a" else b_dir
    run_dir = report_dir / case_id / runner / "1"
    if runner == "bridge":
        path = run_dir / "comparison.json"
        document = fakes.read_json(path)
        document["invariants"][0]["verdict"] = "FAIL"
        if id_problem == "missing":
            del document["invariants"][0]["id"]
        else:
            document["invariants"][0]["id"] = ["bad", "id"]
        fakes.write_json(path, document)
        source_ref = f"{case_id}/bridge/1/comparison.json#/invariants/0"
    else:
        path = run_dir / "source/invariant-evidence.json"
        document = fakes.read_json(path)
        document["invariants"][0]["result"] = "FAIL"
        if id_problem == "missing":
            del document["invariants"][0]["id"]
        else:
            document["invariants"][0]["id"] = ["bad", "id"]
        fakes.write_json(path, document)
        _refresh_nonbridge_source_hash(run_dir, "source/invariant-evidence.json")
        source_ref = f"{case_id}/contract-blackbox/1/source/invariant-evidence.json#/invariants/0"

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"][side] is False
    assert not any(token.startswith(side.upper() + ":" + case_id + ":")
                   for token in result["safety_violations"])
    assert any(row["side"] == side and row["case"] == case_id and row["source_ref"] == source_ref
               and "invariant" in row["reason"] for row in result["unverified_failures"])


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("id_problem", ["missing", "wrong-type"])
def test_malformed_envelope_invariant_id_preserves_fail_by_index(
    tmp_path: Path, side: str, id_problem: str,
):
    case_id = f"malformed-envelope-invariant-{side}-{id_problem}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox")
    report_dir = a_dir if side == "a" else b_dir
    run_dir = report_dir / case_id / "contract-blackbox" / "1"
    envelope_path = run_dir / "runner-observation.json"
    envelope = fakes.read_json(envelope_path)
    envelope["invariants"] = [{"id": "zero_tx_on_reject", "result": "FAIL"}]
    if id_problem == "missing":
        del envelope["invariants"][0]["id"]
    else:
        envelope["invariants"][0]["id"] = ["bad", "id"]
    fakes.write_json(envelope_path, envelope)

    result = _diff(a_dir, b_dir, {case_id})

    assert result["integrity"][side] is False
    assert any(row["side"] == side and row["case"] == case_id
               and row["source_ref"] == f"{case_id}/contract-blackbox/1/runner-observation.json#/invariants/0"
               and "invariant" in row["reason"] for row in result["unverified_failures"])


def test_different_logical_commands_are_reported_even_when_output_matches(tmp_path: Path):
    case_id = "argv-difference-same-output"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    response = {"ok": True, "items": []}
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["session", "list"], response)])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["daemon", "status"], response)])

    result = _diff(a_dir, b_dir, {case_id})

    difference = next(item for item in result["differences"] if item["kind"] == "argv")
    assert difference["path"] == "call[0].argv"
    assert difference["source_refs"]["a"].startswith(f"{case_id}/bridge/1/cli.ndjson#L")
    assert difference["source_refs"]["b"].startswith(f"{case_id}/bridge/1/cli.ndjson#L")


def test_call_count_difference_has_one_entry_per_missing_call(tmp_path: Path):
    case_id = "argv-call-count-difference"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    calls_a = [_call(["session", "list"], {"ok": True}) for _ in range(3)]
    calls_b = [_call(["session", "list"], {"ok": True})]
    fakes.write_run(a_dir, case_id, side="a", calls=calls_a)
    fakes.write_run(b_dir, case_id, side="b", calls=calls_b)

    result = _diff(a_dir, b_dir, {case_id})

    count_differences = [item for item in result["differences"] if item["kind"] == "call_count"]
    assert [item["path"] for item in count_differences] == ["call[1]", "call[2]"]
    assert all(item["source_refs"]["a"] is not None and item["source_refs"]["b"] is None
               for item in count_differences)


@pytest.mark.parametrize("flag", ["--endpoint", "--socket", "--bench"])
def test_logical_argv_ignores_cli_path_and_instance_endpoint(tmp_path: Path, flag: str):
    case_id = f"argv-instance-only-{flag[2:]}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv_a = ["/instance/a/bin/serialwrap", flag, "/instance/a/run/serialwrapd.sock", "session", "list"]
    argv_b = ["C:\\instance\\b\\bin\\serialwrap.exe", flag, "C:\\instance\\b\\run\\serialwrapd.sock", "session", "list"]
    call_a = {"argv": argv_a, "rc": 0, "response": {"ok": True, "items": []}}
    call_b = {"argv": argv_b, "rc": 0, "response": {"ok": True, "items": []}}
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox", calls=[call_a])
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox", calls=[call_b])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []


def test_logical_argv_removes_equals_endpoint_and_substitutes_root(tmp_path: Path):
    case_id = "argv-equals-endpoint"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv_a = ["/instance/a/bin/serialwrap", "--endpoint=/instance/a/run/serialwrapd.sock",
              "event", "reload", "--config", "/instance/a/config/client.yaml"]
    argv_b = ["/instance/b/bin/serialwrap", "--endpoint=/instance/b/run/serialwrapd.sock",
              "event", "reload", "--config", "/instance/b/config/client.yaml"]
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox",
                    calls=[{"argv": argv_a, "rc": 0,
                            "response": {"ok": True, "added": ["one", "two"], "removed": [], "changed": []}}])
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox",
                    calls=[{"argv": argv_b, "rc": 0,
                            "response": {"ok": True, "added": ["one", "two"], "removed": [], "changed": []}}])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []


def test_logical_argv_substitutes_instance_root_in_other_argument_values(tmp_path: Path):
    case_id = "argv-instance-root-value"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv_a = ["/instance/a/bin/serialwrap", "--endpoint", "/instance/a/run/serialwrapd.sock",
              "event", "reload", "--config", "/instance/a/config/client.yaml"]
    argv_b = ["/instance/b/bin/serialwrap", "--endpoint", "/instance/b/run/serialwrapd.sock",
              "event", "reload", "--config", "/instance/b/config/client.yaml"]
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox",
                    calls=[{"argv": argv_a, "rc": 0,
                            "response": {"ok": True, "added": ["one", "two"], "removed": [], "changed": []}}])
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox",
                    calls=[{"argv": argv_b, "rc": 0,
                            "response": {"ok": True, "added": ["one", "two"], "removed": [], "changed": []}}])

    result = _diff(a_dir, b_dir, {case_id})

    assert result["differences"] == []


@pytest.mark.parametrize("preserved_flag", ["--timeout", "--cmd-id"])
def test_logical_argv_keeps_non_instance_arguments(tmp_path: Path, preserved_flag: str):
    case_id = f"argv-preserved-{preserved_flag[2:]}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    argv_a = ["/instance/a/bin/serialwrap", "--endpoint", "/instance/a/run/serialwrapd.sock",
              "cmd", "status", preserved_flag, "value-a"]
    argv_b = ["/instance/b/bin/serialwrap", "--endpoint", "/instance/b/run/serialwrapd.sock",
              "cmd", "status", preserved_flag, "value-b"]
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox",
                    calls=[{"argv": argv_a, "rc": 0, "response": {"ok": True}}])
    fakes.write_run(b_dir, case_id, side="b", runner="contract-blackbox",
                    calls=[{"argv": argv_b, "rc": 0, "response": {"ok": True}}])

    result = _diff(a_dir, b_dir, {case_id})

    assert any(item["kind"] == "argv" and item["path"] == "call[0].argv"
               for item in result["differences"])


def _rewrite_bridge_argv(run_dir: Path, argv: list[str]) -> None:
    """改寫 bridge CLI 觀測並同步 manifest 的原始位元組雜湊。"""
    cli_path = run_dir / "cli.ndjson"
    row = json.loads(cli_path.read_bytes().splitlines()[0].decode("utf-8"))
    row["argv"] = argv
    cli_path.write_bytes(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    manifest_path = run_dir / "manifest.json"
    manifest = fakes.read_json(manifest_path)
    manifest["evidence"]["cli_log"]["sha256"] = hashlib.sha256(cli_path.read_bytes()).hexdigest()
    fakes.write_json(manifest_path, manifest)


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("runner", fakes.RUNNERS, ids=fakes.RUNNERS)
@pytest.mark.parametrize("argv_shape", ["empty", "executable-only", "instance-options-only"])
def test_empty_logical_argv_breaks_integrity(
    tmp_path: Path, side: str, runner: str, argv_shape: str,
):
    case_id = f"empty-logical-argv-{runner}-{side}-{argv_shape}"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    bad_argv = {
        "empty": [],
        "executable-only": ["serialwrap"],
        "instance-options-only": ["serialwrap", "--endpoint", "INSTANCE_ROOT/run/serialwrapd.sock"],
    }[argv_shape]
    for report_dir, report_side in ((a_dir, "a"), (b_dir, "b")):
        calls = None
        if report_side == side:
            calls = [_call(bad_argv, {"ok": True})]
        run_dir = fakes.write_run(
            report_dir,
            case_id,
            side=report_side,
            runner=runner,
            calls=calls,
        )
        if report_side == side and runner == "bridge":
            _rewrite_bridge_argv(run_dir, bad_argv)

    result = _diff(a_dir, b_dir, {case_id})

    expected = "cli_observations[0].argv"
    assert result["integrity"][side] is False
    assert _has_problem(result, side, expected)
    if runner == "bridge":
        assert _has_problem(result, side, "cli.ndjson#L1")


@pytest.mark.parametrize("side", ["a", "b"])
def test_unmapped_raw_failure_breaks_integrity_even_with_passed_mapped_result(
    tmp_path: Path, side: str,
):
    case_id = f"unmapped-failure-with-pass-{side}"
    nodeids = ["tests/test_behavior.py::test_mapped_pass", "tests/test_behavior.py::test_unmapped_failure"]
    raw_results = [
        {"nodeid": nodeids[0], "outcome": "passed", "skip_kind": None,
         "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
        {"nodeid": nodeids[1], "outcome": "failed", "skip_kind": None,
         "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}},
    ]
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    for report_dir, report_side in ((a_dir, "a"), (b_dir, "b")):
        options = {}
        if report_side == side:
            options = {
                "l1_results": raw_results,
                "case_results": [{
                    "nodeid": nodeids[0],
                    "source_ref": f"{case_id}/contract-blackbox/1/source/l1-results.json#results[0]",
                }],
                "runner_exit_code": 0,
            }
        fakes.write_run(
            report_dir, case_id, side=report_side, runner="contract-blackbox", **options,
        )

    result = _diff(a_dir, b_dir, {case_id})
    failure_ref = f"{case_id}/contract-blackbox/1/source/l1-results.json#results[1]"

    assert result["integrity"][side] is False
    assert result["ok"] is False
    assert any(row["side"] == side and row["case"] == case_id and row["source_ref"] == failure_ref
               for row in result["unverified_failures"])
    assert any(failure_ref in problem for problem in result["integrity_problems"][side])


@pytest.mark.parametrize("side", ["a", "b"])
def test_envelope_fail_needs_mapped_raw_failure_evidence(tmp_path: Path, side: str):
    case_id = f"envelope-fail-unmapped-raw-failure-{side}"
    nodeid = "tests/test_behavior.py::test_unmapped_failure"
    raw_results = [{
        "nodeid": nodeid,
        "outcome": "failed",
        "skip_kind": None,
        "phases": {"setup": "passed", "call": "failed", "teardown": "passed"},
    }]
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    for report_dir, report_side in ((a_dir, "a"), (b_dir, "b")):
        options = {}
        if report_side == side:
            options = {
                "verdict": "FAIL",
                "runner_exit_code": 0,
                "l1_results": raw_results,
                "case_results": [],
            }
        fakes.write_run(
            report_dir, case_id, side=report_side, runner="contract-blackbox", **options,
        )

    result = _diff(a_dir, b_dir, {case_id})
    unverified = [row for row in result["unverified_failures"] if row["side"] == side]

    assert result["integrity"][side] is False
    assert result["ok"] is False
    assert any(row["source_ref"] == f"{case_id}/contract-blackbox/1/runner-observation.json"
               for row in unverified)
    assert any(row["source_ref"] == f"{case_id}/contract-blackbox/1/source/l1-results.json#results[0]"
               for row in unverified)
    assert result["integrity_problems"][side]


@pytest.mark.parametrize(
    "left_token,right_token",
    [
        ("--endpoint=alpha", "--endpoint=beta"),
        ("--socket=alpha", "--socket=beta"),
        ("--bench=alpha", "--bench=beta"),
    ],
    ids=["endpoint", "socket", "bench"],
)
def test_logical_argv_preserves_route_looking_tokens_after_double_dash(
    tmp_path: Path, left_token: str, right_token: str,
):
    case_id = "argv-after-double-dash-" + left_token.split("=", 1)[0][2:]
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    response = {"ok": True, "items": []}
    fakes.write_run(
        a_dir, case_id, side="a", runner="contract-blackbox",
        calls=[_call(["remote", "--", left_token], response)],
    )
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox",
        calls=[_call(["remote", "--", right_token], response)],
    )

    result = _diff(a_dir, b_dir, {case_id})

    assert any(item["kind"] == "argv" and item["path"] == "call[0].argv"
               for item in result["differences"])


def test_nonbridge_mixed_mapped_results_only_reports_failed_source(tmp_path: Path):
    case_id = "mixed-mapped-pass-and-fail"
    nodeids = ["tests/test_behavior.py::test_mapped_pass", "tests/test_behavior.py::test_mapped_failure"]
    refs = [f"{case_id}/contract-blackbox/1/source/l1-results.json#results[{index}]" for index in range(2)]
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", runner="contract-blackbox")
    fakes.write_run(
        b_dir, case_id, side="b", runner="contract-blackbox", verdict="FAIL", runner_exit_code=0,
        l1_results=[
            {"nodeid": nodeids[0], "outcome": "passed", "skip_kind": None,
             "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
            {"nodeid": nodeids[1], "outcome": "failed", "skip_kind": None,
             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}},
        ],
        case_results=[{"nodeid": nodeid, "source_ref": source_ref}
                      for nodeid, source_ref in zip(nodeids, refs)],
    )

    result = _diff(a_dir, b_dir, {case_id})

    failures = [row for row in result["product_failures"] if row["case"] == case_id]
    assert [row["source_ref"] for row in failures] == [refs[1]]


def test_paired_runs_follow_numeric_run_index_order(tmp_path: Path):
    # PR 330 review：配對順序須依數值 run_index（2 在 10 之前），不得用字串字典序。
    case_id = "numeric-run-order"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    for run_index in (2, 10):
        fakes.write_run(a_dir, case_id, side="a", run_index=run_index,
                        calls=[_call(["session", "list"], {"state": "READY"})])
        fakes.write_run(b_dir, case_id, side="b", run_index=run_index,
                        calls=[_call(["session", "list"], {"state": f"ATTACHED-{run_index}"})])

    result = _diff(a_dir, b_dir, {case_id})

    order = [item["source_refs"]["b"].split("/")[2] for item in result["differences"]
             if item["case"] == case_id and "state" in item["path"]]
    assert order == ["2", "10"]


def test_case_change_missing_from_registered_breaks_integrity(tmp_path: Path):
    # PR 330 review：case_changes 引用的變更 ID 必須存在於 registered，否則兩側完整性失敗。
    case_id = "unregistered-case-change"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    fakes.write_run(a_dir, case_id, side="a", calls=[_call(["session", "list"], {"state": "READY"})])
    fakes.write_run(b_dir, case_id, side="b", calls=[_call(["session", "list"], {"state": "READY"})])

    result = _diff(a_dir, b_dir, {case_id}, registered={"SC-15": "diff"},
                   case_changes={case_id: "BC-99"})

    assert result["integrity"] == {"a": False, "b": False}
    for side in ("a", "b"):
        assert _has_problem(result, side, f"unregistered case change: {case_id} -> BC-99")
    assert result["ok"] is False

    registered_ok = _diff(a_dir, b_dir, {case_id}, registered={"BC-99": "absolute_only"},
                          case_changes={case_id: "BC-99"})
    assert registered_ok["integrity"] == {"a": True, "b": True}
    assert registered_ok["ok"] is True
