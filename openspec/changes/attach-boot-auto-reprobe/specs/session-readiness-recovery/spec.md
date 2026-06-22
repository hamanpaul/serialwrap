## ADDED Requirements

### Requirement: daemon SHALL 有界自動重探卡住的非 READY session 回 READY

當 session 因 attach 撞 DUT 開機窗、probe 等不到 prompt 而停在非 READY（`ATTACHED` 且 `last_error` 屬 prompt-unavailable 類，或 `DETACHED` 且 `last_error` 屬 prompt-timeout 類），且 device 仍在位、未 RELEASED/FLASHING、無 human-active interactive lease 時，daemon SHALL 在 RX 轉閒後以有界 backoff 自動重跑既有 readiness probe，直到 session 進入 `READY` 或達重探上限。重探 SHALL 複用既有 attach/probe 路徑，不改變 probe/login 的判定邏輯。

#### Scenario: 開機窗 attach 後自動回 READY

- **WHEN** session 因開機窗 attach 而停在非 READY（prompt 當下不可用）
- **AND** DUT 開機完成、prompt 變為可用（RX 自上次起 idle ≥ `REPROBE_RX_IDLE_S`）
- **THEN** daemon 自動重探並使 session 進入 `READY`，無需人工 `recover`/`attach`
- **AND** `cmd submit` 在重探成功後可正常運作

#### Scenario: RX 仍在噴（開機中）不重探

- **WHEN** session 非 READY 但 RX 仍持續活動（boot log 尚在輸出，idle < `REPROBE_RX_IDLE_S`）
- **THEN** daemon 此刻不重探（避免在無 prompt 的開機 log 中瞎探），待 RX 轉閒再試

### Requirement: 自動重探 SHALL 有界且可觀測

自動重探 SHALL 受 backoff 與上限（最大嘗試次數或截止時間）約束；達上限後 SHALL 停止自動重探、維持非 READY 並保留明確 `last_error`。session 的重探進度 SHALL 透過公開欄位（如 `reprobe_attempts` / `next_reprobe_at` / `reprobe_exhausted`）於 `session.get_state` 與 `self_test` 暴露，供呼叫端與 minicom_router 判讀。

#### Scenario: 達上限後停手並回報

- **WHEN** 自動重探達到最大嘗試次數 / 截止時間仍未 READY
- **THEN** daemon 停止對該 session 自動重探，`reprobe_exhausted` 為真、保留明確 `last_error`，等待人工介入

#### Scenario: 重探進度可由 self_test 觀測

- **WHEN** 呼叫端對重探中的 session 執行 `self_test`
- **THEN** 回應包含重探進度欄位（嘗試次數 / 下次重探時間 / 是否已耗盡）

### Requirement: 自動重探 SHALL NOT 干擾 human-active / FLASHING / RELEASED

daemon 自動重探 SHALL 跳過：持有 human-active interactive lease 的 session、處於 `FLASHING` 的 session、以及 `RELEASED`（或其 by-id 在 released 集合）的 session；對這些情況 SHALL NOT 送出任何 probe bytes。

#### Scenario: human 正在互動時不重探

- **WHEN** session 有 human-active interactive lease（最近有真人鍵入）
- **THEN** daemon 不對其自動重探、不送 probe，避免干擾人類操作

#### Scenario: FLASHING / RELEASED 不重探

- **WHEN** session 處於 `FLASHING` 或 `RELEASED`
- **THEN** daemon 不對其自動重探、不送任何 bytes
