from __future__ import annotations

# AF_UNIX RPC server 實作已搬至 rpc_posix（#84 PORT-4 平台 seam 分檔）。
# 保留此 shim 維持既有 `from sw_core.rpc import JsonRpcUnixServer` 與測試零改動。
from sw_core.rpc_posix import JsonRpcUnixServer

__all__ = ["JsonRpcUnixServer"]
