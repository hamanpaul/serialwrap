# serialwrap-mcp Agent Skill

> ⚠️ **本檔狀態**：與 `hamanpaul/custom-skills` 的同名 skill 為兩份平行 agent 指南，且**只更新到 event 時代**——尚缺 device release/attach（#54）、command_capable + `PROFILE_NOT_COMMAND_CAPABLE`（#51）、`/dev/ttyMCU` + `mcu patterns/status`（#55）、human_active/soft-preempt（#53）。source-of-truth 收斂、改名（`serialwrap-mcp` → `serialwrap`）與 plugin 打包追蹤於 **#59**；在 #59 完成前，agent 操作以 README + `openspec/specs/*` 為準。

## 目的
定義 Agent 在 UART 任務中使用 `serialwrap`/`serialwrap-mcp` 的觸發條件、操作順序與安全邊界，避免直接碰觸實體 UART 造成資料失真或衝突。

## 何時該使用
- 需要多 Agent 共用同一 UART 並保證單寫入仲裁。
- 需要完整 RAW log（含 timestamp/source/cmd_id/seq/crc）做回放或稽核。
- 需要在人類 `minicom` 與 Agent 任務同時運作下保持一致視圖。
- 需要以 MCP 工具模式把 UART 操作整合到 Agent workflow。

## 何時不要使用
- 單次、一次性、無需追溯的本機 serial 測試。
- target 不經 UART 而經 SSH/ADB 等其他通道，且不需 UART 證據鏈。

## 前置條件
- `serialwrapd` 必須啟動。
- 目標 session 必須是 `READY`。
- profile 與 target 已綁定（`session bind` + `session attach` 至少完成一次）。

## Remote Support 用法

當 Agent 不在 FAE 現場機器上，而是要從遠端透過 network debug UART target 時，應改走 **remote endpoint** 模式。

### 基本原則

- daemon 仍跑在 **FAE / target 所在主機**
- Agent 端只透過 `--endpoint tcp://host:port` 連到遠端 daemon
- 正式環境優先使用 **ssh-tunnel**
- Docker 雙 container smoke 只用於隔離測試，不代表 production 暴露方式

### 正式環境（ssh-tunnel）

FAE 端：

```bash
socat TCP-LISTEN:7777,bind=127.0.0.1,reuseaddr,fork \
      UNIX-CONNECT:/tmp/serialwrap/serialwrapd.sock &
```

RD / Agent 端：

```bash
ssh -N -L 127.0.0.1:7777:127.0.0.1:7777 fae_user@fae_host
serialwrap --endpoint tcp://127.0.0.1:7777 session list
serialwrap-mcp --endpoint tcp://127.0.0.1:7777 --tool serialwrap_list_sessions
```

### Docker smoke

若只是要驗證 remote-support 在隔離環境內可用，可直接跑：

```bash
./tools/docker/remote_smoke.sh
```

### Remote 模式注意事項

- `daemon start` 不支援 `--endpoint`
- `file.push` / `file.pull` 的 `local_path` 是 **daemon 所在 host/container** 的路徑，不是 Agent 本機路徑
- 正式環境 `socat` 必須 `bind=127.0.0.1`，不可直接暴露到外網

## 標準執行順序（Agent 必須遵守）
1. 健康檢查：`serialwrap_get_health`。
2. 探測資源：`serialwrap_list_sessions`、`serialwrap_list_devices`。
3. 鎖定目標：`serialwrap_get_session_state(selector)`，必要時先做 `serialwrap_self_test`。
4. 若 session 未 READY 或發現裝置換 tty，可用 `serialwrap_bind_session` / `serialwrap_attach_session` / `serialwrap_recover_session`。
5. 提交命令：`serialwrap_submit_command`，必填 `source` 與 `selector`。
6. 前景命令：`serialwrap_get_command` 直接取 `stdout`。
7. 背景命令：`serialwrap_tail_command_result` 增量取回後續內容。
8. 若需要 focused 純文字 RX capture，可用 `serialwrap_log_start` / `serialwrap_log_status` / `serialwrap_log_stop`。
9. 需要完整證據時，改拉 CLI `log tail-raw` / `wal export`；若只要查目前 WAL seq，可用 `serialwrap_wal_current_seq`。

