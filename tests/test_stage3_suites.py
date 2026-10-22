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
import sys
from types import SimpleNamespace

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


def _flag_values(argv, flag):
    items = list(argv)
    return [items[i + 1] for i, value in enumerate(items[:-1]) if value == flag]


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
        from tools.stage3 import ab_manifest
        from tools.stage3.suites.l15_functest import _case_groups

        required_ids, case_rows = ab_manifest.required_cases(s.m, repo_root=s.root)
        groups, _ = _case_groups(required_ids, case_rows)
        expected_ids = {case_id for _, ids in groups for case_id in ids}
        assert len(calls) == 1, side.label
        call = calls[0]
        actual_ids = set(_flag_values(call["argv"], "--case"))
        actual_ids.update(_flag_values(call["argv"], "--scenario-case"))
        assert actual_ids == expected_ids, side.label
        assert call["env"]["SERIALWRAP_CLI_EXE"] == str(side.cli_exe)
        assert call["env"]["SERIALWRAP_DAEMON_EXE"] == str(side.daemon_exe)
        assert _flag(call["argv"], "--report-dir") == str(inst.report_dir)


def test_l15_identity_mismatch_detected(make):
    fs = _fs()
    s = make(layer="l1.5", knobs=fs.Knobs(python_default=True))  # 替身一律寫 Python 基線身分
    expected_b = s.expected(1)
    _, run_b = s.run(1)
    violations = _suites().verify_run_identity(s.m, s.m.sides[1], run_b, expected_trees=expected_b)
    assert "daemon sha256 mismatch" in violations


def test_l15_python_launcher_identity_matches_func_test_artifact_shape(make):
    func_test_root = Path(__file__).resolve().parents[1] / "func-test"
    if str(func_test_root) not in sys.path:
        sys.path.insert(0, str(func_test_root))
    from lib.artifact_identity import collect_artifacts

    s = make(layer="l1.5")
    side = s.m.sides[0]
    _, suite_run = s.run(0)
    artifacts = collect_artifacts([str(side.cli_exe)], [str(side.daemon_exe)], s.root)
    suite_run.identity["daemon"] = artifacts["daemon"]

    violations = _suites().verify_run_identity(
        s.m, side, suite_run, expected_trees=s.expected(0))

    assert "daemon implementation mismatch" not in violations
    assert "daemon sha256 mismatch" not in violations


def test_l15_docker_passes_digest_pinned_image(make):
    s = make(layer="l1.5", **{
        "dut.kind": "docker",
        "dut.image": "serialwrap/dut-sim",
        "dut.image_digest": "a" * 64,
    })

    _, suite_run = s.run(0)

    call = s.calls("functest", side="A")[0]
    assert _flag(call["argv"], "--dut-image") == f"serialwrap/dut-sim@sha256:{'a' * 64}"
    assert suite_run.status == "PASS"


def test_l15_windows_com_pair_uses_manifest_values(make, monkeypatch):
    from dataclasses import replace

    from tools.stage3 import ab_manifest

    s = make(layer="l1.5")
    pair = {"daemon_side": "COM20", "bridge_side": "COM21"}
    s.m = replace(s.m, platform="windows", dut=replace(
        s.m.dut, transport="com0com-pair", com_pair=pair))
    case_id = "windows-bridge-case"
    monkeypatch.setattr(ab_manifest, "required_cases", lambda manifest, *, repo_root: (
        {case_id}, {case_id: {"source_ref": "func-test/cases/windows-bridge-case.yaml"}}))
    s.m = replace(s.m, case_ids=(case_id,))

    _, suite_run = s.run(0)

    argv = s.calls("functest", side="A")[0]["argv"]
    assert _flag(argv, "--daemon-com") == "COM20"
    assert _flag(argv, "--bridge-com") == "COM21"
    assert suite_run.status == "PASS"


