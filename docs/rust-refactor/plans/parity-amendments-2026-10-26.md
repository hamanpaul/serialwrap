# 1:1 parity 稽核結果 → 計畫修訂（2026-10-26）

- 日期：2026-10-26（排程例行工作；純文件／計畫修訂，未改程式碼、測試、contracts 或 lock 檔）
- 基準：`origin/feature/rust-refactor`（分支 `docs/parity-amendments`）；唯讀參照 `origin/feature/rust-refactor-p5`、`-p6`、`-p7`。
- 輸入：稽核 A `origin/audit/parity-a:docs/rust-refactor/reviews/parity-audit-a-external.md`（504 項；GAP 5、DIVERGENT 16）；稽核 B `origin/audit/parity-b:docs/rust-refactor/reviews/parity-audit-b-daemon.md`（725 項；GAP 36、DIVERGENT 64 其中非刻意 22、未測 94＋部分／附註列）；P6 審查 `origin/review/p6-t0-t5:docs/rust-refactor/reviews/p6-t0-t5-sonnet-review.md`（Medium 3、Low 5、偏離 REJECT 1）。
- 約束：規格定稿、`owner-decisions-2026-10-15.md`（OD-14～OD-20）、`plans/spec-reconciliation.md`、`contracts/behavior_changes.yaml`、計畫 00、06～10。
- 擁有者原則（OD-14 #1，本次例行工作重申）：**外部可觀察行為（CLI 指令格式與輸出、RPC 方法、回應欄位與 `error_code`、檔案格式）必須與 Python 1:1，只可新增、不可修改；內部實作可以調整。** 注意：OD-14 原表備註寫「狀態與回應欄位內容可調整」，本次重申的原則較嚴；本文件一律依較嚴原則判定，兩者落差對既有 Rust-only SC 的影響列為 PA-OD-1。

## 判定類別

| 類別 | 意義 | 計畫上的處理 |
|---|---|---|
| ALIGN-TO-PYTHON | 改回與 Python 一致 | 直接改寫衝突的計畫文字／程式碼區塊／測試列，並加回歸測試（名稱＋斷言）；已 commit 的偏差列入「已實作追加修正」 |
| ADD-TO-PLAN | 計畫缺承接 | 在最合適的 Task 末尾「parity-amendments-2026-10-26 追加」段加測試列與完成條件 |
| REGISTER-IN-LOCK | 既有 Python（或已實作 Rust）測試未入凍結鎖 | 指定集合與負責 Task，以 `regen_lock.py --l1-add-only` 登記（本次不改 lock 檔） |
| RECORD-SC | 刻意但沒有編號的差異 | 補登於 `spec-reconciliation.md` §7（SC 候選或 NC） |
| OWNER-DECISION | OD-14 #1 與既有裁決都無法決定 | 列於本文件末，附選項與建議 |

每個修改處都標註「（parity-amendments-2026-10-26 #<編號>）」；Task 末尾的追加段標題為「#### parity-amendments-2026-10-26 追加（Python 1:1 承接）」，表格第一欄即編號。下表「修改的計畫位置」由修訂後檔案機械抽出（`檔案代號 Task L行號`；代號 06＝`06-slice21-daemon-core.md`…，arch＝`rust-architecture.md`，recon＝`spec-reconciliation.md`）。#0 為 `00-master-plan.md` Global Constraints 的總約束（L34）。

## 統計

| 範圍 | 列數 | ALIGN-TO-PYTHON | ADD-TO-PLAN | REGISTER-IN-LOCK | RECORD-SC | OWNER-DECISION |
|---|---:|---:|---:|---:|---:|---:|
| §1 稽核 A＋稽核 B GAP／DIVERGENT／文件（#1～#75） | 75 | 24 | 39 | 0 | 11 | 1 |
| §2 稽核 B 未測列（主判定） | 99 | 5 | 46 | 40 | 6 | 2 |
| 合計（主判定；「A＋B」複合判定以第一個計） | 174 | 29 | 85 | 40 | 17 | 3 |

§2 與 §1 重疊者（同一行為同時是 GAP／DIVERGENT 與未測，如 B-06、C-10、E-109、F-S06）兩表各計一次、承接編號相同；去重後的承接單位即編號 #0～#139 與 #177～#188（#140～#176 未使用）。

另：P6 審查 Medium／Low 與偏離 (h) 共 10 項全數列為「已實作追加修正」（§3）；OWNER-DECISION 清單 3 項（§4，PA-OD-1～3，其中 #60 與 PA-OD-3 同案）。入鎖新集合 4 個（§2.1）。

## §1 總表：GAP、DIVERGENT 與文件不一致（#1～#75）

