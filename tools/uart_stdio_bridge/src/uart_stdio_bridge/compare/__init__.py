"""uart_stdio_bridge.compare 模組（P2-T7）。

提供 WAL↔wire 比較器、correlation 與窗口判定、竄改拒絕與敏感資料遮罩。
"""

from __future__ import annotations

from uart_stdio_bridge.compare.comparator import (
    EXIT_CODES,
    VERDICTS,
    CompareInputs,
    check_contiguous,
    check_zero_tx,
    compare,
)
from uart_stdio_bridge.compare.integrity import verify_integrity
from uart_stdio_bridge.compare.redact import redact
from uart_stdio_bridge.compare.rules import APPROVED_RULES, load_rules
from uart_stdio_bridge.compare.wal_reader import WalRecord, list_wal_files, load_wal
from uart_stdio_bridge.compare.wire_reader import load_correlation, load_wire

__all__ = [
    "APPROVED_RULES",
    "CompareInputs",
    "EXIT_CODES",
    "VERDICTS",
    "WalRecord",
    "check_contiguous",
    "check_zero_tx",
    "compare",
    "list_wal_files",
    "load_correlation",
    "load_rules",
    "load_wal",
    "load_wire",
    "redact",
    "verify_integrity",
]
