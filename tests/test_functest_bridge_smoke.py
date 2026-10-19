# tests/test_functest_bridge_smoke.py
"""func-test bridge 模式煙霧測試（POSIX PTY、DUT 模擬器與比較器端到端）。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FUNC_TEST_DIR = REPO_ROOT / "func-test"


def test_bridge_smoke_sm01(tmp_path: pathlib.Path) -> None:
    """驗證 bridge 模式執行 sm-01 煙霧測試，含 noise quiesce settle、comparison 與 manifest 驗證。"""
    selftest_report = tmp_path / "selftest-report.json"
    selftest_report.write_text(json.dumps({
        "schema_version": "bridge-selftest.v1",
        "qualified": True,
        "tool": {"tree_sha256": "dummy"},
    }), encoding="utf-8")
    report_dir = tmp_path / "reports"

    cmd = [
        sys.executable,
        str(FUNC_TEST_DIR / "runner.py"),
        "--mode", "bridge",
        "--case", "sm-01-attach-to-ready",
        "--selftest-report", str(selftest_report),
        "--report-dir", str(report_dir),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0, f"runner 失敗:\nstdout: {res.stdout}\nstderr: {res.stderr}"

    case_dirs = list(report_dir.glob("sm-01-attach-to-ready/bridge/*"))
    assert len(case_dirs) > 0, "未產生 sm-01 證據目錄"
    case_dir = case_dirs[0]

    corr_path = case_dir / "correlation.ndjson"
    assert corr_path.exists(), f"未找到 correlation.ndjson: {corr_path}"
    corr_lines = [json.loads(line) for line in corr_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    settle_ops = {op.get("op_id"): op for op in corr_lines if op.get("op_id") in ("__settle_start__", "__settle_end__")}
    assert "__settle_start__" in settle_ops and settle_ops["__settle_start__"].get("snapshot", {}).get("settled") is True
    assert "__settle_end__" in settle_ops and settle_ops["__settle_end__"].get("snapshot", {}).get("settled") is True

    comp_path = case_dir / "comparison.json"
    assert comp_path.exists(), f"未找到 comparison.json: {comp_path}"
    comp_data = json.loads(comp_path.read_text(encoding="utf-8"))
    assert comp_data.get("verdict") == "PASS"

    manifest_path = case_dir / "manifest.json"
    assert manifest_path.exists(), f"未找到 manifest.json: {manifest_path}"
    schema_path = REPO_ROOT / "contracts" / "l15_manifest.schema.json"
    if schema_path.exists():
        import jsonschema
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        jsonschema.validate(instance=manifest, schema=schema)


def test_bridge_smoke_sm04_until_op(tmp_path: pathlib.Path) -> None:
    """驗證 bridge 模式執行 sm-04-device-unplug，支援 until_op 比較窗口與 settle。"""
    selftest_report = tmp_path / "selftest-report.json"
    selftest_report.write_text(json.dumps({
        "schema_version": "bridge-selftest.v1",
        "qualified": True,
        "tool": {"tree_sha256": "dummy"},
    }), encoding="utf-8")
    report_dir = tmp_path / "reports"

    cmd = [
        sys.executable,
        str(FUNC_TEST_DIR / "runner.py"),
        "--mode", "bridge",
        "--case", "sm-04-device-unplug",
        "--selftest-report", str(selftest_report),
        "--report-dir", str(report_dir),
    ]
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
        if op.get("op_id") == "s002-inject_device_event" and op.get("type") == "op_start"
    ]
    assert len(inject_event_ops) > 0, "未找到 s002-inject_device_event op_start"
    assert inject_event_ops[0].get("snapshot", {}).get("settled") is True

    comp_path = case_dir / "comparison.json"
    assert comp_path.exists(), f"未找到 comparison.json: {comp_path}"
    comp_data = json.loads(comp_path.read_text(encoding="utf-8"))
    assert comp_data.get("window", {}).get("until_op") == "s002-inject_device_event"
    assert comp_data.get("verdict") == "PASS"
