#!/usr/bin/env python3
"""Rust CLI 不得在執行期解壓檔案或目錄的檢查（P4-T16，規格 §11.6「連續 100 次 CLI 執行不因啟動新增解壓執行檔或目錄」）。

每次執行：
- 建立全新空目錄 D_i，並把 TMP／TEMP／TMPDIR 指向 D_i。
- CLI 行程存活期間，以檔案系統事件遞迴監看 D_i 與系統暫存根，記錄任何「建立」事件（不限名稱、含建立後立即刪除者）。
- 執行前後快照比對（系統暫存根遞迴）：CLI 結束後 D_i 仍殘留的項目列為 leftover。

監看實作（皆 stdlib＋ctypes）：
- Linux：inotify；新子目錄出現時補加監看並收編其既有內容（降低建立後立即寫入的競態漏報）。
- Windows：ReadDirectoryChangesW（bWatchSubtree，OVERLAPPED），記錄 FILE_ACTION_ADDED／RENAMED_NEW_NAME。

歸屬與判定（R5 審查後續）：
- D_i 內的事件皆歸屬於 CLI，計入 created／leftover，任一即 FAIL。
- 系統暫存根為共用目錄時，其他行程的建立事件無法與 CLI 區分；此類事件列為 system_tmp_unattributed，判 INSUFFICIENT
  （不算 created，也不得判 PASS）。`--system-tmp <dir>` 宣告該目錄為本次獨占，其事件即歸屬於 CLI，計入 FAIL。
- 監看失敗（無法加監看、事件佇列溢位、看門狗執行緒異常）一律 INSUFFICIENT，不得判 PASS。

私有 /tmp 隔離（`--private-tmp`，Linux；S1X Y5，比照 OD-25 隔離精神）：
- 共用 /tmp 會混入其他程序的建立事件而恆判 INSUFFICIENT。`--private-tmp` 以 `unshare -Um --map-current-user
  --keep-caps` 建立 user+mount namespace，把 `--private-tmp-base` 下全新的空目錄 bind 到 /tmp，再以 `setpriv`
  清空 capability 後於其中重新執行本工具：namespace 內的 /tmp 宣告為獨占（D_i 亦在其下），任何建立事件、殘留
  或新增項目都歸屬 CLI 判 FAIL——包含無視 TMPDIR、寫死 /tmp 的解壓器；namespace 外的 /tmp 活動看不到也不計入。
- 進入 namespace 後先核對 /tmp 確為該私有目錄（st_dev／st_ino）且為空，否則 INSUFFICIENT。
- 無法建立 namespace（缺 unshare／setpriv／mount、非 Linux、內層失敗）→ INSUFFICIENT，不退回共用 /tmp。
- 受測 CLI、本工具或 `--private-tmp-base` 位於 /tmp 下（namespace 內看不到）→ 用法錯誤（exit 2）。

判定 verdict：FAIL（created／leftover／獨占系統暫存根的新增項目／非零退出）、INSUFFICIENT（上列證據不足）、PASS（皆無）。
ok 僅在 verdict 為 PASS 時為 true。

輸出 JSON：{"runs","verdict","ok","created","leftover","system_tmp_new","system_tmp_unattributed",
"nonzero_exits","errors","insufficient_reasons","watcher","system_tmp","system_tmp_private","argv"}；
`--private-tmp` 另含 `"tmp_isolation":"mount-ns-private-tmp"`（未能隔離時為 null）。
退出碼：報告成功寫出即 0（判定以 JSON 的 `verdict`／`ok` 為準）；用法或工具錯誤為 2。
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence

# --- inotify 常數（Linux）---
IN_CREATE = 0x00000100
IN_MOVED_TO = 0x00000080
IN_ISDIR = 0x40000000
IN_Q_OVERFLOW = 0x00004000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
CREATE_MASK = IN_CREATE | IN_MOVED_TO
EVENT_HEADER = struct.Struct("iIII")  # wd, mask, cookie, len

# --- ReadDirectoryChangesW 常數（Windows）---
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ_WRITE_DELETE = 0x0007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_ACTION_ADDED = 1
FILE_ACTION_RENAMED_NEW_NAME = 5
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_OPERATION_ABORTED = 995
WIN_NOTIFY_FILTER = FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
WIN_BUFFER_BYTES = 65536


class _EventSink:
    """執行緒安全的事件收集器；監看器將建立事件、溢位與失敗寫入此處。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, str]] = []
        self._overflow = False
        self._failures: list[str] = []

    def record(self, path: Path, is_dir: bool) -> None:
        with self._lock:
            self._events.append({"path": str(path), "kind": "dir" if is_dir else "file"})

    def overflow(self) -> None:
        with self._lock:
            self._overflow = True

    def fail(self, message: str) -> None:
        with self._lock:
            self._failures.append(message)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"events": list(self._events), "overflow": self._overflow, "failures": list(self._failures)}


