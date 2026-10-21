"""P3-T17：`scripts/rust/check_win_imports.py` 的匯入 DLL 判定邏輯。

以假 `x86_64-w64-mingw32-objdump`（依環境變數 FAKE_DLLS 輸出 `DLL Name:` 行）驅動，不需 Windows 或真實 mingw。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "check_win_imports.py"

FAKE_OBJDUMP = """#!/bin/sh
# 假 objdump：忽略參數，依 FAKE_DLLS（空白分隔）輸出 objdump -p 的 DLL Name 區段
echo "The Import Tables"
for d in $FAKE_DLLS; do
  echo ""
  printf '\\tDLL Name: %s\\n' "$d"
done
"""


def _assert_script() -> None:
    assert SCRIPT.is_file(), "scripts/rust/check_win_imports.py 尚未實作（P3-T17 Step 4）"


def _exe(tmp_path: Path, name: str = "serialwrap.exe") -> Path:
    exe = tmp_path / name
    exe.write_bytes(b"MZ")  # 判定只看 objdump 輸出，不解析 PE 內容
    return exe


def _run(tmp_path: Path, dlls: str) -> subprocess.CompletedProcess[str]:
    _assert_script()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if os.name == "nt":
        tool = bin_dir / "x86_64-w64-mingw32-objdump.cmd"
        tool.write_text(
            "@echo off\r\n"
            "echo The Import Tables\r\n"
            "for %%D in (%FAKE_DLLS%) do echo.    DLL Name: %%D\r\n",
            encoding="utf-8",
        )
    else:
        tool = bin_dir / "x86_64-w64-mingw32-objdump"
        tool.write_text(FAKE_OBJDUMP, encoding="utf-8")
        tool.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_DLLS": dlls}
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(_exe(tmp_path))],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_allowlisted_imports_ok(tmp_path: Path) -> None:
    r = _run(tmp_path, "api-ms-win-core-synch-l1-2-0.dll KERNEL32.dll")
    data = json.loads(r.stdout)
    assert r.returncode == 0
    assert data["ok"] is True
    assert data["unexpected"] == []
    assert data["forbidden"] == []
    assert set(data["imports"]) == {"api-ms-win-core-synch-l1-2-0.dll", "KERNEL32.dll"}


def test_forbidden_dll_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, "KERNEL32.dll libgcc_s_seh-1.dll")
    data = json.loads(r.stdout)
    assert r.returncode == 1
    assert data["ok"] is False
    assert "libgcc_s_seh-1.dll" in data["forbidden"]


def test_unexpected_dll_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, "KERNEL32.dll foo.dll")
    data = json.loads(r.stdout)
    assert r.returncode == 1
    assert data["ok"] is False
    assert "foo.dll" in data["unexpected"]


def test_dll_name_case_insensitive(tmp_path: Path) -> None:
    # allowlist 不分大小寫（arch §1.1）；api-ms-win-crt-* 與 api-ms-win-core-* 萬用字元成立
    r = _run(tmp_path, "kernel32.DLL bcryptprimitives.dll API-MS-WIN-CRT-STDIO-L1-1-0.DLL api-ms-win-core-synch-l1-2-0.dll")
    data = json.loads(r.stdout)
    assert r.returncode == 0
    assert data["ok"] is True


def test_missing_objdump_is_insufficient(tmp_path: Path) -> None:
    _assert_script()
    empty = tmp_path / "empty_path"
    empty.mkdir()
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(_exe(tmp_path))],
        capture_output=True,
        text=True,
        env={"PATH": str(empty)},  # 找不到 x86_64-w64-mingw32-objdump
        check=False,
    )
    assert r.returncode == 2  # 證據不足，不得 ok
    if r.stdout.strip():
        try:
            parsed = json.loads(r.stdout)
        except json.JSONDecodeError:
            parsed = {}
        assert parsed.get("ok") is not True
