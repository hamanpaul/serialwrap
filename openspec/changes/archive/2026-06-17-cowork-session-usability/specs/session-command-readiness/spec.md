## ADDED Requirements

### Requirement: command_capable SHALL be determined by non-empty ready_probe

`SessionManager` SHALL 以 `command_capable = bool(profile.ready_probe.strip())` 判定一個 session 是否「可下命令」。此判準 SHALL 取代原本以 `profile.platform == "passthrough"` 寫死 `ok=False` 的 readiness 邏輯（`_attach_by_id` 與 `_probe_existing_bridge`）。

當 `command_capable` 為 True 時，attach/probe 路徑 SHALL 走正常 `probe_ready`/`ensure_ready` 流程，使具可用 `ready_probe` 的 profile（包含 `platform == "passthrough"` 但已設定 `ready_probe` 的 target）能進入 `READY`。

當 `command_capable` 為 False 時，session SHALL 維持 `ATTACHED`，不嘗試 `READY`。

#### Scenario: empty ready_probe stays ATTACHED

- **WHEN** session 綁定的 profile `ready_probe` 為空字串（如預設 `others-template`）
- **AND** 裝置 attach 成功、bridge 存活
- **THEN** session.state 為 `ATTACHED`、不轉為 `READY`

#### Scenario: passthrough with configured ready_probe reaches READY

- **WHEN** session 綁定的 profile `platform == "passthrough"` 但 `ready_probe` 非空、且 `prompt_regex` 可匹配 target prompt
- **AND** 裝置 attach 成功、`probe_ready` 成功（送出 probe → 看到 nonce → 回到 prompt）
- **THEN** session.state 為 `READY`、`cmd submit --mode line` 可被接受

### Requirement: cmd submit SHALL return PROFILE_NOT_COMMAND_CAPABLE for non-command-capable sessions

當 session 處於 `ATTACHED` 且 `command_capable` 為 False 時，`cmd submit` SHALL 回 `{"ok": False, "error_code": "PROFILE_NOT_COMMAND_CAPABLE"}` 並附帶說明 hint（此 profile 僅支援 console，要下命令請設定 `ready_probe` 或改用具 prompt 的 profile），SHALL NOT 再回 `SESSION_NOT_READY`。

`SESSION_NOT_READY` SHALL 保留給「`command_capable` 為 True 但尚未 `READY`」的情形。

#### Scenario: non-command-capable cmd submit

- **WHEN** session.state 為 `ATTACHED` 且 `command_capable` 為 False
- **AND** caller 呼叫 `cmd submit --selector <s> --cmd 'echo x' --mode line`
- **THEN** result `ok` 為 False、`error_code` 為 `PROFILE_NOT_COMMAND_CAPABLE`、含說明 hint

#### Scenario: command-capable but not yet ready

- **WHEN** session 綁定的 profile `command_capable` 為 True、但 session.state 仍為 `ATTACHED`（尚未通過 probe）
- **AND** caller 呼叫 `cmd submit`
- **THEN** result `error_code` 為 `SESSION_NOT_READY`（非 `PROFILE_NOT_COMMAND_CAPABLE`）

### Requirement: READY SHALL be platform-agnostic and support bootloader command profiles

`READY` 的判定 SHALL 定義為「對得上該 profile 的 `prompt_regex` + `ready_probe` 能 round-trip」，與底層是 OS shell 或 bootloader 無關。系統 SHALL 提供一個 bootloader 取向的 command profile（`uboot-template`）使 session 在 U-Boot prompt 下可進 `READY` 並接受 line 命令。

READY 判定 SHALL 相對於該 session 綁定 profile 預期的 prompt：若 OS profile 的 `prompt_regex` 對不上當前畫面（例如板子掉進 U-Boot），session SHALL NOT 為 `READY`。

#### Scenario: uboot-template session reaches READY at U-Boot prompt

- **WHEN** session 綁定 `uboot-template`（`prompt_regex` 對 U-Boot 提示符、`ready_probe` 為 U-Boot 可用之回顯指令）
- **AND** target 處於 U-Boot prompt、`probe_ready` 成功
- **THEN** session.state 為 `READY`、`cmd submit --cmd 'printenv' --mode line` 可被接受並框出輸出

#### Scenario: OS profile at bootloader prompt is not READY

- **WHEN** session 綁定 OS profile（如 `prpl-template`）但 target 掉進 bootloader、OS `prompt_regex` 對不上
- **THEN** session SHALL NOT 為 `READY`（不會把 OS 命令送進 bootloader）