class InotifyWatcher:
    """inotify 建立事件監看（遞迴；新子目錄出現時補加監看並收編既有內容）。"""

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        self._libc = ctypes.CDLL(libc_name, use_errno=True)
        self._fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 失敗")
        self._lock = threading.Lock()
        self._wd_paths: dict[int, Path] = {}
        self._sink = _EventSink()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="inotify-pump", daemon=True)

    def add(self, path: Path) -> None:
        import ctypes

        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(str(path)), CREATE_MASK)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch 失敗：{path}")
        with self._lock:
            self._wd_paths[wd] = path

    def add_tree(self, root: Path, strict: bool = True) -> None:
        """監看 root 與其既有子目錄（之後新出現的子目錄由事件處理補加）。

        root 本身加監看失敗一律拋出；子目錄失敗在 strict=False 時記為證據不足並續行（例如系統暫存根內的私有目錄）。
        """
        self.add(root)
        for dirpath, dirnames, _filenames in os.walk(root):
            for name in dirnames:
                child = Path(dirpath) / name
                try:
                    self.add(child)
                except OSError as exc:
                    if strict:
                        raise
                    self._sink.fail(f"無法加監看子目錄：{child}（{exc}）")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=10)
        while self._drain_once(timeout=0.0):
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = -1
        return self._sink.snapshot()

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def _pump(self) -> None:
        while not self._stop.is_set():
            self._drain_once(timeout=0.05)

    def _drain_once(self, timeout: float) -> bool:
        if self._fd < 0:
            return False
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not ready:
            return False
        try:
            buf = os.read(self._fd, 65536)
        except BlockingIOError:
            return False
        self._handle(buf)
        return True

    def _handle(self, buf: bytes) -> None:
        offset = 0
        while offset + EVENT_HEADER.size <= len(buf):
            wd, mask, _cookie, length = EVENT_HEADER.unpack_from(buf, offset)
            name_raw = buf[offset + EVENT_HEADER.size: offset + EVENT_HEADER.size + length]
            offset += EVENT_HEADER.size + length
            if mask & IN_Q_OVERFLOW:
                self._sink.overflow()
                continue
            name = name_raw.rstrip(b"\0").decode("utf-8", "surrogateescape")
            with self._lock:
                base = self._wd_paths.get(wd)
            if base is None or not name or not (mask & CREATE_MASK):
                continue
            path = base / name
            is_dir = bool(mask & IN_ISDIR)
            self._sink.record(path, is_dir)
            if is_dir and not self._stop.is_set():
                self._adopt(path)

    def _adopt(self, path: Path) -> None:
        """新子目錄：補加監看，並收編加監看前已出現的內容（視為執行期建立）。"""
        try:
            self.add(path)
        except OSError as exc:
            self._sink.fail(f"無法補加監看：{path}（{exc}）")
            return
        try:
            children = list(os.scandir(path))
        except OSError:
            return
        for entry in children:
            child = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            self._sink.record(child, is_dir)
            if is_dir:
                self._adopt(child)


def _win_api() -> dict[str, Any]:
    """載入 kernel32 介面（僅於 Windows 呼叫）。"""
    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadDirectoryChangesW.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED), ctypes.c_void_p,
    ]
    k32.ReadDirectoryChangesW.restype = wintypes.BOOL
    k32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
    ]
    k32.GetOverlappedResult.restype = wintypes.BOOL
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.ResetEvent.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.CancelIoEx.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return {"k32": k32, "OVERLAPPED": OVERLAPPED, "wintypes": wintypes, "ctypes": ctypes}


