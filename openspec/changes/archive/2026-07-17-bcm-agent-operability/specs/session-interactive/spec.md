## MODIFIED Requirements

### Requirement: interactive_open SHALL accept allow_attached opt-in for bootloader recovery

`SessionManager.interactive_open` SHALL 接受 `allow_attached: bool = False` keyword argument。`session.interactive_open` RPC SHALL 從 `params.get("allow_attached", False)` 讀取此參數並傳遞給 SessionManager。`session interactive-open` CLI SHALL 接受 `--allow-attached` flag（store_true，default False），無此 flag 時值為 `False`。

當 `allow_attached == False`（預設）時，`interactive_open` SHALL 維持既有 READY-only gate 行為。

當 `allow_attached == True` 時，`interactive_open` SHALL 改用以下 gate 邏輯（在 `_lock` 內以原子方式執行）：

1. `session is None` 或 `session.bridge is None` → 回 `SESSION_NOT_READY`。
2. `session.state == "READY"` → 走原 READY 路徑、不檢查 bootloader（`allow_attached` 在 READY 下無作用）。
3. `session.state == "ATTACHED"` 且 `bridge.snapshot()["running"]` 與 `["serial_alive"]` 與 `["vtty_alive"]` 皆 True → 對 `bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)` 依序判定：
   - 末行匹配 `profile.bootloader_prompts` 任一條（bootloader prompt，如 `=>`）→ 開 lease（`recovery_mode=True`），回應 `boot_interrupt` 省略或為 `False`。
   - 否則，RX tail 命中 boot banner（`detect_boot_banner`，比對 `BOOT_BANNER_PATTERNS`＝`U-Boot` 版本行／`Hit any key to stop autoboot` autoboot 倒數行；#130 既有單一事實來源）→ 開 lease（`recovery_mode=True`），回應標 `boot_interrupt: True`（表「autoboot 倒數窗中斷模式」，供呼叫端連打按鍵中斷 autoboot）。
   - 兩者皆未命中 → 回 `SESSION_NOT_READY`、`error_detail: "NOT_BOOTLOADER"`。
4. 其他 state（`ATTACHING` / `RECOVERING` / `PASSTHROUGH` 等）→ 回 `SESSION_NOT_READY`。

banner 命中所授予的 lease 與 prompt 命中者相同（`recovery_mode=True`、`owner` 沿用 `--owner`，預設 `agent`、human lease stash/restore 行為一致）；差異僅在回應的 `boot_interrupt` 欄位。此 lease 的 TX 不受 #130 boot quiet window gate（human/lease TX 永不 gate），故 agent 可於倒數窗連打按鍵中斷 autoboot。

#### Scenario: allow_attached=False rejects ATTACHED state

- **WHEN** `session.state == "ATTACHED"`、無論是否在 BOOTLOADER 子狀態
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=False)`（或省略）
- **THEN** result `ok` 為 `False`、`error_code` 為 `"SESSION_NOT_READY"`（既有行為，向後相容）

#### Scenario: allow_attached=True opens lease in BOOTLOADER

- **WHEN** `session.state == "ATTACHED"`、bridge healthy、`bridge.rx_tail(512)` 末行匹配 profile.bootloader_prompts 任一條
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result `ok` 為 `True`、回傳 `interactive_id`，且該 lease 的 `recovery_mode == True`、回應 `boot_interrupt` 非 `True`

#### Scenario: allow_attached=True opens lease during autoboot countdown (banner)

- **WHEN** `session.state == "ATTACHED"`、bridge healthy、`bridge.rx_tail(512)` 未匹配 `bootloader_prompts` 但命中 `detect_boot_banner`（如末段含 `Hit any key to stop autoboot`）
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result `ok` 為 `True`、回傳 `interactive_id`、該 lease `recovery_mode == True`，且回應 `boot_interrupt == True`

#### Scenario: allow_attached=True rejects ATTACHED without bootloader nor banner match

- **WHEN** `session.state == "ATTACHED"`、bridge healthy、`bridge.rx_tail(512)` 既不匹配任何 `bootloader_prompts` 也不命中 `detect_boot_banner`
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result `ok` 為 `False`、`error_code` 為 `"SESSION_NOT_READY"`、`error_detail` 為 `"NOT_BOOTLOADER"`

#### Scenario: allow_attached=True with READY state behaves like normal interactive_open

- **WHEN** `session.state == "READY"`
- **AND** caller 呼叫 `interactive_open(selector, allow_attached=True)`
- **THEN** result 與 `allow_attached=False` 在 READY 下相同（不檢查 bootloader/banner、`recovery_mode` 為 `False`）
