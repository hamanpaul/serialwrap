# wp510r 第 24 項：未到終態就等待的統一安全網

起點：`05260206ba71b3164c969b02e67144d0ff2f621e`；分支：`feature/rust-refactor-p8`。範圍只含第 6 次整包審查的一項 High 與 2026-11-12 擁有者要求的統一安全網。

## High 與修正

退休失敗原本以 `parts.is_some()` 產生 `closing`；watcher 已把 handle 拿走、帳本仍在 Closing 時，這個判斷會漏等。release 的 by-id 認領又先排除同一 actor 擁有的紀錄，兩個缺口疊加後會在關閉回條前回 `ok:true, already_released:true`。

新增 `CustodyLedger::pending_for(by_id)`：Opening、Held、Taken、Bridged（包含 worker 已取出的窗口）、BridgeDraining、Closing 都回待終態紀錄；CloseFailed 與 unconfirmed 回失敗狀態。所有權與終態只有帳本一份；actor 每次生命週期事件從 executor 取得唯讀輸入，事件結束即丟棄這份觀測。等待仍由 actor 原有的 retired_drains／awaits、ReleaseWait 與 CustodyPoll 承擔。

`RetirePlaceholder` 驗證失敗改依查詢結果等待；release 的認領不再排除自身 owner。安全網補登記的 token 只等待，不以它取消別的 owner；真正關閉仍由原所有者或既有 ClaimForStop／ClaimByIdForRelease 協定負責。自己的關閉通知保留原本通知時點，不加一條提早送達的 watcher。失敗終態永久保留，release 依 T-1 等首次期限，之後沿用；非 release 停止已失敗時，也不能讓下一個 release 跳過自己的 T-1。

Python 對照以本輪 `git show origin/main:sw_core/session_manager.py` 核實：3104–3125 行找 attach 作業並記首次期限；3188–3209 行等 done 後才回成功；4320–4332 行關閉失敗不設 done；4339–4364 行在安全關閉後完成 RELEASED、清 drain、落盤再設 done；3614–3622 行的失效 attach 先清理再回覆。

## 回覆與開埠判定盤點

| 位置（函式名稱為定位準據） | 原本依據 | 經安全網後 |
|---|---|---|
| `Shared::claim_by_id_for_release_locked` | 自身 owner 略過；部分分支以拿不到 parts 當 NoHandle | 所有 owner 先查帳本；自身紀錄也回等待 token／首次期限；只有確實成功認領的空 Held 才可結算 NoHandle，其他關閉者持有時維持等待 |
| `ManagerExec::retire_placeholder` 的 Invalid | `parts.is_some()` 決定 closing | 在同一鎖區段查 pending_for；watcher 已接手仍回 closing，actor 保留 attach、登記 token、啟動 CustodyPoll |
| `release_request_inner` 已 RELEASED 的早退 | 沒有本地 pending／retired 就 already_released 成功 | `release_reply` 重新檢查事件的帳本輸入；漏登記的 token 進 awaits，建立 release 等待 |
| `begin_stop`、`finish_stop_without_worker` | 無自身 bridge 可直接完成 release／shutdown | 先納入帳本義務，完成前保留停止待決，不落盤、不回成功 |
| `finish_pending_open_stop`、`try_converge` | 本地開埠與 retired 表已收斂 | 額外核對帳本；未終態就保留待決、以 CustodyPoll 補收 |
| `worker_stopped` | 自身回條成功且本地 retired 空即可完成 | 自身回條不能代替帳本回條；轉入同一待決停止，沿用期限 |
| `bridge_opened`、`drain_opened_bridge` | 只有原本的 custody_open 來源轉換 token；落盤中的移交走自身 StopOp | 依查詢到的 Bridged token／epoch 配對，無論一般開埠或保管開埠，都把 token 原子轉成退休 epoch 或自身 StopOp；配對回條前不得完成 release，也不等 ack tombstone 的假關閉 |
| `state_committed(Release)` 與其他成功回覆出口 | 持久化成功或已 RELEASED 即回成功 | 全部透過 release_reply；最後一刻發現新義務也等待，保留原呼叫端的 already_released 形狀 |
| `on_event_with_custody` 的 attach 回覆出口 | 各個早退／開埠失敗／取消／落盤回覆各自結束 | 集中辨識正在完成的 attach channel 與會回成功的 attach 請求，未終態先保留回覆；終態後才補發當下 session／drain 欄位 |
| `custody_terminal` | 配對通知直接結算 | 帳本若仍 Pending，同 token 的成功提示也不算證據；維持等待並補收 |
| `register`、`register_with_token`、`is_open_blocked`、`register_plain_open`、`spawn_attach_with` | 各自看 phase／unconfirmed | 共用 pending_for；未終態不新開埠，失敗永久阻擋；Held 只允許依原 token／路徑精確認領原 handle |
| `SessionManager::bind` 的自身在途輸入 | 直接看 phase.is_in_flight | 由同一查詢取得自身在途 token，再沿用既有取消與 BindWait |
| `publish_retired_drains`／manager shutdown 判定 | fence 只發布 retired 表；RELEASED 上的自身 StopOp 可能被略過 | 把自身 StopOp、pending_open_stop 一併發布；義務改變才更新 fence，GetState 不會等待 TX fence |