## MCP Tool 對應
- `serialwrap_get_health` -> `health.status`
- `serialwrap_list_devices` -> `device.list`
- `serialwrap_list_sessions` -> `session.list`
- `serialwrap_get_session_state` -> `session.get_state`
- `serialwrap_bind_session` -> `session.bind`
- `serialwrap_attach_session` -> `session.attach`
- `serialwrap_self_test` -> `session.self_test`
- `serialwrap_recover_session` -> `session.recover`
- `serialwrap_submit_command` -> `command.submit`
- `serialwrap_get_command` -> `command.get`
- `serialwrap_tail_command_result` -> `command.result_tail`
- `serialwrap_clear_session` -> `session.clear`
- `serialwrap_attach_console` -> `session.console_attach`
- `serialwrap_detach_console` -> `session.console_detach`
- `serialwrap_list_consoles` -> `session.console_list`
- `serialwrap_open_interactive` -> `session.interactive_open`
- `serialwrap_send_interactive_keys` -> `session.interactive_send`
- `serialwrap_get_interactive_status` -> `session.interactive_status`
- `serialwrap_close_interactive` -> `session.interactive_close`
- `serialwrap_log_start` -> `session.log_start`
- `serialwrap_log_stop` -> `session.log_stop`
- `serialwrap_log_status` -> `session.log_status`
- `serialwrap_wal_reset` -> `wal.reset`
- `serialwrap_wal_current_seq` -> `wal.current_seq`
- `serialwrap_tail_results` -> `result.tail`（deprecated alias）
- `serialwrap_file_push` -> `file.push`
- `serialwrap_file_pull` -> `file.pull`

## MCP 參數規範
- `serialwrap_submit_command`
  - 必填：`selector`, `cmd`
  - 建議：`source="agent:<name>"`, `mode="line|background|interactive"`, `timeout_s`, `priority`
  - 選填：`expected_duration_s`（長命令 keepalive hint，broker 在此期間暫停 prompt timeout 並監控 RX 活動延長等待）
  - 命令不得含 `\n` 換行字元，否則回 `CMD_CONTAINS_NEWLINE`
  - 命令長度限制：> 4 KB 會回 warning，> 16 KB 會被 reject (`CMD_TOO_LONG`)
  - 長命令建議改用 `serialwrap_file_push` 傳輸 script 後在 target 執行
- `serialwrap_recover_session`
  - 必填：`selector`
  - 選填：`timeout_s`, `force`（布林，force=true 時自動 clear+reattach+wait-ready）
- `serialwrap_get_command`
  - 必填：`cmd_id`
- `serialwrap_tail_command_result`
  - 必填：`cmd_id`
  - 建議：`from_chunk`, `limit`
- `serialwrap_get_session_state`
  - 必填：`selector`（`session_id | COMx | alias`）
- `serialwrap_log_start`
  - 必填：`selector`
  - 選填：`log_dir`
- `serialwrap_log_status`
  - 必填：`selector`
- `serialwrap_wal_current_seq`
  - 不需參數
- `serialwrap_file_push`
  - 必填：`selector`, `local_path`, `remote_path`
  - 選填：`chunk_size`（預設 2KB）, `checksum`（布林，預設 true）
- `serialwrap_file_pull`
  - 必填：`selector`, `remote_path`
  - 選填：`local_path`（省略時回傳檔案內容）, `chunk_size`

## 安全規則
- 禁止 Agent 直接寫 `/dev/ttyUSB*` 或 `/dev/ttyACM*`。
- 禁止繞過 broker 自行開多個 serial writer。
- 長流命令（`logread -f`, `tcpdump`, kernel debug）一律使用 `mode=background` 或限制 timeout，避免阻塞共享通道。
- 每筆自動化命令必填 `source`，不可省略，確保追蹤性。
- 卡住時先 `serialwrap_self_test`，再決定是否 `serialwrap_recover_session`。
- `serialwrap_recover_session` 成功恢復 prompt 時回傳 `ok: true`（附 `error_code: PROMPT_TIMEOUT_RECOVERED`, `partial: true`），表示 session 可繼續使用。

