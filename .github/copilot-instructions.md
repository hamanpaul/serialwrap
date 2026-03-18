# Copilot Instructions — serialwrap

> 本專案的文件、註解、docstring、README、規格、commit message 與 Copilot 回覆一律使用**繁體中文**。

## 實際命令

本專案**沒有 build 步驟**，主體是純 Python 執行期程式。執行期依賴以 README 為準：`pyyaml`、`pyserial`，human console 路徑另外會用到 `jq` 與 `minicom`。

```bash
# 安裝
./install.sh
./install.sh /custom/path

# 執行全部測試
python3 -m unittest discover -s tests -v

# 執行單一測試檔
python3 -m unittest tests.test_wal -v
python3 -m unittest tests.test_multiagent_e2e -v

# 執行單一測試方法
python3 -m unittest tests.test_wal.TestWal.test_append_and_tail -v
```

```bash
# 常用 daemon / session smoke commands
serialwrap daemon start --profile-dir "$HOME/.paul_tools/profiles"
serialwrap daemon status
serialwrap session list
serialwrap session bind --selector COM0 --device-by-id /dev/serial/by-id/<target-by-id>
serialwrap session attach --selector COM0
```

- 既有測試框架是 `unittest`，不是 `pytest`。
- repository 內**沒有既有 lint / formatter 設定**；目前未見 `pyproject.toml`、`tox.ini`、`.flake8`、`pytest.ini` 或 `setup.cfg`。

## 高層架構

serialwrap 是一個讓多個 agent 與多個 human console 共用**同一條 UART** 的 broker 架構，核心不是單一 CLI，而是 daemon + RPC + broker pipeline。

- `serialwrapd.py`：singleton daemon。啟動時載入 profiles、建立 `SerialwrapService`，再以 `sw_core/rpc.py` 提供 JSON-RPC Unix socket server。只有這個 daemon 會直接碰 UART。
- `serialwrap`（`sw_core/cli.py`）：子命令式 CLI。每個子命令都只是 RPC client；`daemon start` 載入 runtime env（`SERIALWRAP_DAEMON_ENV_FILE` 或 legacy `~/OPI.env`），帳密則是 per-session 在 attach 時解析。
- `serialwrap-mcp`（`sw_mcp/server.py`）：MCP adapter。它不自己實作業務邏輯，只把工具名透過 `_TOOL_MAP` 映射到內部 RPC 方法。

### 主要資料流

`command.submit` 的實際路徑是：

`CLI / MCP` → `SerialwrapService.rpc()` → `_resolve_session_id()`（僅 `READY` 可送 agent 命令）→ `CommandArbiter.submit()` → 該 session 的 worker thread → `SessionManager.execute_command()` → `UARTBridge` → `WalWriter`

要理解前景命令、背景命令、interactive lease、human console 為什麼互不打架，至少要一起看這幾個檔案：

- `sw_core/service.py`：整體組裝點，持有 `CommandArbiter`、`SessionManager`、`DeviceWatcher`、`WalWriter`，也是唯一的 RPC 路由層。
- `sw_core/arbiter.py`：每個 session 一條 daemon worker thread + priority queue，保證單 UART 單寫入者。
- `sw_core/session_manager.py`：session 狀態機、裝置 hotplug、binding/alias 持久化、console attach、interactive lease、recover、background capture 全都在這裡。
- `sw_core/uart_io.py`：serial port 與 PTY bridge、RX fan-out、human line buffering、本地回顯與 backspace 編輯。
- `sw_core/auth.py`：per-session 帳密解析。`SessionAuth` frozen dataclass 持有已解析的帳密；`resolve_session_auth()` 從 `env_file` → `os.environ` 解析。
- `sw_core/login_fsm.py`：prompt probe、登入流程與 `ready_probe` nonce 驗證。接受 `SessionAuth` 參數，不直接碰 `os.environ`。
- `sw_core/wal.py`：`raw.wal.ndjson` 與 `raw.mirror.log` 的雙軌 append-only 記錄。

### Session 狀態機

實際狀態不是只有 `DETACHED -> ATTACHING -> READY`，而是：

`DETACHED -> ATTACHING -> ATTACHED -> READY`

另外還會出現 `RECOVERING`。

- `ATTACHED`：bridge 已經掛上，但 target 還沒確認進入可執行 prompt；這時候 **human console 仍可 attach 進去做手動登入或觀察 boot/log**。
- `READY`：agent 命令可進入 arbiter。
- `platform=passthrough` 的 session 會停在 `ATTACHED`，因為它不做 prompt/login/ready gating。

### WAL 與結果擷取