def test_l15_unsupported_case_source_is_infra(make, monkeypatch):
    from dataclasses import replace

    from tools.stage3 import ab_manifest

    s = make(layer="l1.5", platform="windows")
    case_id = "win-wal-av-rotate"
    monkeypatch.setattr(ab_manifest, "required_cases", lambda manifest, *, repo_root: (
        {case_id}, {case_id: {"source_ref": "docs/rust-refactor/plans/02-stage0-python-bc.md"}}))
    s.m = replace(s.m, platform="windows", case_ids=(case_id,), dut=replace(
        s.m.dut, transport="com0com-pair",
        com_pair={"daemon_side": "COM20", "bridge_side": "COM21"}))

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert any(f"unsupported L1.5 source_ref: {case_id}" in note for note in suite_run.notes), suite_run.notes
    assert s.calls("functest", side="A") == []


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
    expected = set()
    for cli_log in inst.report_dir.rglob("cli.ndjson"):
        for line in cli_log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            argv = record.get("argv", [])
            for index, token in enumerate(argv[:-1]):
                if token in {"--socket", "--endpoint"}:
                    expected.add(argv[index + 1])
    assert set(data["endpoints"]) == expected
    assert data["endpoints"]


def test_l15_missing_cli_log_is_infra(make):
    s = make(layer="l1.5")
    s.knobs.case_overrides = {s.m.case_ids[0]: {"omit_cli_log": True}}

    _, run = s.run(0)

    assert run.status == "INFRA_ERROR"
    assert any("missing L1.5 CLI evidence" in note for note in run.notes)


def test_l15_runs_manifest_case_ids_including_additional_selection(make, monkeypatch):
    from dataclasses import replace

    from tools.stage3 import ab_manifest

    s = make(layer="l1.5")
    required_ids, case_rows = ab_manifest.required_cases(s.m, repo_root=s.root)
    extra_id = "optional-l15-case"
    rows = dict(case_rows)
    rows[extra_id] = {
        "case_id": extra_id,
        "layer": "L1.5-local",
        "platforms": ["linux"],
        "source_ref": "func-test/cases/optional-l15-case.yaml",
    }
    monkeypatch.setattr(ab_manifest, "required_cases", lambda manifest, *, repo_root: (required_ids, rows))
    s.m = replace(s.m, case_ids=(*s.m.case_ids, extra_id))

    _, run = s.run(0)

    calls = s.calls("functest", side="A")
    assert run.status == "PASS"
    assert len(calls) == 1
    assert extra_id in _flag_values(calls[0]["argv"], "--case")


def test_l15_non_product_verdict_is_infra(make):
    s = make(layer="l1.5")
    case_id = s.m.case_ids[0]
    s.knobs.case_overrides = {case_id: {"verdict": "SKIP"}}
    s.runner.script["functest:"] = [_fs().rc_entry(1)]

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert any("non-product comparison verdict" in note for note in suite_run.notes)


# ---------------------------------------------------------------------------
# L2（l2_regression）
# ---------------------------------------------------------------------------


def test_l2_copies_new_tp_dir(make, tmp_path):
    reports = Path(os.environ["HOME"]) / "b-log" / "regression-reports"
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
    assert testbed["allow_destructive"] is s.m.authorization.allow_destructive
    assert testbed["authorized_boards"] == list(s.m.authorization.boards)

    expected = dict(template)
    expected["serialwrap_exe"] = str(s.m.sides[0].cli_exe)
    expected["bench_lock_path"] = str(Path(template["bench_lock_path"]).expanduser())
    expected["allow_destructive"] = s.m.authorization.allow_destructive
    expected["authorized_boards"] = list(s.m.authorization.boards)
    assert testbed == expected


def test_l2_missing_authorization_is_non_destructive(make):
    from dataclasses import replace

    s = make(layer="l2")
    s.m = replace(s.m, authorization=None)

    inst, run = s.run(0)

    testbed = yaml.safe_load((inst.root / "testbed.yaml").read_text(encoding="utf-8"))
    assert testbed["allow_destructive"] is False
    assert testbed["authorized_boards"] == []
    assert run.status == "PASS"


