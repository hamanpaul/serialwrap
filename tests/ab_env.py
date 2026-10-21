"""黑箱 A/B 環境擷取。

模組層從 os.environ 擷取 A/B 選擇器變數；root conftest 於 pop 之前 import 本檔。
另提供既有套件共用的 daemon artifact 核對（P5-T20 Step 0，§10.4）：防止 Rust 輪
靜默跑到 Python daemon 而 PASS。stdlib-only，不 import 任何 repo 模組；pytest 僅在
核對失敗時於函式內延遲匯入。
"""
from __future__ import annotations

import os
import sys

AB_KEYS: tuple[str, ...] = (
    "SERIALWRAP_CLI_EXE",
    "SERIALWRAP_DAEMON_EXE",
    "SERIALWRAP_AB_LABEL",
    "SW_BASELINE_PYTHON",
)
AB_ENV: dict[str, str] = {k: os.environ[k] for k in AB_KEYS if k in os.environ}


def expected_daemon_exe() -> str:
    """受測 daemon 行程 /proc/<pid>/exe 應解析到的 realpath。

    SERIALWRAP_DAEMON_EXE 指向二進位（非 .py）時即該 artifact；未設或為 .py 時為
    sys.executable（預設 [sys.executable, serialwrapd.py] 的解譯器）。
    """
    daemon = AB_ENV.get("SERIALWRAP_DAEMON_EXE")
    if daemon and not daemon.endswith(".py"):
        return os.path.realpath(daemon)
    return os.path.realpath(sys.executable)


def assert_daemon_artifact(pid: int, *, readlink=os.readlink) -> None:
    """daemon 啟動後核對 /proc/<pid>/exe；不符即 pytest.fail("artifact mismatch")。"""
    import pytest

    expected = expected_daemon_exe()
    try:
        actual = os.path.realpath(readlink(f"/proc/{pid}/exe"))
    except OSError as exc:
        pytest.fail(f"artifact mismatch: 無法讀取 /proc/{pid}/exe（{exc}）")
    if actual != expected:
        pytest.fail(f"artifact mismatch: 期望 {expected}，實際 {actual}")
