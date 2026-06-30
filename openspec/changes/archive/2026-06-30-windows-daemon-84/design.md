## Context

#84 的 PORT-1（`sw_core/serial_port.py` 雙後端）與 PORT-2（Windows TCP human console）已 merged。但 daemon 本體仍 Linux-only，三個碰 OS 的 seam 把它釘在 POSIX：

- **RPC server**（`sw_core/rpc.py`）：`asyncio.start_unix_server` + `os.chmod`/`shutil.chown` socket 檔。
- **Singleton lock**（`sw_core/daemon_lock.py`）：`fcntl.flock` + AF_UNIX 存活探測。
- **Device source**（`sw_core/device_watcher.py::_scan`）：掃 `/dev/serial/by-id`、`by-path`。

關鍵既有資產：**client 端（`sw_core/client.py`）已支援 `tcp://host:port`**，`_resolve_endpoint`／`runtime_config` 已以字串 endpoint 運作；`DeviceWatcher` 的 loop/diff/threading 已與 `_scan()` 解耦得很乾淨。這讓本變更可只動 server 與平台 seam，core 業務邏輯（`service`/`arbiter`/`session_manager`/`wal`/`uart_io`）零改動。

精神對齊使用者明示原則：**core 共一份 code、碰 OS 的層各平台各自串接**，POSIX 既有邏輯「搬位、不改寫」（byte-identical），Windows 為新增 sibling 模組。

## Goals / Non-Goals

**Goals:**
- daemon 能在 Windows 以單一前景執行檔跑起來，CLI/agent 經 TCP loopback 連到，singleton 生效。
- Windows 以原生 COM 機制（registry `SERIALCOMM`）列舉，自動接管閒置非藍牙埠為 passthrough session；藍牙 COM 永不接管。
- POSIX 路徑行為 byte-identical；既有 `tests/` 經 re-export shim 零改動全綠。
- 產出 `serialwrapd.exe` / `serialwrap.exe`（PyInstaller one-file）。

**Non-Goals:**
- Windows service／systemd 對應的監管模式（PORT-8）。
- `/proc` peer probe 的跨平台化（PORT-5）。
- by-id 等價的跨重啟穩定識別（明確不做，改用 Windows 原生 COM 機制）。
- Windows 上的 `/dev/ttyMCU` PTY-bridge（#55 已定為 POSIX-only，Windows flash 走 #54 device-release）。

## Decisions

### D1：三個 seam 各抽 core 介面 + 兩個獨立平台實作（不是單檔雙後端）
- 介面：`RpcServer`（`start/stop/serve_forever`）、`SingletonLock`（`acquire/release`）、`DeviceSource`（`scan() -> dict[key, DeviceInfo]`）。
- POSIX 實作 `rpc_posix.py` / `lock_posix.py` / `device_source_posix.py` —— 由現 `rpc.py` / `daemon_lock.py` / `DeviceWatcher._scan` **整段搬入**，邏輯不改。
- Windows 實作 `rpc_win.py` / `lock_win.py` / `device_source_win.py` —— 新增 sibling。
- selector `platform_backends.py` 依 `sys.platform` 回後端，env `SERIALWRAP_RPC_BACKEND` / `SERIALWRAP_LOCK_BACKEND` 可覆寫（對齊 PORT-1 `open_serial_port` 的 `SERIALWRAP_SERIAL_BACKEND` 慣例）。
- **替代方案**：單檔雙後端（同 `serial_port.py`）。使用者明確選擇「各自分開」，故採分檔；代價是 POSIX code 需搬位（非原地不動），以 re-export shim 補償相容性。

### D2：Windows RPC 走 TCP loopback `127.0.0.1`
- Python `asyncio` 在 Windows 不支援 AF_UNIX server；TCP loopback 與 PORT-2 human console 同模式，且 client 已支援 `tcp://`。
- 預設固定埠 `48700`（可覆寫），daemon 起來寫入 `config.yaml::socket_path`，`_resolve_endpoint` 既有邏輯直接消費。
- **替代方案**：Win10+ OS 級 AF_UNIX（Python asyncio 未包，且需處理檔案路徑語意差異，否決）；named pipe（client 需新 transport、TeraTerm/agent 連法不一致，否決）。
- **安全**：loopback 埠對本機任意行程可連，弱於 AF_UNIX 檔權限。單人 Windows 開發機可接受；後續可加 per-daemon token（記 `config.yaml`、RPC 首訊息驗證）作為 follow-up，不在本輪。

