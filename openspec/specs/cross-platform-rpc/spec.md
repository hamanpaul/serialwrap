# cross-platform-rpc Specification

## Purpose
TBD - created by archiving change windows-daemon-84. Update Purpose after archive.
## Requirements
### Requirement: 平台選擇的 RPC transport 後端
RPC server SHALL 透過共用 core 介面與依平台選擇的後端提供服務：POSIX MUST 使用 AF_UNIX（`asyncio.start_unix_server`），Windows MUST 使用 TCP loopback（`127.0.0.1`，`asyncio.start_server`）。後端選擇 SHALL 依 `sys.platform`，並可由 `SERIALWRAP_RPC_BACKEND`（`auto`/`posix`/`win`）覆寫。POSIX 後端的行為 MUST 與既有 `JsonRpcUnixServer` 逐位元相同。

#### Scenario: POSIX 走 AF_UNIX 不變
- **WHEN** 在 Linux 以預設設定啟動 daemon
- **THEN** RPC server 綁定 AF_UNIX socket，且 line-delimited JSON 請求/回應行為與既有 `JsonRpcUnixServer` 完全一致

#### Scenario: Windows 走 TCP loopback
- **WHEN** 在 Windows 以預設設定啟動 daemon
- **THEN** RPC server 綁定 `127.0.0.1` 的 TCP 埠，client 經 `tcp://127.0.0.1:<port>` 可送出 RPC 並取得相同格式回應

#### Scenario: 後端可由 env 覆寫
- **WHEN** 設定 `SERIALWRAP_RPC_BACKEND=posix` 於非預期平台啟動
- **THEN** selector 回傳 POSIX 後端（若該平台不支援則以明確錯誤拒絕，不靜默退化）

### Requirement: 既有 RPC import 相容 shim
`sw_core/rpc.py` SHALL 保留為指向平台模組的 re-export shim，使既有 `from sw_core.rpc import JsonRpcUnixServer` 與相依測試零改動仍可運作。

#### Scenario: 既有 import 仍解析
- **WHEN** 既有程式或測試執行 `from sw_core.rpc import JsonRpcUnixServer`
- **THEN** 解析到 POSIX 後端實作，行為不變

### Requirement: 平台選擇的 singleton lock 後端
daemon 單例保證 SHALL 透過共用 core 介面與依平台選擇的 lock 後端達成：POSIX MUST 使用 `fcntl.flock`，Windows MUST 使用 `msvcrt.locking` 對 lock 檔做獨佔鎖。兩平台皆 SHALL 以「試連 endpoint」做存活探測——連得上視為既有 daemon 在跑（`DAEMON_ALREADY_RUNNING`），連不上則視 lock/endpoint 為 stale 並回收。POSIX 後端行為 MUST 與既有 `SingletonLock` 逐位元相同。

#### Scenario: 第二個 daemon 被擋
- **WHEN** 已有 daemon 持有 lock 且 endpoint 可連，再啟動第二個 daemon
- **THEN** 第二個以 `DAEMON_ALREADY_RUNNING` 失敗並非零退出，不動到既有 daemon

#### Scenario: stale lock 回收
- **WHEN** lock 檔存在但 endpoint 連不上（前一 daemon 已死）
- **THEN** 新 daemon 回收 stale lock/endpoint 後正常啟動

