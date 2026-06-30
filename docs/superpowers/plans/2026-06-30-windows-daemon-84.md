# Windows daemon（#84 PORT-4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 serialwrapd daemon 能在 Windows 以單一前景執行檔跑起來、被 CLI/agent 經 TCP loopback 連到、並自動接管所有閒置的非藍牙 COM 埠。

**Architecture:** 三個碰 OS 的 seam（RPC server / singleton lock / device source）各抽出共用 core 介面 + 兩個獨立平台實作；POSIX 既有邏輯「整段搬位、不改寫」（byte-identical），Windows 為新增 sibling 模組；`platform_backends.py` selector 依 `sys.platform` 選後端。core 業務邏輯（service/arbiter/session_manager/wal/uart_io）零改動。

**Tech Stack:** Python 3.10+、`asyncio`（POSIX AF_UNIX / Windows TCP）、stdlib `winreg`/`msvcrt`、既有 `pyserial`（Windows serial 後端）、PyInstaller（建置期）。

## Global Constraints

- 語言：所有文件、註解、docstring、commit message 一律繁體中文（policy 語言規範）。
- 分支：禁止直接 commit `main`；本工作於 `feature/84-windows-daemon`。
- Commit：Conventional Commits（繁中 subject）；每筆含 trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- 測試 gate：完成 code change 前必跑 `python3 -m pytest -q tests/`；不得引入新失敗（既有失敗僅 `tests/test_multiagent_e2e.py::...::test_five_agents_three_rounds_no_conflict`）。
- Policy gate：完成 phase 前跑 `python3 -m policy_check --repo .`。
- 變更紀錄：production code/docs 變更須同步 `CHANGELOG.md`（`[Unreleased]`）；版本變動同步 `VERSION`。
- Python 風格：模組以 `from __future__ import annotations` 開頭；函式完整型別標註。
- POSIX 行為 byte-identical：搬位不得改語意；以 re-export shim 維持既有 import 與測試零改動。
- 後端覆寫 env 慣例對齊 `SERIALWRAP_SERIAL_BACKEND`：新增 `SERIALWRAP_RPC_BACKEND`、`SERIALWRAP_LOCK_BACKEND`、`SERIALWRAP_DEVICE_BACKEND`（`auto`/`posix`/`win`）。
- 測試裝置：Windows 實機 loopback CH340 = **COM8**；藍牙 COM3/COM4 永不接管。

## File Structure

| 檔案 | 責任 |
|------|------|
| `sw_core/rpc_posix.py`（新，搬入）| AF_UNIX RPC server（現 `JsonRpcUnixServer` 整段） |
| `sw_core/rpc_win.py`（新）| TCP loopback RPC server，共用 `_handle_client` 行為 |
| `sw_core/rpc.py`（改）| re-export shim → `rpc_posix` |
| `sw_core/lock_posix.py`（新，搬入）| `fcntl.flock` singleton（現 `SingletonLock` 整段） |
| `sw_core/lock_win.py`（新）| `msvcrt.locking` 獨佔檔 + TCP 存活探測 |
| `sw_core/daemon_lock.py`（改）| re-export shim → `lock_posix` |
| `sw_core/device_source.py`（新）| `DeviceSource` 介面 + posix/win 實作 + 藍牙排除 pure function |
| `sw_core/platform_backends.py`（新）| selector：依平台/env 回 RpcServer/SingletonLock/DeviceSource |
| `sw_core/device_watcher.py`（改）| `_scan()` 改為注入的 `DeviceSource.scan()` |
| `sw_core/constants.py`（改）| 平台感知 endpoint 預設（Windows `tcp://127.0.0.1:48700`）|
| `sw_core/daemon.py`（改）| 改走 selector 取後端 |
| `sw_core/service.py`（改）| `DeviceWatcher` 注入 selector 選的 `DeviceSource` |
| `scripts/build_windows.ps1`、`serialwrap.spec`（新）| PyInstaller one-file 打包 |
| `tests/test_rpc_tcp.py`、`tests/test_lock_win.py`、`tests/test_device_source.py`、`tests/test_platform_backends.py`、`tests/test_constants_endpoint.py`（新）| 對應測試 |

---

## Task 1: POSIX RPC server 搬位 + shim

**Files:**
- Create: `sw_core/rpc_posix.py`
- Modify: `sw_core/rpc.py`（整檔轉 shim）
- Test:（沿用既有 RPC 相關測試）

**Interfaces:**
- Produces: `sw_core.rpc_posix.JsonRpcUnixServer`（建構子 `(socket_path, handler, *, blocking_methods=None)`，方法 `start/serve_forever/stop` 為 async）；`sw_core.rpc.JsonRpcUnixServer` 經 shim 仍可 import。

- [ ] **Step 1: 搬檔** — 將現 `sw_core/rpc.py` 全部內容複製到 `sw_core/rpc_posix.py`（一字不改）。

- [ ] **Step 2: rpc.py 轉 shim**

```python
from __future__ import annotations

# AF_UNIX RPC server 實作已搬至 rpc_posix（#84 PORT-4 平台 seam 分檔）。
# 保留此 shim 維持既有 `from sw_core.rpc import JsonRpcUnixServer` 與測試零改動。
from sw_core.rpc_posix import JsonRpcUnixServer

__all__ = ["JsonRpcUnixServer"]
```