### D3：Windows singleton lock = `msvcrt.locking` 獨佔檔 + TCP 存活探測
- lock 檔落在 `RUN_DIR`（Windows 解析到 `~/.local/state/serialwrap/run`）；`msvcrt.locking(LK_NBLCK)` 取獨佔，持有到 process 結束。
- 存活探測沿用 POSIX 語意：試 TCP connect endpoint，連得上 → `DAEMON_ALREADY_RUNNING`；refused → stale 回收。
- **替代方案**：Win32 named mutex（需 pywin32 或 ctypes，徒增相依，否決）。

### D4：Windows 裝置列舉 = registry `SERIALCOMM`，藍牙雙判據排除
- `winreg` 讀 `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM`（stdlib，輕量、權威）。
- 藍牙排除：主判據掃 `HKLM\SYSTEM\CurrentControlSet\Enum\BTHENUM\**\Device Parameters\PortName`；輔助判據 `SERIALCOMM` value-name 含 `BthModem`；外加 config `windows.exclude_coms`。實機已驗：COM3/COM4=`BthModem`+`BTHENUM`、COM8=CH340。
- 排除邏輯抽成 pure function（吃「列舉結果 dict」吐「候選清單」），可在 Linux CI 餵注入資料測。
- **替代方案**：WMI `Win32_SerialPort`（漏報部分 USB-serial，且需 `pythoncom`/wmi，否決）。

### D5：接管行為沿用既有 dynamic session 流程
- `DeviceSource` 把 COM 當 device key 餵進既有 `DeviceWatcher` → `service` 的 on_change → 既有 dynamic session 建立路徑；接管預設 `passthrough`，profile 綁定則覆寫（與 POSIX 動態 session 同語意，只是 device key 從 by-id 換成 COM 名）。
- 「閒置」判定 = 試開 `\\.\COMx` 獨佔成功；失敗則本輪跳過、下輪重試（沿用 poll 迴圈）。

### D6：相容 shim
- `rpc.py` → `from .rpc_posix import JsonRpcUnixServer`；`daemon_lock.py` → `from .lock_posix import SingletonLock`。既有 import 與 `tests/` 零改動。

## Risks / Trade-offs

- [TCP loopback 任意本機行程可連，無檔權限保護] → 單人開發機可接受；列為 follow-up 加 token 驗證，design 預留位置。
- [POSIX code 搬位可能不慎改到行為] → 以「整段搬入 + re-export shim + 既有 tests 全綠」三重把關；搬位 commit 與 Windows 新增 commit 分開，便於 review/bisect。
- [PyInstaller one-file 漏帶 `sw_core/assets`] → build spec 明確 `--add-data`，並加「exe 跑 `serialwrap --help` / daemon 起得來」的 smoke 驗收。
- [registry 列舉在不同 Windows 版本欄位差異] → 以實機 COM3/4/8 驗證為基準，藍牙判據雙保險 + config 覆寫兜底。
- [TCP 埠衝突（48700 被佔）] → `--socket`/env 可改埠；lock 存活探測連不上時不誤判既有 daemon。

## Migration Plan

- 純附加 + 搬位，POSIX 行為不變，無資料遷移。
- 部署：Linux 端無感（shim 維持相容）；Windows 端新增 exe 產物。
- 回退：還原平台 seam 檔與 shim 即回到變更前；core 未動，風險低。

## Open Questions

- RPC token 驗證是否本輪納入？（暫定否，列 follow-up；如要納入再開 spec。）
- `serialwrap.exe` 是否一併打包，或本輪只打 `serialwrapd.exe`？（暫定一併打，agent/人都用得到。）
