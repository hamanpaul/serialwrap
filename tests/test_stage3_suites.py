"""Task 5B（suite adapter，`tools/stage3/suites/`）的紅燈測試。

本檔在 TDD 第一輪撰寫，實作尚不存在；所有 `tools.stage3` 與 `stage3_fakes`／`fakes_suites` 的 import
一律延遲到測試函式內，確保 `python3 -m pytest -q --collect-only tests/` 仍可完成收集。

引擎以 `fakes_suites.EngineRunner`（`RecordingRunner` 子類）替身化：引擎輸出由 `on(prefix, effect)` 產生，
報告內的部署內容量測一律呼叫真實 P2 `realhw.deployed_content.measure`。所有 TestPilot 寫入都被
autouse fixture 導向暫存 HOME，避免污染真實 `~/b-log`。

marker（P0 T14）：`core_portable` + `memory_only`，模組層級。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

# 測試中用到的 CmdResult 形狀與 RecordingRunner 腳本相同：{"rc", "stdout", "stderr"}
REGISTERED = {"rc": 0, "stderr": "", "stdout": json.dumps(
    {"ok": True, "mode": "systemd-user", "registered": True, "units": ["serialwrapd.service"]}, sort_keys=True)}


def _fs():
    import fakes_suites

    return fakes_suites


def _s3():
    import stage3_fakes

    return stage3_fakes


def _suites():
    from tools.stage3 import suites

    return suites


def _flag(argv, flag):
    items = list(argv)
    if flag not in items:
        return None
    i = items.index(flag)
    return items[i + 1] if i + 1 < len(items) else None


def _label(call):
    """把一筆呼叫對應到 L3 程序順序的標籤；與本次順序無關的呼叫回 None。"""
    kind, sub, argv = call["kind"], call["sub"], call["argv"]
    if kind == "cli" and sub == "supervision-mode":
        return "supervision"
    if kind == "cli" and sub == "service status":
        return "service"
    if kind == "realhw.acceptance.observer":
        return "observer"
    if kind == "proc" and sub == "terminate" and "realhw.acceptance.observer" in argv:
        return "observer-stop"
    if kind == "realhw.acceptance.winexcl":
        return f"winexcl-{_flag(argv, '--phase')}"
    if kind == "realhw":
        return "realhw"
    if kind == "realhw.acceptance.gate":
        return "gate"
    return None


def _sequence(calls):
    return [lab for lab in (_label(c) for c in calls) if lab is not None]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """TestPilot 替身寫入 ~/b-log；全部測試統一導向暫存 HOME。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def make(tmp_path):
    made = []

    def _make(name="s", **kw):
        env = _fs().make_suite(tmp_path / name, **kw)
        made.append(env)
        return env

    yield _make
    for env in made:
        env.cleanup()


# ---------------------------------------------------------------------------
# L1.5（l15_functest）
# ---------------------------------------------------------------------------


def test_l15_passes_artifact_env_and_report_dir(make):
    s = make(layer="l1.5")
    for idx, side in enumerate(s.m.sides):
        inst, _ = s.run(idx)
        calls = s.calls("functest", side=side.label)
        assert len(calls) == 1, side.label
        call = calls[0]
        assert call["env"]["SERIALWRAP_CLI_EXE"] == str(side.cli_exe)
        assert call["env"]["SERIALWRAP_DAEMON_EXE"] == str(side.daemon_exe)
        assert _flag(call["argv"], "--report-dir") == str(inst.report_dir)


def test_l15_identity_mismatch_detected(make):
    fs = _fs()
    s = make(layer="l1.5", knobs=fs.Knobs(python_default=True))  # 替身一律寫 Python 基線身分
    expected_b = s.expected(1)
    _, run_b = s.run(1)
    violations = _suites().verify_run_identity(s.m, s.m.sides[1], run_b, expected_trees=expected_b)
    assert "daemon implementation mismatch" in violations


def test_l15_unclean_bridge_is_infra(make):
    s = make(layer="l1.5")
    s.knobs.case_overrides = {s.m.case_ids[0]: {"closed_cleanly": False}}
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"


def test_l15_reused_dut_is_infra(make):
    s = make(layer="l1.5")
    # wire.ndjson 出現 dut_generation=2：同一 DUT 被重用於第二次 case 執行（該 case 未宣告 restart）
    s.knobs.case_overrides = {s.m.case_ids[0]: {"dut_generation": 2}}
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"


def test_l15_leaked_daemon_is_infra(make):
    s = make(layer="l1.5")
    side = s.m.sides[0]
    probe = _s3().FakeProbe(by_exe={str(Path(side.daemon_exe).resolve()): [(91,)]})
    _, run = s.run(0, probe=probe)
    assert run.status == "INFRA_ERROR"
    assert any("leaked daemon" in note for note in run.notes)


