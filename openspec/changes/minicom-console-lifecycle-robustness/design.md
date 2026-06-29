## Context

**現況（已查證 file:line）：**

- raw/line 分派 `_handle_console_rx`（`uart_io.py:573-612`）：owner==human:cid → raw 透傳 `send_bytes`（`:583-588`）；agent_active and suspended==cid → deferred（`:590-601`）；其餘 line-buffer `_consume_console_input`（`:603-612`）。決定走哪條的唯一條件是 `_interactive_owner`。
- 授予鏈：`attach_console`（`session_manager.py:2476-2505`）→ `_refresh_interactive_locked`（`:2493`）→ 授予閘 `if state∈{ATTACHED,READY} and lease is None`（`:2494`）→ `_open_interactive_locked`（`:1864`，內呼 `bridge.set_interactive_owner`，`:1886`）。
- `_refresh_interactive_locked`（`:1945-1976`）human 分支：`not console_has_external_peer(cid)` 或 snapshot owner 不一致 → **無 grace 立即** detach+close → owner 清 None（`:1959-1969`）。
- stale 回收 `_prune_stale_consoles_locked`（`uart_io.py:442-458`）：跳過 primary（`:446`）、未過 `_STALE_CONSOLE_GRACE_S=2.0`（attached_at-based）、仍有 peer 者保留；**只被 `attach_console`（`:849`）與 `list_consoles`（`:884`）順手呼叫，無背景週期**。它移除 client 時**只清 `_interactive_owner`**（`:455-456`），不碰 `_suspended_owner/_agent_active/_suspend_depth/_deferred_buffers`（對照完整清理在 `detach_console` `:869-877`）。
- 週期 tick：`DeviceWatcher._loop`（`device_watcher.py:78-87`，1.0s）→ `reconcile_readiness`（`session_manager.py:792-816`，只重探非 READY session，**不碰 console**）。
- `_client_has_external_peer_locked`（`uart_io.py:398-428`）掃 `/proc/*/fd`；procfs 不可用保守回 True（`:407`）；sock(TCP) client 恆 True。
- `minicom_router.sh`：`trap cleanup EXIT INT TERM`（`:355`，cleanup 呼 `console-detach || true`）+ `:362-363` 顯式 cleanup。**缺 HUP**。
- #78（`32b2a95`）已落地：`_suspend_depth`（`uart_io.py:138`）、可重入 suspend/resume。
- TCP 路徑（`_accept_console_conn` `:344-345`）對首個連線於 bridge 層自動授予 ownership——但那是 Windows-only 對照範本，**POSIX 不可照抄成 lease-less**。

**活機坐實（2026-06-29）**：COM0/COM1 `interactive_session_id:null`、`console_count:2`＝真 minicom（pts/15、pts/17，2 fd）+ 死 primary（pts/12、pts/14，僅 daemon slave 1 fd、無外部 reader、因 primary-skip 卡死）。

## Goals / Non-Goals

**Goals:**
- 連入的 minicom **穩定取得 raw ownership**；owner 因故掉失時能**自癒重授**（lease-backed）。
- 孤兒/stale console（含死 primary）被**週期主動回收**，杜絕 RX fan-out 漸進變慢（#76）。
- 全程不重引入 #78（suspend 簿記）、#81（deferred 污染）、#83（use-after-close）。

**Non-Goals:**
- 不動序列埠 I/O 與命令仲裁。
- 不處理 capture 模式（PR-A）。
- 不追求「活著但被遺忘的 second minicom」之自動奪權；第二個**活** console 不得奪 owner（維持 `test_second_console_does_not_get_interactive`）。

## Decisions

### Fix2 孤兒回收
1. **`minicom_router.sh:355`**：`trap cleanup EXIT INT TERM` → `… HUP`；`:362` 的 `trap -` 同步含 HUP。cleanup 已 idempotent。SIGKILL 不可 trap，由 broker 週期回收兜底。
2. **`UARTBridge.reap_stale_consoles()`（新 public 方法）**：lock-split——(a) 鎖內快照候選 `(client_id, slave_path, attached_at, internal)`；(b) **鎖外掃一次 /proc** 建「被外部持有的 slave_path 集合」（O(pids×fds) 單次，非每 client 一次）；(c) 回鎖對「確認無 peer 且過 grace、且**非** `_interactive_owner`／**非** `_suspended_owner`／**非** internal 哨兵」者 pop；(d) 鎖外 close fd。維持 `pop-in-lock / close-out-of-lock`（防 #83 RACE-1）。
3. **primary 可回收**：`ConsoleClient` 加 `internal: bool=False`；start() 哨兵 primary 設 `internal=True`（永不 reap，保 `snapshot.vtty_alive`）。非 internal 但剛好是 `_primary_client_id` 的真實 console 若無 peer 且過 grace **可** reap，reap 後比照 `_drop_console_client` 重指 `_primary_client_id`。
4. **週期化**：`reconcile_readiness`（`session_manager.py:792-816`）末尾對每個 `bridge is not None` 的 session 呼 `reap_stale_consoles()`。**鎖紀律**：/proc 掃描須在 session_manager `_lock` 與 bridge `_state_lock` **之外**（避免每 1.0s 阻塞 RX fan-out，否則反引回 #76）。可做單次共享 /proc 掃描分給各 bridge（O(1)）+ 節流（`last_reap_at`）。

