# attach 撞開機窗後自動重探回 READY 設計（#69）

- 日期：2026-06-22
- 對應 issue：[#69](https://github.com/hamanpaul/serialwrap/issues/69)（COM 開機期 attach 失敗後卡非 READY、不自動重試；minicom_router 報錯不明確）
- 狀態：設計（brainstorming 產出，已含真機複製 + 根因），待 user 審閱 → openspec-propose → writing-plans。
- 方法論（user 要求）：**真機先複製 → 計劃修復 → 真機驗證**。本文件的真機複製已完成（§2）；真機驗證步驟見 §6，待修復後執行。

## 1. 背景

人類 `minicom_router.sh COM1` 連 DUT 時「連不到」，但板子/線/reader 都正常。根因是 serialwrap 在 DUT 開機窗內 attach、probe 等不到 prompt 而卡在非 READY，且**不會在開機完成後自動重探**，需人工 `recover`/`attach`。

## 2. 真機複製（COM1 / ttyUSB0 / FTDI AC01QZT0，2026-06-22）

throwaway 環境風險高（兩塊板都有 human minicom），故經 user 同意在 prod daemon 的 COM1 上複製、完成後還原：

1. baseline：COM1 `READY`、self_test `OK`。
2. `cmd submit reboot`（03:31:37）→ DUT 重開機。
3. 開機窗內 `session clear` + `session attach` → session 落 **`ATTACHED` + `last_error: PROMPT_UNAVAILABLE`**。
4. **卡住 70s+**：state 持續 `ATTACHED/PROMPT_UNAVAILABLE`，`idle_for_ms` 1.9s→69s（boot log 約 03:32:56 停＝開機完成、prompt 已可用），**未自動回 READY**。
5. self_test：`classification: ATTACHED_NOT_READY`、`recommended_action: console_attach`。
6. **手動 `session attach` 救不回**（仍 PROMPT_UNAVAILABLE）；`session recover --force`（回 TIMEOUT 但實際）→ `ATTACHING` → `READY`，板子還原。

> 與 2026-06-18 報告的差異：當時 resting state 是 `DETACHED/LOGIN_PROMPT_TIMEOUT`，本次是 `ATTACHED/PROMPT_UNAVAILABLE`。#51/#53 後 FSM 行為已變，但**「attach 撞開機窗 → 卡非 READY → 不自動重探」的本質相同**，且兩種 resting state 都要處理。

## 3. 根因（code）

- **probe 只在 attach 觸發當下跑一次**：`sw_core/login_fsm.py` 的 `ensure_ready`(:119) / `probe_ready`(:113) → `_probe_prompt`(:53) 送 `\n` 後 `wait_for_regex(prompt_regex, timeout_s)`；失敗分類：`ensure_ready` 走 `_maybe_login` → `*_PROMPT_TIMEOUT`(:45-50,91)、`probe_ready` 走 `_classify_non_ready_state`(:59-63) → `PROMPT_UNAVAILABLE` / `LOGIN_REQUIRED`。
- 這些只由 attach 路徑呼叫一次：`sw_core/session_manager.py` 的 `_spawn_attach` / `attach_session`(:771) / `_probe_existing_bridge`（ensure_ready/probe_ready @ :969/971/973/1074/1093/1095/1255/1268/1270）。
- **daemon 主 loop 無週期性 readiness re-probe**：`sw_core/uart_io.py:_loop`(:443) 只 pump bytes；沒有任何元件在 prompt 稍後出現時重跑 probe。
- 結果：boot-window attach 失敗後，session 停在 `ATTACHED`（bridge 活、`PROMPT_UNAVAILABLE`）或 `DETACHED`（`*_PROMPT_TIMEOUT`），**永遠**不回 READY，直到人工 `recover`/`attach`。self_test 已能正確分類（`ATTACHED_NOT_READY` @ :2372、`BRIDGE_DOWN` @ :2321/2332），但只是「報告」、不觸發復原。

## 4. 設計（方案 A：daemon 有界重探，RX-idle 觸發）

新增一個 **readiness 對帳（reconcile）週期工作**，由 daemon 週期驅動（與 DeviceWatcher 同層或主 loop 的 periodic tick），對「卡住但可復原」的 session 自動重探回 READY：

- **觸發條件（全部滿足才重探）**：
  - session 為 `ATTACHED` 且 `last_error ∈ {PROMPT_UNAVAILABLE, *_PROMPT_TIMEOUT}`（非 READY、非 RELEASED/FLASHING、非 human-active interactive busy）；或 `DETACHED` 且 `last_error ∈ {*_PROMPT_TIMEOUT, LOGIN_PROMPT_TIMEOUT}` 且 device 仍在位、未被 release。
  - **RX 轉閒**：自上次 RX 起 idle ≥ `REPROBE_RX_IDLE_S`（boot log 停＝prompt 可能已出現）——避免在開機 log 狂噴時瞎探。
  - 未達重探上限。
- **動作**：對該 session 重跑既有 attach/probe（`ATTACHED` 走 `_probe_existing_bridge`/`probe_ready`；`DETACHED` 走 `_spawn_attach`/`ensure_ready`，含 auth/login）。成功 → `READY`；失敗 → 留原狀態、記錄嘗試次數與下次重探時間。
- **有界 + backoff**：指數/線性 backoff（`REPROBE_BACKOFF_S` 起跳、上限 `REPROBE_MAX_INTERVAL_S`），總嘗試上限 `REPROBE_MAX_ATTEMPTS` 或截止 `REPROBE_DEADLINE_S`；達上限後停止自動重探（維持非 READY，附明確 `last_error`，等人工）。
- **狀態表達**：不新增 FSM 狀態（避免向後相容/測試面擴大），以 session 欄位 `reprobe_attempts` / `next_reprobe_at` / `reprobe_exhausted` 表達；`to_public_dict()` / self_test 暴露，供 minicom_router 與 agent 判讀。
- **不干擾**：human-active interactive lease、FLASHING、RELEASED 一律跳過；重探用既有 probe（送 `\n` + ready_probe），與現行 attach 行為一致、不引入新副作用。

共通配套：
- **minicom_router 訊息改善**：session 非 READY 且 `last_error` 屬 prompt-timeout 類 / `reprobe_attempts>0` 時，輸出明確提示「DUT 可能仍在開機，serialwrap 正在自動重試（或手動 `serialwrap session recover --selector COMx`）」，取代 `minicom_router.sh:340` 的 `broker not ready, no READY/ATTACHED session`；可選 `MINICOM_WAIT_READY` 阻塞輪詢等待 READY 再開 minicom。
- **docs**：把「self-test → 判讀 → 等自動重探 / 手動 recover」流程寫進 README/skill FAQ。

## 5. 影響範圍（預估）

- `sw_core/session_manager.py`：新增 reconcile 週期工作 + session 欄位；複用既有 `_probe_existing_bridge`/`_spawn_attach`/probe。
- `sw_core/constants.py`：`REPROBE_*` 常數。
- daemon 驅動點（`serialwrapd.py` / service periodic tick）：呼叫 reconcile。
- `tools/minicom_router.sh`：錯誤訊息 + 可選 wait。
- `README.md` / `skills/serialwrap/SKILL.md`：FAQ。
- 測試：unit（reconcile 觸發/backoff/上限/跳過 human-active 等）+ 真機驗證。

## 6. 真機驗證計畫（修復後執行）

於 COM1（user 同意的測試板）重跑 §2 的複製情境：
1. `cmd submit reboot` → 開機窗內 attach。
2. **不做任何人工 recover**，觀察 session 在 boot 完成（RX 轉閒）後 **自動** 在 backoff 窗內回 `READY`（self_test `OK`、`reprobe_attempts` 有值）。
3. `minicom_router.sh COM1` 在開機窗內執行 → 看到新的明確提示（而非 `broker not ready`）；READY 後正常開 minicom。
4. 還原板子、確認 prod daemon/其他 COM 不受影響。

## 7. Non-goals / 風險

- 不改 probe/login FSM 本身的判定邏輯（`login_fsm.py` 既有行為不變），只新增「何時重跑它」。
- 不新增 FSM 狀態（以欄位表達 retrying）。
- 風險：重探送 `\n` 可能在某些 target 留下空行——與現行 attach 相同、可接受；human-active / FLASHING / RELEASED 一律跳過避免干擾。
- 既有 flaky（非本次）：`test_five_agents_three_rounds_no_conflict`、`t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`。
