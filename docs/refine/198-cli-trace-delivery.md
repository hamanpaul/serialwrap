# #198 Task 3 交付：#171 最小 CLI trace

日期：2026-09-13

本 task 交付的是 **CLI 端最小控制平面 trace**，範圍只涵蓋既有 CLI→RPC 路徑；不改 daemon/service/manager/rpc server，也不宣稱已完成 #171 後續 daemon logging。

## 交付內容

- `serialwrap` 新增 `-v` / `-vv` verbosity 開關：
  - `-v` → `INFO`
  - `-vv` → `DEBUG`
  - 未帶 `-v` 時，才讀 `SERIALWRAP_LOG_LEVEL`
  - 無效 `SERIALWRAP_LOG_LEVEL` 靜默退回 `WARNING`
  - `serialwrap --help` 與 README `serialwrap-help` marker 已同步公開此旗標
- CLI trace 使用專用 logger `serialwrap.cli_trace`：
  - `propagate=False`
  - 自有 `stderr` handler 冪等重設，不污染 root / `serialwrap` logger
  - 預設 `WARNING`，所以既有 stdout/stderr 契約維持不變
- `_run_rpc()`、`event` 分派、`daemon stop`、`setup` 的既有 `health.ping`／`mcu.status` 前置檢查，以及 `daemon start` **前置 health probe / 就緒等待** 內的既有 RPC 呼叫，統一走同一個 trace wrapper。`setup` 仍保留每個 probe 各自一次 endpoint 解析、0.5 秒 timeout、原本的順序與 best-effort／`FLASHING_BUSY` 行為；trace 不新增 probe 或 RPC。
- `sw_core.client.rpc_call()` 新增 **內部用** `trace_sink` metadata 通道，只回報：
  - `elapsed_ms`
  - `retry_count`
  - 最後一次 **主請求** attempt 的 `errno` / `errno_name`

## Trace 格式

stderr trace 僅輸出一行白名單 JSON，欄位固定如下：

```json
{
  "endpoint_transport": "unix",
  "endpoint_id": "6d8d0f4f0ad0f0d2a5b1a40b9b8d8f89f8938b0f47e8a57bce74f8d69d1f9fb1",
  "endpoint_source": "--socket",
  "method": "session.list",
  "elapsed_ms": 3,
  "error_code": "SOCKET_ERROR",
  "errno": 2,
  "errno_name": "ENOENT",
  "retry_count": 0,
  "timeout_s": 5.0
}
```

### 識別規則

- Unix endpoint：一律對完整 path 做 SHA-256，輸出完整 64 hex。
- TCP endpoint：
  - 只對 literal loopback（`127.0.0.1` / `localhost` / `::1`）保留 `host:port`
  - 其他 host 一律 SHA-256
- 不輸出 raw path、basename、params、command、response body、secret、UART/WAL 內容。

## 行為界線

- `elapsed_ms` 由 `client.rpc_call()` 入口量到最終回應，**包含**既有 retry/backoff 與 TIMEOUT enrich。
- `retry_count` 只反映既有唯讀白名單 retry；mutating RPC 沒有新增 retry。
- trace wrapper 不會因 `trace_sink`/logger 相關內部錯誤而自動重送主請求；主請求次數仍只受既有 client retry policy 與使用者明確 `--retries` 控制。
- `errno` 只取最後一次 **主請求** attempt 的 `OSError.errno`：
  - retry 後成功 → `errno=null`
  - TIMEOUT 後 enrich 的 `health.ping` / `health.status` 若失敗，也**不會**污染主請求 trace
- `event add` trace 的 method 會記真正送出的 `event.rule_set`，不是 CLI 子命令名稱。

## 使用範例

```bash
serialwrap -v --socket /tmp/serialwrapd.sock session list
SERIALWRAP_LOG_LEVEL=INFO serialwrap session list
serialwrap -v --endpoint tcp://127.0.0.1:48700 event status --selector COM0
```

## 非目標與 defer

- 本輪 **沒有** 加 daemon 檔案 sink、rotation 或 inbound/outbound RPC server trace。
- 本輪 **沒有** 改變 stdout JSON 形狀；trace metadata 僅走內部 sink + stderr logger。
- `TIMEOUT` 只表示 CLI 停止等待，**不代表 daemon 未執行或操作一定失敗**。
- endpoint hash 是最小揭露，不是強匿名；已知 socket path 的人仍可離線比對雜湊。

## 測試覆蓋與 regression-case 評估

- 新增 `tests/test_cli_diagnostics.py`：
  - 預設 byte compatibility
  - `--help` 露出 `-v/--verbose`
  - `-v` / `SERIALWRAP_LOG_LEVEL` precedence
- `daemon start` already-running 與前置 probe failure trace
- `setup` 的 `health.ping`／`mcu.status` success、health failure、`FLASHING_BUSY` early return，以及 quiet/verbose 相同 RPC 順序、次數與 0.5 秒 timeout
  - `trace_sink` 相關 `TypeError` 不得造成 `session.recover` / `command.submit` 額外 RPC
  - 重複 `main()` 不殘留 trace/handler
  - `ENOENT` / `ECONNREFUSED` / `EACCES`
  - retry 後 success `errno=null`
  - TIMEOUT enrich 不污染主請求 errno
  - config fallback source 與 probe 次數
  - `event.rule_set` 真正 method 與 trace 白名單
- logger 隔離
- `SERIALWRAP_LOG_LEVEL` 的空白、有效名稱／合法數字，以及 `--1`、`²`、過長數字等無效值安靜回 `WARNING`；一般 RPC CLI 仍正常送出一次 RPC
- 相關既有 CLI / endpoint / event / timeout / Windows seam 測試已回歸。
- 本缺陷可用 pytest + transport seam 完整覆蓋，**不需新增 `regression/` 真機 case**；本輪也沒有宣稱實際 Windows、真 daemon 或真 UART 驗證。