| # | 稽核來源 | 項目 | 判定 | 承接 Phase/Task | 修改的計畫位置 | 新增測試／入鎖要求 |
|---|---|---|---|---|---|---|
| 1 | A:G-1；B:F-S01（GAP） | `SERIALWRAP_SERIAL_BACKEND` 無讀取點，設定被靜默忽略 | ADD-TO-PLAN＋RECORD-SC | P5-T2、P5-T3 | 06 Task 2 L1121；06 Task 3 L1257；recon §7 L555 | `serial_backend_env_values`、`serial_backend_env_values_windows`；SC-38 候選；README 雙語同步 |
| 2 | A:G-2（GAP） | `SERIALWRAP_TEST_SYSTEM_CONFIG_PATH` 未承接，A/B 黑箱可能讀到真實 `/etc` 設定 | ALIGN-TO-PYTHON | P5-T0c（CLI 端已實作追加修正：feature/rust-refactor） | 06 Task 0c L582 | `sysenv_test_system_config_path_overrides`；runtime_paths golden 加 `system_config_path` 列 |
| 3 | A:G-3（GAP） | `SERIALWRAP_TEST_KILL_AFTER_SAVE` 無 Rust 承接 | ALIGN-TO-PYTHON | P5-T19 | 06 Task 19 L4497 | `release_kill_hook_env_isolated_only`（產品 build、僅隔離實例、commit 後 exit 137） |
| 4 | A:G-4（GAP） | `SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE` 無 Rust 承接 | ALIGN-TO-PYTHON | P5-T19 | 06 Task 19 L4497 | `release_pause_hook_env_isolated_only`（arrived／go 標記、50 ms 輪詢、600 s 上限） |
| 5 | A:G-5（GAP） | `SERIALWRAP_EVENTS_LOG_PATH` 無解析規則 | ADD-TO-PLAN | P7-T13、P7-T16 | 08 Task 13 L1878；08 Task 16 L2212 | `events_log_path_env_override`；`DaemonDirs.events_log_path`；runtime_paths golden add-only 加欄 |
| 6 | A:D-1；B:A-91（DIVERGENT 未登記） | daemon 端 backend env 強制非原生後端 → Rust exit 1 | RECORD-SC | P5-T19 | recon §7 L554 | spec-reconciliation §7.1 SC-37 候選；PA-OD-1 |
| 7 | A:D-16（DIVERGENT 建議補登） | Rust-only env 未列 `x-env` | RECORD-SC | P3／P4（已實作） | recon §7 L566 | spec-reconciliation §7.2 NC-12 |
| 8 | A:其他觀察 | `cli_schema.json` `stage` 歷史值；README 未列 6 個命令 | ADD-TO-PLAN | P8-T15（文件） | 09 Task 15 L2565 | README 中英補列；契約 x-note 由 P0 維護者 add-only |
| 9 | B:A-33（GAP） | 交接時 TX fence 在途 → fail-closed `SESSION_BUSY`＋`tx.in_flight` | ADD-TO-PLAN | P8-T14 | 09 Task 14 L2334 | `handoff_refused_when_tx_fence_in_flight` |
| 10 | B:A-37（GAP） | AdmissionGate 凍結擁有權規則無 Rust 測試 | ADD-TO-PLAN | P8-T14 | 09 Task 14 L2335 | `admission.rs` 4 案（對照 `tests/test_admission_thaw_owner.py`） |
| 11 | B:A-42≡F-S08（GAP） | `foreign_holders` 的 holder_unknown 物件值 | ADD-TO-PLAN | P5-T18 | 06 Task 18 L4263 | `health_status_foreign_holders_holder_unknown_object` |
| 12 | B:B-06（GAP、未測） | `aliases` 非 dict 時 Python 啟動失敗 | ALIGN-TO-PYTHON | P5-T15 | 06 Task 15 L3819 | `aliases_not_dict_fails_startup_like_python`＋Python 特性測試（s21_gate:py_parity） |
| 13 | B:B-12（GAP） | 啟動時一律重寫 state.json | ADD-TO-PLAN | P5-T15 | 06 Task 15 L3822 | `startup_always_rewrites_state_json` |
| 14 | B:B-16（GAP） | 動態 released 實體化 template 後備／act_no／profile_source | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3971 | `released_dynamic_materialize_*` 兩案 |
| 15 | B:B-17（GAP） | 實體化失敗保留持久化阻擋 | ADD-TO-PLAN | P5-T15 | 06 Task 15 L3823 | `released_materialize_failure_keeps_block` |
| 16 | B:B-20（GAP） | 交接窗口背景引擎零 TX | ADD-TO-PLAN | P8-T14 | 09 Task 14 L2336 | `handoff_window_background_zero_tx`（含 Rust L1.5 證據） |
| 17 | B:B-36（GAP、未測） | passthrough 重掛清前一 profile 的 quiet／reconfirm | ADD-TO-PLAN | P7-T4 | 08 Task 4 L789 | `passthrough_reattach_clears_previous_profile_quiet_and_reconfirm`；p1oc 入鎖（#77） |
| 18 | B:B-82（GAP、未測） | io_error＋pending_auto_login → RECOVERING、不 attach | ALIGN-TO-PYTHON | P5-T12、P7-T7 | 06 Task 12 L3146；08 Task 7 L1195 | `io_error_with_pending_auto_login_goes_recovering_not_attach`、`io_error_pending_auto_login_converges_via_reboot_recovery` |
| 19 | B:B-86（GAP、未測） | 遲到回條後 release 持久化失敗重試 | ADD-TO-PLAN | P5-T12（併 #23） | 06 Task 12 L3143；06 Task 15 L3824 | 見 #23 |
| 20 | B:B-91（GAP） | `com_for_by_id` API | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3972 | `com_for_by_id_session_first_then_pending`（或附證據記不移植） |
| 21 | B:C-09（GAP） | release 落在 attach op 飛行窗口 | ADD-TO-PLAN | P5-T12；黑箱 P8-T11 | 06 Task 12 L3141 | `release_during_pending_open_bridge_closes_late_handle` |
| 22 | B:C-10（GAP、未測） | 上項逾時 DRAIN_STALLED | ADD-TO-PLAN＋REGISTER-IN-LOCK | P5-T12；鎖 P5-T0b | 06 Task 12 L3142 | `release_during_pending_open_drain_stalled`；`test_p1oc_drain_stalled.py` 入 s21_gate:py_parity |
| 23 | B:C-11、B-85、B-86（GAP、未測） | RELEASED 持久化失敗重試／單一 persister | ADD-TO-PLAN | P5-T12、P5-T15 | 06 Task 12 L3143；06 Task 15 L3824 | `release_retry_persists_after_commit_failure` 等 3 案＋Python 特性測試 |
| 24 | B:C-28（GAP、未測） | 同 by_id device.attach 去重 | ADD-TO-PLAN | P5-T12 | 06 Task 12 L3144 | `device_attach_dedup_same_by_id_inflight` |
| 25 | B:C-30（GAP、未測） | device.attach 開檔前後重驗 | ADD-TO-PLAN | P5-T12 | 06 Task 12 L3145 | `stale_bridge_opened_after_rebind_closed`、`…real_path_change_closed` |
| 26 | B:C-34（GAP、未測） | bind 與在途 attach op 交互 | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3973 | `bind_with_inflight_attach_op_cancels_waits_3000ms` |
| 27 | B:C-53（GAP、未測） | hotplug 使 attach op 失效、未消費 port 關閉 | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3974 | `hotplug_invalidates_pending_attach_op_and_closes_unused_port` |
| 28 | B:C-54（GAP） | 離線 prune `_pending_com` | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3975 | `pending_rank_pruned_on_removal` |
| 29 | B:C-57（GAP） | 凍結早退時 ATTACHING 還原 DETACHED | ADD-TO-PLAN | P5-T16、P8-T14 | 06 Task 16 L3976；09 Task 14 L2337 | `begin_shutdown_blocks_hotplug_open` 加斷言；`handoff_freeze_restores_preattaching_to_detached` |
| 30 | B:C-62（GAP、未測） | `_finish_attach_op` close 失敗維持阻擋 | ADD-TO-PLAN | P5-T12（併 #21） | 06 Task 12 L3141 | 見 #21 |
| 31 | B:C-83（GAP） | `_rematerialize_profile_locked` | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3977 | `rematerialize_profile_recomputes_session_id_and_alias` |
| 32 | B:C-85（GAP） | #181 reattach 再解析 | ADD-TO-PLAN | P5-T16 | 06 Task 16 L3978 | `pin_applies_on_reattach` 等 4 案 |
| 33 | B:D-28（GAP、未測） | console_unlock reboot 前清 RX | ADD-TO-PLAN | P7-T7 | 08 Task 7 L1194 | `reboot_console_unlock_clears_rx_before_send` |
| 34 | B:D-54（GAP） | 救援鍵提交點被拒的回應碼 | ADD-TO-PLAN | P5-T14、P7-T9 | 06 Task 14 L3521；08 Task 9 L1444 | `recover_key_refused_frozen_maps_flushed_by_recovery` |
| 35 | B:D-71（GAP） | `interactive_open` 帶 command 的回覆時序與拒絕碼 | ADD-TO-PLAN | P6-T7 | 07 Task 7 L1868 | `open_with_command_reply_waits_tx_and_expired_maps` |
| 36 | B:D-74（GAP） | `interactive_send` 提交點拒絕碼 | ADD-TO-PLAN | P5-T14、P6-T7 | 06 Task 14 L3522；07 Task 7 L1869 | `lease_send_refused_at_commit_maps_codes` |
| 37 | B:E-18（GAP、未測） | console mux 錯誤退避 | ADD-TO-PLAN | P6-T9 | 07 Task 9 L2195 | `hub_mux_wait_error_backoff_20ms_then_rebuild` |
| 38 | B:E-65（GAP） | 被撤銷 op 集合上限 512 | ADD-TO-PLAN | P5-T9 | 06 Task 9 L2497 | `cancelled_ops_bounded_512_fifo`；resource_limits add-only（由該 Task 的契約 commit） |
| 39 | B:E-70（GAP，BC-03） | unknown outcome 確認前綴 TX WAL 與 META | ADD-TO-PLAN | P5-T9 | 06 Task 9 L2496 | `unknown_outcome_writes_confirmed_prefix_and_meta`、`unknown_without_confirmed_bytes_writes_meta_only` |
| 40 | B:E-109（GAP、未測） | console reaper holder inode 同一性 | ALIGN-TO-PYTHON | P6-T8（併 #64） | 07 Task 8 L2036 | `console_reaper_ignores_other_namespace_same_name_pts` 等 5 案 |
| 41 | B:E-115（GAP） | snapshot `serial_alive`／`running` 產生語意 | ADD-TO-PLAN | P5-T12、P6-T7 | 06 Task 12 L3149；07 Task 7 L1871 | `snapshot_serial_alive_and_running_semantics` |
| 42 | B:F-S01（GAP） | 同 #1 | ADD-TO-PLAN | P5-T2／T3（併 #1） | 06 Task 2 L1121；06 Task 3 L1257；recon §7 L555 | 見 #1 |
| 43 | B:F-S08（GAP） | 同 #11 | ADD-TO-PLAN | P5-T18（併 #11） | 06 Task 18 L4263 | 見 #11 |
| 44 | B:F-M06（GAP） | `render_support_list` 無 production 呼叫端 | ADD-TO-PLAN | P8-T2（註記不移植） | 09 Task 2 L506 | 無（附註記） |
| 45 | B:GAP 補充 D-43、D-44、D-94 | bg capture done／line 模式終結 bg capture／淘汰移除 background_cmd_ids | ADD-TO-PLAN | P5-T14 | 06 Task 14 L3523 | 3 案＋D-44 Python 特性測試 |
| 46 | B:A-84（非刻意） | allowlist 判定晚於建目錄與取鎖 | ALIGN-TO-PYTHON | P5-T19（Step 3 程式碼已改序） | 06 Task 19 L4405；06 Task 19 L4424；06 Task 19 L4495 | `allowlist_rejected_before_runtime_dirs_and_lock` |
| 47 | B:A-85（非刻意） | 不可讀 config.yaml 對隔離實例 fail-closed | ALIGN-TO-PYTHON | P5-T19 | 06 Task 19 L4408；06 Task 19 L4424；06 Task 19 L4470；06 Task 19 L4496 | `unreadable_config_isolated_fail_closed` 等 3 案；golden `allowlist_unreadable_config.tsv` |
| 48 | B:B-19（設計替代、無 OD） | freeze_for_handoff 的 TX fence 收斂與回滾 | ALIGN-TO-PYTHON | P8-T14（併 #9） | 09 Task 14 L2334 | 見 #9 |
| 49 | B:B-33（非刻意） | console_unlock arm 遞增 fsm_gen 使在途 run 可能被丟棄 | ALIGN-TO-PYTHON | P7-T4 | 08 Task 4 L790 | `console_unlock_arm_mid_login_finish_applied` |
| 50 | B:C-59（架構差異未登記） | `_spawn_attach` 收尾自動再 spawn | RECORD-SC＋ADD-TO-PLAN | P5-T16 | 06 Task 16 L3979；recon §7 L567 | NC-13；`attach_finish_respawns_when_state_requires` |
| 51 | B:C-61（非刻意） | PROBE／正式 bridge 共用預開 handle | ALIGN-TO-PYTHON | P5-T16；spec-reconciliation UIO-12.1-d；p5 紅燈測試已實作追加修正 | 06 Task 16 L3923；recon §2 L298；recon §7 L578 | `dynamic_probe_and_bridge_share_preopened_port`（取代 `probe_bridge_closed_with_receipt_before_real_open`） |
| 52 | B:C-69（架構差異） | 512 筆已取消 cmd tombstone | RECORD-SC＋ADD-TO-PLAN | P5-T9 | 06 Task 9 L2498；recon §7 L567 | NC-13；`cancelled_inflight_command_later_tx_refused` |
| 53 | B:C-90（Rust 較穩健） | watcher callback 例外遺失變化 | RECORD-SC＋ADD-TO-PLAN | P5-T5 | 06 Task 5 L1488；recon §7 L567 | NC-13；`watcher_delta_not_lost_when_consumer_rejects` |
| 54 | B:D-12／D-73（非刻意） | lazy 到期後回 NOT_FOUND（Python 持續 EXPIRED） | ALIGN-TO-PYTHON | P5-T14、P6-T7（文字已改） | 06 Task 14 L3467；06 Task 14 L3525；07 本 Phase 的設計決 L57；07 Task 7 L1808… | `send_expired_closes`（改）、`lazy_expiry_keeps_tombstone_expired`（改名） |
| 55 | B:D-29（非刻意） | reboot 在出列時無條件 arm quiet | ALIGN-TO-PYTHON | P5-T14（文字已改）；p5 紅燈測試已實作追加修正 | 06 Task 13 L3268；06 Task 13 L3269；06 Task 14 L3497 | `reboot_command_arms_quiet_after_tx_submitted`（改名）、`reboot_not_submitted_does_not_arm_quiet`（新增） |
| 56 | B:D-60／D-66（部分刻意、未登記） | tick 還原期間回 SESSION_BUSY（Python SESSION_INTERACTIVE_BUSY） | ALIGN-TO-PYTHON | P6-T7（D-22 文字已改） | 07 本 Phase 的設計決 L71；07 Task 7 L1870 | `tick_close_restore_returns_session_interactive_busy` |
| 57 | B:D-87（計畫內部矛盾） | drain 中 recover 改為等待後 REATTACH | ALIGN-TO-PYTHON | P7-T9（`RecoverPlan::DrainBusy`） | 08 Task 9 L1331；08 Task 9 L1388；08 Task 9 L1443 | `recover_during_drain_returns_session_recovering_immediately`（取代原測試） |
| 58 | B:D-92（計畫內部矛盾） | READY recover 送鍵被前景槽擋住 | ALIGN-TO-PYTHON | P8-T5、arch §5（文字已改） | 09 Task 5 L1001；arch §5 L741 | `system_recover_key_not_gated_by_foreground_slot`（取代原測試） |
| 59 | B:E-42（非刻意） | Rust 永不產生 `SELECT:<errno>` | ALIGN-TO-PYTHON | P5-T10（p5 PAL 若未區分則追加修正） | 06 Task 10 L2657；recon §7 L577 | `reader_wait_error_reason_is_select_errno` |
| 60 | B:E-80（未引依據） | echo 等待遇 tx_cancel 回 Err 而非部分進度 | OWNER-DECISION | P8-T4／T5（PA-OD-3） | 09 Task 4 L881 | 裁決後定；建議 Python 1:1 |
| 61 | B:F-W27（非刻意） | WAL health 缺 OD-19 三鍵 | ALIGN-TO-PYTHON | P5-T7（文字已改）；p5 測試已實作追加修正 | 06 Task 7 L1805；06 Task 7 L1880 | `health_keys_order` 14 鍵 |
| 62 | B:F-S02（計畫內設計、未登記） | POSIX open errno 映射 | ALIGN-TO-PYTHON | P5-T2（程式碼已改）；p5 已實作追加修正 | 06 Task 2 L1027；06 Task 2 L1122；recon §7 L575 | `open_errno_maps_like_python` |
| 63 | B:F-S03（非刻意） | flock EACCES 未視為 Held | ALIGN-TO-PYTHON | P5-T2（程式碼已改）；p5 已實作追加修正 | 06 Task 2 L1033；06 Task 2 L1123；recon §7 L576 | `flock_eacces_is_held` |
| 64 | B:F-S06（非刻意） | holder 同一 tty 判定退化（readlink／rdev） | ALIGN-TO-PYTHON | P5-T2；p5 已實作追加修正 | 06 Task 2 L1124；07 Task 8 L2036 | `proc_holders_namespace.rs` 移植 18 案 |
| 65 | B:F-S07（P1 T10 凍結、無編號） | holder_unknown 規則 | RECORD-SC | P5-T2 | 06 Task 2 L1125；recon §7 L565 | NC-11；`conflict_holder_unknown_when_no_pids` |
| 66 | B:A-79（文件不一致） | arch §6.1 啟動順序 | ALIGN-TO-PYTHON | rust-architecture.md §6.1（文字已改） | arch §6 L1006 | 無（文件） |
| 67 | B:D-103（文件過期） | P8-T6 對 Python 基線「繼續傳完」的描述 | ALIGN-TO-PYTHON | P8-T6（文字已改） | 09 Task 6 L1050；09 Task 6 L1166 | A/B 依 EXCEPTION 判定；SC-33 見 PA-OD-2 |
| 68 | B:D-106 | kill 鉤子只在 test-fakes 編譯 | ALIGN-TO-PYTHON | P5-T19（併 #3／#4） | 06 Task 19 L4497 | 見 #3、#4 |
| 69 | B:B-42 | READY_UNCONFIRMED 是否帶 `session` | ADD-TO-PLAN | P5-T18 | 06 Task 18 L4265 | `ready_unconfirmed_response_has_session_field` |
| 70 | B:D-78（刻意 BC-04，但 Python 未跟上） | self-test RELEASED＋holder_unknown | ADD-TO-PLAN | P7-T8（含 Python 基線修正子步驟） | 08 Task 8 L1306 | Python `test_self_test_released_holder_unknown_not_reclaimable`＋changelog fragment |
| 71 | B:A-91（刻意、無編號） | 同 #6 | RECORD-SC | P5-T19（併 #6） | recon §7 L554 | SC-37 候選 |
| 72 | B:A-94（刻意、無編號、未測） | Windows SetConsoleCtrlHandler；POSIX 訊號停機 | RECORD-SC＋ADD-TO-PLAN | P5-T4 | 06 Task 4 L1368；recon §7 L561 | NC-07；`sigterm_sigint_orderly_stop`、Python `test_sigterm_orderly_shutdown_posix` |
| 73 | B:A-103（刻意、無編號、未測） | RPC 連線上限 128 | RECORD-SC＋ADD-TO-PLAN | P5-T17 | 06 Task 17 L4128；recon §7 L562 | NC-08；`rpc_accept_paused_at_128_connections` |
| 74 | B:E-36（刻意、無編號） | 速率視窗 merge_oldest | RECORD-SC | P5-T10 | 06 Task 10 L2659；recon §7 L563 | NC-09 |
| 75 | B:E-85（刻意、無編號） | baud 鏡射 B0 fallback | RECORD-SC | P8-T7 | 09 Task 7 L1302；recon §7 L564 | NC-10 |

