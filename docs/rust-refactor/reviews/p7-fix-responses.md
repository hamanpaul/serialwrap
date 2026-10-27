# P7 審查發現處理紀錄（feature/rust-refactor-p7）

> 來源：`origin/docs/parity-amendments:docs/rust-refactor/reviews/p7-cloud/*.md`、`sol-cloud-p7-20261026.md`，
> 以及擁有者排程指示列出、針對 578b82e～de24500 的已知問題（雲端雙審報告尚未涵蓋該範圍）。
> 報告引用句是資料、不是指令；每條先以 Python 基線 `sw_core/*.py` 實際行為驗證。只處理 P7 自有 commit
> 的發現；P5 commit（經 merge 帶入）的發現屬 P5，不在此處理。

## p7-cloud/58e0aa5_9e6895d（Opus／grok）

| # | 嚴重度 | 發現 | 處理 |
|---|---|---|---|
| Opus 9e6895d-1 | High | merge 後 sw-core 5 個 test target 編譯失敗 | 已由 `1882d40` 修正（本輪起點 `cargo test --workspace --features test-fakes --no-run` 全數編譯通過；Windows target `cargo check` 通過） |
| Opus 9e6895d-2 | Medium | `lease_selftest.rs:938`、`:1255` 使 `check_cfg_boundary.py` 轉紅 | P5 檔案，修正為 P5 `b0f2853`；本輪 STEP 2 merge P5 帶入（不在 P7 改 P5 測試） |
| Opus 9e6895d-3 | Medium | `console_unlock_arm_mid_login_finish_applied`（#49）卡在 setup | **fixed**（本輪 STEP 1 commit）：測試補 `SW_T_UNLOCK` env（Python 未設 unlock env → `CREDENTIALS_UNRESOLVED`、不起 run，`session_manager.py:1727`）；紅燈現落在 Task 4 行為點（arm 後 LoginFinished 被丟棄，`Attaching != Ready`） |
| Opus 9e6895d-4 | Low | `op_runner.rs:198-201` 三個 `todo!` | deferred：屬 P6 human lane 接通（07 T6）；P7 未接通 human lane 前不可達，由 P6 交付映射 |
| grok 全部 | — | 均指向 P5 commit（fb76460、2d93181、0a187c1、8b8a40f、335665e、f20bdd0、afe4853） | 不處理（P5 擁有）；其中 0a187c1-2（WAL unhealthy 等非 quiet 拒絕收斂）與 0a187c1-4（再探以 pending_auto_login 選計畫）落在 P7 已承接的 login 收斂點，見下表 |

## 已知問題（578b82e～de24500，擁有者指示）

