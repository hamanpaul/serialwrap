# tests/test_functest_oracles.py
"""func-test oracle 修復、case YAML 結構與報告欄位測試。"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FUNC_TEST_DIR = REPO_ROOT / "func-test"
if str(FUNC_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(FUNC_TEST_DIR))


def test_mode_stats_separate():
    """驗證 runner 統計獨立案例數與不同模式（pty 與 bridge）的執行次數分開計算。"""
    import runner

    results = [
        {"case_id": "c1", "mode": "pty", "run_index": 0, "verdict": "PASS"},
        {"case_id": "c1", "mode": "pty", "run_index": 1, "verdict": "PASS"},
        {"case_id": "c2", "mode": "pty", "run_index": 0, "verdict": "PASS"},
        {"case_id": "c2", "mode": "pty", "run_index": 1, "verdict": "PASS"},
        {"case_id": "c1", "mode": "bridge", "run_index": 0, "verdict": "PASS"},
        {"case_id": "c1", "mode": "bridge", "run_index": 1, "verdict": "PASS"},
        {"case_id": "c2", "mode": "bridge", "run_index": 0, "verdict": "PASS"},
        {"case_id": "c2", "mode": "bridge", "run_index": 1, "verdict": "PASS"},
    ]
    results.append({"case_id": "c2", "mode": "bridge", "run_index": 2, "verdict": "SKIP"})
    started_at = "2026-10-19T00:00:00+00:00"
    report = runner.build_summary_report(results, independent_cases=["c1", "c2"], started_at=started_at)
    assert report["independent_cases"] == 2
    assert report["modes"]["pty"]["runs"] == 4
    assert report["modes"]["bridge"]["runs"] == 5
    assert report["modes"]["bridge"]["skip"] == 1
    assert report["started_at"] == started_at


def test_unqualified_bridge_never_pass(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """驗證未核准或不合格的 bridge 自測報告絕不會得到 PASS，改記 INSUFFICIENT_EVIDENCE。"""
    import runner

    bad_report = tmp_path / "selftest-report.json"
    bad_report.write_text(json.dumps({
        "schema_version": "bridge-selftest.v1",
        "tool": {"tree_sha256": "0" * 64},
        "qualified": False,
    }), encoding="utf-8")

    bridge_src = REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"
    if str(bridge_src) not in sys.path:
        sys.path.insert(0, str(bridge_src))
    import uart_stdio_bridge.selftest.runner as selftest_runner

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(selftest_runner, "load_approved_revision", lambda path=None: {"tree_sha256": "approved"})

    def unqualified(report: dict[str, Any], **kwargs: Any) -> bool:
        calls.append({"report": report, "approved": kwargs.get("approved")})
        return False

    monkeypatch.setattr(selftest_runner, "is_qualified", unqualified)
    verdict, gate_eligible = runner.evaluate_bridge_eligibility(bad_report)
    assert gate_eligible is False
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert calls and calls[0]["report"]["qualified"] is False
    assert calls[0]["approved"] == {"tree_sha256": "approved"}


def test_cases_yaml_digest_requires_matching_freeze_lock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """驗證承接清單缺鎖或 revision 不合時不具 gate 資格。"""
    import runner
    from lib.yaml_loader import TestCase

    monkeypatch.setattr(runner, "ROOT_DIR", tmp_path)
    acceptance = tmp_path / "tests" / "acceptance"
    acceptance.mkdir(parents=True)
    cases_path = acceptance / "cases.yaml"
    cases_path.write_text("schema_version: 1\nrevision: r1\n", encoding="utf-8")
    tc = TestCase(name="frozen", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    tr = runner.TestRunner(tc, mode="bridge", report_dir=tmp_path / "reports")

    digest, frozen = tr._acceptance_cases_info()
    assert len(digest or "") == 64
    assert frozen is False

    required_sets = {"stage0", "cli_1A", "cli_1B", "slice_2.1", "slice_2.2", "slice_2.3", "slice_2.4"}
    (acceptance / "cases.lock.json").write_text(json.dumps({
        "schema_version": 1,
        "cases_revision": "r1",
        "sets": {key: [] for key in required_sets},
    }), encoding="utf-8")
    assert tr._acceptance_cases_info() == (digest, True)


def test_full_bridge_comparison_uses_final_settle_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """驗證 full 比較窗口固定在最終 __settle_end__ marker。"""
    import runner

    bridge_src = REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"
    if str(bridge_src) not in sys.path:
        sys.path.insert(0, str(bridge_src))
    import uart_stdio_bridge.compare as compare_module
    from lib.yaml_loader import TestCase

    captured: dict[str, Any] = {}

    def fake_compare(inputs: Any) -> dict[str, Any]:
        captured["until_op"] = inputs.until_op
        return {"verdict": "PASS", "reasons": []}

    monkeypatch.setattr(compare_module, "compare", fake_compare)
    tc = TestCase(
        name="full-window",
        category="test",
        severity="low",
        target={"com": "COM0"},
        profile={},
        env_files={},
        bridge={"applicable": True, "compare": "full"},
    )
    tr = runner.TestRunner(tc, mode="bridge", report_dir=tmp_path)
    tr.run_dir.mkdir(parents=True)

    tr._compare_evidence("full")

    assert captured["until_op"] == "__settle_end__"


def test_comparison_insufficient_evidence_stays_insufficient():
    """驗證比較器的 INSUFFICIENT_EVIDENCE 不會被折成產品 FAIL。"""
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(name="comparison", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    tr = runner.TestRunner(tc, mode="bridge")
    tr._bridge_eligible = True
    tr.comparison = {"verdict": "INSUFFICIENT_EVIDENCE"}

    assert tr._final_verdict(ok=False) == "INSUFFICIENT_EVIDENCE"


def test_settle_marker_records_unsettled_evidence():
    """驗證 settle marker 未達 settled 時會將本次 run 降為證據不足。"""
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(name="unsettled", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    tr = runner.TestRunner(tc, mode="bridge")

    class FakeCorrelation:
        def op_start(self, *args: Any, **kwargs: Any) -> None:
            pass

        def op_end(self, *args: Any, **kwargs: Any) -> None:
            pass

    tr.correlation = FakeCorrelation()
    tr._bridge_snapshot = lambda *, settle: {"settled": False, "wire": None}  # type: ignore[method-assign]
    tr._settle_marker("__settle_end__", 0)

    assert tr._settle_failure is not None
    assert tr._final_verdict(ok=True) == "INSUFFICIENT_EVIDENCE"


def test_bridge_shutdown_failure_downgrades_pass():
    """驗證 bridge shutdown 不乾淨時即使步驟與比較器通過仍屬證據不足。"""
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(name="shutdown", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    tr = runner.TestRunner(tc, mode="bridge")
    tr._bridge_eligible = True
    tr.comparison = {"verdict": "PASS"}
    tr._bridge_shutdown_failure = "bridge exit 3"

    assert tr._final_verdict(ok=True) == "INSUFFICIENT_EVIDENCE"


def test_has_interactive_computed_from_console_list():
    """驗證 has_interactive 依 session console-list 結果動態計算 interactive_owner。"""
    import runner

    consoles_active = [{"interactive_owner": False}, {"interactive_owner": True}]
    assert runner.compute_has_interactive(consoles_active) is True
    assert runner.compute_has_interactive([]) is False
    assert runner.compute_has_interactive([{"interactive_owner": False}]) is False


def test_input_raw_latin1_bytes():
    """驗證 console_write.input_raw 支援 Latin-1 範圍 bytes，非 Latin-1 字元拋出 StepError。"""
    import runner

    converted = runner.parse_input_raw("\x1b[A\xff")
    assert converted == b"\x1b[A\xff"

    with pytest.raises(runner.StepError):
        runner.parse_input_raw("中")


def test_any_tx_contains_raw_list_and_prefix():
    """驗證 assert_wal.any_tx_contains_raw 支援 list 模式並以 tx_source_prefix 過濾來源。"""
    import runner

    records = [
        {"dir": "TX", "source": "human:1", "payload_b64": "G1tB"},  # \x1b[A
        {"dir": "TX", "source": "agent:1", "payload_b64": "CQ=="},   # \t
    ]
    assert runner.check_wal_tx_contains_raw(records, ["\x1b[A"], tx_source_prefix="human:") is True
    assert runner.check_wal_tx_contains_raw(records, ["\t"], tx_source_prefix="human:") is False


def test_case_yaml_ids_and_descriptions_unchanged():
    """驗證 13 個 case 的 ID、描述、分類、嚴重度與基線一致，且 hk-01 不再含有重複 key。"""
    cases_dir = FUNC_TEST_DIR / "cases"
    case_files = sorted(cases_dir.glob("*.yaml"))
    assert len(case_files) == 13

    for cf in case_files:
        baseline_content = os.popen(f"git show 93553e6:func-test/cases/{cf.name}").read()
        base_data = yaml.safe_load(baseline_content)
        current_data = yaml.safe_load(cf.read_text(encoding="utf-8"))

        assert current_data["meta"]["name"] == base_data["meta"]["name"]
        assert current_data["meta"]["description"] == base_data["meta"]["description"]
        assert current_data["meta"]["category"] == base_data["meta"]["category"]
        assert current_data["meta"]["severity"] == base_data["meta"]["severity"]

    # hk-01 不得有重複 key
    hk01_path = cases_dir / "hk-01-special-keys-raw.yaml"

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"重複的 key: {key}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    yaml.load(hk01_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)


def test_all_cases_declare_bridge_block():
    """驗證 13 個 case 皆宣告 bridge 區塊，且 compare 設定合法。"""
    cases_dir = FUNC_TEST_DIR / "cases"
    for cf in sorted(cases_dir.glob("*.yaml")):
        data = yaml.safe_load(cf.read_text(encoding="utf-8"))
        assert "bridge" in data, f"{cf.name} 缺少 bridge 區塊"
        bridge = data["bridge"]
        assert "applicable" in bridge, f"{cf.name} bridge 缺少 applicable 欄位"
        compare = bridge.get("compare")
        assert compare in {"full", "none"} or (isinstance(compare, str) and compare.startswith("until_op:")), (
            f"{cf.name} compare={compare} 非法"
        )


def test_runner_knows_all_case_actions():
    """驗證 13 個 case 所使用的 action 全受 runner 支援，且 cli cmd 不再有 get 子命令。"""
    valid_actions = {
        "wait_ready", "cli", "wait_command_done", "parallel", "attach_console",
        "console_write", "console_read", "detach_console", "assert_state",
        "assert_wal", "sleep", "inject_device_event", "target_stop_responding",
        "repeat", "shell", "target_emit", "wait_for_file", "write_file", "wait_state",
    }
    cases_dir = FUNC_TEST_DIR / "cases"
    for cf in sorted(cases_dir.glob("*.yaml")):
        data = yaml.safe_load(cf.read_text(encoding="utf-8"))
        for step in data.get("steps", []):
            action = step.get("action")
            assert action in valid_actions, f"{cf.name} 含有未支援 action: {action}"
            if action == "cli":
                argv = step.get("argv", [])
                if len(argv) >= 2 and argv[0] == "cmd":
                    subcmd = argv[1]
                    assert subcmd in {"submit", "status", "result-tail", "cancel"}, (
                        f"{cf.name} 使用了非法的 cmd 子命令: {subcmd}"
                    )


def test_cowork01_ready_reachable():
    """驗證 cowork-01 的 boot_banner 能匹配 prompt_regex，且壞命令與後續命令之間含有 wait_ready。"""
    case_path = FUNC_TEST_DIR / "cases" / "cowork-01-agent-bad-cmd.yaml"
    data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    boot_banner = data["target"]["boot_banner"]
    last_line = boot_banner.splitlines()[-1] if boot_banner.splitlines() else ""
    prompt_regex = data["profile"]["prompt_regex"]
    assert re.search(prompt_regex, last_line) is not None, f"boot_banner 最後一行 {repr(last_line)} 不符合 {prompt_regex}"

    steps = data.get("steps", [])
    bad_cmd_idx = -1
    next_submit_idx = -1
    def is_submit(step: dict[str, Any]) -> bool:
        if step.get("action") == "submit_command":
            return True
        argv = step.get("argv", [])
        return step.get("action") == "cli" and len(argv) >= 2 and argv[:2] == ["cmd", "submit"]

    for idx, s in enumerate(steps):
        if is_submit(s) and "bad-hang" in str(s):
            bad_cmd_idx = idx
        elif bad_cmd_idx >= 0 and is_submit(s):
            next_submit_idx = idx
            break

    assert bad_cmd_idx >= 0, "未找到壞命令步驟"
    assert next_submit_idx > bad_cmd_idx, "未找到壞命令後續之 submit 步驟"
    has_wait_ready = any(steps[i].get("action") == "wait_ready" for i in range(bad_cmd_idx + 1, next_submit_idx))
    assert has_wait_ready is True, "壞命令與下一個 submit 命令之間缺少 wait_ready"


def test_brcm_secret_from_env_file(monkeypatch: pytest.MonkeyPatch):
    """驗證 brcm-01 的 secrets_from_env_file 從 case 的 env_files 解析出機密 bytes，不依賴 runner 環境變數。"""
    import runner

    monkeypatch.delenv("BRCM_PASS", raising=False)
    case_path = FUNC_TEST_DIR / "cases" / "brcm-01-login-flow.yaml"
    data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    bridge_cfg = data.get("bridge", {})
    secrets_spec = bridge_cfg.get("secrets_from_env_file")
    assert secrets_spec is not None, "brcm-01 缺少 secrets_from_env_file 配置"
    extracted = runner.resolve_secrets_from_env_file(data.get("env_files", {}), secrets_spec)
    assert b"admin" in extracted


def test_bridge_secrets_from_environment_are_resolved(monkeypatch: pytest.MonkeyPatch):
    """驗證 bridge.secrets_from_env 的環境變數值會以 bytes 傳入遮蔽清單。"""
    import runner

    monkeypatch.setenv("FUNC_TEST_SECRET", "secret-value")
    assert runner.resolve_secrets_from_env(["FUNC_TEST_SECRET", "MISSING_SECRET"]) == [b"secret-value"]


def test_expand_in_shell_and_wait_for_file(tmp_path: pathlib.Path):
    """驗證 runner 的變數展開支援 {run.root}，並套用至 shell.cmd、wait_for_file.path 與 write_file.path。"""
    import runner
    from lib.yaml_loader import TestCase

    case = TestCase(
        name="test-expand",
        category="misc",
        severity="low",
        target={},
        profile={},
        env_files={},
        steps=[],
    )
    tr = runner.TestRunner(case)
    tr.variables["run"] = {"root": str(tmp_path)}

    assert tr._expand("echo {run.root}") == f"echo {tmp_path}"
    assert tr._expand("{run.root}/x") == f"{tmp_path}/x"

    ready_file = tmp_path / "ready"
    ready_file.write_text("ok", encoding="utf-8")
    tr._step_wait_for_file({"path": "{run.root}/ready", "timeout_s": 0.1}, 0)


def test_write_file_expands_json_values_without_breaking_windows_paths(tmp_path: pathlib.Path):
    """驗證 JSON 內容先解析再展開，可保留 Windows 路徑反斜線。"""
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(name="json-write", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    tr = runner.TestRunner(tc)
    tr.variables["python"] = r"C:\Python312\python.exe"
    tr.variables["run"] = {"root": r"C:\Users\tester\AppData\Local\Temp\run-1"}
    out = tmp_path / "rule.json"

    tr._step_write_file({
        "path": str(out),
        "content": '{"python":"{python}","output":"{run.root}/ev-fired"}',
    }, 0)

    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed == {
        "python": r"C:\Python312\python.exe",
        "output": r"C:\Users\tester\AppData\Local\Temp\run-1/ev-fired",
    }


def test_parallel_captures_non_step_exceptions():
    """驗證 parallel 子步驟拋出的非 StepError 會向上回報。"""
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(name="parallel-error", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    tr = runner.TestRunner(tc)

    with pytest.raises(runner.StepError, match="parallel"):
        tr._step_parallel({"steps": [{"action": "sleep", "seconds": "abc"}]}, 1)


def test_main_list_prints_every_case(capsys: pytest.CaptureFixture[str]):
    """驗證 --list 會輸出所有案例，不在第一筆提早返回。"""
    import runner

    assert runner.main(["--list"]) == 0
    output = capsys.readouterr().out
    assert "brcm-01-login-flow" in output
    assert "sm-04-device-unplug" in output


def test_scenarios_option_fails_until_task_11(monkeypatch: pytest.MonkeyPatch):
    """驗證尚未支援的 --scenarios 不會被解析後靜默忽略。"""
    import runner

    monkeypatch.setattr(runner, "discover_test_cases", lambda *args, **kwargs: [])
    with pytest.raises(SystemExit) as exc_info:
        runner.main(["--scenarios"])
    assert exc_info.value.code == 2


def test_bridge_mode_skips_inapplicable_case_without_starting_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """驗證 bridge.applicable=false 會記為 SKIP 且不啟動 daemon。"""
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(
        name="not-bridge-applicable",
        category="test",
        severity="low",
        target={},
        profile={},
        env_files={},
        steps=[],
        bridge={"applicable": False, "compare": "none"},
    )
    monkeypatch.setattr(runner, "discover_test_cases", lambda *args, **kwargs: [tc])
    monkeypatch.setattr(runner.TestRunner, "run", lambda self: pytest.fail("SKIP case must not start harness"))
    monkeypatch.setattr(runner.TestRunner, "_write_manifest", lambda self, *args, **kwargs: None)

    args = [
        "--mode", "bridge",
        "--case", tc.name,
        "--selftest-report", str(tmp_path / "not-read.json"),
        "--report-dir", str(tmp_path / "reports"),
    ]
    if os.name == "nt":
        args.extend([
            "--daemon-com", "COM20",
            "--bridge-com", "COM21",
            "--transport-mode", "virtual_com0com",
        ])
    rc = runner.main(args)

    report = json.loads((tmp_path / "reports" / "func-test-report.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["results"][0]["verdict"] == "SKIP"
    assert report["modes"]["bridge"]["skip"] == 1


def test_bridge_run_args_reach_harness_config(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """驗證 case bridge.run_args 由 runner 傳入 HarnessConfig。"""
    import runner
    from lib import artifact_identity
    from lib.yaml_loader import TestCase

    config_type = runner.HarnessConfig
    captured: dict[str, Any] = {}

    class FakeHarness:
        def __init__(self, config: Any) -> None:
            captured["config"] = config
            self.root = tmp_path / "harness"
            self.endpoint = "unix:///tmp/isolated.sock"
            self.env = {}

        def start(self) -> None:
            raise runner.StepError("測試停止")

        def stop(self) -> None:
            pass

    monkeypatch.setattr(runner, "evaluate_bridge_eligibility", lambda *_: ("PASS", True))
    monkeypatch.setattr(runner, "HarnessConfig", lambda **kwargs: config_type(**kwargs))
    monkeypatch.setattr(runner, "DaemonHarness", FakeHarness)
    monkeypatch.setattr(runner.TestRunner, "_new_correlation", lambda self: None)
    monkeypatch.setattr(runner.TestRunner, "_tool_identity", lambda self: {"approved": True})
    monkeypatch.setattr(runner.TestRunner, "_capture_live_daemon", lambda self: None)
    monkeypatch.setattr(runner.TestRunner, "_write_manifest", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(artifact_identity, "collect_artifacts", lambda *args, **kwargs: {})
    monkeypatch.setattr("realhw.deployed_content.measure", lambda *args, **kwargs: {})

    tc = TestCase(
        name="bridge-args",
        category="test",
        severity="low",
        target={},
        profile={},
        env_files={},
        steps=[],
        bridge={"applicable": True, "compare": "none", "run_args": ["--read-chunk", "64"]},
    )
    tr = runner.TestRunner(tc, mode="bridge", selftest_report=tmp_path / "selftest.json", report_dir=tmp_path)
    tr.run()

    assert captured["config"].bridge_run_args == ("--read-chunk", "64")


def test_report_never_embeds_payload():
    """驗證 func-test-report.json 與 comparison.json 絕不內嵌 payload_b64 或機密文字。"""
    import runner

    secret = "SUPER_SECRET_12345"
    report_dict = runner.create_test_report(
        results=[{"case_id": "c1", "secret": secret, "payload_b64": "eA=="}],
        comparison={"details": {"payload_b64": "eA==", "token": secret}},
    )
    report_text = json.dumps(report_dict)
    assert "payload_b64" not in report_text
    assert secret not in report_text
