"""Slice 2.1 前置交付檢查（計畫 06 §0.3、Task 0）：逐項 import 與檔案存在檢查。

缺任一符號或檔案即 collection／執行失敗，表示上游 P0～P4 前置未到位（屬上游缺陷，應停止並回報）。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

# 黑箱輔助（bb_faketarget／bb_daemon）以 POSIX 行程與 FIFO 為前提，故模組層標 posix_only；資源 marker 逐項標示（規則 2）。
pytestmark = [pytest.mark.posix_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 黑箱套件的 bb_*.py 以裸模組名互相 import（`from bb_harness import ...`），故 contract_blackbox 目錄也須在 sys.path。
for _p in (ROOT, ROOT / "tests", ROOT / "tests" / "contract_blackbox"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# P3：ab_env 與 contract_blackbox 黑箱輔助（計畫 §0.3 P3 列）
from ab_env import AB_ENV  # noqa: E402,F401
from tests.contract_blackbox.bb_harness import (  # noqa: E402,F401
    repo_root, ab_label, is_rust, cli_prefix, daemon_prefix, isolated_env, run_cli, CliResult, NL,
)
from tests.contract_blackbox.bb_faketarget import FakeTarget  # noqa: E402,F401
from tests.contract_blackbox.bb_daemon import PyDaemon  # noqa: E402,F401


@pytest.mark.memory_only
def test_prereq_files_exist():
    """§0.3 P0～P3 與 P2 的檔案型前置（缺一即上游缺陷，停止並回報）。"""
    for rel in ("tests/acceptance/cases.lock.json", "tests/acceptance/cases.yaml",
                "tests/acceptance/cases.schema.json", "tests/acceptance/regen_lock.py",
                "func-test/scenarios/l15-reject-zero-tx.yaml", "func-test/lib/daemon_harness.py",
                "tests/contract_blackbox/bin/py-serialwrapd",
                "scripts/rust/check_cfg_boundary.py", "scripts/rust/win-test.sh", "scripts/rust/win-runner.sh",
                "scripts/rust/win-blackbox.ps1", "scripts/rust/mkbundle.sh",
                "contracts/error_codes.yaml", "contracts/session_last_errors.yaml",
                "contracts/behavior_changes.yaml", "contracts/rpc_schema.json",
                "contracts/wal_record.schema.json", "contracts/state_file.schema.json",
                "contracts/golden/rpc/line_limit.json", "crates/sw-daemon/src/launcher.rs"):
        assert (ROOT / rel).is_file(), rel
    # §0.3 P1：contracts/golden/rpc/*.json（P1-OC 重新擷取的 RPC golden，本 Phase 比對基準）。
    golden = ROOT / "contracts" / "golden" / "rpc"
    assert golden.is_dir(), "contracts/golden/rpc 目錄不存在（P1 上游缺陷）"
    assert any(golden.glob("*.json")), "contracts/golden/rpc/*.json 不得為空（P1 上游缺陷）"
    # §0.3 P2：daemon_harness 的兩個解析函式（只做 AST 名稱檢查，不 import 以免產生副作用）。
    import ast
    tree = ast.parse((ROOT / "func-test" / "lib" / "daemon_harness.py").read_text(encoding="utf-8"))
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"resolve_cli_exe", "resolve_daemon_argv"} <= defined


@pytest.mark.process_rpc
def test_func_test_runner_flags():
    """§0.3 P2：runner 旗標（`--mode`、`--dut-mode`、`--report-dir`、`--selftest-report`、`--scenarios`、`--case`）。"""
    out = subprocess.run(
        [sys.executable, str(ROOT / "func-test" / "runner.py"), "--help"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    text = out.stdout + out.stderr
    for flag in ("--mode", "--dut-mode", "--report-dir", "--selftest-report", "--scenarios", "--case"):
        assert flag in text, flag
