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
SERIALWRAP_TEST_PREFIX = b"SERIALWRAP_TEST_"
SW_TEST_UPDATE_PATTERN = re.compile(rb"SW_TEST_UPDATE_[A-Za-z0-9_]*")
SERIALWRAP_TEST_PATTERN = re.compile(rb"SERIALWRAP_TEST_[A-Za-z0-9_]*")
# rustc/link.exe 可把相鄰的靜態字串放在同一個 PE 區段且不加 NUL；只為實際相鄰字串
# 精確列出尾綴，避免把已核准 hook 誤讀成更長的環境變數名稱。
ADJACENT_LITERAL_SUFFIXES = {
    "SERIALWRAP_TEST_SYSTEM_CONFIG_PATH": (b"config.yaml",),
    "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE": (b"Traceback (most call last):",),
}
DAEMON_NAMES = frozenset({"serialwrapd", "serialwrapd.exe"})


def _is_identifier_byte(value: int) -> bool:
    return 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122 or value == 95


def _identifier_at(data: bytes, start: int) -> bytes:
    end = start
    while end < len(data) and _is_identifier_byte(data[end]):
        end += 1
    return data[start:end]


def _allowed_daemon_marker(data: bytes, start: int) -> str | None:
    """只辨識精確 hook 名；不把任意同前綴的更長環境變數一併放行。"""
    for name in DAEMON_PARITY_HOOKS:
        marker = name.encode("ascii")
        if not data.startswith(marker, start):
            continue
        end = start + len(marker)
        if end == len(data) or not _is_identifier_byte(data[end]):
            return name
        # 相鄰的另一個 hook 會在其自己的 prefix 位置再次檢查；未知名稱仍會被拒。
        if data.startswith(SERIALWRAP_TEST_PREFIX, end):
            return name
        for suffix in ADJACENT_LITERAL_SUFFIXES.get(name, ()):
            after_suffix = end + len(suffix)
            if data.startswith(suffix, end) and (
                after_suffix == len(data) or not _is_identifier_byte(data[after_suffix])
            ):
                return name
    return None


def inspect_binary(path: pathlib.Path) -> list[str]:
    data = path.read_bytes()
    is_daemon = path.name in DAEMON_NAMES
    rejected: list[str] = []
    for match in SERIALWRAP_TEST_PATTERN.finditer(data):
        start = match.start()
        if is_daemon and _allowed_daemon_marker(data, start) is not None:
            continue
        rejected.append(_identifier_at(data, start).decode("ascii", errors="replace"))
    rejected.extend(
        match.group().decode("ascii", errors="replace")
        for match in SW_TEST_UPDATE_PATTERN.finditer(data)
    )
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