### Fix3 raw ownership robust
5. **peer-loss grace**：`InteractiveLease` 加可變欄位 `peer_lost_at: float|None=None`。`_refresh_interactive_locked` human 分支改為：peer 在 → `peer_lost_at=None`；peer 不在 → 若 `peer_lost_at is None` 設為 now 並**暫不拆、回傳 lease**；僅 `now-peer_lost_at > _HUMAN_PEER_GRACE_S`（`constants.py`，2.0~3.0s）才 detach+close。消除觸發B（flap）。grace **只**套用於被動拆當前 owner，不套用於新 attach 接管。
6. **bridge 原子鎖內快照 + 條件式 grant（Codex 對抗審查 finding-2）**：self-heal／reaper **不得**裸讀 bridge 私有欄位（`_interactive_owner`/`_suspended_owner`/`_agent_active`/`_flash_mode`/`_primary_client_id`——皆 `_state_lock` 保護，跨層裸讀會與 `suspend_interactive`/flash 競態）。改為：
   - (a) bridge 提供 public 原子鎖內快照（擴充既有 `snapshot()` 補 `agent_active`/`suspended_owner`/`flash_mode`/`primary_client_id`，或新增專用 accessor），單次 `_state_lock` 回傳全部決策欄位；
   - (b) peer-liveness（`console_has_external_peer`，掃 `/proc`）在**兩鎖之外**計算，與 reaper 共用同一次 `/proc` 掃描；
   - (c) bridge 提供**原子條件式 grant** primitive（單次 `_state_lock`：僅當 `_interactive_owner is None and _suspended_owner is None and not _agent_active and not _flash_mode` 才設 owner、回成功，否則失敗不變更）——消除「讀到陳舊 idle 快照→期間 agent suspend/flash→仍誤授」的 TOCTOU。
7. **lease-backed 週期自癒**：`reconcile_readiness` tick（與 reaper 同 tick）對每個 bridge：依 (6) 的原子快照判定 idle（無 owner/無 suspended owner/非 agent/非 flash）、state∈{ATTACHED,READY}、有活 primary console（peer-liveness 鎖外判定）、`session.interactive_session_id` 解析為 None → 透過 (6c) 原子條件式 grant 設 owner 並**同時**開對應 lease（owner+lease 成對）。grant 失敗（期間狀態已變）則本 tick 不開 lease、留待下次。修觸發C 善後與任何 owner 掉失。
8. **不做 bridge 層 lease-less 自癒**：明文禁止在 `_handle_console_rx` 直接設 `_interactive_owner` 而無對應 lease（會與 session_manager lease 脫鉤 → 下個 agent 命令依「無 lease」漏 suspend → human 鍵入 raw 汙染命令＝#78/#81 回歸）。
9. **鎖序（finding-2）**：維持既有不變式 `SessionManager._lock ⊃ UARTBridge._state_lock`（外 SM、內 bridge，絕不反向；對齊既有 `_write_lock ⊃ _state_lock`）；self-heal／reaper **不得**在持 `SessionManager._lock` 期間做 `/proc` 掃描或阻塞 I/O。

## Risks / Trade-offs

- **reaper 重引入 #78**：若 reap 一個正被 suspend 的 owner（agent 命令期間 `_interactive_owner=None`、`_suspended_owner=cid`），會破壞 suspend 簿記。**緩解**：reaper 硬性跳過 `_interactive_owner` **與** `_suspended_owner` 衍生的 cid；被 reap 的其他 client 一律 `_deferred_buffers.pop`。
- **reaper vs grace 互踩**：reaper 的 attached_at-grace 對長存 owner≈0 peer-grace，可能繞過 Fix3 的 peer-loss grace。**緩解**：reaper 不碰當前 owner（其拆除全權交 `_refresh_interactive_locked` 的 grace）。職責切分：reaper 只清非-owner 孤兒。
- **lease-backed 自癒誤奪權 / stale-read race（Codex finding-2）**：若 self-heal 以「先讀私有狀態判 idle、稍後才 grant」的非原子方式進行，讀到陳舊 idle 快照後 agent suspend／flash 介入，會在 agent 命令或 flash 窗誤授 ownership → 正是本變更要避免的命令/flash byte 污染。**緩解**：(1) 守衛欄位一律經 bridge 原子鎖內快照取得、不裸讀；(2) 實際授予走 bridge **原子條件式 grant**（check-and-set 同一 `_state_lock` critical section），期間狀態已變則 grant 失敗、不誤授。需測「agent 進行中不自癒」「快照→grant 間 agent 介入時 grant 失敗」「第二活 console 不奪權」。
- **/proc 成本**：週期高頻掃描，多 session 時線性放大 → 單次共享掃描 + 節流。procfs 抖動時保守回 True（不誤剪活 console）。
- **鎖序（finding-2）**：維持單向鎖序 `SessionManager._lock ⊃ UARTBridge._state_lock`（對齊既有 `_write_lock ⊃ _state_lock`）；reconcile tick **不得**在持 `SessionManager._lock` 期間做 `/proc` 掃描或阻塞 I/O；bridge 狀態一律經短暫 `_state_lock` 原子快照/條件式 grant 取得，不在 SM 層裸讀私有欄位。
- **HUP idempotency**：`:355` 加 HUP 與 `:362-363` 顯式 cleanup 雙呼無害（`|| true`）；`:362` 的 `trap -` 需同步含 HUP 避免殘留。
