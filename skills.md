# serialwrap-mcp Agent Skill

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

## 標準執行順序（Agent 必須遵守）
1. 健康檢查：`serialwrap_get_health`。
2. 探測資源：`serialwrap_list_sessions`、`serialwrap_list_devices`。
3. 鎖定目標：`serialwrap_get_session_state(selector)`，必要時先做 `serialwrap_self_test`。
4. 若 session 未 READY 或發現裝置換 tty，可用 `serialwrap_bind_session` / `serialwrap_attach_session` / `serialwrap_recover_session`。
5. 提交命令：`serialwrap_submit_command`，必填 `source` 與 `selector`。
6. 前景命令：`serialwrap_get_command` 直接取 `stdout`。
7. 背景命令：`serialwrap_tail_command_result` 增量取回後續內容。
8. 需要完整證據時，改拉 CLI `log tail-raw` / `wal export`。

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
- `serialwrap_tail_results` -> `result.tail`（deprecated alias）

## MCP 參數規範
- `serialwrap_submit_command`
  - 必填：`selector`, `cmd`
  - 建議：`source="agent:<name>"`, `mode="line|background|interactive"`, `timeout_s`, `priority`
- `serialwrap_get_command`
  - 必填：`cmd_id`
- `serialwrap_tail_command_result`
  - 必填：`cmd_id`
  - 建議：`from_chunk`, `limit`
- `serialwrap_get_session_state`
  - 必填：`selector`（`session_id | COMx | alias`）

## 安全規則
- 禁止 Agent 直接寫 `/dev/ttyUSB*` 或 `/dev/ttyACM*`。
- 禁止繞過 broker 自行開多個 serial writer。
- 長流命令（`logread -f`, `tcpdump`, kernel debug）一律使用 `mode=background` 或限制 timeout，避免阻塞共享通道。
- 每筆自動化命令必填 `source`，不可省略，確保追蹤性。
- 卡住時先 `serialwrap_self_test`，再決定是否 `serialwrap_recover_session`。

## 最小可用 MCP 範例
```bash
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_get_health --params "{}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_get_session_state --params "{\"selector\":\"COM0\"}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_self_test --params "{\"selector\":\"COM0\"}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_submit_command --params "{\"selector\":\"COM0\",\"cmd\":\"ifconfig\",\"source\":\"agent:diag\",\"mode\":\"line\"}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_get_command --params "{\"cmd_id\":\"<cmd_id>\"}"
/home/paul_chen/.paul_tools/serialwrap-mcp --tool serialwrap_tail_command_result --params "{\"cmd_id\":\"<cmd_id>\",\"from_chunk\":0,\"limit\":120}"
```
