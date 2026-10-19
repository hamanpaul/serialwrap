"""uart_stdio_bridge — serialwrap 獨立外部 bridge 與 DUT 測試基礎工具（P2）。"""

from __future__ import annotations

__version__: str = "0.1.0"
TOOL_NAME: str = "uart-stdio-bridge"
WIRE_SCHEMA: str = "bridge.v1"
CONTROL_SCHEMA: str = "control.v1"
CORRELATION_SCHEMA: str = "correlation.v1"
COMPARISON_SCHEMA: str = "comparison.v1"
MANIFEST_SCHEMA: str = "l15-manifest.v1"
BRIDGE_MANIFEST_SCHEMA: str = "bridge-manifest.v1"

__all__ = [
    "__version__",
    "TOOL_NAME",
    "WIRE_SCHEMA",
    "CONTROL_SCHEMA",
    "CORRELATION_SCHEMA",
    "COMPARISON_SCHEMA",
    "MANIFEST_SCHEMA",
    "BRIDGE_MANIFEST_SCHEMA",
]