def test_l2_preserves_shared_home_and_expands_bench_lock(make):
    from dataclasses import replace

    s = make(layer="l2", platform="windows")
    s.m = replace(s.m, platform_settings={
        "bench_lock_path": "~/.local/state/serialwrap/bench.lock"})
    inst, _ = s.run(0)

    call = s.calls("testpilot", side="A")[0]
    assert call["env"].get("HOME") == os.environ.get("HOME")
    assert call["env"].get("USERPROFILE") == os.environ.get("USERPROFILE")
    testbed = yaml.safe_load((inst.root / "testbed.yaml").read_text(encoding="utf-8"))
    assert testbed["bench_lock_path"] == str(Path.home() / ".local/state/serialwrap/bench.lock")
    report_root = Path(os.environ["HOME"]) / "b-log" / "regression-reports"
    assert any(report_root.glob("tp-*"))


def test_l2_stale_plugin_authorization_is_infra(make):
    s = make(layer="l2")
    s.knobs.testpilot_authorization_override = {"allow_destructive": False, "authorized_boards": []}

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert any("TestPilot authorization mismatch" in note for note in suite_run.notes)


@pytest.mark.parametrize("daemon_live", [
    [],
    [{"pid": 4102, "image_path": "/wrong/serialwrapd", "image_sha256": "0" * 64,
      "in_install": True, "error": None}],
    [{"pid": 4102, "image_path": "/bundle/bin/serialwrapd", "image_sha256": "0" * 64,
      "in_install": False, "error": None}],
    [{"pid": 4102, "image_path": None, "image_sha256": None,
      "in_install": False, "error": "image_unavailable"}],
])
def test_l2_rust_live_daemon_identity_mismatch_detected(make, daemon_live):
    s = make(layer="l2")
    s.run(0)
    expected = s.expected(1)
    _, suite_run = s.run(1)
    suite_run.identity["deployed_content"]["daemon_live"] = daemon_live

    violations = _suites().verify_run_identity(
        s.m, s.m.sides[1], suite_run, expected_trees=expected)

    assert "daemon sha256 mismatch" in violations


def test_l2_rust_live_daemon_identity_matches_manifest(make):
    s = make(layer="l2")
    s.run(0)
    expected = s.expected(1)
    side = s.m.sides[1]
    _, suite_run = s.run(1)
    expected_hash = side.manifest_doc["components"]["daemon"]["launcher"]["sha256"]
    suite_run.identity["deployed_content"]["daemon_live"] = [{
        "pid": 4202,
        "image_path": str(side.daemon_exe),
        "image_sha256": expected_hash,
        "in_install": True,
        "error": None,
    }]

    violations = _suites().verify_run_identity(
        s.m, side, suite_run, expected_trees=expected)

    assert "daemon sha256 mismatch" not in violations


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
    realhw_a = s.calls("realhw", side="A")[0]
    assert _flag(realhw_a["argv"], "--duration") == "48h"
    gate_a = s.calls("realhw.acceptance.gate", side="A")[0]
    assert _flag(gate_a["argv"], "--case-dir") == str(inst_a.report_dir / "realhw" / "lr-mixed")
    observer = s.calls("realhw.acceptance.observer", side="A")[0]
    assert _flag(observer["argv"], "--supervision-attest") == str(inst_a.report_dir / "supervision-attest.json")
    assert observer["cwd"] == inst_a.report_dir
    assert observer["env"]["PYTHONPATH"].startswith(str(s.root))
    attestation = json.loads((inst_a.report_dir / "supervision-attest.json").read_text(encoding="utf-8"))
    assert attestation["auto_restart_disabled"] is True
    assert attestation["evidence"]["service_status"]["response"]["error_code"] == "NO_SYSTEMD"
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
    assert _flag(obs["argv"], "--supervision-attest") == str(inst.report_dir / "supervision-attest.json")
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
    observer = s.calls("realhw.acceptance.observer", side="A")[0]
    assert _flag(observer["argv"], "--endpoint") == s.m.endpoint
    for call in s.calls("realhw.acceptance.winexcl", side="A"):
        assert _flag(call["argv"], "--endpoint") == s.m.endpoint


