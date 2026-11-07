"""Windows bundle 驗證工具的內層 SHA256SUMS 解析：重複列與格式錯誤都不得靜默放行。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rust" / "mkbundle_windows.py"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _module():
    spec = importlib.util.spec_from_file_location("mkbundle_windows", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_valid_lines_are_parsed() -> None:
    listed, errors = _module().parse_sha256sums(f"{DIGEST_A}  bin/a.exe\n{DIGEST_B}  py/b.py\n")
    assert errors == []
    assert listed == {"bin/a.exe": DIGEST_A, "py/b.py": DIGEST_B}


def test_duplicate_path_is_rejected() -> None:
    _listed, errors = _module().parse_sha256sums(f"{DIGEST_A}  bin/a.exe\n{DIGEST_B}  bin/a.exe\n")
    assert any("重複" in error and "bin/a.exe" in error for error in errors), errors


@pytest.mark.parametrize(
    "line",
    [
        "not-a-digest  bin/a.exe",
        f"{DIGEST_A} bin/a.exe",
        f"{DIGEST_A.upper()}  bin/a.exe",
        f"{DIGEST_A[:-1]}  bin/a.exe",
        DIGEST_A,
    ],
)
def test_malformed_line_is_rejected(line: str) -> None:
    listed, errors = _module().parse_sha256sums(f"{line}\n")
    assert errors, line
    assert "bin/a.exe" not in listed


@pytest.mark.parametrize("path", ["../evil", "/etc/passwd", "bin\\a.exe"])
def test_illegal_path_is_rejected(path: str) -> None:
    _listed, errors = _module().parse_sha256sums(f"{DIGEST_A}  {path}\n")
    assert any("路徑不合法" in error for error in errors), path
