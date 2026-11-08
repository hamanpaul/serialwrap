"""DUT 組態型別重新匯出模組（供 Windows runner 與非 PTY 環境安全使用）。

僅依賴 Python 標準函式庫與 uart_stdio_bridge.dut.model，絕不匯入 pty 或 termios。
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BRIDGE_SRC = _ROOT / "tools" / "uart_stdio_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from uart_stdio_bridge.dut.model import (
    CommandOverride,
    LoginStep,
    TargetConfig,
)

__all__ = [
    "CommandOverride",
    "LoginStep",
    "TargetConfig",
]
