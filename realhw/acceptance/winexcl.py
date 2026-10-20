"""Windows 原生 COM 獨占開啟證據（stdlib-only；ctypes 呼叫 kernel32）。

目的：Windows 無法反查 COM 持有者（GAP-G-03），`holder_unknown` 恆為真，不能作雙重持有證據。
本模組改以「獨占開啟結果」取證：`CreateFileW(OPEN_EXISTING, dwShareMode=0)` 與共享模式探測，
在 daemon 持有期間第二次開啟必被拒；release 後外部可取得；外部持有時 daemon attach 被拒。

約束：只做零 I/O 的開啟與立即關閉，不呼叫任何讀寫、控制線或佇列清除類 API，不改變 DTR／RTS；
只呼叫唯讀 RPC（`daemon status`、`session list`）；`device release`／`attach` 只出現在 handoff 流程。
不 import 產品碼與 `uart_stdio_bridge`。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

ERROR_ACCESS_DENIED = 5
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROBE_SHARE_MODES = (0, FILE_SHARE_READ | FILE_SHARE_WRITE)
DEVICE_PREFIX = "\\\\.\\"
_LOCK_NAME = ".probe.lock"


@dataclass(frozen=True)
class OpenAttempt:
    share_mode: int
    opened: bool
    winerror: int | None


@dataclass(frozen=True)
class Binding:
    com: str
    state: str | None
    device: str | None
    bridge_generation: int | None
    daemon_pid: int | None
    daemon_created_at: int | None


Opener = Callable[[str, int], OpenAttempt]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _kernel32() -> Any:
    """取得 kernel32 並設定 HANDLE 相關型別（僅 Windows 會呼叫到）。"""
    lib = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    lib.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    lib.CreateFileW.restype = ctypes.c_void_p
    lib.CloseHandle.argtypes = [ctypes.c_void_p]
    lib.CloseHandle.restype = ctypes.c_int
    lib.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    lib.OpenProcess.restype = ctypes.c_void_p
    lib.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.GetProcessTimes.restype = ctypes.c_int
    lib.GetLastError.restype = ctypes.c_uint32
    return lib


def _is_invalid(handle: Any) -> bool:
    return handle is None or handle in (0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


def _device_path(device: str) -> str:
    return DEVICE_PREFIX + device


def win_open(device: str, share_mode: int) -> OpenAttempt:
    """以指定共享模式嘗試開啟 COM 裝置後立即關閉；零 I/O。"""
    kernel = _kernel32()
    handle = kernel.CreateFileW(_device_path(device), GENERIC_READ | GENERIC_WRITE, share_mode,
                                None, OPEN_EXISTING, 0, None)
    if _is_invalid(handle):
        return OpenAttempt(share_mode=share_mode, opened=False, winerror=int(kernel.GetLastError()))
    kernel.CloseHandle(handle)
    return OpenAttempt(share_mode=share_mode, opened=True, winerror=None)


@contextlib.contextmanager
def win_hold(device: str) -> Iterator[OpenAttempt]:
    """以 dwShareMode=0 持有 handle 直到 context 結束（外部持有者）；零 I/O。"""
    kernel = _kernel32()
    handle = kernel.CreateFileW(_device_path(device), GENERIC_READ | GENERIC_WRITE, 0,
                                None, OPEN_EXISTING, 0, None)
    if _is_invalid(handle):
        yield OpenAttempt(share_mode=0, opened=False, winerror=int(kernel.GetLastError()))
        return
    try:
        yield OpenAttempt(share_mode=0, opened=True, winerror=None)
    finally:
        kernel.CloseHandle(handle)


def process_created_at(pid: int | None) -> int | None:
    """回傳程序建立時間（FILETIME，100 ns 單位）；無法取得回 None。"""
    if pid is None or os.name != "nt":
        return None
    from ctypes import wintypes

    kernel = _kernel32()
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if _is_invalid(handle):
        return None
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME(), wintypes.FILETIME(),
                                                   wintypes.FILETIME(), wintypes.FILETIME())
        ok = kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel_time), ctypes.byref(user_time))
        if not ok:
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _session_row(sw: Any, com: str) -> dict[str, Any]:
    response = sw.run("session", "list")
    rows = response.get("sessions") if isinstance(response, dict) else None
    for row in rows or []:
        if isinstance(row, dict) and (row.get("selector") == com or row.get("com") == com):
            return row
    return {}


def read_binding(sw: Any, com: str, *, identity: Callable[[int], int | None] = process_created_at) -> Binding:
    """只呼叫唯讀 `daemon status` 與 `session list`，組出綁定快照。"""
    status = sw.run("daemon", "status")
    pid = None
    if isinstance(status, dict) and status.get("ok") is not False:
        pid = _as_int(status.get("pid"))
    row = _session_row(sw, com)
    created = identity(pid) if pid is not None else None
    return Binding(com=com, state=row.get("state"), device=row.get("attached_real_path"),
                   bridge_generation=_as_int(row.get("bridge_generation")),
                   daemon_pid=pid, daemon_created_at=created)


class ProbeLock:
    """`.probe.lock` 檔案鎖：探測與 handoff 互斥（僅 Windows 使用；建構不觸碰檔案系統）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._handle: Any = None

    def _open(self) -> Any:
        if self._handle is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a+b")
        return self._handle

    def _lock(self, *, blocking: bool) -> bool:  # noqa: ARG002 - 呼叫端以 try_acquire 取非阻塞結果
        if os.name != "nt":
            raise RuntimeError("ProbeLock 僅支援 Windows（windows only）")
        import msvcrt

        handle = self._open()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def try_acquire(self) -> bool:
        return self._lock(blocking=False)

    def acquire(self) -> bool:
        while not self._lock(blocking=True):
            time.sleep(0.1)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        import msvcrt

        self._handle.seek(0)
        try:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "ProbeLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.release()
        return False


