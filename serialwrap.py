#!/usr/bin/env python3
# 薄 shim：實作在 sw_core.cli，此檔以絕對 import 重新導出 main，
# 供 `python serialwrap.py` 與 PyInstaller entry 使用（sw_core/cli.py 為 package-relative
# import，不能直接當 __main__ 入口，否則 PyInstaller 會 ImportError，#84 PORT-4）。
from sw_core.cli import main  # noqa: F401  重新導出

if __name__ == "__main__":
    raise SystemExit(main())
