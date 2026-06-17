## ADDED Requirements

### Requirement: self_test SHALL expose human_active in every result

`SessionManager.self_test` 的所有 return SHALL 在最外層 dict 包含 `human_active: boolean`：當 session 存在 human lease 且 `now - last_human_input_at <= HUMAN_ACTIVE_WINDOW_S`（60s）時為 `true`；其他情況（無 lease、agent owner、human lease 但已閒置超過視窗）為 `false`。

此欄位 SHALL 與既有 `human_attached` 並存且不改變 `human_attached` 語意（`human_attached` 仍為「有 human lease」）。`self_test` 的 idle 相關 `recommended_action` SHALL 以 `human_active`（而非 `human_attached`）為依據——閒置（`human_active=False`）的 human lease SHALL NOT 阻擋 readiness 判定或 agent 操作建議。

#### Scenario: human lease present but idle

- **WHEN** session 有 human lease，但 `now - last_human_input_at > 60s`
- **THEN** result 含 `human_attached == True` 且 `human_active == False`
- **AND** `recommended_action` SHALL NOT 因 human lease 而要求 wait/detach（不被閒置 human 阻擋）

#### Scenario: human lease present and active

- **WHEN** session 有 human lease，且 `now - last_human_input_at <= 60s`
- **THEN** result 含 `human_attached == True` 且 `human_active == True`

#### Scenario: no lease

- **WHEN** session 無 interactive lease
- **THEN** result 含 `human_attached == False` 且 `human_active == False`

### Requirement: self_test SHALL expose command_capable in every result

`SessionManager.self_test` 的所有 return SHALL 在最外層 dict 包含 `command_capable: boolean`，其值為 `bool(profile.ready_probe.strip())`。此欄位讓呼叫端能區分「ATTACHED 但本來就不可下命令」與「ATTACHED 且應可進 READY」。

#### Scenario: passthrough profile without ready_probe

- **WHEN** session 綁定的 profile `ready_probe` 為空字串
- **THEN** result 含 `command_capable == False`

#### Scenario: profile with ready_probe

- **WHEN** session 綁定的 profile `ready_probe` 非空
- **THEN** result 含 `command_capable == True`
