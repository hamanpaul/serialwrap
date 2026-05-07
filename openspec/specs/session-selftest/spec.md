## ADDED Requirements

### Requirement: self_test SHALL evaluate full readiness regardless of human lease by default

`SessionManager.self_test` 在預設模式下（`strict_human_lock=False`）SHALL 執行完整的 readiness 判斷順序：session 存在 → recovering 檢查 → device by-id 存在 → attached_real_path 一致 → bridge / vtty alive → ATTACHED 子狀態判斷 → `ready_probe` 寫入並等 nonce + prompt。human interactive lease 的存在 SHALL NOT 在預設模式下中斷此流程。

#### Scenario: human attaches console while session is otherwise ready

- **WHEN** session 處於 `READY`、bridge 存活、device 一致，且有 lease owner 以 `human:` 開頭
- **AND** caller 以 `strict_human_lock=False`（或省略此參數）呼叫 `session.self_test`
- **THEN** result `ok` 為 `True`、`classification` 為 `OK`、`probe_ok` 為 `True`、`recommended_action` 為 `none`
- **AND** result 不再含有 `HUMAN_INTERACTIVE_ACTIVE` 或 `wait_or_detach_console`

#### Scenario: device missing while human is attached

- **WHEN** session 有 human lease 但 by-id 已不存在
- **AND** caller 以預設模式呼叫
- **THEN** classification 為 `DEVICE_MISSING`、`recommended_action` 為 `check_cable_or_bind`
- **AND** result 同時帶上 `human_attached=True` 與 `interactive_owner` 為 lease owner 字串

### Requirement: self_test SHALL expose human attach context in every result

self_test 的所有 return SHALL 在最外層 dict 包含：
- `interactive_owner: str | null` — 若 lease 存在，為 lease owner 字串（例：`"human:abcd1234"`）；否則為 `null`。
- `human_attached: boolean` — 當 lease 存在且 owner 以 `"human:"` 開頭時為 `true`；其他情況（含無 lease、agent owner）為 `false`。

此兩欄位 SHALL 出現在所有 classification（`OK` / `SESSION_RECOVERING` / `DEVICE_MISSING` / `DEVICE_REBOUND_REQUIRED` / `BRIDGE_DOWN` / `VTTY_STALE` / `TARGET_UNRESPONSIVE` / `LOGIN_REQUIRED` / `ATTACHED_NOT_READY` / `REBOOTING` / `PASSTHROUGH` / `HUMAN_INTERACTIVE_ACTIVE`）的 result 中。

#### Scenario: lease present and human-owned

- **WHEN** session lease 存在且 owner 為 `"human:c11f019e6da44c4fabb25ab737e861d3"`
- **THEN** result 含 `interactive_owner == "human:c11f019e6da44c4fabb25ab737e861d3"` 且 `human_attached == True`

#### Scenario: lease present and agent-owned

- **WHEN** session lease 存在且 owner 為 `"agent"`
- **THEN** result 含 `interactive_owner == "agent"` 且 `human_attached == False`

#### Scenario: no active lease

- **WHEN** session 無 active interactive lease
- **THEN** result 含 `interactive_owner == None` 且 `human_attached == False`

### Requirement: self_test SHALL suspend human interactive during ready_probe

當 self_test 執行到 `ready_probe` 階段（即 session `READY`、bridge 存活、device 一致、未走早期 return）且 lease owner 以 `"human:"` 開頭時，self_test SHALL 在送出 probe 之前呼叫 `bridge.suspend_interactive()`，並在 probe 結束（含 nonce 與 prompt 等待完成或 timeout）後於 `finally` 區塊呼叫 `bridge.resume_interactive()`。`suspend_interactive()` 與 `resume_interactive()` SHALL 在 `SessionManager._lock` 之外呼叫。

當 lease 不存在或 lease owner 不以 `"human:"` 開頭時，self_test SHALL NOT 呼叫 `suspend_interactive` / `resume_interactive`。

#### Scenario: probe runs while human is attached

- **WHEN** session lease owner 為 `"human:xxx"`
- **AND** self_test 走到 probe 路徑
- **THEN** `bridge.suspend_interactive` 在 `bridge.send_command(probe, ...)` 之前被呼叫一次
- **AND** `bridge.resume_interactive` 在 probe / nonce / prompt wait 完成後被呼叫一次
- **AND** 即使 probe 內部例外，`resume_interactive` 仍透過 `finally` 被呼叫

#### Scenario: probe runs with agent lease

- **WHEN** session lease owner 為 `"agent"` 或 `"agent:xxx"`
- **AND** self_test 走到 probe 路徑
- **THEN** `bridge.suspend_interactive` 與 `bridge.resume_interactive` 皆未被呼叫

#### Scenario: early-return path with human attached

- **WHEN** session 處於 `BRIDGE_DOWN`、`VTTY_STALE`、`DEVICE_MISSING` 或其他不送 probe 的狀態
- **AND** human lease 存在
- **THEN** `bridge.suspend_interactive` 與 `bridge.resume_interactive` 皆未被呼叫
- **AND** result 仍含 `human_attached=True`、`interactive_owner` 為 lease owner

### Requirement: self_test SHALL accept strict_human_lock opt-in for legacy short-circuit

`SessionManager.self_test` SHALL 接受 `strict_human_lock: bool = False` keyword argument。當 `strict_human_lock=True` 且 lease owner 以 `"human:"` 開頭時，self_test SHALL 立即 return `classification == "HUMAN_INTERACTIVE_ACTIVE"`、`recommended_action == "wait_or_detach_console"`，並仍包含 `interactive_owner` / `human_attached` 兩個欄位以及 `interactive_id`。

`session.self_test` RPC SHALL 從 `params.get("strict_human_lock", False)` 讀取此參數並傳遞給 SessionManager。

`session self-test` CLI SHALL 接受 `--strict-human-lock` flag，無此 flag 時值為 `False`。

#### Scenario: strict mode with human attached

- **WHEN** caller 以 `strict_human_lock=True` 呼叫 `session.self_test`，且 session 有 human lease
- **THEN** classification 為 `HUMAN_INTERACTIVE_ACTIVE`、`recommended_action` 為 `wait_or_detach_console`
- **AND** result 含 `interactive_owner` 為 lease owner、`human_attached == True`、`interactive_id` 為 lease 的 id

#### Scenario: strict mode without human lease

- **WHEN** caller 以 `strict_human_lock=True` 呼叫，但 session 無 human lease（無 lease 或 agent lease）
- **THEN** self_test 行為與預設模式相同，走完整 readiness 流程

#### Scenario: CLI flag passthrough

- **WHEN** 使用 `serialwrap session self-test --selector COM0 --strict-human-lock`
- **THEN** CLI SHALL 在 RPC params 中送出 `strict_human_lock=True`
