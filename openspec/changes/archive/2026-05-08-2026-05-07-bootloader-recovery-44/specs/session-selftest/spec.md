## ADDED Requirements

### Requirement: self_test SHALL classify BOOTLOADER when ATTACHED RX tail matches bootloader_prompts

`SessionManager.self_test` 在 `session.state == "ATTACHED"` 路徑下，SHALL 在既有 `LOGIN_REQUIRED` / `REBOOTING` / `PASSTHROUGH` 子分類判斷之後，但在落入 `ATTACHED_NOT_READY` 之前，先檢查 `profile.bootloader_prompts`：將 `bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)` 的最後一行對 `bootloader_prompts` 中每條 regex 逐一匹配；若任一命中，SHALL 回傳 `classification: "BOOTLOADER"`、`recommended_action: "recover_interactive"`，並在 result 上附帶：

- `matched_prompt: str` — 命中的 regex 字面值。
- `rx_tail: str` — `clean_text(bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES))` 的結果，作為 agent 判斷階段（kernel banner / boot menu / U-Boot prompt）的證據。

`BOOTLOADER_RX_TAIL_BYTES` SHALL 為 daemon 內部常數（`sw_core/constants.py`，初值 512），不暴露為 RPC 入參。

#### Scenario: bootloader prompt visible in RX tail

- **WHEN** session profile 帶 `bootloader_prompts: ["^=> $"]`、`session.state == "ATTACHED"`、`bridge.rx_tail(512)` 末行為 `"=> "`
- **AND** caller 以預設模式（`strict_human_lock=False`）呼叫 `session.self_test`
- **THEN** result `ok` 為 `True`、`classification` 為 `"BOOTLOADER"`、`recommended_action` 為 `"recover_interactive"`、`matched_prompt` 為 `"^=> $"`、`rx_tail` 含子字串 `"=> "`

#### Scenario: bootloader_prompts empty falls back to ATTACHED_NOT_READY

- **WHEN** session profile 的 `bootloader_prompts` 為 `[]`、`session.state == "ATTACHED"`、無 `LOGIN_REQUIRED` / `REBOOTING` / `PASSTHROUGH` 條件
- **THEN** result `classification` 為 `"ATTACHED_NOT_READY"`、`recommended_action` 為 `"console_attach"`（既有行為，向後相容）

#### Scenario: bootloader prompt and OS prompt both could match

- **WHEN** `bridge.rx_tail` 同時可能匹配 `prompt_regex`（OS）與 `bootloader_prompts`
- **THEN** 因 self_test 在 `state == "ATTACHED"` 才走 bootloader 分支，而非在 probe 後分類，因此實務上不會與 OS prompt 同步成立；若 ATTACHED 路徑下 RX tail 命中 bootloader regex，SHALL 取 `BOOTLOADER` classification

### Requirement: self_test result SHALL expose recovery_mode in lease_context

`SessionManager.self_test` 的所有 return（不分 classification）SHALL 在 `_lease_context` 既有 `interactive_owner` / `human_attached` 兩欄之外，新增 `recovery_mode: bool`：當 lease 存在且 `lease.recovery_mode == True` 時為 `true`，其他情況（無 lease、agent 一般 lease、human lease）為 `false`。

#### Scenario: recovery lease in progress

- **WHEN** session 已經有 lease owner 為 `"agent"`、`lease.recovery_mode == True`
- **AND** caller 呼叫 `session.self_test`
- **THEN** result 含 `recovery_mode == true`、`interactive_owner == "agent"`、`human_attached == false`

#### Scenario: no lease

- **WHEN** session 無 active lease
- **THEN** result 含 `recovery_mode == false`、`interactive_owner == null`、`human_attached == false`

### Requirement: profile parser SHALL accept optional bootloader_prompts list

profile 載入流程 SHALL 接受可選欄位 `bootloader_prompts: list[str]`（預設 `[]`）。每元素 SHALL 為合法 Python regex 字串（與既有 `prompt_regex` 同 flavor）。SHALL NOT 驗證 regex 在 RX tail 上的可行性（profile 維護者責任）。

#### Scenario: profile without bootloader_prompts

- **WHEN** profile YAML 不含 `bootloader_prompts` key
- **THEN** profile 載入成功、`profile.bootloader_prompts` 為 `[]`、self_test 行為與導入此欄位前一致

#### Scenario: profile with bootloader_prompts list

- **WHEN** profile YAML 含 `bootloader_prompts: ["^=> $", "^Marvell>> $"]`
- **THEN** profile 載入成功、`profile.bootloader_prompts == ["^=> $", "^Marvell>> $"]`
