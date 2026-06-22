## Context

完整背景、真機複製證據與根因見 `docs/superpowers/specs/2026-06-22-attach-boot-auto-reprobe-design.md`。摘要：readiness probe（`login_fsm.ensure_ready`/`probe_ready`）只在 attach 觸發跑一次；daemon loop（`uart_io._loop`）無週期 re-probe；故 attach 撞開機窗失敗後 session 永遠卡非 READY 直到人工 recover。

## Goals / Non-Goals

**Goals**
- daemon 在 prompt 稍後可用時，有界自動把卡住的 session 重探回 READY，無需人工。
- 對 agent 與 human minicom 兩條路徑都生效（修根因，非只改 router UX）。

**Non-Goals**
- 不改 `login_fsm` 的 probe/login 判定邏輯與 command_capable（session-command-readiness）requirement。
- 不新增 FSM 狀態（以 session 欄位表達 retrying）。
- 不處理 #52（共用 console 流量）。

## Decisions

- **方案 A（daemon 有界重探，RX-idle 觸發）** 而非 B（新增 WAITING_PROMPT 狀態）：避免擴大 FSM/相容/測試面；以 `reprobe_attempts`/`next_reprobe_at`/`reprobe_exhausted` 欄位表達。
- **RX-idle 觸發**：自上次 RX idle ≥ `REPROBE_RX_IDLE_S` 才重探——boot log 狂噴時不瞎探，log 停＝prompt 可能已出現（真機觀察 idle 在 boot 完成後開始成長）。
- **複用既有 probe**：`ATTACHED` 走 `_probe_existing_bridge`、`DETACHED` 走 `_spawn_attach`/`ensure_ready`（含 auth），不另寫 probe。
- **有界**：backoff（`REPROBE_BACKOFF_S`→`REPROBE_MAX_INTERVAL_S`）+ 上限（`REPROBE_MAX_ATTEMPTS` 或 `REPROBE_DEADLINE_S`）；達上限停手、維持非 READY + 明確 `last_error` + `reprobe_exhausted=True`。
- **跳過**：human-active interactive lease、FLASHING、RELEASED、device 不在位。

## Risks

- 重探送 `\n`/ready_probe 可能在 target 留空行——與現行 attach 相同、可接受。
- 與既有 recover/attach 並發：以 session lock + `next_reprobe_at` 去重，避免重入。

## Migration

無資料/介面破壞；新增欄位為附加。既有手動 `recover`/`attach` 行為不變；自動重探只是讓多數情況不再需要人工。
