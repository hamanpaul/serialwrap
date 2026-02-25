# serialwrap

`serialwrap` 是面向單一 UART/多 Agent 協作的 broker 架構，核心是 `serialwrapd`（常駐仲裁）+ `serialwrap`（CLI client）+ `serialwrap-mcp`（MCP adapter）。

## 動機

- 解決多 Agent/人類同時操作 UART 時的競爭寫入、回應混線、資料遺失與不可追溯。
- 提供可機器處理與可人工閱讀的雙軌原始紀錄，滿足除錯、稽核與回放需求。
- 將 profile 與實體 UART 連接點解耦，讓同一組 login/UART 參數可套用到多個 target。

## 依賴

- Python 3.10+
- `pyyaml` — profile YAML 解析
- `pyserial`（間接需要 `termios`）— UART 控制

## 架構

```text
Agent/CLI ----\
Agent/MCP -----+--> serialwrapd (RPC + Arbiter) --> UART IO --> Target Shell
Human/minicom -/              |
                              +--> WAL (raw.wal.ndjson)
                              +--> Mirror (raw.mirror.log)
```

| 元件 | 入口 | 說明 |
|------|------|------|
| **daemon** | `serialwrapd.py` | singleton 常駐，獨占實體 UART，提供 JSON-RPC Unix socket |
| **CLI client** | `serialwrap` → `sw_core/cli.py` | 子命令式 CLI，透過 socket 發送 RPC |
| **MCP adapter** | `serialwrap-mcp` → `sw_mcp/server.py` | 將 MCP 工具名對應到內部 RPC 方法（`_TOOL_MAP`） |

### 內部模組

| 模組 | 職責 |
|------|------|
| `sw_core/service.py` | 頂層調度器（`SerialwrapService`），路由所有 RPC 方法 |
| `sw_core/arbiter.py` | 每 session 優先佇列，單寫入者保證 |
| `sw_core/session_manager.py` | session 生命週期管理、裝置綁定、alias 解析 |
| `sw_core/uart_io.py` | serial port + PTY bridge，TX/RX 搭配 WAL 記錄 |
| `sw_core/login_fsm.py` | 平台相依 login FSM（`bcm` / `prpl` 路徑） |
| `sw_core/wal.py` | 雙軌 append-only log |
| `sw_core/device_watcher.py` | 輪詢 `/dev/serial/by-id/` 偵測 hotplug |
| `sw_core/config.py` | profile YAML 載入：`ProfileTemplate` → `SessionProfile` 合併 |
| `sw_core/alias_registry.py` | alias → session_id / by-id 映射 |

### Session 狀態機

```text
DETACHED ──(裝置出現 + attach)──► ATTACHING ──(login FSM 成功)──► READY
   ▲                                  │                            │
   │                                  │ (FSM 失敗/例外)            │ (裝置拔除)
   └──────────────────────────────────┘                            │
   └───────────────────────────────────────────────────────────────┘
```

- `DETACHED`：未連接或裝置已移除。
- `ATTACHING`：bridge 已開啟，login FSM 進行中。
- `READY`：可接受命令。arbiter worker thread 已註冊。

### 啟動流程

```text
serialwrap daemon start
  └─ Popen(serialwrapd.py, background)
       └─ ensure_runtime_dirs()        建立 /tmp/serialwrap/*
       └─ load_profiles(profile_dir)   載入 profiles/*.yaml
       └─ SingletonLock.acquire()      flock + socket 排他
       └─ SerialwrapService(profiles)
            └─ SessionManager.__init__  讀取 state.json（alias/binding 持久化）
       └─ service.start()
            └─ DeviceWatcher.start()   啟動輪詢線程
            └─ poll_once()             首次掃描 → update_devices → _spawn_attach
            └─ bootstrap_attach()      確保所有已知裝置嘗試 attach
       └─ server.start()              建立 Unix socket，開始接受連線
       └─ stop_event.wait()           阻塞直到 SIGTERM/daemon.stop
  └─ CLI 等待就緒（health.ping，最多 3 秒）
       └─ 成功 → 回報 pid + socket（附帶 warnings 如有）
       └─ daemon 提前退出 → DAEMON_EXITED
       └─ 超時 → DAEMON_NOT_READY
```

