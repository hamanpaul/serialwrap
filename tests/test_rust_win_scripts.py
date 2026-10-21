"""P3-T17：WSL interop 的 Windows 原生執行腳本（win-runner.sh／win-test.sh）。

需要 WSL interop（cmd.exe）與 cargo；缺任一即 SKIP（證據不足，非 PASS）。
每個測試先斷言腳本存在，紅燈原因因此是「尚未實作」而非收集或執行期錯誤。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIN_EXE = ROOT / "target" / "x86_64-pc-windows-gnu" / "debug" / "serialwrap.exe"
RUNNER = ROOT / "scripts/rust/win-runner.sh"
WIN_TEST = ROOT / "scripts/rust/win-test.sh"
AB_BLACKBOX = ROOT / "scripts/rust/ab_blackbox.sh"
WIN_BLACKBOX_SH = ROOT / "scripts/rust/win-blackbox.sh"
WIN_BLACKBOX_PS1 = ROOT / "scripts/rust/win-blackbox.ps1"
L1_LOCK = ROOT / "tests/acceptance/l1_required.lock.json"
REPORT_DIR = ROOT / "target/win-test-report"
BLACKBOX_PURITY_GUARDS = {
    "tests/contract_blackbox/test_bb_no_product_import.py::test_no_product_import_anywhere_in_blackbox",
    "tests/contract_blackbox/test_bb_harness_selftest.py::test_bb_standalone_writes_l1_report",
}
pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc,
              pytest.mark.skipif(shutil.which("cmd.exe") is None or shutil.which("cargo") is None,
                                 reason="需要 WSL interop 與 cargo（證據不足，非 PASS）")]
XENV = {"CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER": "x86_64-w64-mingw32-gcc"}


def _ast_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _ast_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _is_pytest_xfail_reference(node: ast.AST, pytest_aliases: set[str], mark_aliases: set[str],
                               xfail_aliases: set[str]) -> bool:
    dotted = _ast_name(node)
    targets = {f"{name}.mark.xfail" for name in pytest_aliases}
    targets.update(f"{name}.xfail" for name in mark_aliases)
    return dotted in targets or dotted in xfail_aliases


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


def test_blackbox_pytest_argv_matches_frozen_l1_selectors():
    """A／B 兩端 pytest argv 必須只含鎖定 selector 與 junit 輸出路徑。"""
    lock = json.loads(L1_LOCK.read_text(encoding="utf-8"))["sets"]
    ab_script = AB_BLACKBOX.read_text(encoding="utf-8")
    linux_selector = lock["s1a_blackbox"]["linux"]["selector"]["argv"]
    windows_selector = lock["s1a_blackbox"]["windows"]["selector"]["argv"]
    for set_id in ("s1a_delegation", "s1b_20groups"):
        assert lock[set_id]["linux"]["selector"]["argv"] == linux_selector
        assert lock[set_id]["windows"]["selector"]["argv"] == windows_selector
    normalized = re.sub(r"\\\s*\n\s*", " ", ab_script)
    normalized = re.sub(r"\s+", " ", normalized)

    a_block = normalized.split("# 2. 執行 A 輪", 1)[1].split("a_rc=$?", 1)[0]
    b_block = normalized.split("# 4. 執行 B 輪", 1)[1].split("b_rc=$?", 1)[0]
    a_argv = shlex.split(a_block.split("python3 -m pytest ", 1)[1].strip())
    b_argv = shlex.split(b_block.split("python3 -m pytest ", 1)[1].strip())
    assert a_argv == [*linux_selector, "--junitxml=$A_XML"]
    assert b_argv == [*linux_selector, "--junitxml=$B_XML"]
    for block in (a_block, b_block):
        assert "--confcutdir" not in block
        assert "no:cacheprovider" not in block
        assert "xfail_strict" not in block

    win_script = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    invoke = win_script.split("function Invoke-Blackbox", 1)[1].split("$sourceRoot =", 1)[0]
    assignment = re.search(r"\$arguments = @\((.*?)\)", invoke, re.DOTALL)
    assert assignment, "Invoke-Blackbox 必須明確列出 pytest argv"
    actual_windows = [single if single else double for single, double in re.findall(
        r"'([^']*)'|\"([^\"]*)\"", assignment.group(1)
    )]
    assert actual_windows == ["-3.12", "-m", "pytest", *windows_selector, "--junitxml=$JunitPath"]
    assert "--confcutdir" not in invoke
    assert "no:cacheprovider" not in invoke
    assert "xfail_strict" not in invoke
    assert "-k" not in invoke


def test_blackbox_selector_keeps_purity_guards_in_every_required_platform_set():
    lock = json.loads(L1_LOCK.read_text(encoding="utf-8"))["sets"]
    for platform in ("linux", "windows"):
        locked_nodes = set(lock["s1a_blackbox"][platform]["nodeids"])
        assert BLACKBOX_PURITY_GUARDS <= locked_nodes, (platform, BLACKBOX_PURITY_GUARDS - locked_nodes)


def test_contract_blackbox_xfails_are_explicitly_strict():
    violations = []
    for path in sorted((ROOT / "tests/contract_blackbox").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        pytest_aliases: set[str] = set()
        mark_aliases: set[str] = set()
        xfail_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pytest_aliases.update(
                    alias.asname or alias.name.split(".", 1)[0]
                    for alias in node.names if alias.name == "pytest"
                )
            elif isinstance(node, ast.ImportFrom) and node.module in ("pytest", "pytest.mark"):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if alias.name == "mark":
                        mark_aliases.add(name)
                    elif alias.name == "xfail":
                        xfail_aliases.add(name)

        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not _is_pytest_xfail_reference(node, pytest_aliases, mark_aliases, xfail_aliases):
                continue
            parent = parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: xfail marker must be called with strict=True")
                continue
            strict = next((kw.value for kw in parent.keywords if kw.arg == "strict"), None)
            if not isinstance(strict, ast.Constant) or strict.value is not True:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: xfail must explicitly set strict=True")

    assert not violations, "\\n".join(violations)


def test_windows_com_runner_is_confined_and_wires_the_frozen_case():
    """COM 證據只使用 DUT pair，且 B 輪提供 allowlist、bridge wire 與 release/attach 驗收。"""
    wrapper = WIN_BLACKBOX_SH.read_text(encoding="utf-8")
    powershell = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    com_test = (ROOT / "tests/contract_blackbox/test_cli_mgmt_daemon.py").read_text(encoding="utf-8")

    assert '"$SW_TEST_COM_PAIR" != "COM20:COM21"' in wrapper
    assert 'SW_WIN_SELFTEST_TIMEOUT_S' in wrapper
    assert "$ComPair -cne 'COM20:COM21'" in powershell
    assert '$SelftestTimeoutSeconds' in powershell
    assert "selftest --endpoint win-serial" in powershell
    assert "'run', '--run-id'" in powershell
    assert "COM21" in powershell and "SW_TEST_COM_PAIR" in powershell
    assert "SW_TEST_DUT_WIRE" in powershell
    assert "SERIALWRAP_TEST_DEVICE_ALLOWLIST" in com_test
    assert "SERIALWRAP_ENDPOINT" in com_test
    assert '["device", "release", "--selector", "COM0"]' in powershell
    assert '["device", "attach", "--selector", "COM0"]' in powershell
    assert 'cmd-status-poll' in powershell
    assert 'status_deadline = time.monotonic() + 20' in powershell
    assert "echo X\\r" in com_test
    assert 'row.get("dir") == "TX" and row.get("observation") == "dut_stdin_write"' in com_test
    assert 'base64.b64decode(row["payload_b64"], validate=True)' in com_test


def test_win_staging_covers_contract_blackbox_scripts_refs():
    """tests/contract_blackbox 內以 import 或路徑引用的 scripts/rust/*.py 必須列入 win-blackbox.ps1 的 staging 清單。

    缺漏時 Windows 黑箱收集會因找不到檔案而中止（staging 不含 scripts/ 的歷史缺陷）。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8")
    refs: set[str] = set()
    for path in (ROOT / "tests" / "contract_blackbox").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"scripts/rust/[A-Za-z0-9_]+\.py", text):
            refs.add(match.group(0))
        for match in re.finditer(r"^\s*(?:from|import)\s+scripts\.rust\.([A-Za-z0-9_]+)", text, re.MULTILINE):
            refs.add(f"scripts/rust/{match.group(1)}.py")
    assert refs, "預期 contract_blackbox 有 scripts/rust 引用（ab_blackbox_1b）"
    missing = sorted(ref for ref in refs if f"'{ref}'" not in ps1)
    assert not missing, f"win-blackbox.ps1 staging 清單未涵蓋：{missing}"
