"""Slice 2.3 L1.5 gate 案例（P7；OD-34，2026-10-28 擁有者裁決）：依賴 P7 readiness reprobe／#162 再確認的三案收進
Slice 2.3 gate，須在 Rust daemon 下 PASS。

- `l15-delayed-reply`（P2 凍結情境）、`s21-reboot-minimal-guard`（P5 s21 情境）：`func-test/runner.py --mode bridge
  --scenarios --case <id>`。
- `cowork-01-agent-bad-cmd`（Slice 2.2 func-test 案例）：`--mode bridge --case <id>`；另依賴 P6 human console
  （`session.console_attach`），P6 合入前 Rust daemon 回 METHOD_NOT_FOUND（BLOCKED-UPSTREAM，見
  `docs/rust-refactor/reviews/p7-local-windows-todo.md` P7-BU-7）。
- `ev-01-spawn-handler`（daemon_slice 2.3）：Linux 列於本檔驗證；Windows 列（OD-31，SC-26 `cmd.exe /d /s /c` shell
  handler）須原生 PASS，列 LOCAL-WINDOWS-TODO P7-LW-7。

缺 daemon artifact 或 bridge 自測報告時 skip（EVIDENCE_MISSING）；`SERIALWRAP_GATE=1` 下缺證據即失敗，不得 skip。
`comparison.json` 缺漏或 verdict 非 PASS 一律 FAIL（不得轉 SKIP／PASS）。
"""
import json, os, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCENARIOS = ["l15-delayed-reply", "s21-reboot-minimal-guard"]
CASES = ["cowork-01-agent-bad-cmd", "ev-01-spawn-handler"]
GATE = os.environ.get("SERIALWRAP_GATE") == "1"


def _run(case: str, scenarios: bool, tmp_path: pathlib.Path) -> None:
    exe, report = os.environ.get("SERIALWRAP_L15_RUST_DAEMON"), os.environ.get("SERIALWRAP_L15_SELFTEST_REPORT")
    if not exe or not report:
        if GATE:
            pytest.fail("SERIALWRAP_L15_RUST_DAEMON/SERIALWRAP_L15_SELFTEST_REPORT not set under SERIALWRAP_GATE=1")
        pytest.skip("EVIDENCE_MISSING: artifact or bridge selftest report not provided")
    env = dict(os.environ, SERIALWRAP_DAEMON_EXE=exe, SERIALWRAP_AB_LABEL="rust-s23")
    out = tmp_path / "rust" / case
    argv = [sys.executable, "func-test/runner.py", "--mode", "bridge", "--case", case,
            "--selftest-report", report, "--report-dir", str(out)]
    if scenarios:
        argv.insert(4, "--scenarios")
    r = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    found = next(out.rglob("comparison.json"), None)
    assert found is not None, f"INSUFFICIENT_EVIDENCE: {case}/rust 無 comparison.json（rc={r.returncode}）\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
    verdict = json.loads(found.read_text(encoding="utf-8"))
    assert r.returncode == 0 and verdict["verdict"] == "PASS", verdict


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", SCENARIOS)
def test_scenario_rust(case, tmp_path):
    _run(case, True, tmp_path)


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", CASES)
def test_case_rust(case, tmp_path):
    _run(case, False, tmp_path)
