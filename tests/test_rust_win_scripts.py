"""P3-T17：WSL interop 的 Windows 原生執行腳本（win-runner.sh／win-test.sh）。

需要 WSL interop（cmd.exe）與 cargo；缺任一即 SKIP（證據不足，非 PASS）。
每個測試先斷言腳本存在，紅燈原因因此是「尚未實作」而非收集或執行期錯誤。
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

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
    # daemon 以 body＋"\\n" 送出 agent 命令（sw_core/uart_io.py）；案例須比對本案例基線後恰一行。
    assert '_dut_tx_bytes(wire)[tx_baseline:]' in com_test
    assert 'b"echo X\\n"' in com_test
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


def test_win_staging_covers_imports_of_staged_scripts_rust():
    """已列入 win-blackbox.ps1 staging 的 scripts/rust/*.py，其對 scripts/rust 同層模組的 import 亦須遞迴列入。

    回歸：check_p4_cases.py `import owner_gated`，但 owner_gated.py 未列入 staging，Windows 原生 A 輪
    以 ModuleNotFoundError: owner_gated 中止（僅測 contract_blackbox 的引用不足以涵蓋間接依賴）。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    staged = set(re.findall(r"'(scripts/rust/[A-Za-z0-9_]+\.py)'", ps1))
    queue = sorted(staged)
    closure: set[str] = set()
    while queue:
        ref = queue.pop()
        if ref in closure:
            continue
        closure.add(ref)
        text = (ROOT / ref).read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE):
            candidate = f"scripts/rust/{match.group(1)}.py"
            if (ROOT / candidate).is_file():
                queue.append(candidate)
    missing = sorted(ref for ref in closure if f"'{ref}'" not in ps1)
    assert not missing, f"win-blackbox.ps1 staging 清單未涵蓋間接 import 的腳本：{missing}"


def _ps_function(source: str, name: str) -> str:
    """取出 win-blackbox.ps1 中指定 function 的本體（到下一個 function 或頂層 `$sourceRoot =` 為止）。"""
    head = source.split(f"function {name}", 1)
    assert len(head) == 2, f"win-blackbox.ps1 缺少 function {name}"
    return head[1].split("\nfunction ", 1)[0].split("\n$sourceRoot =", 1)[0]


def test_windows_process_handles_are_cached_after_start_process():
    """每個 `$process = Start-Process ... -PassThru` 之後必須立即快取 `$process.Handle`。

    Windows PowerShell 5.1 若未快取 Handle，行程結束後 ExitCode 會是 null（bridge_stop process_exit=null 的根因）。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    starts = re.findall(r"\$process = Start-Process\b", ps1)
    cached = re.findall(
        r"\$process = Start-Process(?:[^\r\n]*`\r?\n)*[^\r\n]*\r?\n(?:\s*#[^\r\n]*\r?\n)*\s*\$null = \$process\.Handle\b",
        ps1,
    )
    assert starts, "win-blackbox.ps1 應至少含一個 $process = Start-Process"
    assert len(cached) == len(starts), f"Start-Process {len(starts)} 處，但僅 {len(cached)} 處緊接快取 Handle"


def test_windows_exit_code_null_is_failure_not_zero():
    """ExitCode 為 null 時必須判定為失敗，不得以 [int]$null（= 0）或 `ExitCode -eq 0` 放行。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    # 舊的 null→0 放行形式不得再出現。
    assert "[int]$process.ExitCode" not in ps1
    assert "$Bridge.process.ExitCode -eq 0" not in ps1

    selftest = _ps_function(ps1, "Invoke-BridgeSelftest")
    assert "$null -eq $rawSelftestExit" in selftest
    assert re.search(r"\$ok = \(\$exitStatusKnown -and\s", selftest), "selftest PASS 必須要求 ExitCode 已知"
    assert "exit_status_known" in selftest

    stop = _ps_function(ps1, "Stop-WindowsComBridge")
    assert "$processExitKnown = ($null -ne $processExit)" in stop
    assert re.search(r"status = if \(\$controlExit -eq 0 -and \$exited -and \$processExitKnown -and \$processExit -eq 0\)", stop)
    assert "process_exit_known" in stop


def test_windows_blackbox_l1_is_staged_then_copied_to_outdir():
    """SW_RESOURCE_REPORT 與 B 輪 junit 必須指向 `<staging>\\l1\\windows\\blackbox\\`，之後複製到 OutDir。

    test_cli_mgmt_ab_suite 要求 A/B 證據路徑位於本 worktree（staging），直接指向 \\\\wsl.localhost 會失敗。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    assert "Join-Path $stagingRepo 'l1\\windows\\blackbox'" in ps1
    assert "$stagingResourceReport = Join-Path $stagingL1Directory 'resource-report.json'" in ps1
    assert "$bStagingJunit = Join-Path $stagingL1Directory 'junit.xml'" in ps1
    # B 輪 Invoke-Blackbox 必須同時帶 staging 的 junit 與 resource report。
    b_call = re.search(r"Invoke-Blackbox 'b-rust-win' \$bStagingJunit\b[\s\S]*?\$stagingResourceReport\b", ps1)
    assert b_call, "B 輪必須以 staging junit 與 staging resource report 呼叫 Invoke-Blackbox"
    assert "Invoke-Blackbox 'b-rust-win' $bJunit" not in ps1
    assert "$resourceReport $bundleRoot" not in ps1
    # 複製回 OutDir 的步驟必須存在，且目的地仍為 OutDir\l1\windows\blackbox。
    assert "$l1Directory = Join-Path $outputRoot 'l1\\windows\\blackbox'" in ps1
    assert "Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $l1Directory $_.Name) -Force" in ps1


def test_windows_blackbox_staging_repo_has_exact_git_revision():
    """staging repo 從 Windows 本機 bundle 匯入 exact HEAD，供 P4-T16 身分驗證使用。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    initialize = _ps_function(ps1, "Initialize-StagingGit")
    assert "git -C $StagingRepo init --quiet" in initialize
    assert "Copy-Item -LiteralPath $GitBundle -Destination $localBundle -Force" in initialize
    assert "git -C $StagingRepo fetch --quiet --no-tags $localBundle HEAD" in initialize
    assert "git -C $StagingRepo rev-parse FETCH_HEAD" in initialize
    assert "Initialize-StagingGit $GitBundle $stagingRepo $revision $scratchRoot" in ps1
    assert "git -C $StagingRepo fetch --quiet --no-tags $SourceRoot $Revision" not in initialize
    assert "refs/heads/sw-staging $fetchedRevision" in initialize
    assert "git -C $StagingRepo symbolic-ref HEAD refs/heads/sw-staging" in initialize

    shell = WIN_BLACKBOX_SH.read_text(encoding="utf-8")
    assert 'git -C "$ROOT" bundle create "$git_bundle" HEAD' in shell
    assert '-GitBundle "$git_bundle_win"' in shell

    workflow = (ROOT / ".github" / "workflows" / "rust.yml").read_text(encoding="utf-8")
    assert "git -C $root bundle create $gitBundle HEAD" in workflow
    assert "-GitBundle $gitBundle" in workflow


def test_windows_blackbox_ps1_parses_under_windows_powershell():
    """以 Windows PowerShell 解析器語法檢查 win-blackbox.ps1（不執行）；無 powershell.exe 時 SKIP。"""
    powershell = shutil.which("powershell.exe")
    wslpath = shutil.which("wslpath")
    if not powershell or not wslpath:
        pytest.skip("需要 WSL interop（powershell.exe／wslpath）以解析 PowerShell 語法")
    win_path = subprocess.run([wslpath, "-w", str(WIN_BLACKBOX_PS1)], capture_output=True, text=True, check=True).stdout.strip()
    script = (
        "$t=$null;$e=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{win_path}',[ref]$t,[ref]$e);"
        "Write-Output ('parse_errors=' + $e.Count)"
    )
    out = subprocess.run([powershell, "-NoProfile", "-Command", script], capture_output=True, text=True,
                         timeout=120, check=False).stdout
    assert "parse_errors=0" in out, out


def test_win_dev_bundle_pointer_is_install_root_relative():
    """bin\\current-release.json 的 `dir` 必須為相對安裝根的 `versions/<V>`（正斜線）。

    justification（OD-18 (3)）：`dir` 一律以正斜線表示，與 launcher／`delegate.rs` 安全路徑規則一致；
    `crates/sw-daemon/src/launcher.rs` 拒絕含 `\\` 的 `dir`（`current.dir.contains('\\\\')`），故 JSON 指標字串不得使用反斜線。
    Rust 端以安裝根為基準（delegate.rs 以 exe_dir.parent() 接 `dir`；rollback.rs 寫入 `versions/<V>`）。
    1e470cd 曾改成 `..\\versions\\<V>`（以 bin\\ 為基準）與之不一致，予以回復；本測試只改 JSON 指標字串，檔案系統的 Join-Path 維持原樣。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    assert 'dir = "versions/$version"' in ps1
    assert 'dir = "versions\\$version"' not in ps1
    assert '"..\\versions' not in ps1


def test_windows_blackbox_copies_ab_suite_and_dev_bundle_to_outdir():
    """ab-suite 輸出與 Windows dev bundle 必須在 staging 刪除前複製回 OutDir（否則 A/B 失敗無法診斷，register 拿不到 --windows-cli）。

    ab-suite 位於 `<staging>\\l1\\windows\\blackbox\\ab-suite\\`（SW_RESOURCE_REPORT 同層）；bundle 匯出到 `<OutDir>\\bundle\\`，
    內含 bin\\serialwrap.exe、bin\\current-release.json、versions\\<V>\\release-manifest.json，以及 manifest 所指的
    versions\\<V>\\bin\\serialwrap.exe（register 的 Rust CLI artifact 必須與 manifest 的 cli.path 相符）。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    assert "function Copy-BlackboxDiagnostics" in ps1
    assert "Join-Path $StagingRepo 'l1\\windows\\blackbox\\ab-suite'" in ps1
    assert "Join-Path $OutputRoot 'l1\\windows\\blackbox\\ab-suite'" in ps1
    assert "Join-Path $OutputRoot 'bundle'" in ps1
    assert "'bin\\serialwrap.exe'" in ps1
    assert "'bin\\current-release.json'" in ps1
    assert '"versions\\$bundleVersion\\release-manifest.json"' in ps1
    assert '"versions\\$bundleVersion\\bin\\serialwrap.exe"' in ps1
    finally_block = ps1.split("} finally {", 1)[1]
    assert "Copy-BlackboxDiagnostics $stagingRepo $bundleRoot $outputRoot" in finally_block
    assert finally_block.index("Copy-BlackboxDiagnostics") < finally_block.index("Remove-Item -LiteralPath $scratchRoot")


def test_windows_com_bridge_evidence_is_local_scratch_with_verified_copy_back():
    """COM 序列 bridge（`uart_stdio_bridge run`）的 --evidence-dir 與 stdout／stderr 必須位於 Windows 本機 scratch；
    停止後（以及啟動失敗時）整份回寫 OutputRoot\\windows-com\\bridge 並逐檔比對 SHA256，回寫不符時 bridge stop 不得 PASS。

    回歸：`261027-152902-584296` 的 bridge 已寫出 wire（bridge_start、peer_state）後，`control.json` 未建立即以
    `HARNESS_DUT_SPAWN_FAILED`／`[Errno 9] Bad file descriptor` 結束（exit 3）。Windows CRT `_commit`（`os.fsync`）在
    `FlushFileBuffers` 任何失敗時都回 EBADF；當時 evidence dir 位於 \\\\wsl.localhost（9P），與 selftest 曾遇到的 9P 寫入失敗同類。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    start = _ps_function(ps1, "Start-WindowsComBridge")
    start_code = "\n".join(line for line in start.splitlines() if not line.strip().startswith("#"))

    # 1. --evidence-dir、wire、control、stdout、stderr 只能來自本機 scratch。
    assert "$localBridgeRoot = Join-Path $ScratchRoot 'windows-com-bridge'" in start_code
    for name in ("wirePath", "controlPath", "stdoutPath", "stderrPath"):
        assignment = re.search(rf"^\s*\${name} = (.+)$", start_code, re.MULTILINE)
        assert assignment, name
        assert "$localBridgeRoot" in assignment.group(1), assignment.group(0)
    assert "'--evidence-dir', $localBridgeRoot" in start_code
    assert "'--evidence-dir', $bridgeRoot" not in start_code
    assert "wsl.localhost" not in start_code.lower()

    # 2. 啟動失敗也先回寫再拋出，保留診斷證據。
    throw_index = start_code.rindex("throw $reason")
    assert "Copy-WindowsComBridgeEvidence $localBridgeRoot $bridgeRoot" in start_code[:throw_index]

    # 3. 回寫函式逐檔比對 SHA256。
    copy = _ps_function(ps1, "Copy-WindowsComBridgeEvidence")
    assert "Get-Sha256 $file.FullName" in copy and "Get-Sha256 $copied" in copy

    # 4. 停止時在行程結束後回寫；回寫不完整不得 PASS。
    stop = _ps_function(ps1, "Stop-WindowsComBridge")
    exit_index = stop.index("$exited = $Bridge.process.WaitForExit(30000)")
    copy_index = stop.index("Copy-WindowsComBridgeEvidence $Bridge.evidence_dir $Bridge.output_dir")
    assert exit_index < copy_index, "回寫必須在 bridge 行程結束之後"
    assert "if ($status -eq 'PASS' -and -not $copyOk) { $status = 'EVIDENCE-INSUFFICIENT' }" in stop
    assert stop.index("$copyOk = ") < stop.index("if ($status -eq 'PASS' -and -not $copyOk)")
    assert "evidence_copy_ok" in stop


def test_windows_com_bridge_stop_exception_still_copies_back_evidence():
    """Stop-WindowsComBridge 拋例外時（catch 分支與 finally 兜底）也須先回寫本機 bridge 證據再刪 scratch，
    回寫錯誤附到 bridge_stop；B 輪結果另記 OutputRoot 回寫副本路徑（本機 scratch 刪除後不得只剩懸空引用）。
    雲端審查 0e97195..d2118cd 對 7c94c31 的 Low 1／Low 2。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    code = "\n".join(line for line in ps1.splitlines() if not line.strip().startswith("#"))
    main_stop = code.index("$bridgeStop = Stop-WindowsComBridge $bridge $scratchRoot")
    main_catch = code[main_stop:code.index("windows_com_case = ", main_stop)]
    assert "Copy-WindowsComBridgeEvidence $bridge.evidence_dir $bridge.output_dir" in main_catch
    assert main_catch.index("taskkill.exe") < main_catch.index("Copy-WindowsComBridgeEvidence")
    assert "evidence_copy_errors" in main_catch
    final_stop = code.index("try { $null = Stop-WindowsComBridge $bridge $scratchRoot } catch {")
    final_catch = code[final_stop:code.index("Copy-BlackboxDiagnostics $stagingRepo", final_stop)]
    assert "Copy-WindowsComBridgeEvidence $bridge.evidence_dir $bridge.output_dir" in final_catch
    assert final_catch.index("taskkill.exe") < final_catch.index("Copy-WindowsComBridgeEvidence")
    invoke_b = code.index("$b = Invoke-Blackbox 'b-rust-win'")
    after_b = code[invoke_b:code.index("$bridgeStop = Stop-WindowsComBridge", invoke_b)]
    assert "$b['bridge_wire_output'] = " in after_b
    assert "Join-Path $outputRoot 'windows-com\\bridge\\wire.ndjson'" in after_b


def test_windows_selftest_out_is_local_scratch_with_verified_copy_back():
    """bridge selftest 的 --out 必須位於 Windows 本機 scratch，不得位於 OutputRoot 或網路路徑之下；
    行程結束後須整份回寫 OutputRoot\\windows-com\\bridge-selftest 並逐檔比對 SHA256，資格判定行維持不變。

    回歸：ST-04 把 50 MB stderr 證據寫到 \\\\wsl.localhost（9P）時曾截斷（dut.stderr.log 僅 48431104 bytes）並出現
    HARNESS_EVIDENCE_WRITE_FAILED（bridge rc=3），連鎖使 windows-com 判 FAIL、B 輪 COM pair 缺席。
    """
    raw = WIN_BLACKBOX_PS1.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "win-blackbox.ps1 必須維持 UTF-8 BOM"
    ps1 = raw.decode("utf-8-sig")
    fn = _ps_function(ps1, "Invoke-BridgeSelftest")
    # 只檢查程式行（略過註解），避免說明文字干擾斷言。
    code = "\n".join(line for line in fn.splitlines() if not line.strip().startswith("#"))

    # 1. selftest 的 --out、stdout 與 stderr 只能來自本機 scratch。
    assert "$selftestRoot = Join-Path $ScratchRoot 'bridge-selftest'" in code
    assert "$selftestLog = Join-Path $ScratchRoot 'bridge-selftest.stdout.log'" in code
    assert "$selftestError = Join-Path $ScratchRoot 'bridge-selftest.stderr.log'" in code
    assert '--out "{1}"\' -f $ComPair, $selftestRoot' in code
    for assignment in re.findall(r"^\s*\$selftestRoot = (.+)$", code, re.MULTILINE):
        assert "$OutputRoot" not in assignment and "$evidenceRoot" not in assignment, assignment
    assert "wsl.localhost" not in code.lower(), "selftest 不得以 \\\\wsl.localhost 路徑作為 --out"

    # 2. 行程結束之後才回寫 OutputRoot，並逐檔比對 SHA256。
    wait_index = code.index("$process.WaitForExit($timeoutSeconds * 1000)")
    copy_index = code.index("$copyWork = Join-Path $copyRoot 'work'")
    assert wait_index < copy_index, "回寫必須在 selftest 行程結束之後"
    assert "$copyRoot = Join-Path $evidenceRoot 'bridge-selftest'" in code
    assert "Copy-Item -LiteralPath (Join-Path $selftestRoot 'work') -Destination $copyWork -Recurse -Force" in code
    assert "Copy-Item -LiteralPath $reportPath -Destination $copyReportPath -Force" in code
    assert "Get-Sha256 $file.FullName" in code and "Get-Sha256 $copied" in code
    assert "$copyOk = ($copyErrors.Count -eq 0)" in code

    # 3. 資格判定不變：verify 腳本與 is_qualified 呼叫維持原樣；回寫只會使證據不足，不會放行。
    assert "$ok = ($exitStatusKnown -and $selftestExit -eq 0 -and $qualifiedExit -eq 0 -and $report -and $report.verdict -eq 'PASS')" in code
    assert "& py -3.12 $verifyScript $reportPath" in code
    assert "is_qualified(" in ps1
    assert "$status = if (-not $ok)" in code
    assert "elseif ($copyOk) { 'PASS' }" in code

    # 4. 回傳物件記錄本機 --out 與 OutDir 副本兩個路徑。
    for key in ("local_out", "evidence_copy", "evidence_copy_ok", "report"):
        assert re.search(rf"^\s*{key} = ", code, re.MULTILINE), key

    # 5. windows-com.json 明列兩個路徑。
    assert "selftest_paths = if ($selftest)" in ps1
    assert "local_out = $selftest.local_out" in ps1 and "evidence_copy = $selftest.evidence_copy" in ps1


def test_windows_com_diagnostics_run_after_raw_pass_without_qualification():
    """raw selftest PASS 可啟動診斷，但 bridge qualification 仍是最終 PASS 必要條件。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    selftest = _ps_function(ps1, "Invoke-BridgeSelftest")

    diagnostic = re.search(r"\$diagnosticReady = ([^\r\n]+)", selftest)
    assert diagnostic, "selftest 應分開輸出 raw 診斷可執行狀態與 qualification status"
    assert "$exitStatusKnown" in diagnostic.group(1)
    assert "$selftestExit -eq 0" in diagnostic.group(1)
    assert "$report.verdict -eq 'PASS'" in diagnostic.group(1)
    assert "$report.complete -eq $true" in diagnostic.group(1)
    assert "$qualifiedExit" not in diagnostic.group(1), "revision qualification 不得阻止診斷案例執行"
    assert "diagnostic_ready = $diagnosticReady" in selftest

    assert "$selftest.diagnostic_ready" in ps1, "raw PASS 時應執行 COM bridge 與 B 輪診斷案例"
    assert "elseif (-not $selftest -or $selftest.status -ne 'PASS' -or" in ps1, (
        "qualification 未通過時不得把診斷執行結果提升為 COM PASS"
    )


def test_windows_com_autostart_case_supplies_release_manifest():
    """隔離 fixture 會清除 SERIALWRAP_*；Windows Rust autostart 須明確傳入 staging bundle manifest。"""
    case_file = ROOT / "tests/contract_blackbox/test_cli_mgmt_daemon.py"
    source = case_file.read_text(encoding="utf-8")
    body = source.split("def test_autostart_l15_tx_once(", 1)[1]
    assert "bundle_manifest_path(" in body
    assert 'env["SERIALWRAP_RELEASE_MANIFEST"]' in body
    assert body.index('env["SERIALWRAP_RELEASE_MANIFEST"]') < body.index("_submit_after_on_demand_autostart(env, wire, tx_baseline)")


def test_windows_com_autostart_case_pins_com20_profile():
    """SC-18 綁定唯一核准 DUT 埠，避免依賴空目錄的動態 fallback profile。"""
    case_file = ROOT / "tests/contract_blackbox/test_cli_mgmt_daemon.py"
    source = case_file.read_text(encoding="utf-8")
    body = source.split("def test_autostart_l15_tx_once(", 1)[1]
    assert 'profile_dir = Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles"' in body
    assert "device_by_id: COM20" in body
    assert "com: COM0" in body
    assert body.index("device_by_id: COM20") < body.index("_submit_after_on_demand_autostart(env, wire, tx_baseline)")


def test_windows_com_autostart_waits_for_passthrough_ready_before_final_submit(tmp_path, monkeypatch):
    """SC-18 先證明 on-demand 啟動，再等 passthrough READY 才傳送唯一命令。"""
    case_dir = ROOT / "tests/contract_blackbox"
    case_file = case_dir / "test_cli_mgmt_daemon.py"
    sys.path.insert(0, str(case_dir))
    try:
        spec = importlib.util.spec_from_file_location("serialwrap_sc18_case_tdd", case_file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
            spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(case_dir))

    bundle = tmp_path / "bundle"
    manifest = bundle / "versions" / "0.3.2" / "release-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    wire = tmp_path / "wire.ndjson"
    env = {"SERIALWRAP_CONFIG_DIR": str(tmp_path / "config")}
    module.os = SimpleNamespace(
        name="nt",
        environ={
            "SW_WIN_DEV_BUNDLE": str(bundle),
            "SW_TEST_COM_PAIR": "COM20:COM21",
            "SW_TEST_DUT_WIRE": str(wire),
        },
    )
    monkeypatch.setattr(module, "is_rust", lambda: True)
    monkeypatch.setattr(module, "bundle_manifest_path", lambda _: manifest)
    monkeypatch.setattr(module, "_WIRE_SETTLE_S", 0.05)
    monkeypatch.setattr(module, "_WIRE_FLUSH_WAIT_S", 1.0)

    calls = []
    states = ["ATTACHED", "READY"]

    def fake_run_cli(args, *, env, timeout_s=120.0):
        calls.append(list(args))
        profile = Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles" / "windows-com.yaml"
        profile_text = profile.read_text(encoding="utf-8")
        assert "com: COM0" in profile_text and "device_by_id: COM20" in profile_text
        if args[:2] == ["cmd", "submit"]:
            if sum(call[:2] == ["cmd", "submit"] for call in calls) == 1:
                return SimpleNamespace(
                    rc=2,
                    stdout=b'{"ok":false,"error_code":"SESSION_NOT_READY","session":{"com":"COM0","state":"ATTACHED"}}',
                    stderr="serialwrap: on-demand：已自動啟動\n".encode(),
                )
            wire.write_text(
                '{"dir":"TX","observation":"dut_stdin_write","payload_b64":"' +
                base64.b64encode(b"echo X\n").decode("ascii") + '"}\n',
                encoding="utf-8",
            )
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        if args == ["session", "list"]:
            state = states.pop(0) if states else "READY"
            return SimpleNamespace(
                rc=0,
                stdout=json.dumps({"sessions": [{"com": "COM0", "state": state}]}).encode(),
                stderr=b"",
            )
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")

    commands = [call for call in calls if call[:2] == ["cmd", "submit"]]
    assert len(commands) == 2
    assert commands[0][-2:] == ["--cmd-timeout", "1"]
    assert commands[1][-2:] == ["--completion-regex", "echo X"]
    assert calls[1:3] == [["session", "list"], ["session", "list"]]
    assert calls[3:] == [commands[1], ["daemon", "stop"]]


def _load_sc18_case(tmp_path, monkeypatch):
    """載入 SC-18 案例模組並以假 Windows 環境替換 os／bundle 解析。"""
    case_dir = ROOT / "tests/contract_blackbox"
    case_file = case_dir / "test_cli_mgmt_daemon.py"
    sys.path.insert(0, str(case_dir))
    try:
        spec = importlib.util.spec_from_file_location("serialwrap_sc18_case_variants", case_file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
            spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(case_dir))
    bundle = tmp_path / "bundle"
    manifest = bundle / "versions" / "0.3.2" / "release-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    wire = tmp_path / "wire.ndjson"
    module.os = SimpleNamespace(
        name="nt",
        environ={
            "SW_WIN_DEV_BUNDLE": str(bundle),
            "SW_TEST_COM_PAIR": "COM20:COM21",
            "SW_TEST_DUT_WIRE": str(wire),
        },
    )
    monkeypatch.setattr(module, "is_rust", lambda: True)
    monkeypatch.setattr(module, "bundle_manifest_path", lambda _: manifest)
    monkeypatch.setattr(module, "_WIRE_SETTLE_S", 0.05)
    monkeypatch.setattr(module, "_WIRE_FLUSH_WAIT_S", 1.0)
    return module, {"SERIALWRAP_CONFIG_DIR": str(tmp_path / "config")}, wire


def _write_echo_tx(wire):
    wire.write_text(
        '{"dir":"TX","observation":"dut_stdin_write","payload_b64":"' +
        base64.b64encode(b"echo X\n").decode("ascii") + '"}\n',
        encoding="utf-8",
    )


def test_windows_com_autostart_first_submit_success_is_single_request(tmp_path, monkeypatch):
    """首次 autostart 請求直接成功（原驗收形狀）時不得再送第二次。"""
    module, env, wire = _load_sc18_case(tmp_path, monkeypatch)
    calls = []

    def fake_run_cli(args, *, env, timeout_s=120.0):
        calls.append(list(args))
        if args[:2] == ["cmd", "submit"]:
            _write_echo_tx(wire)
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr="serialwrap: on-demand：已自動啟動\n".encode())
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")
    assert [call[:2] for call in calls] == [["cmd", "submit"], ["daemon", "stop"]]


@pytest.mark.parametrize(
    ("first_stdout", "tx_on_reject", "message"),
    [
        (b'{"ok":false,"error_code":"SESSION_NOT_READY"}', True, "被拒絕的首次請求不得在 DUT 端留下任何 TX"),
        (b'{"ok":false,"error_code":"TIMEOUT"}', False, "只接受 session 尚未 READY 的拒絕"),
        (b"not-json", False, "只接受 session 尚未 READY 的拒絕"),
    ],
)
def test_windows_com_autostart_rejects_unexpected_first_failure(
    tmp_path, monkeypatch, first_stdout, tx_on_reject, message,
):
    """首次請求失敗時只接受零 TX 的 SESSION_NOT_READY；其他錯誤或已 TX 皆 FAIL。"""
    module, env, wire = _load_sc18_case(tmp_path, monkeypatch)

    def fake_run_cli(args, *, env, timeout_s=120.0):
        if args[:2] == ["cmd", "submit"]:
            if tx_on_reject:
                _write_echo_tx(wire)
            return SimpleNamespace(rc=2, stdout=first_stdout, stderr="serialwrap: on-demand：已自動啟動\n".encode())
        if args == ["session", "list"]:
            return SimpleNamespace(
                rc=0, stdout=json.dumps({"sessions": [{"com": "COM0", "state": "READY"}]}).encode(), stderr=b"",
            )
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    with pytest.raises(AssertionError, match=message):
        module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")


def test_windows_com_autostart_counts_only_its_own_tx(tmp_path, monkeypatch):
    """共用 wire 已含其他案例的 `echo X` 時仍以基線後的 TX 判定；本案例多送一次即 FAIL。"""
    module, env, wire = _load_sc18_case(tmp_path, monkeypatch)
    _write_echo_tx(wire)  # 同一 B 輪較早案例留下的 TX
    sends = []

    def fake_run_cli(args, *, env, timeout_s=120.0):
        if args[:2] == ["cmd", "submit"]:
            sends.append(args)
            with wire.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"dir":"TX","observation":"dut_stdin_write","payload_b64":"' +
                    base64.b64encode(b"echo X\n").decode("ascii") + '"}\n'
                )
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr="serialwrap: on-demand：已自動啟動\n".encode())
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")

    # 同一案例若寫出兩行命令（重送）必須 FAIL。
    module2, env2, wire2 = _load_sc18_case(tmp_path / "dup", monkeypatch)

    def duplicate_run_cli(args, *, env, timeout_s=120.0):
        if args[:2] == ["cmd", "submit"]:
            for _ in range(2):
                with wire2.open("a", encoding="utf-8") as handle:
                    handle.write(
                        '{"dir":"TX","observation":"dut_stdin_write","payload_b64":"' +
                        base64.b64encode(b"echo X\n").decode("ascii") + '"}\n'
                    )
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr="serialwrap: on-demand：已自動啟動\n".encode())
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module2, "run_cli", duplicate_run_cli)
    with pytest.raises(AssertionError):
        module2.test_autostart_l15_tx_once(tmp_path / "dup", env2, tmp_path / "dup" / "DELEGATED")


def test_windows_com_autostart_waits_for_lagging_wire_record(tmp_path, monkeypatch):
    """bridge 證據 writer 非同步落盤：CLI 回傳後才寫入的 TX 列仍須被計入（有界等待）。"""
    import threading

    module, env, wire = _load_sc18_case(tmp_path, monkeypatch)
    writers = []

    def fake_run_cli(args, *, env, timeout_s=120.0):
        if args[:2] == ["cmd", "submit"]:
            timer = threading.Timer(0.3, _write_echo_tx, args=(wire,))
            timer.start()
            writers.append(timer)
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr="serialwrap: on-demand：已自動啟動\n".encode())
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    try:
        module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")
    finally:
        for timer in writers:
            timer.join(2)


def test_windows_com_autostart_rejected_request_delayed_tx_is_counted(tmp_path, monkeypatch):
    """被拒請求的 TX 延遲落盤、READY 立即、最後一次送出遺失 → 必須 FAIL（不得以延遲 TX 冒充最後一次送出）。"""
    import threading

    module, env, wire = _load_sc18_case(tmp_path, monkeypatch)
    writers = []
    submits = []

    def fake_run_cli(args, *, env, timeout_s=120.0):
        if args[:2] == ["cmd", "submit"]:
            submits.append(args)
            if len(submits) == 1:
                timer = threading.Timer(0.3, _write_echo_tx, args=(wire,))
                timer.start()
                writers.append(timer)
                return SimpleNamespace(
                    rc=2, stdout=b'{"ok":false,"error_code":"SESSION_NOT_READY"}',
                    stderr="serialwrap: on-demand：已自動啟動\n".encode(),
                )
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        if args == ["session", "list"]:
            return SimpleNamespace(
                rc=0, stdout=json.dumps({"sessions": [{"com": "COM0", "state": "READY"}]}).encode(), stderr=b"",
            )
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    try:
        with pytest.raises(AssertionError, match="被拒絕的首次請求不得在 DUT 端留下任何 TX"):
            module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")
    finally:
        for timer in writers:
            timer.join(2)


def test_windows_com_autostart_requires_autostart_evidence(tmp_path, monkeypatch):
    """首次請求即使成功，也必須留下 on-demand 自動啟動證據。"""
    module, env, wire = _load_sc18_case(tmp_path, monkeypatch)

    def fake_run_cli(args, *, env, timeout_s=120.0):
        if args[:2] == ["cmd", "submit"]:
            _write_echo_tx(wire)
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        if args == ["daemon", "stop"]:
            return SimpleNamespace(rc=0, stdout=b'{"ok":true}', stderr=b"")
        raise AssertionError(f"未預期的 CLI 呼叫：{args!r}")

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    with pytest.raises(AssertionError, match="autostart 證據"):
        module.test_autostart_l15_tx_once(tmp_path, env, tmp_path / "DELEGATED")


def test_windows_dev_bundle_keeps_daemon_inside_install_tree():
    """Windows B 輪 manifest launcher 與執行程序須落在同一版本安裝樹。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    bundle = _ps_function(ps1, "New-WindowsDevBundle")
    assert "$daemonScript = Join-Path $versionRoot 'serialwrapd.py'" in bundle
    assert "Copy-Item -LiteralPath (Join-Path $RepoRoot 'serialwrapd.py') -Destination $daemonScript -Force" in bundle
    assert "Copy-RepoTree (Join-Path $RepoRoot 'sw_core') (Join-Path $versionRoot 'sw_core')" in bundle
    assert "$launcherPath = 'serialwrapd.py'" in bundle
    assert "path = $launcherPath; sha256 = Get-Sha256 $daemonScript" in bundle
    assert "$bundleDaemon = Join-Path $bundleRoot \"versions\\$bundleVersion\\serialwrapd.py\"" in ps1
    assert "$bundleDaemon $runtimeExe $Label $true" in ps1
    assert "$pythonDaemon $runtimeExe $Label $true" not in ps1

def test_windows_selftest_writeback_retries_and_compares_both_logs():
    """F8：selftest 回寫最多 3 次有界重試；stdout／stderr 兩份 log 併入 SHA256 逐檔比對（不一致只使證據不足）。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    block = ps1.split("function Invoke-BridgeSelftest", 1)[1].split("\nfunction ", 1)[0]
    assert "$copyAttempt -le 3" in block
    assert "foreach ($log in @($selftestLog, $selftestError))" in block
    assert "(Get-Sha256 $log) -ne (Get-Sha256 $logCopy)" in block
    assert "if ($copyErrors.Count -eq 0) { break }" in block


def test_windows_selftest_qualification_failure_records_reason():
    """D：status FAIL 若只因核准 tree 不符，selftest-result.json 必須寫明原因並保留 tree 比對診斷。"""
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    block = ps1.split("function Invoke-BridgeSelftest", 1)[1].split("\nfunction ", 1)[0]
    # 診斷只輸出、不參與判定：退出碼仍只由 is_qualified 決定。
    assert '"approved_tree_sha256": approved.get("tree_sha256")' in block
    assert '"current_tree_sha256": current_tree' in block
    assert "raise SystemExit(0 if qualified else 1)" in block
    reason = block.split("$reason = ", 1)[1].split("\n", 1)[0]
    assert "elseif ($qualifiedExit -ne 0)" in reason
    assert reason.index("$selftestExit -ne 0 -or $report.verdict -ne 'PASS'") < reason.index("$qualifiedExit -ne 0")
    assert reason.rstrip().endswith("else { $null }")
    # status 判定不得因新增原因而改變：qualification 失敗仍為 FAIL。
    assert "$ok = ($exitStatusKnown -and $selftestExit -eq 0 -and $qualifiedExit -eq 0 -and $report -and $report.verdict -eq 'PASS')" in block


def test_embedded_runtime_site_packages_include_pyserial():
    """SC-18 根因：B 輪 daemon 以 bundle 內嵌 3.12.3 runtime（-I）執行，必須能 import pyserial 才能開 COM。

    依賴實際所在目錄（可能是 user site）須加入 ._pth，且驗證 yaml 與 serial 皆可匯入；只驗 yaml 會讓
    COM 開啟在 daemon 內以 ATTACH_FAILED:OSError 失敗。
    """
    ps1 = WIN_BLACKBOX_PS1.read_text(encoding="utf-8-sig")
    block = _ps_function(ps1, "Enable-EmbeddedSitePackages")
    assert "import os, serial, yaml" in block
    assert "& (Join-Path $RuntimeRoot 'python.exe') -I -c 'import yaml, serial'" in block
    assert "-c 'import yaml'\n" not in block