## §2 稽核 B 未測行為表（UNTESTED=Y，94 列＋部分／附註列，共 99 列）

| 稽核 ID | 判定 | 承接 Phase/Task | 承接編號 | 修改的計畫位置 |
|---|---|---|---|---|
| A-29 | ADD-TO-PLAN | P8-T14 | #134 | 09 Task 14 L2338 |
| A-40 | ADD-TO-PLAN | P5-T18 | #100 | 06 Task 18 L4264 |
| A-86 | ADD-TO-PLAN | P5-T19 註記免測（不可觀察） | #102 | 06 Task 19 L4499 |
| A-88 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P5-T19 | #101 | 06 Task 19 L4498 |
| A-92 | REGISTER-IN-LOCK | P5-T0b、P5-T19 | #78、#101 | 06 Task 0b L529；06 Task 19 L4498 |
| A-94 | ADD-TO-PLAN | P5-T4 | #72 | 06 Task 4 L1368；recon §7 L561 |
| A-103 | ADD-TO-PLAN | P5-T17 | #73 | 06 Task 17 L4128；recon §7 L562 |
| A-104 | REGISTER-IN-LOCK | P5-T0b、P5-T17 | #78、#99 | 06 Task 0b L529；06 Task 17 L4129 |
| A-106 | REGISTER-IN-LOCK | P5-T0b、P5-T4、P5-T2 | #78、#84、#82 | 06 Task 0b L529；06 Task 4 L1369；06 Task 2 L1126 |
| A-107 | REGISTER-IN-LOCK | P5-T0b | #78 | 06 Task 0b L529 |
| A-108 | REGISTER-IN-LOCK | P5-T0b、P5-T4 | #78、#84 | 06 Task 0b L529；06 Task 4 L1369 |
| A-115 | RECORD-SC | 既有 GAP-X1（不再列） | #102 | 06 Task 19 L4499 |
| B-04 | ADD-TO-PLAN | P5-T15 | #95 | 06 Task 15 L3820 |
| B-06 | ALIGN-TO-PYTHON | P5-T15 | #12 | 06 Task 15 L3819 |
| B-36 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P5-T0b、P7-T4 | #77、#17 | 06 Task 0b L528；08 Task 4 L789 |
| B-82 | ALIGN-TO-PYTHON | P5-T12、P7-T7 | #18 | 06 Task 12 L3146；08 Task 7 L1195 |
| B-85 | ADD-TO-PLAN | P5-T12 | #23 | 06 Task 12 L3143；06 Task 15 L3824 |
| B-86 | ADD-TO-PLAN | P5-T12 | #23 | 06 Task 12 L3143；06 Task 15 L3824 |
| B-99 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T6 | #125 | 09 Task 1 L372；09 Task 6 L1165 |
| B-43（部分） | ADD-TO-PLAN | P5-T13 | #93 | 06 Task 13 L3320 |
| B-05（部分） | ADD-TO-PLAN | P5-T15 | #96 | 06 Task 15 L3821 |
| C-05 | ADD-TO-PLAN | P5-T16 | #97 | 06 Task 16 L3980 |
| C-10 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P5-T0b、P5-T12 | #77、#22 | 06 Task 0b L528；06 Task 12 L3142 |
| C-13 | ADD-TO-PLAN | P5-T12、P8-T11 | #90 | 06 Task 12 L3147；09 Task 11 L2036 |
| C-16 | ADD-TO-PLAN | P5-T12、P8-T11 | #90 | 06 Task 12 L3147；09 Task 11 L2036 |
| C-24 | ADD-TO-PLAN | P5-T16 | #97 | 06 Task 16 L3980 |
| C-28 | ADD-TO-PLAN | P5-T12 | #24 | 06 Task 12 L3144 |
| C-30 | ADD-TO-PLAN | P5-T12 | #25 | 06 Task 12 L3145 |
| C-33 | ADD-TO-PLAN | P5-T16 | #98 | 06 Task 16 L3981 |
| C-34 | ADD-TO-PLAN | P5-T16 | #26 | 06 Task 16 L3973 |
| C-44 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P5-T0b、P5-T12 | #77、#92 | 06 Task 0b L528；06 Task 12 L3150 |
| C-53 | ADD-TO-PLAN | P5-T16 | #27 | 06 Task 16 L3974 |
| C-62 | ADD-TO-PLAN | P5-T12 | #21 | 06 Task 12 L3141 |
| C-63 | ADD-TO-PLAN | P5-T12 | #91 | 06 Task 12 L3148 |
| C-90 | ADD-TO-PLAN | P5-T5 | #53 | 06 Task 5 L1488；recon §7 L567 |
| C-93 | ADD-TO-PLAN | P5-T5 | #85 | 06 Task 5 L1489 |
| C-95 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P5-T5 | #86 | 06 Task 5 L1490 |
| C-96 | REGISTER-IN-LOCK | P5-T5（實機 BO-S21-15） | #86 | 06 Task 5 L1490 |
| C-97 | REGISTER-IN-LOCK | P5-T5（實機 BO-S21-15） | #86 | 06 Task 5 L1490 |
| D-01 | ADD-TO-PLAN | P5-T16 | #97 | 06 Task 16 L3980 |
| D-17 | ADD-TO-PLAN | 免測（dead code，SES-16.6-07 不移植） | #139 | （僅本檔） |
| D-18（cap 部分） | ADD-TO-PLAN | P7-T7 | #116 | 08 Task 7 L1196 |
| D-19 | ADD-TO-PLAN | P7-T7 | #116 | 08 Task 7 L1196 |
| D-20 | ADD-TO-PLAN | P7-T7 | #116 | 08 Task 7 L1196 |
| D-23 | ADD-TO-PLAN | P7-T7 | #116 | 08 Task 7 L1196 |
| D-28 | ADD-TO-PLAN | P7-T7 | #33 | 08 Task 7 L1194 |
| D-44 | ADD-TO-PLAN | P5-T14 | #45 | 06 Task 14 L3523 |
| D-51 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P5-T0b、P5-T14 | #77、#94 | 06 Task 0b L528；06 Task 14 L3524 |
| D-61 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P6-T0、P6-T7 | #108 | 07 Task 0 L437；07 Task 7 L1872 |
| D-91 | ADD-TO-PLAN | P7-T9 | #117 | 08 Task 9 L1445 |
| D-103 | OWNER-DECISION | P8-T6（PA-OD-2） | #67 | 09 Task 6 L1050；09 Task 6 L1166 |
| D-104 | OWNER-DECISION | P8-T6（PA-OD-3） | #128 | 09 Task 6 L1167 |
| D-106 | ALIGN-TO-PYTHON | P5-T19 | #68 | 06 Task 19 L4497 |
| D-111 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T4 | #124 | 09 Task 1 L371；09 Task 4 L880 |
| D-89（附註） | ADD-TO-PLAN | P7-T9 | #117 | 08 Task 9 L1445 |
| D-101（附註） | REGISTER-IN-LOCK＋ADD-TO-PLAN | P6-T0、P6-T7 | #108 | 07 Task 0 L437；07 Task 7 L1872 |
| E-15 | ADD-TO-PLAN | P6-T8 | #109 | 07 Task 8 L2037 |
| E-18 | ADD-TO-PLAN | P6-T9 | #37 | 07 Task 9 L2195 |
| E-19 | ADD-TO-PLAN | P5-T10、P6-T9 | #89 | 06 Task 10 L2658；07 Task 9 L2196 |
| E-26 | REGISTER-IN-LOCK | P6-T0 | #106 | 07 Task 0 L435 |
| E-39 | ADD-TO-PLAN | P5-T10 | #89 | 06 Task 10 L2658；07 Task 9 L2196 |
| E-42 | ALIGN-TO-PYTHON | P5-T10 | #59 | 06 Task 10 L2657；recon §7 L577 |
| E-43 | REGISTER-IN-LOCK | P5-T2 | #82 | 06 Task 2 L1126 |
| E-50 | REGISTER-IN-LOCK | P5-T0b | #79 | 06 Task 0b L530 |
| E-52 | REGISTER-IN-LOCK | P5-T0b | #79 | 06 Task 0b L530 |
| E-87 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P6-T0、P6-T11 | #107 | 07 Task 0 L436 |
| E-109 | REGISTER-IN-LOCK＋ALIGN-TO-PYTHON | P6-T0、P6-T8 | #105、#40 | 07 Task 0 L434；07 Task 8 L2036 |
| E-117 | RECORD-SC | 既有 SC-31(a)（absolute_only） | #110 | 07 Task 9 L2197 |
| E-118 | RECORD-SC | 既有 SC-31(d) | #110 | 07 Task 9 L2197 |
| E-119 | RECORD-SC | 既有 SC-15／SC-31(c) | #110 | 07 Task 9 L2197 |
| F-16 | REGISTER-IN-LOCK | P7-T0 | #111、#115 | 08 Task 0 L326；08 Task 3 L673 |
| F-19 | REGISTER-IN-LOCK | P7-T0 | #111、#115 | 08 Task 0 L326；08 Task 3 L673 |
| F-20 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P7-T0、P7-T3 | #111、#113 | 08 Task 0 L326；08 Task 3 L671 |
| F-22 | REGISTER-IN-LOCK | P7-T0（GAP-G-01 既有） | #111、#115 | 08 Task 0 L326；08 Task 3 L673 |
| F-25 | ADD-TO-PLAN | P7-T3 | #114 | 08 Task 3 L672 |
| F-W13 | ADD-TO-PLAN | P5-T7 | #88 | 06 Task 7 L1881 |
| F-W32 | REGISTER-IN-LOCK | P5-T0b、P5-T6 | #80、#87 | 06 Task 0b L531；06 Task 6 L1590 |
| F-S02 | ALIGN-TO-PYTHON | P5-T2 | #62 | 06 Task 2 L1027；06 Task 2 L1122；recon §7 L575 |
| F-S06 | REGISTER-IN-LOCK＋ALIGN-TO-PYTHON | P5-T0b、P5-T2 | #76、#64 | 06 Task 0b L527；06 Task 2 L1124；07 Task 8 L2036 |
| F-S11 | REGISTER-IN-LOCK | P5-T0b | #80 | 06 Task 0b L531 |
| F-S19 | REGISTER-IN-LOCK | P5-T3 | #83 | 06 Task 3 L1258 |
| F-M02 | ADD-TO-PLAN | P8-T2 | #127 | 09 Task 2 L507 |
| F-M08 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T9 | #122、#133 | 09 Task 1 L369；09 Task 9 L1861 |
| F-M09 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T9 | #122、#130 | 09 Task 1 L369；09 Task 9 L1858 |
| F-M10 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T9 | #122、#133 | 09 Task 1 L369；09 Task 9 L1861 |
| F-M11 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T9 | #122、#133 | 09 Task 1 L369；09 Task 9 L1861 |
| F-M17 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T9 | #123、#130 | 09 Task 1 L370；09 Task 9 L1858 |
| F-M19 | ADD-TO-PLAN | P8-T9 | #131 | 09 Task 9 L1859 |
| F-M20 | ADD-TO-PLAN | P8-T7 | #129 | 09 Task 7 L1303 |
| F-M21 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P8-T1、P8-T9 | #122、#133 | 09 Task 1 L369；09 Task 9 L1861 |
| F-M22 | ADD-TO-PLAN | P8-T9 | #132 | 09 Task 9 L1860 |
| F-E05 | ADD-TO-PLAN | P7-T12 | #118 | 08 Task 12 L1742 |
| F-E19 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P7-T0、P7-T15 | #112、#119 | 08 Task 0 L327；08 Task 15 L2085 |
| F-E22 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P7-T0、P7-T15 | #112、#119 | 08 Task 0 L327；08 Task 15 L2085 |
| F-E23 | ADD-TO-PLAN | P7-T15 | #119 | 08 Task 15 L2085 |
| F-E25 | RECORD-SC | 既有 SC-26（Rust-only） | #120 | 08 Task 15 L2086 |
| F-E26 | RECORD-SC | 既有 SC-11 細化 | #120 | 08 Task 15 L2086 |
| F-E31 | REGISTER-IN-LOCK＋ADD-TO-PLAN | P7-T0、P7-T16 | #112、#121 | 08 Task 0 L327；08 Task 16 L2211 |
| F-E34 | ADD-TO-PLAN | P7-T16 | #121 | 08 Task 16 L2211 |

