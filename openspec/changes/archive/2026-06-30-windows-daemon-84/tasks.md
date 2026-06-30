## 1. POSIX 搬位 + 相容 shim（零回歸基線，獨立 commit）

- [x] 1.1 新增 `sw_core/rpc_posix.py`，將 `sw_core/rpc.py` 的 `JsonRpcUnixServer` 整段搬入（邏輯不改）
- [x] 1.2 `sw_core/rpc.py` 改為 re-export shim（`from sw_core.rpc_posix import JsonRpcUnixServer`）
- [x] 1.3 新增 `sw_core/lock_posix.py`，將 `sw_core/daemon_lock.py` 的 `SingletonLock` 整段搬入
- [x] 1.4 `sw_core/daemon_lock.py` 改為 re-export shim（`from sw_core.lock_posix import SingletonLock`）
- [x] 1.5 跑 `python3 -m pytest -q tests/` 確認既有測試全綠（僅 CLAUDE.md 列的既有失敗，無新失敗）

## 2. core 介面 + selector

- [x] 2.1 定義 `RpcServer` / `SingletonLock` 的最小共用介面（型別契約：`start/stop/serve_forever`、`acquire/release`），POSIX/Windows 實作皆遵循
- [x] 2.2 新增 `sw_core/platform_backends.py` selector：依 `sys.platform` + `SERIALWRAP_RPC_BACKEND`/`SERIALWRAP_LOCK_BACKEND`（`auto`/`posix`/`win`）回傳後端；不支援平台以明確錯誤拒絕（不靜默退化）
- [x] 2.3 撰寫 selector 單元測試（auto/posix/win 分支、env 覆寫、不支援平台拒絕）

## 3. Windows RPC 後端（TCP loopback）

- [x] 3.1 先寫測試：`rpc_win` 在 `127.0.0.1` 起 server，client 經 `tcp://` 送 line-delimited JSON 請求/回應 round-trip（跨平台可跑，Linux CI 亦綠）
- [x] 3.2 實作 `sw_core/rpc_win.py`：`asyncio.start_server` 綁 `127.0.0.1`，重用與 POSIX 相同的 `_handle_client` 行 buffer/分派/blocking_methods 語意（共用 handler，不複製業務邏輯）
- [x] 3.3 確認 Windows 不做 `os.chmod`/`shutil.chown`（POSIX-only），且 stop 正確關閉 listener

## 4. Windows singleton lock 後端

- [x] 4.1 先寫測試（Win32-gated）：第二個 acquire 在 endpoint 可連時 `DAEMON_ALREADY_RUNNING`；endpoint 連不上時回收 stale 後成功
- [x] 4.2 實作 `sw_core/lock_win.py`：`msvcrt.locking` 對 `RUN_DIR` 下 lock 檔取獨佔 + 試 TCP connect endpoint 做存活探測；release 釋放鎖並關檔

## 5. Device source 抽象 + Windows 列舉/藍牙排除

- [x] 5.1 抽 `DeviceSource` 介面（`scan() -> dict[key, DeviceInfo]`），`DeviceWatcher` 改為注入 `DeviceSource`，loop/diff/threading 留在 core
- [x] 5.2 新增 `device_source_posix.py`：將現 `DeviceWatcher._scan`（by-id/by-path）搬入，行為不變；跑既有 watcher 測試確認綠
- [x] 5.3 先寫測試：藍牙排除 pure function 吃注入的 `SERIALCOMM`+`BTHENUM` 資料（以實機 COM3/4=藍牙、COM8=CH340 為案例）吐候選清單；含 `windows.exclude_coms` 覆寫
- [x] 5.4 實作 `device_source_win.py`：`winreg` 讀 `SERIALCOMM` 列舉、`BTHENUM` PortName + `BthModem` 雙判據排除、key=COM 名、real_path=`\\.\COMx`
- [x] 5.5 撰寫 `device_source_win` registry 解析測試（mock `winreg`，Win32-gated 或注入式）

## 6. 平台感知 endpoint + daemon/service 接線

- [x] 6.1 `sw_core/constants.py`：endpoint 預設平台感知（Windows 回 `tcp://127.0.0.1:48700`，POSIX 維持 AF_UNIX 檔路徑）；加測試確認兩平台預設值
- [x] 6.2 `daemon.py`/`service.py` 改走 `platform_backends` selector 取 RpcServer/SingletonLock/DeviceSource；daemon 起來寫入 `config.yaml::socket_path`
- [x] 6.3 確認 `_resolve_endpoint`（CLI）對 `tcp://` 既有支援可直接消費，無須改動；補一個解析測試

## 7. 閒置 COM 自動接管接線

- [x] 7.1 先寫測試：注入式 `DeviceSource` 回報 COM8 → 既有 dynamic session 路徑建立 passthrough session；profile 綁定時走該 template
- [x] 7.2 接線「閒置＝試開 `\\.\COMx` 獨佔成功」判定：成功則接管、失敗本輪跳過下輪重試
- [x] 7.3 確認被排除藍牙埠全程不被開啟（以注入資料驗證 open 未被呼叫）

## 8. 打包（單一執行檔）

- [x] 8.1 新增 PyInstaller spec 與 `scripts/build_windows.ps1`：one-file 打 `serialwrapd` 與 `serialwrap`，`--add-data` 帶入 `sw_core/assets`
- [x] 8.2 `pyproject.toml` 加 `pyinstaller`（dev/optional 群組）
- [x] 8.3 建置驗收：`dist/serialwrapd.exe`/`serialwrap.exe` 產出，`serialwrap.exe --help` 可跑、`serialwrapd.exe` 前景起得來

## 9. 文件與政策同步

- [x] 9.1 更新 `README.md`/`docs/**`：Windows daemon（TCP endpoint、接管行為、藍牙排除、exe 用法）；對齊 R-16/R-18
- [x] 9.2 更新 `CLAUDE.md` 架構段（平台 seam 模組、Windows 接管語意）
- [x] 9.3 更新 `CHANGELOG.md`（`[Unreleased]`）與 `VERSION`（若有版本變動）

## 10. 實機驗證（loopback CH340 COM8）

- [x] 10.1 在 Windows 跑 daemon → `serialwrap session list` 確認接管 COM8、排除 COM3/COM4
- [x] 10.2 COM8 passthrough session attach → TX/RX loopback 經 WAL 可見、command 路徑通
- [x] 10.3 雙開 daemon 驗 singleton（第二個 `DAEMON_ALREADY_RUNNING`）；stale 回收驗證
- [x] 10.4 最終 `python3 -m pytest -q tests/` 全綠 + `python3 -m policy_check --repo .` 通過
