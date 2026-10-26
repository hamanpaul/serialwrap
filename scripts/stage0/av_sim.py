#!/usr/bin/env python3
"""OD-19：可重現的「模擬端點防毒」WAL 干擾（stdlib 與 ctypes；不依賴任何防毒產品或其主控台資料）。

行為模型（與防毒掃描器對新檔案的典型行為相同）：
- 監看 WAL 目錄；新檔或新段落（名稱或 inode 首次出現）一出現，就以「不含 FILE_SHARE_DELETE」的共用模式
  開檔（FILE_SHARE_READ｜FILE_SHARE_WRITE），隨機持有 ``HOLD_MS_MIN``～``HOLD_MS_MAX`` 毫秒後關閉。
- 持有期間，WAL 對該檔的 rename（rotate）會因共用違規失敗（ERROR_SHARING_VIOLATION＝32，Python 形式為
  PermissionError）；寫入（FILE_APPEND_DATA）仍被允許。這屬暫時性干擾，WAL 必須以有界退避最終完成輪替、
  不遺失紀錄、寫入不阻塞。
- ``seed`` 決定每次持有時長的序列，同 seed 可重現。

兩種實作：
- ``AvSimWalFs``：注入式 WalFs（``core_portable``，任何 OS 皆可執行），以注入的邏輯時鐘套用同一持有模型，
  包裝任一既有 WalFs（預設為當前平台的 WAL 後端）。
- ``run_helper``：Windows 原生 helper 行程（``windows_native``，只能在擁有者 Windows host 以
  ``scripts/stage0/win-pytest.sh`` 或 spike3 執行）。非 Windows 以退出碼 ``EXIT_NOT_WINDOWS`` 拒絕。

oplock 變體（FSCTL_REQUEST_OPLOCK）：``--oplock`` 以允許 delete 的共用模式開啟 WAL 檔，對檔案提出
overlapped Read-Handle（RH）oplock request；helper 記錄 granted、denied 或 unavailable。一般模式仍以
不含 FILE_SHARE_DELETE 的共用模式重現輪替／刪除干擾。

用法（Windows helper）：
  py -3.12 scripts/stage0/av_sim.py --watch-dir <WAL 目錄> --seed <N> --duration-s <秒> \\
      --ready-file <檔> --stop-file <檔> --log-file <檔> --go-file <檔> --ack-file <檔>
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import random
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOLD_MS_MIN = 10
HOLD_MS_MAX = 500
WATCH_PREFIXES: tuple[str, ...] = ("raw.wal.ndjson", "raw.mirror.log")
WATCH_POLL_S = 0.005
READY_REPLACE_RETRIES = 40
READY_REPLACE_RETRY_S = 0.025
MODE = "share_no_delete"
SHARE_MODE_TEXT = "FILE_SHARE_READ|FILE_SHARE_WRITE"
OPLOCK_STATUS = "not_requested"
EXIT_NOT_WINDOWS = 3
ERROR_SHARING_VIOLATION = 32
ERROR_IO_PENDING = 997
ERROR_OPLOCK_NOT_GRANTED = 300
ERROR_CANNOT_GRANT_REQUESTED_OPLOCK = 801

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FSCTL_REQUEST_OPLOCK = (9 << 16) | (144 << 2)
REQUEST_OPLOCK_CURRENT_VERSION = 1
REQUEST_OPLOCK_INPUT_FLAG_REQUEST = 0x00000001
OPLOCK_LEVEL_CACHE_READ = 0x00000001
OPLOCK_LEVEL_CACHE_HANDLE = 0x00000002


class REQUEST_OPLOCK_INPUT_BUFFER(ctypes.Structure):
    _fields_ = [
        ("StructureVersion", ctypes.c_ushort),
        ("StructureLength", ctypes.c_ushort),
        ("RequestedOplockLevel", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
    ]


class REQUEST_OPLOCK_OUTPUT_BUFFER(ctypes.Structure):
    _fields_ = [
        ("StructureVersion", ctypes.c_ushort),
        ("StructureLength", ctypes.c_ushort),
        ("OriginalOplockLevel", ctypes.c_uint32),
        ("NewOplockLevel", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("AccessMode", ctypes.c_uint32),
        ("ShareMode", ctypes.c_ushort),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


@dataclass
class OplockLease:
    """保持 overlapped oplock request 所需的 handle 與結構存活。"""

    handle: int
    event: int | None = None
    overlapped: _OVERLAPPED | None = None
    output: REQUEST_OPLOCK_OUTPUT_BUFFER | None = None


@dataclass
class HeldFile:
    """helper 暫時持有的 WAL 檔與其 seeded 到期時間。"""

    until: float
    lease: OplockLease
    name: str


class HoldSchedule:
    """以 seed 決定的持有時長序列（毫秒整數，落在 [HOLD_MS_MIN, HOLD_MS_MAX]）。"""

    def __init__(self, seed: int) -> None:
        self.seed = int(seed)
        self._rng = random.Random(self.seed)

    def next_hold_s(self) -> float:
        return self._rng.randint(HOLD_MS_MIN, HOLD_MS_MAX) / 1000.0


def describe(seed: int, *, oplock: bool = False) -> dict[str, Any]:
    """證據用的 av_sim 參數區塊（spike3 與測試共用，欄位名稱與 spike3_evidence.schema.json 對齊）。"""
    return {
        "mode": "oplock" if oplock else MODE,
        "share_mode": "FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE" if oplock else SHARE_MODE_TEXT,
        "hold_ms_range": [HOLD_MS_MIN, HOLD_MS_MAX],
        "seed": int(seed),
        "oplock": "pending" if oplock else OPLOCK_STATUS,
    }


def is_watched(name: str) -> bool:
    return name.startswith(WATCH_PREFIXES)


def _file_key(path: str) -> tuple[str, int, int]:
    """名稱加 (dev, inode)：同名但新 inode 視為新檔（rotate 後的新現行檔）；寫入不改變此鍵。"""
    st = os.stat(path)
    return (os.path.basename(path), int(st.st_dev), int(st.st_ino))


def _sharing_violation(path: str) -> PermissionError:
    exc = PermissionError(errno.EACCES, "sharing violation (av_sim: no FILE_SHARE_DELETE)", path)
    exc.winerror = ERROR_SHARING_VIOLATION  # type: ignore[attr-defined]
    return exc


class AvSimWalFs:
    """注入式 WalFs：包裝既有 WalFs，對 WAL 目錄內新出現的檔案套用 seeded 持有模型。

    僅 rename（rotate）受干擾；open_append、size、identity 等皆照常轉交。持有以注入時鐘計時，
    故測試可用邏輯時間精確推進，不依賴真實 sleep。
    """

    def __init__(self, *, clock: Callable[[], float], seed: int, inner: Any | None = None) -> None:
        if inner is None:
            from sw_core.wal_fs import default_wal_fs  # 延遲匯入：本模組頂層不相依 sw_core

            inner = default_wal_fs()
        self._inner = inner
        self._clock = clock
        self._schedule = HoldSchedule(seed)
        self._seen: set[tuple[str, int, int]] = set()
        self._held: dict[str, float] = {}  # 檔名 → 釋放時間（注入時鐘）
        self.grabs: list[dict[str, Any]] = []
        self.denied = 0

    @property
    def seed(self) -> int:
        return self._schedule.seed

    def _expire(self) -> None:
        now = self._clock()
        for name, until in list(self._held.items()):
            if now >= until:
                del self._held[name]

    def _observe(self, path: str) -> None:
        self._expire()
        directory = os.path.dirname(path) or "."
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not is_watched(name):
                continue
            full = os.path.join(directory, name)
            try:
                key = _file_key(full)
            except OSError:
                continue
            if key in self._seen:
                continue
            self._seen.add(key)
            hold = self._schedule.next_hold_s()
            self._held[name] = self._clock() + hold
            self.grabs.append({"name": name, "hold_ms": int(round(hold * 1000)), "at": self._clock()})

    def open_append(self, path: str) -> Any:
        self._observe(path)
        return self._inner.open_append(path)

    def rename_noreplace_or_suffix(self, src: str, base_dst: str) -> str:
        self._observe(src)
        self._expire()
        if os.path.basename(src) in self._held:
            self.denied += 1
            raise _sharing_violation(src)
        return self._inner.rename_noreplace_or_suffix(src, base_dst)

    def path_identity(self, path: str) -> Any:
        return self._inner.path_identity(path)

    def size(self, path: str) -> int | None:
        return self._inner.size(path)

    def tail_needs_termination(self, path: str) -> bool:
        return self._inner.tail_needs_termination(path)

    def sync_dir(self, path: str) -> None:
        self._inner.sync_dir(path)

    def ensure_dir(self, path: str) -> None:
        self._inner.ensure_dir(path)

    def dir_writable(self, path: str) -> bool:
        return self._inner.dir_writable(path)


# ---------------------------------------------------------------------------
# Windows 原生 helper 行程（以 ctypes 開檔；非 Windows 不可執行）
# ---------------------------------------------------------------------------


def _open_share_hold(path: str) -> int:
    """以不含 FILE_SHARE_DELETE 的共用模式開啟並回傳 handle（Windows 限定）。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    handle = create(path, _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                    _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle in (None, _INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def _request_oplock(path: str, *, kernel32: Any | None = None,
                    last_error: Callable[[], int] | None = None) -> tuple[OplockLease, str]:
    """以允許 delete 的 overlapped handle 申請 RH oplock，回傳 lease 與明確結果。"""
    api = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
    get_error = last_error or ctypes.get_last_error
    create = api.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    handle = create(path, _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                    None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OVERLAPPED, None)
    if handle in (None, _INVALID_HANDLE_VALUE):
        raise ctypes.WinError(get_error())
    file_handle = int(handle)

    create_event = api.CreateEventW
    create_event.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    create_event.restype = ctypes.c_void_p
    event = create_event(None, 1, 0, None)
    if event in (None, _INVALID_HANDLE_VALUE):
        error = get_error()
        _close_handle(file_handle)
        raise ctypes.WinError(error)
    event_handle = int(event)

    request = REQUEST_OPLOCK_INPUT_BUFFER(
        REQUEST_OPLOCK_CURRENT_VERSION,
        ctypes.sizeof(REQUEST_OPLOCK_INPUT_BUFFER),
        OPLOCK_LEVEL_CACHE_READ | OPLOCK_LEVEL_CACHE_HANDLE,
        REQUEST_OPLOCK_INPUT_FLAG_REQUEST,
    )
    output = REQUEST_OPLOCK_OUTPUT_BUFFER()
    overlapped = _OVERLAPPED()
    overlapped.hEvent = ctypes.c_void_p(event_handle)
    ioctl = api.DeviceIoControl
    ioctl.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(_OVERLAPPED),
    ]
    ioctl.restype = ctypes.c_int
    granted = ioctl(
        ctypes.c_void_p(file_handle), FSCTL_REQUEST_OPLOCK,
        ctypes.byref(request), ctypes.sizeof(request),
        ctypes.byref(output), ctypes.sizeof(output), None, ctypes.byref(overlapped),
    )
    if not granted:
        error = get_error()
        if error == ERROR_IO_PENDING:
            return OplockLease(file_handle, event_handle, overlapped, output), "granted"
        _close_handle(event_handle)
        status = "denied" if error in (ERROR_OPLOCK_NOT_GRANTED, ERROR_CANNOT_GRANT_REQUESTED_OPLOCK) else "unavailable"
        return OplockLease(file_handle), status
    # Synchronous completion is not the pending request required for an active oplock lease.
    _close_handle(event_handle)
    return OplockLease(file_handle), "unavailable"


def _close_lease(lease: OplockLease) -> None:
    """先關閉檔案以結束 oplock request，再關閉其 notification event。"""
    _close_handle(lease.handle)
    if lease.event is not None:
        _close_handle(lease.event)


def _publish_atomic_record(destination: str | Path, record: dict[str, Any]) -> None:
    """同目錄完整寫入後原子發布；Windows 暫時共用拒絕時做有界重試。"""
    destination = Path(destination)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        for attempt in range(READY_REPLACE_RETRIES):
            try:
                os.replace(temp_path, destination)
                break
            except PermissionError:
                if attempt + 1 >= READY_REPLACE_RETRIES:
                    raise
                time.sleep(READY_REPLACE_RETRY_S)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _publish_ready_record(ready_file: str | Path, held_files: list[str]) -> None:
    """先完整寫入同目錄暫存檔，再原子發布首次掃描結果。"""
    _publish_atomic_record(ready_file, {"ready": True, "held_files": held_files})


def _process_rearm_request(
    *, watch_dir: str, go_file: str, ack_file: str, seen_request_ids: set[str],
    holds: list[HeldFile], schedule: HoldSchedule, counts: dict[str, int],
    events: list[dict[str, Any]], oplock: bool,
) -> None:
    """依 consumer 握手重新開啟既存 WAL，直接忽略首次掃描的 seen 去重。"""
    try:
        request = json.loads(Path(go_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    request_id = request.get("request_id") if isinstance(request, dict) else None
    name = request.get("name") if isinstance(request, dict) else None
    if (not isinstance(request_id, str) or not request_id or request_id in seen_request_ids
            or not isinstance(name, str) or not is_watched(name)):
        return
    seen_request_ids.add(request_id)
    full = os.path.join(watch_dir, name)
    response: dict[str, Any] = {
        "request_id": request_id, "held_file": None, "hold_ms": None,
        "acquired_ns": None, "until_ns": None,
    }
    try:
        key = _file_key(full)
        if key[0] != name:
            raise FileNotFoundError(name)
        for entry in list(holds):
            if entry.name == name:
                _close_lease(entry.lease)
                holds.remove(entry)
        hold = schedule.next_hold_s()
        if oplock:
            lease, oplock_result = _request_oplock(full)
        else:
            lease, oplock_result = OplockLease(_open_share_hold(full)), OPLOCK_STATUS
        acquired_ns = time.monotonic_ns()
        until_ns = acquired_ns + int(round(hold * 1_000_000_000))
        holds.append(HeldFile(until_ns / 1_000_000_000, lease, name))
        counts["grabs"] += 1
        events.append({"event": "rearm", "name": name, "hold_ms": int(round(hold * 1000)),
                       "oplock": oplock_result, "request_id": request_id,
                       "acquired_ns": acquired_ns, "until_ns": until_ns})
        response.update({"held_file": name, "hold_ms": int(round(hold * 1000)),
                         "acquired_ns": acquired_ns, "until_ns": until_ns})
    except OSError as exc:
        counts["open_errors"] += 1
        events.append({"event": "rearm_error", "name": name, "error": str(exc), "request_id": request_id})
        response["error"] = f"{type(exc).__name__}: {exc}"
    _publish_atomic_record(ack_file, response)


def run_helper(*, watch_dir: str, seed: int, duration_s: float, ready_file: str,
               stop_file: str, log_file: str, go_file: str, ack_file: str,
               oplock: bool = False) -> int:
    """Windows helper：監看 WAL 目錄並對新檔套用持有模型，直到逾時或 stop_file 出現。"""
    if os.name != "nt":
        print("av_sim helper 只能在原生 Windows 執行（需 CreateFileW 共用模式）", file=sys.stderr)
        return EXIT_NOT_WINDOWS
    schedule = HoldSchedule(seed)
    events: list[dict[str, Any]] = []
    holds: list[HeldFile] = []
    seen: set[tuple[str, int, int]] = set()
    counts = {"grabs": 0, "open_errors": 0}
    oplock_results: list[str] = []
    seen_request_ids: set[str] = set()
    ready_written = False
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline and not os.path.exists(stop_file):
        now = time.monotonic()
        for entry in list(holds):
            if now >= entry.until:
                _close_lease(entry.lease)
                holds.remove(entry)
        try:
            names = os.listdir(watch_dir)
        except OSError:
            names = []
        for name in names:
            if not is_watched(name):
                continue
            full = os.path.join(watch_dir, name)
            try:
                key = _file_key(full)
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            hold = schedule.next_hold_s()
            try:
                if oplock:
                    lease, oplock_result = _request_oplock(full)
                    oplock_results.append(oplock_result)
                else:
                    lease = OplockLease(_open_share_hold(full))
            except OSError as exc:
                counts["open_errors"] += 1
                events.append({"event": "open_error", "name": name, "error": str(exc)})
                continue
            holds.append(HeldFile(now + hold, lease, name))
            counts["grabs"] += 1
            events.append({"event": "grab", "name": name, "hold_ms": int(round(hold * 1000)),
                           "oplock": oplock_result if oplock else OPLOCK_STATUS})
        if not ready_written:
            # ready 是首次掃描完成的確認；既有 WAL 也已在此之前取得 seeded handle。
            _publish_ready_record(ready_file, sorted({entry.name for entry in holds}))
            ready_written = True
        _process_rearm_request(
            watch_dir=watch_dir, go_file=go_file, ack_file=ack_file,
            seen_request_ids=seen_request_ids, holds=holds, schedule=schedule,
            counts=counts, events=events, oplock=oplock,
        )
        time.sleep(WATCH_POLL_S)
    for entry in holds:
        _close_lease(entry.lease)
    oplock_status = (
        "granted" if "granted" in oplock_results else
        "denied" if "denied" in oplock_results else
        "unavailable" if oplock else OPLOCK_STATUS
    )
    summary = {"event": "summary", "seed": int(seed), **counts, "oplock": oplock_status}
    events.append(summary)
    Path(log_file).write_text(
        "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events),
        encoding="utf-8",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OD-19 模擬端點防毒 WAL 干擾 helper（Windows 原生）")
    p.add_argument("--watch-dir", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--duration-s", type=float, required=True)
    p.add_argument("--ready-file", required=True)
    p.add_argument("--stop-file", required=True)
    p.add_argument("--log-file", required=True)
    p.add_argument("--go-file", required=True)
    p.add_argument("--ack-file", required=True)
    p.add_argument("--oplock", action="store_true",
                   help="以允許 delete 的 overlapped handle 申請 FSCTL_REQUEST_OPLOCK RH 變體")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_helper(
        watch_dir=args.watch_dir,
        seed=args.seed,
        duration_s=args.duration_s,
        ready_file=args.ready_file,
        stop_file=args.stop_file,
        log_file=args.log_file,
        go_file=args.go_file,
        ack_file=args.ack_file,
        oplock=args.oplock,
    )


if __name__ == "__main__":
    sys.exit(main())