def _record(com: str, expected_device: str | None, wall_ts: str, *, verdict: str,
            reason: str | None, before: Binding | None = None, after: Binding | None = None,
            attempts: list[OpenAttempt] | None = None) -> dict[str, Any]:
    daemon = {"pid": before.daemon_pid if before else None,
              "created_at": before.daemon_created_at if before else None}
    return {
        "kind": "excl_probe",
        "wall_ts": wall_ts,
        "com": com,
        "expected_device": expected_device,
        "before": asdict(before) if before else None,
        "after": asdict(after) if after else None,
        "attempts": [asdict(item) for item in (attempts or [])],
        "daemon": daemon,
        "verdict": verdict,
        "reason": reason,
    }


def _probe_unlocked(sw: Any, com: str, *, expected_device: str | None, opener: Opener,
                    identity: Callable[[int], int | None], wall_ts: str) -> dict[str, Any]:
    before = read_binding(sw, com, identity=identity)
    if before.state != "READY":
        return _record(com, expected_device, wall_ts, verdict="inconclusive", reason="not_ready", before=before)
    if expected_device is None or before.device != expected_device:
        return _record(com, expected_device, wall_ts, verdict="inconclusive", reason="device_unbound", before=before)
    if before.daemon_pid is None or before.daemon_created_at is None:
        return _record(com, expected_device, wall_ts, verdict="inconclusive",
                       reason="process_identity_unknown", before=before)

    attempts = [opener(str(expected_device), mode) for mode in PROBE_SHARE_MODES]
    after = read_binding(sw, com, identity=identity)
    if after != before:
        verdict, reason = "inconclusive", "binding_changed"
    elif any(item.opened for item in attempts):
        verdict, reason = "violated", "external_open_succeeded"
    elif all((not item.opened) and item.winerror == ERROR_ACCESS_DENIED for item in attempts):
        verdict, reason = "exclusive", None
    else:
        verdict, reason = "inconclusive", "unexpected_winerror"
    return _record(com, expected_device, wall_ts, verdict=verdict, reason=reason,
                   before=before, after=after, attempts=attempts)


def second_open_probe(sw: Any, com: str, *, expected_device: str | None, opener: Opener = win_open,
                      identity: Callable[[int], int | None] = process_created_at,
                      lock: Any) -> dict[str, Any]:
    """daemon 持有期間的第二次獨占開啟探測（非阻塞取鎖；取不到即 skipped）。"""
    wall_ts = _now_iso()
    if not lock.try_acquire():
        return _record(com, expected_device, wall_ts, verdict="skipped", reason="handoff_in_progress")
    try:
        return _probe_unlocked(sw, com, expected_device=expected_device, opener=opener,
                               identity=identity, wall_ts=wall_ts)
    finally:
        lock.release()


