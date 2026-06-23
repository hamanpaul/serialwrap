#!/usr/bin/env python3
# 薄 shim：實作已搬入 sw_core.daemon，此檔保持回溯相容。
from sw_core.daemon import BLOCKING_RPC_METHODS, main  # noqa: F401  重新導出（tests/test_issue52_rpc_concurrency.py 直接存取 serialwrapd.BLOCKING_RPC_METHODS，勿移除）

if __name__ == "__main__":
    raise SystemExit(main())