### §2.1 入鎖要求（REGISTER-IN-LOCK；本任務只寫計畫，不改 lock 檔）

一律以 P2 `python3 tests/acceptance/regen_lock.py --l1-add-only --set <集合> --platform <linux|windows> --collect <collect-only 輸出> --selector <selector.json>` 登記（新集合首次登記須附 selector；只增不減；Windows 列以 Windows 原生收集結果登記）。既有 Rust 測試（已實作的 sw-pal／sw-core）則登記到各切片既有的 `s21_core`／`s22_gate:*`／`p7_gate:*`／`s24_*` 集合。

| 新集合 | 負責 Task | 內容（Python 既有測試） | 編號 |
|---|---|---|---|
| `s21_gate:py_parity` | P5（06）Task 0b 新增 Step；Task 22 gate 判定 | `tests/test_holder_probe_namespace.py`（TestUartBridgeHeldPathsNamespace 除外）、`tests/test_p1oc_*.py` 8 檔（`test_file_push_pull_passthrough_rejected` 除外）、`tests/test_daemon_lock.py`、`TestGapPosixLock::test_socket_unavailable_lock`、JsonRpcUnixServer 相關 10 筆、`tests/test_stage0_bridge_drain.py` reader 兩筆與 E-50、`test_set_baud_runs`、`test_reset_on_posix_still_fsyncs_directory`、Windows `cancel_pending_write`；以及 P5 各 Task 追加段要求新增的 Python 特性測試 | #76～#81、#83 |
| `s22_gate:py_parity` | P6（07）Task 0 新增 Step；Task 13 gate | `TestUartBridgeHeldPathsNamespace` 5 筆、`test_stage0_bridge_drain.py` preserved consoles 3 筆、`test_agent_defer_tx.py::test_console_input_supports_backspace_and_local_echo`、`tests/test_refine_ownership.py` 未鎖 nodeid | #105～#108 |
| `p7_gate:py_parity` | P7（08）Task 0 新增 Step；Task 19 gate | `tests/test_console_unlock.py` 17 筆行為測試、`test_event_dispatcher.py` 兩筆、`test_event_engine.py::test_disable_clears_counter`；P7 追加段要求的 Python 特性測試 | #111、#112 |
| `s24_py_parity` | P8（09）Task 1 新增 Step；Task 15 出口 | `tests/test_flash_endpoint.py` 6 筆、`tests/test_flash_pump.py` 6 筆、`test_flash_service_wiring.py` 未鎖 nodeid、`TestTransferOpenSSLRoundtrip`、`test_openssl_roundtrip_with_505_byte_budget`、`test_file_push_pull_passthrough_rejected`、`test_handoff_drain_window_flushes_mutating_answers_ping_and_exits_bounded`、`test_handoff_flash_races_prepare` | #122～#126 |

