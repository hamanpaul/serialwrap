"""OD-09 凍結門檻交由正式長跑 gate 判定的整合測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

import fakes_od09


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable]


def _od09():
    from realhw.acceptance import od09_freeze

    return od09_freeze


def _gate():
    from realhw.acceptance import gate

    return gate


def _yaml():
    import yaml

    return yaml


def _load_yaml(path: Path):
    return _yaml().safe_load(path.read_text(encoding="utf-8"))


def _freeze_env(root: Path, *, overrides=None):
    env = fakes_od09.od09_env(root)
    od09 = _od09()
    proposal = od09.propose(env.linux_stats, env.windows_stats, baseline_gates=env.baseline_gates)
    od09.freeze(
        env.acceptance,
        proposal,
        decision_id="OD-09-F1",
        approved_by="owner",
        overrides=overrides or {},
        stage0_index=env.stage0_index,
        evidence_root=env.evidence_root,
        ledger_path=env.ledger,
        addenda_path=env.addenda,
        now=fakes_od09.ts(20, 9),
    )
    return env


def _check(result, check_id: str):
    return next(item for item in result["checks"] if item["id"] == check_id)


def _commands(submitted: int, completed: int, *, source: str, com: str, offset: int = 0):
    rows = []
    for index in range(submitted):
        base = {
            "source": source,
            "com": com,
            "round": index,
            "cmd_id": f"{com}-{source}-{index}",
            "t": offset + index * 2,
        }
        rows.append({**base, "kind": "submit"})
        if index < completed:
            rows.append({**base, "kind": "done", "t": offset + index * 2 + 1})
    return rows


def _set_pair_counts(run, *, submitted: int, completed_agent: int, completed_human: int):
    preserved = [
        row for row in run.events
        if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}
    ]
    commands = _commands(
        submitted,
        completed_agent,
        source="agent:rhw0",
        com="COM0",
        offset=10,
    )
    commands += _commands(
        submitted,
        completed_human,
        source="human:h1",
        com="COM1",
        offset=11,
    )
    run.set_events(sorted(preserved + commands, key=lambda row: (row.get("t", 0), row["kind"] == "start")))


@pytest.mark.memory_only
def test_deferred_still_blocked(tmp_path):
    env = fakes_od09.od09_env(tmp_path)
    run = fakes_od09.longrun_case(
        tmp_path / "candidate",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
    )
    result = _gate().evaluate(run.inputs(
        mode="candidate",
        acceptance_path=env.acceptance,
        baseline=env.baseline_gates["linux"],
    ))
    checks = {item["id"]: item["result"] for item in result["checks"]}
    for check_id in ("resources", "min_effective_load", "comparison_uncertainty"):
        assert checks[check_id] == "BLOCKED"
    assert result["release_gate_result"] == "BLOCKED"


@pytest.mark.memory_only
def test_frozen_resources_fail_over_budget(tmp_path):
    env = _freeze_env(tmp_path / "over")
    acceptance = _load_yaml(env.acceptance)
    limit = acceptance["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"]
    baseline = fakes_od09.rerun_baseline_gate(env, platform="linux")
    run = fakes_od09.longrun_case(
        tmp_path / "linux-over",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
        rss_peak_kb=limit + 1,
    )
    result = _gate().evaluate(run.inputs(
        mode="candidate",
        acceptance_path=env.acceptance,
        baseline=baseline,
    ))
    assert _check(result, "resources")["result"] == "FAIL"
    assert result["release_gate_result"] == "FAIL"

    overrides = {
        "resources.per_platform.linux.rss_peak_kb": 60000,
        "resources.per_platform.windows.rss_peak_kb": 30000,
    }
    env = _freeze_env(tmp_path / "platform-overrides", overrides=overrides)
    linux_baseline = fakes_od09.rerun_baseline_gate(env, platform="linux")
    windows_baseline = fakes_od09.rerun_baseline_gate(env, platform="windows")
    linux_run = fakes_od09.longrun_case(
        tmp_path / "linux-same-peak",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
        rss_peak_kb=40000,
    )
    windows_run = fakes_od09.longrun_case(
        tmp_path / "windows-same-peak",
        platform="windows",
        started_at=fakes_od09.ts(21, 0),
        rss_peak_kb=40000,
    )
    linux_result = _gate().evaluate(linux_run.inputs(
        mode="candidate", acceptance_path=env.acceptance, baseline=linux_baseline
    ))
    windows_result = _gate().evaluate(windows_run.inputs(
        mode="candidate", acceptance_path=env.acceptance, baseline=windows_baseline
    ))
    assert _check(linux_result, "resources")["result"] == "PASS"
    assert _check(windows_result, "resources")["result"] == "FAIL"


@pytest.mark.memory_only
def test_frozen_min_load_insufficient(tmp_path):
    env = _freeze_env(tmp_path / "low-count")
    acceptance = _load_yaml(env.acceptance)
    minimum = acceptance["thresholds"]["min_effective_load"]["per_platform"]["linux"][
        "min_completed_per_board_source"
    ]["COM0|agent:rhw0"]
    baseline = fakes_od09.rerun_baseline_gate(env, platform="linux")
    run = fakes_od09.longrun_case(
        tmp_path / "count-candidate",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
    )
    kept = 0
    events = []
    for row in run.events:
        if row["kind"] == "done" and row.get("com") == "COM0" and row.get("source") == "agent:rhw0":
            if kept >= minimum - 1:
                continue
            kept += 1
        events.append(row)
    run.set_events(events)
    result = _gate().evaluate(run.inputs(
        mode="candidate", acceptance_path=env.acceptance, baseline=baseline
    ))
    assert _check(result, "min_effective_load")["result"] == "INSUFFICIENT_EVIDENCE"
    assert "COM0|agent:rhw0" in str(_check(result, "min_effective_load")["detail"])

    run = fakes_od09.longrun_case(
        tmp_path / "gap-candidate",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
    )
    completions = [
        row for row in run.events
        if row["kind"] == "done" and row.get("com") == "COM0" and row.get("source") == "agent:rhw0"
    ]
    completions[1]["t"] = completions[0]["t"] + 3601
    run.set_events(run.events)
    result = _gate().evaluate(run.inputs(
        mode="candidate", acceptance_path=env.acceptance, baseline=baseline
    ))
    assert _check(result, "min_effective_load")["result"] == "INSUFFICIENT_EVIDENCE"
    assert "COM0|agent:rhw0" in str(_check(result, "min_effective_load")["detail"])


@pytest.mark.memory_only
def test_frozen_tolerance_band_insufficient(tmp_path):
    env = _freeze_env(tmp_path)
    baseline_run = env.linux_run
    _set_pair_counts(baseline_run, submitted=1000, completed_agent=1000, completed_human=1000)
    baseline = fakes_od09.rerun_baseline_gate(env, platform="linux")

    for completed, expected in ((999, "INSUFFICIENT_EVIDENCE"), (997, "FAIL")):
        run = fakes_od09.longrun_case(
            tmp_path / f"error-{completed}",
            platform="linux",
            started_at=fakes_od09.ts(21, 0),
        )
        _set_pair_counts(run, submitted=1000, completed_agent=completed, completed_human=1000)
        result = _gate().evaluate(run.inputs(
            mode="candidate", acceptance_path=env.acceptance, baseline=baseline
        ))
        assert _check(result, "error_rate_relative")["result"] == expected

    run = fakes_od09.longrun_case(
        tmp_path / "throughput-94",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
    )
    _set_pair_counts(run, submitted=1000, completed_agent=940, completed_human=1000)
    result = _gate().evaluate(run.inputs(
        mode="candidate", acceptance_path=env.acceptance, baseline=baseline
    ))
    assert _check(result, "throughput_relative")["result"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.memory_only
def test_baseline_mode_not_applicable(tmp_path):
    env = _freeze_env(tmp_path)
    run = env.linux_run
    result = _gate().evaluate(run.inputs(mode="baseline", acceptance_path=env.acceptance))
    for check_id in ("resources", "min_effective_load", "comparison_uncertainty"):
        assert _check(result, check_id)["result"] == "NOT_APPLICABLE_BASELINE"


@pytest.mark.memory_only
def test_all_frozen_candidate_pass(tmp_path):
    env = _freeze_env(tmp_path)
    baseline = fakes_od09.rerun_baseline_gate(env, platform="linux")
    run = fakes_od09.longrun_case(
        tmp_path / "candidate",
        platform="linux",
        started_at=fakes_od09.ts(21, 0),
    )
    result = _gate().evaluate(run.inputs(
        mode="candidate",
        acceptance_path=env.acceptance,
        baseline=baseline,
    ))
    assert run.started.isoformat(timespec="seconds") > _load_yaml(env.acceptance)["thresholds"]["resources"]["frozen_at"]
    for check_id in ("resources", "min_effective_load", "comparison_uncertainty"):
        assert _check(result, check_id)["result"] == "PASS"
    assert result["release_gate_result"] == "PASS"