- [ ] **Step 3: 跑既有測試確認零回歸**

Run: `python3 -m pytest -q tests/`
Expected: PASS（僅既有已知失敗，無新失敗）

- [ ] **Step 4: Commit**

```bash
git add sw_core/rpc_posix.py sw_core/rpc.py
git commit -m "refactor(rpc): AF_UNIX server 搬至 rpc_posix + shim（#84 PORT-4）"
```

---

## Task 2: POSIX singleton lock 搬位 + shim

**Files:**
- Create: `sw_core/lock_posix.py`
- Modify: `sw_core/daemon_lock.py`（整檔轉 shim）

**Interfaces:**
- Produces: `sw_core.lock_posix.SingletonLock`（建構子 `(lock_path, socket_path)`，方法 `acquire()`/`release()`，`acquire` 失敗 raise `RuntimeError("DAEMON_ALREADY_RUNNING")` / `"SOCKET_UNAVAILABLE"`）；`sw_core.daemon_lock.SingletonLock` 經 shim 仍可 import。

- [ ] **Step 1: 搬檔** — 將現 `sw_core/daemon_lock.py` 全部內容複製到 `sw_core/lock_posix.py`（一字不改）。

- [ ] **Step 2: daemon_lock.py 轉 shim**

```python
from __future__ import annotations

# flock singleton 實作已搬至 lock_posix（#84 PORT-4 平台 seam 分檔）。
from sw_core.lock_posix import SingletonLock

__all__ = ["SingletonLock"]
```

- [ ] **Step 3: 跑既有測試**

Run: `python3 -m pytest -q tests/`
Expected: PASS（無新失敗）

- [ ] **Step 4: Commit**

```bash
git add sw_core/lock_posix.py sw_core/daemon_lock.py
git commit -m "refactor(lock): flock singleton 搬至 lock_posix + shim（#84 PORT-4）"
```

---

## Task 3: platform_backends selector

**Files:**
- Create: `sw_core/platform_backends.py`
- Test: `tests/test_platform_backends.py`

**Interfaces:**
- Consumes: `rpc_posix.JsonRpcUnixServer`、`lock_posix.SingletonLock`（Task 1/2）；`rpc_win.TcpRpcServer`、`lock_win.WindowsSingletonLock`（Task 4/5，延後 import 以免非 Windows import 失敗）。
- Produces:
  - `select_rpc_backend(backend: str | None = None) -> str`（回 `"posix"`/`"win"`）
  - `select_lock_backend(backend: str | None = None) -> str`
  - `select_device_backend(backend: str | None = None) -> str`
  - 字串解析規則同 `serial_port._select_backend`：`posix`→posix；`win`/`windows`/`win32`→win；`auto`→Windows 回 win、其餘 posix。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_platform_backends.py
from __future__ import annotations
import importlib

from sw_core import platform_backends as pb


def test_explicit_posix():
    assert pb.select_rpc_backend("posix") == "posix"

def test_explicit_win_aliases():
    for v in ("win", "windows", "win32"):
        assert pb.select_rpc_backend(v) == "win"

def test_auto_follows_platform(monkeypatch):
    monkeypatch.setattr(pb.sys, "platform", "linux")
    monkeypatch.setattr(pb.os, "name", "posix")
    assert pb.select_rpc_backend("auto") == "posix"
    monkeypatch.setattr(pb.sys, "platform", "win32")
    assert pb.select_rpc_backend("auto") == "win"

def test_env_override(monkeypatch):
    monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
    assert pb.select_rpc_backend() == "win"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_platform_backends.py -v`
Expected: FAIL（`No module named 'sw_core.platform_backends'`）

- [ ] **Step 3: 實作**

```python
# sw_core/platform_backends.py
from __future__ import annotations

import os
import sys


def _select(env_name: str, backend: str | None) -> str:
    mode = (backend or os.environ.get(env_name) or "auto").lower()
    if mode in ("posix", "unix", "termios"):
        return "posix"
    if mode in ("win", "windows", "win32"):
        return "win"
    # auto：Windows 走 win，其餘維持 posix（生產路徑零回歸）。
    if os.name == "nt" or sys.platform.startswith("win"):
        return "win"
    return "posix"


def select_rpc_backend(backend: str | None = None) -> str:
    return _select("SERIALWRAP_RPC_BACKEND", backend)


def select_lock_backend(backend: str | None = None) -> str:
    return _select("SERIALWRAP_LOCK_BACKEND", backend)


def select_device_backend(backend: str | None = None) -> str:
    return _select("SERIALWRAP_DEVICE_BACKEND", backend)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_platform_backends.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/platform_backends.py tests/test_platform_backends.py
git commit -m "feat(platform): 新增 RPC/lock/device 後端 selector（#84 PORT-4）"
```

---

## Task 4: Windows TCP RPC server

**Files:**
- Create: `sw_core/rpc_win.py`
- Modify: `sw_core/rpc_posix.py`（抽出可共用的 `handle_rpc_line` 行為，避免複製分派邏輯）
- Test: `tests/test_rpc_tcp.py`

**Interfaces:**
- Consumes: 共用的逐行請求處理（`_handle_client` 內 JSON 解析→handler→回應 的邏輯）。
- Produces: `sw_core.rpc_win.TcpRpcServer`（建構子 `(endpoint: str, handler, *, blocking_methods=None)`，`endpoint` 形如 `tcp://127.0.0.1:48700`；async `start/serve_forever/stop`，介面與 `JsonRpcUnixServer` 對齊）。