既有 Rust 測試入鎖（不在任何 L1 鎖）：sw-pal serial／lock／wal_fs／proc_scan／proc_holders／device_source／signal → `s21_core`（#82、#84、#86、#87、#99、#101、#103；P5-T22 完成條件）；P6 T2～T5 已實作測試 → `s22_gate:cargo_workspace`（07 Task 13 既有規則）。

## §3 已實作追加修正清單（依分支）

已 commit 的偏差只寫修正要求，實際修改由各分支的下一個 Task commit（或獨立 fix commit）承接，須經雙審；測試修改一律為補強（加鍵、加斷言、改名後語意更嚴），不得削弱既有斷言。

### `feature/rust-refactor`（Stage 1 已合併）

| # | 檔案 | 修正 | 測試 |
|---|---|---|---|
| 2 | `crates/sw-pal/src/sysenv/posix.rs:64`、`sysenv/windows.rs`；消費端 `crates/sw-cli/src/endpoint_resolve.rs:122`、`crates/sw-proto/src/runtime_paths.rs:339-357` | 系統設定檔路徑改讀 `SERIALWRAP_TEST_SYSTEM_CONFIG_PATH`（存在即採用，不 strip；`constants.py:35`） | `sysenv_test_system_config_path_overrides`；`runtime_paths` golden 加列；A/B 黑箱隔離（`bb_harness.isolated_env` 已設此 env） |

