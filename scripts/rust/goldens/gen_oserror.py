#!/usr/bin/env python3
"""產生 P3-T9 sw-pal client 側的 OSError 基準與 errno 名稱基準（golden）。

輸出（依執行平台擇一後綴 ``linux`` ｜ ``windows``）：
- ``contracts/golden/oserror.<os>.tsv``：每個操作的例外分類、``errno``、``winerror``、``strerror``、
  ``str(exc)`` 與 ``errno.errorcode`` 名稱。
- ``contracts/golden/errno_names.<os>.tsv``：``errno.errorcode`` 完整映射（含同值別名的勝出名稱）。

格式沿用 ``_tsv.py``：``case_id\\top\\thex(input)\\thex(expected)``。欄位內以 ``\\x1f`` 分隔子欄位。

input 子欄位：
- ``tmp:<相對路徑>``：每個 case 獨立暫存目錄下的路徑；``tmp:`` 為暫存目錄本身。
- ``lit:<路徑>``：字面路徑（不依賴暫存目錄）。
- timeout：``1.0`` 為 ``settimeout(1.0)``；``0`` 為 ``settimeout(0)``（非阻塞）。

expected 子欄位順序：``exc_class``、``errno``、``winerror``、``strerror``、``message``、``errno_name``。
無例外時 ``exc_class`` 為 ``NONE``，其餘子欄位為空字串。

平台差異：
- AF_UNIX 相關 case 只在 ``socket.AF_UNIX`` 存在的平台產生；Windows 版 Python 3.12 無此常數，不記錄假資料。
- Windows 的 ``file_eacces`` 以 ``CreateFileW`` 不共享方式開啟目標檔（sharing violation → EACCES），
  取代 POSIX mode 000 語意。

限制：拒絕以 root 執行（root 會繞過權限檢查，EACCES 案例失效）。

用法（repo 根目錄）::

    python3 scripts/rust/goldens/gen_oserror.py [--out-dir contracts/golden]
    py -3.12 gen_oserror.py --out-dir <dir>
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
# 不依賴 -I 以外的 sys.path 設定：明確加入腳本目錄以匯入 _tsv。
sys.path.insert(0, str(SCRIPT_DIR))

from _tsv import Row, write_tsv  # noqa: E402

SEP = "\x1f"
CLOSED_PORT = "1"
NONE_FIELDS: tuple[str, ...] = ("NONE", "", "", "", "", "")


def _platform_suffix() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    raise SystemExit(f"不支援的平台：{sys.platform}（僅產出 Linux 與 Windows 基準）")


def _scrub(text: str, tmp: str) -> str:
    """把隨機暫存目錄（含 repr 的反斜線跳脫形式）替換為佔位符，使 golden 可重現。"""
    for needle in {tmp, tmp.replace("\\", "\\\\")}:
        text = text.replace(needle, "<TMP>")
    return text


def _fields(exc: BaseException, tmp: str) -> tuple[str, ...]:
    code = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    return (
        type(exc).__name__,
        "" if code is None else str(code),
        "" if winerror is None else str(winerror),
        getattr(exc, "strerror", None) or "",
        _scrub(str(exc), tmp),
        "" if code is None else errno.errorcode.get(code, ""),
    )


def _measure(action: Callable[[], None], tmp: str) -> tuple[str, ...]:
    try:
        action()
    except Exception as exc:  # 只記錄 Python 例外語意，不吞 KeyboardInterrupt
        return _fields(exc, tmp)
    return NONE_FIELDS


def _path(label: str, tmp: str) -> str:
    kind, _, rel = label.partition(":")
    if kind == "tmp":
        return os.path.join(tmp, rel) if rel else tmp
    if kind == "lit":
        return rel
    raise ValueError(f"未知路徑標籤：{label!r}")


def _connect_unix(path: str, timeout: float) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
    finally:
        s.close()


def _recv_unix_nonblock(path: str) -> None:
    # 對不回應的 listener（bind+listen、不 accept）：connect 後 settimeout(0) 再 recv。
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        cli.connect(path)
        cli.settimeout(0)
        cli.recv(65536)
    finally:
        cli.close()
        srv.close()


def _connect_tcp(host: str, port: int, timeout: float) -> None:
    socket.create_connection((host, port), timeout).close()


def _resolve_tcp(host: str, port: int) -> None:
    socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)


def _file_read(path: str) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        fh.read()


def _listdir(path: str) -> None:
    os.listdir(path)


def _dispatch(op: str, inputs: tuple[str, ...], tmp: str) -> None:
    if op == "connect_unix":
        _connect_unix(_path(inputs[0], tmp), float(inputs[1]))
    elif op == "recv_unix_nonblock":
        _recv_unix_nonblock(_path(inputs[0], tmp))
    elif op == "connect_tcp":
        _connect_tcp(inputs[0], int(inputs[1]), float(inputs[2]))
    elif op == "resolve_tcp":
        _resolve_tcp(inputs[0], int(inputs[1]))
    elif op == "file_read":
        _file_read(_path(inputs[0], tmp))
    elif op == "listdir":
        _listdir(_path(inputs[0], tmp))
    else:
        raise ValueError(f"未知操作：{op!r}")


def _hold_exclusive_windows(path: str) -> Callable[[], None]:
    """以 CreateFileW（share mode 0）佔住檔案，使其他開啟回 sharing violation。"""
    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    open_existing = 3
    file_attribute_normal = 0x80
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(path, generic_read, 0, None, open_existing, file_attribute_normal, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW 失敗")
    return lambda: k32.CloseHandle(handle)


def _setup(case_id: str, tmp: str) -> Callable[[], None] | None:
    """建立 case 所需的檔案系統前置條件；回傳清理函式（可為 None）。"""
    if case_id == "unix_refused":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(tmp, "refused.sock"))
        s.close()  # 留下未監聽的 socket 檔
        return None
    if case_id == "unix_not_socket":
        Path(tmp, "plain.txt").write_bytes(b"x")
        return None
    if case_id == "unix_eacces":
        locked = os.path.join(tmp, "locked")
        os.mkdir(locked)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(locked, "s.sock"))
        s.close()
        os.chmod(locked, 0)
        return lambda: os.chmod(locked, 0o700)
    if case_id == "file_eacces":
        target = os.path.join(tmp, "locked.json")
        Path(target).write_bytes(b"{}")
        if sys.platform == "win32":
            return _hold_exclusive_windows(target)
        os.chmod(target, 0)
        return None
    return None


def _cases() -> list[tuple[str, str, tuple[str, ...]]]:
    cases: list[tuple[str, str, tuple[str, ...]]] = []
    if hasattr(socket, "AF_UNIX"):
        cases += [
            ("unix_enoent", "connect_unix", ("tmp:nope.sock", "1.0")),
            ("unix_refused", "connect_unix", ("tmp:refused.sock", "1.0")),
            ("unix_not_socket", "connect_unix", ("tmp:plain.txt", "1.0")),
            ("unix_eacces", "connect_unix", ("tmp:locked/s.sock", "1.0")),
            ("unix_too_long", "connect_unix", ("lit:/tmp/" + "x" * 200, "1.0")),
            ("unix_len_107", "connect_unix", ("lit:/" + "a" * 106, "1.0")),
            ("unix_len_108", "connect_unix", ("lit:/" + "a" * 107, "1.0")),
            ("unix_empty", "connect_unix", ("lit:", "1.0")),
            ("unix_recv_nonblock", "recv_unix_nonblock", ("tmp:silent.sock",)),
        ]
    cases += [
        ("tcp_refused", "connect_tcp", ("127.0.0.1", CLOSED_PORT, "1.0")),
        ("tcp_localhost_refused", "connect_tcp", ("localhost", CLOSED_PORT, "1.0")),
        ("tcp_gai", "connect_tcp", ("no-such-host.invalid", CLOSED_PORT, "1.0")),
        ("tcp_timeout0", "connect_tcp", ("127.0.0.1", CLOSED_PORT, "0")),
        ("tcp_gai", "resolve_tcp", ("no-such-host.invalid", CLOSED_PORT)),
        ("file_enoent", "file_read", ("tmp:missing.json",)),
        ("file_isdir", "file_read", ("tmp:",)),
        ("file_eacces", "file_read", ("tmp:locked.json",)),
        ("listdir_enoent", "listdir", ("tmp:missing-dir",)),
    ]
    return cases


def _run_case(case_id: str, op: str, inputs: tuple[str, ...]) -> tuple[str, ...]:
    tmp = tempfile.mkdtemp(prefix="swpal-golden-")
    closer: Callable[[], None] | None = None
    try:
        closer = _setup(case_id, tmp)
        return _measure(lambda: _dispatch(op, inputs, tmp), tmp)
    finally:
        if closer is not None:
            closer()
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default=str(ROOT / "contracts" / "golden"))
    args = parser.parse_args(argv)

    if sys.version_info[:2] != (3, 12):
        raise SystemExit(f"golden 必須以 Python 3.12 產生（目前 {sys.version.split()[0]}）")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("拒絕以 root 產生：EACCES 案例會失效")

    suffix = _platform_suffix()
    oserror_rows: list[Row] = []
    for case_id, op, inputs in _cases():
        expected = SEP.join(_run_case(case_id, op, inputs))
        oserror_rows.append((case_id, op, SEP.join(inputs), expected))

    errno_rows: list[Row] = [
        (f"errno_{code}", "errorcode", str(code), name)
        for code, name in sorted(errno.errorcode.items())
    ]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_tsv(out / f"oserror.{suffix}.tsv", oserror_rows)
    write_tsv(out / f"errno_names.{suffix}.tsv", errno_rows)
    print(f"python={sys.version.split()[0]} platform={sys.platform}")
    print(f"oserror.{suffix}.tsv rows={len(oserror_rows)}")
    print(f"errno_names.{suffix}.tsv rows={len(errno_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