def _is_daemon_ok(value: Any) -> bool:
    return isinstance(value, dict)


def _handoff_body(sw: Any, com: str, *, expected_device: str | None, opener: Opener,
                  holder: Callable[[str], Any], identity: Callable[[int], int | None],
                  ready_timeout_s: float, stack: contextlib.ExitStack,
                  daemon: dict[str, Any], steps: list[dict[str, Any]]) -> tuple[str, str | None]:
    def record(name: str, ok: bool, detail: dict[str, Any]) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    probe = _probe_unlocked(sw, com, expected_device=expected_device, opener=opener,
                            identity=identity, wall_ts=_now_iso())
    before_ok = probe["verdict"] == "exclusive"
    record("probe_before", before_ok, {"verdict": probe["verdict"], "reason": probe["reason"]})
    if not before_ok:
        return probe["verdict"], "probe_before"
    generation_before = probe["before"]["bridge_generation"]

    response = sw.run("device", "release", "--selector", com)
    row = _session_row(sw, com)
    release_ok = isinstance(response, dict) and response.get("ok") is True and row.get("state") == "RELEASED"
    record("release", release_ok, {"ok": response.get("ok") if isinstance(response, dict) else None,
                                    "state": row.get("state")})
    if not release_ok:
        return "inconclusive", "release"

    attempt = stack.enter_context(holder(str(expected_device)))
    hold_ok = attempt.opened is True
    record("hold", hold_ok, {"opened": attempt.opened, "winerror": attempt.winerror})
    if not hold_ok:
        verdict = "violated" if attempt.winerror == ERROR_ACCESS_DENIED else "inconclusive"
        return verdict, "hold"

    response = sw.run("device", "attach", "--selector", com)
    row = _session_row(sw, com)
    detail = {"ok": response.get("ok"), "error_code": response.get("error_code"),
              "pids": response.get("pids"), "holder_unknown": response.get("holder_unknown"),
              "state": row.get("state")}
    if response.get("ok") is True:
        record("attach_held", False, detail)
        return "violated", "attach_held"
    held_ok = response.get("ok") is False and response.get("error_code") == "DEVICE_STILL_HELD" \
        and row.get("state") == "RELEASED"
    record("attach_held", held_ok, detail)
    if not held_ok:
        return "inconclusive", "attach_held"

    stack.close()
    record("unhold", True, {})

    response = sw.run("device", "attach", "--selector", com)
    attach_ok = isinstance(response, dict) and response.get("ok") is True
    deadline = time.monotonic() + float(ready_timeout_s)
    row = {}
    ready = False
    while True:
        row = _session_row(sw, com)
        if row.get("state") == "READY":
            ready = True
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    record("attach", attach_ok and ready, {"ok": response.get("ok") if isinstance(response, dict) else None,
                                            "state": row.get("state")})
    if not (attach_ok and ready):
        return "inconclusive", "attach"

    after = _probe_unlocked(sw, com, expected_device=expected_device, opener=opener,
                            identity=identity, wall_ts=_now_iso())
    generation_after = (after.get("after") or after.get("before") or {}).get("bridge_generation")
    same_process = (after["daemon"]["pid"] == daemon["pid"]
                    and after["daemon"]["created_at"] == daemon["created_at"])
    generation_advanced = generation_after is not None and generation_before is not None \
        and generation_after > generation_before
    after_ok = after["verdict"] == "exclusive" and same_process and generation_advanced
    record("probe_after", after_ok, {"verdict": after["verdict"], "reason": after["reason"],
                                      "generation_before": generation_before,
                                      "generation_after": generation_after,
                                      "same_process": same_process})
    if after_ok:
        return "exclusive", None
    if after["verdict"] == "violated":
        return "violated", "probe_after"
    return "inconclusive", "probe_after"


