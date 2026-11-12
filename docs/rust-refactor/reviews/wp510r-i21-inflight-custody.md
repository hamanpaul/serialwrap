# wp510r 第 21 項：在途辨識與遺失終態補收

起點為 `0b37df5d6099506af5b5cdaf23b58b113a060f3d`，分支 `feature/rust-refactor-p8`。範圍僅限第 4 次整包審查的兩項 Medium；本文件是實作與驗證回報，沒有另跑整包審查或出口證據 driver。

## 根因與處理

兩項屬於同一根因：帳本的資源義務仍在，但消費者以來源或局部階段判斷「是否還要等」。第一項只補收 `by_id_claim:true`，漏掉自身 `ClaimForStop` 的等待；第二項把一般開埠的在途限於 Opening／Taken，漏掉 actor 尚未確認接手的 Bridged。

| 第 4 次審查 finding | 結果 |
|---|---|
| Medium 1：自身保管紀錄關閉通知遇 Full 後沒有補收 | fixed（本提交）。`custody_claimed` 對自身等待啟動計時器；`custody_poll` 與排程共用 `pending_custody_tokens()`，涵蓋所有仍 Pending 的保管 token，不依 `by_id_claim` 區分 |
| Medium 2：一般開埠已 Bridged、actor 尚未接手時 bind 漏掉資源 | fixed（本提交）。bind 使用共用在途判定帶入原 token、寫取消並建立 BindWait；Bridged 的停止回饋為 Cancelled，BridgeDraining 為 Closing。BridgeOpened 到達時把 token 等待轉成同資源的 epoch／stop_id 排空等待，保留首次 release 期限；配對回條前不改綁、不進登入，BindWait 逾時後的遲到成功也不改綁 |

Python 依據皆以本輪 `git show origin/main:sw_core/session_manager.py` 核實：`:4335-4364` 關閉後完成 RELEASED、清 drain、落盤再設 done；`:3192-3215` release 等 done 後回覆；`:3676-3727` bind 取消在途、等 done／期限後重試或回 RECOVERING；`:3757-3813` 依 bridge／無 bridge 路徑改綁。D1／T-1、C3～C5 與 D6 依設計第 5 版及 `origin/docs/parity-amendments` 裁決，沒有改契約或 Python 基線。

## 在途辨識盤點

共用 `CustodyPhase::is_in_flight()` 明列 Opening、Taken、Bridged、BridgeDraining、Closing。Held 是可供 C1′ 認領的保管資源；CloseFailed 是永久阻擋的失敗終態，兩者分開處理。新增狀態會因完整 match 而要求明確分類。

| 消費者 | 本輪處理 |
|---|---|
| `CustodyLedger::is_open_blocked` | 共用在途判定，加上 CloseFailed／unconfirmed 永久阻擋 |
| `Shared::claim_custody_for_stop_locked` | 共用在途判定來取消對應 worker；各階段仍依自己的所有權執行關閉／等待 |
| `Shared::claim_by_id_for_release_locked` 的作業紀錄取消 | 共用在途判定，不在 Bridged 後漏寫取消 |
| `Shared::spawn_attach_with` | 共用在途判定，不在開埠、交接或關閉尚未收斂時另開 |
| `SessionManager::bind` | 共用在途判定，加上不可變 actor owner 身分核對；涵蓋 Bridged、BridgeDraining 與 Closing |
| actor 補收與排程 | 同用 `pending_custody_tokens()`；收集所有 Pending 保管等待，token 去重；Failed 不再反覆補收 |
| `place_session_custody_locked`、`mark_open_job_taken`、`open_bridge_custody`、`register_plain_open`、`claim_parked`／`claim_for_release`／`claim_bridged_worker` | 這些是特定所有權轉換，保留 Opening／Taken／Held／Bridged 的精確合法來源檢查，不能用「任一在途」放寬 |
| NoHandle 清理、`finish_worker_op` 的 Reaper Closing 屏障、他人 Closing 的 release watcher、測試用 handoff 查詢 | 保留各自的特定狀態或 owner 條件；不是一般在途存在性判斷。`by_id_claim` 仍用於來源與失敗收尾語意，移除它作為補收篩選條件 |

沒有新增 RPC、事件或效果。既有 CustodyPoll 在 Open 執行、Pending 延後並於撤銷後重播、Permanent 照常收尾；ClaimForStop、PollCustody 與關閉／回收仍屬資源收尾。測試鉤子僅在 `test-fakes` 建置中存在。

## 先紅後綠與端到端接線

新增六個測試，沒有更動任何既有凍結節點的步驟、名稱或斷言。

