"""P7 gate 凍結集合與平台承擔的收集回歸。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "tests/acceptance/l1_required.lock.json"


def _lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_p7_gate_frozen_nodes_are_collectable() -> None:
    """P7 gate 每個 pytest 凍結 nodeid 都必須在對應 selector 的收集結果中。"""
    sets = _lock()["sets"]
    for set_id, platforms in sets.items():
        if not set_id.startswith("p7_gate:") or "linux" not in platforms:
            continue
        spec = platforms["linux"]
        argv = spec["selector"]["argv"]
        if not any(arg == "tests" or arg.startswith("tests/") for arg in argv):
            continue  # Cargo 集合沒有 pytest selector。

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", *argv],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert result.returncode == 0, f"{set_id}.linux collect-only 失敗：{result.stdout}\n{result.stderr}"
        collected = {line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line}
        missing = sorted(set(spec["nodeids"]) - collected)
        assert not missing, f"{set_id}.linux 凍結 nodeid 未被 selector 收集：{missing}"


def test_od72_slice21_nodes_keep_their_assigned_owner() -> None:
    """OD-72 移出 Linux slice21 集合時，Windows 案例與 DF-20 案例仍保留承擔。"""
    sets = _lock()["sets"]
    linux_nodes = set(sets["p7_gate:contract-blackbox-slice21-b"]["linux"]["nodeids"])
    windows_nodes = set(sets["s0_win_pytest"]["windows"]["nodeids"])
    full_linux_nodes = set(sets["p7_gate:python3-m-pytest-q-tests"]["linux"]["nodeids"])

    windows_cases = {
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_config_yaml_socket_path_written",
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_deferred_methods_windows",
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_second_daemon_exit_2",
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_session_public_console_endpoint_null",
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_stop_exit_0",
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_tcp_endpoint_loopback_and_ping",
        "tests/contract_blackbox/slice21/test_windows_nocom.py::test_wal_share_delete_while_running",
    }
    deferred_case = "tests/contract_blackbox/slice21/test_safety_core.py::test_stale_context_not_replayed"

    assert not (linux_nodes & windows_cases), f"Windows 原生案例仍在 Linux slice21 集合：{sorted(linux_nodes & windows_cases)}"
    assert windows_cases <= windows_nodes, f"Windows 原生集合缺案例：{sorted(windows_cases - windows_nodes)}"
    assert deferred_case not in linux_nodes, "DF-20 deferred 案例仍在 P7 Linux slice21 集合"
    assert deferred_case in full_linux_nodes, "DF-20 案例不得從完整 pytest 分母刪除"
