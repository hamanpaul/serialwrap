"""P9-T13 實機回滾演練工具的黑箱程序替身測試。"""

from __future__ import annotations

import hashlib
import importlib
import json
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from tools.stage3.reason_codes import HarnessError


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _fakes():
    return importlib.import_module("stage3_fakes")


def _drill_fakes():
    return importlib.import_module("fakes_drill")


def _module():
    return importlib.import_module("tools.stage3.rollback_drill")


def _run(tmp_path, *, boards=("COM0",), current="0.9.0", previous="0.8.0", corrupt=True, start_fail=True, **over):
    fakes = _fakes()
    drill_fakes = _drill_fakes()
    runner_options = {
        key: over.pop(key) for key in tuple(over) if key in {
            "d1_generation", "reacquire", "pairing_ok", "flashing", "smoke_ok", "corrupt_refused",
            "start_fail_reverts", "forward_sideload",
        }
    }
    runner = drill_fakes.drill_runner(
        tmp_path,
        boards=boards,
        current=current,
        previous=previous,
        **runner_options,
    )
    for key in tuple(over):
        if key in {
            "same_pid_after_rollback", "smoke_fail_attempts", "foreign_holders", "multi_open",
            "cli_version_override", "status_version_override", "raise_setup_for",
            "corrupt_accepted_unchanged", "corrupt_mutates_current_tree", "corrupt_refused_but_mutates_state",
            "start_fail_reacquire",
            "smoke_gate_pending_remaining",
        }:
            setattr(runner, key, over.pop(key))
    cfg = drill_fakes.drill_config(
        tmp_path,
        runner,
        corrupt=corrupt,
        start_fail=start_fail,
        **over,
    )
    clock = fakes.FakeClock()
    result = _module().run_drill(
        cfg,
        run=runner,
        probe=fakes.FakeProbe(),
        clock=clock.now,
        sleep=clock.sleep,
        now=clock.iso,
        marker=lambda: "abc123",
    )
    return result, runner, cfg


def _run_config(runner, cfg):
    clock = _fakes().FakeClock()
    return _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                               sleep=clock.sleep, now=clock.iso, marker=lambda: "abc123")


def test_happy_drill_order_and_reacquire_checks(tmp_path):
    result, runner, _ = _run(tmp_path, boards=("COM0", "COM1"), d1_generation=7)
    assert result.verdict == "PASS"
    assert [step.step_id for step in result.steps] == list(_module().STEP_IDS)
    subs = [call["sub"] for call in runner.calls if call["kind"] == "cli"]
    assert subs.index("setup") < subs.index("cmd submit")
    assert all("interactive" not in sub for sub in subs)


def test_forward_argv_is_derived_from_d1_bundle_by_default(tmp_path):
    result, runner, cfg = _run(tmp_path, forward_argv=())
    forward = next(call for call in runner.calls if call["argv"][1:] == ["setup"])
    assert result.verdict == "PASS"
    assert Path(forward["argv"][0]).resolve() == (runner.bundle_dirs[cfg.expect_current] / "bin" / "serialwrap").resolve()


def test_force_forward_argv_is_rejected_in_preflight(tmp_path):
    result, runner, _ = _run(tmp_path, forward_argv=("/fixture/serialwrap", "setup", "--force-next"))
    assert result.verdict == "PRECONDITION_FAILED"
    assert "--force" in json.dumps(result.steps[0].detail)
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_invalid_corrupt_source_is_rejected_before_rollback(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=False)
    cfg = replace(cfg, corrupt_from=tmp_path / "missing-bundle")
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    assert result.verdict == "PRECONDITION_FAILED"
    assert "corrupt source" in json.dumps(result.steps[0].detail)
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_corrupt_source_with_invalid_launcher_path_is_rejected_in_preflight(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=False)
    manifest_path = cfg.corrupt_from / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"]["daemon"]["launcher"]["path"] = "../outside/launcher"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run_config(runner, cfg)
    assert result.verdict == "PRECONDITION_FAILED"
    assert "manifest 路徑不在 bundle 內" in json.dumps(result.steps[0].detail, ensure_ascii=False)
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_existing_corrupt_output_is_preserved_and_preflight_fails(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=False)
    destination = cfg.out_dir / "corrupt"
    destination.mkdir(parents=True)
    marker = destination / "user-data.txt"
    marker.write_text("keep", encoding="utf-8")
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    assert result.verdict == "PRECONDITION_FAILED"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_version_flip_without_reacquire_fails_and_restores(tmp_path):
    result, _, _ = _run(tmp_path, reacquire=False)
    assert result.verdict == "FAIL" and result.reason_code == "HARNESS_DRILL_STEP_FAILED"
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert d3.ok is False and "last_ready_at" in json.dumps(d3.detail)
    assert result.restore is not None and result.restore.step_id == "restore"