| 新節點 | 未修正邏輯上的紅燈 |
|---|---|
| `daemon::dispatch::wp510r_i21_rpc_own_custody_terminal_full_poll_completes_release` | 關閉終態 Closed 已在帳本、實際 try_send 因 Full 遺失，補收後仍無法到 RELEASED |
| `daemon::dispatch::wp510r_i21_rpc_bind_bridged_waits_close_then_applies` | bind 立即回 SESSION_RECOVERING，沒有取消／等待 |
| `daemon::dispatch::wp510r_i21_rpc_bind_bridged_timeout_cancels_late_login` | 同上，未進入 BindWait 期限語意 |
| `core::drain_release::wp510r_i21_own_custody_poll_open` | 應補收 token 77，實際沒有 PollCustody |
| `core::drain_release::wp510r_i21_own_custody_poll_pending` | 撤銷 Pending 後仍沒有 PollCustody |
| `core::drain_release::wp510r_i21_own_custody_poll_permanent` | Permanent 下沒有 PollCustody |

紅燈輸出：`target/s2-par/wp510r/i21-red.log`（0 passed／3 failed）；`i21-red-freeze.log`（0 passed／3 failed，暫存並換回起點 session_core 來源後執行，結束完整還原）。同組綠燈為 `i21-green.log`、`i21-green-freeze.log`；增加 epoch 與錯誤碼斷言後的 RPC 最終重跑為 `i21-final-focused.log`（3 passed）。

通知遺失接線：`Service::rpc("session.bind")` → `SessionManager::bind` → `AcceptCustody`／`ClaimOffered` 以同 token 接手 parked，停在 DeviceOnline 之前；`Service::rpc("device.release")` → `ReleaseWithAck` → actor 停止 → `ClaimForStop` → 獨立 closer → `finish_custody_close`。ServiceRig 確認自身 owner、Taken、awaits 與首次 DRAIN_STALLED，再暫停 actor、灌滿 control lane；實際 try_send 的 Full 有明確觀測。恢復後推進 CustodyPoll，經 `PollCustody` 的立即回饋補收成功終態，完成 RELEASED、清 false/null、落盤；再次 release 以 RPC_WIRE 逐位元組比對 already_released。handle 關閉一次、開埠次數不增、零登入 TX。

一般開埠接線：`Service::rpc("device.attach")` → `OpenBridge` → `register_plain_open` → `open_bridge_with` → 帳本持有真 IoWorker 的 Bridged，暫停在 BridgeOpened 送出前；`Service::rpc("session.bind")` → `SessionManager::bind` → `BindWithAck` → `bind_request` → `ClaimForStop`／BindWait。放行後 `bridge_opened` 把等待轉成配對 epoch 的退休排空，`AdoptBridge`／`BeginDrain` → 真 close → WorkerStopped → `settle_retired`／`resume_bind_waits`。回條前保留該 epoch、不改綁；期限內成功才改綁並落盤，成功 RPC 全形狀逐位元組比對。另一支等滿 BindWait，驗 SESSION_RECOVERING、RECOVERING、true/g，放行後確認舊 handle 關閉、等待清空但不套用改綁；兩支皆無登入 TX、不另開舊埠。這兩項由記憶體 UART 與正式 Service／manager／actor 路徑覆蓋，無需實體硬體 regression case。

## 不成立主張

無；兩項 Medium 均接受並修正。

## SPEC-GAPS

本輪沒有需要新裁決或擴權的缺口。既有 S24 `test_s24_device_handoff.py` 的五項 Rust 失敗未重跑：本輪沒有修改 `attach_open_failed` 或 DEVICE_STILL_HELD 的輸出形狀，該既有基線差異未涵蓋。owner／硬體項目維持 BLOCKED-OWNER；沒有 Windows 原生／com0com 實跑證據，Windows gate 僅為交叉編譯。

## 既有碼待辦

workspace 的 26 項 CLI 失敗保留為 FAIL，沒有修範圍外碼。將本輪五個 Rust 修改檔完整暫存到 target，逐檔換回 `git show 0b37df5d:<path>`，`i21-baseline-source-diff.log` 為空；前景重跑三個失敗檔，仍為相同 26 個 id（117 passed／26 failed／3 ignored）。結束後逐檔位元組比對，已恢復本輪來源。`i21-failure-comparison.json` 記錄兩邊逐名相同；不宣稱已查明每個失敗根因。

逐名失敗與最終 gate 數字見下方驗收結果。

## 驗收結果

全部命令在本 worktree 前景執行並等待結束，原始輸出位於 `target/s2-par/wp510r/i21-*`。