具名的所有權移交維持定稿設計與凍結斷言：正常 `AdoptBridge` 必須在**本事件**明確發出，並配對帳本 Bridged 的 epoch；既有 conn_epoch 相同不構成確認接手。轉成 actor 的退休排空時，等的是原 epoch／stop_id，不能再以 ack tombstone 假裝關閉。DR-4 的 attach 落盤失敗仍可在這個配對責任移交後回例外，release 繼續等真實回條。成功佔位退休與 D2 parked 則在 manager 鎖內明確交出責任；它們回報 attach 的接受結果，不等待後繼 worker 的整段生命週期。拒絕另一筆 attach、RELEASED 的 session.attach 建議回覆，也不結束現有資源作業。

沒有新增 RPC、ControlEvent、Timer 或效果類別；唯讀帳本輸入與既有資源收尾同步。CustodyPoll 的 Open／Pending／Permanent 分類不變：Open 執行、Pending 延後至撤銷重播、Permanent 照常收尾；不啟動登入或其他新工作。

定位：帳本入口 `crates/sw-core/src/custody.rs:539`；actor 分派 `crates/sw-core/src/actor.rs:740`；manager 查詢 `crates/sw-core/src/session_manager.rs:5383`、by-id 認領 `:1460`、退休重驗 `:5152`；core 統一出口 `crates/sw-core/src/session_core.rs:1143`、等待登記 `:2650`、release 回覆 `:2695`、attach 回覆 `:2712`、shutdown 屏障 `:4980`。以上行號屬本提交；盤點以函式名稱為準。

## 紅燈、接線與回歸

High 的三支 ServiceRig 回歸在產品來源完全維持 `05260206` 時均失敗於「回條前 release 不得成功」，0 passed／3 failed；輸出 `target/s2-par/wp510r/i24-red.log`。測試經正式 device.attach RPC 暫停 RetirePlaceholder，先把 release 排入 safety lane，再讓正式 update_devices 取走 handle 並停在真實 FakeSerial close，沒有用偽造終態代替關閉；成功、逾時後遲到成功、失敗三條都先放行與 join 再斷言。

安全網另有六個 core 節點：所有未終態 phase 的普通／already_released 回覆、自身 bridge 回條、最後持久化回覆、attach 開埠失敗、現有 bridge 的 attach 成功回覆、失敗／未知終態。將 session_core 換回起點、只加接受新測試輸入但直接呼叫舊 on_event 的轉接後，六支均轉紅，0 passed／6 failed；`i24-safety-red.log`、`i24-core-baseline-adapter.diff` 保存實際差異，測試後逐位元組恢復修正來源。帳本單測另涵蓋各 phase 的重開拒絕、錯 token 不解除、成功終態可重開與失敗／未知永久阻擋。

安全網整合時再補三個會紅的邊界：一般 OpenBridge 以配對 epoch 承接 release（`i24-plain-bridge-red.log`，原本只留下 token 並逾時）；被延後的 attach 成功遇失敗終態必須拒絕（`i24-failed-attach-red.log`，原本回 ok:true）；attach 落盤期間加入 release 時，把 token 轉成自身 StopOp 的真實回條（`i24-own-transfer-red.log`，原本配對回條後仍等 token）。三支各為 0 passed／1 failed，修正後原斷言通過，未改任何既有凍結斷言。

帳本查詢只在資源生命週期事件執行，避免普通 Tick／RX 因取得 manager 鎖而堵住唯讀 RPC。新增的 actor 測試把查詢阻住，要求 Tick／GetState 仍可回覆、Shutdown 必須查詢；修正前 0 passed／1 failed（`i24-observer-red.log`），修正後通過。測試查詢 helper 也先釋放 manager 鎖再等 actor，防止測試本身造成循環等待。現有 `get_state_does_not_wait_for_tx_fence_held_by_os_submit` 仍通過；沒有用新增安全網犧牲原本的唯讀行為。