def _recover_attach(sw: Any, com: str, steps: list[dict[str, Any]]) -> None:
    """中途失敗後若板子仍 RELEASED，best-effort 重新 attach（結果記入 steps，不改變判定）。"""
    try:
        row = _session_row(sw, com)
        if row.get("state") != "RELEASED":
            return
        response = sw.run("device", "attach", "--selector", com)
        ok = isinstance(response, dict) and response.get("ok") is True
        steps.append({"name": "recover_attach", "ok": ok, "detail": {"ok": ok}})
    except Exception as exc:  # noqa: BLE001
        steps.append({"name": "recover_attach", "ok": False, "detail": {"error": type(exc).__name__}})


def handoff_probe(sw: Any, com: str, *, phase: str, expected_device: str | None, opener: Opener = win_open,
                  holder: Callable[[str], Any] = win_hold,
                  identity: Callable[[int], int | None] = process_created_at,
                  lock: Any, ready_timeout_s: float = 120.0) -> dict[str, Any]:
    """Windows handoff：release → 外部持有 → attach 必被拒 → 解除 → attach → 獨占再驗證。"""
    wall_start = _now_iso()
    steps: list[dict[str, Any]] = []
    daemon: dict[str, Any] = {"pid": None, "created_at": None}
    verdict, reason = "inconclusive", "probe_before"
    with lock:
        stack = contextlib.ExitStack()
        try:
            try:
                status = sw.run("daemon", "status")
                pid = _as_int(status.get("pid")) if _is_daemon_ok(status) else None
                daemon = {"pid": pid, "created_at": identity(pid) if pid is not None else None}
                verdict, reason = _handoff_body(sw, com, expected_device=expected_device, opener=opener,
                                                holder=holder, identity=identity,
                                                ready_timeout_s=ready_timeout_s, stack=stack,
                                                daemon=daemon, steps=steps)
            except Exception as exc:  # noqa: BLE001 - 中途 RPC 失敗記 inconclusive，不得中斷整輪 handoff
                verdict, reason = "inconclusive", "rpc_error"
                steps.append({"name": "rpc_error", "ok": False, "detail": {"error": type(exc).__name__}})
                stack.close()
                _recover_attach(sw, com, steps)
        finally:
            stack.close()
    if verdict == "exclusive":
        reason = None
    return {
        "kind": "excl_handoff",
        "phase": phase,
        "wall_ts_start": wall_start,
        "wall_ts_end": _now_iso(),
        "com": com,
        "expected_device": expected_device,
        "daemon": daemon,
        "steps": steps,
        "verdict": verdict,
        "reason": reason,
    }


def _append_ndjson(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _observer_binding(header_path: Path) -> dict[str, Any]:
    first = header_path.read_text(encoding="utf-8").splitlines()[0]
    header = json.loads(first)
    return header.get("device_binding") or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 原生 COM 獨占 handoff 證據")
    commands = parser.add_subparsers(dest="command", required=True)
    handoff = commands.add_parser("handoff", help="執行一次 release→hold→attach→probe 循環")
    handoff.add_argument("--phase", choices=["pre", "post"], required=True)
    handoff.add_argument("--serialwrap-exe", default="serialwrap")
    handoff.add_argument("--boards", required=True, help="逗號分隔的受測 COM")
    handoff.add_argument("--observer-header", required=True, type=Path)
    handoff.add_argument("--excl-dir", required=True, type=Path)
    handoff.add_argument("--ready-timeout-s", type=float, default=120.0)
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("winexcl handoff 只能在 Windows 執行（windows only）", file=sys.stderr)
        return 2

    from realhw.drivers import SwCli

    boards = [item.strip() for item in args.boards.split(",") if item.strip()]
    binding = _observer_binding(args.observer_header)
    sw = SwCli(args.serialwrap_exe)
    lock = ProbeLock(args.excl_dir / _LOCK_NAME)
    verdicts: list[str] = []
    for com in boards:
        record = handoff_probe(sw, com, phase=args.phase, expected_device=binding.get(com),
                               lock=lock, ready_timeout_s=args.ready_timeout_s)
        _append_ndjson(args.excl_dir / "handoff.ndjson", record)
        verdicts.append(record["verdict"])
        print(json.dumps({"com": com, "phase": args.phase, "verdict": record["verdict"],
                          "reason": record["reason"]}, ensure_ascii=False, sort_keys=True))
    if "violated" in verdicts:
        return 1
    if verdicts and all(item == "exclusive" for item in verdicts):
        return 0
    return 4


if __name__ == "__main__":
    sys.exit(main())