| gate | 結果與輸出 |
|---|---|
| 新 RPC 回歸 | 3 passed／0 failed；`i21-final-focused.log` |
| 新補收／凍結回歸 | 3 passed／0 failed；`i21-green-freeze.log` |
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast` | **2114 passed／0 failed／7 ignored**；core 含 doc-test 1722 passed／1 ignored、daemon 392 passed／6 ignored；`i21-test-affected.log` |
| `cargo test --workspace --no-fail-fast` | **FAIL：1629 passed／26 failed／46 ignored**；三個 CLI 目標的起點失敗逐名相同，見下表；`i21-test-workspace.log` |
| 起點 CLI 三檔重跑 | **FAIL：117 passed／26 failed／3 ignored**；`i21-baseline-cli.log`，來源差異檔為空 |
| `cargo build --workspace --all-targets` | exit 0；`i21-build.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | exit 0；`i21-clippy.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu` | exit 0；`i21-windows-check.log`，僅交叉編譯 |
| Rust 新增警告 | **0**；起點／本輪的 build 皆 18、Clippy 皆 61、Windows check 皆 32 種診斷；以警告文字與來源檔名去重、排除摘要及行號位移，比對新增／移除均為空；`i21-warning-comparison.json`，基準為本輪直接換回的 `0b37df5d` 來源 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0；`i21-cfg.log` |
| `python3 -m tools.contracts check` | ok=true、findings 0；`i21-contracts.log` |
| L1 `regen_lock.py --l1-add-only` | s21_core.linux 新增 **6**、移除 **0**；其他集合與 selector 不變；`i21-l1-add.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 0b37df5d` | exit 0；案例與 L1 結構／基準檢查通過；`i21-lock-check.log` |
| fragment pytest | **1 passed**；`i21-fragment.log` |
| `python3 -m policy_check --repo .` | **25 PASS／2 WARN／0 FAIL**；R-19、R-22 advisory；`i21-policy.log` |
| 增量差異 | `git diff --check` 通過；不改 Python、contracts／golden 或既有測試斷言 |

26 個 workspace／起點共同失敗 id：

| 目標 | 失敗 id |
|---|---|
| `mgmt_doctor`（6） | `overall_ok_ignores_advisory_failures_only`、`python_runtime_nonzero_rc_is_unrunnable`、`pyyaml_import_failure_is_not_installed_with_fix`、`runtime_spawn_error_is_unrunnable_not_not_installed`、`runtime_timeout_is_unrunnable_not_not_installed`、`s1m241_doctor_code_mismatch_is_advisory` |
| `mgmt_layout`（1） | `locate_manifest_env_override_first` |
| `mgmt_setup`（19） | `s1m241_assessment_is_omitted_when_initial_ping_is_unreachable`、`s1m241_assessment_is_omitted_without_installed_fingerprint`、`s1m241_assessment_matching_ping_is_reported_once`、`s1m241_assessment_restart_after_unreachable_is_reported`、`s1m241_assessment_stale_systemd_pending_sudo_is_reported`、`s1m241_assessment_stale_systemd_restart_failure_is_reported`、`s1m241_assessment_stale_systemd_restarts_and_verifies`、`s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero`、`s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero`、`s1m241_failure_restart_error_preserves_python_hint_without_postcheck`、`s1m241_failure_retries_unreachable_then_reports_match`、`s1m241_setup_stale_daemon_without_fingerprint_is_restarted`、`s1m241_setup_stale_service_is_restarted_and_verified`、`s1m241_setup_system_scope_with_sudo_restarts_after_enable`、`s1m241_skips_decision_priority`、`s1m241_skips_flashing_without_force_before_materialize`、`s1m241_skips_no_restart_flag`、`s1m241_skips_no_restart_flag_preserves_mode_transition`、`s1m_c1_setup_verify_polls_reconciled_socket` |

fragment 測試使用 `python3 -m pytest --noconftest -q tests/test_changelog_fragments.py --basetemp target/s2-par/wp510r/i21-pytest-tmp`；避免載入會探測 production daemon／設定／狀態的全域 conftest。兩筆 UnknownMarkWarning 來自未載入 conftest 的 marker 定義，沒有改判準或抑制警告。未觸及其他 Python 測試，沒有跑全套 pytest、slice gate、Docker 或出口證據 driver。

需要磁碟暫存的 Rust 測試以 `TMPDIR=/proc/<前景 shell PID>/cwd/target/t` 指回本 worktree，讓 Unix socket 保持短路徑；沒有建立 namespace 或自製隔離 runner。ServiceRig 的 UART、WAL、state 均在記憶體。沒有接觸 production daemon、真實序列埠、其他 worktree 或 thala 容器，沒有 push。

## REMAINING

REMAINING: none
