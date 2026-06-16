## ADDED Requirements

### Requirement: self_test SHALL 回報 RELEASED handoff 狀態與可收回性

當 session 處於 `RELEASED` 狀態，`SessionManager.self_test` SHALL 在 result 中加入 handoff
provenance 與唯讀偵測結果：
- `released_by` / `released_at` / `reason` — release 來源、時間、原因。
- `external_holder` — 唯讀偵測（讀 `/proc` 或 `lsof`，**不開 tty、不碰 I/O**）所得的外部持有者
  pid 清單；無持有者時為 `none`／空。
- `reclaimable` — 當無外部持有者時為 `true`，否則 `false`。
- `recommended_action` — 有持有者時為 `wait_external_flash`；無持有者時為 `device_attach`。

唯讀偵測 SHALL NOT 對 raw device 進行任何開啟或讀寫，以免干擾外部燒錄。

#### Scenario: RELEASED 且外部仍在燒錄

- **WHEN** session 為 `RELEASED`，唯讀偵測顯示外部 process（如 flasher）仍持有該裝置
- **AND** caller 呼叫 `session.self_test`
- **THEN** result 含 `external_holder` 為非空 pid 清單、`reclaimable=false`、`recommended_action="wait_external_flash"`
- **AND** result 含 `released_by` / `released_at`

#### Scenario: RELEASED 且外部已結束（可安全收回）

- **WHEN** session 為 `RELEASED`，唯讀偵測顯示已無外部持有者
- **AND** caller 呼叫 `session.self_test`
- **THEN** result 含 `external_holder` 為 `none`／空、`reclaimable=true`、`recommended_action="device_attach"`
