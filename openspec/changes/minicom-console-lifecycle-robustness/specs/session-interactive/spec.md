## MODIFIED Requirements

### Requirement: dead orphan console SHALL be detached via liveness

`SessionManager` SHALL 在 self-test 與 agent 取得控制權之前，沿用既有 `console_has_external_peer` liveness 檢查：當 human lease 的 console peer 關閉並**持續超過 `_HUMAN_PEER_GRACE_S`**（藉 grace 窗確認 process 已結束、非瞬時 flap）時 SHALL detach 該 console 並關閉 lease。**首次**觀測到 peer 消失時 SHALL 只記錄 `peer_lost_at`、SHALL NOT 立即 detach 或關閉 lease（拆除一律走下方「human lease 被動拆除 SHALL 套用 peer-loss grace」requirement 的統一路徑，使所有觸發點——self-test、agent 取得控制權、週期 tick——對「當前 owner」的拆除行為一致，杜絕第一次 peer-check False 即拆所重新引入的 flap 回歸）。系統 SHALL NOT 對 alive-but-idle 的 console 做自動 detach（該情形以 soft preempt 處理；真正清理交由 agent 主動 `recover`/`console-detach`）。

此外，孤兒 console 的回收 SHALL NOT 僅依賴 RPC 觸發：daemon SHALL 在週期 readiness tick（`reconcile_readiness`）中對每個有 bridge 的 session 主動回收**非當前 interactive owner、非 suspended owner、且無外部 reader（peer 已關）並過 grace** 的 console，使穩態下（無 RPC 進來）孤兒不會無界累積而拖慢 RX fan-out。當前 interactive owner 的拆除 SHALL 交由 `_refresh_interactive_locked` 的 peer-loss grace 處理（見對應 requirement），週期回收 SHALL NOT 觸碰當前 owner 與 suspended owner 的 console。

#### Scenario: dead console peer is detached after grace

- **WHEN** human lease 存在，其 console peer 關閉（`console_has_external_peer` 為 False）並**持續超過 `_HUMAN_PEER_GRACE_S`**
- **THEN** 該 console 被 detach、lease 關閉、`interactive_owner` 清為 null

#### Scenario: first peer-loss observation keeps lease (no immediate detach)

- **WHEN** human lease 存在，其 console peer **首次**被觀測為 False（尚未超過 `_HUMAN_PEER_GRACE_S`）
- **THEN** SHALL 只記錄 `peer_lost_at`、SHALL NOT detach、SHALL NOT 關閉 lease、`interactive_owner` 維持不變；若 peer 在 grace 窗內回復 True 則 SHALL 清除 `peer_lost_at`

#### Scenario: alive idle console is not auto-detached

- **WHEN** human lease 存在、peer 仍開著、但已閒置超過視窗
- **THEN** 該 console SHALL NOT 被自動 detach（僅可被 soft preempt 降級）

#### Scenario: 週期回收清除非 owner 孤兒 console

- **WHEN** 一個非 interactive owner 的 console 其外部 reader 已關（`console_has_external_peer` 為 False）且過 grace、無任何 RPC 進來
- **THEN** 在下一次 readiness tick SHALL 被週期回收（從 `_clients` 移除並關 fd），`console_count` 隨之下降

#### Scenario: 週期回收不觸碰 suspended owner

- **WHEN** agent 命令進行中（`_agent_active` 為真、`_suspended_owner` 指向某 human console），該 human console 短暫無 peer
- **THEN** 週期回收 SHALL NOT 移除該 suspended owner console，suspend/resume 簿記（`_suspend_depth`/`_deferred_buffers`）SHALL 維持完整、resume 後 owner 正常還原

## ADDED Requirements

### Requirement: 死掉的 primary console SHALL 可被回收並重指

start() 建立的 broker 內部哨兵 primary console（無外部 reader、僅作 `snapshot.vtty` 錨點）SHALL 以 `internal` 旗標標記且**永不**被回收。但一個真實 human console 在成為 `_primary_client_id` 後若其外部 reader 已關（peer 為 False）且過 grace，SHALL 可被回收；回收後 daemon SHALL 將 `_primary_client_id` 重指到其餘 console（若有），否則重指回 internal 哨兵或 None。

