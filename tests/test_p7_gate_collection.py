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

    # OD101I-02：同一 Windows 原生 selector 的三個完整 L1 集合必須凍結同一分母。
    sets = _lock()["sets"]
    windows_sets = [
        sets["s21_core"]["windows"],
        sets["p7_gate:win-test-sh-non-com"]["windows"],
        sets["s22_gate:win_test"]["windows"],
    ]
    assert windows_sets[0]["selector"] == windows_sets[1]["selector"] == windows_sets[2]["selector"]
    windows_nodeids = [set(spec["nodeids"]) for spec in windows_sets]
    assert windows_nodeids[0] == windows_nodeids[1] == windows_nodeids[2]

    # OD101I-12：H* 同一 HEAD 的三份 Windows L1 已收集並 passed 這兩列，不是未解差異。
    audit = (ROOT / "docs/rust-refactor/reviews/od98-linux-pytest-skipped-audit.md").read_text(encoding="utf-8")
    assert "需保留為未解差異" not in audit
    assert "ba29434ce55f1bfd903945b451e269d6915b89e1" in audit
    assert "selector 完全相同" in audit and "finished=true" in audit and "exit_code=0" in audit
    assert "failed=0" in audit and "passed=2516" in audit and "skipped=33" in audit
    assert "exact_cap_kept" in audit and "connection_cap_128_pauses_accept" in audit
    assert "outcome=passed" in audit and "skip_kind=null" in audit


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
    startup_case = "tests/contract_blackbox/slice21/test_state_file_compat.py::test_startup_always_rewrites_state_json"
    reboot_tail_case = "tests/contract_blackbox/slice21/test_f4_session_semantics.py::test_background_reboot_result_tail_matches_python"

    assert not (linux_nodes & windows_cases), f"Windows 原生案例仍在 Linux slice21 集合：{sorted(linux_nodes & windows_cases)}"
    assert windows_cases <= windows_nodes, f"Windows 原生集合缺案例：{sorted(windows_cases - windows_nodes)}"
    assert deferred_case not in linux_nodes, "DF-20 deferred 案例仍在 P7 Linux slice21 集合"
    # OD-105（2026-11-18）：Linux 全套 pytest 不可執行的 DF-20 案例改由 Rust 黑箱承擔，不再留在完整 pytest 分母；
    # 但必須在 OD-105 分類表以分類 1／Rust 黑箱（s24_ab）列名，且路徑仍在 gate_shared.sh 的 S24_TARGETS 收集範圍內。
    assert deferred_case not in full_linux_nodes, "OD-105 已將 DF-20 案例移出完整 pytest 分母"
    od105 = (ROOT / "docs/rust-refactor/reviews/od105-node-classification.md").read_text(encoding="utf-8")
    od105_rows = [line for line in od105.splitlines() if f"`{deferred_case}`" in line]
    assert len(od105_rows) == 1, "DF-20 案例必須在 OD-105 分類表恰列一次"
    assert "| 1 |" in od105_rows[0] and "`1-S24-RUST`" in od105_rows[0], f"DF-20 案例的 OD-105 承擔不正確：{od105_rows[0]}"
    # DF-20 在 s21_gate:blackbox_rust（rust-s21）與 P7 slice21 B 輪會 skip，承擔只能是 s24_ab（rust-linux）。
    assert deferred_case not in set(sets["s21_gate:blackbox_rust"]["linux"]["nodeids"]), "DF-20 案例不得列為 blackbox_rust 必要列"
    gate_shared = (ROOT / "scripts/rust/gate_shared.sh").read_text(encoding="utf-8")
    assert "S24_TARGETS=(tests/contract_blackbox " in gate_shared, "s24_ab 必須收集 tests/contract_blackbox 才能承擔 DF-20 案例"
    assert startup_case in linux_nodes, "OD-72 前已屬 Linux slice21 的 startup 案例必須保留原承擔"
    assert reboot_tail_case in linux_nodes, "A1 新增的 background reboot result_tail 案例依裁決保留於 slice21"


def test_od78_replacement_preserves_continue_on_error_coverage() -> None:
    """OD-78 替換舊 Windows bundle 禁止斷言時，no continue_on_error 仍須由獨立測試涵蓋。"""
    nodes = set(_lock()["sets"]["p7_gate:python3-m-pytest-q-tests"]["linux"]["nodeids"])
    assert "tests/test_release_workflow.py::test_windows_bundle_job_runs_on_hosted_windows_after_linux_bundle" in nodes
    assert "tests/test_release_workflow.py::test_workflow_does_not_build_windows_bundle_and_has_no_continue_on_error" not in nodes
    source = (ROOT / "tests/test_release_workflow.py").read_text(encoding="utf-8")
    assert "def test_no_job_or_step_uses_continue_on_error(" in source
