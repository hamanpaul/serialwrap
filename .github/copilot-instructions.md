# Copilot Instructions — serialwrap

> 本專案文件、註解、commit message 一律使用**繁體中文**。Copilot 回覆也請使用繁體中文。

## 架構

serialwrap 是面向多 Agent UART 協作的 broker 架構，三大元件：

- **`serialwrapd.py`** — singleton daemon，獨占實體 UART。以 asyncio 事件迴圈運行 JSON-RPC Unix socket server（`sw_core/rpc.py`）。所有 UART 讀寫皆經此 daemon。
- **`serialwrap`**（CLI client）— 子命令式 CLI（`sw_core/cli.py`），透過 Unix socket 發送 JSON-RPC 呼叫（`sw_core/client.py`）。
- **`serialwrap-mcp`**（`sw_mcp/server.py`）— MCP adapter，將 MCP 工具名稱（如 `serialwrap_submit_command`）透過 `_TOOL_MAP` 轉換為內部 RPC 方法（如 `command.submit`）。已安裝至 Codex 環境作為 MCP server 使用。

### 內部模組對照

```
sw_core/service.py    SerialwrapService — 頂層調度器，擁有所有子系統，路由 RPC 方法
sw_core/arbiter.py    CommandArbiter — 每 session 優先佇列，單寫入者保證（每 session 一條 worker thread）
sw_core/session_manager.py  SessionManager — session 生命週期（DETACHED → ATTACHING → READY）、裝置綁定、alias 解析
sw_core/uart_io.py    UARTBridge — serial port + PTY bridge，TX/RX 搭配 WAL 記錄
sw_core/login_fsm.py  ensure_ready() — 平台相依 login FSM（bcm vs prpl 路徑）
sw_core/wal.py        WalWriter — 雙軌 append-only log：raw.wal.ndjson（機器可讀）+ raw.mirror.log（人類可讀）
sw_core/device_watcher.py  DeviceWatcher — 輪詢 /dev/serial/by-id/ 偵測 hotplug 事件
sw_core/config.py     Profile/target YAML 載入：ProfileTemplate → SessionProfile 合併
sw_core/alias_registry.py  Alias → session_id / by-id 映射
```

### 核心不變量

- 單 UART 單寫入者 — `CommandArbiter` 透過優先佇列 worker thread 保證每 session 同時只有一條命令執行。
- 每筆 WAL 事件帶 `seq + timestamp + source + cmd_id + crc32`，確保完整可追溯。
- Session 狀態機：`DETACHED` → `ATTACHING` → `READY`。僅 `READY` 狀態接受命令。
- 裝置綁定使用 `/dev/serial/by-id/` 路徑（跨重開機穩定）。同一 `by-id` 裝置重新插入時自動掛回。

### IPC 協定

JSON-RPC over Unix socket（`/tmp/serialwrap/serialwrapd.sock`）。每行一個 JSON 物件，換行分隔。所有回應包含 `"ok": true/false`，錯誤回應包含 `error_code`。

### Profile 系統

`profiles/` 中的 YAML 檔定義 `ProfileTemplate`（login/prompt/UART 參數）與 `targets`（將 template 綁定到 COM slot + 裝置）。Template 與 target 透過 `config.py` 的 `_merge_session()` 合併，target 欄位覆蓋 template 預設值。

## 建置與測試

無建置步驟。純 Python，執行期依賴 `pyyaml` 與 `pyserial`。

```bash
# 執行全部測試
python3 -m unittest discover -s tests -v

# 執行單一測試檔
python3 -m unittest tests.test_wal -v

# 執行單一測試方法
python3 -m unittest tests.test_wal.TestWal.test_append_and_tail -v
```

測試框架使用 `unittest`（非 pytest）。E2E 測試（`test_multiagent_e2e.py`）會啟動真實 daemon 搭配 PTY 模擬 target，測試完整流程包含 arbiter 序列化。

## 慣例

- **語言**：Python 3.10+，所有檔案使用 `from __future__ import annotations`。所有函式簽章附型別提示。
- **Dataclasses**：值物件使用 `frozen=True`（`UartProfile`、`ProfileTemplate`、`SessionProfile`、`DeviceInfo`）。可變執行期狀態使用一般 dataclass（`SessionRuntime`）。
- **RPC 路由**：`SerialwrapService.rpc()` 為平面 if/elif 分派器，無動態方法註冊。新增 RPC 方法直接加分支。
- **JSON 輸出**：CLI 與 MCP 一律輸出緊湊 JSON（`separators=(",",":")`）並使用 `ensure_ascii=False`。WAL 使用 `sort_keys=True` 確保穩定序列化。
- **錯誤模式**：所有 RPC 回應為 `dict[str, Any]`，包含 `ok: bool`，失敗時附 `error_code: str`。例外不跨越 RPC 邊界。
- **執行緒**：共享狀態使用 `threading.RLock`，停止訊號使用 `threading.Event`，worker 使用 daemon thread。核心不使用 asyncio — 僅 RPC server 使用 asyncio。
- **文件語言**：註解、docstring、README、規格書一律使用繁體中文。
- **`serialwrap_lib.py`**：v1 舊版函式庫（tmux/minicom 方式）。v2 daemon 架構（`sw_core/`）為目前主力。

## 安裝

```bash
./install.sh              # 安裝至 ~/.paul_tools/
./install.sh /custom/path # 安裝至自訂路徑
```
