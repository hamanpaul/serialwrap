"""L1 必要集合的平台一致性：Linux 必要集合不得登記 Windows 原生測試。

只在 POSIX 執行：這裡檢查的是 Linux 集合在 Linux collection 下的結果；Windows 的 conftest
以不同規則套用平台 marker，在 Windows 上收集沒有意義。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_linux_l1_sets_exclude_windows_native_nodeids() -> None:
    """Linux 必要集合不得把原生 Windows 測試登記為必要案例。"""
    lock = json.loads((REPO_ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    for set_id in ("p7_gate:python3-m-pytest-q-tests", "s21_gate:pytest_full"):
        spec = lock["sets"][set_id]["linux"]
        selector = spec["selector"]
        argv = list(selector["argv"])
        markexpr = selector.get("markexpr", "").strip()
        windows_expr = "windows_native"
        if "-m" in argv:
            marker_index = argv.index("-m")
            argv[marker_index + 1] = f"({argv[marker_index + 1]}) and {windows_expr}"
        elif markexpr:
            argv.extend(["-m", f"({markexpr}) and {windows_expr}"])
        else:
            argv.extend(["-m", windows_expr])
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", *argv],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        windows_native = {
            line.strip()
            for line in res.stdout.splitlines()
            if line.strip().startswith("tests/") and "::" in line
        }
        assert windows_native, f"{set_id}.linux 的 selector 未收集到 windows_native 測試"
        invalid = sorted(windows_native & set(spec["nodeids"]))
        assert not invalid, (
            f"{set_id}.linux 將 {len(invalid)} 個 Windows 原生測試列為必要案例：{invalid[:5]}"
        )
