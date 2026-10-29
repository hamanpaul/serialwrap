"""P7 Task 19：`p7_gate.sh` 兩個 Windows 列的 win-test.sh 參數可被解析（審查 P7-cd7acf0-5）。

`win-test.sh` 只在 `--` 之後接受 cargo 參數；以 `--dry-run`（插在 `--` 之前）執行解析階段，不需 Windows。
"""
from __future__ import annotations

import pathlib
import re
import shlex
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


@pytest.mark.parametrize("var", ["WIN_NONCOM_ARGS", "WIN_COM_ARGS"])
def test_gate_win_test_argv_parses(var):
    text = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    m = re.search(rf"^{var}=\((.*)\)$", text, re.M)
    assert m, var
    args = shlex.split(m.group(1))
    cut = args.index("--")
    r = subprocess.run(["bash", str(ROOT / "scripts/rust/win-test.sh"), *args[:cut], "--dry-run", *args[cut:]],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (var, r.stderr)
    assert "未知參數" not in r.stderr, (var, r.stderr)
    if var == "WIN_COM_ARGS":
        assert "--test cap_windows_com_p7" in r.stdout, r.stdout


def test_gate_func_comparison_lookup_finds_nested_report(tmp_path):
    """func-test 列取 comparison.json 的那一行須找到 runner 的實際深度 report/<case>/bridge/<n>/（第 12 輪實跑：
    舊 glob 只到兩層，PASS 的 brcm-01-login-flow 被記成 rc 1）。"""
    text = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    line = next(ln.strip() for ln in text.splitlines() if ln.strip().startswith('cmp="$('))
    nested = tmp_path / "report" / "brcm-01-login-flow" / "bridge" / "0"
    nested.mkdir(parents=True)
    (nested / "comparison.json").write_text("{}", encoding="utf-8")
    r = subprocess.run(["bash", "-c", f'd="{tmp_path}"; {line}; printf %s "$cmp"'], capture_output=True, text=True, timeout=30)
    assert r.stdout == str(nested / "comparison.json"), (r.stdout, r.stderr)
