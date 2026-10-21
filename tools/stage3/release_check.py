"""A/B 兩側切換前的程序、端點、鎖與裝置釋放確認。"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .proc import Runner, run_cmd, run_json
from .reason_codes import HarnessError

if TYPE_CHECKING:
    from .ab_manifest import ReleaseSpec


@dataclass(frozen=True)
class PreStopSnapshot:
    daemon_pid: int | None
    descendant_pids: tuple[int, ...]
    match_paths: tuple[str, ...]
    endpoints: tuple[str, ...]
    device_paths: tuple[str, ...]
    lock_paths: tuple[str, ...]
    captured_at: float


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    target: str
    status: str
    detail: str


@dataclass(frozen=True)
class ReleaseResult:
    released: bool
    checks: tuple[CheckOutcome, ...]
    elapsed_s: float


class ReleaseProbe(Protocol):
    def pid_alive(self, pid: int) -> bool | None: ...

    def descendants(self, pid: int) -> tuple[int, ...] | None: ...

    def pids_by_exe(self, match_paths: Sequence[str]) -> tuple[int, ...] | None: ...

    def endpoint_connectable(self, endpoint: str) -> bool | None: ...

    def lock_acquirable(self, path: str) -> bool | None: ...

    def device_holders(self, path: str) -> tuple[tuple[int, ...], int] | None: ...

    def device_exclusive_open(self, path: str) -> bool | None: ...


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    """保留首次出現順序並移除重複字串。"""
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _status(value: object, *, true_status: str, false_status: str) -> str:
    if value is True:
        return true_status
    if value is False:
        return false_status
    return "unknown"


def _safe_probe(probe: ReleaseProbe, method: str, *args: object) -> object:
    """把平台探測例外收斂成 unknown，避免例外被誤當成資源已釋放。"""
    try:
        return getattr(probe, method)(*args)
    except Exception:
        return None


def _pid_values(value: object) -> tuple[int, ...] | None:
    """驗證程序探測結果只包含正整數 PID。"""
    if not isinstance(value, (tuple, list, set)):
        return None
    pids: set[int] = set()
    for pid in value:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        pids.add(pid)
    return tuple(sorted(pids))


class PosixProbe:
    """以 procfs、Unix/TCP socket、flock 與 fd 掃描觀測 POSIX 資源。"""

    _PROC_ROOT = "/proc"
    _SOCKET_TIMEOUT_S = 0.5

    @staticmethod
    def _is_script_interpreter(path: str | None) -> bool:
        if not path:
            return False
        basename = os.path.basename(path).casefold()
        return basename.startswith(("python", "pypy"))

    def _read_stat(self, pid: int) -> tuple[str, int] | None:
        """讀取 proc stat 的 state 與 parent PID；程序消失時回傳 None。"""
        try:
            raw = (Path(self._PROC_ROOT) / str(pid) / "stat").read_bytes().decode(
                "utf-8", errors="replace"
            )
        except (FileNotFoundError, ProcessLookupError):
            return None
        except PermissionError:
            raise
        except OSError:
            raise
        close = raw.rfind(")")
        if close < 0:
            raise ValueError("proc stat 格式無效")
        fields = raw[close + 1 :].split()
        if len(fields) < 2:
            raise ValueError("proc stat 欄位不足")
        return fields[0], int(fields[1])

    def pid_alive(self, pid: int) -> bool | None:
        if pid <= 0:
            return None
        if not os.path.isdir(self._PROC_ROOT):
            return None
        try:
            result = self._read_stat(pid)
        except PermissionError:
            return True
        except (OSError, ValueError):
            return None
        if result is None:
            return False
        return result[0] != "Z"

    def descendants(self, pid: int) -> tuple[int, ...] | None:
        if pid <= 0 or not os.path.isdir(self._PROC_ROOT):
            return None
        try:
            entries = list(os.scandir(self._PROC_ROOT))
        except OSError:
            return None

        parents: dict[int, int] = {}
        for entry in entries:
            if not entry.name.isdigit():
                continue
            child_pid = int(entry.name)
            try:
                stat = self._read_stat(child_pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError):
                return None
            if stat is not None:
                parents[child_pid] = stat[1]

        children: dict[int, list[int]] = {}
        for child_pid, parent_pid in parents.items():
            children.setdefault(parent_pid, []).append(child_pid)

        found: set[int] = set()
        pending = list(children.get(pid, ()))
        while pending:
            child_pid = pending.pop()
            if child_pid in found:
                continue
            found.add(child_pid)
            pending.extend(children.get(child_pid, ()))
        return tuple(sorted(found))

    def pids_by_exe(self, match_paths: Sequence[str]) -> tuple[int, ...] | None:
        if not os.path.isdir(self._PROC_ROOT):
            return None
        wanted = {os.path.realpath(path) for path in match_paths if path}
        if not wanted:
            return ()
        wanted_normalized = {os.path.normcase(os.path.normpath(path)) for path in wanted}
        try:
            entries = list(os.scandir(self._PROC_ROOT))
        except OSError:
            return None

        found: set[int] = set()
        unreadable = False
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            proc_dir = os.path.join(self._PROC_ROOT, entry.name)
            matched = False
            exe_unreadable = False
            exe_path: str | None = None
            try:
                exe = os.readlink(os.path.join(proc_dir, "exe"))
                exe_path = os.path.realpath(exe)
                if os.path.normcase(exe_path) in wanted_normalized:
                    matched = True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                exe_unreadable = True
            except OSError:
                exe_unreadable = True

            cmdline_unreadable = False
            try:
                raw = Path(proc_dir, "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                cmdline_unreadable = True
                raw = b""
            except OSError:
                cmdline_unreadable = True
                raw = b""

            cwd: str | None = None
            relative_argument_seen = False
            for argument in raw.split(b"\0"):
                if not argument:
                    continue
                candidate = os.fsdecode(argument)
                if os.path.isabs(candidate):
                    resolved = os.path.realpath(candidate)
                else:
                    relative_argument_seen = True
                    if cwd is None:
                        try:
                            cwd = os.readlink(os.path.join(proc_dir, "cwd"))
                        except (FileNotFoundError, ProcessLookupError):
                            cwd = ""
                        except OSError:
                            cwd = ""
                    if not cwd:
                        continue
                    resolved = os.path.realpath(os.path.join(cwd, candidate))
                if os.path.normcase(os.path.normpath(resolved)) in wanted_normalized:
                    matched = True
                    break

            if matched:
                found.add(pid)
            elif (cmdline_unreadable and (exe_unreadable or self._is_script_interpreter(exe_path))) or (
                self._is_script_interpreter(exe_path) and relative_argument_seen and not cwd
            ):
                unreadable = True
        if unreadable:
            return None
        return tuple(sorted(found))

    def endpoint_connectable(self, endpoint: str) -> bool | None:
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() == "tcp":
            if not parsed.hostname:
                return None
            try:
                port = parsed.port
            except ValueError:
                return None
            if port is None:
                return None
            family = socket.AF_INET
            address: object = (parsed.hostname, port)
        elif endpoint.startswith("unix://"):
            path = endpoint[len("unix://") :]
            if not path or not hasattr(socket, "AF_UNIX"):
                return None
            family = socket.AF_UNIX
            address = path
        elif os.path.isabs(endpoint):
            if not hasattr(socket, "AF_UNIX"):
                return None
            family = socket.AF_UNIX
            address = endpoint
        else:
            return None

        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._SOCKET_TIMEOUT_S)
                connection.connect(address)  # type: ignore[arg-type]
            return True
        except socket.timeout:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return False
            return None

    def lock_acquirable(self, path: str) -> bool | None:
        import fcntl

        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return True
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            except OSError:
                return None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                return None
            return True
        finally:
            os.close(fd)

    def device_holders(self, path: str) -> tuple[tuple[int, ...], int] | None:
        if not os.path.isdir(self._PROC_ROOT):
            return None
        target = os.path.normcase(os.path.normpath(os.path.realpath(path)))
        try:
            processes = list(os.scandir(self._PROC_ROOT))
        except OSError:
            return None

        holders: set[int] = set()
        unreadable = 0
        for process in processes:
            if not process.name.isdigit():
                continue
            pid = int(process.name)
            fd_dir = os.path.join(self._PROC_ROOT, process.name, "fd")
            try:
                descriptors = list(os.scandir(fd_dir))
            except FileNotFoundError:
                continue
            except PermissionError:
                unreadable += 1
                continue
            except ProcessLookupError:
                continue
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue
                return None

            process_unreadable = False
            for descriptor in descriptors:
                try:
                    link_target = os.readlink(descriptor.path)
                except FileNotFoundError:
                    continue
                except PermissionError:
                    process_unreadable = True
                    continue
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        continue
                    return None
                if os.path.normcase(os.path.normpath(link_target)) == target:
                    holders.add(pid)
            if process_unreadable:
                unreadable += 1
        return tuple(sorted(holders)), unreadable

    def device_exclusive_open(self, path: str) -> bool | None:
        flags = os.O_RDWR | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                return False
            return None
        os.close(fd)
        return True


class WindowsProbe:
    """以原生 Win32 程序、檔案鎖與 COM 獨占開啟 API 觀測資源。"""

    _SOCKET_TIMEOUT_S = 5.0
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87
    _TH32CS_SNAPPROCESS = 0x00000002
    _GENERIC_READ_WRITE = 0xC0000000
    _OPEN_EXISTING = 3

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._process_entry_type = ProcessEntry32W
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare_apis()

    def _declare_apis(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        entry_pointer = ctypes.POINTER(self._process_entry_type)
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, entry_pointer]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, entry_pointer]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def _last_error(self) -> int:
        return int(self._ctypes.get_last_error())

    def _process_rows(self) -> list[tuple[int, int, str]] | None:
        ctypes = self._ctypes
        snapshot = self._kernel32.CreateToolhelp32Snapshot(self._TH32CS_SNAPPROCESS, 0)
        value = snapshot.value if isinstance(snapshot, ctypes.c_void_p) else snapshot
        if value in (None, 0, -1, ctypes.c_void_p(-1).value):
            return None
        rows: list[tuple[int, int, str]] = []
        try:
            entry = self._process_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            if not self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return None
            while True:
                rows.append(
                    (
                        int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        str(entry.szExeFile),
                    )
                )
                entry.dwSize = ctypes.sizeof(entry)
                if not self._kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    if self._last_error() == 18:  # ERROR_NO_MORE_FILES
                        return rows
                    return None
        finally:
            self._kernel32.CloseHandle(snapshot)

    def _image_path(self, pid: int) -> tuple[bool | None, str | None]:
        handle = self._kernel32.OpenProcess(self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = self._last_error()
            if error == self._ERROR_INVALID_PARAMETER:
                return False, None
            return None, None
        try:
            size = self._wintypes.DWORD(32768)
            buffer = self._ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, self._ctypes.byref(size)
            ):
                if self._last_error() == self._ERROR_INVALID_PARAMETER:
                    return False, None
                return None, None
            return True, buffer.value
        finally:
            self._kernel32.CloseHandle(handle)

    def pid_alive(self, pid: int) -> bool | None:
        if pid <= 0:
            return None
        access = self._PROCESS_QUERY_LIMITED_INFORMATION | self._SYNCHRONIZE
        handle = self._kernel32.OpenProcess(access, False, pid)
        if not handle:
            error = self._last_error()
            if error == self._ERROR_INVALID_PARAMETER:
                return False
            if error == self._ERROR_ACCESS_DENIED:
                return True
            return None
        try:
            exit_code = self._wintypes.DWORD()
            if not self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(exit_code)):
                return None
            return int(exit_code.value) == self._STILL_ACTIVE
        finally:
            self._kernel32.CloseHandle(handle)

    def descendants(self, pid: int) -> tuple[int, ...] | None:
        rows = self._process_rows()
        if rows is None:
            return None
        children: dict[int, list[int]] = {}
        for child_pid, parent_pid, _ in rows:
            children.setdefault(parent_pid, []).append(child_pid)
        found: set[int] = set()
        pending = list(children.get(pid, ()))
        while pending:
            child_pid = pending.pop()
            if child_pid in found:
                continue
            found.add(child_pid)
            pending.extend(children.get(child_pid, ()))
        return tuple(sorted(found))

    def pids_by_exe(self, match_paths: Sequence[str]) -> tuple[int, ...] | None:
        rows = self._process_rows()
        if rows is None:
            return None
        wanted = {
            os.path.normcase(os.path.realpath(path))
            for path in match_paths
            if path
        }
        if not wanted:
            return ()
        wanted_names = {os.path.basename(path).casefold() for path in wanted}
        found: list[int] = []
        for pid, _, exe_name in rows:
            if exe_name.casefold() not in wanted_names:
                continue
            readable, image = self._image_path(pid)
            if readable is None:
                return None
            if not readable or image is None:
                continue
            if os.path.normcase(os.path.realpath(image)) in wanted:
                found.append(pid)
        return tuple(sorted(set(found)))

    def endpoint_connectable(self, endpoint: str) -> bool | None:
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() != "tcp" or not parsed.hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is None:
            return None
        try:
            with socket.create_connection((parsed.hostname, port), timeout=self._SOCKET_TIMEOUT_S):
                return True
        except socket.timeout:
            return None
        except OSError as exc:
            if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
                return False
            return None

    def lock_acquirable(self, path: str) -> bool | None:
        import msvcrt

        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return True
            return None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EDEADLK) or getattr(exc, "winerror", None) in (32, 33):
                    return False
                return None
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                return None
            return True
        finally:
            os.close(fd)

    def device_holders(self, path: str) -> tuple[tuple[int, ...], int] | None:
        return None

    def device_exclusive_open(self, path: str) -> bool | None:
        device = path
        if not device.startswith("\\\\.\\"):
            device = "\\\\.\\" + device
        handle = self._kernel32.CreateFileW(
            device,
            self._GENERIC_READ_WRITE,
            0,
            None,
            self._OPEN_EXISTING,
            0,
            None,
        )
        value = handle.value if isinstance(handle, self._wintypes.HANDLE) else handle
        invalid = self._ctypes.c_void_p(-1).value
        if value in (None, 0, -1, invalid):
            if self._last_error() == self._ERROR_ACCESS_DENIED:
                return False
            return None
        self._kernel32.CloseHandle(handle)
        return True


def default_probe() -> ReleaseProbe:
    """依執行平台選擇程序與裝置探測器。"""
    if os.name == "nt":
        return WindowsProbe()
    return PosixProbe()


def _release_unconfirmed(detail: str) -> HarnessError:
    return HarnessError("HARNESS_RELEASE_UNCONFIRMED", detail)


def capture_pre_stop(
    cli_exe: Path,
    env: Mapping[str, str],
    spec: ReleaseSpec,
    *,
    daemon_exe: Path,
    entry_scripts: Sequence[str] = (),
    endpoint: str | None = None,
    status_timeout_s: float = 15.0,
    run: Runner = run_cmd,
    probe: ReleaseProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PreStopSnapshot:
    """在停止 A 前取得程序樹與需釋放資源的快照。"""
    argv = [str(cli_exe)]
    if endpoint:
        argv.extend(("--endpoint", endpoint))
    argv.extend(("daemon", "status"))
    try:
        result, status_doc = run_json(
            run,
            argv,
            env=env,
            timeout=status_timeout_s,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _release_unconfirmed(f"無法查詢 daemon status：{exc}") from exc

    daemon_pid: int | None
    if result.timed_out:
        raise _release_unconfirmed("daemon status 查詢逾時")
    selected_probe = probe or default_probe()
    if result.rc == 2:
        if status_doc is not None:
            if status_doc.get("daemon_reachable") is True or status_doc.get("error_code") != "SOCKET_ERROR":
                raise _release_unconfirmed("daemon status 回報錯誤，不能視為 daemon 已停止")
        elif result.stdout.strip():
            raise _release_unconfirmed("daemon status 輸出不是可解析的 JSON，不能視為 daemon 已停止")
        if not spec.endpoints:
            raise _release_unconfirmed("daemon status 失敗且沒有端點可確認 daemon 已停止")
        endpoint_states = [
            _safe_probe(selected_probe, "endpoint_connectable", item)
            for item in spec.endpoints
        ]
        if any(state is not False for state in endpoint_states):
            raise _release_unconfirmed(
                f"daemon status 失敗且端點尚未全部確認關閉：{endpoint_states!r}"
            )
        daemon_pid = None
    elif result.rc == 0 and status_doc is not None and status_doc.get("ok") is True:
        pid = status_doc.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            daemon_pid = pid
        elif status_doc.get("running") is False:
            daemon_pid = None
        else:
            raise _release_unconfirmed("daemon status 缺少有效 PID")
    else:
        raise _release_unconfirmed("daemon status 無法解析或回報失敗")

    match_paths = _unique(
        tuple(os.path.realpath(os.fspath(path)) for path in (daemon_exe, *entry_scripts))
    )
    if not match_paths:
        raise _release_unconfirmed("daemon 執行檔比對路徑為空")

    descendants: tuple[int, ...] = ()
    if daemon_pid is not None:
        found = _safe_probe(selected_probe, "descendants", daemon_pid)
        descendants_result = _pid_values(found)
        if descendants_result is None:
            raise _release_unconfirmed("無法取得 daemon 子程序清單")
        descendants = descendants_result

    return PreStopSnapshot(
        daemon_pid=daemon_pid,
        descendant_pids=descendants,
        match_paths=match_paths,
        endpoints=tuple(spec.endpoints),
        device_paths=tuple(spec.device_paths),
        lock_paths=tuple(spec.lock_paths),
        captured_at=clock(),
    )


def snapshot_from_endpoints(
    endpoints_json: Path | None,
    spec: ReleaseSpec,
    *,
    match_paths: Sequence[str],
    clock: Callable[[], float] = time.monotonic,
) -> PreStopSnapshot:
    """建立 L1.5 快照；合併 manifest 與 suite adapter 的端點資料。"""
    try:
        document = (
            json.loads(Path(endpoints_json).read_text(encoding="utf-8"))
            if endpoints_json is not None
            else {}
        )
    except FileNotFoundError:
        document = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _release_unconfirmed(f"endpoints.json 無法讀取：{exc}") from exc
    if not isinstance(document, dict):
        raise _release_unconfirmed("endpoints.json 頂層必須是 object")

    merged: dict[str, tuple[str, ...]] = {}
    for key in ("endpoints", "device_paths", "lock_paths"):
        supplied = document.get(key, [])
        if not isinstance(supplied, list) or any(not isinstance(value, str) or not value for value in supplied):
            raise _release_unconfirmed(f"endpoints.json 的 {key} 必須是字串清單且不得含空值")
        merged[key] = _unique(tuple(getattr(spec, key)) + tuple(supplied))

    normalized_paths = _unique(tuple(os.path.realpath(path) for path in match_paths))
    if not normalized_paths:
        raise _release_unconfirmed("daemon 執行檔比對路徑為空")
    return PreStopSnapshot(
        daemon_pid=None,
        descendant_pids=(),
        match_paths=normalized_paths,
        endpoints=merged["endpoints"],
        device_paths=merged["device_paths"],
        lock_paths=merged["lock_paths"],
        captured_at=clock(),
    )


def _evaluate(snapshot: PreStopSnapshot, spec: ReleaseSpec, probe: ReleaseProbe) -> tuple[CheckOutcome, ...]:
    checks: list[CheckOutcome] = []

    def add(name: str, target: str, value: str, detail: str) -> None:
        checks.append(CheckOutcome(name=name, target=target, status=value, detail=detail))

    if snapshot.daemon_pid is not None:
        if not isinstance(snapshot.daemon_pid, int) or isinstance(snapshot.daemon_pid, bool) or snapshot.daemon_pid <= 0:
            add("daemon_pid", str(snapshot.daemon_pid), "unknown", "PID 必須為正整數")
        else:
            alive = _safe_probe(probe, "pid_alive", snapshot.daemon_pid)
            status = _status(alive, true_status="held", false_status="released")
            add("daemon_pid", str(snapshot.daemon_pid), status, f"pid_alive={alive!r}")

    for pid in snapshot.descendant_pids:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            add("descendant", str(pid), "unknown", "PID 必須為正整數")
            continue
        alive = _safe_probe(probe, "pid_alive", pid)
        status = _status(alive, true_status="held", false_status="released")
        add("descendant", str(pid), status, f"pid_alive={alive!r}")

    if not snapshot.match_paths:
        add("leaked_daemon", "", "unknown", "daemon 執行檔比對路徑為空")
    else:
        pids = _safe_probe(probe, "pids_by_exe", snapshot.match_paths)
        observed_pids = _pid_values(pids)
        if observed_pids is None:
            add("leaked_daemon", ",".join(snapshot.match_paths), "unknown", "程序掃描結果不明或含無效 PID")
        else:
            leaked = tuple(pid for pid in observed_pids if pid != os.getpid())
            status = "released" if not leaked else "held"
            add("leaked_daemon", ",".join(snapshot.match_paths), status, f"pids={leaked!r}")

    for endpoint in snapshot.endpoints:
        connected = _safe_probe(probe, "endpoint_connectable", endpoint)
        status = _status(connected, true_status="held", false_status="released")
        add("endpoint", endpoint, status, f"connectable={connected!r}")

    for path in snapshot.lock_paths:
        acquirable = _safe_probe(probe, "lock_acquirable", path)
        status = _status(acquirable, true_status="released", false_status="held")
        add("lock", path, status, f"acquirable={acquirable!r}")

    if not isinstance(probe, WindowsProbe):
        for path in snapshot.device_paths:
            observed = _safe_probe(probe, "device_holders", path)
            if (
                not isinstance(observed, tuple)
                or len(observed) != 2
                or not isinstance(observed[1], int)
                or isinstance(observed[1], bool)
                or observed[1] < 0
            ):
                add(
                    "device_holders",
                    path,
                    "unknown",
                    f"holders=unknown; unreadable=unknown; accept_unreadable_holders={bool(spec.accept_unreadable_holders)}",
                )
                continue
            holders = _pid_values(observed[0])
            if holders is None:
                add(
                    "device_holders",
                    path,
                    "unknown",
                    f"holders=unknown; unreadable={observed[1]}; accept_unreadable_holders={bool(spec.accept_unreadable_holders)}",
                )
                continue
            unreadable = observed[1]
            if holders:
                status = "held"
            elif unreadable and not spec.accept_unreadable_holders:
                status = "unknown"
            else:
                status = "released"
            detail = (
                f"holders={holders!r}; unreadable={unreadable}; "
                f"accept_unreadable_holders={bool(spec.accept_unreadable_holders)}"
            )
            add("device_holders", path, status, detail)

    if spec.device_open_probe:
        for path in snapshot.device_paths:
            opened = _safe_probe(probe, "device_exclusive_open", path)
            status = _status(opened, true_status="released", false_status="held")
            add("device_exclusive", path, status, f"exclusive_open={opened!r}")

    return tuple(checks)


def verify_released(
    snap: PreStopSnapshot,
    spec: ReleaseSpec,
    *,
    probe: ReleaseProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReleaseResult:
    """輪詢全部資源；逾時或任何 unknown 都不得宣告已釋放。"""
    selected_probe = probe or default_probe()
    started = clock()
    timeout_s = max(0.0, float(spec.timeout_s))
    poll_s = max(0.01, float(spec.poll_s))
    last_checks: tuple[CheckOutcome, ...] = ()

    while True:
        last_checks = _evaluate(snap, spec, selected_probe)
        elapsed = max(0.0, clock() - started)
        if last_checks and all(check.status == "released" for check in last_checks):
            return ReleaseResult(released=True, checks=last_checks, elapsed_s=elapsed)
        if elapsed >= timeout_s:
            return ReleaseResult(released=False, checks=last_checks, elapsed_s=elapsed)
        sleep(min(poll_s, timeout_s - elapsed))