def test_l3_supervisor_present_refused(make):
    s = make(layer="l3", platform="linux", script={"cli:service status": [REGISTERED]})
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"
    assert any("HARNESS_FAILURE_RETENTION_UNVERIFIED" in note for note in run.notes)
    assert s.calls("realhw", side="A") == []


def test_l3_non_on_demand_supervision_mode_is_refused(make):
    s = make(layer="l3", platform="linux", script={
        "cli:supervision-mode": [_fs()._ok("systemd-user\n")],
    })

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert any("HARNESS_FAILURE_RETENTION_UNVERIFIED" in note for note in suite_run.notes)
    assert s.calls("realhw", side="A") == []


def test_l3_no_systemd_requires_platform_query(make):
    # 替身分類會略過 `--user` 旗標，system 與 user 兩次查詢共用 `systemctl:list-unit-files` 鍵；
    # 依呼叫順序（先 system 後 user）讓 user 查詢失敗。
    script = {"systemctl:list-unit-files": [_fs()._ok(""), {"rc": 1, "stdout": "", "stderr": "bus unavailable"}]}
    s = make(layer="l3", platform="linux", script=script)
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"
    assert any("HARNESS_FAILURE_RETENTION_UNVERIFIED" in note for note in run.notes)
    assert s.calls("realhw", side="A") == []


def test_l3_systemd_not_booted_is_not_a_supervisor(make):
    result = {"rc": 1, "stdout": "", "stderr": "System has not been booted with systemd as init system."}
    s = make(layer="l3", platform="linux", script={"systemctl:list-unit-files": [result]})

    _, suite_run = s.run(0)

    assert suite_run.status == "PASS"
    assert s.calls("realhw", side="A")


def test_l3_system_without_systemd_still_checks_user_units(make):
    system = {"rc": 1, "stdout": "", "stderr": "System has not been booted with systemd as init system."}
    user = _fs()._ok("serialwrapd.service enabled enabled\n")
    s = make(layer="l3", platform="linux", script={"systemctl:list-unit-files": [system, user]})

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert any("registered supervisor" in note for note in suite_run.notes)
    assert s.calls("realhw", side="A") == []


def test_l3_disabled_serialwrap_unit_is_not_registered(make):
    s = make(layer="l3", platform="linux", script={
        "systemctl:list-unit-files": [_fs()._ok("serialwrapd.service disabled disabled\n")],
    })

    _, suite_run = s.run(0)

    assert suite_run.status == "PASS"


def test_l3_platform_unit_query_overrides_no_systemd_status(make):
    script = {"systemctl:list-unit-files": [_fs()._ok("serialwrapd.service enabled enabled\n")]}
    s = make(layer="l3", platform="linux", script=script)
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"
    assert any("HARNESS_FAILURE_RETENTION_UNVERIFIED" in note for note in run.notes)
    assert s.calls("realhw", side="A") == []


def test_l3_windows_task_query_filters_full_csv(make):
    script = {"schtasks:": [_fs()._ok('"\\serialwrapd\\restart","N/A","Ready"\n')]}
    s = make(layer="l3", platform="windows", script=script)
    _, run = s.run(0)
    assert run.status == "INFRA_ERROR"
    call = s.calls("schtasks", side="A")[0]
    assert _flag(call["argv"], "/FO") == "CSV"
    assert _flag(call["argv"], "/NH") is None
    assert s.calls("realhw", side="A") == []


@pytest.mark.parametrize(("verdict", "expected"), [
    ("PASS", "PASS"),
    ("FAIL", "FAIL"),
    ("INSUFFICIENT_EVIDENCE", "INFRA_ERROR"),
    ("BLOCKED", "INFRA_ERROR"),
])
def test_l3_candidate_status_follows_gate_verdict(make, verdict, expected):
    knobs = _fs().Knobs(candidate_gate_result=verdict)
    exit_code = {"PASS": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 4, "BLOCKED": 5}[verdict]
    s = make(layer="l3", platform="linux", knobs=knobs, script={
        "realhw.acceptance.gate:": [_fs().rc_entry(0), _fs().rc_entry(exit_code)],
    })
    s.run(0)
    _, run = s.run(1)
    assert run.status == expected