#### Scenario: internal 哨兵 primary 永不回收

- **WHEN** 週期回收掃描，且 `_primary_client_id` 指向 `internal=True` 的哨兵
- **THEN** 該哨兵 SHALL NOT 被回收，`snapshot.vtty_alive` 維持有效

#### Scenario: 死掉的真實 primary 被回收並重指

- **WHEN** 一個 `internal=False` 的真實 console 為 `_primary_client_id`，其外部 reader 已關、過 grace
- **THEN** 該 console SHALL 被回收，`_primary_client_id` SHALL 重指到其餘 console（或 None）

### Requirement: human lease 被動拆除 SHALL 套用 peer-loss grace

`_refresh_interactive_locked` 對當前 human lease 的拆除 SHALL NOT 在 `console_has_external_peer` 一次回 False 即立刻執行；daemon SHALL 記錄首次觀測到 peer 消失的時間（`InteractiveLease.peer_lost_at`），僅當持續無 peer 超過 `_HUMAN_PEER_GRACE_S`（daemon 常數，建議 2.0~3.0s）才 detach + 關閉 lease。peer 在 grace 窗內回復時 SHALL 清除 `peer_lost_at` 並保留 lease。此 grace 僅套用於「被動拆除當前 owner」，不套用於新 attach 的接管判定。

#### Scenario: 瞬時 peer flap 不拆 lease

- **WHEN** 當前 human owner 的 `console_has_external_peer` 瞬時回 False，但在 `_HUMAN_PEER_GRACE_S` 內回復 True
- **THEN** lease SHALL 維持、`interactive_owner` 不變、`peer_lost_at` 被清回 None

#### Scenario: 持續無 peer 超過 grace 才拆

- **WHEN** 當前 human owner 的 peer 持續 False 超過 `_HUMAN_PEER_GRACE_S`
- **THEN** 該 console SHALL 被 detach、lease 關閉、`interactive_owner` 清 null

### Requirement: bridge SHALL 提供原子鎖內快照與條件式 grant 供 ownership 決策

`SessionManager` 的週期 self-heal 與 reaper SHALL NOT 直接讀取 `UARTBridge` 的私有可變欄位（`_interactive_owner`／`_suspended_owner`／`_agent_active`／`_flash_mode`／`_primary_client_id`）——那些受 `UARTBridge._state_lock` 保護，跨層裸讀會與 `suspend_interactive`／flash 轉換競態、或誘使在持 `SessionManager._lock` 時取 bridge 鎖。

bridge SHALL 提供一個 public 存取面，於**單次 `_state_lock` critical section** 內原子回傳決策所需欄位：`interactive_owner`、`suspended_owner`、`agent_active`、`flash_mode`、`primary_client_id`（可擴充既有 `snapshot()` 或新增專用 accessor）。peer-liveness（`console_has_external_peer`，需掃 `/proc`）SHALL 在**兩個鎖之外**單獨計算（與 reaper 共用同一次 `/proc` 掃描），SHALL NOT 在持 `_state_lock` 或 `SessionManager._lock` 時執行。

bridge SHALL 另提供一個**原子條件式 grant** primitive：於單次 `_state_lock` 內，**僅當** `_interactive_owner is None` 且 `_suspended_owner is None` 且非 `_agent_active` 且非 `_flash_mode` 時，才將 `_interactive_owner` 設為指定 owner 並回傳成功；否則回傳失敗且不變更狀態。此 check-and-set 原子性 SHALL 消除「讀到陳舊 idle 快照 → 期間 agent suspend／flash 介入 → 仍誤授 ownership」的 TOCTOU。

**鎖序** SHALL 維持既有不變式 `SessionManager._lock ⊃ UARTBridge._state_lock`（外層 SM、內層 bridge，絕不反向）；self-heal／reaper SHALL NOT 在持 `SessionManager._lock` 期間執行 `/proc` 掃描或任何阻塞 I/O。

