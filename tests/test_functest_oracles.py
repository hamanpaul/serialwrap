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
    report = runner.build_summary_report(results, independent_cases=["c1", "c2"])
    assert report["independent_cases"] == 2
    assert report["modes"]["pty"]["runs"] == 4
    assert report["modes"]["bridge"]["runs"] == 4


def test_unqualified_bridge_never_pass(tmp_path: pathlib.Path):
    """驗證未核准或不合格的 bridge 自測報告絕不會得到 PASS，改記 INSUFFICIENT_EVIDENCE。"""
    import runner

    bad_report = tmp_path / "selftest-report.json"
    bad_report.write_text(json.dumps({
        "schema_version": "bridge-selftest.v1",
        "tool": {"tree_sha256": "0" * 64},
        "qualified": False,
    }), encoding="utf-8")

    verdict, gate_eligible = runner.evaluate_bridge_eligibility(bad_report)
    assert gate_eligible is False
    assert verdict == "INSUFFICIENT_EVIDENCE"


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
        {"source": "human:1", "payload_b64": "G1tB"},  # \x1b[A
        {"source": "agent:1", "payload_b64": "CQ=="},   # \t
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
    for idx, s in enumerate(steps):
        if s.get("action") in ("submit_command", "cli") and ("bad-hang" in str(s)):
            bad_cmd_idx = idx
        elif bad_cmd_idx >= 0 and s.get("action") in ("submit_command", "cli") and ("cmd" in str(s) or "command" in str(s)):
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
