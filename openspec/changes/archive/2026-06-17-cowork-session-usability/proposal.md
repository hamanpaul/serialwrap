## Why

Co-work 下 session 常「看起來 attached、實際不能用」：#53 的 human interactive lease 因孤兒 minicom 而退化成無限期硬鎖（無 idle/staleness 判定）；#51 的 passthrough/`others-template` 永遠停在 `ATTACHED`，`cmd submit` 回語意不清的 `SESSION_NOT_READY`。兩者本質相同，合併處理。設計見 `docs/superpowers/specs/2026-06-17-cowork-session-usability-design.md`。

## What Changes

- **#53 真實鍵入時間追蹤**：`UARTBridge` 記錄 human owner 的 `last_human_input_at`（只算真人鍵入，排除 broker probe / agent 注入），解決 `idle_for_ms` 被 probe 洗小的假訊號。
- **#53 active 判準 + 語意拆分（additive）**：新增 `human_active`（最後真人鍵入 ≤ `HUMAN_ACTIVE_WINDOW_S = 60s`）；`human_attached` 維持原語意（有 human lease）。擋人/搶佔與 `self-test recommended_action` 改看 `human_active`。
- **#53 soft preempt**：agent 取得 interactive 控制權時，若 human lease 存在但 `human_active=False`，以既有 `suspend_interactive()`/`_deferred_buffers` 將 human **降級**（畫面與輸入不丟），結束後 resume/回放。
- **#53 liveness**：沿用 `console_has_external_peer` 路徑，console peer 已關（死孤兒）→ detach + 關 lease。不做 long-idle 自動清理。
- **#51 command_capable 判準**：`command_capable = bool(profile.ready_probe.strip())`，取代 `platform == "passthrough"` 寫死 `ok=False`。有 `ready_probe` 的 target（含 passthrough）可走 `probe_ready` 進 READY。
- **#51 error code（BREAKING-ish）**：非 command-capable session 的 `cmd submit` 改回 **`PROFILE_NOT_COMMAND_CAPABLE`**（取代 `SESSION_NOT_READY`）；保留 `SESSION_NOT_READY` 給「command-capable 但尚未 READY」。
- **#51 self-test 欄位（additive）**：新增 `command_capable`。
- **#51 READY 泛化 + U-Boot**：READY 定義為「對得上預期 prompt + `ready_probe` round-trip」，與底層 OS/bootloader 無關；新增 `uboot-template` profile，並在真機 COM1 驗證可下 line 命令。

## Capabilities

### New Capabilities
- `session-command-readiness`: session 的「可下命令」契約——`command_capable` 判準（`ready_probe` 非空）、`PROFILE_NOT_COMMAND_CAPABLE` 錯誤語意、READY 泛化（OS/bootloader 一致、含 U-Boot）、profile schema 對 command-capable 的影響。

### Modified Capabilities
- `session-interactive`: 新增閒置/孤兒 lease 的釋放路徑——`human_active` active 視窗、idle 時 agent 可 soft preempt（降級而非硬鎖）、死孤兒 console liveness detach。
- `session-selftest`: self_test 結果新增 `human_active` 與 `command_capable` 欄位（additive），並以 `human_active`（而非 `human_attached`）驅動 idle 相關 `recommended_action`。

## Impact

- 程式碼：`sw_core/uart_io.py`（last_human_input_at、snapshot）、`sw_core/session_manager.py`（`_lease_context`、`_refresh_interactive_locked`、`_attach_by_id`/`_probe_existing_bridge`、cmd submit gate、self_test）、`sw_core/login_fsm.py`（readiness 泛化確認）、`sw_core/config.py` 與 `profiles/default.yaml`（`uboot-template`）。
- 對外行為：`cmd submit` 對 passthrough 改回 `PROFILE_NOT_COMMAND_CAPABLE`；self-test 新增 `human_active`/`command_capable`；human lease 閒置可被 agent soft preempt。下游 agent/MCP/skill 若依賴 `SESSION_NOT_READY` 或 `human_attached` 需檢視。
- 測試：fake PTY 單元/整合（入 CI）+ COM1 U-Boot 真機驗證（不入 CI）。注意兩支 pre-existing flaky。
