# wp510r 第 25 項第 2 輪：bind 晚到完成的世代發布

起點為 `ea9e03d2`，分支 `feature/rust-refactor-p8`。本輪只處理增量審查的 **P8-ea9e03d-1 High**；前輪的過期義務及退避修正保留。未修改 Python 基線、contracts、golden、OD-55 能力集合或其他工作包。

## 修正與接線

Python 基線 `origin/main:sw_core/session_manager.py:3774-3791` 在同一鎖內套用綁定，`:3810-3812` 再存檔並啟動 attach。原本 Rust actor 在 `apply_bind` 遞增世代，manager 在成功收到 RPC 回覆後再自行遞增。已入列的 bind 等滿正式 8 秒期限後，manager 由 `?` 返回；actor 晚到完成只增加自己的世代。之後成功 bind 仍留下差值，`DeviceOnline` 與 watcher 的合法通知都被相等檢查拒絕。

本提交改為 actor 在實際套用 bind 時產生唯一世代，以 `Effect::PublishBinding` 先於 RPC 回覆同步 manager。`ManagerExec` 核對穩定 ActorId 後，直接指定 `Entry.binding_generation`、by-id 與 I/O profile，並撤銷該 actor 的舊義務；RPC 回覆通道已關閉仍照常發布。無 bridge 的立即 bind、排空完成後的 bind、BindWait 收斂後的 bind，以及改變 by-id 的 `SetProfile` 都使用同一發布入口。manager 的 bind、DETACHED-rebind、rematerialize 不再各自增加世代。過期通知的裝置路徑、actor 身分與世代檢查全部保留。

正式接線：`Service::rpc("session.bind")` → `SessionManager::bind` → `ActorHandle::request_with_ack_notice` → `SessionCore::bind_request` → `apply_bind`／`publish_binding` → `Effect::PublishBinding` → `ManagerExec::execute` 同步 manager。重試的 `DeviceOnline` 與重插的 `DeviceAttached` 再經 `device_delivery_current` → `device_attached` → `OpenBridge`，由 ServiceRig 的記憶體序列埠實際開啟；`session.self_test` 驗證目前與已連接路徑。

T14 分類：沒有新增正式請求或事件。`PublishBinding` 只同步已套用事實並取消舊義務，屬資源／中繼資料收尾，Open／Pending／Permanent 均執行，不開埠、不持有 handle。Bind 與 SetProfile 原有的凍結受理規則保留。

## 先紅後綠

兩項新增節點均用正式 ServiceRig RPC，從 READY → release → bind 已入列、套用前暫停 → 正式 8 秒 RPC 逾時 → 放行 actor → 同 control lane 查詢屏障確認晚到完成。沒有縮短正式 RPC 期限。

- `daemon::dispatch::wp510r_i25_rpc_timed_out_bind_late_completion_allows_retry`：再次 bind 回 ATTACHING，最後 READY；開埠次數恰為 2；self-test 不得誤報 DEVICE_REBOUND_REQUIRED，兩個路徑均正確。
- `daemon::dispatch::wp510r_i25_rpc_timed_out_bind_late_completion_allows_replug`：沿審查反例原順序，再次 bind 成功到 READY 後模擬拔插；再次到 READY，開埠次數由 2 增至 3，self-test 路徑正確，沒有殘留送達義務。

新增的 test-fakes 暫停點只攔 bind；原有泛用 Request 暫停能力保留。假 DUT 對每次登入的空行均回 prompt，共用 fixture 原本只回一次的 trigger 不足以測試重開埠，因此只在新增測試中補上持續回覆。

最終兩個測試也重新套回 `ea9e03d2` 的產品來源確認紅燈：**0 passed／2 failed**，兩者均在成功重試仍回 `DETACHED / DEVICE_NOT_FOUND` 的斷言失敗（`i25-r2-red-final.log`）。這次對照只在本 worktree 保存／替換兩個 Rust 來源，保留 test-fakes 暫停入口，結束後逐位元組恢復；雜湊見 `i25-r2-source/hashes.json`。沒有 reset、stash 或其他 checkout。

