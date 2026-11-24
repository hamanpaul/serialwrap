# issue 275：fix(console): Windows TCP console（Telnet）連入未建立 session lease，human_attached／human_active 恆為 false

## 現象

Windows（0.4.1 bundle）上，以 TeraTerm Telnet 連入 session 的 `console_endpoint`（#84 PORT-2／#131）並打字。WAL 可見逐字 TX，source 為 `human:<client_id>`，代表 bridge 層已把 raw 擁有權交給這條 console。但 18 秒後跑 `session self-test`：

```
console_count: 1
human_attached: false
human_active: false
interactive_owner: null
```

60 秒時間窗內有真實鍵入，`human_active` 應為 `true`。

## 重現

1. Windows daemon，任一 READY session（本例為 OpenWrt 板、passthrough profile）。
2. TeraTerm：TCP/IP、`127.0.0.1:<console_endpoint port>`、Service = Telnet。
3. 在 TeraTerm 打一行命令並按 Enter。
4. 60 秒內 `serialwrap session self-test --selector <COM>`：`human_attached`／`human_active` 皆為 false。

## 根因（讀碼）

- `sw_core/uart_io.py` `_accept_console_conn`（約 1347–1392 行）：TCP console 連入時，在 **bridge 層**直接設 `_interactive_owner = "human:<cid>"`，沒有經過 `SessionManager` 建立 `InteractiveLease`。
- `sw_core/session_manager.py` `_lease_context`（約 6138 行）與 `_human_active_locked`（約 2141 行）都以 **session 層 lease** 判定 human 是否在場、是否活躍。沒有 lease 時恆為 false。
- lease 自癒（`session_manager.py` 約 2554 行）在 bridge 已有 `interactive_owner` 時直接 `continue`，所以這種「bridge 有 owner、session 沒 lease」的狀態永遠補不上。
- POSIX 的 `console-attach` RPC／`serialwrap-minicom` 路徑會建 lease，不受影響。

## 影響

- agent 送命令前用 `self-test` 的 `human_active` 判斷人類是否正在用，在 Windows 上會恆判為閒置。
- `_prepare_reprobe_locked`（約 2231 行）與 `_reprobe_target_still_valid_locked`（約 2341 行）用 `_human_active_locked` 避免人類打字時注入自動 reprobe。Windows TCP console 下這道保護失效：會 probe 的 profile（shell／prpl／bcm）可能在人類打字途中插入探測命令。此點為讀碼推論，尚未實機重現。
- 與 #264（raw interactive 下 human 未送出的半行與 agent 命令串行）相關。

## 修法方向（待評估）

1. TCP console 取得 raw 擁有權時通知 `SessionManager` 建 lease，讓狀態與 POSIX `console-attach` 一致（最根本，但要處理 accept 執行緒與 `self._lock` 的鎖序）。
2. lease 自癒新增分支：bridge 已有 `human:` owner 但 session 沒有 lease 時補記 lease。
3. 只修回報：`_lease_context`／`_human_active_locked` 在沒有 lease 時退回看 bridge owner 與 `last_human_input_at`（改動最小，但 lease 相關的其他語意仍不一致）。

本項改到 Python 基線（`sw_core/`），PR 需填「Rust 重構跟進」段。


## 留言