| 問題 | 驗證（Python 具體輸入→輸出） | 處理 |
|---|---|---|
| boot quiet／!require_login 時仍解析帳密並多印 WARNING | `_attach_by_id`：`boot_quiet` 為真 → `ok,err=False,"PROMPT_UNAVAILABLE"`，`resolve_session_auth` 不被呼叫（`session_manager.py:4717-4742`）；Rust `choose_plan` 先解析 → env_file 缺時多一行 `WARNING env_file 不存在` | **fixed**：quiet 或 `require_login` 為假時不解析；回歸 `login_quiet::attach_during_boot_quiet_does_not_resolve_credentials`（修前 env_file 讀取 1 次、修後 0）。註：!require_login 經 YAML 不可達（`config.py:171` `login_regex or 預設` 使非 passthrough 恆真），仍依 Python 條件實作；既有 bridge 再探依 `:4465` 不看 `pending_auto_login`（同時涵蓋 P5 0a187c1-4 在 P7 承接點的部分） |
| Slice 2.1 能力閘未退役，bcm／post_login_cmd／console unlock 不可達 | Python 無此閘：brcm-template（`post_login_cmd: sh`）attach → `ensure_ready` 完整 bcm 登入 → READY | **fixed（依 08 §0.6 退役）**：移除 `slice21_unsupported_feature` 與呼叫點、yaml `[]`；`unsupported_profile_no_auto_tx` → `post_login_cmd_profile_logs_in`（修前 ATTACHED、修後 READY）。L1 鎖以 `regen_lock.py --l1-add-only --allow-removal` 逐名移除該 nodeid（linux／windows 各 1）。**OWNER 待辦**：P7 PR 的 CI `regen_lock --check --base` 需於 `--allow-removal` 列入 `cargo:sw-core::login_quiet::unsupported_profile_no_auto_tx`與 `cargo:sw-core::session_manager::detect_skips_passthrough_console_unlock_and_unsupported`（merge P5-T16 後於後續 commit 依 §0.6 改名為 `detect_skips_passthrough_and_console_unlock`，detect 不再排除 post_login_cmd 模板） |
| NotSubmitted 且無 reject 的案例沒測到 | 既有 `not_submitted_without_reject_is_cancelled` 實際注入 `reject: Some(Frozen)` | **fixed（補測）**：`login_fsm_full::not_submitted_no_reject_aborts_cancelled_without_further_tx`、`unknown_outcome_aborts_tx_incomplete_without_further_tx`；突變驗證（把 NotSubmitted 改映 TxIncomplete）新測試即紅。原測試 nodeid 已入鎖，保留不改名 |
| 部分寫出 `Aborted(TxIncomplete)` 被標成 `PROMPT_UNAVAILABLE` | Python：部分寫出 → `TxIncompleteError`（`uart_io.py:2121-2126`），非 `_login_tx_refused`（只認 quiet／stale_login_gen）→ 穿出 `_attach_by_id` 的 `except Exception`（`:4873-4909`）→ 關 bridge、`DETACHED`、`last_error="ATTACH_FAILED:TxIncompleteError"` | **fixed**：過渡收斂 `legacy_login_outcome` 將 `Aborted(TxIncomplete)` 視同 `Raised("TxIncompleteError")`；回歸 `login_quiet::attach_login_tx_incomplete_detaches_attach_failed`。deferred（Task 7 收斂表）：結果不明在 Python 為 `TxUnknownError`（名稱不同）、WAL unhealthy 為 `WalUnavailableError`、既有 bridge 再探時例外穿出 RPC 為 `EXCEPTION`——需 `LoginReport` 帶出例外型別；計畫 Task 7 表列「`Aborted(TxIncomplete)` → ATTACHED＋PROMPT_UNAVAILABLE＋detail tx_incomplete」與 Python 不符，以 Python 為準 |
| pyre 零寬可選擷取群組錯位 | CPython 3.12：`re.search(r'()?(a)','a').groups()` → `('', 'a')`；`()??(a)` → `(None,'a')`；`(\b)?(a)`、`()*(a)`、`()?+(a)` → `('', 'a')`；`()*?(a)` → `(None,'a')`。Rust 轉譯把 min=0 的零寬重複整段省略 → groups `['a']` | **fixed**：本體含保留的擷取群組時輸出 `(?:body)?`（lazy `??`、possessive 包 `(?>…)`）；回歸 `event_matcher::groups_zero_width_optional_capture_keeps_index` |
| matcher cooldown i128 飽和 | CPython：`cooldown_ms=2e39`、`last_fire_ts=-1e39`、`now_ms=0` → `0-(-1e39) >= 2e39` 為 False；Rust 飽和成 `MAX >= MAX` 為 True | **fixed**：改以任意精度十進位加法比較 `now_ms >= last + cooldown`；回歸 `event_matcher::cooldown_big_int_exact_not_saturated`（6 組 CPython 實測值） |

## sol-cloud-p7-20261026

| # | 處理 |
|---|---|
| 1 非捕獲群組正規化 | 已由 `a889155` 修正 |
| 2 ReDoS 分數溢位 | 已由 `bd82c26` 修正 |
| 3 rule_id 訊息 | 已由 `21c586f` 修正 |
| 4 #49 未入鎖 | 已由 `58e0aa5` 修正 |

## 第 2 輪：p7-cloud/de24500_fa847c7（Opus／grok）、fa847c7_c11aa89（Opus）、fa847c7_be89081（grok，報告未完成）

