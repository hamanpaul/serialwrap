"""shell 腳本不得以 `\\\\` 結尾（雙反斜線不是續行，下一行會被當成新命令執行）。

候選版 36ef2d7b 的 `scripts/rust/win-func-test.sh:216` 曾以 `\\\\` 結尾，寫 meta 的命令被截斷、
下一行把 `$BUNDLE_IDENTITY_JSON` 當命令執行，`set -e` 以 exit 127 結束，ev-01 的 meta 永遠寫不出來。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOUBLE_BACKSLASH_EOL = re.compile(r"(?<!\\)\\\\$")

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def _shell_scripts() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.sh"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


def test_shell_scripts_have_no_double_backslash_line_end() -> None:
    offenders = []
    for path in _shell_scripts():
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if DOUBLE_BACKSLASH_EOL.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], f"以雙反斜線結尾的行（不是續行）：{offenders}"


def test_pattern_detects_double_but_not_single_backslash() -> None:
    assert DOUBLE_BACKSLASH_EOL.search('  "$a" "$b" \\\\')
    assert not DOUBLE_BACKSLASH_EOL.search('  "$a" "$b" \\')
