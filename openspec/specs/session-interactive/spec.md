## Purpose

定義 session interactive lease 的對外契約，包含 READY-only 預設行為、Issue #44 bootloader recovery 的 `allow_attached` opt-in、human lease stash/restore、recovery timeout cap 與 `recovery_mode` 回傳欄位。

## Requirements

### Requirement: interactive_open SHALL accept allow_attached opt-in for bootloader recovery

`SessionManager.interactive_open` SHALL 接受 `allow_attached: bool = False` keyword argument。`session.interactive_open` RPC SHALL 從 `params.get("allow_attached", False)` 讀取此參數並傳遞給 SessionManager。`session interactive-open` CLI SHALL 接受 `--allow-attached` flag（store_true，default False），無此 flag 時值為 `False`。

當 `allow_attached == False`（預設）時，`interactive_open` SHALL 維持既有 READY-only gate 行為。

當 `allow_attached == True` 時，`interactive_open` SHALL 改用以下 gate 邏輯（在 `_lock` 內以原子方式執行）：

1. `session is None` 或 `session.bridge is None` → 回 `SESSION_NOT_READY`。
2. `session.state == "READY"` → 走原 READY 路徑、不檢查 bootloader（`allow_attached` 在 READY 下無作用）。
3. `session.state == "ATTACHED"` 且 `bridge.snapshot()["running"]` 與 `["serial_alive"]` 與 `["vtty_alive"]` 皆 True → 重新對 `bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)` 執行 bootloader 匹配：
   - 命中 → 開 lease（`recovery_mode=True`）。
   - 未命中 → 回 `SESSION_NOT_READY`、`error_detail: "NOT_BOOTLOADER"`。
4. 其他 state（`ATTACHING` / `RECOVERING` / `PASSTHROUGH` 等）→ 回 `SESSION_NOT_READY`。

#### Scenario: allow_attached=False rejects ATTACHED state

