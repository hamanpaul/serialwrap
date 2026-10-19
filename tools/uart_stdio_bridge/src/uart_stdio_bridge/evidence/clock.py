"""時鐘識別碼產生器。"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import time


def clock_id() -> str:
    """產生本機單調時鐘識別碼。

    Linux 走 /proc/sys/kernel/random/boot_id；Windows 走 windows-qpc；其他為通用格式。
    """
    if sys.platform.startswith("linux"):
        boot_id_path = pathlib.Path("/proc/sys/kernel/random/boot_id")
        if boot_id_path.exists():
            boot_id = boot_id_path.read_text(encoding="utf-8").strip()
            return f"linux-monotonic:{boot_id}"
    elif sys.platform == "win32":
        return f"windows-qpc:{socket.gethostname()}:{int(time.time() - time.monotonic())}"
    return f"{sys.platform}-monotonic:{socket.gethostname()}:{os.getpid()}"