class _WinRootWatch:
    """單一根目錄的 ReadDirectoryChangesW 監看（bWatchSubtree=TRUE，遞迴涵蓋所有子目錄）。"""

    def __init__(self, api: dict[str, Any], root: Path, sink: _EventSink) -> None:
        self._api = api
        self._root = root
        self._sink = sink
        self._handle: Any = None
        self._event: Any = None
        self._stop = threading.Event()
        self._buf = api["ctypes"].create_string_buffer(WIN_BUFFER_BYTES)
        self._ov: Any = None
        self._thread = threading.Thread(target=self._run, name=f"rdcw-{root.name}", daemon=True)

    def open(self) -> None:
        k32 = self._api["k32"]
        ctypes = self._api["ctypes"]
        invalid = ctypes.c_void_p(-1).value
        handle = k32.CreateFileW(
            str(self._root),
            FILE_LIST_DIRECTORY,
            FILE_SHARE_READ_WRITE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
            None,
        )
        if not handle or handle == invalid:
            raise OSError(ctypes.get_last_error(), f"CreateFileW 失敗：{self._root}")
        self._handle = handle
        self._event = k32.CreateEventW(None, True, False, None)
        if not self._event:
            raise OSError(ctypes.get_last_error(), "CreateEventW 失敗")

    def _issue(self) -> None:
        k32 = self._api["k32"]
        ctypes = self._api["ctypes"]
        wt = self._api["wintypes"]
        self._ov = self._api["OVERLAPPED"]()
        self._ov.hEvent = self._event
        self._returned = wt.DWORD(0)
        ok = k32.ReadDirectoryChangesW(
            self._handle,
            self._buf,
            WIN_BUFFER_BYTES,
            True,
            WIN_NOTIFY_FILTER,
            ctypes.byref(self._returned),
            ctypes.byref(self._ov),
            None,
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), f"ReadDirectoryChangesW 失敗：{self._root}")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        k32 = self._api["k32"]
        ctypes = self._api["ctypes"]
        wt = self._api["wintypes"]
        try:
            self._issue()
            while not self._stop.is_set():
                rc = k32.WaitForSingleObject(self._event, 50)
                if rc == WAIT_TIMEOUT:
                    continue
                if rc != WAIT_OBJECT_0:
                    self._sink.fail(f"WaitForSingleObject 回傳 {rc}：{self._root}")
                    return
                got = wt.DWORD(0)
                if not k32.GetOverlappedResult(self._handle, ctypes.byref(self._ov), ctypes.byref(got), False):
                    err = ctypes.get_last_error()
                    if err == ERROR_OPERATION_ABORTED:
                        return
                    self._sink.fail(f"GetOverlappedResult 失敗（{err}）：{self._root}")
                    return
                if got.value == 0:
                    self._sink.overflow()
                else:
                    self._parse(bytes(self._buf.raw[: got.value]))
                k32.ResetEvent(self._event)
                self._issue()
        except OSError as exc:
            self._sink.fail(str(exc))

    def _parse(self, data: bytes) -> None:
        offset = 0
        while True:
            next_off, action, name_len = struct.unpack_from("<III", data, offset)
            raw = data[offset + 12: offset + 12 + name_len]
            name = raw.decode("utf-16-le", "surrogatepass")
            if action in (FILE_ACTION_ADDED, FILE_ACTION_RENAMED_NEW_NAME) and name:
                path = self._root / name
                self._sink.record(path, os.path.isdir(path))
            if next_off == 0:
                break
            offset += next_off

    def stop(self) -> None:
        self._stop.set()
        k32 = self._api["k32"]
        if self._handle:
            k32.CancelIoEx(self._handle, None)
        self._thread.join(timeout=10)
        if self._handle:
            k32.CloseHandle(self._handle)
            self._handle = None
        if self._event:
            k32.CloseHandle(self._event)
            self._event = None


class WindowsWatcher:
    """ReadDirectoryChangesW 監看器（ctypes）；介面與 InotifyWatcher 相同。"""

    def __init__(self) -> None:
        self._api = _win_api()
        self._sink = _EventSink()
        self._roots: list[_WinRootWatch] = []

    def add_tree(self, root: Path, strict: bool = True) -> None:
        """ReadDirectoryChangesW 以 subtree 監看涵蓋整棵子樹；strict 參數與 InotifyWatcher 相容。"""
        del strict
        watch = _WinRootWatch(self._api, root, self._sink)
        watch.open()
        self._roots.append(watch)

    def start(self) -> None:
        for watch in self._roots:
            watch.start()

    def stop(self) -> dict[str, Any]:
        for watch in self._roots:
            watch.stop()
        return self._sink.snapshot()

    def close(self) -> None:
        for watch in self._roots:
            watch.stop()