def test_l3_baseline_not_applicable_is_pass(make):
    s = make(layer="l3", platform="linux")
    _, run = s.run(0)
    assert run.status == "PASS"


def test_l3_candidate_not_applicable_baseline_is_infra(make):
    knobs = _fs().Knobs(candidate_gate_result="NOT_APPLICABLE_BASELINE")
    s = make(layer="l3", platform="linux", knobs=knobs)
    s.run(0)

    _, suite_run = s.run(1)

    assert suite_run.status == "INFRA_ERROR"


def test_l3_gate_exit_code_must_match_report_verdict(make):
    knobs = _fs().Knobs(candidate_gate_result="FAIL")
    s = make(layer="l3", platform="linux", knobs=knobs, script={
        "realhw.acceptance.gate:": [_fs().rc_entry(0), _fs().rc_entry(0)],
    })
    s.run(0)

    _, suite_run = s.run(1)

    assert suite_run.status == "INFRA_ERROR"
    assert any("gate rc/verdict mismatch" in note for note in suite_run.notes)


def test_l3_candidate_without_baseline_gate_refuses_longrun(make):
    s = make(layer="l3", platform="linux")

    _, suite_run = s.run(1)

    assert suite_run.status == "INFRA_ERROR"
    assert s.calls("realhw", side="B") == []
    assert s.calls("realhw.acceptance.observer", side="B") == []


def test_l3_longrun_timeout_must_cover_minimum_duration(make):
    from dataclasses import replace

    s = make(layer="l3", platform="linux")
    s.m = replace(s.m, suite=replace(s.m.suite, timeout_s=3600))

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert s.calls("realhw", side="A") == []
    assert s.calls("realhw.acceptance.observer", side="A") == []


def test_l3_non_longrun_without_report_is_infra(make):
    from dataclasses import replace

    knobs = _fs().Knobs(omit_realhw_report=True)
    s = make(layer="l3", platform="linux", knobs=knobs)
    s.m = replace(s.m, suite=replace(s.m.suite, tier="p0,p1,remote"))

    _, suite_run = s.run(0)

    assert suite_run.status == "INFRA_ERROR"
    assert any("realhw report is missing or invalid" in note for note in suite_run.notes)


def test_l3_wal_copy_keeps_only_serialwrap_logs_and_rejects_nested_report(tmp_path):
    from tools.stage3.suites.l3_realhw import _copy_wal

    source = tmp_path / "wal-source"
    source.mkdir()
    for name in ("raw.wal.ndjson", "raw.wal.ndjson.1", "raw.mirror.log", "unrelated.bin"):
        (source / name).write_text(name, encoding="utf-8")

    copied = _copy_wal(source, tmp_path / "report")

    assert copied is not None
    assert sorted(path.name for path in copied.iterdir()) == [
        "raw.mirror.log", "raw.wal.ndjson", "raw.wal.ndjson.1"]
    with pytest.raises(ValueError, match="report directory is inside WAL source"):
        _copy_wal(source, source / "report")


def test_l3_windows_handoff_waits_for_delayed_observer_header(make):
    import threading
    import time

    s = make(layer="l3", platform="windows")
    original_spawn = s.runner.spawn
    threads = []

    def delayed_spawn(argv, *, env=None, cwd=None):
        proc = original_spawn(argv, env=env, cwd=cwd)
        if "realhw.acceptance.observer" in argv:
            header_path = Path(_flag(argv, "--out"))
            header_path.unlink(missing_ok=True)
            def write_header():
                time.sleep(0.03)
                header_path.parent.mkdir(parents=True, exist_ok=True)
                header_path.write_text(json.dumps({
                    "kind": "header", "device_binding": {"TEST0": "COM20", "TEST1": "COM21"}
                }) + "\n", encoding="utf-8")
            thread = threading.Thread(target=write_header)
            thread.start()
            threads.append(thread)
        return proc

    def require_header_before_handoff(argv, *, env=None, timeout=None, cwd=None):
        if "realhw.acceptance.winexcl" in argv and _flag(argv, "--phase") == "pre":
            header = Path(_flag(argv, "--observer-header"))
            assert header.is_file()
            row = json.loads(header.read_text(encoding="utf-8").splitlines()[0])
            assert row.get("device_binding") == {"TEST0": "COM20", "TEST1": "COM21"}
        return s.runner(argv, env=env, timeout=timeout, cwd=cwd)

    inst = s.instance(0)
    from tools.stage3 import suites
    suite_run = suites.run_side(s.m, s.m.sides[0], inst, run=require_header_before_handoff,
                                spawn=delayed_spawn, now=lambda: _fs().NOW, repo_root=s.root)
    for thread in threads:
        thread.join(timeout=1)

    assert suite_run.status == "PASS", suite_run.notes
    pre = next(call for call in s.calls("realhw.acceptance.winexcl", side="A")
               if _flag(call["argv"], "--phase") == "pre")
    header = Path(_flag(pre["argv"], "--observer-header"))
    assert header.is_file()
    assert suite_run.status == "PASS"