#### Scenario: 決策不裸讀 bridge 私有欄位

- **WHEN** self-heal 或 reaper 需判斷 bridge ownership/agent/flash 狀態
- **THEN** SHALL 透過 bridge 的原子鎖內快照取得，SHALL NOT 直接讀取 bridge 私有屬性

#### Scenario: 條件式 grant 在 agent 中途介入時失敗而非誤授

- **WHEN** self-heal 讀到「idle」快照後、實際 grant 之前，agent 命令路徑已 `suspend_interactive()`（`_agent_active` 轉真）
- **THEN** 原子條件式 grant SHALL 偵測到非 idle 而**失敗**、不設 owner；human 鍵入維持 deferred、不污染 agent 命令

### Requirement: raw ownership SHALL 由 lease-backed 週期自癒重授

當 bridge 處於「無 owner」idle 狀態（由上述原子鎖內快照判定：`interactive_owner` 為 None、`suspended_owner` 為 None、非 `agent_active`、非 `flash_mode`）、session 處於 READY 或 ATTACHED、存在一個有外部 reader 的活 primary console（peer-liveness 於鎖外判定）、且無既有 interactive lease 時，daemon SHALL 在週期 readiness tick 透過**原子條件式 grant** 重新授予該 console raw ownership，並**同時**建立對應 session-layer lease（ownership 與 lease 永遠成對、不脫鉤）。daemon SHALL NOT 在 bridge 層設定 `_interactive_owner` 而無對應 lease（ownership 必須永遠由 lease 背書，以確保 agent 命令路徑能正確 suspend、不致 human 鍵入污染命令）。若原子條件式 grant 失敗（期間狀態已變），本次 tick SHALL NOT 建立 lease，留待下次 tick 重評。

#### Scenario: owner 掉失後自癒重授

- **WHEN** session READY、原子快照顯示無 owner/無 suspended owner/非 agent/非 flash、無 lease、有活 primary console
- **THEN** 下一次 readiness tick SHALL 透過原子條件式 grant 設 bridge owner 並開出對應 `human:<primary_cid>` lease，使該 console 後續鍵入走 raw 透傳

#### Scenario: agent 進行中不自癒奪權

- **WHEN** `agent_active` 為真（agent 命令進行中、human 在 deferred 模式）
- **THEN** 自癒 SHALL NOT 觸發；即使快照與實際 grant 間有競態，原子條件式 grant 亦 SHALL 失敗，ownership 不得被重授給打字的 human（避免污染 agent 命令）

#### Scenario: 第二個活 console 不奪 owner

- **WHEN** 已有一個持 lease 的活 human owner，第二個活 console 連入
- **THEN** 自癒/接管 SHALL NOT 把 ownership 從第一個活 owner 奪走（原子條件式 grant 因 `interactive_owner` 非 None 而失敗；維持既有「第二 console 不奪權」契約）

### Requirement: console-attach 連入 client SHALL 取得 raw ownership

在 READY/ATTACHED 下透過 `console-attach` 連入的 human console，經授予流程後其鍵入 SHALL 走 raw 透傳（`\x1b[A`/`\t` 等 SHALL 原樣送達 UART，而非落入 line-buffer）。

> 註：關終端（SIGHUP）的 console-detach 不在本變更範圍——真機驗證（真 minicom + 真 tmux pane kill＝process-group SIGHUP）證明既有 `minicom_router.sh` 於前景 minicom 被 HUP 殺死後即跑到顯式 cleanup → `console-detach`，不留孤兒。真正需處理的 orphan 來源是 SIGKILL/crash（跑不到 cleanup），由「dead orphan console 經 liveness 週期回收」requirement 涵蓋。

#### Scenario: attach 後方向鍵/Tab 原樣到 UART

- **WHEN** human 經 console-attach 連入一個 READY session（無其他活 owner）
- **THEN** 該 client SHALL 取得 `interactive_owner`，其送出的 `\x1b[A`/`\t` SHALL 原樣寫入 UART（非經 line-buffer 行編輯）
