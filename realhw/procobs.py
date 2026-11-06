"""跨平台行程存活與 RSS 觀測；無法觀測時交由呼叫端標示證據不足。"""
from __future__ import annotations

import ctypes
import csv
import os
import pathlib
import subprocess
from typing import Any


def _windows_libraries() -> tuple[Any, Any]:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is not None:
        return (loader("kernel32", use_last_error=True),
                loader("psapi", use_last_error=True))
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        return windll.kernel32, windll.psapi
    raise OSError("Windows API 不可用")


def _set_signature(function: Any, argtypes: list[Any], restype: Any) -> None:
    """設定 ctypes 函式簽章；Python 測試替身不支援時略過。"""
    try:
        function.argtypes = argtypes
        function.restype = restype
    except (AttributeError, TypeError):
        pass


def rss_kb(pid: int) -> int:
    """回傳指定行程的 working-set RSS；失敗回 0。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return 0
    if pid <= 0:
        return 0
    if os.name == "nt":
        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        handle = None
        try:
            kernel32, psapi = _windows_libraries()
            open_process = kernel32.OpenProcess
            _set_signature(open_process, [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong],
                           ctypes.c_void_p)
            handle = open_process(0x0410, False, pid)
            if not handle:
                return 0
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            get_memory_info = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if get_memory_info is None:
                get_memory_info = getattr(psapi, "GetProcessMemoryInfo")
            _set_signature(get_memory_info,
                           [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong], ctypes.c_int)
            if not get_memory_info(handle, ctypes.byref(counters), counters.cb):
                return 0
            return int(counters.WorkingSetSize) // 1024
        except (AttributeError, OSError, TypeError, ValueError):
            return 0
        finally:
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except (AttributeError, OSError):
                    pass
    try:
        text = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1])
            return 0
    return 0


def pid_alive(pid: int) -> bool:
    """在不讀取目標行程狀態的情況下確認 PID 是否仍存在。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = None
        try:
            kernel32, _ = _windows_libraries()
            open_process = kernel32.OpenProcess
            _set_signature(open_process, [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong],
                           ctypes.c_void_p)
            handle = open_process(0x1000, False, pid)
            if not handle:
                get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
                return int(get_last_error()) == 5  # ERROR_ACCESS_DENIED：行程存在但不可查詢
            get_exit_code = kernel32.GetExitCodeProcess
            _set_signature(get_exit_code, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)],
                           ctypes.c_int)
            exit_code = ctypes.c_ulong()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == 259  # STILL_ACTIVE
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        finally:
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except (AttributeError, OSError):
                    pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_cmdlines() -> dict[int, str]:
    """嘗試以 CIM 取得命令列；工具或權限不足時回傳空對照。"""
    for exe in ("wmic", "powershell.exe", "powershell"):
        try:
            if exe == "wmic":
                cp = subprocess.run(
                    [exe, "process", "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
                    capture_output=True, text=True, timeout=8,
                )
                if cp.returncode != 0:
                    continue
                rows: dict[int, str] = {}
                for row in csv.DictReader(cp.stdout.splitlines()):
                    pid_text = str(row.get("ProcessId") or "").strip()
                    if pid_text.isdigit():
                        rows[int(pid_text)] = str(row.get("CommandLine") or "").strip()
                if rows:
                    return rows
            else:
                script = ("Get-CimInstance Win32_Process | ForEach-Object { "
                          "Write-Output ($_.ProcessId.ToString() + \"`t\" + $_.CommandLine) }")
                cp = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, text=True, timeout=8,
                )
                if cp.returncode != 0:
                    continue
                rows = {}
                for line in cp.stdout.splitlines():
                    pid_text, sep, command = line.partition("\t")
                    if sep and pid_text.strip().isdigit():
                        rows[int(pid_text.strip())] = command.strip()
                if rows:
                    return rows
        except (OSError, subprocess.SubprocessError):
            continue
    return {}


def _list_windows_processes() -> list[dict[str, Any]] | None:
    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    try:
        kernel32, _ = _windows_libraries()
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        first_process = kernel32.Process32FirstW
        next_process = kernel32.Process32NextW
        _set_signature(create_snapshot, [ctypes.c_ulong, ctypes.c_ulong], ctypes.c_void_p)
        _set_signature(first_process, [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
        _set_signature(next_process, [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
        snapshot = create_snapshot(0x00000002, 0)
        snapshot_value = snapshot.value if isinstance(snapshot, ctypes.c_void_p) else snapshot
        invalid = ctypes.c_void_p(-1).value
        if snapshot_value in (None, 0, -1, invalid):
            return None
        try:
            rows: list[dict[str, Any]] = []
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = bool(first_process(snapshot, ctypes.byref(entry)))
            if not ok:
                return None
            while ok:
                rows.append({"pid": int(entry.th32ProcessID), "name": str(entry.szExeFile),
                             "cmdline": None})
                ok = bool(next_process(snapshot, ctypes.byref(entry)))
        finally:
            try:
                kernel32.CloseHandle(snapshot)
            except (AttributeError, OSError):
                pass
        cmdlines = _windows_cmdlines()
        if not any(str(value or "").strip() for value in cmdlines.values()):
            return None
        for row in rows:
            row["cmdline"] = cmdlines.get(row["pid"]) or None
        return rows
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def list_processes() -> list[dict[str, Any]] | None:
    """列出 PID、程序名與可取得的命令列；無法完整列舉時回 None。"""
    if os.name == "nt":
        return _list_windows_processes()
    try:
        proc_entries = pathlib.Path("/proc").iterdir()
        rows: list[dict[str, Any]] = []
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                raw = (entry / "cmdline").read_bytes()
                cmdline = " ".join(part.decode("utf-8", errors="replace")
                                    for part in raw.split(b"\0") if part)
            except (OSError, PermissionError, ValueError):
                continue
            rows.append({"pid": pid, "name": name, "cmdline": cmdline or None})
        return rows
    except OSError:
        return None


__all__ = ["list_processes", "pid_alive", "rss_kb"]