def test_l3_observer_exit_before_header_is_infra(make):
    class ExitedObserver:
        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            return None

        def kill(self):
            return None

    s = make(layer="l3", platform="windows")
    original_spawn = s.runner.spawn

    def exited_spawn(argv, *, env=None, cwd=None):
        if "realhw.acceptance.observer" in argv:
            proc = original_spawn(argv, env=env, cwd=cwd)
            Path(_flag(argv, "--out")).unlink(missing_ok=True)
            return ExitedObserver()
        return original_spawn(argv, env=env, cwd=cwd)

    inst = s.instance(0)
    from tools.stage3 import suites
    suite_run = suites.run_side(s.m, s.m.sides[0], inst, run=s.runner,
                                spawn=exited_spawn, now=lambda: _fs().NOW, repo_root=s.root)

    assert suite_run.status == "INFRA_ERROR"
    assert any("observer exited or header was not ready" in note for note in suite_run.notes)
    assert s.calls("realhw", side="A") == []
    assert s.calls("realhw.acceptance.winexcl", side="A") == []


def test_l3_reverify_uses_latest_existing_baseline_gate(make):
    s = make(layer="l3", platform="linux")
    s.run(0, round=1)
    _, run = s.run(1, round=2)
    gate = next(call for call in s.calls("realhw.acceptance.gate", side="B")
                if call.get("round") == "2")
    assert _flag(gate["argv"], "--baseline-gate") == str(
        s.root / "out" / "r1-A" / "report" / "release-gate.json")
    assert run.status == "PASS"


