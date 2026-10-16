"""DUT 行為模型與模擬器套件。

提供 sans-IO DUT 行為模型、情境計畫、檔案目錄控制通道與 stdin/stdout 模擬器。
"""
from __future__ import annotations

from uart_stdio_bridge.dut.control_file import ControlFileChannel, DutControlClient
from uart_stdio_bridge.dut.model import (
    CommandOverride,
    DutModel,
    Emit,
    LoginStep,
    TargetConfig,
)
from uart_stdio_bridge.dut.plan import ScenarioPlan, Trigger
from uart_stdio_bridge.dut.stdio_adapter import run_echo, run_stdio

__all__ = [
    "CommandOverride",
    "ControlFileChannel",
    "DutControlClient",
    "DutModel",
    "Emit",
    "LoginStep",
    "ScenarioPlan",
    "TargetConfig",
    "Trigger",
    "run_echo",
    "run_stdio",
]
