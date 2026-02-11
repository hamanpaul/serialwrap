# serialwrap

`serialwrap` 是面向單一 UART/多 Agent 協作的 broker 架構，核心是 `serialwrapd`（常駐仲裁）+ `serialwrap`（CLI client）+ `serialwrap-mcp`（MCP adapter）。

## 動機
- 解決多 Agent/人類同時操作 UART 時的競爭寫入、回應混線、資料遺失與不可追溯。
- 提供可機器處理與可人工閱讀的雙軌原始紀錄，滿足除錯、稽核與回放需求。
- 將 profile 與實體 UART 連接點解耦，讓同一組 login/UART 參數可套用到多個 target。

## 原機（既有方式的限制）
- 直接多程序開啟 `/dev/ttyUSB*`：無全域仲裁，TX 交錯，RX 難對應。
- `logread -f`、`tcpdump`、kernel debug 長流模式：沒有明確結束點，舊流程容易誤判。
- 以手工 minicom 互動為主：可視化有餘，但對多 Agent 任務協同與可追溯不足。

## 功能
- 單 UART 單寫入者仲裁（Command Arbiter），避免多來源直接衝突。
- RAW 事件雙軌保存：
  - 權威檔：`/tmp/serialwrap/wal/raw.wal.ndjson`
  - 文字鏡像：`/tmp/serialwrap/wal/raw.mirror.log`
- `seq + timestamp + source + cmd_id + crc32` 追蹤鏈路，支持完整回溯。
- Profile/Target 分離：
  - `profiles`：login/prompt/UART 參數模板（不綁 port）
  - target binding：以 `/dev/serial/by-id/*` 綁定實體裝置
- 動態 hotplug：拔除轉 `DETACHED`，同 `by-id` 回插可自動掛回。
- 人類互動相容：`minicom_router.sh` 優先走 broker PTY，必要時才 raw fallback。
- MCP 化：提供 `serialwrap-mcp` 供 Agent 透過 stdio 工具介面呼叫。

## 架構
```text
Agent/CLI ----\
Agent/MCP -----+--> serialwrapd (RPC + Arbiter) --> UART IO --> Target Shell
Human/minicom -/              |
                              +--> WAL (raw.wal.ndjson)
                              +--> Mirror (raw.mirror.log)
```

- `serialwrapd.py`：daemon 入口，維持 singleton 與 RPC socket。
- `sw_core/*`：仲裁、UART、session、login FSM、device watcher、WAL。
- `sw_mcp/server.py`：MCP adapter（stdio in/out），轉發到 `serialwrapd` RPC。
- 詳細規格：`/home/paul_chen/arc_prj/ser-dep/docs/serialwrap-spec.md`

## 安裝說明
### 1) 部署
```bash
/home/paul_chen/arc_prj/ser-dep/install.sh
/home/paul_chen/arc_prj/ser-dep/install.sh /home/paul_chen/.paul_tools
```

### 2) Shell 設定（建議）
```bash
export PATH="/home/paul_chen/.paul_tools:$PATH"
alias minicom="/home/paul_chen/.paul_tools/minicom_router.sh"
```

### 3) 啟動 daemon
```bash
/home/paul_chen/.paul_tools/serialwrap daemon start --profile-dir /home/paul_chen/.paul_tools/profiles
/home/paul_chen/.paul_tools/serialwrap daemon status
```

## 使用說明（Help）
### CLI help
```bash
/home/paul_chen/.paul_tools/serialwrap --help
/home/paul_chen/.paul_tools/serialwrap daemon --help
/home/paul_chen/.paul_tools/serialwrap session --help
/home/paul_chen/.paul_tools/serialwrap cmd --help
/home/paul_chen/.paul_tools/serialwrap log --help
```

### 常用操作流程
```bash
# 1) 看裝置/Session
/home/paul_chen/.paul_tools/serialwrap device list
/home/paul_chen/.paul_tools/serialwrap session list

# 2) 綁定目標並 attach（只需首次或換線）
/home/paul_chen/.paul_tools/serialwrap session bind --selector COM0 --device-by-id /dev/serial/by-id/<target-by-id>
/home/paul_chen/.paul_tools/serialwrap session attach --selector COM0

# 3) 下命令與查結果
/home/paul_chen/.paul_tools/serialwrap cmd submit --selector COM0 --source agent:test --cmd "ifconfig"
/home/paul_chen/.paul_tools/serialwrap cmd status --cmd-id <cmd_id>

# 4) 追蹤輸出與原始資料
/home/paul_chen/.paul_tools/serialwrap log tail-text --selector COM0 --from-seq 0 --limit 200
/home/paul_chen/.paul_tools/serialwrap log tail-raw  --selector COM0 --from-seq 0 --limit 200
/home/paul_chen/.paul_tools/serialwrap wal export --from-seq 0 --limit 500
```

### MCP help 與單次呼叫
```bash
/home/paul_chen/.paul_tools/serialwrap-mcp --help
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_get_health --params "{}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_list_sessions --params "{}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_submit_command --params "{\"selector\":\"COM0\",\"cmd\":\"echo hello\",\"source\":\"agent:mcp\"}"
```

### minicom 互動
```bash
# 自動選 READY session 的 broker PTY
minicom

# 指定 session/com/alias
/home/paul_chen/.paul_tools/minicom_router.sh COM0
```
