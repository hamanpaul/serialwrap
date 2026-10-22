"""P9-T6：Sequential A/B 執行器（tools/stage3/ab_runner.py）測試。

替身與 fixture 來源（由其他 Task 提供，本檔只消費）：
- `stage3_fakes`（P9-T2）：FakeClock、FakeProbe、RecordingRunner 等基礎替身。
- `fakes_suites`（P9-T5B）：ab_env，產生 A/B manifest、RecordingRunner 與報告樹。
- `fakes_od09`（P2 Task 16）：L3 門檻凍結所需的合成 repo 與 freeze 流程。

所有 import 皆延後到測試函式內，使前置模組尚未存在時整個測試套件仍可收集（plan Task 6 Step 1）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

# 只計 functest／testpilot／realhw／cli 等「側內」命令，不含 git、docker 等前置呼叫。
SIDE_KINDS = frozenset({"functest", "cli", "testpilot", "realhw"})
# 晚於 fakes_od09 的凍結時點 ts(20, 9)，使 check_frozen_before 判定為「凍結早於 run 起點」。
L3_LATE_START = "2026-11-22T00:00:00+00:00"


def _stage3_fakes():
    import stage3_fakes

    return stage3_fakes


def _ab_fixtures():
    import fakes_suites

    return fakes_suites


def _rc(rc: int, stdout: str = "", stderr: str = "") -> dict:
    return {"rc": rc, "stdout": stdout, "stderr": stderr}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _script(env, key: str, *results: dict) -> None:
    """設定 RecordingRunner 某一鍵（f"{kind}:{sub}"）的逐次回應；耗盡後重複最後一筆。"""
    env.runner.script[key] = [dict(r) for r in results]


def _late_clock():
    return _stage3_fakes().FakeClock(start_wall=L3_LATE_START)


def _run(tmp_path, env, probe=None, clock=None, *, out_dir=None):
    from tools.stage3 import ab_runner

    fakes = _stage3_fakes()
    probe = probe if probe is not None else fakes.FakeProbe()
    clock = clock if clock is not None else fakes.FakeClock()
    return ab_runner.run_ab(
        env.manifest,
        out_dir=tmp_path / "out" if out_dir is None else out_dir,
        repo_root=env.repo,
        run=env.runner,
        spawn=env.runner.spawn,
        probe=probe,
        clock=clock.now,
        sleep=clock.sleep,
        now=clock.iso,
    )


def _functest_calls(env) -> list[dict]:
    return [c for c in env.recorded() if c["kind"] == "functest"]


def _freeze_acceptance(env, root) -> None:
    """以 P2 fakes_od09 的 propose／freeze 流程凍結 OD-09 延後項，換入本 env 的門檻檔（R4-F2）。

    換檔後同步 manifest 宣告的 acceptance sha256，避免 verify_frozen 把門檻檔替換判為漂移。
    """
    import fakes_od09
    from realhw.acceptance import od09_freeze

    od = fakes_od09.od09_env(root / "od09")
    proposal = od09_freeze.propose(od.linux_stats, od.windows_stats, baseline_gates=od.baseline_gates)
    od09_freeze.freeze(
        od.acceptance,
        proposal,
        decision_id="OD-09-F1",
        approved_by="owner",
        overrides={},
        stage0_index=od.stage0_index,
        evidence_root=od.evidence_root,
        ledger_path=od.ledger,
        addenda_path=od.addenda,
        now=fakes_od09.ts(20, 9),
    )
    target = env.repo / env.m.acceptance.path
    target.write_bytes(od.acceptance.read_bytes())
    doc = json.loads(env.manifest.read_text(encoding="utf-8"))
    doc["acceptance"]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    import stage3_fakes

    # acceptance 同時位於 realhw 凍結工具樹內，替換門檻後須同步重算整棵樹。
    for tool in doc["tools"]:
        if tool.get("path") == "realhw":
            tool["tree_sha256"] = stage3_fakes.tree_sha256(env.repo / "realhw")
    env.manifest.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """把 TestPilot 替身的 b-log 輸出限制在此測試的暫存 HOME。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture(autouse=True)
