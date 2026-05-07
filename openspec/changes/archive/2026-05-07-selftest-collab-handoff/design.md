# Design: self_test 與 human-monitor 協作

## Context

`SessionManager.self_test`（`sw_core/session_manager.py:1651`）目前的判斷順序：
1. session 存在
2. session.recovering
3. **human lease → 立即回 `HUMAN_INTERACTIVE_ACTIVE`**（lines 1666–1675，short-circuit）
4. device by-id 仍存在
5. attached_real_path 一致
6. bridge / vtty alive
7. ATTACHED 子分類
8. 送 `ready_probe` 並等 nonce + prompt

問題出在第 3 步。實際的 command path（`_execute_command`，session_manager.py:1289-1311）已能在 `source != "human:..."` 且 `lease.owner.startswith("human:")` 時呼叫 `bridge.suspend_interactive()` → 執行 → `bridge.resume_interactive()`，把 human 即時輸入 defer 到 buffer 並在恢復時 flush。`file.push` / `file.pull`（lines 1981-2014、2027-2057）採同一 pattern。

self_test 的 short-circuit 因此給出**錯誤的 readiness 訊號**：實際上 agent 仍可接管下指令，但 controller 看到 `wait_or_detach_console` 會選擇放棄。Issue #42 中提到的 reboot-test toolkit 即因此無法在 human 仍 attach console 觀察 reboot 訊息時，啟動 agent 控制流程。

## Goals / Non-Goals

**Goals:**
- self_test 在 human 仍 attach console 時，預設仍能完成完整 readiness 評估與 probe，回報正確的 `OK` / 故障分類。
- 呼叫者能從 self_test result 直接得知 human 是否在場（`human_attached`、`interactive_owner`），無須額外查 `session.list`。
- self_test 在 probe 階段對 UART 寫入時，與 human typing 不會互相覆寫；行為與 `command.submit` 等 path 一致。
- 對既有的 strict 行為仍提供 opt-in 入口（向後相容）。

**Non-Goals:**
- 不重做 suspend/resume 機制本身——僅延用 `UARTBridge.suspend_interactive` / `resume_interactive`。
- 不改 `session.list` 或 `session.get_state` 的輸出結構（human 在場資訊原本就已可間接讀到）。
- 不為 human typing/observation 引入新的 throttling / coordination 機制。
- 不嘗試區分「human 只是 attached」vs「human 正在打字」——以 lease 存在與否為單一信號。

## Decisions

### D1：移除 self_test 對 human lease 的 short-circuit（預設）
- **決定**：刪除 session_manager.py:1666–1675 的早期 return；human lease 不再阻斷 self_test 流程。
- **理由**：與 command path 行為對齊；現有 short-circuit 給出與真實能力不符的訊號。
- **替代方案**：保留 short-circuit、新增 `HUMAN_MONITORED_OK` 子分類，由 self_test 內部判斷「human 沒有 active typing」才解鎖。被否決——需要追蹤 last input ts，邊界（多久才算 idle）難以客觀定義，且增加狀態管理複雜度，效益低於直接走完整 probe。

### D2：以 `strict_human_lock: bool = False` 提供 opt-in 舊行為
- **決定**：`SessionManager.self_test(selector, *, timeout_s=2.0, strict_human_lock=False)`；RPC `session.self_test` 從 `params.get("strict_human_lock", False)` 讀；CLI 加 `--strict-human-lock`。
- **理由**：少數情境（例如 firmware flash 進行中、認證模擬）人類確實不希望任何 agent 介入；保留語意明確的 opt-in，比 caller 自行從 `human_attached=True` 推導再放棄更安全。
- **替代方案**：完全移除 `HUMAN_INTERACTIVE_ACTIVE` classification。被否決——過度激進且失去 strict 場景的明確訊號。

### D3：`interactive_owner` / `human_attached` 加在所有 result branch
- **決定**：每個 self_test return 都帶上 `interactive_owner: str | None`（lease 存在時為 owner 字串，否則 `None`）與 `human_attached: bool`（owner 是否以 `human:` 開頭）。
- **理由**：避免「`OK` 才有完整資訊、其他 classification 缺欄位」的不一致；caller 不必為每個 classification 寫不同處理。
- **實作**：在 `self_test` 函式入口決定 lease snapshot 後，將兩個值統一塞進每個 return dict（或在最終 helper 包一層）。

### D4：probe 階段以 suspend/resume 包覆
- **決定**：當 `lease and lease.owner.startswith("human:")` 為真，在 `bridge.send_command(probe, source="system:self_test", ...)` 與隨後的 `bridge.wait_for_regex_from(...)` 周圍呼叫 `bridge.suspend_interactive()` / `resume_interactive()`，包在 try/finally。suspend/resume 必須在 `self._lock` 之外呼叫（與 `_execute_command` 同模式）。
- **理由**：probe 字元（如 `echo nonce-XXXXXXXX`）與 human 即時輸入若同時寫到 serial fd，會交錯影響 prompt 解析與顯示。command path 既有 pattern 是這個問題的成熟解。
- **僅在 probe 路徑**：非 probe 的早期 return（`DEVICE_MISSING` / `BRIDGE_DOWN` / `VTTY_STALE` / `ATTACHED_*`）不會寫 UART，所以不需要 suspend。
- **僅 human owner 觸發**：`agent` / `agent:xxx` lease 與 self_test 同陣營，由上層協調，不額外 suspend。

### D5：spec / docs 同步更新但不引入新 capability
- **決定**：以 modified capability `session-selftest` 表達；`docs/serialwrap-spec.md` §9.1 更新分類列表、欄位 schema、新增「Collaborative monitoring」段；不另開 README。
- **理由**：行為層級調整，無新功能面，重在訊號正確性與 caller 契約。

## Risks / Trade-offs

- **[Risk] 既有 caller 仍依賴 `HUMAN_INTERACTIVE_ACTIVE` 為 ready signal** → Mitigation：grep 確認 docs / openspec / func-test / tests 內無外部 hard-code（已確認），並在 spec / proposal 標明預設行為改變、提供 `--strict-human-lock` 還原。

- **[Risk] human typing 過程中 self_test probe 進來，suspend 期間 typing 累積到 deferred buffer，resume 時 flush 出去產生延遲回顯** → Mitigation：suspend/resume 是既有機制，command path 早已生產驗證；self_test probe 持續時間 ≤ `timeout_s`（預設 2s）短於既有 command 的 timeout，使用者體感與既有相似。

- **[Risk] 移除 short-circuit 後，self_test 在 human attach 時會真的對 UART 寫一個 probe（如 `echo nonce`），可能干擾人類正在閱讀的輸出** → Mitigation：probe 本來就只在 `READY` 且 bridge alive 的最後階段送出，並會被 `suspend_interactive` 覆蓋；caller 若要避免寫 probe，可走 `strict_human_lock=True` 取得早期 return。

- **[Trade-off] 雙模式（default / strict）增加 caller 認知成本** → 接受：兩種 mode 語意明確且互不重疊；strict 預期使用率低、文件足以說明。

- **[Risk] suspend/resume 與 self._lock 順序錯誤導致死鎖或 race** → Mitigation：嚴格按 `_execute_command` 既有 pattern——所有 lease snapshot / decision 在 lock 內完成、`bridge` 與 `prompt_regex` 引用先取出 lock 外、suspend/resume 只在 lock 外呼叫。