- **WHEN** `session.state == "ATTACHED"`、無論是否在 BOOTLOADER 子狀態
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=False)`（或省略）
- **THEN** result `ok` 為 `False`、`error_code` 為 `"SESSION_NOT_READY"`（既有行為，向後相容）

#### Scenario: allow_attached=True opens lease in BOOTLOADER

- **WHEN** `session.state == "ATTACHED"`、bridge healthy、`bridge.rx_tail(512)` 末行匹配 profile.bootloader_prompts 任一條
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result `ok` 為 `True`、回傳 `interactive_id`，且該 lease 的 `recovery_mode == True`

#### Scenario: allow_attached=True rejects ATTACHED without bootloader match

- **WHEN** `session.state == "ATTACHED"`、bridge healthy、`bridge.rx_tail(512)` 不匹配任何 `bootloader_prompts`
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result `ok` 為 `False`、`error_code` 為 `"SESSION_NOT_READY"`、`error_detail` 為 `"NOT_BOOTLOADER"`

#### Scenario: allow_attached=True with READY state behaves like normal interactive_open

- **WHEN** `session.state == "READY"`
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result 與 `allow_attached=False` 在 READY 下相同（不檢查 bootloader、`recovery_mode` 為 `False`）

### Requirement: recovery interactive lease SHALL stash human lease and restore on close

當 `interactive_open(allow_attached=True)` 在 ATTACHED + bootloader 命中下開 lease 時，若 `_refresh_interactive_locked(session)` 回非 None 且 owner 以 `"human:"` 開頭，daemon SHALL 採 **stash-and-restore** 機制——把 human session-layer lease 從 `self._interactive` 暫存到 `session._stashed_human_lease`、呼叫 `bridge.suspend_interactive()` 切換 bridge-layer ownership、開出 agent recovery lease；recovery 結束時 SHALL 還原 stashed lease 並 resume bridge-layer ownership。

具體步驟（在 `_lock` 內以原子方式執行）：

1. 將該 human lease 從 `self._interactive` pop 移除，並指派給 `session._stashed_human_lease`。
2. 將 `session.interactive_session_id` 設為 None。
3. 呼叫 `bridge.suspend_interactive()`（與 self_test 共用機制）。
4. 開出新的 agent recovery lease（`recovery_mode=True`、`suspended_human=True`）並置入 `self._interactive`、`session.interactive_session_id` 指向此 lease、`bridge.set_interactive_owner("agent")`。

當 recovery lease 透過 `interactive_close` 或 `lease.expired()` 走 close path 時，daemon SHALL：

1. 從 `self._interactive` pop recovery lease、`session.interactive_session_id` 設為 None。
2. 若 `lease.suspended_human == True`：
   - 回傳 lock 外 post-close action 呼叫 `bridge.resume_interactive()`。`resume_interactive` 既有實作會將 `_suspended_owner` 還原為 human、把 deferred buffer 中累積的 human bytes 以 `bridge.send_bytes(source="human:<client_id>", payload=<deferred>)` 一次性 flush 到 UART。
   - 從 `session._stashed_human_lease` 取出 stashed lease。若 stashed 仍未 expire 且 `bridge.console_has_external_peer(client_id) == True`，SHALL 把 stashed lease 還原回 `self._interactive`、`session.interactive_session_id` 指向它、呼叫 `bridge.set_interactive_owner(stashed.owner)`。否則 SHALL 丟棄 stashed lease（human 已離開或 lease 已 expire）。
   - 將 `session._stashed_human_lease` 設為 None。

`SessionRuntime` SHALL 新增欄位 `_stashed_human_lease: InteractiveLease | None = None`。此欄位只在 recovery lease 存在時非 None；recovery close 後恢復 None。

如果開 recovery lease 當下 `_refresh_interactive_locked` 回 None（無既有 lease），SHALL NOT 呼叫 `suspend_interactive`、SHALL 將 lease.suspended_human 設為 False、close 時亦 SHALL NOT 呼叫 `resume_interactive`。

#### Scenario: open recovery with human lease stashes existing lease

- **WHEN** session 既有 lease owner `"human:abc"`（透過 `console-attach` 開）、ATTACHED + bootloader 命中
- **AND** agent 呼叫 `interactive_open(allow_attached=True, owner="agent")`
- **THEN** human lease 從 `self._interactive` 移除、`session._stashed_human_lease` 指向該 lease、`bridge.suspend_interactive()` 被呼叫一次、新 recovery lease.recovery_mode=True、suspended_human=True、`session.interactive_session_id` 指向 recovery lease

#### Scenario: close recovery flushes deferred buffer and restores human lease

- **WHEN** recovery lease 已開、human 在期間從 console 寫入若干 bytes（這些 bytes 進入 deferred buffer）、stashed human lease 仍未 expire、human 仍 console-attached
- **AND** agent 呼叫 `interactive_close`
- **THEN** recovery lease 從 `_interactive` 移除、`bridge.resume_interactive()` 被呼叫一次、`bridge.send_bytes(<deferred bytes>, source="human:abc")` 被呼叫一次、stashed human lease 還原回 `self._interactive`、`session.interactive_session_id` 指向 human lease、`session._stashed_human_lease` 為 None

#### Scenario: close recovery discards expired stash

- **WHEN** recovery lease 已開、stashed human lease 在 recovery 期間 expire（`lease.expired() == True`）
- **AND** agent 呼叫 `interactive_close`
- **THEN** recovery lease 移除、`bridge.resume_interactive()` 被呼叫一次（仍 flush 任何 deferred bytes）、stashed human lease 被丟棄、`session._stashed_human_lease` 為 None、session 回到「無 lease」狀態

#### Scenario: close recovery discards stash when human detached

- **WHEN** recovery lease 已開、human 在 recovery 期間透過 `console-detach` 斷開（或 console connection 自然中斷）、`bridge.console_has_external_peer(client_id) == False`
- **AND** agent 呼叫 `interactive_close`
- **THEN** stashed human lease 被丟棄、session 回到「無 lease」狀態

#### Scenario: open recovery without human attached

- **WHEN** session 無 lease、ATTACHED + bootloader 命中
- **AND** agent 呼叫 `interactive_open(allow_attached=True, owner="agent")`
- **THEN** `bridge.suspend_interactive()` SHALL NOT 被呼叫、lease.suspended_human=False、`session._stashed_human_lease` 維持 None

#### Scenario: recovery lease auto-expires resumes human

- **WHEN** recovery lease 已開、human 已 stashed
- **AND** `lease.expired()` 變成 True、agent 後續呼叫 `interactive_send`
- **THEN** `interactive_send` 走 expired close path（與 `interactive_close` 相同的 stash 還原流程）、回傳 `ok: false` / `error_code: "INTERACTIVE_EXPIRED"`

#### Scenario: refresh caller returning busy still resumes human

- **WHEN** recovery lease 已開、human 已 stashed，且 `lease.expired()` 變成 True
- **AND** 後續 caller 經由 `_refresh_interactive_locked` 清理 expired recovery lease 後仍要回 `SESSION_INTERACTIVE_BUSY`
- **THEN** caller SHALL 先在 `SessionManager._lock` 外執行 post-close action，呼叫 `bridge.resume_interactive()` flush deferred bytes，再回傳 busy result

### Requirement: recovery lease SHALL enforce MAX_RECOVERY_LEASE_S timeout cap

當 `interactive_open(allow_attached=True)` 開 lease 時，daemon SHALL 對 caller 傳入的 `timeout_s` 做 `min(timeout_s, MAX_RECOVERY_LEASE_S)` 的 clamp。`MAX_RECOVERY_LEASE_S` SHALL 是 daemon 內部常數（`sw_core/constants.py`，初值 120.0），不暴露為 RPC 入參。Result 內 `session` 或 lease snapshot SHALL 反映實際生效的 `timeout_s`，使 caller 能觀察到 clamp 結果。

READY 路徑（`allow_attached=False` 或 `state == "READY"`）SHALL NOT 受此 clamp 影響。

#### Scenario: clamp caller-supplied timeout

- **WHEN** caller 呼叫 `interactive_open(allow_attached=True, timeout_s=600.0)`、ATTACHED + bootloader 命中
- **THEN** lease.timeout_s == 120.0、result 反映實際 timeout 為 120.0

#### Scenario: small timeout passes through

- **WHEN** caller 呼叫 `interactive_open(allow_attached=True, timeout_s=30.0)`、ATTACHED + bootloader 命中
- **THEN** lease.timeout_s == 30.0（不被 clamp）

#### Scenario: READY path unaffected

- **WHEN** caller 呼叫 `interactive_open(allow_attached=True, timeout_s=600.0)`、`session.state == "READY"`
- **THEN** lease.timeout_s == 600.0（READY 路徑不受 recovery clamp 約束）

### Requirement: lease snapshot SHALL expose recovery_mode field

`InteractiveLease` SHALL 新增 `recovery_mode: bool`（預設 False）與 `suspended_human: bool`（預設 False）兩個欄位。`recovery_mode` SHALL 在以下處透出：

- `SessionManager._lease_context(lease)` 的 return dict（被 `self_test` 使用）。
- `SessionManager.interactive_status` 的 return dict（在 `lease` / `session` 旁的最外層）。
- `SessionManager.interactive_open` 開 lease 成功時的 return dict（最外層）。

`suspended_human` SHALL 為 daemon 內部 lifecycle 旗標，不必透出至 RPC result（避免 caller 依賴內部實作細節）。

#### Scenario: recovery_mode visible to caller

- **WHEN** agent 呼叫 `interactive_open(allow_attached=True)` 成功
- **THEN** result 最外層 `recovery_mode == true`

#### Scenario: recovery_mode visible in interactive_status

- **WHEN** recovery lease 開著、agent 呼叫 `interactive_status(interactive_id)`
- **THEN** result 最外層 `recovery_mode == true`

#### Scenario: recovery_mode false for normal READY lease

- **WHEN** agent 在 READY 狀態呼叫 `interactive_open(allow_attached=False)` 成功
- **THEN** result `recovery_mode == false`

### Requirement: recovery lease SHALL NOT preempt non-human lease

`interactive_open(allow_attached=True)` 的「stash human lease」行為 SHALL 僅限於既有 lease owner 以 `"human:"` 開頭的情況。若 `_refresh_interactive_locked(session)` 回非 None lease 且 owner **不**以 `"human:"` 開頭（例如 owner=`"agent"` 的既有 interactive lease 或 command path lease），SHALL 回 `ok: false` / `error_code: "SESSION_INTERACTIVE_BUSY"`，不執行 stash。

理由：recovery 的設計目的是不打擾人類觀察者；agent 之間的搶 lease 沒有同樣的協作正當性，應由 caller 自行協調而非由 daemon 強制 stash。

#### Scenario: agent lease present blocks new recovery lease

- **WHEN** session 已有 lease owner `"agent"`、`recovery_mode == False`
- **AND** agent 呼叫 `interactive_open(allow_attached=True)`
- **THEN** result `ok` 為 `False`、`error_code` 為 `"SESSION_INTERACTIVE_BUSY"`、stashed lease 不被建立

#### Scenario: existing recovery lease blocks new recovery lease

- **WHEN** session 已有 recovery lease（owner=`"agent"`、`recovery_mode == True`）
- **AND** 同一個或另一個 agent 呼叫 `interactive_open(allow_attached=True)`
- **THEN** result `error_code` 為 `"SESSION_INTERACTIVE_BUSY"`、不影響既有 recovery lease 與 stashed human lease