def test_l3_observer_stops_when_engine_is_interrupted(make):
    s = make(layer="l3", platform="linux")
    s.runner.raise_on("realhw:", KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        s.run(0)
    assert any(call["kind"] == "proc" and call["sub"] == "terminate"
               for call in s.runner.calls)


def test_l3_unpinned_cli_mismatch(make):
    s = make(layer="l3", knobs=_fs().Knobs(pinned=False))
    expected = s.expected(0)
    _, run = s.run(0)
    violations = _suites().verify_run_identity(s.m, s.m.sides[0], run, expected_trees=expected)
    assert "cli not pinned" in violations


def test_l3_live_daemon_image_mismatch_detected(make):
    s = make(layer="l3", platform="linux")
    s.run(0)
    expected = s.expected(1)
    _, run = s.run(1)
    daemon_identity = run.identity["daemon"]
    assert daemon_identity["image_sha256"]
    daemon_identity["image_sha256"] = "0" * 64

    violations = _suites().verify_run_identity(
        s.m, s.m.sides[1], run, expected_trees=expected)

    assert "daemon sha256 mismatch" in violations


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
    if layer == "l3":
        control.run(0)
    expected_c = control.expected(1)
    _, run_c = control.run(1)
    assert suites.verify_run_identity(control.m, control.m.sides[1], run_c, expected_trees=expected_c) == []

    # 旁載：引擎實際執行的安裝目錄 <inst.root>/versions/v 多一個 bin/version.dll；
    # manifest 與 bin/serialwrap 位元組不變，故 cli sha256 與 release manifest 皆相符
    sideload_root = tmp_path / "side" / "out" / "r1-B"
    knobs = fs.Knobs(sideload_root=sideload_root)
    s = make("side", layer=layer, knobs=knobs)
    if layer == "l3":
        s.run(0)
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
    if layer == "l3":
        s.run(0)
    if mode == "changed":
        # suite 期間有人在實際部署樹加檔：只在 B 側 start／end 量測之間寫入新檔
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


def test_l15_reboot_scenario_string_action_is_detected(tmp_path):
    from tools.stage3.suites.l15_functest import _case_restarts_dut

    scenario = tmp_path / "reboot.yaml"
    scenario.write_text("steps:\n  - action: dut_control\n    action_name: logical_reboot\n", encoding="utf-8")
    assert _case_restarts_dut({"source_ref": str(scenario)}, tmp_path) is True


def test_l15_markdown_source_ref_is_not_parsed_as_yaml(tmp_path):
    from tools.stage3.suites.l15_functest import _case_restarts_dut

    source = tmp_path / "plan.md"
    source.write_text("steps: [this is not yaml: [", encoding="utf-8")
    assert _case_restarts_dut({"source_ref": str(source)}, tmp_path) is False


@pytest.mark.parametrize("case_id", ["l15-dut-reboot-port-alive", "l15-reboot-min-guard"])
def test_l15_real_reboot_scenarios_declare_restart(case_id):
    import yaml
    from tools.stage3.suites.l15_functest import _case_restarts_dut

    cases = {row["case_id"]: row for row in yaml.safe_load(
        (Path(__file__).parent / "acceptance" / "cases.yaml").read_text(encoding="utf-8"))["cases"]}
    assert _case_restarts_dut(cases[case_id], Path(__file__).resolve().parents[1]) is True


def test_l15_case_groups_separate_cases_scenarios_and_unsupported():
    from tools.stage3.suites.l15_functest import _case_groups

    groups, unsupported = _case_groups(
        ["case", "scenario", "plan"],
        {
            "case": {"source_ref": "func-test/cases/case.yaml"},
            "scenario": {"source_ref": "func-test/scenarios/scenario.yml"},
            "plan": {"source_ref": "docs/rust-refactor/plan.md"},
        },
    )
    assert groups == [("cases", ["case"]), ("scenarios", ["scenario"])]
    assert unsupported == ["plan"]


@pytest.mark.parametrize("value", ["/tmp/serialwrap.sock", "unix:///tmp/serialwrap.sock",
                                    "tcp://127.0.0.1:48700"])
def test_l15_classifies_probeable_endpoints(value):
    from tools.stage3.suites.l15_functest import _classifiable_endpoint

    assert _classifiable_endpoint(value) is True


@pytest.mark.parametrize("value", ["uart0", "COM0", "", None])
def test_l15_ignores_logical_endpoint_ids(value):
    from tools.stage3.suites.l15_functest import _classifiable_endpoint

    assert _classifiable_endpoint(value) is False


def test_verify_run_identity_checks_daemon_version_and_pinned_path():
    from tools.stage3.suites import SuiteRun, verify_run_identity

    m = SimpleNamespace(layer="l2")
    side = SimpleNamespace(
        manifest_doc={"components": {"cli": {"sha256": "c" * 64},
                                     "daemon": {"implementation": "rust", "version": "1.2.3"}}},
        release_manifest=SimpleNamespace(sha256="m" * 64),
        cli_exe=Path("/bundle/bin/serialwrap"),
    )
    suite_run = SuiteRun(
        "PASS", 0, Path("/report"),
        {"cli": {"sha256": "c" * 64, "pinned": True, "resolved": "/wrong/serialwrap"},
         "daemon": {"implementation": "rust"}, "daemon_version": "1.2.2"},
        (),
    )
    violations = verify_run_identity(
        m, side, suite_run,
        expected_trees={"bundle_tree_sha256": "b" * 64, "runtime_tree_sha256": "r" * 64},
    )
    assert "daemon version mismatch" in violations
    assert "cli not pinned" in violations
