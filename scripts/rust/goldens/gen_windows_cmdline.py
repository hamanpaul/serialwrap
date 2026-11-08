#!/usr/bin/env python3
"""以 Windows Python 與 CommandLineToArgvW 重建 Windows argv golden。"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path
from ctypes import wintypes


def command_line_to_argv(command_line: str) -> list[str]:
    """解析命令列並釋放 CommandLineToArgvW 配置的緩衝區。"""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    parser = shell32.CommandLineToArgvW
    parser.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    argc = ctypes.c_int()
    argv = parser(command_line, ctypes.byref(argc))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free(ctypes.cast(argv, ctypes.c_void_p))


def main() -> None:
    if os.name != "nt":
        raise SystemExit("此 golden 產生器須在 Windows Python 執行。")

    root = Path(__file__).resolve().parents[3]
    golden_path = root / "contracts" / "golden" / "windows_cmdline.ndjson"
    cases = [json.loads(line)["args"] for line in golden_path.read_text(encoding="utf-8").splitlines() if line]
    rows = []
    for args in cases:
        joined = subprocess.list2cmdline(args)
        # argv[0] 有特殊解析規則；前綴虛擬程式名稱，讓 native parser 只解析參數，再丟棄該首項。
        command_line = "serialwrap-golden" + (f" {joined}" if joined else "")
        split = command_line_to_argv(command_line)[1:]
        rows.append(
            json.dumps(
                {"args": args, "joined": joined, "split": split},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    golden_path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
