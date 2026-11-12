# wp510r 第 22 項：token 轉退休 epoch 的排空鏡像

起點 `22acfbb88c0afae65574e68519f4e31be6842314`，分支 `feature/rust-refactor-p8`。只處理 `P8-22acfbb-1`；未跑整包審查或出口證據 driver。

## Finding 處理

| 項目 | 結果 |
|---|---|
| P8-22acfbb-1（High） | fixed（本提交）：token→epoch 替換期間保持待排空鏡像為 true，加入 epoch 後統一發布，配對成功回條才解除該等待 |
| A 段 High／Medium | 沒有標「未修」且須本輪處理的項目；前輪已結案項沿用，未重做整套 review |
| P8-057a065-4（Medium） | 依既有裁決不處理 |
| A 段未修 Low | 依指令不修，留給編排者的 low-backlog |

## 根因、修正與發布點盤點

`SessionCore::bridge_opened` 把被 bind 取消的 custody token 等待換成退休 epoch。舊版先從 `retired_drains` 移除 token，立即發布 false，再由 `retire_opened_bridge` 加入 epoch、發布 true。兩次 fence 臨界區之間，shutdown 能在先前取得的 RELEASED 快照下讀到 false，跳過 session 的 Shutdown／回條／actor 退出等待。之後 AdoptBridge 移除帳本紀錄，Phase 2 也無從補收該 worker。

修正只移除這次中間發布：替換期間鏡像保持 true，`retire_opened_bridge` 加入 epoch 後統一發布。原本的首次期限、PendingOpenStop、release 收尾與成功回條結算都保留。

已盤點所有 `retired_drains` 的 push／remove／retain 與鏡像寫入：

- `release_claimed`、`custody_claimed`、`retire_result(Invalid)`、`retire_opened_bridge` 都在新增等待後發布，沒有先清除再重設。
- `settle_retired` 只在成功回條時移除等待並發布；失敗回條保留等待及未確認關閉集合。
- `abandon_failed_claim_release` 是既有的失敗 by-id claim 放棄路徑：首次期限已過、沒有 release waiter、沒有開埠／attach 在途時，撤下 actor 的重複 claim 等待。關閉義務仍在 manager 的 CloseFailed 帳本及未確認集合，沒有轉成 epoch 或重新發布 true，並非本項的替換窗口。本輪不改這條路徑。
- `publish_retired_drains` 是唯一的執行期鏡像寫入點；Fence 建構時的 false 是初始值。未發現其他先清除、後重設的非原子發布，沒有額外行為修正。

Python 依據以本輪 `git show origin/main:sw_core/session_manager.py` 核實：`:1181-1207` 在同一把鎖下收集 attach 作業、追蹤 attach bridge；`:1228-1239` 等待並重收未決作業。符合定稿設計 C3、§3.1.3 的收斂規則及 §3.1.4 W3′。沒有修改 Python 基線、contracts、golden、既有測試或凍結節點的斷言。

沒有新增事件、請求或效果。沿用既有 BridgeOpened、AdoptBridge、BeginDrain、WorkerStopped 與 Shutdown 的 Open／Pending／Permanent 分類；新增鉤子只存在於 test-fakes，於 fence／manager 鎖外執行。

## 先紅後綠與端到端接線

新增兩支 `sw-daemon::dispatch` ServiceRig 回歸：

- `wp510r_i22_rpc_shutdown_token_epoch_window_waits_successful_close`：關閉回條之前不得返回，成功後 session 必須列為已排空。
- `wp510r_i22_rpc_shutdown_token_epoch_window_reports_blocked_close`：close 持續阻塞，shutdown 必須等共用期限並列出該 session 未排空；遲到成功回條仍清除鏡像。

在起點的行為上只加測試與 test-fakes 鉤子，結果為 **0 passed／2 failed**（`i22-red.log`）：兩支都報「token→epoch 替換窗口不可發布 retired_drains_pending=false；shutdown=[]」。套修正後 **2 passed／0 failed**（`i22-green.log`）。

接線：`Service::rpc(device.release)` 先建立 RELEASED 前置；`Service::rpc(device.attach)` → SessionManager → actor OpenBridge → `register_plain_open` → Bridged 帳本。開埠工作停在 BridgeOpened 送出前；正式 `Service::rpc(session.bind)` → BindWithAck → bind_request → ClaimForStop／BindWait，確認同 token 已取消並正在等待。

