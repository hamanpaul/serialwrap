## Purpose

定義 `session.self_test` readiness 分類、human interactive context 欄位，以及 Issue #44 bootloader recovery 所需的 BOOTLOADER 分類與 profile schema。
## Requirements
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

- **WHEN** session profile 的 `bootloader_prompts` 為 `()`、`session.state == "ATTACHED"`、無 `LOGIN_REQUIRED` / `REBOOTING` / `PASSTHROUGH` 條件
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

profile 載入流程 SHALL 接受可選 YAML 欄位 `bootloader_prompts: list[str]`（預設 `[]`）。loader SHALL 只保留 list 中的 `str` 元素，並在 `ProfileTemplate.bootloader_prompts` 與 `SessionProfile.bootloader_prompts` 內以 immutable `tuple[str, ...]` 儲存。每個字串元素 SHALL 為 Python regex 字串（與既有 `prompt_regex` 同 flavor）；loader SHALL NOT 驗證 regex 在 RX tail 上的可行性（profile 維護者責任）。

#### Scenario: profile without bootloader_prompts

- **WHEN** profile YAML 不含 `bootloader_prompts` key
- **THEN** profile 載入成功、`profile.bootloader_prompts` 為 `()`、self_test 行為與導入此欄位前一致

#### Scenario: profile with bootloader_prompts list

- **WHEN** profile YAML 含 `bootloader_prompts: ["^=> $", "^Marvell>> $"]`
- **THEN** profile 載入成功、`profile.bootloader_prompts == ("^=> $", "^Marvell>> $")`

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

### Requirement: self_test SHALL expose human_active in every result

`SessionManager.self_test` 的所有 return SHALL 在最外層 dict 包含 `human_active: boolean`：當 session 存在 human lease 且 `now - last_human_input_at <= HUMAN_ACTIVE_WINDOW_S`（60s）時為 `true`；其他情況（無 lease、agent owner、human lease 但已閒置超過視窗）為 `false`。

此欄位 SHALL 與既有 `human_attached` 並存且不改變 `human_attached` 語意（`human_attached` 仍為「有 human lease」）。在**預設模式**（`strict_human_lock=False`）下，`self_test` 的 idle 相關 `recommended_action` SHALL 以 `human_active`（而非 `human_attached`）為依據——閒置（`human_active=False`）的 human lease SHALL NOT 阻擋 readiness 判定或 agent 操作建議。

`strict_human_lock=True` 為明確 opt-in 的嚴格模式，刻意尊重**任何** human lease（不論 active/idle），故其 `HUMAN_INTERACTIVE_ACTIVE` / `wait_or_detach_console` 行為 SHALL NOT 受 `human_active` 放寬影響——這是設計上的刻意例外。

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

