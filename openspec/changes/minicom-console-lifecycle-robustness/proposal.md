## Why

兩個同源回歸的真正修正：

- **症狀1（minicom 無法 Tab 補完、方向鍵變 `[A`）**：human console 掉回 line-buffer＝連入的 client 未持有 `_interactive_owner == human:<id>`。活機坐實（2026-06-29，COM0/COM1）：兩 session 皆 `state:READY` 但 `interactive_session_id:null`、console-list 兩 console `interactive_owner:false`。
- **#76（minicom 卡頓/掉字）**：serialwrap 端 **stale/orphan console 累積**——RX fan-out 對每個 console master 寫入，孤兒愈多單 reader 愈慢，形成「byte 不流動」stall。活機坐實：每條 COM `console_count:2`＝1 真 minicom + 1 個 label=`primary` 的死 console（pts 僅 daemon slave 持有、無外部 reader），且因 **primary 永不被 prune**（`uart_io.py:446`）而卡死。

兩者**同源**：舊 minicom 不乾淨關閉（SIGKILL/crash、跑不到 wrapper cleanup）留下死 primary／孤兒 → `_refresh_interactive_locked` 拆掉 human lease 成 `null` → 新 minicom 變第二 console，又無 lease-backed 自癒去重授 → 卡 line-buffer。現況回收只在 `attach_console`/`list_consoles` 兩個 RPC 順手做、**無背景週期**。`#84` 已查證為**無罪**（POSIX raw 路徑 byte-identical）。

> 註：原以為「關終端 SIGHUP 跳過 console-detach」亦為孤兒來源，經真機驗證（真 minicom + 真 tmux pane kill＝process-group SIGHUP）**推翻**——前景 minicom 被 HUP 殺死後 wrapper 仍跑到顯式 cleanup→detach，不留孤兒；故**不改** `minicom_router.sh` 的 trap。真正來源為 SIGKILL/crash，由 broker 週期回收涵蓋。

## What Changes

- **孤兒回收（Fix2）**：
  - 新增 broker 週期回收：`UARTBridge.reap_stale_consoles()`（lock-split：鎖內快照 → 鎖外掃一次 `/proc` → 回鎖 pop → 鎖外 close fd），由 `reconcile_readiness` tick 週期呼叫，不再僅依賴 RPC。
  - 死掉的 primary（無外部 reader）SHALL 可被回收並重指；start() 建的哨兵 primary 以 `internal` 旗標標記、永不回收。
  - **安全鐵則**：reaper SHALL NOT 觸碰當前 `_interactive_owner` 與 `_suspended_owner` 的 console（其生命週期歸 lease 層），避免破壞 #78 suspend 簿記與與 grace 互踩。
- **raw ownership robust（Fix3）**：
  - `_refresh_interactive_locked` 對 human lease 的被動拆除加 **peer-loss grace**（瞬時 flap 不立即拆）。
  - 新增 **lease-backed 週期自癒**：`reconcile_readiness` tick 中，若 bridge owner 為 None 且 READY/ATTACHED、非 agent-active/flash、有活 primary console、且無既有 lease → 開 `human:<primary_cid>` lease（**同時**設 lease + bridge owner，永不脫鉤）。
  - **BREAKING（內部設計約束，非外部 API）**：raw ownership SHALL 永遠由 session-layer lease 背書；**不得**在 bridge 層做 lease-less 自癒（會與 lease 脫鉤、使 agent 命令漏 suspend → 重引入 #78/#81 deferred-input 污染）。

## Capabilities

### New Capabilities
（無；本 change 全部為既有 capability 的 requirement 變更。）

### Modified Capabilities
- `session-interactive`: (a) 「dead orphan console 經 liveness detach」擴充為**週期主動回收 + primary 可回收 + reaper 不碰 owner/suspended owner**；(b) 新增 human lease 被動拆除的 **peer-loss grace**；(c) 新增 **lease-backed 週期自癒重授 raw ownership**；(d) console-attach 後連入 client 取得 raw ownership 的不變式。

## Impact

- `sw_core/uart_io.py`：`ConsoleClient.internal` 欄位、`reap_stale_consoles()`、primary-reap、reaper 完整清理（含 suspended/deferred）；**原子鎖內快照擴充**（`snapshot()` 補 `agent_active`/`suspended_owner`/`flash_mode`/`primary_client_id`）與**原子條件式 grant** primitive（`try_grant_interactive_if_idle`，Codex finding-2）。
- `sw_core/session_manager.py`：`_refresh_interactive_locked` grace（`InteractiveLease.peer_lost_at`）、`reconcile_readiness` tick 接 reaper + lease-backed self-heal。
- `sw_core/constants.py`：`_HUMAN_PEER_GRACE_S`。
- 測試：`tests/test_uart_io.py`、`tests/test_interactive_raw.py`、`tests/test_suspend_resume_reentrant.py`（#78 不回歸）。
- 硬相依：症狀1 主因（觸發A：舊孤兒讓新 minicom 跳過授予）由本 change 的 **broker 週期回收 + lease-backed 自癒**修；Fix3 的 grace（觸發B）單獨不足以宣稱修好症狀1。
- 不在範圍：`minicom_router.sh` SIGHUP trap（原 Task 1，真機驗證為非必要，已 drop）。
