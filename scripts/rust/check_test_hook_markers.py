#!/usr/bin/env python3
"""正式 bundle 執行檔的測試環境鉤子檢查器（Windows／Linux 共用）。"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

# 只允許 Python Stage 0 使用的四個 daemon 測試環境鉤子；Rust 隔離守衛由 daemon 測試另行驗證。
# Windows 與 Linux bundle 建置器都呼叫本檔，勿在呼叫端另建放行清單。
DAEMON_PARITY_HOOKS = frozenset(
    {
        "SERIALWRAP_TEST_DEVICE_ALLOWLIST",
        "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH",
        "SERIALWRAP_TEST_KILL_AFTER_SAVE",
        "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE",
    }
)
# Rust daemon hook names are NUL-terminated CStr literals; keep the binary scan exact when linkers coalesce statics.
HOOK_PATTERNS = (
    ("SERIALWRAP_TEST_", re.compile(rb"SERIALWRAP_TEST_[A-Za-z0-9_]*")),
    ("SW_TEST_UPDATE_", re.compile(rb"SW_TEST_UPDATE_[A-Za-z0-9_]*")),
)
DAEMON_NAMES = frozenset({"serialwrapd", "serialwrapd.exe"})


def inspect_binary(path: pathlib.Path) -> list[str]:
    data = path.read_bytes()
    is_daemon = path.name in DAEMON_NAMES
    rejected: list[str] = []
    for prefix, pattern in HOOK_PATTERNS:
        for match in pattern.finditer(data):
            marker = match.group().decode("ascii", errors="replace")
            if prefix == "SERIALWRAP_TEST_" and is_daemon and marker in DAEMON_PARITY_HOOKS:
                continue
            rejected.append(marker)
    return list(dict.fromkeys(rejected))


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", action="append", required=True, type=pathlib.Path)
    args = parser.parse_args()

    errors: list[str] = []
    for path in args.binary:
        try:
            markers = inspect_binary(path)
        except OSError as exc:
            errors.append(f"無法讀取執行檔 {path}: {exc}")
            continue
        for marker in markers:
            errors.append(f"{path.as_posix()} 含測試鉤子 {marker}")

    if errors:
        for error in errors:
            print(f"check_test_hook_markers.py: {error}", file=sys.stderr)
        return 1
    print(f"測試鉤子檢查通過：{len(args.binary)} 個執行檔；daemon parity allowlist={len(DAEMON_PARITY_HOOKS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