新舊回歸的精確路徑與世代斷言保留；既有凍結節點沒有改名、刪除或修改預期。新增兩個節點經正式收集器 list-only 與 `regen_lock.py --l1-add-only` 登記，沒有移除節點或改 selector。

## 不成立主張

無。P8-ea9e03d-1 接受並修正；本輪沒有其他未修 High／Medium。

## SPEC-GAPS

本 High 不需修改契約或範圍外檔案。前輪所列 S24 黑箱既有問題沒有修改、沒有重跑，仍不宣稱已修復；擁有者／硬體項目維持 BLOCKED-OWNER。整包送審與出口 driver 由編排者接續，本輪未執行。

## 驗證與既有碼待辦

所有命令均在本 worktree 前景執行並等待結束，原始輸出在 `target/s2-par/wp510r/i25-r2-*`。`TMPDIR=/proc/$$/cwd/target/t` 指回本樹；未建立 namespace 或自製隔離 runner，未跑全套 pytest、slice gate、Docker、production daemon 或真實序列埠。ServiceRig 是完整 Service／manager／actor 接線的記憶體替身證據；交叉編譯不代表 Windows 原生或 com0com 實跑。

| 檢查 | 結果 | 原始輸出（同目錄） |
|---|---|---|
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast -- --test-threads=4` | **FAIL**：2143 passed、1 failed、7 ignored；唯一失敗起點已重現 | `i25-r2-affected.log`、`i25-r2-baseline-dr1d.log` |
| 本輪新增回歸／本項新舊回歸 | **PASS：2/2、7/7** | 同上；定向 `i25-r2-green.log` |
| `cargo test --workspace --no-fail-fast -- --test-threads=4` | **FAIL**：1629 passed、26 failed、46 ignored；失敗集合與起點完全相同 | `i25-r2-workspace.log`、`i25-r2-baseline-workspace.log`、`i25-r2-test-results.json` |
| `cargo build --workspace --all-targets` | **PASS**，exit 0 | `i25-r2-build.log` |
| `cargo clippy --workspace --all-targets --features test-fakes` | **FAIL**，exit 101；3 個起點既有 lint 錯誤，見下列 | `i25-r2-clippy.log`、`i25-r2-baseline-clippy.log`、`i25-r2-baseline-proc-scan-clippy.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | **PASS**，exit 0 | `i25-r2-clippy-affected.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu`；另含 `--features test-fakes` 一次 | **PASS**，兩次 exit 0；只有交叉編譯證據 | `i25-r2-windows.log`、`i25-r2-windows-features.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | **PASS**，exit 0 | `i25-r2-cfg.log` |
| `python3 -m tools.contracts check` | **PASS**，ok:true、findings 0 | `i25-r2-contracts.log` |
| `python3 tests/acceptance/regen_lock.py --check --base ea9e03d2` | **PASS**；新增 2、移除 0，沒有 selector 或 OD-55 變更 | `i25-r2-lock.log`、`i25-r2-regen-lock.log` |
| changelog fragment pytest | **PASS**：1 passed、2 marker warnings、0 skipped | `i25-r2-fragment.log` |
| `python3 -m policy_check --repo .` | **PASS**：25 pass、0 fail、2 warn | `i25-r2-policy.log` |

fragment pytest 使用 `--noconftest --basetemp=target/s2-par/wp510r/pytest-i25-r2-fragment`，避免全域 conftest 讀取 production daemon／設定／狀態快照；測試本體與斷言未改，沒有跳過案例。兩項 warning 為未載入 conftest 後的 core_portable／memory_only marker 註冊提示。policy 的兩項 advisory 為 R-19 與 R-22，沒有新增豁免。

另以 `ea9e03d2` 的完整 crates 來源重跑 build、受影響 clippy、Windows all-targets 作警告對照，三項均 exit 0。只在本樹保存／替換五個 Rust 檔，確認 `git diff ea9e03d2 -- crates` 為空，再逐位元組恢復；差異與雜湊在 `i25-r2-warning-source/`。警告依訊息及來源檔比較，診斷出現次數分別為 build 19→19、受影響 clippy 93→93、Windows 33→33；新增集合與新增次數皆空（`i25-r2-warning-comparison.json`）。未新增 lint suppression。

既有碼待辦（保留 FAIL，不修其他工作包）：

1. `sw-core::custody::test_dr1_d_release_not_enqueued_keeps_ledger_then_next_release_completes`：預期 ok:true，實際 DRAIN_STALLED。未修改產品邏輯的起點單跑同樣 **0 passed／1 failed**（`i25-r2-baseline-dr1d.log`）。
2. sw-cli 的 26 項失敗：本輪起點與最終均為 workspace **1629 passed／26 failed／46 ignored**；逐名差集及反向差集均空（`i25-r2-test-results.json`）。完整 ID 見下方。
3. workspace clippy：`crates/sw-proto/src/pyre/parse.rs:587` 的 `never_loop`，起點完整 clippy 已重現。最終另因編譯排程走到 `crates/sw-pal/tests/proc_scan.rs:431`、`:532` 的 `non_octal_unix_permissions`；兩個 lint 已單獨重現。sw-pal、sw-proto、Cargo.toml／Cargo.lock／toolchain 與 `ea9e03d2` 的差異為空（`i25-r2-baseline-lint-inputs.diff`），兩個相關檔案的 Git blob 亦與起點相同，故為起點既有錯誤。
4. bind RPC 逾時後，若未先重試 bind 就直接重插，舊的持久化 RELEASED 紀錄仍可能阻擋 attach：manager 的 Unreleased／Binding 存檔在 RPC `?` 之後，逾時即略過。這個既有路徑在 `ea9e03d2` 相同，初始 ServiceRig 對照已重現（`i25-r2-red.log` 的直接重插案例）。本輪同步世代與綁定鏡像；本 High 的端到端驗收依審查原順序，包含再次 bind，未宣稱逾時的 bind 已完成持久化。

sw-cli 失敗 ID：

```text
mgmt_doctor::overall_ok_ignores_advisory_failures_only
mgmt_doctor::python_runtime_nonzero_rc_is_unrunnable
mgmt_doctor::pyyaml_import_failure_is_not_installed_with_fix
mgmt_doctor::runtime_spawn_error_is_unrunnable_not_not_installed
mgmt_doctor::runtime_timeout_is_unrunnable_not_not_installed
mgmt_doctor::s1m241_doctor_code_mismatch_is_advisory
mgmt_layout::locate_manifest_env_override_first
mgmt_setup::s1m241_assessment_is_omitted_when_initial_ping_is_unreachable
mgmt_setup::s1m241_assessment_is_omitted_without_installed_fingerprint
mgmt_setup::s1m241_assessment_matching_ping_is_reported_once
mgmt_setup::s1m241_assessment_restart_after_unreachable_is_reported
mgmt_setup::s1m241_assessment_stale_systemd_pending_sudo_is_reported
mgmt_setup::s1m241_assessment_stale_systemd_restart_failure_is_reported
mgmt_setup::s1m241_assessment_stale_systemd_restarts_and_verifies
mgmt_setup::s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero
mgmt_setup::s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero
mgmt_setup::s1m241_failure_restart_error_preserves_python_hint_without_postcheck
mgmt_setup::s1m241_failure_retries_unreachable_then_reports_match
mgmt_setup::s1m241_setup_stale_daemon_without_fingerprint_is_restarted
mgmt_setup::s1m241_setup_stale_service_is_restarted_and_verified
mgmt_setup::s1m241_setup_system_scope_with_sudo_restarts_after_enable
mgmt_setup::s1m241_skips_decision_priority
mgmt_setup::s1m241_skips_flashing_without_force_before_materialize
mgmt_setup::s1m241_skips_no_restart_flag
mgmt_setup::s1m241_skips_no_restart_flag_preserves_mode_transition
mgmt_setup::s1m_c1_setup_verify_polls_reconciled_socket
```

## 交付狀態

本提交包含修正、兩項新回歸、L1 add-only 登記、changelog fragment 與本報告。未 push、開 PR、merge 或部署。

REMAINING: none
