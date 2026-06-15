## ADDED Requirements

### Requirement: device.release SHALL clean-slate detach 並標記 RELEASED

`device.release`（RPC）／`serialwrap device release --selector <COM>`（CLI）SHALL 對指定 session 關閉真實 UART FD、以 clean slate 方式拆除所有 console client（不保留、不 stash），並將 session 置於 `RELEASED` 狀態。release SHALL NOT 觸發任何自動 re-attach。release SHALL 記錄 provenance：`released_by`（caller `source`，預設 `"cli"`）、`released_at`（ISO 時間）、`released_reason`（caller `reason`，可為 null）。

#### Scenario: release 一個 attached session

- **WHEN** session 處於 `ATTACHED` 或 `READY`、bridge 存活
- **AND** caller 呼叫 `device.release` 帶 `source="agent:flash"`、`reason="flash CC2674"`
- **THEN** session.state 變為 `RELEASED`，真實 UART FD 已關閉、bridge 為 None
- **AND** 所有 console client 被關閉，`retained_consoles` 為 None（不還原）
- **AND** result 含 `released_by="agent:flash"`、`released_at`、`reason="flash CC2674"`

#### Scenario: release 強拆進行中的 foreground 命令與人類 console

- **WHEN** session 有 foreground 命令在跑或有 `human:` interactive lease
- **AND** caller 呼叫 `device.release`
- **THEN** clean slate 仍強制執行（FD 關、console 全清）
- **AND** result 透明回報拆除內容（如 `closed_consoles` 數量、是否 `aborted_cmd`）

### Requirement: RELEASED 裝置 SHALL 被所有自動 attach 路徑略過

當某 by-id 對應的 session 處於 `RELEASED`，`_spawn_attach(by_id)` SHALL 在最前面略過，使
`clear_session` re-attach、`update_devices`、`bootstrap_attach`、recovery 的 force re-attach 都
不會重新開啟該裝置。released by-id 集合 SHALL 由 session 欄位衍生並在 release/attach/load 時同步維護。

#### Scenario: clear_session 不再搶回 released 裝置

- **WHEN** session 為 `RELEASED`
- **AND** 對該 session 呼叫 `clear_session`
- **THEN** 不會呼叫 `_spawn_attach` 重開裝置，session 維持 `RELEASED`、FD 維持關閉

#### Scenario: USB 重插不搶回 released 裝置

- **WHEN** session 為 `RELEASED`
- **AND** `update_devices` 收到該 by-id 的 realpath 變動或重新出現（模擬 USB 重插）
- **THEN** `_spawn_attach` 對該 by-id 直接 return，session 維持 `RELEASED`

#### Scenario: bootstrap_attach 不搶回 released 裝置

- **WHEN** session 為 `RELEASED`
- **AND** 呼叫 `bootstrap_attach`（模擬 daemon 啟動）
- **THEN** `_spawn_attach` 對該 by-id 直接 return，session 維持 `RELEASED`

### Requirement: RELEASED 狀態 SHALL 跨 daemon 重啟保留並在 bootstrap 前還原

`_save_state` SHALL 持久化 released 資訊（`by_id` / `released_by` / `released_at` / `reason`，以
session_id 為鍵）。`_load_state` SHALL 在 `bootstrap_attach` 之前還原對應 session 的 `RELEASED`
狀態與 released by-id 集合，確保 daemon 重啟後燒錄中的裝置不被 bootstrap 搶回。

#### Scenario: release 後 daemon 重啟仍維持 released

- **WHEN** session 被 release 並寫入持久化 state
- **AND** 模擬 daemon 重啟：重新 `_load_state` 後執行 `bootstrap_attach`
- **THEN** 該 session 仍為 `RELEASED`，`bootstrap_attach` 不會開啟該裝置

### Requirement: device.attach SHALL 收回裝置並還原 session

`device.attach`（RPC）／`serialwrap device attach --selector <COM>`（CLI）SHALL 清除 session 的
released 狀態與 released by-id 集合、persist，然後重新 attach 該裝置（重建一個乾淨的 primary
console），使 session 回到 `ATTACHED`／`READY`。

#### Scenario: attach 收回並恢復 console

- **WHEN** session 為 `RELEASED` 且外部已無持有者
- **AND** caller 呼叫 `device.attach`
- **THEN** released 狀態被清除、persist
- **AND** 裝置重新 attach、session 進入 `ATTACHING`→`ATTACHED`/`READY`，並有一個新的 primary console

### Requirement: device.attach SHALL 預設拒絕外部仍持有的收回

`device.attach` SHALL 先以唯讀方式偵測該真實裝置是否仍被其他 process 持有；若仍被持有且未帶
`force`，SHALL 回 `DEVICE_STILL_HELD`（附持有者 pid 清單）並 NOT re-attach，避免重回 two-reader
race。帶 `force=true` 時 SHALL 略過此檢查並強制 re-attach。

#### Scenario: 外部仍持有時安全拒絕

- **WHEN** session 為 `RELEASED`，且唯讀偵測顯示外部 process 仍持有該裝置
- **AND** caller 呼叫 `device.attach`（未帶 force）
- **THEN** result `ok=false`、`error_code="DEVICE_STILL_HELD"`，附 `pids`
- **AND** 裝置未被 re-attach，session 維持 `RELEASED`

#### Scenario: force 略過持有檢查

- **WHEN** 同上但 caller 帶 `force=true`
- **THEN** 略過偵測，裝置被 re-attach

### Requirement: release/attach SHALL 提供冪等與明確錯誤碼

`device.release` 對已 `RELEASED`/`DETACHED` 的 session SHALL 冪等回 `ok`（標示 `already_released`）。
selector 不存在 SHALL 回 `SESSION_NOT_FOUND`。`device.attach` 對 by-id 不在 device 表的 session
SHALL 回 `DEVICE_NOT_PRESENT`。

#### Scenario: 對已 released 的 session 再 release

- **WHEN** session 已為 `RELEASED`
- **AND** caller 再次呼叫 `device.release`
- **THEN** result `ok=true` 且標示 `already_released`，狀態不變

#### Scenario: selector 不存在

- **WHEN** caller 以不存在的 selector 呼叫 `device.release` 或 `device.attach`
- **THEN** result `ok=false`、`error_code="SESSION_NOT_FOUND"`