測試讓真正的 `SessionManager::shutdown_phase1` 完成 GetState 後停在讀 fence 前，才放行 BridgeOpened；actor 停在移除 token、加入 epoch 之前，讓 shutdown 確定在這個窗口讀鏡像。待 Shutdown 入列（舊版則直接完成 Phase 1）才放行 actor，經 `retire_opened_bridge` → AdoptBridge → BeginDrain。close 暫停時確認原帳本紀錄已移除、退休 epoch 存在、鏡像 true，再執行真正的 `shutdown_phase2`；此時無法靠帳本補收而掩蓋漏列。

兩支均驗證窗口／close 暫停期間的鏡像、成功回條前 handle 仍開啟、shutdown 回覆時點與逐 session 結果、配對成功後鏡像 false、關閉恰好一次、不增開埠、零登入 TX。attach／bind 完整回應形狀與 drain 投影使用 `dumps(..., &RPC_WIRE)` 逐位元組比對。序列埠、WAL、state 與行程掃描皆為 ServiceRig 替身，實際 actor／IoWorker／停止兩階段有執行；不需要硬體 regression case。

## 不成立主張

無；接受 High 並修正。

## SPEC-GAPS

本輪沒有需要新裁決或範圍外修改的缺口。既有 S24 的 Rust 回應差異不在本輪路徑：沒有改寫 `attach_open_failed` 或 DEVICE_STILL_HELD，未重跑該黑箱檔。owner／硬體項目維持 BLOCKED-OWNER；沒有 Windows 原生或 com0com 實跑證據，Windows gate 只代表交叉編譯。

## 既有碼待辦

沿用審查中的三項，不在本輪修：完整 shutdown 與 parked close 遲到回條的停止順序；沒有 release 介入時 parked 關閉期間重插被舊拔除事件覆蓋；保管開埠途中 clear 的 DETACHED／CLEARED 差異。其他分支的 graceful_stop.rs 等 finding 不處理。

workspace 的 CLI 既有失敗保留 FAIL。本輪修改任何來源檔前，已在 `22acfbb8` 完整執行 workspace：**1629 passed／26 failed／46 ignored**。baseline source 比對與各 gate 結果如下。

## 驗收結果

全部在本 worktree 前景執行並等待結束；原始輸出位於 `target/s2-par/wp510r/i22-*`。

| gate | 結果／輸出 |
|---|---|
| 起點行為上的新回歸 | **0 passed／2 failed**，鏡像 false、shutdown 漏列；`red.log` |
| 修正後新回歸 | **2 passed／0 failed**；`green.log`，最後完整 affected run 亦含相同兩個節點 |
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast` | **2118 passed／0 failed／7 ignored**；`test-affected.log` |
| `cargo test --workspace --no-fail-fast` | **FAIL：1629 passed／26 failed／46 ignored**；與完整起點 run 的數字及失敗 id 逐名相同；`test-workspace.log`、`baseline-workspace.log`、`test-comparison.json` |
| `cargo build --workspace --all-targets` | exit 0；`build.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | exit 0；`clippy-affected.log` |
| 額外 `cargo clippy --workspace --all-targets --features test-fakes --no-deps` | **FAIL：exit 101，3 個起點既有錯誤**；`clippy-workspace.log`、`baseline-clippy-workspace.log` |
| 額外 `cargo clippy -p sw-proto --all-targets --no-deps` | **FAIL：exit 101，起點相同的 never_loop**；`clippy-proto.log`、`baseline-clippy-proto.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu` | exit 0；只代表交叉編譯，`windows-check.log` |
| 本輪新增 Rust 警告 | **0**；build 18／Clippy affected 61／Windows check 32 種診斷均與起點一致；`warning-comparison.json` |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0、0 診斷；`cfg.log` |
| `python3 -m tools.contracts check` | ok=true、findings 0；`contracts.log` |
| L1 收集＋`regen_lock.py --l1-add-only` | s21_core.linux **新增 2、移除 0**；其他欄位、selector 與原節點順序逐項不變；`l1-collected.json`、`l1-add.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 22acfbb8` | exit 0；`lock-check.log` |
| fragment pytest | **1 passed**；`fragment.log` |
| `python3 -m policy_check --repo .` | **25 PASS／2 WARN／0 FAIL**（R-19、R-22 advisory）；`policy.log` |
| `git diff --check` | 通過 |

輸出欄縮寫皆以 `i22-` 為前綴。Rust 警告比對排除摘要與行號位移，以警告文字＋來源檔名去重。workspace Clippy 因既有編譯錯誤中止，兩輪完成的 target 不盡相同：起點記錄 161 種警告，本輪 165 種；本輪記錄額外顯示的 8 種中，7 種已在起點的 affected Clippy 輸出出現，另 1 種已在起點的 sw-proto Clippy 輸出出現，且這 8 種的來源檔 SHA256 均與起點相同。4 種起點記錄中的警告未出現在本輪 workspace 輸出。沒有把排程造成的診斷清單差異視為修復；詳細逐項比對與 hash 在 `i22-warning-comparison.json`。

