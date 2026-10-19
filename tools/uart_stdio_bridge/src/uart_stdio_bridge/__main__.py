"""uart_stdio_bridge 模組直接執行入口（`python -m uart_stdio_bridge`）。"""

from __future__ import annotations

from uart_stdio_bridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
