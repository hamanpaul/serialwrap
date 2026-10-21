"""P3-T17：WSL interop 的 Windows 原生執行腳本（win-runner.sh／win-test.sh）。

需要 WSL interop（cmd.exe）與 cargo；缺任一即 SKIP（證據不足，非 PASS）。
每個測試先斷言腳本存在，紅燈原因因此是「尚未實作」而非收集或執行期錯誤。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIN_EXE = ROOT / "target" / "x86_64-pc-windows-gnu" / "debug" / "serialwrap.exe"
RUNNER = ROOT / "scripts/rust/win-runner.sh"
WIN_TEST = ROOT / "scripts/rust/win-test.sh"
REPORT_DIR = ROOT / "target/win-test-report"
pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc,
              pytest.mark.skipif(shutil.which("cmd.exe") is None or shutil.which("cargo") is None,
                                 reason="需要 WSL interop 與 cargo（證據不足，非 PASS）")]
XENV = {"CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER": "x86_64-w64-mingw32-gcc"}


@pytest.fixture(scope="module")
def win_exe():
    subprocess.run(["cargo", "build", "-p", "sw-cli", "--target", "x86_64-pc-windows-gnu"], cwd=ROOT, check=True,
                   env={**os.environ, **XENV})
    return WIN_EXE


def _runner(*args, env=None):
    assert RUNNER.is_file(), "scripts/rust/win-runner.sh 尚未實作（P3-T17 Step 4）"
    return subprocess.run([str(RUNNER), *map(str, args)],
                          capture_output=True, env={**os.environ, **(env or {})}, check=False)


def test_runner_version_uses_crlf(win_exe):
    r = _runner(win_exe, "--version")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert r.returncode == 0
    assert r.stdout == f"serialwrap {version}\r\n".encode()     # Q-05


def test_runner_propagates_nonzero_exit(win_exe):
    r = _runner(win_exe, "session")                              # argparse 錯 → 2
    assert r.returncode == 2
    assert b"\r\n" in r.stderr and b"usage: serialwrap session" in r.stderr


def test_runner_forwards_serialwrap_env(win_exe):
    r = _runner(win_exe, "--endpoint", "tcp://127.0.0.1:1", "session", "list", env={"SERIALWRAP_LOG_LEVEL": "INFO"})
    assert r.returncode == 2
    first = r.stderr.split(b"\r\n")[0]
    assert json.loads(first)["endpoint_id"] == "127.0.0.1:1"


def test_runner_cleans_or_keeps_staging(win_exe):
    keep = _runner(win_exe, "--version", env={"SW_WIN_KEEP": "1", "SW_WIN_PRINT_STAGING": "1"})
    staging = Path(keep.stderr.decode().strip().splitlines()[-1])
    assert staging.is_dir()
    shutil.rmtree(staging)
    gone = _runner(win_exe, "--version", env={"SW_WIN_PRINT_STAGING": "1"})
    assert not Path(gone.stderr.decode().strip().splitlines()[-1]).exists()


def test_win_test_report_schema():
    assert WIN_TEST.is_file(), "scripts/rust/win-test.sh 尚未實作（P3-T17 Step 4）"
    r = subprocess.run([str(WIN_TEST), "--package", "sw-proto"], cwd=ROOT,
                       capture_output=True, text=True, check=False)
    reports = sorted(REPORT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    assert reports, f"win-test 未產生 {REPORT_DIR}/*.json；stderr={r.stderr[-2000:]}"
    report = reports[-1]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert set(data) >= {"run_id", "exes", "overall", "evidence_insufficient"}
    assert all(set(e) >= {"exe", "caps", "passed", "failed", "ignored", "exit"} for e in data["exes"])
    assert (r.returncode == 0) == (data["overall"] == "pass")
    assert data["exes"] and sum(e["passed"] for e in data["exes"]) > 0   # 空集合不得算 pass
    l1 = json.loads((report.parent / data["l1_results"]).read_text(encoding="utf-8"))   # R5-F3：逐案結果，計數只是摘要
    assert l1["format"] == "l1-results.v1" and l1["platform"] == "windows" and l1["finished"] is True
    assert {row["nodeid"] for row in l1["results"]} == set(l1["collected"]) - set(l1["deselected"])
    assert all(n.startswith("cargo:sw-proto::") for n in l1["collected"])
    assert sum(row["outcome"] == "passed" for row in l1["results"]) == sum(e["passed"] for e in data["exes"])


def test_runner_stages_fixture_dir(win_exe, tmp_path):
    # golden 測試在 Windows 執行時必須讀得到資料：runner 要建 data\ 並設 SW_TEST_DATA_DIR
    assert WIN_TEST.is_file(), "scripts/rust/win-test.sh 尚未實作（P3-T17 Step 4）"
    r = subprocess.run([str(WIN_TEST), "--package", "sw-proto", "--", "--test", "pyjson_golden"],
                       cwd=ROOT, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    keep = _runner(win_exe, "--version", env={"SW_WIN_KEEP": "1", "SW_WIN_PRINT_STAGING": "1"})
    staging = Path(keep.stderr.decode().strip().splitlines()[-1])
    for rel in ("data/contracts/golden/pyjson.tsv", "data/contracts/cli_help/_meta.json", "data/VERSION",
                "data/sw_core/assets/skill/SKILL.md", "data/tests/contract_blackbox/data/rpc_params.json"):
        assert (staging / rel).is_file(), rel
    shutil.rmtree(staging)


def test_win_test_injects_runner_under_wsl(tmp_path):
    assert WIN_TEST.is_file(), "scripts/rust/win-test.sh 尚未實作（P3-T17 Step 4）"
    out = subprocess.run(["bash", "-c", f"WSL_DISTRO_NAME= SW_WIN_TEST_PRINT_ENV=1 {ROOT}/scripts/rust/win-test.sh --dry-run"],
                         capture_output=True, text=True, check=False).stdout
    assert "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER=" in out    # 本機為 WSL（binfmt_misc/WSLInterop 存在）