> 設計註：為「core 共 code」，把 `JsonRpcUnixServer._handle_client` 內與 transport 無關的「讀一行→分派→寫回應」邏輯抽成模組層 async 函式 `serve_connection(reader, writer, handler, blocking_methods)`，POSIX 與 TCP server 都呼叫它；transport-specific 只剩 listener 建立（`start_unix_server` vs `start_server`）與 socket 權限（POSIX-only）。

- [ ] **Step 1: 把連線處理抽成共用函式**（重構 `rpc_posix.py`）

在 `rpc_posix.py` 新增模組層函式（將現 `_handle_client` 主體搬入，行為不變），並讓 `JsonRpcUnixServer._handle_client` 呼叫它：

```python
async def serve_connection(reader, writer, handler, blocking_methods):
    """逐行讀 JSON-RPC 請求、分派 handler、寫回應。transport 無關，POSIX/TCP 共用。"""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req_id = None
            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                writer.write((json.dumps({"ok": False, "error_code": "INVALID_JSON"}, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
                continue
            if not isinstance(obj, dict):
                writer.write((json.dumps({"ok": False, "error_code": "INVALID_REQUEST"}, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
                continue
            req_id = obj.get("id")
            method = obj.get("method")
            params = obj.get("params")
            if not isinstance(method, str):
                resp = {"id": req_id, "ok": False, "error_code": "INVALID_METHOD"}
            else:
                if not isinstance(params, dict):
                    params = {}
                try:
                    if method in blocking_methods:
                        loop = asyncio.get_running_loop()
                        result = await loop.run_in_executor(None, handler, method, params)
                    else:
                        result = handler(method, params)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error_code": "EXCEPTION", "message": str(exc)}
                resp = {"id": req_id}
                if isinstance(result, dict):
                    resp.update(result)
                else:
                    resp.update({"ok": True, "data": result})
            writer.write((json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
```

`JsonRpcUnixServer._handle_client` 改為：
```python
    async def _handle_client(self, reader, writer):
        await serve_connection(reader, writer, self._handler, self._blocking_methods)
```

- [ ] **Step 2: 跑既有測試確認重構零回歸**

Run: `python3 -m pytest -q tests/`
Expected: PASS（無新失敗）

- [ ] **Step 3: 寫 TCP server 失敗測試**

```python
# tests/test_rpc_tcp.py
from __future__ import annotations
import asyncio
import threading

from sw_core.rpc_win import TcpRpcServer
from sw_core.client import rpc_call


def test_tcp_rpc_round_trip():
    endpoint = "tcp://127.0.0.1:48721"

    def handler(method, params):
        if method == "echo":
            return {"ok": True, "got": params}
        return {"ok": False, "error_code": "UNKNOWN_METHOD"}

    loop = asyncio.new_event_loop()
    server = TcpRpcServer(endpoint, handler)
    ready = threading.Event()

    async def run():
        await server.start()
        ready.set()
        await asyncio.Event().wait()  # 阻塞直到外部 stop

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(timeout=5.0)
    try:
        resp = rpc_call(endpoint, "echo", {"x": 1})
        assert resp["ok"] is True
        assert resp["got"] == {"x": 1}
    finally:
        loop.call_soon_threadsafe(loop.stop)
```

- [ ] **Step 4: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_rpc_tcp.py -v`
Expected: FAIL（`No module named 'sw_core.rpc_win'`）

- [ ] **Step 5: 實作 TcpRpcServer**

```python
# sw_core/rpc_win.py
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from sw_core.rpc_posix import serve_connection