def test_l15_writes_endpoints_json(make):
    s = make(layer="l1.5")
    inst, _ = s.run(0)
    data = json.loads((inst.report_dir / "endpoints.json").read_text(encoding="utf-8"))
    run_dir = inst.env["SERIALWRAP_RUN_DIR"]
    expected = {os.path.join(run_dir, f"{case}.sock") for case in s.m.case_ids}
    assert expected <= set(data["endpoints"])


# ---------------------------------------------------------------------------
# L2（l2_regression）
# ---------------------------------------------------------------------------


def test_l2_copies_new_tp_dir(make, tmp_path):
    reports = tmp_path / "home" / "b-log" / "regression-reports"
    (reports / "tp-0").mkdir(parents=True)
    (reports / "tp-0" / "report.json").write_text(json.dumps({"marker": "old"}), encoding="utf-8")
    s = make(layer="l2")
    inst, _ = s.run(0)
    copied = inst.report_dir / "testpilot"
    assert json.loads((copied / "report.json").read_text(encoding="utf-8"))["marker"] == "new"
    assert not (copied / "tp-0").exists()
    assert not any(json.loads(p.read_text(encoding="utf-8")).get("marker") == "old"
                   for p in copied.rglob("*.json"))


def test_l2_ambiguous_tp_dir(make):
    s = make(layer="l2")
    s.knobs.testpilot_new_dirs = 2  # 單次執行新增兩個 tp-* 目錄
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"
    assert any("testpilot report dir ambiguous" in note for note in run.notes)


def test_l2_testbed_pins_side_exe(make):
    s = make(layer="l2")
    inst, _ = s.run(0)
    testbed = yaml.safe_load((inst.root / "testbed.yaml").read_text(encoding="utf-8"))
    template_path = Path(s.m.suite.testbed_template.path)
    if not template_path.is_absolute():
        template_path = s.root / template_path
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    assert testbed["serialwrap_exe"] == str(s.m.sides[0].cli_exe)

    def _rest(doc):
        return {k: v for k, v in doc.items() if k not in {"serialwrap_exe", "bench_lock_path"}}

    assert _rest(testbed) == _rest(template)


# ---------------------------------------------------------------------------
# L3（l3_realhw）
# ---------------------------------------------------------------------------


def test_l3_runs_observer_engine_wal_gate_in_order(make):
    s = make(layer="l3", platform="linux")
    inst_a, _ = s.run(0)
    inst_b, _ = s.run(1)
    expected = ["supervision", "service", "observer", "realhw", "observer-stop", "gate"]
    assert _sequence(s.calls(side="A")) == expected
    assert _sequence(s.calls(side="B")) == expected
    # B 側 gate 以 A 側 release-gate.json 作為基線
    gate_b = s.calls("realhw.acceptance.gate", side="B")[0]
    assert _flag(gate_b["argv"], "--baseline-gate") == str(inst_a.report_dir / "release-gate.json")
    # Linux 不得出現 Windows 獨占證據相關旗標或 winexcl 呼叫
    for call in s.runner.calls:
        assert not any(arg in {"--excl-dir", "--excl-evidence"} for arg in call["argv"]), call["kind"]
        assert not any("winexcl" in arg for arg in call["argv"]), call["kind"]
    assert inst_b.report_dir != inst_a.report_dir


def test_l3_windows_exclusive_evidence_steps(make):
    s = make(layer="l3", platform="windows")
    inst, run = s.run(0)
    expected = ["supervision", "service", "observer", "winexcl-pre", "realhw", "winexcl-post",
                "observer-stop", "gate"]
    assert _sequence(s.calls(side="A")) == expected
    assert s.calls("schtasks", side="A"), "Windows 需以 schtasks 確認無已註冊工作"
    obs = s.calls("realhw.acceptance.observer", side="A")[0]
    assert _flag(obs["argv"], "--excl-dir") == str(inst.report_dir / "excl")
    assert _flag(obs["argv"], "--excl-probe-interval-s") == "300"
    pre = [c for c in s.calls("realhw.acceptance.winexcl", side="A") if _flag(c["argv"], "--phase") == "pre"][0]
    assert _flag(pre["argv"], "--observer-header") == str(inst.report_dir / "observer.ndjson")
    assert _flag(pre["argv"], "--excl-dir") == str(inst.report_dir / "excl")
    gate = s.calls("realhw.acceptance.gate", side="A")[0]
    assert _flag(gate["argv"], "--excl-evidence") == str(inst.report_dir / "excl")