## 短命令原則（Best Practice）
- **避免 heredoc**：heredoc 經 UART 傳輸時容易遺失字元或打亂 prompt，改用 `echo ... > file` 分步寫入。
- **單行 < 2 KB**：每條命令盡量控制在 2 KB 以內，超過 4 KB 會收到 warning。
- **避免 base64 inline**：不要將整個檔案 base64 編碼塞進 `cmd submit`，改用 `serialwrap_file_push` 傳輸。
- **長命令拆分**：管線命令過長時，先寫成 script 檔再 `source` 或 `sh /tmp/script.sh`。
- **長命令 keepalive**：長時間命令加 `expected_duration_s` 避免誤判 prompt timeout。
- **回復策略**：若命令導致 PROMPT_TIMEOUT，使用 `serialwrap_recover_session` (可加 `force=true`)。recover 成功後 `ok: true` 表示 session 已恢復。

## 最小可用 MCP 範例
```bash
~/.paul_tools/serialwrap-mcp --tool serialwrap_get_health --params "{}"
~/.paul_tools/serialwrap-mcp --endpoint tcp://127.0.0.1:7777 --tool serialwrap_get_health --params "{}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_get_session_state --params "{\"selector\":\"COM0\"}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_self_test --params "{\"selector\":\"COM0\"}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_submit_command --params "{\"selector\":\"COM0\",\"cmd\":\"ifconfig\",\"source\":\"agent:diag\",\"mode\":\"line\"}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_get_command --params "{\"cmd_id\":\"<cmd_id>\"}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_tail_command_result --params "{\"cmd_id\":\"<cmd_id>\",\"from_chunk\":0,\"limit\":120}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_log_status --params "{\"selector\":\"COM0\"}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_wal_current_seq --params "{}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_file_push --params "{\"selector\":\"COM0\",\"local_path\":\"./fw.bin\",\"remote_path\":\"/tmp/fw.bin\"}"
~/.paul_tools/serialwrap-mcp --tool serialwrap_file_pull --params "{\"selector\":\"COM0\",\"remote_path\":\"/etc/config/wireless\"}"
```

## Event Trigger Engine

用於 UART RX 觸發自動化 handler。先呼叫 `serialwrap_event_status` 確認狀態。

### 基本使用流程

```bash
# 1. 寫 rule 檔案（JSON）
# 2. 載入
serialwrap event add --file /path/to/rule.json
# 3. 確認狀態
serialwrap event status --selector COM0
# 4. 若未啟用，手動啟用
serialwrap event enable --selector COM0
# 5. 查看 fire 記錄
serialwrap event tail --rule-id owner.name -n 20
```

### MCP 使用範例

```bash
# 載入規則
~/.paul_tools/serialwrap-mcp --tool serialwrap_event_rule_set --params '{
  "rule": {
    "schema_version": 1, "owner": "ops", "name": "panic",
    "kind": "tool", "selectors": ["COM0"],
    "pattern": {"kind": "contains", "value": "Kernel panic"},
    "handler": {"exec": ["/usr/local/bin/alert"]},
    "auto_enable_com_on_load": true
  }
}'

# 查詢狀態（必須在 enable/disable 之前呼叫）
~/.paul_tools/serialwrap-mcp --tool serialwrap_event_status --params '{"selector": "COM0"}'

# 查看最近觸發記錄
~/.paul_tools/serialwrap-mcp --tool serialwrap_event_tail --params '{"rule_id": "ops.panic", "n": 10}'
```

### 安全規則

- **先呼叫 `serialwrap_event_status`** 再 enable/disable，避免假設狀態。
- `auto_enable_com_on_load: true` 的規則在 daemon 重啟後自動啟用。
- Counter 由 disable/delete/reset 清除；daemon 重啟後 counter 保留（tmpfs 除外）。