合計新增 14 個 Rust 節點：8 core 收尾測試、1 帳本測試、1 actor 查詢測試與 4 ServiceRig 測試。凍結鎖以 `regen_lock.py --l1-add-only` 登記：新增 14、移除 0；既有節點名稱、selector、斷言均保留。

正式接線：`Service::rpc(device.attach / device.release)` → `SessionManager::route_with_selector / route` → actor safety／control lane → `ManagerExec::pending_custody` → `CustodyLedger::pending_for` → `SessionCore::on_event_with_custody` → `RetirePlaceholder / ClaimByIdForRelease` → watcher／closer 真實 close → 帳本終態 → `CustodyTerminal` 或 `CustodyPoll / PollCustody` → 收斂、持久化、RPC 回覆。ServiceRig 使用記憶體 UART、state 與 WAL；attach 失敗以 RPC_WIRE 逐位元組比對原 selector 與完整錯誤形狀，並驗證開關各一次、零 TX、prepared／awaits 清空、失敗保持阻擋。

本輪不需實體 regression case；交錯由 ServiceRig 與記憶體替身確定性覆蓋。沒有 Windows 原生、com0com 或硬體證據。

## 不成立主張

無。第 6 次審查的單一 High 接受並修正。

## SPEC-GAPS

本輪沒有需要新增裁決的缺口。沒有修改 `attach_open_failed` 的錯誤回應建構，brief 所載一般 session 的 DEVICE_STILL_HELD 多 session 鍵與 S24 五項既有黑箱失敗未在本輪重跑，保留給對應工作包；本輪只改回覆前的等待，未宣稱修復其輸出形狀。其他分支的 finding 不處理。擁有者／硬體項目維持 BLOCKED-OWNER。

## 既有碼待辦與驗證

所有原始輸出位於 `target/s2-par/wp510r/`。最終來源以 `i24-delivery-source.json` 的逐檔 SHA-256 固定，最終 gate 完成後再次核對一致。Rust 測試依序在本工作樹前景執行，指定 `--test-threads=4`、`--no-fail-fast`；`TMPDIR=/proc/$$/cwd/target/t` 指回本樹，沒有建立 namespace 或自製 runner。

| 檢查 | 結果 | 輸出 |
|---|---|---|
| 受影響 crate：`cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast -- --test-threads=4` | **FAIL**：2132 passed、1 failed、7 ignored；失敗為下列已在起點重現的 DR1-d | `i24-verify-affected.log` |
| `cargo test --workspace --features test-fakes --no-fail-fast -- --test-threads=4` | **FAIL**：3470 passed、26 failed、51 ignored；全為下列既有 CLI 失敗 | `i24-verify-workspace-features.log` |
| `cargo test --workspace --no-fail-fast -- --test-threads=4` | **FAIL**：1629 passed、26 failed、46 ignored；相同 CLI 失敗 | `i24-verify-workspace.log` |
| 本項新增節點 | **PASS：14／14**；受影響 crate 與 workspace test-fakes 兩次均通過 | 上述兩份 test-fakes 輸出、`i24-delivery-results.json` |
| `cargo build --workspace --all-targets` | **PASS**，exit 0；警告集合與起點相同 | `i24-verify-build.log` |
| `cargo clippy --workspace --all-targets --features test-fakes` | **FAIL**，exit 101；起點同樣因 `sw-proto` 的 `clippy::never_loop` 中止 | `i24-verify-clippy.log`、`i24-baseline-clippy.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | **PASS**，exit 0；沒有新增警告 | `i24-verify-clippy-affected.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu`，另含 `--features test-fakes` 再跑一次 | **PASS**，兩次 exit 0；僅交叉編譯證據 | `i24-verify-windows.log`、`i24-verify-windows-features.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | **PASS**，exit 0 | `i24-delivery-cfg.log` |
| `python3 -m tools.contracts check` | **PASS**，ok:true、findings 0 | `i24-delivery-contracts.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 05260206` | **PASS**，鎖檔、集合、案例屬性與統計一致 | `i24-delivery-lock.log` |
| changelog fragment pytest | **PASS**，1 passed、2 marker warnings、0 skipped | `i24-delivery-fragment.log` |
| `python3 -m policy_check --repo .` | **PASS**，25 pass、0 fail、2 warn | `i24-delivery-policy.log` |
| `git diff --check` | **PASS** | 提交前執行 |

