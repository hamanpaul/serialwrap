"""P5 Task 21a：Slice 2.1 情境動作的協調器側輔助（測試資產，§10.4）。

本模組只放 runner 動作所需的純協調器工具，不寫入 UART byte stream（§15.3）：
- `ExternalHolder`：在協調器行程外以子行程開埠並持有（`flock_tiocexcl`／`plain`），
  關閉時於子行程的 SIGTERM handler 內先 `ioctl(TIOCNXCL)` 再 close（R0-F3），不使用 SIGKILL；
- `rpc_roundtrip_ms`：以直接 NDJSON socket 往返計時（不經 CLI）；
- `json_path_get`：`wait_cli_until` 的點分隔路徑解析；
- `load_event_latency_contract`／`summarize_samples`：`contracts/resource_limits.yaml event_latency` 的讀取與統計。

runner 動作表本身仍在 `func-test/runner.py` 的 `_exec_step`（P2 Task 11 的實際位置）。
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import socket
import subprocess
import sys
import time
from typing import Any

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
RESOURCE_LIMITS = ROOT_DIR / "contracts" / "resource_limits.yaml"

EXTERNAL_HOLD_MODES: frozenset[str] = frozenset({"flock_tiocexcl", "plain"})
EXTERNAL_TARGETS: frozenset[str] = frozenset({"session_slave", "by_id_link"})

# 子行程程式碼：以 `python -I -c` 執行，argv = [path, hold]。
# 每個狀態變化以一行 JSON 寫到 stdout，由協調器讀取；任何控制都不經 UART。
_HOLDER_SCRIPT = r"""
import errno, fcntl, json, os, signal, sys, termios

path, hold = sys.argv[1], sys.argv[2]
state = {"fd": -1}


def emit(obj):
    # 直接 os.write：SIGTERM handler 可能在主流程 emit 途中觸發，buffered stdout 會 reentrant 失敗
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    while data:
        data = data[os.write(1, data):]


def release(signum, frame):
    fd = state["fd"]
    rec = {"event": "closed", "pid": os.getpid(), "tiocnxcl": None, "unlock": None, "fd_closed": False, "errors": []}
    if fd >= 0:
        if hold == "flock_tiocexcl":
            try:
                fcntl.ioctl(fd, termios.TIOCNXCL)
                rec["tiocnxcl"] = True
            except OSError as exc:
                rec["tiocnxcl"] = False
                rec["errors"].append("TIOCNXCL:%s" % exc.errno)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                rec["unlock"] = True
            except OSError as exc:
                rec["unlock"] = False
                rec["errors"].append("LOCK_UN:%s" % exc.errno)
        try:
            os.close(fd)
        except OSError as exc:
            rec["errors"].append("close:%s" % exc.errno)
        try:
            os.fstat(fd)
        except OSError as exc:
            rec["fd_closed"] = exc.errno == errno.EBADF
        state["fd"] = -1
    emit(rec)
    os._exit(0 if rec["fd_closed"] and not rec["errors"] else 3)


signal.signal(signal.SIGTERM, release)
fd = -1
try:
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    if hold == "flock_tiocexcl":
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
except OSError as exc:
    if fd >= 0:
        os.close(fd)
    emit({"event": "open", "opened": False, "errno": exc.errno, "error": str(exc), "pid": os.getpid()})
    sys.exit(0)
state["fd"] = fd
emit({"event": "open", "opened": True, "errno": None, "error": None, "pid": os.getpid()})
while True:
    signal.pause()
