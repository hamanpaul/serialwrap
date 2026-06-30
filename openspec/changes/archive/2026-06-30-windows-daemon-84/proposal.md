## Why

#84 已交付 PORT-1（序列埠抽象）與 PORT-2（Windows TCP human console），但 serialwrapd daemon 本身仍是 Linux-only：RPC server 綁 `asyncio.start_unix_server`（AF_UNIX）、singleton lock 綁 `fcntl.flock`、裝置偵測綁 `/dev/serial/by-id`。因此 Windows 上目前只跑得起 bridge，沒有 daemon／CLI／agent 路徑。本變更補上 PORT-4 的核心：讓 daemon 能在 Windows 以單一執行檔前景跑起來、被 CLI/agent 連到、並自動接管閒置 COM 埠。

## What Changes

- 將三個碰 OS 的 seam 各自抽出 core 介面 + 兩個獨立平台實作（POSIX 既有邏輯整段搬位、行為 byte-identical；Windows 為新增 sibling 模組）：
  - **RPC server**：`rpc_posix.py`（AF_UNIX，由現 `JsonRpcUnixServer` 搬入）／`rpc_win.py`（`asyncio.start_server` TCP `127.0.0.1`）。
  - **Singleton lock**：`lock_posix.py`（`fcntl.flock`，由現 `daemon_lock.py` 搬入）／`lock_win.py`（`msvcrt.locking` 獨佔檔 + TCP 存活探測）。
  - **Device source**：`DeviceWatcher` 的 loop/diff/threading 留在 core，`_scan()` 抽成可注入的 `DeviceSource`；POSIX 為 by-id/by-path 掃描、Windows 為 registry `SERIALCOMM` 列舉。
- Windows daemon 自動接管所有**閒置**（可獨佔開啟）的非藍牙 COM 埠，預設建立 `passthrough` session（profile 有綁則覆寫）；**藍牙 COM 永不接管**（`BTHENUM` 列舉 + `BthModem` 雙判據，外加 config 覆寫清單）。
- RPC endpoint 改為平台感知：Windows 預設 `tcp://127.0.0.1:48700`，寫入 `config.yaml` 的 `socket_path`；client 端既有 `tcp://` 支援，CLI/agent 零改動。
- 以 PyInstaller 將 `serialwrapd` 與 `serialwrap` 打成單一 `.exe`（內嵌 `sw_core/assets`），附 Windows 建置腳本。
- `rpc.py`／`daemon_lock.py` 保留為指向平台模組的 re-export shim，確保既有 import 與測試零改動。

非目標（明確 defer）：Windows service／systemd 對應的監管模式（PORT-8）；`/proc` peer probe 的跨平台化（PORT-5）；by-id 等價穩定識別（不做，改用 Windows 原生 COM 機制）。

## Capabilities

### New Capabilities
- `cross-platform-rpc`: 依平台選擇的 RPC transport 與 singleton lock，後端（POSIX AF_UNIX/flock、Windows TCP-loopback/msvcrt）藏在共用 core 介面之後。
- `windows-device-claim`: Windows 原生 COM 列舉（registry `SERIALCOMM`）、藍牙排除、與閒置埠自動接管成 passthrough session 的行為契約。

### Modified Capabilities
- `runtime-paths`: socket/lock endpoint 預設改為平台感知（Windows 回 `tcp://` endpoint）。
- `packaging-distribution`: 新增 Windows 單一執行檔（PyInstaller）建置產物。

## Impact

- 新增：`sw_core/rpc_posix.py`、`sw_core/rpc_win.py`、`sw_core/lock_posix.py`、`sw_core/lock_win.py`、`sw_core/device_source.py`（介面 + posix/win 實作）、`sw_core/platform_backends.py`（selector）、`scripts/build_windows.ps1`、PyInstaller spec。
- 改動：`sw_core/rpc.py`、`sw_core/daemon_lock.py`（轉 re-export shim）、`sw_core/device_watcher.py`（注入 `DeviceSource`）、`sw_core/constants.py`（平台感知 endpoint/path）、`sw_core/daemon.py`／`sw_core/service.py`（改走 selector）。
- 相依：Windows 後端僅用 stdlib（`winreg`、`msvcrt`、`asyncio` TCP）+ 既有 `pyserial`；建置期新增 `pyinstaller`（dev-only）。
- 測試：新增跨平台 unit（TCP RPC round-trip、藍牙排除 pure function、endpoint 解析）與 Win32-gated unit（lock、registry 列舉）；POSIX 既有 `tests/` 經 shim 維持全綠；Windows 實機以 loopback CH340 **COM8** 驗證接管與 attach/command。