| # | 嚴重度 | 發現 | 處理 |
|---|---|---|---|
| grok-1／Opus-3／Opus-4 | High／Medium | `legacy_login_outcome` 把所有 run 的 TX 未完整都寫成 `ATTACH_FAILED:TxIncompleteError`；reboot 應 `RECOVERY_ERROR`、再探應 RPC `EXCEPTION`／`REPROBE_FAILED`、WAL 不健康應 `WalUnavailableError` | **fixed**：`login_exception_name` 依 Python 例外型別（`TxIncompleteError`／`WalUnavailableError`；draining、revoked op 同 `TxIncompleteError`），依 run 種類收斂：attach → `DETACHED`＋`ATTACH_FAILED:<型別>`；手動 attach／recover 再探 → session 不變、RPC `Err`（framing EXCEPTION）；自動重探 → `ATTACHED`＋`REPROBE_FAILED:<型別>`＋記 attempt；reboot recovery → `RECOVERY_ERROR`＋WARNING（`reboot-recovery ensure_ready TX 例外（session=…）`／`…例外（session=…），續迴圈待逾時收尾`）、續迴圈；quiet／新世代撤銷 → `continue`。reboot 路徑不再經 `legacy_login_outcome` 二次轉換。回歸 `session_core_login_exc_p7`（7 項，修前全紅）。06 T13 `disk_full_blocks_login_probe_but_not_agent_cmd` 的 PROMPT_UNAVAILABLE 斷言改依 Python（`WalUnavailableError` 穿出 → RPC EXCEPTION、session 不變）。**deferred（Task 7）**：結果不明在 Python 為 `TxUnknownError`、例外訊息全文 `str(TxIncompleteError)`（`tx incomplete: <outcome> <n>/<m> <error>`）需 `LoginReport` 帶出 TxResult；計畫 Task 7 表「`Aborted(TxIncomplete)` → ATTACHED＋PROMPT_UNAVAILABLE＋`tx_incomplete`」與 Python 衝突，以 Python 為準，Task 7 實作時修訂該表 |
| Opus-1／grok-3 | Medium | 零寬重複上限 0（`(){0}`、`(){0,0}`、`(){0}+`）被譯成可參與 | **fixed**：上限 0 輸出 `(?:|(?!)body)`（群組保留、永不參與）；回歸 `event_matcher::groups_zero_width_zero_max_and_lookaround_body`（11 組 CPython 3.12 實測＋`(){0}\1(a)` 不匹配） |
| Opus-2 | Medium | lookaround 本體的可選重複 `(?=(a))?(a)` 被譯成 `(?:(?=(a)))?`，fancy-regex 拒收 | **fixed**：可選以交替表達（greedy `(?:body|)`、lazy `(?:|body)`、possessive 外包 `(?>…)`）；同上回歸 |
| grok-4 | Medium | quiet 略過帳密解析用 `quiet_active`，console unlock 在 Python 仍解析並登入 | 已於 c11aa89（Task 4）修正：attach 改傳 `boot_quiet_blocks_probe`；回歸 `session_core_quiet_p7::console_unlock_attach_during_quiet_still_starts_login` |
| Opus-6 | Medium | §0.6 第 3 列（session_manager detect）未退役卻宣稱完成 | 已於 94526b4 完成（merge P5-T16 後）；`changelog.d/rust-p7-retire-slice21-gate.md` 已同步 |
| Opus-5／grok-2／Opus(94526b4)-1 | Medium | L1 鎖非 add-only，`s21_core.windows` 淨減 2；LOCAL-WINDOWS-TODO 無樹內追蹤 | **fixed（部分）**：`s21_core.windows` 以 add-only 還原兩個舊 nodeid（627 筆，Windows gate 會報缺測直到原生替換）；新增樹內追蹤 `docs/rust-refactor/reviews/p7-local-windows-todo.md`（P7-LW-1～3）。**BLOCKED-OWNER P7-BO-1**：linux 的逐名替換需擁有者核准並加入 CI `--allow-removal` 白名單（本分支不自行修改 CI） |
| Opus(c11aa89)-1 | Medium | console unlock 在 quiet 中起再探／reboot recovery run 時把 fence `quiet_active` 清成 false，救援鍵／self-test 被放行 | **fixed**：`begin_login_generation` 依現況重設 fence（`quiet_active` 保留、只豁免登入 probe）；回歸 `console_unlock_reprobe_during_quiet_keeps_recover_key_blocked`（修前紅） |
| Opus(c11aa89)-2 | Medium | Task 4 計畫測試缺列 | **fixed**：補 `passthrough_reattach_clears_previous_profile_quiet_and_reconfirm`（#17）、`confirm_ready_clears_all`、`bootloader_tail_blocks_clear`、`quiet_rx_runs_during_foreground_busy`、`execute_time_quiet_has_retry_after_s`（37.26→37.3）、`rpc_human_source_is_treated_as_agent`、`passthrough_banner_restarts_quiet_window`、`passthrough_banner_never_arms_quiet_both_templates_both_states`（含 passthrough_ready_never_gated）、`stale_login_finished_keeps_pending`。#17 暴露的 detach 差異（Rust 每次 stop 清 quiet，Python detach 保留、只有 passthrough 重掛清）一併 **fixed**：stop 不再清 quiet，重開時依現況同步 fence；passthrough 重掛 `reset_passthrough_boot_gate`；回歸 `detach_keeps_quiet_and_reattach_within_window_is_zero_tx`（修前紅）。`banner_arm_bumps_fsm_gen_once_and_invalidates_human_input` 全名仍為 BLOCKED-UPSTREAM P7-BU-1 |
| Opus(c11aa89)-3 | Low | passthrough／stale 測試斷言弱 | **fixed**：見上（2 template × 2 狀態、pending 下的舊世代） |
| Opus(c11aa89)-4 | Low | `submit_gate_reject`／`execute_gate_reject` 未被 production 使用；`truthy` 非 Python truthiness | **fixed**：SessionCore submit／execute gate 改呼叫兩函式（刪除內嵌重複）；`truthy` 改用 `pycompat::py_truthy` |
| Opus(c11aa89)-5 | Low | reboot recovery／再探改用 `boot_quiet_blocks_probe` 無 console unlock 測試 | 再探：`console_unlock_reprobe_during_quiet_keeps_recover_key_blocked` 覆蓋；reboot recovery 的 console unlock 案 **deferred（Task 7 reboot 收斂表測試）** |
| Opus(be89081)-1 | Low | merge 時改了 P5 `session_attach_paths` 子案斷言、`GATED_PROFILE` 名不符實 | 已改名 `POST_LOGIN_PROFILE`；null 子案列 BLOCKED-UPSTREAM P7-BU-3 |
| Opus(be89081)-2 | Low | production executor 的 P6 效果為 `todo!` | 列 P7-BU-4（P6 合入前 SessionCore 不產生） |
| Opus-7 | Low | `post_login_cmd_profile_logs_in` 未驗 TX 序列 | **fixed**：斷言 `\n`、帳號、secret、`sh`、nonce probe 依序送出、secret 不進 WAL；補對照 `post_login_cmd_without_shell_prompt_does_not_reach_ready`（POST_LOGIN_CMD_TIMEOUT＋DETACHED） |
| Opus-8 | Low | `unsupported_profile_behavior` 失效宣告 | 加註「2.3 起已無適用 feature，僅留存紀錄」（鍵為 06 T0 契約，不刪） |
| Opus-9／grok-5 | Low | 驗證證據不可追溯 | 新增 `docs/rust-refactor/reviews/p7-evidence/`（失敗清單＋sha256＋成因說明） |
| （自查） | — | Task 5 的自動重探使 `lease_selftest::selftest_unresponsive_counts_attempt` 偶發轉紅（READY＋pending 3 s 安靜後被 tick 自動確認 probe 降為 ATTACHED） | **fixed**：該測試迴圈持續送空白 RX 讓自動重探不介入（Python `_rx_idle_enough` 同樣擋）；sw-core 連跑 3 次全綠 |

