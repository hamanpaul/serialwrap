"""S1X G3：Windows func-test 與 spike3 staging wrapper 的靜態與 argv 契約。

涵蓋 scripts/rust/win-func-test.sh、scripts/rust/win-spike3.sh 與共用的 scripts/rust/win-stage-lib.sh：
- COM 只允許 COM20（serialwrap／受測端）與 COM21（bridge／測試端）；
- runner argv 逐字等於 plan 04 Step 5 的 Windows 形式；spike3 每案 argv 只含 plan 指定的參數；
- EDR 參數只原樣透傳呼叫者提供的值，wrapper 不自填；
- 不接觸 serialwrap-throwaway-* 工作、其 serialwrapd 或任何映像名稱的行程終止；
- 輸入不完整或自測報告不符時在任何 interop 呼叫之前即 fail closed。

本檔不需要 Windows 或 WSL interop：只使用 --print-argv、拒絕路徑與靜態掃描，不會啟動 Windows 行程或開啟序列埠。
實機證據（func-test-report.json、spike3 證據 JSON）由 wrapper 寫出，本檔不斷言其判定。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FUNC = ROOT / "scripts" / "rust" / "win-func-test.sh"
SPIKE3 = ROOT / "scripts" / "rust" / "win-spike3.sh"
LIB = ROOT / "scripts" / "rust" / "win-stage-lib.sh"
PLAN04 = ROOT / "docs" / "rust-refactor" / "plans" / "04-stage1a-rust-cli.md"
ALLOWED_COMS = {"COM20", "COM21"}
SPIKE3_CASES = [
    "win-wal-av-rotate",
    "win-wal-av-unlink",
    "win-wal-av-reader-handle",
    "win-wal-f12-progress",
]
EXPECTED_FUNC_ARGV = [
    "-3.12", "func-test/runner.py", "--mode", "bridge", "--scenarios",
    "--daemon-com", "COM20", "--bridge-com", "COM21", "--transport-mode", "virtual_com0com",
    "--selftest-report", "<RUN>\\selftest-report.json", "--report-dir", "<RUN>\\report",
]
REQUIRED_SUBSET = {
    "func-test", "realhw", "sw_core", "tools/uart_stdio_bridge", "contracts", "tests/acceptance",
    "scripts/stage0/spike3_win_wal.py", "scripts/stage0/spike3_evidence.schema.json",
    "scripts/stage0/wal_microbench.py",
    "serialwrap.py", "serialwrapd.py", "VERSION",
}

pytestmark = [pytest.mark.posix_only]
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _print_argv(script: Path, *args: str) -> list:
    out = _run(script, "--print-argv", *args)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _fake_bundle(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "serialwrap.exe").write_bytes(b"not-a-real-exe")
    (root / "bin" / "current-release.json").write_text("{}\n", encoding="utf-8")
    return root


@needs_bash
@pytest.mark.parametrize("script", [FUNC, SPIKE3, LIB])
def test_scripts_parse(script: Path) -> None:
    out = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr


def test_com_tokens_limited_to_com20_com21() -> None:
    found: set[str] = set()
    for path in (FUNC, SPIKE3, LIB):
        found |= set(re.findall(r"\bCOM\d+\b", path.read_text(encoding="utf-8")))
    assert found <= ALLOWED_COMS, found
    assert {"COM20", "COM21"} <= found


@needs_bash
def test_func_test_argv_matches_plan_step5() -> None:
    assert _print_argv(FUNC) == EXPECTED_FUNC_ARGV
    plan = PLAN04.read_text(encoding="utf-8")
    fixed = ("py -3.12 func-test/runner.py --mode bridge --scenarios --daemon-com COM20 "
             "--bridge-com COM21 --transport-mode virtual_com0com")
    assert fixed in plan, "plan 04 Step 5 的 Windows 命令已變更；wrapper argv 須同步"


@needs_bash
def test_spike3_argv_per_case_is_exact() -> None:
    rows = _print_argv(SPIKE3)
    assert [row[row.index("--case") + 1] for row in rows] == SPIKE3_CASES
    for row, cid in zip(rows, SPIKE3_CASES, strict=True):
        assert row == [
            "-3.12", "scripts/stage0/spike3_win_wal.py", "--case", cid,
            "--evidence-dir", "<RUN>\\spike3",
        ]


@needs_bash
def test_spike3_edr_values_are_pass_through_only() -> None:
    for row in _print_argv(SPIKE3):
        assert not any(arg.startswith("--edr-") for arg in row), row
    rows = _print_argv(SPIKE3, "--edr-product", "Example EDR", "--edr-interceptions", "7")
    for row in rows:
        assert "--edr-status" not in row
        assert row[-4:] == ["--edr-product", "Example EDR", "--edr-interceptions", "7"]


def test_no_throwaway_scheduler_or_name_based_kill() -> None:
    for path in (FUNC, SPIKE3, LIB):
        text = path.read_text(encoding="utf-8")
        assert "throwaway" not in text.lower(), path.name
        assert "schtasks" not in text.lower(), path.name
        assert not re.search(r"taskkill[^\n]*/IM", text, re.IGNORECASE), path.name
        assert "Stop-Process" not in text, path.name
        assert "-Name serialwrap" not in text, path.name


def test_lib_kills_only_by_pid_and_start_identity() -> None:
    text = LIB.read_text(encoding="utf-8")
    assert "taskkill.exe /PID $proc.Id /T /F" in text
    assert "taskkill.exe /PID $targetId /T /F" in text
    assert "Get-Process -Id" in text
    assert "StartTime.ToUniversalTime().Ticks" in text


def test_lib_subset_covers_runner_and_spike3_dependencies() -> None:
    text = LIB.read_text(encoding="utf-8")
    block = re.search(r"SW_WIN_SUBSET=\((.*?)\n\)", text, re.DOTALL)
    assert block is not None, "找不到 SW_WIN_SUBSET"
    entries = {line.strip() for line in block.group(1).splitlines()
               if line.strip() and not line.strip().startswith("#")}
    assert REQUIRED_SUBSET <= entries


def test_lib_stages_head_archive_not_worktree() -> None:
    text = LIB.read_text(encoding="utf-8")
    assert 'git -C "$ROOT" archive --format=tar HEAD' in text
    assert "--skip-worktree" in text


@needs_bash
def test_func_refuses_incomplete_bundle_before_interop(tmp_path: Path) -> None:
    selftest = tmp_path / "selftest-report.json"
    selftest.write_text("{}\n", encoding="utf-8")
    out = _run(FUNC, "--bundle", str(tmp_path / "missing"), "--selftest-report", str(selftest),
               "--report-dir", str(tmp_path / "r"))
    assert out.returncode == 2
    assert "bundle 不完整" in out.stderr


@needs_bash
def test_func_refuses_mismatched_selftest_before_interop(tmp_path: Path) -> None:
    bundle = _fake_bundle(tmp_path / "bundle")
    selftest = tmp_path / "selftest-report.json"
    selftest.write_text(json.dumps({
        "verdict": "FAIL", "complete": True, "endpoint": "win-serial",
        "transport_mode": "virtual_com0com", "platform": {"com_pair": ["COM20", "COM21"]},
    }), encoding="utf-8")
    out = _run(FUNC, "--bundle", str(bundle), "--selftest-report", str(selftest),
               "--report-dir", str(tmp_path / "r"))
    assert out.returncode == 2
    assert "自測報告不符" in out.stderr


@needs_bash
def test_func_rejects_com_arguments(tmp_path: Path) -> None:
    out = _run(FUNC, "--daemon-com", "COM22", "--bridge-com", "COM23")
    assert out.returncode == 2
    assert "未知參數" in out.stderr


@needs_bash
def test_spike3_requires_evidence_dir_before_interop() -> None:
    out = _run(SPIKE3)
    assert out.returncode == 2
    assert "缺少 --evidence-dir" in out.stderr
