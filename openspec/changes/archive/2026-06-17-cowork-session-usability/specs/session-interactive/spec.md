## ADDED Requirements

### Requirement: UARTBridge SHALL track last real human input time

`UARTBridge` SHALL 記錄當前 human interactive owner 的「最後一次真實鍵入時間」`last_human_input_at`。此時間 SHALL 僅在 human owner 真實鍵入（`_handle_console_rx` 的 human-owner 直送路徑）時更新，SHALL NOT 因 broker 週期 probe 的 RX 或 agent 命令注入而更新。`UARTBridge.snapshot()` SHALL 對外暴露此值。

#### Scenario: human keystroke updates last_human_input_at

- **WHEN** human owner 經其 console 送出真實位元組
- **THEN** bridge 的 `last_human_input_at` 更新為當前時間

#### Scenario: broker probe does not update last_human_input_at

- **WHEN** broker 週期 probe 在 UART 上產生 RX，但無 human owner 鍵入
- **THEN** `last_human_input_at` SHALL NOT 因此更新

### Requirement: human lease SHALL be soft-preemptible when idle

當 agent 要取得 interactive 控制權、而 session 存在 human lease 但該 human **非 active**（`now - last_human_input_at > HUMAN_ACTIVE_WINDOW_S`，`HUMAN_ACTIVE_WINDOW_S = 60s`）時，`SessionManager` SHALL 允許 agent 以 **soft preempt** 取得 owner：沿用既有 `suspend_interactive()` / `_deferred_buffers` 將 human 降級（human 鍵入進 deferral 暫存），agent 結束後 SHALL `resume_interactive()` 還原並回放，human console SHALL NOT 被中斷。

當 human **為 active**（視窗內有真實鍵入）時，SHALL 維持既有行為，agent SHALL NOT 奪取互動 owner（命令仍走 deferral 共存路徑）。

#### Scenario: agent preempts idle human lease

- **WHEN** session 有 human lease，且 `now - last_human_input_at > 60s`
- **AND** agent 請求取得 interactive 控制權
- **THEN** agent 取得 owner、human 被降級為 suspended（console 不中斷）、human 後續鍵入進 deferral
- **AND** agent 結束後 human owner 還原、deferral 內容回放

#### Scenario: active human lease is not preempted

- **WHEN** session 有 human lease，且 `now - last_human_input_at <= 60s`
- **AND** agent 請求取得 interactive 控制權
- **THEN** agent SHALL NOT 奪取互動 owner（維持既有共存行為）

### Requirement: dead orphan console SHALL be detached via liveness

`SessionManager` SHALL 在 self-test 與 agent 取得控制權之前，沿用既有 `console_has_external_peer` liveness 檢查：當 human lease 的 console peer 已關閉（process 已結束）時 SHALL detach 該 console 並關閉 lease。系統 SHALL NOT 對 alive-but-idle 的 console 做自動 detach（該情形以 soft preempt 處理；真正清理交由 agent 主動 `recover`/`console-detach`）。

#### Scenario: dead console peer is detached

- **WHEN** human lease 存在但其 console peer 已關閉（`console_has_external_peer` 為 False）
- **THEN** 該 console 被 detach、lease 關閉、`interactive_owner` 清為 null

#### Scenario: alive idle console is not auto-detached

- **WHEN** human lease 存在、peer 仍開著、但已閒置超過視窗
- **THEN** 該 console SHALL NOT 被自動 detach（僅可被 soft preempt 降級）
