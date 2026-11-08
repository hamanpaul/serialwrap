"""bridge 自測（§15.4.3／Spike 4）與資格報告模組。"""

from uart_stdio_bridge.selftest.checks import CHECK_IDS, CheckResult, SelftestContext, run_check
from uart_stdio_bridge.selftest.runner import is_qualified, load_approved_revision, run_selftest

__all__ = [
    "CHECK_IDS",
    "CheckResult",
    "SelftestContext",
    "run_check",
    "run_selftest",
    "load_approved_revision",
    "is_qualified",
]
