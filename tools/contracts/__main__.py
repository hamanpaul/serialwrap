"""契約工具命令列入口（`python3 -m tools.contracts`）。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    # 輸出契約為 UTF-8＋LF（DISK_STABLE）；原生 Windows 管線預設為 locale 編碼（如 cp950）
    # 且把 \n 轉為 \r\n，讀取端以 UTF-8 解碼會失敗，故在入口固定兩條串流的編碼與換行。
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            _reconfigure(encoding="utf-8", newline="\n")
    sys.exit(main())
