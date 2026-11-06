"""Stage 1 出口摘要的 fail-closed 回歸（S1M-ba2 A2）。

只執行 stage1_exit_driver.sh 讀完各步 rc 之後的結尾判定，不跑出口證據 driver。
l15_exit_driver.py 以未捕捉例外結束、或 RC_SUMMARY 不是合法 JSON、缺少非負整數
failed_steps 時，這段結尾必須以非 0 結束。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "rust" / "stage1_exit_driver.sh"


def _exit_epilogue() -> str:
    """取出 main 裡從 l15 摘要賦值到函式結束前的實際判定。"""
    text = DRIVER.read_text(encoding="utf-8")
    needle = 'RC_SUMMARY="$(python3 scripts/rust/l15_exit_driver.py '
    position = text.index(needle)
    line_start = text.rfind("\n", 0, position) + 1
    end = text.index('\n}\n\nif [[ "${BASH_SOURCE[0]}" == "$0" ]]; then\n', position)
    return text[line_start:end]


def _run_exit_epilogue(tmp_path: Path, *, mode: str, summary: str = "") -> subprocess.CompletedProcess[str]:
    """以假 python3 只攔截 l15_exit_driver.py；摘要解析仍走系統 python。"""
    evidence = tmp_path / "ev"
    (evidence / "func-test-windows" / "basic").mkdir(parents=True)
    (evidence / "rc.txt").write_text("step 0\n", encoding="utf-8")
    (evidence / "stage1a-gate.json").write_text(
        json.dumps({"overall": "VERIFIED", "errors": [], "cases": []}),
        encoding="utf-8",
    )
    for relative in (
        "func-test-windows/func-test-report.json",
        "func-test-windows/basic/func-test-report.json",
        "func-test-windows/win-func-test-meta.json",
    ):
        (evidence / relative).write_text("{}", encoding="utf-8")

    real_python = shutil.which("python3")
    assert real_python, "找不到系統 python3"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "python3"
    fake.write_text(
        f"""#!/bin/bash
for arg in "$@"; do
  if [[ "$arg" == *scripts/rust/l15_exit_driver.py ]]; then
    if [[ "${{FAKE_L15_MODE}}" == "crash" ]]; then
      exec {shlex.quote(real_python)} -c '
import sys
from scripts.rust import l15_exit_driver

def boom(*_args, **_kwargs):
    raise RuntimeError("uncaught fixture")

l15_exit_driver.unresolved_steps = boom
raise SystemExit(l15_exit_driver.main(sys.argv[1:]))
' "${{@:2}}"
    fi
    printf '%s' "${{FAKE_L15_STDOUT-}}"
    exit 0
  fi
done
exec {shlex.quote(real_python)} "$@"
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    script = (
        "set -uo pipefail\n"
        f"REPO={shlex.quote(str(REPO_ROOT))}\n"
        f"EV={shlex.quote(str(evidence))}\n"
        f"LOG={shlex.quote(str(evidence / 'driver.log'))}\n"
        "log() { printf '%s\\n' \"$*\" >> \"$LOG\"; }\n"
        "epilogue() {\n"
        f"{_exit_epilogue()}\n"
        "}\n"
        "epilogue\n"
    )
    env = os.environ.copy()
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["FAKE_L15_MODE"] = mode
    env["FAKE_L15_STDOUT"] = summary
    return subprocess.run(
        ["bash", "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_nonzero(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, (
        "出口 driver 誤判通過 "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr[-800:]!r}"
    )


def test_uncaught_l15_exception_makes_exit_driver_nonzero(tmp_path: Path) -> None:
    result = _run_exit_epilogue(tmp_path, mode="crash")
    _assert_nonzero(result)


@pytest.mark.parametrize(
    "summary",
    [
        "",
        "not-json",
        "{}",
        "[]",
        "null",
        '{"failed_steps":true}',
        '{"failed_steps":"0"}',
        '{"failed_steps":-1}',
        '{"failed_steps":0.0}',
    ],
)
def test_invalid_or_incomplete_rc_summary_fails_exit_driver(summary: str, tmp_path: Path) -> None:
    result = _run_exit_epilogue(tmp_path, mode="print", summary=summary)
    _assert_nonzero(result)


def test_zero_failed_steps_still_passes_exit_driver(tmp_path: Path) -> None:
    summary = '{"failed_steps":0,"suppressed":[],"unresolved":[]}'
    result = _run_exit_epilogue(tmp_path, mode="print", summary=summary)
    assert result.returncode == 0, result.stderr[-800:]


def test_nonzero_failed_steps_fails_exit_driver(tmp_path: Path) -> None:
    result = _run_exit_epilogue(tmp_path, mode="print", summary='{"failed_steps":2}')
    _assert_nonzero(result)
