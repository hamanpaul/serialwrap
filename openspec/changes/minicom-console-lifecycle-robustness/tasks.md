## 1. Fix2 — minicom wrapper SIGHUP detach

- [ ] 1.1 `sw_core/assets/tools/minicom_router.sh:355`：`trap cleanup EXIT INT TERM` → `trap cleanup EXIT INT TERM HUP`；`:362` 的 `trap - EXIT INT TERM` 同步加 `HUP`。
- [ ] 1.2 `tests/test_minicom_router.py`：新增 `test_broker_console_detaches_on_sighup`——fake minicom 睡較久，啟動 router 子行程後送 SIGHUP，斷言 `session console-detach` 被呼叫。

## 2. Fix2 — broker 端孤兒回收（reap_stale_consoles）

- [ ] 2.1 `sw_core/uart_io.py`：`ConsoleClient` 加 `internal: bool = False`；`start()` 建的哨兵 primary 設 `internal=True`。
- [ ] 2.2 `sw_core/uart_io.py`：新增 `reap_stale_consoles()`——lock-split（鎖內快照 → 鎖外掃一次 /proc → 回鎖 pop → 鎖外 close fd，沿用 pop-in-lock/close-out-of-lock 防 #83）。**硬性跳過** `_interactive_owner` 與 `_suspended_owner` 衍生 cid、`internal=True` 哨兵；被 reap 的 client 一律 `_deferred_buffers.pop`。
- [ ] 2.3 `sw_core/uart_io.py`：死掉的非-internal primary（無 peer、過 grace）可被 reap，reap 後比照 `_drop_console_client` 重指 `_primary_client_id`。
- [ ] 2.4 `sw_core/session_manager.py:792-816`：`reconcile_readiness` tick 末尾對每個 `bridge is not None` 的 session 呼 `reap_stale_consoles()`；/proc 掃描在 session `_lock` 與 bridge `_state_lock` 之外（單次共享掃描分給各 bridge + `last_reap_at` 節流）。
- [ ] 2.5 `tests/test_uart_io.py`：`test_reap_stale_consoles_drops_orphan_non_primary`、`test_reap_drops_dead_non_sentinel_primary_and_promotes`、`test_reap_skips_owner_and_suspended_owner`（含 reap-during-suspend → resume 不還原 phantom owner）、`test_reap_proc_scan_outside_state_lock`。
- [ ] 2.6 refine `tests/test_uart_io.py::test_list_consoles_never_prunes_primary_client`：明確只對 `internal=True` 哨兵斷言永不 reap。

## 3. Fix3 — peer-loss grace

- [ ] 3.1 `sw_core/session_manager.py`：`InteractiveLease` 加可變欄位 `peer_lost_at: float | None = None`。
- [ ] 3.2 `sw_core/constants.py`：新增 `_HUMAN_PEER_GRACE_S`（2.0~3.0s，量級對齊 `_STALE_CONSOLE_GRACE_S`）。
- [ ] 3.3 `sw_core/session_manager.py:1959-1969`：human 分支改 grace 版（peer 在→清 `peer_lost_at`；peer 不在→首次設 now 暫不拆；超過 grace 才 detach+close）。grace 只套被動拆當前 owner。
- [ ] 3.4 `tests/test_interactive_raw.py`（或 session 測試）：`test_peer_flap_within_grace_keeps_lease`、`test_peer_gone_past_grace_tears_down`。

## 4. Fix3 — bridge 原子存取面 + lease-backed 週期自癒（含 Codex finding-2）

- [ ] 4.1 `sw_core/uart_io.py`：擴充 `snapshot()`（或新增專用 accessor）於**單次 `_state_lock`** 原子補回傳 `agent_active`/`suspended_owner`/`flash_mode`/`primary_client_id`（既有已含 `interactive_owner`/`vtty`/`last_human_input_at`）。self-heal／reaper 一律經此取得 bridge 狀態，**不裸讀私有欄位**。
- [ ] 4.2 `sw_core/uart_io.py`：新增**原子條件式 grant** primitive（如 `try_grant_interactive_if_idle(owner) -> bool`）——單次 `_state_lock`，**僅當** `_interactive_owner is None and _suspended_owner is None and not _agent_active and not _flash_mode` 才設 owner 並回 `True`，否則回 `False` 不變更狀態（消除 snapshot→grant 之間的 TOCTOU）。
- [ ] 4.3 `sw_core/session_manager.py:792-816`：`reconcile_readiness` tick（與 reaper 同 tick）：以 4.1 原子快照判 idle（無 owner/無 suspended owner/非 agent/非 flash）+ state∈{ATTACHED,READY} + peer-liveness（**鎖外**、與 reaper 共用同一次 /proc 掃描）+ 無 lease → 透過 4.2 原子條件式 grant 重授並**同時**開對應 lease；grant 失敗則本 tick 不開 lease。**鎖序** `SessionManager._lock ⊃ UARTBridge._state_lock`，且**不得**在持 `SessionManager._lock` 時做 /proc。
- [ ] 4.4 **不**新增 bridge 層 lease-less 自癒（明文約束，防 #78/#81 deferred-input 污染）。
- [ ] 4.5 `tests/test_interactive_raw.py`：真 PTY `UARTBridge.start()+attach_console()`（不手動 set_interactive_owner）→ 斷言連入 client 取得 owner、寫 `\x1b[A`/`\t` 原樣到 UART；`test_self_heal_regrants_after_owner_loss`；`test_self_heal_skips_during_agent_active`（#78 守衛）；`test_grant_fails_when_agent_intervenes_between_snapshot_and_grant`（TOCTOU——快照判 idle 後、grant 前以 `suspend_interactive()` 介入，斷言 grant 回 False、owner 不被設、不開 lease）；回歸 `test_second_console_does_not_get_interactive` 續綠。
- [ ] 4.6 `tests/test_suspend_resume_reentrant.py` 全數續綠（#78 不回歸）。

## 5. 整合驗證與 policy

- [ ] 5.1 `python3 -m pytest -q tests/` SHALL 通過，**唯一**可容忍的 pre-existing 失敗為 CLAUDE.md 載明的 `tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（agent TX count mismatch）。**任何其他**失敗（含 PTY/coexist）SHALL NOT 以「flaky」一語帶過——必須先在 base commit（未含本變更）以**完全相同的指令與環境**重現該確切 test id 並附證據，才可判定為 pre-existing；否則一律視為本變更引入的回歸、須修正。（本變更觸及 PTY/stale console/suspend-resume 競態，寬鬆例外會讓真回歸被當成可容忍失敗放行。）
- [ ] 5.2 真機（COM0/COM1）回歸：console-attach 後 `console-list` 顯示連入 client `interactive_owner:true`；不乾淨關 minicom 後孤兒於下一 tick 被回收（`console_count` 回落）；agent 命令期間 human 不奪權。
- [ ] 5.3 `python3 -m policy_check --repo .` 通過；docs（README/spec 若涉及 console 生命週期契約）對齊（R-18）。
- [ ] 5.4 分支 `feature/<slug>`、PR body Policy Checklist、commit 帶 Co-authored-by trailer；PR body 註明硬相依（症狀1 主因觸發A 由本 change 修）。
