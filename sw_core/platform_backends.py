from __future__ import annotations

import os
import sys


def _select(env_name: str, backend: str | None) -> str:
    """
    選擇後端字串。

    參數：
    - env_name：環境變數名稱（如 SERIALWRAP_RPC_BACKEND）
    - backend：明確指定的後端（優先度：明確指定 > 環境變數 > auto）

    回傳值：
    - "posix"：POSIX 後端
    - "win"：Windows 後端

    解析規則：
    - "posix"/"unix"/"termios" → posix
    - "win"/"windows"/"win32" → win
    - "auto" 或預設：Windows（os.name=="nt" 或 sys.platform.startswith("win")）回 win；其餘 posix
    """
    mode = (backend or os.environ.get(env_name) or "auto").lower()
    if mode in ("posix", "unix", "termios"):
        return "posix"
    if mode in ("win", "windows", "win32"):
        return "win"
    # auto：Windows 走 win，其餘維持 posix（生產路徑零回歸）。
    if os.name == "nt" or sys.platform.startswith("win"):
        return "win"
    return "posix"


def select_rpc_backend(backend: str | None = None) -> str:
    """
    選擇 RPC 後端。

    參數：
    - backend：明確指定的後端（優先度順序）；若為 None 則讀環境變數 SERIALWRAP_RPC_BACKEND

    回傳值：
    - "posix"：POSIX Unix socket RPC
    - "win"：Windows TCP RPC
    """
    return _select("SERIALWRAP_RPC_BACKEND", backend)


def select_lock_backend(backend: str | None = None) -> str:
    """
    選擇 lock 後端。

    參數：
    - backend：明確指定的後端（優先度順序）；若為 None 則讀環境變數 SERIALWRAP_LOCK_BACKEND

    回傳值：
    - "posix"：POSIX flock/fcntl lock
    - "win"：Windows mutex lock
    """
    return _select("SERIALWRAP_LOCK_BACKEND", backend)


def select_device_backend(backend: str | None = None) -> str:
    """
    選擇 device 後端。

    參數：
    - backend：明確指定的後端（優先度順序）；若為 None 則讀環境變數 SERIALWRAP_DEVICE_BACKEND

    回傳值：
    - "posix"：POSIX 設備路徑與偵測邏輯
    - "win"：Windows COM 與偵測邏輯
    """
    return _select("SERIALWRAP_DEVICE_BACKEND", backend)