@pytest.mark.parametrize("pre_rc", [4, 1])
def test_l3_windows_exclusive_evidence_pre_failure(make, pre_rc):
    script = {"realhw.acceptance.winexcl:handoff pre": [{"rc": pre_rc, "stdout": "", "stderr": ""}]}
    s = make(layer="l3", platform="windows", script=script)
    _, run = s.run(0)
    # 不論 rc，pre 失敗時都不得開始長跑（realhw）
    assert s.calls("realhw", side="A") == []
    if pre_rc == 4:
        assert run.status == "INFRA_ERROR"
    else:
        # exit 1（violated）：證據照存並仍執行 gate，由 gate 判 safety_double_holder
        assert len(s.calls("realhw.acceptance.gate", side="A")) == 1


def test_l3_explicit_endpoint_everywhere(make):
    s = make(layer="l3", platform="linux")
    inst, _ = s.run(0)
    rpc = [c for c in s.runner.calls
           if c["kind"] == "cli" and c["sub"].split(" ")[0] in {"daemon", "session", "cmd"}]
    for call in rpc:
        argv = call["argv"]
        assert "--endpoint" in argv and argv[argv.index("--endpoint") + 1] == s.m.endpoint, call["sub"]
    cfg = json.loads((inst.root / "realhw-config.json").read_text(encoding="utf-8"))
    assert cfg["endpoint"] == s.m.endpoint


def test_l3_supervisor_present_refused(make):
    s = make(layer="l3", platform="linux", script={"cli:service status": [REGISTERED]})
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"
    assert any("HARNESS_FAILURE_RETENTION_UNVERIFIED" in note for note in run.notes)
    assert s.calls("realhw", side="A") == []


def test_l3_unpinned_cli_mismatch(make):
    s = make(layer="l3", knobs=_fs().Knobs(pinned=False))
    expected = s.expected(0)
    _, run = s.run(0)
    violations = _suites().verify_run_identity(s.m, s.m.sides[0], run, expected_trees=expected)
    assert "cli not pinned" in violations


def test_l3_p0p1_tier_skips_gate(make):
    s = make(layer="l3", platform="linux", **{"suite.tier": "p0,p1,remote"})
    s.run(0)
    kinds = {c["kind"] for c in s.runner.calls}
    assert "realhw.acceptance.observer" not in kinds
    assert "realhw.acceptance.gate" not in kinds
    assert not any(c["kind"] == "proc" for c in s.runner.calls)
    realhw_calls = s.calls("realhw", side="A")
    assert len(realhw_calls) == 1
    assert _flag(realhw_calls[0]["argv"], "--tier") == "p0,p1,remote"


# ---------------------------------------------------------------------------
# 已部署內容（R5-F4）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", ["l1.5", "l2", "l3"])
def test_suite_deployed_tree_sideload_detected(make, tmp_path, layer):
    fs = _fs()
    suites = _suites()
    # 對照組：未旁載，身分與實際部署樹皆相符
    control = make("control", layer=layer)
    expected_c = control.expected(1)
    _, run_c = control.run(1)
    assert suites.verify_run_identity(control.m, control.m.sides[1], run_c, expected_trees=expected_c) == []

    # 旁載：引擎實際執行的安裝目錄 <inst.root>/versions/v 多一個 bin/version.dll；
    # manifest 與 bin/serialwrap 位元組不變，故 cli sha256 與 release manifest 皆相符
    sideload_root = tmp_path / "side" / "out" / "r1-B"
    knobs = fs.Knobs(sideload_root=sideload_root)
    s = make("side", layer=layer, knobs=knobs)
    expected = s.expected(1)
    _, run = s.run(1)
    violations = suites.verify_run_identity(s.m, s.m.sides[1], run, expected_trees=expected)
    assert set(violations) == {"deployed tree mismatch"}


@pytest.mark.parametrize("layer", ["l1.5", "l2", "l3"])
@pytest.mark.parametrize("mode", ["omit", "unresolved", "end_none", "changed"])
def test_suite_deployed_content_missing_or_unstable(make, layer, mode):
    fs = _fs()
    suites = _suites()
    s = make(layer=layer)
    if mode == "changed":
        # suite 期間有人在實際部署樹加檔：每組 start／end 之間寫入新檔
        s.knobs.between = lambda idx: fs.add_late_file(s.m.sides[1].bundle_dir, idx)
    else:
        s.knobs.deployed = mode
    expected = s.expected(1)  # 量測前的 manifest 宣告指紋
    _, run = s.run(1)
    violations = suites.verify_run_identity(s.m, s.m.sides[1], run, expected_trees=expected)
    wanted = "deployed content changed during suite" if mode == "changed" else "deployed content unmeasured"
    assert wanted in violations
    if layer == "l1.5":
        # expected_trees 必填無預設：省略即 TypeError
        with pytest.raises(TypeError):
            suites.verify_run_identity(s.m, s.m.sides[1], run)