class TcpRpcServer:
    """TCP loopback JSON-RPC server（Windows daemon RPC plane，#84 PORT-4）。

    介面與 JsonRpcUnixServer 對齊（start/serve_forever/stop），分派邏輯共用 serve_connection。
    """

    def __init__(
        self,
        endpoint: str,
        handler: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        blocking_methods: set[str] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._handler = handler
        self._blocking_methods = blocking_methods or set()
        self._server: asyncio.AbstractServer | None = None
        host, port = _parse_tcp(endpoint)
        self._host = host
        self._port = port

    async def _handle_client(self, reader, writer):
        await serve_connection(reader, writer, self._handler, self._blocking_methods)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, host=self._host, port=self._port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None


def _parse_tcp(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"invalid tcp endpoint: {endpoint!r}")
    return parsed.hostname, parsed.port
```

- [ ] **Step 6: 跑測試確認通過**

Run: `python3 -m pytest tests/test_rpc_tcp.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add sw_core/rpc_posix.py sw_core/rpc_win.py tests/test_rpc_tcp.py
git commit -m "feat(rpc): 抽 serve_connection 共用 + 新增 TCP loopback server（#84 PORT-4）"
```

---

## Task 5: Windows singleton lock

**Files:**
- Create: `sw_core/lock_win.py`
- Test: `tests/test_lock_win.py`

**Interfaces:**
- Consumes: `sw_core.client.rpc_call`（存活探測可重用，或直接 TCP connect）。
- Produces: `sw_core.lock_win.WindowsSingletonLock`（建構子 `(lock_path, endpoint)`；`acquire()` 失敗 raise `RuntimeError("DAEMON_ALREADY_RUNNING")`；`release()`）。endpoint 為 `tcp://host:port`。

> 存活探測：嘗試 TCP connect endpoint。連得上＝既有 daemon 在跑→`DAEMON_ALREADY_RUNNING`；refused/timeout＝視為 stale，繼續取得檔鎖。檔鎖以 `msvcrt.locking(fd, LK_NBLCK, 1)`；取不到＝另一個 daemon 持有→`DAEMON_ALREADY_RUNNING`。

- [ ] **Step 1: 寫測試**（以 monkeypatch 注入 probe 結果，使測試跨平台可跑；`msvcrt` 分支 Win32-gated）

```python
# tests/test_lock_win.py
from __future__ import annotations
import os
import sys
import pytest

from sw_core import lock_win


def test_endpoint_alive_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_win, "_endpoint_alive", lambda ep: True)
    lk = lock_win.WindowsSingletonLock(str(tmp_path / "d.lock"), "tcp://127.0.0.1:48799")
    with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
        lk.acquire()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="msvcrt 檔鎖僅 Windows")
def test_stale_endpoint_acquires(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_win, "_endpoint_alive", lambda ep: False)
    lk = lock_win.WindowsSingletonLock(str(tmp_path / "d.lock"), "tcp://127.0.0.1:48798")
    lk.acquire()
    try:
        assert os.path.exists(str(tmp_path / "d.lock"))
    finally:
        lk.release()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_lock_win.py -v`
Expected: FAIL（`No module named 'sw_core.lock_win'`）

- [ ] **Step 3: 實作**

```python
# sw_core/lock_win.py
from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit


def _endpoint_alive(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.2):
            return True
    except OSError:
        return False


class WindowsSingletonLock:
    """Windows daemon 單例：msvcrt 獨佔檔鎖 + TCP endpoint 存活探測（#84 PORT-4）。"""

    def __init__(self, lock_path: str, endpoint: str) -> None:
        self.lock_path = lock_path
        self.endpoint = endpoint
        self._fd: int | None = None

    def acquire(self) -> None:
        if _endpoint_alive(self.endpoint):
            raise RuntimeError("DAEMON_ALREADY_RUNNING")
        import msvcrt
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            raise RuntimeError("DAEMON_ALREADY_RUNNING")
        self._fd = fd
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))

    def release(self) -> None:
        if self._fd is None:
            return
        import msvcrt
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._fd)
            self._fd = None
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_lock_win.py -v`
Expected: PASS（非 Windows 上 stale 案例自動 skip）

- [ ] **Step 5: Commit**

```bash
git add sw_core/lock_win.py tests/test_lock_win.py
git commit -m "feat(lock): Windows msvcrt singleton lock + TCP 存活探測（#84 PORT-4）"
```

---

## Task 6: DeviceSource 介面 + POSIX 搬位 + 藍牙排除 pure function

**Files:**
- Create: `sw_core/device_source.py`
- Test: `tests/test_device_source.py`

**Interfaces:**
- Consumes: `sw_core.device_watcher.DeviceInfo`。
- Produces:
  - `DeviceSource`（協定：`scan() -> dict[str, DeviceInfo]`）
  - `PosixDeviceSource(scan_dirs: list[str])`（由現 `DeviceWatcher._scan` 搬入）
  - `exclude_bluetooth(serialcomm: dict[str, str], bt_ports: set[str], exclude: set[str]) -> dict[str, str]`（pure function：吃 `{device_path: COMname}` + 藍牙埠集合 + 手動排除，吐保留的 `{COMname: COMname}`）
  - `WindowsDeviceSource(exclude_coms: set[str] | None = None)`（Task 7 補完列舉細節）

- [ ] **Step 1: 寫測試**（藍牙排除為核心，pure function 跨平台可測）

```python
# tests/test_device_source.py
from __future__ import annotations
from sw_core.device_source import exclude_bluetooth


def test_exclude_bluetooth_real_machine_case():
    # 實機：COM3/COM4=BthModem（藍牙）、COM8=CH340
    serialcomm = {
        r"\Device\BthModem0": "COM3",
        r"\Device\BthModem1": "COM4",
        r"\Device\Serial2": "COM8",
    }
    bt_ports = {"COM3", "COM4"}  # 由 BTHENUM PortName 收集
    kept = exclude_bluetooth(serialcomm, bt_ports, set())
    assert set(kept.keys()) == {"COM8"}


def test_exclude_bluetooth_by_value_name_heuristic():
    serialcomm = {r"\Device\BthModem9": "COM9", r"\Device\Serial0": "COM10"}
    kept = exclude_bluetooth(serialcomm, set(), set())  # 無 BTHENUM 資料時靠 value-name 兜底
    assert set(kept.keys()) == {"COM10"}


def test_exclude_bluetooth_manual_override():
    serialcomm = {r"\Device\Serial2": "COM8"}
    kept = exclude_bluetooth(serialcomm, set(), {"COM8"})
    assert kept == {}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_device_source.py -v`
Expected: FAIL（`No module named 'sw_core.device_source'`）

- [ ] **Step 3: 實作 device_source.py**

```python
# sw_core/device_source.py
from __future__ import annotations

import os
from typing import Protocol

from sw_core.device_watcher import DeviceInfo


class DeviceSource(Protocol):
    def scan(self) -> dict[str, DeviceInfo]:
        ...


class PosixDeviceSource:
    """掃 /dev/serial/by-id（+by-path）。由 DeviceWatcher._scan 搬入，行為不變。"""

    def __init__(self, scan_dirs: list[str]) -> None:
        self._scan_dirs = scan_dirs

    def scan(self) -> dict[str, DeviceInfo]:
        out: dict[str, DeviceInfo] = {}
        seen_real: set[str] = set()
        for scan_dir in self._scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            for name in sorted(os.listdir(scan_dir)):
                path = os.path.join(scan_dir, name)
                if not os.path.exists(path):
                    continue
                real_path = os.path.realpath(path)
                if real_path in seen_real:
                    continue
                seen_real.add(real_path)
                out[path] = DeviceInfo(by_id=path, real_path=real_path)
        return out


def exclude_bluetooth(
    serialcomm: dict[str, str],
    bt_ports: set[str],
    exclude: set[str],
) -> dict[str, str]:
    """從 SERIALCOMM 列舉剔除藍牙埠與手動排除，回 {COMname: COMname}。

    判據：(1) COM 名在 bt_ports（BTHENUM PortName 收集）；(2) device path value-name 含
    'BthModem'（兜底）；(3) COM 名在 exclude（config windows.exclude_coms）。
    """
    kept: dict[str, str] = {}
    for device_path, com in serialcomm.items():
        if com in bt_ports or com in exclude:
            continue
        if "bthmodem" in device_path.lower():
            continue
        kept[com] = com
    return kept
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_device_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/device_source.py tests/test_device_source.py
git commit -m "feat(device): DeviceSource 介面 + POSIX 搬位 + 藍牙排除 pure function（#84 PORT-4）"
```

---

## Task 7: Windows COM 列舉（device_source_win）

**Files:**
- Modify: `sw_core/device_source.py`（補 `WindowsDeviceSource` + registry 讀取）
- Test: `tests/test_device_source.py`（補 registry 解析測試）

**Interfaces:**
- Produces: `WindowsDeviceSource(exclude_coms: set[str] | None = None)`，`scan()` 回 `{COMname: DeviceInfo(by_id=COMname, real_path=r"\\.\COMname")}`；模組層 `_read_serialcomm() -> dict[str,str]`、`_read_bt_ports() -> set[str]`（`winreg`，可 monkeypatch）。

- [ ] **Step 1: 寫測試**（monkeypatch 注入 registry 讀取結果，跨平台可跑）

```python
# tests/test_device_source.py（追加）
from sw_core import device_source as ds


def test_windows_device_source_scan(monkeypatch):
    monkeypatch.setattr(ds, "_read_serialcomm", lambda: {
        r"\Device\BthModem0": "COM3",
        r"\Device\Serial2": "COM8",
    })
    monkeypatch.setattr(ds, "_read_bt_ports", lambda: {"COM3"})
    src = ds.WindowsDeviceSource()
    devices = src.scan()
    assert set(devices.keys()) == {"COM8"}
    assert devices["COM8"].real_path == r"\\.\COM8"
    assert devices["COM8"].by_id == "COM8"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_device_source.py::test_windows_device_source_scan -v`
Expected: FAIL（`AttributeError: ... WindowsDeviceSource` / `_read_serialcomm`）

- [ ] **Step 3: 實作**（追加到 `device_source.py`）

```python
def _read_serialcomm() -> dict[str, str]:
    """讀 HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM → {device_path: COMname}。"""
    import winreg
    out: dict[str, str] = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except FileNotFoundError:
        return out
    try:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            out[name] = value
            i += 1
    finally:
        winreg.CloseKey(key)
    return out


def _read_bt_ports() -> set[str]:
    """遞迴掃 HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM 下的 PortName，收集藍牙 COM。"""
    import winreg
    ports: set[str] = set()

    def walk(root, path: str) -> None:
        try:
            key = winreg.OpenKey(root, path)
        except OSError:
            return
        try:
            # 本層若有 Device Parameters\PortName 收集之
            try:
                pp = winreg.OpenKey(root, path + r"\Device Parameters")
                try:
                    val, _ = winreg.QueryValueEx(pp, "PortName")
                    if isinstance(val, str):
                        ports.add(val)
                except OSError:
                    pass
                finally:
                    winreg.CloseKey(pp)
            except OSError:
                pass
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                walk(root, path + "\\" + sub)
                i += 1
        finally:
            winreg.CloseKey(key)

    walk(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHENUM")
    return ports


class WindowsDeviceSource:
    """Windows 原生 COM 列舉（SERIALCOMM）+ 藍牙排除（#84 PORT-4）。"""

    def __init__(self, exclude_coms: set[str] | None = None) -> None:
        self._exclude = exclude_coms or set()

    def scan(self) -> dict[str, DeviceInfo]:
        serialcomm = _read_serialcomm()
        bt_ports = _read_bt_ports()
        kept = exclude_bluetooth(serialcomm, bt_ports, self._exclude)
        return {
            com: DeviceInfo(by_id=com, real_path=rf"\\.\{com}")
            for com in kept
        }
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_device_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/device_source.py tests/test_device_source.py
git commit -m "feat(device): Windows SERIALCOMM 列舉 + BTHENUM 藍牙排除（#84 PORT-4）"
```

---

## Task 8: DeviceWatcher 注入 DeviceSource

**Files:**
- Modify: `sw_core/device_watcher.py`
- Test:（沿用既有 watcher 測試）

**Interfaces:**
- Consumes: `device_source.DeviceSource`、`PosixDeviceSource`。
- Produces: `DeviceWatcher(..., source: DeviceSource | None = None)`；未給 source 時以 `PosixDeviceSource([by_id_dir]+extra_scan_dirs)` 維持既有預設行為。`_scan()` 改 delegate 至 `source.scan()`。

- [ ] **Step 1: 修改 DeviceWatcher**

`__init__` 末段新增（保留既有參數簽章相容）：
```python
        from sw_core.device_source import PosixDeviceSource
        self._source = source if source is not None else PosixDeviceSource(self._scan_dirs)
```
並把 `_scan` 改為：
```python
    def _scan(self) -> dict[str, DeviceInfo]:
        return self._source.scan()
```
建構子簽章加上 `source: "DeviceSource | None" = None`（置於既有參數之後）。

- [ ] **Step 2: 跑既有 watcher 測試確認零回歸**

Run: `python3 -m pytest -q tests/ -k "watcher or device or session"`
Expected: PASS（無新失敗）

- [ ] **Step 3: 跑全測試**

Run: `python3 -m pytest -q tests/`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add sw_core/device_watcher.py
git commit -m "refactor(device): DeviceWatcher 改注入 DeviceSource，預設 POSIX 不變（#84 PORT-4）"
```

---

## Task 9: 平台感知 endpoint 預設（constants）

**Files:**
- Modify: `sw_core/constants.py`
- Test: `tests/test_constants_endpoint.py`

**Interfaces:**
- Produces: `DEFAULT_ENDPOINT`（Windows `tcp://127.0.0.1:48700`；POSIX = `SOCKET_PATH` 檔路徑）；`SOCKET_PATH` 在 Windows 仍為檔案路徑供 lock 旁佐，但 daemon endpoint 預設改用 `DEFAULT_ENDPOINT`。

- [ ] **Step 1: 寫測試**

```python
# tests/test_constants_endpoint.py
from __future__ import annotations
import importlib


def test_default_endpoint_posix(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("os.name", "posix")
    import sw_core.constants as c
    importlib.reload(c)
    assert c.DEFAULT_ENDPOINT == c.SOCKET_PATH  # POSIX：AF_UNIX 檔路徑

def test_default_endpoint_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    import sw_core.constants as c
    importlib.reload(c)
    assert c.DEFAULT_ENDPOINT == "tcp://127.0.0.1:48700"
    importlib.reload(c)  # 還原
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_constants_endpoint.py -v`
Expected: FAIL（`AttributeError: ... DEFAULT_ENDPOINT`）

- [ ] **Step 3: 實作**（`constants.py` 加，靠近 `SOCKET_PATH` 之後）

```python
import sys

DEFAULT_TCP_PORT = int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))
if os.name == "nt" or sys.platform.startswith("win"):
    DEFAULT_ENDPOINT = _env_path("SERIALWRAP_ENDPOINT", f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}") \
        if os.environ.get("SERIALWRAP_ENDPOINT") else f"tcp://127.0.0.1:{DEFAULT_TCP_PORT}"
else:
    DEFAULT_ENDPOINT = os.environ.get("SERIALWRAP_ENDPOINT") or SOCKET_PATH
```
> 註：`tcp://` 不可過 `os.path.expanduser`，故 Windows 分支不走 `_env_path`。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_constants_endpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/constants.py tests/test_constants_endpoint.py
git commit -m "feat(constants): 平台感知 RPC endpoint 預設（Windows tcp loopback）（#84 PORT-4）"
```

---

## Task 10: daemon/service 接線 selector

**Files:**
- Modify: `sw_core/daemon.py`、`sw_core/service.py`

**Interfaces:**
- Consumes: `platform_backends.select_rpc_backend/select_lock_backend/select_device_backend`、`rpc_win.TcpRpcServer`、`lock_win.WindowsSingletonLock`、`device_source.WindowsDeviceSource`、`constants.DEFAULT_ENDPOINT`。
- Produces:（無新公開符號；行為：Windows 走 TCP/msvcrt/SERIALCOMM，POSIX 不變並寫 `config.yaml::socket_path`）

- [ ] **Step 1: daemon.py 改走 selector**

`build_parser` 的 `--socket` 預設改 `DEFAULT_ENDPOINT`。`_run_async` 內依後端選擇：
```python
    from sw_core.platform_backends import select_rpc_backend, select_lock_backend
    from sw_core.constants import DEFAULT_ENDPOINT

    if select_lock_backend() == "win":
        from sw_core.lock_win import WindowsSingletonLock
        lock = WindowsSingletonLock(args.lock, args.socket)
    else:
        from sw_core.lock_posix import SingletonLock
        lock = SingletonLock(args.lock, args.socket)
    ...
    if select_rpc_backend() == "win":
        from sw_core.rpc_win import TcpRpcServer
        server = TcpRpcServer(args.socket, _handle, blocking_methods=BLOCKING_RPC_METHODS)
    else:
        from sw_core.rpc_posix import JsonRpcUnixServer
        server = JsonRpcUnixServer(args.socket, _handle, blocking_methods=BLOCKING_RPC_METHODS)
```
並在 `server.start()` 後，將有效 endpoint 寫入 `config.yaml`（沿用 `RuntimeConfig.set_mode` 或新增 `set_socket`），使 CLI `_resolve_endpoint` 連得上。

- [ ] **Step 2: service.py 注入 Windows DeviceSource**

`SerialwrapService.__init__` 的 `DeviceWatcher` 建構改為：
```python
        from sw_core.platform_backends import select_device_backend
        if select_device_backend() == "win":
            from sw_core.device_source import WindowsDeviceSource
            source = WindowsDeviceSource(exclude_coms=_load_exclude_coms())
        else:
            source = None  # 預設 PosixDeviceSource
        self._watcher = DeviceWatcher(
            by_id_dir, self._on_device_change,
            extra_scan_dirs=[by_path_dir],
            on_tick=self._sessions.reconcile_readiness,
            source=source,
        )
```
（`_load_exclude_coms` 從 `config.yaml` 的 `windows.exclude_coms` 讀，缺則空集合。）

- [ ] **Step 3: 跑全測試確認 POSIX 零回歸**

Run: `python3 -m pytest -q tests/`
Expected: PASS（無新失敗）

- [ ] **Step 4: Commit**

```bash
git add sw_core/daemon.py sw_core/service.py
git commit -m "feat(daemon): daemon/service 接 selector，Windows 走 TCP/msvcrt/SERIALCOMM（#84 PORT-4）"
```

---

## Task 11: 閒置 COM 接管 + 被佔用跳過

**Files:**
- Modify: `sw_core/session_manager.py`（attach 路徑：開埠失敗則跳過、下輪重試；確認 passthrough 預設）
- Test: `tests/test_session_bind.py` 或新增 `tests/test_windows_claim.py`（注入式）

**Interfaces:**
- Consumes: `open_serial_port`（試開判閒置）、既有 dynamic session 建立路徑（`update_devices`）。
- Produces:（行為）開不起來的 COM 不建 session、不污染狀態；profile 綁定覆寫 passthrough 預設。

> 既有 dynamic session 建立已走 `SessionManager.update_devices` → attach thread。本任務確認「開埠失敗 → session 進入可重試而非永久錯誤狀態」，並補測試。若既有行為已涵蓋（attach 失敗自動退回 DETACHED 待下輪），則僅補測試與註解。

- [ ] **Step 1: 寫注入式測試**（驗證：被佔用埠不建 ready session、藍牙埠 open 不被呼叫）

```python
# tests/test_windows_claim.py
from __future__ import annotations
from sw_core.device_source import WindowsDeviceSource
from sw_core import device_source as ds


def test_bluetooth_never_opened(monkeypatch):
    opened = []
    monkeypatch.setattr(ds, "_read_serialcomm", lambda: {
        r"\Device\BthModem0": "COM3", r"\Device\Serial2": "COM8",
    })
    monkeypatch.setattr(ds, "_read_bt_ports", lambda: {"COM3"})
    devices = WindowsDeviceSource().scan()
    # 藍牙 COM3 不在掃描結果 → 不可能被接管/開啟
    assert "COM3" not in devices
    assert "COM8" in devices
```

- [ ] **Step 2: 跑測試確認失敗/通過基線**

Run: `python3 -m pytest tests/test_windows_claim.py -v`
Expected: 視既有狀況；若 import 既成則應 PASS（此測試核心驗證 scan 不含藍牙）

- [ ] **Step 3: 確認/補強 session attach 對開埠失敗的處理**

檢查 `session_manager.py` attach 路徑：若 `open_serial_port(...).open()` 拋例外（埠被佔），session 應退回 DETACHED 並於下一輪 `update_devices` 重試，不得卡死或污染 RELEASED。必要時加防護與繁中註解。

- [ ] **Step 4: 跑全測試**

Run: `python3 -m pytest -q tests/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_windows_claim.py sw_core/session_manager.py
git commit -m "feat(session): 閒置 COM 接管/被佔用跳過重試 + 藍牙不開啟測試（#84 PORT-4）"
```

---

## Task 12: PyInstaller 單一執行檔打包

**Files:**
- Create: `serialwrap.spec`、`scripts/build_windows.ps1`
- Modify: `pyproject.toml`（加 dev optional `pyinstaller`）

**Interfaces:**
- Produces: `dist/serialwrapd.exe`、`dist/serialwrap.exe`（one-file，內嵌 `sw_core/assets`）。

- [ ] **Step 1: pyproject.toml 加 dev 依賴**

於 `[project.optional-dependencies]` 加：
```toml
dev = ["pyinstaller>=6"]
```
（若已有 dev 群組則 append。）

- [ ] **Step 2: 建立 PyInstaller spec**（`serialwrap.spec`，兩個 entry）

```python
# serialwrap.spec — PyInstaller one-file（serialwrapd + serialwrap），內嵌 sw_core/assets
# 用法：pyinstaller serialwrap.spec
import os
datas = [("sw_core/assets", "sw_core/assets")]
a_d = Analysis(["sw_core/daemon.py"], datas=datas, hiddenimports=["winreg", "msvcrt"])
pyz_d = PYZ(a_d.pure)
exe_d = EXE(pyz_d, a_d.scripts, a_d.binaries, a_d.datas, name="serialwrapd", console=True)

a_c = Analysis(["sw_core/cli.py"], datas=datas)
pyz_c = PYZ(a_c.pure)
exe_c = EXE(pyz_c, a_c.scripts, a_c.binaries, a_c.datas, name="serialwrap", console=True)
```
> 註：daemon.py / cli.py 需有 `if __name__ == "__main__": main()` 入口；若無則於檔末補上。

- [ ] **Step 3: 建立建置腳本**

```powershell
# scripts/build_windows.ps1 — 在 Windows 產出單一執行檔
param([switch]$Clean)
if ($Clean) { Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue }
python -m pip install --disable-pip-version-check pyinstaller>=6
python -m PyInstaller --noconfirm serialwrap.spec
Write-Output "產出："; Get-ChildItem dist\*.exe | Select-Object Name, Length
```

- [ ] **Step 4: 確認 daemon.py / cli.py 有 __main__ 入口**

檢查兩檔末是否有 `if __name__ == "__main__": ...`；無則補（呼叫各自 `main()`）。

- [ ] **Step 5: Commit**

```bash
git add serialwrap.spec scripts/build_windows.ps1 pyproject.toml sw_core/daemon.py sw_core/cli.py
git commit -m "build(windows): PyInstaller one-file 打包 serialwrapd/serialwrap（#84 PORT-4）"
```

> 實機建置驗收（Windows）於 Task 14 進行。

---

## Task 13: 文件與變更紀錄同步

**Files:**
- Modify: `README.md`、`CLAUDE.md`、`CHANGELOG.md`、（必要時）`VERSION`、`docs/**`

**Interfaces:** 無（文件）。

- [ ] **Step 1: README/docs 補 Windows daemon 段**

說明：Windows daemon 走 TCP loopback（`tcp://127.0.0.1:48700`，可 `--socket`/env 覆寫）、自動接管閒置非藍牙 COM、藍牙永不接管（`windows.exclude_coms` 可再排除）、`serialwrapd.exe`/`serialwrap.exe` 用法。對齊 R-16/R-18。

- [ ] **Step 2: CLAUDE.md 架構段補平台 seam**

於「高層架構 / 關鍵慣例」補：三個平台 seam（rpc/lock/device）分檔 + selector；Windows 接管語意。

- [ ] **Step 3: CHANGELOG `[Unreleased]` 記錄**

新增條目：Windows daemon（PORT-4）——TCP RPC、msvcrt singleton、SERIALCOMM 列舉 + 藍牙排除、PyInstaller 打包。

- [ ] **Step 4: 跑 policy_check**

Run: `python3 -m policy_check --repo .`
Expected: PASS（Windows 本地 R-14/R-16 已知偽失敗以 Linux CI 為準）

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md CHANGELOG.md docs/
git commit -m "docs: Windows daemon（PORT-4）說明與架構同步 + CHANGELOG（#84）"
```

---

## Task 14: 實機驗證（loopback CH340 COM8）

**Files:** 無（驗證）。

**Interfaces:** 無。

- [ ] **Step 1: 建置 exe**

Run（Windows）: `pwsh scripts/build_windows.ps1 -Clean`
Expected: `dist/serialwrapd.exe`、`dist/serialwrap.exe` 產出

- [ ] **Step 2: 起 daemon，驗接管/排除**

Run: `dist\serialwrapd.exe`（前景），另開 `dist\serialwrap.exe session list`
Expected: 接管 COM8（passthrough、ATTACHED）；COM3/COM4 不在清單

- [ ] **Step 3: COM8 loopback TX/RX**

Run: `serialwrap session attach --selector COM8` → 送命令 → `serialwrap cmd result-tail ...`
Expected: TX 由 loopback 回讀、WAL 可見

- [ ] **Step 4: 雙開 singleton 驗證**

Run: 再起一個 `serialwrapd.exe`
Expected: 第二個以 `DAEMON_ALREADY_RUNNING` 退出

- [ ] **Step 5: 最終 gate**

Run: `python3 -m pytest -q tests/` 且 `python3 -m policy_check --repo .`
Expected: 全綠 / 通過（無新失敗）

- [ ] **Step 6: 記錄驗證結果於 PR body**（截圖/輸出）

---

## Self-Review

- **Spec coverage**：`cross-platform-rpc`→Task 1/3/4/5/10；`windows-device-claim`→Task 6/7/8/11；`runtime-paths`(endpoint)→Task 9/10；`packaging-distribution`(exe)→Task 12/14。皆有對應任務。
- **Type 一致性**：RpcServer 介面 `start/serve_forever/stop`（Task 1/4）一致；`SingletonLock`/`WindowsSingletonLock` `acquire/release`（Task 2/5）一致；`DeviceSource.scan() -> dict[str, DeviceInfo]`（Task 6/7/8）一致；`exclude_bluetooth(serialcomm, bt_ports, exclude)` 簽章 Task 6 定義、Task 7 使用一致。
- **Placeholder**：無 TBD/TODO；每 code step 有完整 code。
- **風險備註**：Task 11 若既有 attach 失敗處理已涵蓋重試，則僅補測試（已於任務內註明判斷準則）。
