"""Stage 3 共用基礎替身中需要真實子行程的測試。

memory_only 不得啟動子行程（tests/conftest.py 的 marker 定義與 tests/test_resource_markers.py 稽核），
所以 stub CLI 的實際執行（POSIX shebang＋0755、Windows 同名 .cmd 包裝）獨立放在 process_rpc 檔案，
比照 Task 1 的 tests/test_stage3_proc.py。
"""

from __future__ import annotations

import importlib
import os
import subprocess

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


def _fakes():
    """延遲載入新替身，讓紅燈明確落在缺少的 stage3_fakes 模組。"""
    return importlib.import_module("stage3_fakes")


def _stub_exe(stub):
    return stub.with_suffix(".cmd") if os.name == "nt" else stub


def _run(exe, *args):
    return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=60)


def test_stub_cli_logs_calls(tmp_path):
    fakes = _fakes()
    stub_dir = tmp_path / "stub"
    stub = fakes.make_stub_cli(
        stub_dir,
        {
            "session list": [
                {"rc": 0, "stdout": "first\n", "stderr": ""},
                {"rc": 0, "stdout": "second\n", "stderr": ""},
            ]
        },
    )
    exe = _stub_exe(stub)
    assert exe.is_file()
    if os.name != "nt":
        assert os.access(exe, os.X_OK), "POSIX stub 必須可直接執行（shebang＋0755）"

    outputs = []
    for _ in range(2):
        result = _run(exe, "session", "list")
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())

    assert len(fakes.read_calls(stub_dir)) == 2
    assert outputs == ["first", "second"]


def test_stub_cli_strips_global_flags_before_lookup(tmp_path):
    fakes = _fakes()
    stub_dir = tmp_path / "stub"
    stub = fakes.make_stub_cli(stub_dir, {"daemon status": [{"rc": 3, "stdout": "{\"ok\":false}\n", "stderr": "busy\n"}]})
    exe = _stub_exe(stub)

    result = _run(exe, "--socket", "sock", "--retries", "2", "--timeout", "5", "-v", "daemon", "status")

    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, "{\"ok\":false}", "busy")
    calls = fakes.read_calls(stub_dir)
    assert len(calls) == 1
    assert calls[0]["argv"][-2:] == ["daemon", "status"]
