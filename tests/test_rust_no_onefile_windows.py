"""Windows 原生單檔解壓監看（ReadDirectoryChangesW）測試（P4-T16，審查 #5）。

僅於原生 Windows 執行（conftest 對 windows_native 項目自動 skip，理由為「證據不足，非 PASS」）；
獨立成檔以符合 core_portable 模組不得含平台 skip 的規則。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.windows_native,
    pytest.mark.process_rpc,
]

ROOT = Path(__file__).resolve().parents[1]
ONEFILE_SCRIPT = ROOT / "scripts" / "rust" / "check_no_onefile_extract.py"


def _run(tmp_path: Path, cli_text: str) -> dict:
    fake_cli = tmp_path / "fake_win.py"
    fake_cli.write_text(cli_text, encoding="utf-8")
    work = tmp_path / "work"
    sys_root = tmp_path / "sys"
    work.mkdir(exist_ok=True)
    sys_root.mkdir(exist_ok=True)
    res = subprocess.run(
        [
            sys.executable, str(ONEFILE_SCRIPT),
            "--cli", f"{sys.executable} {fake_cli}",
            "--cli-args", "",
            "--runs", "1",
            "--work-base", str(work),
            "--system-tmp", str(sys_root),
        ],
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    return json.loads(res.stdout.decode("utf-8"))


def test_windows_watcher_clean_cli_passes(tmp_path: Path) -> None:
    """正向對照：乾淨 CLI → ReadDirectoryChangesW 監看下 verdict PASS。"""
    data = _run(tmp_path, "print('ok')\n")
    assert data["watcher"] == "ReadDirectoryChangesW"
    assert data["verdict"] == "PASS", data


def test_windows_watcher_flags_create_then_delete(tmp_path: Path) -> None:
    """負向：建立後立即刪除的 x.exe 仍被記錄（不依賴檔案殘留）。"""
    data = _run(
        tmp_path,
        "import os\np = os.path.join(os.environ['TMP'], 'x.exe')\nopen(p, 'w').write('x')\nos.remove(p)\n",
    )
    assert data["verdict"] == "FAIL", data
    assert any("x.exe" in row["path"] for row in data["created"]), data["created"]
