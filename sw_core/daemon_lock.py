from __future__ import annotations

# flock singleton 實作已搬至 lock_posix（#84 PORT-4 平台 seam 分檔）。
from sw_core.lock_posix import SingletonLock

__all__ = ["SingletonLock"]