警告依「訊息＋檔案」去除重複後與起點比較：build 18→18、受影響 clippy 61→61、Windows all-targets＋test-fakes 32→32，新增集合皆空；詳見 `i24-warning-comparison.json`。沒有加 lint suppression。全 workspace clippy 的既有錯誤在 `crates/sw-proto/src/pyre/parse.rs:587`，不屬本項，未修改。

fragment 是獨立純檔案測試，執行 `python3 -m pytest --noconftest -q --basetemp=target/s2-par/wp510r/pytest-fragment-final tests/test_changelog_fragments.py`；停用全域 conftest 是為遵守不得探查 production daemon／狀態的限制，沒有修改測試或跳過案例。兩個 warning 是 conftest 未註冊的 `core_portable`／`memory_only` marker。policy 的兩個 advisory 為 R-19 工作流程解析提示、R-22 既有懸空文件引用，未把 warning 改成 PASS。

### 起點對照與保留的失敗

只在本工作樹暫時以 `05260206` 的 Rust 來源與測試執行對照，來源 diff 為空（`i24-baseline-source.diff`、`i24-baseline-source-final.diff`），之後核對 SHA-256 恢復本輪來源；沒有使用其他 checkout 或 reset。原始失敗全部保留為 FAIL，不以本項完成取代 gate 結果。

1. `sw-core::custody::test_dr1_d_release_not_enqueued_keeps_ledger_then_next_release_completes`：最終受影響 suite 失敗於預期 ok:true、實際 DRAIN_STALLED；起點單跑相同失敗（0 passed／1 failed，`i24-baseline-dr1d.log`，起點 `crates/sw-core/tests/custody.rs:2901`）。最終 workspace test-fakes 此案例通過；本輪不以「flaky」或環境推測放行，也沒有改它的斷言。
2. CLI 26 項：起點含／不含 test-fakes 皆 **117 passed／26 failed／3 ignored**（`i24-baseline-cli-features.log`、`i24-baseline-cli.log`），失敗集合與最終 workspace 完全一致。`sw-cli` 來源未修改；完整 id 如下。
3. 全 workspace clippy：起點同樣 exit 101、同一個 `sw-proto::pyre::parse` never_loop；受影響 crate 的起點 clippy 與最終 clippy 均 exit 0。

`sw-cli::mgmt_doctor`（6）：

```text
overall_ok_ignores_advisory_failures_only
python_runtime_nonzero_rc_is_unrunnable
pyyaml_import_failure_is_not_installed_with_fix
runtime_spawn_error_is_unrunnable_not_not_installed
runtime_timeout_is_unrunnable_not_not_installed
s1m241_doctor_code_mismatch_is_advisory
```

`sw-cli::mgmt_layout`（1）：

```text
locate_manifest_env_override_first
```

`sw-cli::mgmt_setup`（19）：

```text
s1m241_assessment_is_omitted_when_initial_ping_is_unreachable
s1m241_assessment_is_omitted_without_installed_fingerprint
s1m241_assessment_matching_ping_is_reported_once
s1m241_assessment_restart_after_unreachable_is_reported
s1m241_assessment_stale_systemd_pending_sudo_is_reported
s1m241_assessment_stale_systemd_restart_failure_is_reported
s1m241_assessment_stale_systemd_restarts_and_verifies
s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero
s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero
s1m241_failure_restart_error_preserves_python_hint_without_postcheck
s1m241_failure_retries_unreachable_then_reports_match
s1m241_setup_stale_daemon_without_fingerprint_is_restarted
s1m241_setup_stale_service_is_restarted_and_verified
s1m241_setup_system_scope_with_sudo_restarts_after_enable
s1m241_skips_decision_priority
s1m241_skips_flashing_without_force_before_materialize
s1m241_skips_no_restart_flag
s1m241_skips_no_restart_flag_preserves_mode_transition
s1m_c1_setup_verify_polls_reconciled_socket
```

開發中間版本曾出現 `sw-daemon::daemon_process::daemon_stop_during_force_recover_with_client_gone`、`sw-daemon::event_lifecycle::shutdown_aborts_long_handler_within_budget` 失敗。起點兩次同範圍均 37 passed／0 failed（`i24-baseline-runtime-1.log`、`i24-baseline-runtime-2.log`），因此**不列為已證明的既有失敗**。本輪補齊 actor 查詢範圍與關閉屏障後，固定來源的最終受影響 suite 及 workspace test-fakes 中，兩個案例均 PASS；不主張已查明中間失敗的單一根因。

不跑全套 pytest、slice gate、Docker 或出口證據 driver；沒有 push、PR、merge、部署。Python 基線、contracts、golden 與既有凍結斷言未修改。

## REMAINING

REMAINING: none