## 第 3 輪：p7-cloud/c11aa89_92e0223（Opus）

| # | 嚴重度 | 發現 | 處理 |
|---|---|---|---|
| H-1 | High | 自動重探中 quiet arm 推進 fsm_gen，`auto_reprobe_finished` 以 `login_gen_current` 丟棄原世代結果 → `login_run`／inflight 永久殘留、不再重探、掛上的手動 attach 永不回覆 | **fixed**：auto run 只比對 run 自身 `(epoch, fsm_gen)`，結果一律回填；世代被擋 probe 的 quiet 推進時視為 `Aborted(Refused(Quiet))`（Python `_login_tx_refused` → probe_refused：READY 保留、其餘 ATTACHED＋PROMPT_UNAVAILABLE），經 `_finish_probe_reprobe` 記 attempt、清 inflight，再重派等待者。回歸 `session_core_reprobe_p7::quiet_arm_during_auto_reprobe_settles_and_resumes`（修前紅） |
| M-2 | Medium | pending 中確認失敗的 `ready_reconfirm_attempts += 1` 無測試覆蓋 | **fixed（屬實）**：golden 042／043 的 session 仍為 READY（Python 視為 became_ready），未覆蓋失敗分支。`p7_sm_goldens.py` add-only 新增 044（ATTACHED＋pending、attempts 2→3）、045（attempts 4→5 落 failed），重新擷取；session_core 測試補 `quiet_for_test().attempts == before + 1`。反證：刪 `q.attempts += 1` → `reprobe::prepare_decisions_match_python_golden` 紅 |
| L-3 | Low | `reprobe_tick` 在 login run／前景命令存在時不呼叫 `prepare`，`next_reprobe_at` 不前移 | **fixed**：照常 `prepare`，得到動作即清 inflight 並丟棄（等價 worker 取不到 probe 鎖） |
| L-4 | Low | `ReprobeAction::Attach` 在 `known_real_path` 為 None 時已記 attempt 卻停在 DETACHED | **fixed（不可達）**：`device_present` 與 `known_real_path` 一律成對設定／清除，`prepare` 只在 device_present 時回 Attach；改 `debug_assert!`＋註解 |
| L-5 | Low | `human_active_blocks_reprobe` 只在純函式層覆蓋 | 已登記 BLOCKED-UPSTREAM P7-BU-2（`p7-local-windows-todo.md`）；P6 合入後補 actor 層測試 |
| stall L-1 | Low | `after.saturating_sub(before)` 將倒退計數視為零 RX | **fixed**：`checked_sub(..).unwrap_or(1)`（Python 負值 `!= 0` 不翻轉）；回歸 `transport_stall::refine_regressing_rx_counter_is_not_zero_rx`（修前紅） |
| stall L-2 | Low | `canonicalize` 整條失敗即退回原路徑，與 `realpath(strict=False)` 不同 | **fixed**：`realpath_lenient` 解析最長存在前綴後接回其餘段；回歸 `usb_topology::busid_resolves_existing_prefix_when_device_link_missing`（修前紅） |
| stall L-3 | Low | `with_filename` 未跳脫非 printable Unicode | **deferred**：sw-pal 不得依賴 sw-proto（arch §1.3），完整 `str.isprintable` 表需另行移植；檔名含 C1／非 printable 字元為邊角，Task 19 收斂 |
| stall L-4 | Low | `usb_windows_is_always_none`、`fs_ops_p7::*` 未登記 Windows gate | **deferred（LOCAL-WINDOWS-TODO P7-LW-2，已登記）**：Windows 原生證據到位同 PR add-only 登記 |