- 預設權威記錄是 `/tmp/serialwrap/wal/raw.wal.ndjson`。
- 預設人類可讀鏡像是 `/tmp/serialwrap/wal/raw.mirror.log`。
- 每筆 WAL 都有 `seq`、`mono_ts_ns`、`wall_ts`、`source`、`cmd_id`、`crc32`、`payload_b64`。
- `background` 命令不是直接把所有輸出塞回 `command.get`；需要透過 `command.result_tail` 逐段讀取 capture。
- 若只要改 log 位置、不想搬動 socket / state，可在 shell 環境設 `SERIALWRAP_WAL_DIR="$HOME/b-log"` 或透過 `SERIALWRAP_DAEMON_ENV_FILE` 指向 runtime env 檔。

### MCP 與 RPC 的關係

MCP 只是 RPC 的薄轉接層。新增或改名工具時，要把 `sw_mcp/server.py` 的 `_TOOL_MAP` 跟 `sw_core/service.py` 的 RPC 方法一起看，不然 CLI / MCP 很容易不同步。

## 關鍵慣例

### 設定物件 immutable，執行期狀態 mutable

- `sw_core/config.py` 的 `UartProfile`、`ProfileTemplate`、`SessionProfile` 都是 `@dataclass(frozen=True)`。
- `sw_core/session_manager.py` 的 `SessionRuntime`、`BackgroundCapture`、`InteractiveLease` 則是可變 dataclass。
- 需要更新 session profile（例如 alias、device_by_id）時，慣例是用 `dataclasses.replace(...)` 產生新物件，而不是原地改 frozen config。

### RPC 路由是平面 if/elif，不做動態註冊

- `SerialwrapService.rpc()` 是單一平面分派器。
- 新增 RPC 方法時，直接在 `sw_core/service.py` 加分支，不要額外引入 decorator registry 或 metaprogramming。
- 所有 RPC 回應都維持 `dict[str, Any]` + `ok: bool`；失敗時附 `error_code`，例外不要穿越 RPC 邊界。

### JSON 輸出必須維持緊湊且穩定

- CLI 與 MCP 一律用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`。
- `state.json` 與 WAL 相關輸出會加上 `sort_keys=True`，避免不必要的 diff 與測試波動。

### human console 不會直接把每個按鍵原樣寫進 UART

human console 走的是 brokered console 模型：

- `UARTBridge` 先做本地回顯與 backspace 編輯。
- 只有湊成一整行之後，才由 `SerialwrapService._on_console_line()` 送進 arbiter。
- 這是為了讓多 console / 多 agent 共用 UART 時，target 不會看到半截輸入。

### 常見 human 互動式命令會自動升級成 interactive 模式

`sw_core/service.py` 的 `_human_console_mode()` 會辨識 `vi`、`vim`、`top`、`less`、`menuconfig` 等命令；若 human console 打的是這類命令，broker 會自動走 interactive ownership，而不是當成普通 line command。

這個機制會影響：

- agent 命令是否需要等待 human interactive lease 釋放
- `SESSION_INTERACTIVE_BUSY` 何時出現
- recover 流程是否應該介入

### Alias / binding 是持久化狀態，不只存在記憶體

- `SessionManager` 會把 alias 與 binding override 存到 `state.json`。
- `profiles/*.yaml` 是預設來源，但執行期 `session.bind` / `alias.*` 的結果會覆寫到持久化狀態。
- 裝置綁定慣例是使用 `/dev/serial/by-id/`，不要改回不穩定的 `/dev/ttyUSB*`。

### 新增能力通常要同步改多個面

如果你新增一個命令、RPC 方法或工具，通常至少要一起檢查：

- `sw_core/service.py`：RPC 分派
- `sw_core/cli.py`：CLI subparser 與參數轉換
- `sw_mcp/server.py`：MCP `_TOOL_MAP`
- `README.md` / `docs/serialwrap-spec.md`：對外契約與使用方式
- `tests/` 下的代表性測試：至少補觸及該流程的 unit 或 E2E

這個 repo 的設計是**顯式同步多個表面**，而不是靠自動產生。

### legacy alias 仍存在，但新介面優先

- CLI 的 `stream tail`
- MCP 的 `serialwrap_tail_results`

這些仍可用，但目前新設計優先使用：

- `serialwrap cmd result-tail`
- `serialwrap_tail_command_result`

### Python 風格慣例

- Python 3.10+
- 幾乎所有模組都以 `from __future__ import annotations` 開頭
- 函式簽章普遍有完整型別標註

## 測試與除錯重點

- `tests/test_multiagent_e2e.py` 會啟動真實 daemon，再用 PTY 假 target 驗證 `READY` 流程與多 agent 序列化，任何跨 `service / arbiter / session_manager / uart_io` 的改動都很適合先看這個測試。
- `tests/test_wal.py`、`tests/test_login_fsm.py`、`tests/test_session_bind.py` 分別對應 WAL、登入狀態機與綁定/持久化行為。
- `install.sh` 不是單純複製檔案：它還會移除 legacy `serialwrap_lib.py`，並在偵測到唯一一個 `/dev/serial/by-id/*` 且 `profiles/default.yaml` 仍是 placeholder 時，自動改寫預設 target。
