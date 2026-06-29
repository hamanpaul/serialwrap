## ADDED Requirements

### Requirement: COM 編號依 device_key 排序確定性指派

daemon 在 startup 對「當下在線的 dynamic 自動偵測裝置」SHALL 依 `device_key`（`/dev/serial/by-id` 路徑字串）排序後的 rank 指派 COM 編號（`COM0, COM1, …`），且指派 MUST 在配置 lock 內、於 spawn 並發 attach thread 之前完成，使 COM↔裝置 對應與 attach 完成順序無關。

排序鍵已預留 by-path 次序 tiebreak 骨架（`device_sort_key` 接受 by-path 參數）；同款晶片（如 CH340）by-id 相同時以 by-path 去衝突的 **end-to-end 完整支援為 TODO**，待 `DeviceInfo.by_path` 欄位接上資料來源，在此之前 rank 僅依 by-id。

#### Scenario: 亂序/並發 attach 仍得確定性 COM
- **WHEN** 兩片 dynamic 裝置（by-id `AC01QZT0`、`AQ00OAQ7`）以任意順序或並發被偵測 attach
- **THEN** COM0 綁定 `AC01QZT0`、COM1 綁定 `AQ00OAQ7`（依 by-id 字典序），與 attach 完成順序無關

#### Scenario: restart 後對應不變
- **WHEN** daemon restart 且兩片裝置皆在線
- **THEN** 重新指派的 COM↔by-id 對應與 restart 前相同

#### Scenario: 同款晶片 by-id 衝突改用 by-path（TODO：待 DeviceInfo.by_path）
- **WHEN** 兩裝置 by-id 相同（如同款 CH340）
- **THEN** 以 by-path 作為排序鍵決定 rank，不致遺漏或對調（排序單元已備；end-to-end 待 `DeviceInfo.by_path` 接上）

### Requirement: rank 僅作用於 dynamic 自動偵測 session

確定性 rank SHALL 僅套用於 dynamic 自動偵測產生的 session。由 explicit YAML `targets` 指定的 COM、或 `session.bind` / binding override 綁定的 COM MUST 維持其權威值，排除於 rank pool 之外。RELEASED by-id 不自動 attach、不進入 rank pool。

#### Scenario: explicit target COM 不被 rank 覆寫
- **WHEN** 某 COM 由 YAML `targets` 或 `session.bind` 顯式指定
- **THEN** 該 COM↔裝置 對應不受 dynamic rank 重排影響

### Requirement: runtime hotplug 維持既有 slot 繼承行為

daemon 存活期間，runtime 熱插裝置 SHALL 維持既有行為：同 by-id 裝置重接拿回自己原槽；不同 by-id 裝置繼承空出的 DETACHED / RELEASED 槽；active session 的 COM 名 MUST NOT 在 runtime 被自動重編。

#### Scenario: 同 by-id 重接拿回原槽
- **WHEN** COM0 的裝置拔除後同一片板重新插入
- **THEN** 該板仍為 COM0

#### Scenario: 不同板繼承空出的槽
- **WHEN** COM0 的裝置拔除（session 轉 DETACHED）後插入不同 by-id 的板
- **THEN** 新板繼承 COM0（沿用既有 DETACHED-rebind），active session 不被打斷