def test_flashing_precondition_stops_before_setup(tmp_path):
    result, runner, _ = _run(tmp_path, flashing=True)
    assert result.verdict == "PRECONDITION_FAILED"
    assert result.reason_code == "HARNESS_DRILL_PRECONDITION"
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_board_not_ready_precondition_stops_before_setup(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.not_ready_boards.add("COM0")
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    assert result.verdict == "PRECONDITION_FAILED"
    assert result.reason_code == "HARNESS_DRILL_PRECONDITION"
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_board_row_disappearing_after_rollback_fails_restores_and_reports(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.drop_boards_after_rollback.add("COM0")
    result = _run_config(runner, cfg)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.verdict == "FAIL" and d3.ok is False
    assert "COM0" in json.dumps(d3.detail)
    assert result.restore is not None and result.restore.step_id == "restore"
    assert report["verdict"] == "FAIL" and report["restore"] is not None


def test_non_board_session_disappearance_fails_alias_invariant(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.extra_sessions.append({"alias": "detached-fixture", "com": "COM5", "state": "DETACHED"})
    runner.drop_sessions_after_rollback.add("COM5")
    result = _run_config(runner, cfg)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "COM5" in json.dumps(d3.detail)


def test_alias_change_fails_d3(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.changed_aliases_after_rollback.add("COM0")
    result = _run_config(runner, cfg)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "alias" in json.dumps(d3.detail)


def test_multi_open_after_rollback_fails_d3(tmp_path):
    result, _, _ = _run(tmp_path, multi_open=True)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "multi_open" in json.dumps(d3.detail)


def test_foreign_holders_use_contract_object_shape_and_record_unknowns(tmp_path):
    unknown = {"device": "/dev/tty-fixture-0", "holder_unknown": True}
    result, _, _ = _run(tmp_path, foreign_holders={"/dev/tty-fixture-0": unknown})
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert d3.ok is True
    assert d3.detail["foreign_holders_detail"] == [
        {"tty": "/dev/tty-fixture-0", "device": "/dev/tty-fixture-0", "holder_unknown": True,
         "pids": [], "foreign_pids": []}
    ]


@pytest.mark.parametrize(
    "foreign_holders",
    [
        [],
        {"/dev/tty-fixture-0": 999},
        {"/dev/tty-fixture-0": {"holder_unknown": True}},
        {"/dev/tty-fixture-0": {"device": "/dev/tty-fixture-0", "holder_unknown": True, "pids": [999]}},
    ],
    ids=["list-shape", "other-pid", "unknown-holder-missing-device", "unknown-holder-with-foreign-pid"],
)
def test_invalid_or_foreign_holder_fails_d3(tmp_path, foreign_holders):
    result, _, _ = _run(tmp_path, foreign_holders=foreign_holders)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "foreign_holders" in json.dumps(d3.detail)


def test_d3_rejects_unexpected_installed_layout(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.layout_dir_override = runner.bundle_dirs[cfg.expect_current]
    result = _run_config(runner, cfg)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "安裝佈局" in json.dumps(d3.detail, ensure_ascii=False)
    assert result.restore is not None and result.restore.ok is False
    assert result.restore.detail["action"] == "none"


@pytest.mark.parametrize("override_field", ["cli_version_override", "status_version_override"])
def test_d3_rejects_cli_or_daemon_version_mismatch(tmp_path, override_field):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    setattr(runner, override_field, "0.9.0")
    result = _run_config(runner, cfg)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False


def test_unchanged_daemon_pid_after_rollback_fails_d3(tmp_path):
    result, _, _ = _run(tmp_path, same_pid_after_rollback=True)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "pid" in json.dumps(d3.detail)


def test_empty_board_list_is_a_precondition_failure(tmp_path):
    runner = _drill_fakes().drill_runner(tmp_path, boards=(), current="0.9.0", previous="0.8.0")
    cfg = _drill_fakes().drill_config(tmp_path, runner, boards=(), corrupt=True, start_fail=True)
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    assert result.verdict == "PRECONDITION_FAILED"
    assert "boards 不可為空" in json.dumps(result.steps[0].detail, ensure_ascii=False)
    assert all(call["sub"] != "setup" for call in runner.calls)


def test_pairing_failure_after_rollback_fails_d3(tmp_path):
    result, _, _ = _run(tmp_path, pairing_ok=False)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "version_pairing" in json.dumps(d3.detail)


def test_doctor_checks_list_shape_and_pairing_row_are_required(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=False, start_fail=False)
    runner.doctor_checks = [{"check": "python", "ok": True}]
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    d1 = next(step for step in result.steps if step.step_id == "D1-record")
    assert result.verdict == "FAIL" and d1.ok is False
    assert "version_pairing" in json.dumps(d1.detail)


def test_old_pid_still_alive_fails_d3(tmp_path):
    old_pid = 100
    result = _run_with_probe(tmp_path, _fakes().FakeProbe(pid_alive={old_pid: [True]}))
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "pid" in json.dumps(d3.detail)


def _run_with_probe(tmp_path: Path, probe):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    clock = _fakes().FakeClock()
    return _module().run_drill(cfg, run=runner, probe=probe, clock=clock.now, sleep=clock.sleep, now=clock.iso)


def test_device_binding_change_fails_d3(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.changed_bindings_after_rollback.add("COM0")
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "device_by_id" in json.dumps(d3.detail)


def test_released_session_lost_fails_d3(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.released_sessions = [{
        "alias": "mcu",
        "attached_real_path": "/dev/ttyMCU",
        "bridge_generation": 3,
        "com": "COM2",
        "device_by_id": "/dev/serial/by-id/mcu",
        "last_ready_at": None,
        "state": "RELEASED",
    }]
    runner.drop_released_session = True
    runner.not_ready_boards.add("COM2")
    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso)
    d3 = next(step for step in result.steps if step.step_id == "D3-verify-previous")
    assert result.verdict == "FAIL" and d3.ok is False
    assert "RELEASED" in json.dumps(d3.detail)


def test_smoke_marker_missing_fails_d4(tmp_path):
    result, _, _ = _run(tmp_path, smoke_ok=False)
    d4 = next(step for step in result.steps if step.step_id == "D4-smoke-previous")
    assert result.verdict == "FAIL" and d4.ok is False


def test_smoke_command_echo_is_not_accepted_as_output(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    runner.echo_only = True
    result = _run_config(runner, cfg)
    d4 = next(step for step in result.steps if step.step_id == "D4-smoke-previous")
    assert result.verdict == "FAIL" and d4.ok is False
    assert "echo SW''DRILL" in json.dumps(d4.detail)


def test_smoke_waits_for_ready_gate_before_single_submit(tmp_path):
    result, runner, _ = _run(tmp_path, smoke_gate_pending_remaining=2)
    assert result.verdict == "PASS"
    assert runner.smoke_gate_pending_remaining == 0
    assert sum(call["sub"] == "cmd submit" for call in runner.calls) == 3


def test_smoke_stops_polling_after_terminal_command_error(tmp_path):
    result, runner, _ = _run(tmp_path, smoke_fail_attempts={1})
    d4 = next(step for step in result.steps if step.step_id == "D4-smoke-previous")
    assert result.verdict == "FAIL" and d4.ok is False
    assert sum(call["sub"] == "cmd status" for call in runner.calls) == 1


def test_d7_corrupt_bundle_refused(tmp_path):
    result, _, _ = _run(tmp_path, corrupt=True)
    d7 = next(step for step in result.steps if step.step_id == "D7-invalid-artifact-refusal")
    assert result.verdict == "PASS" and d7.ok is True


def test_d7_corrupt_bundle_accepted_fails(tmp_path):
    result, _, _ = _run(tmp_path, corrupt=True, corrupt_refused=False)
    d7 = next(step for step in result.steps if step.step_id == "D7-invalid-artifact-refusal")
    assert result.verdict == "FAIL" and d7.ok is False


def test_d7_runner_spawn_failure_is_not_a_refusal(tmp_path):
    result, _, _ = _run(tmp_path, raise_setup_for={"corrupt"})
    d7 = next(step for step in result.steps if step.step_id == "D7-invalid-artifact-refusal")
    assert result.verdict == "FAIL" and d7.ok is False
    assert "corrupt artifact 未被拒絕" in json.dumps(d7.detail, ensure_ascii=False)


def test_d7_success_response_without_state_change_is_not_a_refusal(tmp_path):
    result, _, _ = _run(tmp_path, corrupt_refused=False, corrupt_accepted_unchanged=True)
    d7 = next(step for step in result.steps if step.step_id == "D7-invalid-artifact-refusal")
    assert result.verdict == "FAIL" and d7.ok is False
    assert "corrupt artifact 未被拒絕" in json.dumps(d7.detail, ensure_ascii=False)


def test_d7_rejected_setup_must_leave_deployed_tree_unchanged(tmp_path):
    result, _, _ = _run(tmp_path, corrupt_refused=False, corrupt_accepted_unchanged=True,
                         corrupt_mutates_current_tree=True)
    d7 = next(step for step in result.steps if step.step_id == "D7-invalid-artifact-refusal")
    assert result.verdict == "FAIL" and d7.ok is False
    assert "Current bundle_tree_sha256 改變" in json.dumps(d7.detail, ensure_ascii=False)


def test_d7_refusal_with_pid_change_fails(tmp_path):
    result, _, _ = _run(tmp_path, corrupt_refused_but_mutates_state=True)
    d7 = next(step for step in result.steps if step.step_id == "D7-invalid-artifact-refusal")
    assert result.verdict == "FAIL" and d7.ok is False
    assert "daemon version／pid 改變" in json.dumps(d7.detail, ensure_ascii=False)


def test_d8_start_failure_reverted_restores_complete_current(tmp_path):
    result, runner, _ = _run(tmp_path, start_fail=True)
    d8 = next(step for step in result.steps if step.step_id == "D8-start-failure-revert")
    assert result.verdict == "PASS" and d8.ok is True
    assert runner.pid == 103 and d8.detail["daemon_pid"] == 103
    assert runner.last_ready_at > d8.started_at
    assert result.restore is None


def test_d8_release_check_allows_only_the_new_same_path_daemon(tmp_path):
    current_daemon = str(tmp_path / "versions" / "0.9.0" / "bin" / "serialwrapd")
    probe = _fakes().FakeProbe(by_exe={current_daemon: [(), (103,)]})
    result = _run_with_probe(tmp_path, probe)
    d8 = next(step for step in result.steps if step.step_id == "D8-start-failure-revert")
    assert result.verdict == "PASS" and d8.ok is True


def test_d8_failure_without_revert_fails(tmp_path):
    result, _, _ = _run(tmp_path, start_fail=True, start_fail_reverts=False)
    d8 = next(step for step in result.steps if step.step_id == "D8-start-failure-revert")
    assert result.verdict == "FAIL" and d8.ok is False


def test_d8_spawn_failure_is_not_a_refusal(tmp_path):
    result, _, _ = _run(tmp_path, raise_setup_for={"startfail"})
    d8 = next(step for step in result.steps if step.step_id == "D8-start-failure-revert")
    assert result.verdict == "FAIL" and d8.ok is False
    assert "start-fail artifact 未回報啟動失敗" in json.dumps(d8.detail, ensure_ascii=False)


def test_d8_requires_fresh_ready_timestamp_after_pid_change(tmp_path):
    result, runner, _ = _run(tmp_path, start_fail_reacquire=False)
    d8 = next(step for step in result.steps if step.step_id == "D8-start-failure-revert")
    assert runner.pid == 103
    assert result.verdict == "FAIL" and d8.ok is False
    assert "last_ready_at 未晚於步驟開始時間" in json.dumps(d8.detail, ensure_ascii=False)


def test_d6_smoke_failure_is_not_hidden_by_d4_success(tmp_path):
    result, _, _ = _run(tmp_path, smoke_fail_attempts={2})
    d6 = next(step for step in result.steps if step.step_id == "D6-verify-current")
    assert result.verdict == "FAIL" and d6.ok is False
    assert "終態錯誤" in json.dumps(d6.detail, ensure_ascii=False)


def test_d8_smoke_failure_is_not_hidden_by_d4_and_d6_success(tmp_path):
    result, _, _ = _run(tmp_path, smoke_fail_attempts={3})
    d8 = next(step for step in result.steps if step.step_id == "D8-start-failure-revert")
    assert result.verdict == "FAIL" and d8.ok is False
    assert "終態錯誤" in json.dumps(d8.detail, ensure_ascii=False)


def test_unexpected_d4_exception_restores_and_writes_report(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)

    def fail_marker():
        raise RuntimeError("fixture exception")

    clock = _fakes().FakeClock()
    result = _module().run_drill(cfg, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                 sleep=clock.sleep, now=clock.iso, marker=fail_marker)
    failed = next(step for step in result.steps if step.step_id == "D4-smoke-previous")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.verdict == "FAIL" and failed.ok is False
    assert result.restore is not None and result.restore.detail["action"] == "roll-forward"
    assert report["verdict"] == "FAIL" and report["restore"] is not None


def test_missing_start_fail_bundle_is_incomplete(tmp_path):
    result, _, _ = _run(tmp_path, start_fail=False)
    assert result.verdict == "INCOMPLETE"
    assert result.reason_code == "HARNESS_DRILL_INCOMPLETE"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert "step7_revert_not_exercised" in report["limitations"]


def test_make_corrupted_bundle_changes_only_cli_manifest_digest(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    source = cfg.corrupt_from
    corrupt = _module().make_corrupted_bundle(source, tmp_path / "bad")
    original = json.loads((source / "release-manifest.json").read_text(encoding="utf-8"))
    changed = json.loads((corrupt / "release-manifest.json").read_text(encoding="utf-8"))
    old_digest = original["components"]["cli"]["sha256"]
    new_digest = changed["components"]["cli"]["sha256"]
    expected = copy.deepcopy(original)
    expected["components"]["cli"]["sha256"] = new_digest
    original_bytes = (source / "release-manifest.json").read_bytes()
    changed_bytes = (corrupt / "release-manifest.json").read_bytes()
    assert original_bytes.count(old_digest.encode()) == 1
    assert changed_bytes == original_bytes.replace(old_digest.encode(), new_digest.encode(), 1)
    assert old_digest != new_digest
    assert old_digest[:-1] == new_digest[:-1]
    assert expected == changed


def test_make_start_fail_bundle_hashes_stub_and_checksums(tmp_path):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    source_manifest = json.loads((cfg.corrupt_from / "release-manifest.json").read_text(encoding="utf-8"))
    source_sums = (cfg.corrupt_from / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    bundle = _module().make_start_fail_bundle(cfg.corrupt_from, tmp_path / "startfail")
    doc = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    rel = doc["components"]["daemon"]["launcher"]["path"]
    launcher = bundle / rel
    assert doc["components"]["daemon"]["launcher"]["sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    sums = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"{hashlib.sha256(launcher.read_bytes()).hexdigest()}  {rel}" in sums.splitlines()
    original_launcher = source_manifest["components"]["daemon"]["launcher"]["path"]
    expected = copy.deepcopy(source_manifest)
    expected_launcher = expected["components"]["daemon"]["launcher"]
    expected_launcher["path"] = doc["components"]["daemon"]["launcher"]["path"]
    expected_launcher["sha256"] = doc["components"]["daemon"]["launcher"]["sha256"]
    assert doc == expected
    output_sums = (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    changed_paths = {original_launcher, rel}
    assert {line for line in output_sums if line.split("  ", 1)[1] not in changed_paths} == {
        line for line in source_sums if line.split("  ", 1)[1] not in changed_paths
    }


@pytest.mark.parametrize("helper_name,destination_name", [
    ("make_corrupted_bundle", "bad"), ("make_start_fail_bundle", "startfail"),
])
def test_bundle_helpers_preserve_existing_destination(tmp_path, helper_name, destination_name):
    drill_fakes = _drill_fakes()
    runner = drill_fakes.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg = drill_fakes.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    destination = tmp_path / destination_name
    destination.mkdir()
    marker = destination / "user-data.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(HarnessError, match="目的地已存在"):
        getattr(_module(), helper_name)(cfg.corrupt_from, destination)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_report_records_actual_deployed_tree_and_manifest(tmp_path):
    result, _, cfg = _run(tmp_path)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    current = tmp_path / "versions" / cfg.expect_current
    manifest_sha = hashlib.sha256((current / "release-manifest.json").read_bytes()).hexdigest()
    assert report["current_release_manifest_sha256"] == manifest_sha
    assert report["current_bundle_tree_sha256"] == _bundle_tree(current)
    assert report["current_runtime_tree_sha256"] == _runtime_tree(current)
    assert report["started_at"] and report["finished_at"]
    assert report["started_at"] <= report["finished_at"]

    sidecar_root = tmp_path / "sidecar"
    runner = _drill_fakes().drill_runner(sidecar_root, boards=("COM0",), current="0.9.0", previous="0.8.0")
    cfg2 = _drill_fakes().drill_config(sidecar_root, runner, corrupt=True, start_fail=True)
    current2 = sidecar_root / "versions" / cfg2.expect_current
    clean_tree = _bundle_tree(current2)
    (current2 / "bin" / "version.dll").write_bytes("旁載檔\n".encode("utf-8"))
    dirty_tree = _bundle_tree(current2)
    clock = _fakes().FakeClock()
    result2 = _module().run_drill(cfg2, run=runner, probe=_fakes().FakeProbe(), clock=clock.now,
                                  sleep=clock.sleep, now=clock.iso)
    report2 = json.loads(result2.report_path.read_text(encoding="utf-8"))
    assert result2.verdict == "PASS"
    assert report2["current_bundle_tree_sha256"] == dirty_tree
    assert report2["current_runtime_tree_sha256"] == _runtime_tree(current2)
    assert report2["current_bundle_tree_sha256"] != clean_tree


def test_forward_to_different_tree_fails_d6(tmp_path):
    result, _, _ = _run(tmp_path, forward_sideload=True)
    d6 = next(step for step in result.steps if step.step_id == "D6-verify-current")
    assert result.verdict == "FAIL" and d6.ok is False
    assert "deployed tree changed: bundle_tree_sha256" in json.dumps(d6.detail)
    assert "deployed tree changed: runtime_tree_sha256" in json.dumps(d6.detail)


def test_no_forbidden_commands_or_force_flags(tmp_path):
    _, runner, _ = _run(tmp_path)
    forbidden = {"session interactive-status", "session interactive-open", "device release"}
    for call in runner.calls:
        if call["kind"] != "cli":
            continue
        assert call["sub"] not in forbidden
        assert "--force" not in call["argv"]


def _tree(root: Path, *, runtime: bool) -> str:
    rows: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if runtime and rel in {"release-manifest.json", "SHA256SUMS"}:
            continue
        if path.is_file():
            rows.append(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    return hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()


def _bundle_tree(root: Path) -> str:
    return _tree(root, runtime=False)


def _runtime_tree(root: Path) -> str:
    return _tree(root, runtime=True)