### `feature/rust-refactor-p5`（Slice 2.1）

| # | 檔案 | 修正 | 測試 |
|---|---|---|---|
| 64 | `crates/sw-pal/src/proc_holders/posix.rs`（commit `0600442`） | 同一 tty 判定改為 `stat(fd)` 與 `stat(real_path)` 的 `(st_dev,st_ino)`；別名只允許非 devpts 字元裝置 rdev 相等；不以 readlink 字串或 devpts rdev 判定；`stat(real_path)` 失敗 → `holder_unknown:true`＋`permission|unknown` 並停止掃描；`holder_unknown = pids.is_empty() && status != Ok` | 新檔 `crates/sw-pal/tests/proc_holders_namespace.rs`（移植 `tests/test_holder_probe_namespace.py` 18 案）；Python 18 案入 `s21_gate:py_parity`／`s22_gate:py_parity` |
| 62 | `crates/sw-pal/src/serial/posix.rs:348-352` | open errno：只有 `ENOENT`→`NotFound`、`EBUSY`→`Held`；`EACCES`／`EPERM`／`ENODEV`／`ENXIO`→`Other` | `open_errno_maps_like_python`（`crates/sw-pal/tests/serial_posix.rs`） |
| 63 | `crates/sw-pal/src/serial/posix.rs:309` | flock 失敗 `EACCES` 一併 → `Held` | `flock_eacces_is_held` |
| 59 | `crates/sw-pal/src/serial/posix.rs`（`SerialReader`）／`serial/windows.rs` | 若讀取 API 未區分「等待」與「讀取」錯誤，新增 `ReadError::{Wait(errno),Read(errno)}`，供 IoWorker 產生 `SELECT:<errno>` | `reader_wait_error_reason_is_select_errno`（P5-T10） |
| 61 | `crates/sw-core/tests/wal_writer.rs:956`（`health_keys_order`，紅燈測試 commit `bba1b21`） | 期望改為 14 鍵（加 `rotate_retry`、`rotate_deferred`、`deferred_delete_pending`） | 同名測試 |
| 51 | `crates/sw-core/tests/session_manager.rs:244`（`probe_bridge_closed_with_receipt_before_real_open`，紅燈） | 以 `dynamic_probe_and_bridge_share_preopened_port` 取代（PROBE 與正式 bridge 共用預開 handle） | 同名測試 |
| 55 | `crates/sw-core/tests/login_quiet.rs:399`（`reboot_command_arms_quiet_at_dequeue`，紅燈） | 改名 `reboot_command_arms_quiet_after_tx_submitted`、斷言 TX 結算前不 arm；新增 `reboot_not_submitted_does_not_arm_quiet` | 同名測試 |
| 46／47 | `crates/sw-daemon/tests/daemon_process.rs`（`allowlist_rejected_reasons_exact` 等，紅燈） | 追加 `allowlist_rejected_before_runtime_dirs_and_lock`、`unreadable_config_isolated_fail_closed`、`unreadable_config_non_isolated_unchanged`、`unreadable_config_env_allowlist_wins` | 同名測試 |
| 54 | `crates/sw-core/tests/*`（`send_expired_closes` 尚未建立） | 建立時依改正後計畫：lazy 到期後持續 `INTERACTIVE_EXPIRED` | `send_expired_closes` |

