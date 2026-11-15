"""MCU A/B 工具的黑箱自測：確認 v0.3.2 wrapper 身分與逐欄差異判定。"""
from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only]

_ROOT = Path(__file__).resolve().parents[2]
_BB = _ROOT / "tests" / "contract_blackbox"
_V032_WRAPPER = _BB / "bin" / "py-serialwrapd-v032.py"
_DIFF_TOOL = _BB / "mcu_flash_parity.py"


@pytest.mark.process_rpc
@pytest.mark.posix_pty
def test_v032_wrapper_runs_tagged_daemon_and_reports_032_version(tmp_path, monkeypatch):
    """實際以 git archive 的 v0.3.2 啟 daemon，並由同版 CLI 讀回版本。"""
    assert _V032_WRAPPER.is_file(), f"缺少 v0.3.2 daemon wrapper：{_V032_WRAPPER}"

    from bb_harness import repo_root
    from p8_fixtures import SwEnv
    import ab_env

    archive = tmp_path / "v0.3.2.tar"
    source = tmp_path / "v0.3.2"
    source.mkdir()
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive}", "v0.3.2"],
        cwd=repo_root(),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with tarfile.open(archive) as bundle:
        if "filter" in inspect.signature(bundle.extractall).parameters:
            bundle.extractall(source, filter="data")
        else:
            bundle.extractall(source)
    assert (source / "VERSION").read_text(encoding="utf-8").strip() == "0.3.2"

    monkeypatch.setattr(
        ab_env,
        "AB_ENV",
        {
            "SERIALWRAP_AB_LABEL": "python-032",
            "SERIALWRAP_CLI_EXE": str(source / "serialwrap.py"),
            "SERIALWRAP_DAEMON_EXE": str(_V032_WRAPPER),
        },
    )
    sw = SwEnv(tmp_path / "rig")
    sw._env["SW_PY032_ROOT"] = str(source)
    try:
        sw.start(targets={}, profile="passthrough")
        status = sw.cli("daemon", "status")
        assert status.exit == 0 and status.json is not None, (status.exit, status.stdout, status.stderr)
        assert status.json.get("version") == "0.3.2", status.json
    finally:
        sw.close()


@pytest.mark.memory_only
def test_mcu_flash_diff_tool_requires_and_renders_attribution(tmp_path):
    """逐 case/field 比對三輪；未歸因差異須失敗，具來源的裁決可通過。"""
    assert _DIFF_TOOL.is_file(), f"缺少 MCU A/B 差異比對工具：{_DIFF_TOOL}"
    spec = importlib.util.spec_from_file_location("mcu_flash_parity_under_test", _DIFF_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = {
        "python032": {"s24-flash-example": {"ack_hex": "00cc"}},
        "python_main": {"s24-flash-example": {"ack_hex": "00cc"}},
        "rust": {"s24-flash-example": {"ack_hex": "00cc"}},
        "attributions": {},
    }
    report, failed = module.compare(data)
    assert not failed
    assert "三輪一致" in report

    data["python_main"]["s24-flash-example"]["ack_hex"] = "00cc01"
    data["rust"]["s24-flash-example"]["ack_hex"] = "00cc01"
    report, failed = module.compare(data)
    assert failed
    assert "未歸因" in report

    data["attributions"]["s24-flash-example.ack_hex"] = {
        "kind": "python_post_032",
        "reference": "abc1234 sw_core/flash_endpoint.py:100",
    }
    report, failed = module.compare(data)
    assert not failed
    assert "Python 0.3.2 之後修正" in report

    # 缺少 Windows Rust self_test observation 必須在歸因前拒絕，OD-91 不能補出不存在的 observation。
    missing_observation = {
        "python032": {"win-self-test": {"result": "ok"}, "shared-case": {"result": "ok"}},
        "python_main": {"win-self-test": {"result": "ok"}, "shared-case": {"result": "ok"}},
        "rust": {"shared-case": {"result": "ok"}},
        "attributions": {"win-self-test.result": {"kind": "v032_no_windows_daemon", "reference": "OD-91"}},
    }
    input_path, output_path = tmp_path / "missing.json", tmp_path / "missing.md"
    input_path.write_text(json.dumps(missing_observation), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(_DIFF_TOOL), "compare", "--input", str(input_path),
                           "--output", str(output_path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)


@pytest.mark.memory_only
def test_mcu_flash_diff_loader_keeps_multiple_nodes_per_case(tmp_path):
    """同一 acceptance case 的多個 pytest node 必須各自保留觀察欄位。"""
    spec = importlib.util.spec_from_file_location("mcu_flash_parity_multi_node", _DIFF_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "observations.ndjson"
    rows = [
        {"round": "python032", "case_id": "s24-mcu-rpc-shapes", "nodeid": "tests/test.py::patterns",
         "fields": {"body": "patterns"}},
        {"round": "python032", "case_id": "s24-mcu-rpc-shapes", "nodeid": "tests/test.py::status",
         "fields": {"body": "status"}},
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    loaded = module._load_rows(source, "python032")
    assert loaded == {
        "s24-mcu-rpc-shapes::tests/test.py::patterns": {"body": "patterns"},
        "s24-mcu-rpc-shapes::tests/test.py::status": {"body": "status"},
    }
    data = {
        "python032": loaded,
        "python_main": loaded,
        "rust": {
            **loaded,
            "s24-mcu-rpc-shapes::tests/test.py::patterns": {"body": "patterns-rust"},
        },
        "attributions": {
            "s24-mcu-rpc-shapes::tests/test.py::patterns.body": {
                "kind": "registered_df_or_ruling",
                "reference": "SC-32",
            },
        },
    }
    report, failed = module.compare(data)
    assert not failed
    assert "tests/test.py::patterns" in report and "tests/test.py::status" in report
    assert "SC-32" in report