### WAL 雙軌記錄

| 檔案 | 格式 | 用途 |
|------|------|------|
| `/tmp/serialwrap/wal/raw.wal.ndjson` | NDJSON（base64 payload） | 機器可讀權威記錄 |
| `/tmp/serialwrap/wal/raw.mirror.log` | 純文字 | 人類可讀鏡像 |

每筆記錄包含：`seq`、`mono_ts_ns`、`wall_ts`、`com`、`dir`、`source`、`cmd_id`、`len`、`crc32`、`payload_b64`。
WAL 檔案達 64 MiB 自動 rotate。

### 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `SERIALWRAP_STATE_DIR` | `/tmp/serialwrap` | 狀態目錄（WAL、state.json） |
| `SERIALWRAP_RUN_DIR` | 同 `STATE_DIR` | lock/socket 目錄 |
| `SERIALWRAP_PROFILE_DIR` | `<安裝目錄>/profiles` | profile YAML 目錄 |
| `SERIALWRAP_BY_ID_DIR` | `/dev/serial/by-id` | 裝置搜尋目錄 |

### Profile 系統

`profiles/*.yaml` 定義 `ProfileTemplate`（login/prompt/UART 參數模板）與 `targets`（將 template 綁定到 COM slot + 裝置）。target 欄位覆蓋 template 預設值。

```yaml
profiles:
  prpl-template:
    platform: prpl
    prompt_regex: ".*# $"
    uart:
      baud: 115200

targets:
  - act_no: 1
    com: COM0
    alias: default+1
    profile: prpl-template
    device_by_id: /dev/serial/by-id/target0
```

## 安裝

```bash
# 預設安裝至 ~/.paul_tools/
./install.sh

# 指定安裝目錄
./install.sh /path/to/install
```

安裝後建議設定 shell：

```bash
export INSTALL_DIR="$HOME/.paul_tools"   # 或你指定的路徑
export PATH="$INSTALL_DIR:$PATH"
alias minicom="$INSTALL_DIR/minicom_router.sh"
```

## 啟動 daemon

```bash
serialwrap daemon start --profile-dir "$INSTALL_DIR/profiles"
serialwrap daemon status
```

## 使用說明

### CLI help

```bash
serialwrap --help
serialwrap daemon --help
serialwrap session --help
serialwrap cmd --help
serialwrap log --help
```

### 常用操作流程

```bash
# 1) 查看裝置與 Session
serialwrap device list
serialwrap session list

# 2) 綁定目標並 attach（首次或換線時）
serialwrap session bind --selector COM0 --device-by-id /dev/serial/by-id/<target-by-id>
serialwrap session attach --selector COM0

# 3) 下命令與查結果
serialwrap cmd submit --selector COM0 --source agent:test --cmd "ifconfig"
serialwrap cmd status --cmd-id <cmd_id>

# 4) 追蹤輸出與原始資料
serialwrap log tail-text --selector COM0 --from-seq 0 --limit 200
serialwrap log tail-raw  --selector COM0 --from-seq 0 --limit 200
serialwrap wal export --from-seq 0 --limit 500
```

### MCP 呼叫

```bash
serialwrap-mcp --help
serialwrap-mcp --tool serialwrap_get_health --params "{}"
serialwrap-mcp --tool serialwrap_list_sessions --params "{}"
serialwrap-mcp --tool serialwrap_submit_command \
  --params '{"selector":"COM0","cmd":"echo hello","source":"agent:mcp"}'
```

### minicom 互動

```bash
# 自動選 READY session 的 broker PTY
minicom

# 指定 session/com/alias
minicom_router.sh COM0
```

## 測試

```bash
# 全部測試
python3 -m unittest discover -s tests -v

# 單一測試檔
python3 -m unittest tests.test_wal -v

# 單一測試方法
python3 -m unittest tests.test_wal.TestWal.test_append_and_tail -v
```

## 規格書

詳細設計規格見 `docs/serialwrap-spec.md`。