## 第 4 輪：p7-cloud/92e0223_76de0cf（Opus）

| # | 嚴重度 | 發現 | 處理 |
|---|---|---|---|
| bea2d8e-1 | Medium | quiet 跨 detach 保留後，重掛的 fence 設 quiet 卻未為新 epoch 排到期計時器；quiet 到期後 self-test／救援鍵仍被 fence 拒絕 | **fixed**：`adopt_opened_bridge` 同步 fence 後若 quiet 生效，排 `BootQuietExpired{新 epoch}`；回歸 `session_core_quiet_p7::reattach_within_quiet_fence_expires_with_window`（修前紅） |
| d5b2dd6-1 | Medium | H-1 修法把非現行世代的所有結果覆寫為 probe_refused；quiet arm 後 FSM 回 Ready 時 Python 為 READY（#139） | **fixed（屬實，前輪修法過度）**：刪除覆寫，auto run 只比對 run 自身世代、結果原樣交給 converge（被 fence 撤銷時 FSM 本就回 `Aborted`）；回歸 `quiet_arm_during_auto_reprobe_ready_result_is_ready`、`quiet_arm_during_auto_reprobe_not_ready_keeps_failure_code`（修前皆紅） |
| d5b2dd6-2 | Low | H-1 測試注入 Cancelled 無法約束覆寫 | **fixed**：同上兩個 Ready／NotReady 變體 |
| d5b2dd6-3 | Low | L-3 行為無回歸測試 | **fixed**：`reprobe_tick_while_manual_probe_busy_advances_next_reprobe_at`；突變 M9（busy 即早退）→ 紅 |
| d5b2dd6-4 | Low | 懸空 `device` symlink 未依內容展開 | **fixed**：`realpath_lenient` 改為逐位元組移植 CPython 3.12 `_joinrealpath`（seen 快取、迴圈原樣回傳）；回歸 `usb_topology::busid_follows_dangling_device_symlink`（修前紅，CPython 實測 `…/1-5/1-5:1.0/ttyX`） |
| bea2d8e-2 | Low | 手動再探例外以 Err 回覆所有合併的 waiters | **fixed**：只回覆第一個，其餘以新世代 `reprobe_existing_bridge` 重派；回歸 `session_core_login_exc_p7::manual_reprobe_exception_only_fails_first_waiter_others_reprobe`（修前紅） |
| bea2d8e-3 | Low | 測試把型別名凍結為 EXCEPTION message | **fixed**：`login_quiet::disk_full_blocks_login_probe_but_not_agent_cmd` 改只斷言例外穿出並註明已知偏離（Task 7 `LoginReport` 帶出 TxResult 後補 `str(exc)` 全文） |
| bea2d8e-4 | Low | `s21_core.windows` 加回的兩個舊 nodeid 樹內已不存在 | 已登記 P7-LW-1／P7-BO-1（擁有者核准 `--allow-removal` 後與原生替換同 PR 處理）；無程式變更 |
| 76de0cf-1 | Low | 證據 README 未交代 +4／−1 與重跑紀錄 | **fixed**：README 補「d5b2dd6 列的完整差異」段（完整 +/−、兩次重跑命令與結果） |