workspace Clippy 的 3 個共同錯誤位置完全相同：`crates/sw-pal/tests/proc_scan.rs:431`、`:532` 的 `clippy::non_octal_unix_permissions`，以及 `crates/sw-proto/src/pyre/parse.rs:587` 的 `clippy::never_loop`。本輪沒有修改這些檔案或降低 lint。baseline build／Clippy／Windows check 僅在本 worktree 暫存修改稿、把三個受影響 Rust 檔換回 `22acfbb8` 後前景執行；`i22-baseline-source-diff.log` 為空，結束後用 trap 恢復，並逐位元組確認修改稿完整恢復，才執行最終驗收。

26 個 workspace／起點共同失敗 id（維持 FAIL）：

- `sw-cli::mgmt_doctor::overall_ok_ignores_advisory_failures_only`
- `sw-cli::mgmt_doctor::python_runtime_nonzero_rc_is_unrunnable`
- `sw-cli::mgmt_doctor::pyyaml_import_failure_is_not_installed_with_fix`
- `sw-cli::mgmt_doctor::runtime_spawn_error_is_unrunnable_not_not_installed`
- `sw-cli::mgmt_doctor::runtime_timeout_is_unrunnable_not_not_installed`
- `sw-cli::mgmt_doctor::s1m241_doctor_code_mismatch_is_advisory`
- `sw-cli::mgmt_layout::locate_manifest_env_override_first`
- `sw-cli::mgmt_setup::s1m241_assessment_is_omitted_when_initial_ping_is_unreachable`
- `sw-cli::mgmt_setup::s1m241_assessment_is_omitted_without_installed_fingerprint`
- `sw-cli::mgmt_setup::s1m241_assessment_matching_ping_is_reported_once`
- `sw-cli::mgmt_setup::s1m241_assessment_restart_after_unreachable_is_reported`
- `sw-cli::mgmt_setup::s1m241_assessment_stale_systemd_pending_sudo_is_reported`
- `sw-cli::mgmt_setup::s1m241_assessment_stale_systemd_restart_failure_is_reported`
- `sw-cli::mgmt_setup::s1m241_assessment_stale_systemd_restarts_and_verifies`
- `sw-cli::mgmt_setup::s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero`
- `sw-cli::mgmt_setup::s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero`
- `sw-cli::mgmt_setup::s1m241_failure_restart_error_preserves_python_hint_without_postcheck`
- `sw-cli::mgmt_setup::s1m241_failure_retries_unreachable_then_reports_match`
- `sw-cli::mgmt_setup::s1m241_setup_stale_daemon_without_fingerprint_is_restarted`
- `sw-cli::mgmt_setup::s1m241_setup_stale_service_is_restarted_and_verified`
- `sw-cli::mgmt_setup::s1m241_setup_system_scope_with_sudo_restarts_after_enable`
- `sw-cli::mgmt_setup::s1m241_skips_decision_priority`
- `sw-cli::mgmt_setup::s1m241_skips_flashing_without_force_before_materialize`
- `sw-cli::mgmt_setup::s1m241_skips_no_restart_flag`
- `sw-cli::mgmt_setup::s1m241_skips_no_restart_flag_preserves_mode_transition`
- `sw-cli::mgmt_setup::s1m_c1_setup_verify_polls_reconciled_socket`

fragment 命令為 `TMPDIR="$PWD/target/t" python3 -m pytest --noconftest -q tests/test_changelog_fragments.py --basetemp target/s2-par/wp510r/i22-pytest-tmp`，避免全域 conftest 探測 production daemon／設定／狀態。兩個 UnknownMarkWarning 是未載入 conftest 的 marker 定義所致，未改測試判準或抑制診斷。未觸及其他 Python 測試；沒有跑全套 pytest、slice gate、Docker 或出口證據 driver。

Rust 測試需磁碟暫存時使用 `TMPDIR=/proc/<前景 shell PID>/cwd/target/t` 指回本 worktree，保持 Unix socket 短路徑；沒有 namespace 或自製隔離 runner。未操作 production daemon、其設定／狀態、實體序列埠、其他 worktree、主 checkout 或 thala 容器；未 push。實作、測試、L1 add-only、fragment 與本報告以同一提交交付。


## REMAINING

REMAINING: none
