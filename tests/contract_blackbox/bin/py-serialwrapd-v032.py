#!/usr/bin/env python3
"""以指定的 v0.3.2 source archive 啟動 Python daemon。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = os.environ.get("SW_PY032_ROOT")
    entry = Path(root) / "serialwrapd.py" if root else None
    if entry is None or not entry.is_file():
        print("SW_PY032_ROOT must name the extracted v0.3.2 source archive", file=sys.stderr)
        return 64
    command = [sys.executable, str(entry), *sys.argv[1:]]
    if os.name == "nt":
        # Windows 的 execv 會啟動子行程後讓 wrapper 正常返回；等待子 daemon，
        # 讓 harness 能以 wrapper PID 管理生命週期並取得真正的 exit status。
        return subprocess.call(command)
    os.execv(sys.executable, command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