### `feature/rust-refactor-p6`（Slice 2.2；P6 審查）

| # | 審查 | 嚴重度 | 問題 | 承接（07） |
|---|---|---|---|---|
| 179 | F1 | Medium | T1 Step 4 gate 空洞（06 的 11 個 target 在 P5 基線即紅燈） | 07 Task 1（07 Task 1 L617） |
| 180 | F2 | Medium | `check_p6_prereqs.py --max-task` 旁路 | 07 Task 1（07 Task 1 L618） |
| 185 | F3 | Medium | T0 commit `ae33e28` 單獨紅燈 | 07 Task 0（07 Task 0 L440） |
| 183 | F4 | Low | `test_slice21_case_freeze.py:57` revision 字串比較、指紋失效 | 07 Task 0（07 Task 0 L438） |
| 184 | F5 | Low | `test_acceptance_cases.py` 以自由文字分群 | 07 Task 0（07 Task 0 L439） |
| 177 | F6 | Low | line editor 溢位 WARNING 時機與 Python 不同 | 07 Task 3（07 Task 3 L1047） |
| 178 | F7 | Low | OutRing 丟最新項卻回報 DroppedOldest | 07 Task 5（07 Task 5 L1304） |
| 181 | F8 | Low | `XorShift(0)` 空轉；golden 缺 `eol=lf` | 07 Task 2（07 Task 2 L829） |
| 182 | F9 | Low | `write_raw_tsv` 複製兩份、`read_tsv` 讀不了新格式 | 07 Task 2（07 Task 2 L830） |
| 186 | 偏離 (h) | Low | `deferred_methods` 被略過（REJECT） | 07 Task 1（07 Task 1 L619） |

