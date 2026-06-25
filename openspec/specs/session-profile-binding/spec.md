# session-profile-binding Specification

## Purpose
TBD - created by archiving change profile-pin-sticky. Update Purpose after archive.
## Requirements
### Requirement: 動態裝置 profile 四層優先序解析
attach 動態偵測裝置時，系統 SHALL 依固定優先序決定 profile：`pin` > `sticky` > `detect` > `fallback`。pin 或 sticky 命中時系統 MUST 跳過 prompt probe（不開 PROBE bridge）。

#### Scenario: pin 命中跳過偵測
- **WHEN** 某裝置的 device_key 存在於 `profile_pins`，且該 profile 名對應到已載入 template
- **THEN** 系統使用該 pin 的 template、不執行 `detect_template`，session 的 `profile_source` 為 `pin`

#### Scenario: sticky 命中跳過偵測
- **WHEN** 無 pin、但 device_key 存在於 `profile_detected` 且對應 template 存在
- **THEN** 系統使用該 sticky template、不執行 `detect_template`，`profile_source` 為 `sticky`

#### Scenario: 無 pin/sticky 時動態偵測
- **WHEN** 無 pin 也無 sticky
- **THEN** 系統執行 `detect_template`；正向命中時 `profile_source` 為 `detected`，全不符時 fallback 到 others-template、`profile_source` 為 `fallback`

#### Scenario: pin/sticky 指向不存在的 template
- **WHEN** `profile_pins`/`profile_detected` 記的 profile 名在當前 templates 中不存在
- **THEN** 該順位視為未命中，往下一順位解析（不報錯）

### Requirement: READY-gated sticky 持久化
系統 SHALL 僅在「正向偵測（`profile_source==detected`）的 session 達到 READY」後，才將該 device_key→profile 寫入 `profile_detected`。fallback 與未達 READY 的偵測 MUST NOT 被持久化為 sticky。

#### Scenario: 達 READY 才寫 sticky
- **WHEN** 一個 `detected` 來源的 session 完成 `ready_probe` 驗證、轉入 READY
- **THEN** 系統將其 device_key→profile 寫入 `profile_detected` 並持久化

#### Scenario: 未達 READY 不寫 sticky
- **WHEN** 一個 `detected` 來源的 session 偵測命中但停在 ATTACHED、未達 READY（如板子持續吐 log，或 passthrough 無 ready_probe）
- **THEN** 系統 MUST NOT 寫入 `profile_detected`

#### Scenario: fallback 不寫 sticky
- **WHEN** 偵測失敗落到 fallback（others-template）
- **THEN** 系統 MUST NOT 寫入 `profile_detected`

#### Scenario: TOCTOU 防護
- **WHEN** 寫 sticky 前該 device_key 對應的 `real_path` 與 attach 當時記錄的 `real_path` 不一致
- **THEN** 系統 MUST NOT 寫入 `profile_detected`

### Requirement: explicit pin 與 unpin
系統 SHALL 提供 `session pin` / `session unpin`（CLI 與 RPC `session.pin`/`session.unpin`）操作 `profile_pins`。pin 為最高優先權威來源。

#### Scenario: pin 有效 profile
- **WHEN** 使用者對某裝置 `session pin --profile <已載入 template 名>`
- **THEN** 系統將 device_key→profile 寫入 `profile_pins` 並回 `ok:true`，不立即重新 attach

#### Scenario: pin 未知 profile 被拒
- **WHEN** `--profile` 不在 SessionManager 載入的 templates 中
- **THEN** 系統拒絕、回 `error_code: UNKNOWN_PROFILE`，不寫入

#### Scenario: pin 對 explicit-target 裝置被拒
- **WHEN** 目標 session 的 `profile_source` 為 `yaml-target`
- **THEN** 系統拒絕、回 `error_code: PROFILE_IS_EXPLICIT`，不寫入

#### Scenario: unpin 只清 pin
- **WHEN** 使用者 `session unpin`
- **THEN** 系統清除 `profile_pins[device_key]`，但保留 `profile_detected[device_key]`（下次回 sticky 或動態偵測）

### Requirement: profile_source provenance 欄位
`session list`（`to_public_dict`）每筆 session SHALL 包含 `profile_source` 欄位，值為 `pin` / `sticky` / `detected` / `fallback` / `yaml-target` 之一。

#### Scenario: list 顯示來源
- **WHEN** 查詢 `session list`
- **THEN** 每筆 session 含 `profile_source`，反映該 session 當前 profile 的來源

#### Scenario: YAML target session 標記 yaml-target
- **WHEN** session 由 YAML explicit target 在 `__init__` 建立
- **THEN** 其 `profile_source` 為 `yaml-target`

### Requirement: 持久化向後相容與跨重啟保留
`profile_pins` 與 `profile_detected` SHALL 隨 `state.json` 持久化並跨 daemon 重啟保留；載入缺少新 key 的舊 `state.json` MUST 以空 map 處理、不報錯。

#### Scenario: 舊 state.json 向後相容
- **WHEN** 載入不含 `profile_pins`/`profile_detected` 的舊 `state.json`
- **THEN** 系統以空 map 載入、正常啟動，且 `__init__` 的首次 `_save_state` 不得洗掉這兩個 key

#### Scenario: 跨重啟保留
- **WHEN** 已寫入 pin/sticky 後 daemon 重啟
- **THEN** 重啟後 `profile_pins`/`profile_detected` 仍存在，attach 沿用之

### Requirement: device_key 穩定性
系統 SHALL 以穩定的 device_key 作為 `profile_pins`/`profile_detected` 的鍵；同款晶片 by-id 相同（碰撞）時 MUST 可改以 by-path 綁定，`session pin` 的 selector 須接受 by-path。

#### Scenario: 同晶片以 by-path 區分
- **WHEN** 兩個同款晶片裝置的 by-id 相同、但 by-path 不同
- **THEN** 系統可用 by-path 作為 device_key 對個別裝置 pin/sticky，不致張冠李戴

