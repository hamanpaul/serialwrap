# tests/test_functest_bridge_smoke.py
"""func-test bridge 模式煙霧測試（POSIX PTY、DUT 模擬器與比較器端到端）。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import platform
from typing import Any

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FUNC_TEST_DIR = REPO_ROOT / "func-test"


def test_harness_closes_fake_target_slave_before_daemon_start(tmp_path: pathlib.Path) -> None:
    """驗證 POSIX harness 不保留 PTY slave，避免被外部 holder 防線誤判。"""
    sys.path.insert(0, str(FUNC_TEST_DIR))
    from lib.daemon_harness import DaemonHarness
    from lib.target_config import TargetConfig

    harness = DaemonHarness()
    harness.config.target_config = TargetConfig()
    harness._root = tmp_path
    try:
        harness._start_fake_target()
        assert harness.fake_target is not None
        assert harness.fake_target.slave_fd == -1
    finally:
        if harness.fake_target is not None:
            harness.fake_target.stop()


def _qualified_fixture(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """建構符合 Task 8 欄位的資格 fixture；runner 子程序僅注入 fixture 核准資料。"""
    bridge_src = REPO_ROOT / "tools" / "uart_stdio_bridge" / "src"
    if str(bridge_src) not in sys.path:
        sys.path.insert(0, str(bridge_src))
    from uart_stdio_bridge import TOOL_NAME, __version__
    from uart_stdio_bridge.identity import tool_tree_sha256
    from uart_stdio_bridge.selftest.checks import CHECK_IDS
    from uart_stdio_bridge.evidence.manifest import platform_info

    tree_sha = tool_tree_sha256()
    approved = {
        "schema_version": "bridge-approved.v1",
        "tree_sha256": tree_sha,
        "reviewer": "test-fixture",
        "review_ref": "test-only",
        "date": "2026-10-19",
    }
    approved_path = tmp_path / "approved-revision.json"
    approved_path.write_text(json.dumps(approved), encoding="utf-8")
    report = {
        "schema_version": "bridge-selftest.v1",
        "created_at": "2026-10-19T00:00:00Z",
        "complete": True,
        "not_selected": [],
        "tool": {"name": TOOL_NAME, "version": __version__, "tree_sha256": tree_sha},
        "platform": {**platform_info(), "evidence_layer": "posix-pty", "com_pair": None},
        "endpoint": "posix-pty",
        "transport_mode": "virtual_pty",
        "capabilities": {"transport_disconnect": True},
        "checks": [{"id": check_id, "verdict": "PASS", "reason": None, "evidence_dir": str(tmp_path)} for check_id in CHECK_IDS],
        "verdict": "PASS",
        "test_overrides": {},
    }
    report_path = tmp_path / "selftest-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path, approved_path


def _runner_command(report: pathlib.Path, approved: pathlib.Path, report_dir: pathlib.Path, case_id: str) -> list[str]:
    code = (
        "import json,pathlib,sys; "
        f"root=pathlib.Path({str(REPO_ROOT)!r}); "
        "sys.path.insert(0,str(root/'tools'/'uart_stdio_bridge'/'src')); "
        "sys.path.insert(0,str(root/'func-test')); "
        f"approved=json.loads(pathlib.Path({str(approved)!r}).read_text()); "
        "import runner; import uart_stdio_bridge.selftest.runner as st; "
        "st.load_approved_revision=lambda path=None: approved; "
        f"sys.exit(runner.main(['--mode','bridge','--case',{case_id!r},'--selftest-report',{str(report)!r},'--report-dir',{str(report_dir)!r}]))"
    )
    return [sys.executable, "-c", code]


def test_bridge_smoke_sm01(tmp_path: pathlib.Path) -> None:
    """驗證 bridge 模式執行 sm-01 煙霧測試，含 noise quiesce settle、comparison 與 manifest 驗證。"""
    selftest_report, approved = _qualified_fixture(tmp_path)
    report_dir = tmp_path / "reports"

    cmd = _runner_command(selftest_report, approved, report_dir, "sm-01-attach-to-ready")
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0, f"runner 失敗:\nstdout: {res.stdout}\nstderr: {res.stderr}"

    case_dirs = list(report_dir.glob("sm-01-attach-to-ready/bridge/*"))
    assert len(case_dirs) > 0, "未產生 sm-01 證據目錄"
    case_dir = case_dirs[0]

    corr_path = case_dir / "correlation.ndjson"
    assert corr_path.exists(), f"未找到 correlation.ndjson: {corr_path}"
    corr_lines = [json.loads(line) for line in corr_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    settle_ops = {op.get("op_id"): op for op in corr_lines if op.get("op_id") in ("__settle_start__", "__settle_end__") and op.get("kind") == "op_start"}
    assert "__settle_start__" in settle_ops and settle_ops["__settle_start__"].get("settled") is True
    assert "__settle_end__" in settle_ops and settle_ops["__settle_end__"].get("settled") is True

    comp_path = case_dir / "comparison.json"
    assert comp_path.exists(), f"未找到 comparison.json: {comp_path}"
    comp_data = json.loads(comp_path.read_text(encoding="utf-8"))
    assert comp_data.get("verdict") == "PASS"

    manifest_path = case_dir / "manifest.json"
    assert manifest_path.exists(), f"未找到 manifest.json: {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cases_yaml_sha256"] is None
    assert manifest["dut"]["seed"] == 0
    report = json.loads((report_dir / "func-test-report.json").read_text(encoding="utf-8"))
    assert report["results"][0]["gate_eligible"] is False
    schema_path = REPO_ROOT / "tests" / "acceptance" / "l15_manifest.schema.json"
    if schema_path.exists():
        import jsonschema
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(instance=manifest, schema=schema)
        missing_evidence = json.loads(json.dumps(manifest))
        missing_evidence["evidence"]["control"] = None
        missing_evidence["evidence"]["wal_dir"] = None
        jsonschema.validate(instance=missing_evidence, schema=schema)


def test_bridge_smoke_sm04_until_op(tmp_path: pathlib.Path) -> None:
    """驗證 bridge 模式執行 sm-04-device-unplug，支援 until_op 比較窗口與 settle。"""
    selftest_report, approved = _qualified_fixture(tmp_path)
    report_dir = tmp_path / "reports"

    cmd = _runner_command(selftest_report, approved, report_dir, "sm-04-device-unplug")
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0, f"runner 失敗:\nstdout: {res.stdout}\nstderr: {res.stderr}"

    case_dirs = list(report_dir.glob("sm-04-device-unplug/bridge/*"))
    assert len(case_dirs) > 0, "未產生 sm-04 證據目錄"
    case_dir = case_dirs[0]

    corr_path = case_dir / "correlation.ndjson"
    assert corr_path.exists(), f"未找到 correlation.ndjson: {corr_path}"
    corr_lines = [json.loads(line) for line in corr_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    inject_event_ops = [
        op for op in corr_lines
        if op.get("op_id") == "s002-inject_device_event" and op.get("kind") == "op_start"
    ]
    assert len(inject_event_ops) > 0, "未找到 s002-inject_device_event op_start"
    assert inject_event_ops[0].get("settled") is True

    comp_path = case_dir / "comparison.json"
    assert comp_path.exists(), f"未找到 comparison.json: {comp_path}"
    comp_data = json.loads(comp_path.read_text(encoding="utf-8"))
    assert comp_data.get("window", {}).get("until_op") == "s002-inject_device_event"
    assert comp_data.get("verdict") == "PASS"