def _forbid_real_candidate_ledger(monkeypatch):
    """本檔測試不得寫入 repo 內真實的候選長跑 ledger；需要登記的測試自行覆寫此函式。"""
    from realhw.acceptance import od09_freeze

    def _forbidden(*args, **kwargs):
        raise AssertionError("測試不得呼叫真實 register_candidate_start（會寫入 repo ledger）")

    monkeypatch.setattr(od09_freeze, "register_candidate_start", _forbidden)


# ---------------------------------------------------------------------------
# 執行序主幹（plan Task 6 Step 1 關鍵測試）
# ---------------------------------------------------------------------------


def test_happy_path_order(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path)
    res = _run(tmp_path, env)
    assert res.verdict == "PASS"
    seq = [(c["kind"], c["side"]) for c in env.recorded() if c["kind"] == "functest"]
    assert seq == [("functest", "A"), ("functest", "B")]
    a_env = [c["env"] for c in env.recorded() if c["kind"] == "functest"][0]
    assert a_env["SERIALWRAP_DAEMON_EXE"] == str(env.m.sides[0].daemon_exe)


def test_b_not_started_when_a_release_unconfirmed(tmp_path):
    fakes = _stage3_fakes()
    env = _ab_fixtures().ab_env(tmp_path)
    # suite 的前置探測先證實無殘留；release check 再觀察到未釋放 PID。
    probe = fakes.FakeProbe(by_exe={str(env.m.sides[0].daemon_exe): [(), (env.a_daemon_pid,)]})
    res = _run(tmp_path, env, probe, fakes.FakeClock())
    assert (res.verdict, res.reason_code) == ("INFRA_ERROR", "HARNESS_RELEASE_UNCONFIRMED")
    assert [c["side"] for c in _functest_calls(env)] == ["A"]
    report = json.loads((tmp_path / "out" / "ab-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INFRA_ERROR"
    assert report["sides"][0]["release"]["released"] is False
    assert report["sides"][1]["status"] == "not_run"
    assert report["sides"][1]["abort_code"] is None


def test_both_sides_on_default_python_rejected(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, functest_identity="python_default")
    res = _run(tmp_path, env)
    assert res.reason_code == "HARNESS_ARTIFACT_MISMATCH"
    assert res.verdict != "PASS"


def test_manifest_drift_runs_nothing(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path)
    p = env.repo / "tests" / "acceptance" / "cases.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    res = _run(tmp_path, env)
    assert (res.verdict, res.reason_code) == ("INFRA_ERROR", "HARNESS_MANIFEST_DRIFT")
    assert {c["kind"] for c in env.recorded()} <= {"git", "docker"}
    report = json.loads(res.report_path.read_text(encoding="utf-8"))
    assert all(side["status"] == "not_run" and side["abort_code"] is None for side in report["sides"])
    assert report["reason_code"] == "HARNESS_MANIFEST_DRIFT"


def test_b_product_fail_survives_release_unknown(tmp_path):
    fakes = _stage3_fakes()
    env = _ab_fixtures().ab_env(tmp_path, b_functest_rc=1)
    # suite 的前置探測乾淨；只有後續釋放確認回報未知，才能驗證產品 FAIL 保留。
    probe = fakes.FakeProbe(by_exe={str(env.m.sides[1].daemon_exe): [(), None]})
    res = _run(tmp_path, env, probe, fakes.FakeClock())
    assert res.verdict == "FAIL" and res.reason_code == "HARNESS_RELEASE_UNCONFIRMED"
    report = json.loads(res.report_path.read_text(encoding="utf-8"))
    assert report["sides"][1]["status"] == "FAIL"
    assert any("release unconfirmed" in note for note in report["rounds"][0]["notes"])


def test_release_checked_even_when_suite_raises(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path)
    env.runner.raise_on("functest:", RuntimeError("boom"))
    res = _run(tmp_path, env)
    assert res.sides[0].release is not None
    assert res.verdict == "INFRA_ERROR"


# ---------------------------------------------------------------------------
# 複驗、漂移、案例涵蓋與 L3 守門（plan Task 6 Step 1 表格）
# ---------------------------------------------------------------------------


def test_reverify_always_runs_second_round(tmp_path):
    # 第一輪 B 即 FAIL；複驗輪仍須執行（複驗是否執行由 manifest 預先決定，不看第一輪結果）。
    env = _ab_fixtures().ab_env(tmp_path, b_functest_rc=1, **{"reverify.enabled": True})
    res = _run(tmp_path, env)
    assert len(res.rounds) == 2
    assert res.rounds[1].order == ("B", "A")
    round2_sides = [c["side"] for c in _functest_calls(env) if c["round"] == "2"]
    assert round2_sides == ["B", "A"]


def test_reverify_requires_both_rounds_pass(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, **{"reverify.enabled": True})
    # functest 呼叫依序：第 1 輪 A、B；第 2 輪 B（順序互換）、A。第 2 輪 B 回 rc=1。
    _script(env, "functest:", _rc(0), _rc(0), _rc(1), _rc(0))
    res = _run(tmp_path, env)
    assert len(res.rounds) == 2
    assert res.rounds[0].verdict == "PASS"
    assert res.verdict == "FAIL"


def test_fail_not_masked_by_infra(tmp_path, monkeypatch):
    from tools.stage3 import ab_diff
    from tools.stage3.reason_codes import HarnessError

    env = _ab_fixtures().ab_env(tmp_path, b_functest_rc=1, **{"reverify.enabled": True})
    real_diff = ab_diff.diff_l15
    state = {"calls": 0}

    def _diff_second_round_fails(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] >= 2:
            raise HarnessError("HARNESS_COMPARATOR_ERROR", "forced in round 2")
        return real_diff(*args, **kwargs)

    monkeypatch.setattr(ab_diff, "diff_l15", _diff_second_round_fails)
    res = _run(tmp_path, env)
    assert res.verdict == "FAIL"


def test_candidate_fail_not_masked_by_same_round_comparator_error(tmp_path, monkeypatch):
    from tools.stage3 import ab_diff
    from tools.stage3.reason_codes import HarnessError

    env = _ab_fixtures().ab_env(tmp_path, b_functest_rc=1)

    def _fail_comparison(*args, **kwargs):
        raise HarnessError("HARNESS_COMPARATOR_ERROR", "forced in the failing round")

    monkeypatch.setattr(ab_diff, "diff_l15", _fail_comparison)
    res = _run(tmp_path, env)
    assert (res.verdict, res.reason_code) == ("FAIL", "HARNESS_COMPARATOR_ERROR")


def test_drift_between_sides_aborts_b(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path)
    b_daemon = env.m.sides[1].bundle_dir / "bin" / "serialwrapd"

    def _tamper_b_while_a_runs(argv, call_env):
        if (call_env or {}).get("SW_STAGE3_SIDE") == "A":
            b_daemon.write_bytes(b_daemon.read_bytes() + b"# tampered\n")

    env.runner.on("functest:", _tamper_b_while_a_runs)
    res = _run(tmp_path, env)
    assert res.reason_code == "HARNESS_MANIFEST_DRIFT"
    assert [c for c in env.recorded() if c["kind"] == "functest" and c["side"] == "B"] == []


def test_sideload_between_sides_aborts_b(tmp_path):
    # R3-F2：manifest 未宣告的旁載檔只在 bundle 內容指紋現形；verify_frozen 仍應回 []。
    env = _ab_fixtures().ab_env(tmp_path)
    side_b_sideload = env.m.sides[1].bundle_dir / "bin" / "version.dll"

    def _sideload_b_while_a_runs(argv, call_env):
        if (call_env or {}).get("SW_STAGE3_SIDE") == "A":
            side_b_sideload.write_bytes(b"MZ-sideload")

    env.runner.on("functest:", _sideload_b_while_a_runs)
    res = _run(tmp_path, env)
    assert res.reason_code == "HARNESS_MANIFEST_DRIFT"
    assert [c for c in env.recorded() if c["kind"] == "functest" and c["side"] == "B"] == []
    assert res.verdict != "PASS"


def test_bundle_tree_recheck_failure_aborts_before_next_side(tmp_path, monkeypatch):
    from tools.stage3 import ab_manifest

    env = _ab_fixtures().ab_env(tmp_path)
    target = env.m.sides[0].bundle_dir
    real_tree = ab_manifest.bundle_tree_sha256
    seen = 0

    def _fail_after_release(path):
        nonlocal seen
        if path == target:
            seen += 1
            if seen == 3:
                raise OSError("post-release tree unavailable")
        return real_tree(path)

    monkeypatch.setattr(ab_manifest, "bundle_tree_sha256", _fail_after_release)
    res = _run(tmp_path, env)
    assert (res.verdict, res.reason_code) == ("INFRA_ERROR", "HARNESS_MANIFEST_DRIFT")
    assert [c for c in _functest_calls(env) if c["side"] == "B"] == []


def test_bundle_tree_change_during_side_is_reported_and_aborts_b(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path)

    def _change_a_while_running(argv, call_env):
        if (call_env or {}).get("SW_STAGE3_SIDE") == "A":
            _ab_fixtures().add_late_file(env.m.sides[0].bundle_dir, 0)

    env.runner.on("functest:", _change_a_while_running)
    res = _run(tmp_path, env)
    report = json.loads(res.report_path.read_text(encoding="utf-8"))
    assert (res.verdict, res.reason_code) == ("INFRA_ERROR", "HARNESS_MANIFEST_DRIFT")
    assert [call for call in _functest_calls(env) if call["side"] == "B"] == []
    assert "bundle tree changed during side" in report["sides"][0]["notes"]
    assert any("bundle tree changed during side" in note for note in report["rounds"][0]["notes"])


def test_trimmed_case_ids_refused(tmp_path):
    fakes_suites = _ab_fixtures()
    base = fakes_suites.ab_env(tmp_path / "base")
    required = list(base.m.case_ids)
    assert len(required) >= 2
    env = fakes_suites.ab_env(tmp_path / "trim", **{"case_ids": required[1:]})
    res = _run(tmp_path, env)
    assert res.reason_code == "HARNESS_CASE_SET_INCOMPLETE"
    assert _functest_calls(env) == []
    assert not [c for c in env.recorded() if c["kind"] in SIDE_KINDS]


def test_l3_od09_not_frozen_refuses_before_a(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, layer="l3")
    res = _run(tmp_path, env)
    assert res.verdict == "INFRA_ERROR"
    assert res.reason_code == "HARNESS_OD09_NOT_FROZEN"
    assert not [c for c in env.recorded() if c["kind"] in ("cli", "realhw", "testpilot", "functest")]


def test_l3_registers_candidate_before_b(tmp_path, monkeypatch):
    from realhw.acceptance import od09_freeze

    fakes_suites = _ab_fixtures()

    # 情境一：門檻已凍結、登記成功；B 側開跑前恰登記一次，複驗輪不重複登記。
    env = fakes_suites.ab_env(tmp_path / "ok", layer="l3", platform="linux", **{"reverify.enabled": True})
    _freeze_acceptance(env, tmp_path / "ok")
    registered: list[dict] = []

    def _record_register(acceptance_path, *, item_id, platform, run_id, started_at, ledger_path=None):
        registered.append({"item_id": item_id, "platform": platform, "run_id": run_id,
                           "calls_before": len(env.recorded())})
        return {}

    monkeypatch.setattr(od09_freeze, "register_candidate_start", _record_register)
    _run(tmp_path / "ok", env, None, _late_clock())
    assert len(registered) == 1
    assert registered[0]["item_id"] == "S3-L3-AB-48H-LINUX"
    assert registered[0]["platform"] == "linux"
    assert registered[0]["run_id"] == env.m.run_id
    b_positions = [i for i, c in enumerate(env.recorded()) if c["side"] == "B"]
    assert b_positions and b_positions[0] >= registered[0]["calls_before"]

    # 情境二：登記拋出 Od09Error；B 側完全不執行。
    env2 = fakes_suites.ab_env(tmp_path / "refuse", layer="l3", platform="linux")
    _freeze_acceptance(env2, tmp_path / "refuse")

    def _refuse_register(acceptance_path, *, item_id, platform, run_id, started_at, ledger_path=None):
        raise od09_freeze.Od09Error("HARNESS_OD09_NOT_FROZEN", ["forced"])

    monkeypatch.setattr(od09_freeze, "register_candidate_start", _refuse_register)
    res2 = _run(tmp_path / "refuse", env2, None, _late_clock())
    assert res2.verdict == "INFRA_ERROR"
    assert res2.reason_code == "HARNESS_OD09_NOT_FROZEN"
    assert [c for c in env2.recorded() if c["side"] == "B"] == []

    # ValueError（例如 run_id 已登記）是 manifest／登記輸入錯誤，不得誤報成未凍結。
    env3 = fakes_suites.ab_env(tmp_path / "invalid", layer="l3", platform="linux")
    _freeze_acceptance(env3, tmp_path / "invalid")

    def _invalid_register(*args, **kwargs):
        raise ValueError("run_id 已登記：duplicate")

    monkeypatch.setattr(od09_freeze, "register_candidate_start", _invalid_register)
    res3 = _run(tmp_path / "invalid", env3, None, _late_clock())
    assert res3.verdict == "INFRA_ERROR"
    assert res3.reason_code == "HARNESS_MANIFEST_INVALID"
    assert [c for c in env3.recorded() if c.get("side") == "B"] == []


def test_l3_supervisor_present_refused(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, layer="l3")
    _freeze_acceptance(env, tmp_path)
    # 監管模式本身合法（on-demand），只有 service status 回報已註冊監管，才能單獨觸發本測試的拒絕條件。
    _script(env, "cli:supervision-mode", _rc(0, "on-demand\n"))
    _script(env, "cli:service status", _rc(0, _json({"ok": True, "registered": True,
                                                       "units": ["serialwrap.service"]})))
    res = _run(tmp_path, env, None, _late_clock())
    assert res.reason_code == "HARNESS_FAILURE_RETENTION_UNVERIFIED"
    assert not [c for c in env.recorded() if c["kind"] == "realhw"]
    # A 側即使監管檢查失敗，仍須完成停止與釋放驗證。
    assert res.sides[0].release is not None


def test_dut_not_clean_aborts_before_suite(tmp_path):
    env = _ab_fixtures().ab_env(
        tmp_path,
        layer="l2",
        **{"dut.boards": ["COM0", "COM1"], "authorization.boards": ["COM0", "COM1"]},
    )
    _script(env, "cli:session list", _rc(0, _json({"ok": True, "sessions": [
        {"com": "COM0", "state": "READY", "bridge_generation": 1},
        {"com": "COM1", "state": "ATTACHED", "bridge_generation": 1},
    ]})))
    res = _run(tmp_path, env)
    assert res.reason_code == "HARNESS_DUT_NOT_CLEAN"
    assert not [c for c in env.recorded() if c["kind"] == "testpilot"]


# ---------------------------------------------------------------------------
# 結果分類、報告與中斷處理（plan Task 6 Step 1 表格）
# ---------------------------------------------------------------------------


def test_suite_rc_unknown_is_infra(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, a_functest_rc=5)
    res = _run(tmp_path, env)
    assert res.sides[0].suite.status == "INFRA_ERROR"
    assert res.verdict == "INFRA_ERROR"


def test_out_dir_exists_refused(tmp_path):
    from tools.stage3.reason_codes import HarnessError

    env = _ab_fixtures().ab_env(tmp_path)
    (tmp_path / "out").mkdir()
    with pytest.raises(HarnessError) as ei:
        _run(tmp_path, env)
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"
    # 步驟 1 先於任何 manifest 驗證與命令執行。
    assert env.recorded() == []


def test_side_env_propagated(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path)
    _run(tmp_path, env)
    side_cmds = [c for c in env.recorded() if c["kind"] in SIDE_KINDS]
    assert side_cmds
    assert all(c["side"] is not None for c in side_cmds)
    assert all(c["round"] == "1" for c in side_cmds if c["side"] == "B")


def test_report_records_identity(tmp_path):
    from tools.stage3 import ab_manifest

    env = _ab_fixtures().ab_env(tmp_path)
    res = _run(tmp_path, env)
    report = json.loads(res.report_path.read_text(encoding="utf-8"))
    side_b = report["sides"][1]
    assert side_b["release_manifest_sha256"] == env.m.sides[1].release_manifest.sha256
    for key in ("cli_sha256", "daemon_sha256", "runtime_digest"):
        assert key in side_b
    for key in ("tool_revision", "tools_stage3_tree_sha256"):
        assert key in report
    assert "transport" in report["dut"]
    assert side_b["bundle_tree_sha256"] == ab_manifest.bundle_tree_sha256(env.m.sides[1].bundle_dir)
    assert side_b["runtime_tree_sha256"] == ab_manifest.runtime_tree_sha256(env.m.sides[1].bundle_dir)


def test_l2_ab_reads_and_compares_complete_testpilot_reports(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, layer="l2")
    res = _run(tmp_path, env)

    assert res.verdict == "PASS", res.rounds
    assert res.rounds[0].diff["missing"] == {"a": [], "b": []}
    assert res.rounds[0].diff["integrity"] == {"a": True, "b": True}


def test_l2_candidate_report_failure_is_preserved(tmp_path):
    env = _ab_fixtures().ab_env(tmp_path, layer="l2")
    env.knobs.testpilot_verdicts["B"] = "FAIL"
    env.runner.script["testpilot:"] = [_rc(0), _rc(1)]
    res = _run(tmp_path, env)

    assert res.verdict == "FAIL"
    assert res.rounds[0].diff["b_failures"]


@pytest.mark.parametrize("contents", ["{broken", "[]"])
def test_l2_json_reader_rejects_invalid_report_shapes(tmp_path, contents):
    from tools.stage3.ab_runner import _read_json_object
    from tools.stage3.reason_codes import HarnessError

    report = tmp_path / "report.json"
    report.write_text(contents, encoding="utf-8")
    with pytest.raises(HarnessError) as exc_info:
        _read_json_object(report)
    assert exc_info.value.code == "HARNESS_COMPARATOR_ERROR"


def test_l3_gate_reports_are_compared_on_pass_fail_and_invalid_baseline(tmp_path, monkeypatch):
    from realhw.acceptance import od09_freeze

    def _register(*args, **kwargs):
        return {}

    monkeypatch.setattr(od09_freeze, "register_candidate_start", _register)

    passed = _ab_fixtures().ab_env(tmp_path / "pass", layer="l3")
    _freeze_acceptance(passed, tmp_path / "pass")
    pass_result = _run(tmp_path / "pass", passed, None, _late_clock())
    assert pass_result.verdict == "PASS"
    assert pass_result.rounds[0].diff["a_mode"] == "baseline"
    assert pass_result.rounds[0].diff["b_mode"] == "candidate"
    assert pass_result.rounds[0].diff["b_baseline_compatible"] is True

    failed = _ab_fixtures().ab_env(tmp_path / "fail", layer="l3")
    _freeze_acceptance(failed, tmp_path / "fail")
    failed.knobs.candidate_gate_result = "FAIL"
    failed.knobs.candidate_engine_verdict = "FAIL"
    failed.runner.script["realhw.acceptance.gate:"] = [_rc(0), _rc(1)]
    fail_result = _run(tmp_path / "fail", failed, None, _late_clock())
    assert fail_result.verdict == "FAIL"
    assert fail_result.rounds[0].diff["b_release_gate_result"] == "FAIL"

    invalid = _ab_fixtures().ab_env(tmp_path / "invalid", layer="l3")
    _freeze_acceptance(invalid, tmp_path / "invalid")
    invalid.knobs.baseline_result = "FAIL"
    invalid_result = _run(tmp_path / "invalid", invalid, None, _late_clock())
    assert invalid_result.verdict == "BASELINE_INVALID"
    assert invalid_result.reason_code == "HARNESS_BASELINE_INVALID"


def test_keyboard_interrupt_still_releases(tmp_path):
    fakes = _stage3_fakes()
    env = _ab_fixtures().ab_env(tmp_path)
    env.runner.raise_on("functest:", KeyboardInterrupt())
    probe = fakes.FakeProbe()
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, env, probe, fakes.FakeClock())
    report = json.loads((tmp_path / "out" / "ab-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "ABORTED"
    assert report["reason_code"] == "HARNESS_ABORTED"
    # A 側的釋放驗證已執行（中斷後仍完成停止與釋放確認）。
    assert isinstance(report["sides"][0]["release"]["released"], bool)
    assert probe.calls


def test_keyboard_interrupt_during_deactivation_still_releases_and_cleans(tmp_path, monkeypatch):
    fakes = _stage3_fakes()
    from tools.stage3 import dut_state

    env = _ab_fixtures().ab_env(tmp_path, layer="l2")
    interrupt = {"attempts": 0, "rollback_completed": False}
    cleanup_calls = []
    real_cleanup = dut_state.cleanup_instance

    def _interrupt_deactivation(argv, call_env):
        if "--rollback" in argv:
            interrupt["attempts"] += 1
            # 模擬 rollback 已執行，只在回傳結果前收到中斷；副作用命令不得自動重送。
            interrupt["rollback_completed"] = True
            raise KeyboardInterrupt()

    def _track_cleanup(instance):
        cleanup_calls.append(instance.side)
        return real_cleanup(instance)

    env.runner.on("cli:setup", _interrupt_deactivation)
    monkeypatch.setattr(dut_state, "cleanup_instance", _track_cleanup)
    probe = fakes.FakeProbe()
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, env, probe, fakes.FakeClock())

    report = json.loads((tmp_path / "out" / "ab-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "ABORTED"
    assert report["sides"][0]["release"]["released"] is True
    assert interrupt["attempts"] == 1
    assert interrupt["rollback_completed"] is True
    assert cleanup_calls == ["A"]
    assert probe.calls
    assert not [call for call in env.recorded() if call.get("side") == "B"]


def test_missing_endpoints_and_suite_notes_reach_side_report(tmp_path, monkeypatch):
    from dataclasses import replace
    from tools.stage3 import suites

    env = _ab_fixtures().ab_env(tmp_path)
    real_run_side = suites.run_side

    def _drop_endpoints_and_add_note(*args, **kwargs):
        result = real_run_side(*args, **kwargs)
        inst = args[2]
        (inst.report_dir / suites.REPORT_ENDPOINTS).unlink()
        return replace(result, notes=("suite diagnostic retained",))

    monkeypatch.setattr(suites, "run_side", _drop_endpoints_and_add_note)
    res = _run(tmp_path, env)
    report = json.loads(res.report_path.read_text(encoding="utf-8"))

    assert "endpoints.json missing" in report["sides"][0]["notes"]
    assert "suite diagnostic retained" in report["sides"][0]["notes"]


def test_l15_rust_only_difference_is_attributed_as_absolute_only(tmp_path, monkeypatch):
    from tools.stage3 import ab_diff, ab_manifest, ab_runner, suites

    env = _ab_fixtures().ab_env(tmp_path, layer="l1.5")
    case_id = "sc15-console-ring-drop-oldest"
    side_by_label = {side.label: side for side in env.m.sides}
    effect = _ab_fixtures().l15_functest((case_id,), side_by_label, env.knobs)
    reports = {}
    for side in env.m.sides:
        report_dir = tmp_path / "reports" / side.label
        reports[side.label] = report_dir
        effect(
            ["func-test", "--report-dir", str(report_dir), "--case", case_id],
            {
                "SW_STAGE3_SIDE": side.label,
                "SERIALWRAP_CLI_EXE": str(side.cli_exe),
                "SERIALWRAP_DAEMON_EXE": str(side.daemon_exe),
                "SERIALWRAP_RUN_DIR": str(tmp_path / "instances" / side.label),
            },
        )

    a_run = reports["A"] / case_id / "bridge" / "1"
    b_run = reports["B"] / case_id / "bridge" / "1"
    for name in ("manifest.json", "comparison.json", "bridge-manifest.json", "wire.ndjson"):
        shutil.copyfile(a_run / name, b_run / name)
    for run_dir, value in ((a_run, "python"), (b_run, "rust")):
        record = {"argv": ["session", "list"], "rc": 0, "response": {"value": value},
                  "stdout": json.dumps({"value": value}, separators=(",", ":")), "stderr": ""}
        (run_dir / "cli.ndjson").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        ab_manifest,
        "required_cases",
        lambda manifest, *, repo_root: ({case_id}, {case_id: {"expected": "PASS", "invariants": []}}),
    )
    run_results = []
    for label in ("A", "B"):
        report_dir = reports[label]
        run_results.append(ab_runner.SideResult(
            label=label,
            role="baseline" if label == "A" else "candidate",
            round=1,
            suite=suites.SuiteRun("PASS", 0, report_dir, {}, ()),
            identity_violations=(),
            report_dir=report_dir,
            clean_violations=(),
            pre_stop=None,
            release=None,
            cleanup_violations=(),
            started_at="2026-10-23T00:00:00Z",
            finished_at="2026-10-23T00:01:00Z",
            abort_code=None,
        ))

    diff, verdict, _, _ = ab_runner._compare_round(env.m, run_results, env.repo, now=lambda: "2026-10-23T00:02:00Z")
    assert diff["differences"][0]["registered_change"] == "SC-15"
    assert verdict == "PASS"


def test_relative_out_dir_is_resolved_before_child_commands(tmp_path, monkeypatch):
    env = _ab_fixtures().ab_env(tmp_path / "repo")
    monkeypatch.chdir(tmp_path)
    relative_out = Path("relative-ab-output")
    res = _run(tmp_path, env, out_dir=relative_out)

    assert res.verdict == "PASS"
    assert res.report_path == (tmp_path / relative_out / "ab-report.json").resolve()
    assert res.report_path.is_file()
    assert not (env.repo / relative_out).exists()


def test_falsey_explicit_probe_is_not_replaced(tmp_path, monkeypatch):
    from tools.stage3 import ab_runner
    from tools.stage3 import release_check

    class FalseyProbe:
        def __bool__(self):
            return False

    env = _ab_fixtures().ab_env(tmp_path)
    probe = FalseyProbe()
    received = []

    def _unexpected_default_probe():
        raise AssertionError("explicit probe was replaced")

    def _stop_at_side_boundary(*args, **kwargs):
        received.append(kwargs["probe"])
        raise RuntimeError("stop after checking probe selection")

    monkeypatch.setattr(release_check, "default_probe", _unexpected_default_probe)
    monkeypatch.setattr(ab_runner, "_run_side", _stop_at_side_boundary)
    _run(tmp_path, env, probe)

    assert received == [probe]


def test_recorded_fail_survives_unexpected_error_in_later_round(tmp_path, monkeypatch):
    from tools.stage3 import ab_runner

    env = _ab_fixtures().ab_env(tmp_path, b_functest_rc=1, **{"reverify.enabled": True})
    real_run_side = ab_runner._run_side

    def _fail_second_round(manifest, label, round_number, *args, **kwargs):
        if round_number == 2:
            raise RuntimeError("unexpected second-round harness error")
        return real_run_side(manifest, label, round_number, *args, **kwargs)

    monkeypatch.setattr(ab_runner, "_run_side", _fail_second_round)
    res = _run(tmp_path, env)

    assert res.verdict == "FAIL"
    assert res.reason_code is None


def test_windows_com0com_allowlist_failure_maps_to_authorization():
    from tools.stage3.ab_runner import _initial_reason

    assert _initial_reason("windows com0com instance without test_device_allowlist") == "HARNESS_AUTHORIZATION_MISSING"