"""


def _read_json_line(proc: subprocess.Popen[str], timeout_s: float) -> dict[str, Any] | None:
    """在 timeout_s 內讀子行程一行 JSON；逾時或 EOF 回 None。"""
    import selectors

    assert proc.stdout is not None
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not sel.select(timeout=remaining):
                return None
            line = proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    finally:
        sel.close()


class ExternalHolder:
    """以子行程模擬外部工具持有埠：`flock_tiocexcl`＝遵守鎖的工具、`plain`＝不取鎖的工具（如 minicom）。"""

    def __init__(self, path: str, hold: str) -> None:
        if hold not in EXTERNAL_HOLD_MODES:
            raise ValueError(f"不支援的 hold '{hold}'，必須為 {sorted(EXTERNAL_HOLD_MODES)}")
        self.path = path
        self.hold = hold
        self.proc: subprocess.Popen[str] | None = None
        self.open_result: dict[str, Any] | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def start(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        if os.name == "nt":
            raise RuntimeError("external_open 子行程持有僅支援 POSIX（Windows 由 win-com-* 案例承接）")
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLDER_SCRIPT, self.path, self.hold],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            close_fds=True,
        )
        rec = _read_json_line(self.proc, timeout_s)
        if rec is None or rec.get("event") != "open":
            self._reap_after_failure()
            raise RuntimeError(f"外部持有子行程未在 {timeout_s}s 內回報開埠結果（path={self.path}）")
        self.open_result = {
            "opened": rec.get("opened") is True,
            "errno": rec.get("errno"),
            "error": rec.get("error"),
            "pid": self.proc.pid,
        }
        if not self.open_result["opened"]:
            try:
                self.proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                pass
        return dict(self.open_result)

    def close(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        """送 SIGTERM（不得 SIGKILL）；子行程於 handler 內 TIOCNXCL→LOCK_UN→close 後結束，確認 fd 已關。"""
        import signal

        proc = self.proc
        if proc is None:
            return {"closed": True, "fd_closed": True, "pid": None, "note": "never_started"}
        if proc.poll() is not None:
            # 開埠失敗時子行程已自行結束，沒有 fd 可關
            return {"closed": True, "fd_closed": True, "pid": proc.pid, "exit_code": proc.returncode,
                    "tiocnxcl": None, "note": "already_exited"}
        proc.send_signal(signal.SIGTERM)
        rec = _read_json_line(proc, timeout_s)
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            exit_code = None
        if rec is None or rec.get("event") != "closed":
            stderr = ""
            if exit_code is not None and proc.stderr is not None:
                try:
                    stderr = proc.stderr.read()[-2000:]
                except OSError:
                    stderr = ""
            raise RuntimeError(f"外部持有子行程 pid={proc.pid} 未回報關閉（exit={exit_code}）stderr={stderr!r}")
        result = {
            "closed": exit_code == 0 and rec.get("fd_closed") is True,
            "fd_closed": rec.get("fd_closed") is True,
            "tiocnxcl": rec.get("tiocnxcl"),
            "unlock": rec.get("unlock"),
            "errors": rec.get("errors") or [],
            "pid": proc.pid,
            "exit_code": exit_code,
        }
        if exit_code is None:
            raise RuntimeError(f"外部持有子行程 pid={proc.pid} 關閉 fd 後未結束：{result}")
        self._close_pipes()
        return result

    def _close_pipes(self) -> None:
        if self.proc is None:
            return
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _reap_after_failure(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        self._close_pipes()


def rpc_roundtrip_ms(endpoint: str, method: str, params: dict[str, Any] | None = None,
                     *, timeout_s: float = 5.0, req_id: Any = 1) -> tuple[float, dict[str, Any]]:
    """以直接 NDJSON socket 送一筆請求並計時（含連線建立）；回傳 (毫秒, 回應)。"""
    payload = (json.dumps({"id": req_id, "method": method, "params": params or {}},
                          ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    started = time.perf_counter_ns()
    if endpoint.startswith("tcp://"):
        host, _, port = endpoint.removeprefix("tcp://").rpartition(":")
        sock = socket.create_connection((host, int(port)), timeout=timeout_s)
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        sock.connect(endpoint)
    try:
        sock.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    line = buf.split(b"\n", 1)[0]
    if not line:
        raise RuntimeError(f"RPC {method} 未收到回應行（endpoint={endpoint}）")
    resp = json.loads(line.decode("utf-8"))
    if not isinstance(resp, dict):
        raise RuntimeError(f"RPC {method} 回應非物件：{resp!r}")
    return elapsed_ms, resp


_MISSING = object()


def json_path_get(obj: Any, path: str) -> Any:
    """點分隔路徑：dict 以鍵、list 以數字索引；`key=value` 片段在 list 中選第一個符合的元素。找不到回 `_MISSING`。"""
    current = obj
    for part in [p for p in str(path).split(".") if p != ""]:
        if isinstance(current, list):
            if "=" in part:
                key, _, want = part.partition("=")
                current = next((it for it in current if isinstance(it, dict) and str(it.get(key)) == want), _MISSING)
            else:
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return _MISSING
        elif isinstance(current, dict):
            current = current.get(part, _MISSING)
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def is_missing(value: Any) -> bool:
    return value is _MISSING


def load_event_latency_contract(path: pathlib.Path = RESOURCE_LIMITS) -> dict[str, Any]:
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    lat = doc.get("event_latency")
    if not isinstance(lat, dict):
        raise RuntimeError(f"{path} 缺少 event_latency 區段")
    return lat


def summarize_samples(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"count": 0, "max_ms": None, "p99_ms": None}
    ordered = sorted(samples)
    idx = max(0, math.ceil(0.99 * len(ordered)) - 1)
    return {"count": len(ordered), "max_ms": ordered[-1], "p99_ms": ordered[idx]}


def host_load() -> dict[str, Any]:
    """量測期間 host 負載：/proc/loadavg 1min 與 CPU 數（無 /proc 時以 os.getloadavg）。"""
    load1: float | None = None
    try:
        load1 = float(pathlib.Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            load1 = None
    return {"loadavg_1min": load1, "cpu_count": os.cpu_count()}


__all__ = [
    "EXTERNAL_HOLD_MODES",
    "EXTERNAL_TARGETS",
    "ExternalHolder",
    "host_load",
    "is_missing",
    "json_path_get",
    "load_event_latency_contract",
    "rpc_roundtrip_ms",
    "summarize_samples",
]