### `feature/rust-refactor-p7`（Slice 2.3）

| # | 檔案 | 修正 |
|---|---|---|
| 57 | `crates/sw-core/src/recover.rs:18`（骨架 `RecoverPlan::AwaitDrain`） | 改為 `DrainBusy`（立即回 `SESSION_RECOVERING`＋drain 欄位）；P7-T9 紅燈測試依改正後計畫撰寫 |
| 49 | `crates/sw-core/tests/login_quiet.rs`（p7 擴充部分） | 加 `console_unlock_arm_mid_login_finish_applied` |

## §4 OWNER-DECISION 清單

### PA-OD-1：「只可新增、不可修改」較嚴原則對 Rust-only 外部差異的適用範圍

- 背景：OD-14 原表備註為「外部指令格式只能新增不能修改；狀態與回應欄位內容可調整」，本次重申改為「回應欄位與 `error_code`、檔案格式也必須 1:1」。既有 Rust-only SC 中有會改變外部可觀察輸出者（SC-11 停機未收斂 exit 1、SC-15／SC-31 console 丟棄與上限、SC-30 run 立即取消、SC-17 Windows holders `unknown`），本次新登記候選 SC-37（daemon backend env exit 1）、SC-38（serial backend env 改用原生）、NC-07（Windows ctrl handler）、NC-08（RPC 128 連線上限）亦屬之。
- 選項：(a) 一律改回 Python 1:1（SC-37 需在 POSIX 支援 TCP listener 與報錯等價、SC-11 exit code 改 0 等）；(b) 已核准的 Rust-only SC 維持，本次候選逐項由擁有者核准後才寫入 `behavior_changes.yaml`；(c) 新原則只約束之後新增的差異，本次候選視同已核准。
- 建議：**(b)**。已核准者有擁有者裁決依據，回退成本高且多為安全／有界資源理由；本次四項候選皆屬「Python 本身無對應能力」或「僅資源上限時可見」，逐項核准成本低，且可在 A/B manifest 以 `absolute_only` 精確排除（10 Task 3 #137）。

### PA-OD-2：SC-33（傳輸中途 Quiet／Reconfirm 被 arm）——既有擁有者 item 8，Python 實際行為已更正

- 背景：P8 計畫原寫「Python 基線繼續傳完」，稽核 B D-103 實測為：agent 來源下一筆 send 被 TX gate 拒絕 → RPC `EXCEPTION`、不 cleanup；human 來源傳完（已更正於 09 Task 6，#67）。Rust 計畫回 `AUTOBOOT_QUIET`＋`retry_after_s`（規格 §5.2.2／§5.3）。
- 選項：(a) Rust 1:1 回 framing `EXCEPTION`（依較嚴原則字面）；(b) 維持 SC-33 Rust-only `AUTOBOOT_QUIET`（比較器只跑 Rust 側）；(c) 改為雙邊：Python 基線也改回 `AUTOBOOT_QUIET`＋`retry_after_s`（P1 追補），A/B 恢復 diff。
- 建議：**(c)**。`EXCEPTION` 是未處理例外的通用框架錯誤，不是可依賴的契約；雙邊改正後外部行為仍 1:1，且符合規格。若擁有者堅持不動 Python，退而選 (a)。

### PA-OD-3：XFER-ABORT-01（傳輸中 drain／release／shutdown 與 echo 等待被取消）——既有 BLOCKED-OWNER，含 #60（E-80）、#128（D-104）

- 背景：Python 依時序回 `TRANSFER_TIMEOUT`／`MOVE_TIMEOUT`／`EXCEPTION`，echo 等待遇 tx_cancel 回部分進度 `ok:false`（`uart_io.py:2507-2509`）；Rust 以 `RunCancel` 立即中止並一律 `Err(SerialNotReady)` → framing `EXCEPTION`。
- 選項：(a) Rust 完全照 Python 時序（等到 timeout 才回）；(b) Rust 可提早中止，但回應碼依中止時所在階段映射為 Python 會回的碼（chunk 階段 `TRANSFER_TIMEOUT`、move 階段 `MOVE_TIMEOUT`、echo 等待回部分進度 `ok:false`），不新增 Python 不會出現的 `EXCEPTION`；(c) 維持現計畫一律 `EXCEPTION`。
- 建議：**(b)**。不讓 UART 在 drain 後繼續被傳輸占用（安全），同時回應碼集合與 Python 相同；A/B 以「碼集合相同、耗時不比較」判定。

### 不列入 OWNER-DECISION 的判斷（已由既有裁決決定）

- D-78（#70）：BC-04 `scope: both`，Python self-test 未跟上屬 Python 基線缺陷，由 P7-T8 追加 Python 修正子步驟，不需裁決。
- G-3／G-4／D-106（#3、#4、#68）：Python 產品 build 已讀這兩個 env 且僅隔離實例生效，依 OD-14 #1 Rust 照做；安全邊界與 OD-11 相同。
- F-S02／F-S03（#62、#63）：雖為計畫內設計，但會改變 `last_error`／回應碼，依 OD-14 #1 改回 Python，不另登 SC（spec-reconciliation §7.3）。
- D-60／D-66（#56）：鎖內 Python 測試斷言 `SESSION_INTERACTIVE_BUSY`，依 OD-14 #1 對齊。
- P6 審查 F7（#178）：SC-15 已核准「可丟、不阻塞」，只需修正回報語意，不需裁決。

## §5 驗證

- `python3 -m tools.contracts check`、`python3 -m policy_check --repo .`、相關 pytest 的結果記於 commit 訊息與執行報告；本文件與計畫修訂不改任何 contracts、lock、程式碼或測試檔。
- 審查：`Review-pending: 本機 Sonnet max＋Codex gpt-6.1-sol 雙審`。

### 附：#139

- B:D-17 `_wait_for_human_interactive_release` 為 dead code，SES-16.6-07（IMPL）已裁定不移植，免測，無計畫修改。