def make_watcher() -> InotifyWatcher | WindowsWatcher:
    return WindowsWatcher() if os.name == "nt" else InotifyWatcher()


def watcher_name() -> str:
    return "ReadDirectoryChangesW" if os.name == "nt" else "inotify"


def _split_words(text: str) -> list[str]:
    """切分命令字串：POSIX 用 shlex；Windows 保留反斜線路徑並去除外層引號。"""
    if os.name != "nt":
        return shlex.split(text)
    words = []
    for tok in shlex.split(text, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        words.append(tok)
    return words


def _split_cli(cli: str) -> list[str]:
    if Path(cli).is_file():
        return [cli]
    return _split_words(cli)


def _system_tmp_root() -> Path:
    if os.name == "posix" and Path("/tmp").is_dir():
        return Path("/tmp")
    return Path(tempfile.gettempdir())


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _tree_snapshot(root: Path, exclude: Path) -> set[str]:
    """遞迴快照 root 下所有項目（排除 exclude 子樹）。"""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _within(here / d, exclude)]
        for name in dirnames + filenames:
            found.add(str(here / name))
    return found


def _leftover_in(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            found.append(str(Path(dirpath) / name))
    return sorted(found)


def _dedupe(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for row in rows:
        seen.setdefault(row["path"], row)
    return [seen[key] for key in sorted(seen)]


def run_checks(
    cli_argv: list[str],
    runs: int,
    cli_args: list[str],
    work_base: Path,
    system_tmp: Path | None = None,
) -> dict[str, Any]:
    private = system_tmp is not None
    sys_root = (system_tmp if private else _system_tmp_root()).resolve()
    base = work_base.resolve()
    report: dict[str, Any] = {
        "runs": runs,
        "argv": cli_argv + cli_args,
        "created": [],
        "leftover": [],
        "system_tmp_new": [],
        "system_tmp_unattributed": [],
        "nonzero_exits": [],
        "errors": [],
        "insufficient_reasons": [],
        "watcher": watcher_name(),
        "system_tmp": str(sys_root),
        "system_tmp_private": private,
    }
    if not sys_root.is_dir():
        report["insufficient_reasons"].append(f"system_tmp_missing：{sys_root}")

    raw_created: list[dict[str, str]] = []
    raw_unattr: list[dict[str, str]] = []
    for idx in range(runs):
        run_dir = Path(tempfile.mkdtemp(prefix=f"sw-onefile-{idx}-", dir=str(base))).resolve()
        # 獨占系統暫存根才做快照比對（共用根的快照無法歸屬，且遞迴走訪成本高）。
        sys_before = _tree_snapshot(sys_root, run_dir) if private and sys_root.is_dir() else set()
        watcher = make_watcher()
        # 監看失敗不跳過本次執行：仍執行 CLI，但結果判為證據不足（不得 PASS）。
        try:
            watcher.add_tree(run_dir)
        except OSError as exc:
            report["insufficient_reasons"].append(f"run {idx}: watch_failed（D_i）：{exc}")
        if sys_root.is_dir():
            try:
                watcher.add_tree(sys_root, strict=False)
            except OSError as exc:
                report["insufficient_reasons"].append(f"run {idx}: watch_failed（系統暫存根）：{exc}")
        watcher.start()

        env = dict(os.environ)
        env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(run_dir)
        proc = subprocess.run(cli_argv + cli_args, env=env, capture_output=True)
        if proc.returncode != 0:
            report["nonzero_exits"].append({"run": idx, "rc": proc.returncode})

        result = watcher.stop()
        for failure in result["failures"]:
            report["insufficient_reasons"].append(f"run {idx}: watcher_failure：{failure}")
        if result["overflow"]:
            report["insufficient_reasons"].append(f"run {idx}: watch_overflow")
        for event in result["events"]:
            row = {"run": idx, "path": event["path"], "kind": event["kind"]}
            if _within(Path(event["path"]), run_dir) or private:
                raw_created.append(row)
            else:
                raw_unattr.append(row)

        for left in _leftover_in(run_dir):
            report["leftover"].append({"run": idx, "path": left})
        if private and sys_root.is_dir():
            for path in sorted(_tree_snapshot(sys_root, run_dir) - sys_before):
                report["system_tmp_new"].append({"run": idx, "path": path})
        shutil.rmtree(run_dir, ignore_errors=True)

    report["created"] = [dict(r) for r in _dedupe_with_run(raw_created)]
    report["system_tmp_unattributed"] = _dedupe_with_run(raw_unattr) + report["system_tmp_unattributed"]
    if not private and report["system_tmp_unattributed"]:
        report["insufficient_reasons"].append("system_tmp_unattributed：共用系統暫存根的事件無法歸屬 CLI（請以 --system-tmp 指定獨占目錄）")

    if report["created"] or report["leftover"] or report["system_tmp_new"] or report["nonzero_exits"]:
        verdict = "FAIL"
    elif report["insufficient_reasons"] or report["errors"]:
        verdict = "INSUFFICIENT"
    else:
        verdict = "PASS"
    report["verdict"] = verdict
    report["ok"] = verdict == "PASS"
    return report


def _dedupe_with_run(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        seen.setdefault((row["run"], row["path"]), row)
    return [seen[key] for key in sorted(seen, key=lambda k: (k[0], k[1]))]


PRIVATE_TMP_ENV = "SW_ONEFILE_PRIVATE_TMP_ID"  # 內層：外層私有目錄的 "st_dev:st_ino"
TMP_ISOLATION = "mount-ns-private-tmp"


def _insufficient_report(cli_argv: list[str], runs: int, cli_args: list[str], reason: str) -> dict[str, Any]:
    """無法執行（隔離失敗）時的報告：不執行 CLI，verdict INSUFFICIENT。"""
    return {
        "runs": runs, "argv": cli_argv + cli_args, "created": [], "leftover": [], "system_tmp_new": [],
        "system_tmp_unattributed": [], "nonzero_exits": [], "errors": [], "insufficient_reasons": [reason],
        "watcher": watcher_name(), "system_tmp": "/tmp", "system_tmp_private": False, "tmp_isolation": None,
        "verdict": "INSUFFICIENT", "ok": False,
    }


def _under_tmp(path: Path) -> bool:
    """path 是否位於 /tmp 下：不跟隨 symlink 的字面路徑與 resolve 後路徑，任一在 /tmp 下即是（namespace 內兩者皆看不到）。"""
    literal = Path(os.path.abspath(path))
    return (_within(literal, Path("/tmp")) or _within(literal, Path("/tmp").resolve())
            or _within(path.resolve(), Path("/tmp").resolve()))


def _private_tmp_outer(args: argparse.Namespace, cli_argv: list[str], cli_args: list[str]) -> dict[str, Any]:
    """外層：建立私有目錄與 user+mount namespace，於其中重新執行本工具並取回報告。"""
    script = Path(__file__).resolve()
    # 內層以 sys.executable 執行本工具；直譯器、本工具與受測 CLI 的任何絕對路徑字詞都必須在 namespace 內可見。
    for word in [sys.executable, os.path.abspath(__file__), str(script), *cli_argv]:
        if word.startswith("/") and _under_tmp(Path(word)):
            raise ValueError(f"--private-tmp：{word} 位於 /tmp 下，namespace 內看不到")
    if not sys.platform.startswith("linux"):
        return _insufficient_report(cli_argv, args.runs, cli_args, f"private_tmp_unavailable：平台 {sys.platform} 不支援")
    tools = {name: shutil.which(name) for name in ("unshare", "setpriv", "mount")}
    missing = sorted(name for name, path in tools.items() if path is None)
    if missing:
        return _insufficient_report(cli_argv, args.runs, cli_args, f"private_tmp_unavailable：找不到 {missing}")
    base_text = args.private_tmp_base or os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    base = Path(base_text)
    if _under_tmp(base):
        raise ValueError(f"--private-tmp-base 不得位於 /tmp 下：{base}")
    base.mkdir(parents=True, exist_ok=True)
    priv = Path(tempfile.mkdtemp(prefix="sw-onefile-privtmp-", dir=str(base))).resolve()
    try:
        st = os.stat(priv)
        inner = [sys.executable, str(script), "--cli", args.cli, "--runs", str(args.runs),
                 f"--cli-args={args.cli_args}", "--private-tmp"]  # = 形式：值以 - 開頭（預設 --version）時不被當成旗標
        cmd = [tools["unshare"], "-Um", "--map-current-user", "--keep-caps", "--",
               "/bin/sh", "-c", 'm="$1"; p="$2"; shift 2; "$m" --bind "$p" /tmp && exec "$@"', "sh",
               tools["mount"], str(priv),
               tools["setpriv"], "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", "--", *inner]
        env = dict(os.environ)
        env[PRIVATE_TMP_ENV] = f"{st.st_dev}:{st.st_ino}"
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        try:
            report = json.loads(proc.stdout) if proc.returncode == 0 else None
        except json.JSONDecodeError:
            report = None
        if not isinstance(report, dict):
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            return _insufficient_report(cli_argv, args.runs, cli_args,
                                        f"private_tmp_failed：內層 rc={proc.returncode} stderr={tail}")
        return report
    finally:
        shutil.rmtree(priv, ignore_errors=True)


def _private_tmp_inner(cli_argv: list[str], runs: int, cli_args: list[str], expected_id: str) -> dict[str, Any]:
    """內層（namespace 內）：核對 /tmp 為外層私有目錄且為空，再以獨占 /tmp 執行檢查。"""
    tmp = Path("/tmp")
    st = os.stat(tmp)
    if f"{st.st_dev}:{st.st_ino}" != expected_id:
        return _insufficient_report(cli_argv, runs, cli_args, "private_tmp_failed：namespace 內 /tmp 不是外層私有目錄")
    if any(tmp.iterdir()):
        return _insufficient_report(cli_argv, runs, cli_args, "private_tmp_failed：namespace 內 /tmp 執行前非空")
    report = run_checks(cli_argv, runs, cli_args, tmp, tmp)
    report["tmp_isolation"] = TMP_ISOLATION
    return report


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI 單檔執行期解壓檢查（P4-T16）")
    parser.add_argument("--cli", required=True, help="CLI 執行檔路徑，或含參數的命令字串")
    parser.add_argument("--runs", type=int, default=100, help="執行次數（預設 100）")
    parser.add_argument("--cli-args", default="--version", help="傳給 CLI 的參數字串（預設 --version）")
    parser.add_argument("--work-base", help="放置各次 D_i 的父目錄（預設系統暫存根）")
    parser.add_argument(
        "--system-tmp",
        help="獨占的系統暫存根目錄（宣告本次獨占，其事件即歸屬於 CLI）；未指定時共用系統暫存根，事件判 INSUFFICIENT",
    )
    parser.add_argument(
        "--private-tmp", action="store_true",
        help="Linux：於 user+mount namespace 內以 bind 進來的全新空目錄作為獨占 /tmp 執行（S1X Y5）",
    )
    parser.add_argument("--private-tmp-base", help="--private-tmp 私有目錄的父目錄（不得位於 /tmp；預設 XDG_RUNTIME_DIR）")
    parser.add_argument("--json-out", help="報告 JSON 輸出路徑")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.runs < 1:
        parser.error("--runs 須 ≥1")
    if args.private_tmp and (args.system_tmp or args.work_base):
        parser.error("--private-tmp 不可與 --system-tmp／--work-base 併用（namespace 內固定以 /tmp 為獨占根）")
    if args.private_tmp_base and not args.private_tmp:
        parser.error("--private-tmp-base 須搭配 --private-tmp")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cli_argv = _split_cli(args.cli)
        if args.private_tmp:
            cli_args = _split_words(args.cli_args)
            inner_id = os.environ.get(PRIVATE_TMP_ENV)
            if inner_id:
                report = _private_tmp_inner(cli_argv, args.runs, cli_args, inner_id)
            else:
                report = _private_tmp_outer(args, cli_argv, cli_args)
            return _emit(report, args.json_out)
        work_base = Path(args.work_base) if args.work_base else _system_tmp_root()
        work_base.mkdir(parents=True, exist_ok=True)
        system_tmp = Path(args.system_tmp) if args.system_tmp else None
        if system_tmp is not None:
            system_tmp.mkdir(parents=True, exist_ok=True)
        report = run_checks(cli_argv, args.runs, _split_words(args.cli_args), work_base, system_tmp)
    except (OSError, ValueError) as exc:
        print(f"check_no_onefile_extract: {exc}", file=sys.stderr)
        return 2
    return _emit(report, args.json_out)


def _emit(report: dict[str, Any], json_out: str | None) -> int:
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(text + "\n", encoding="utf-8")
    # 強制 UTF-8 輸出：Windows 主控台預設 cp950 無法表示部分字元，且報告含繁中說明。
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
