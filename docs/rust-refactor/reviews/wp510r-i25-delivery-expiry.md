# wp510r 第 25 項：過期送達義務與重試退避

本項起點為 `8c2320e9`，分支 `feature/rust-refactor-p8`。只處理第 7 次整包審查的 Medium 與本項擁有者明確要求一併修復的 Low；不處理其他工作包，也不修改 OD-55 的六個提交。

## 修正與接線

- Medium：bind 保留本次真正的 `binding_generation` 與 actor 身分；逾時後登記義務、送達者投遞前、actor 實際套用時，都檢查目前 by-id、路徑與綁定世代。裝置改指／離線、改綁會撤銷舊義務；晚到的舊 bind 不會取消較新世代的義務。
- Low：`next_at` 未到時不投遞，也不增加 attempts、移動期限或倍增退避。送達者依最近期限等待，退避期間不每 20 ms 重取 manager 鎖。閘門時鐘只計算扣除 Pending 的 30 秒預算；實時決定背景重試排程，避免注入的閘門時鐘停住後無法恢復送達。

`device_attached` 先判斷是否接受開埠事件，再更新路徑。watcher 的目前裝置觀測先經 manager 事實重驗，仍可在 READY、開埠、排空、RELEASED、flash 期間更新合法裝置事實，保留 Python self-test 讀目前 `_devices` 的行為及既有凍結案例。送達重試帶原綁定世代，使用獨立的 `DeviceAttachedRetry`，不借用 watcher 的觀測入口。

正式接線：`Service::rpc("session.bind")` → `SessionManager::bind` → actor `BindWithAck` → 取得本次綁定世代 → `DeviceOnline`；逾時則重驗後登記 `DeliveryObligation` → `Shared::step_delivery` → `Deliverer::step_delivery_with_retry_clock` → `DeviceAttachedRetry` → `SessionCore::device_delivery_current` → `device_attached` → `OpenBridge`。`session.self_test` 仍由 actor 的 `known_real_path` 提供目前路徑；過期通知不能覆寫它。

新增事件的 T14 分類：Open 重驗後處理；Pending 延後，撤銷後重播時再次查詢目前事實；Permanent 丟棄。`DeviceOnline` 延用 Request 的既有分類，套用前也要重驗。沒有新增持有 handle 的事件、請求或效果。

## 紅燈與回歸

先加入兩個主要回歸，產品邏輯維持起點，只增加逾時後、登記前的 test-fakes 暫停點及共用測試 helper 的可見性：

| 節點 | 起點結果 | 原始輸出 |
|---|---|---|
| `daemon::dispatch::wp510r_i25_rpc_late_delivery_registration_cannot_revert_current_path` | 0 passed／1 failed；bridge 已在新路徑 READY，self-test 誤報 DEVICE_REBOUND_REQUIRED，current_real_path 為舊路徑 | `target/s2-par/wp510r/i25-red-stale.log` |
| `core::custody::wp510r_i25_deliverer_does_not_retry_before_backoff_deadline` | 0 passed／1 failed；t0+9 ms 尚未到 10 ms 期限，attempts 已由 1 變 2 | `target/s2-par/wp510r/i25-red-backoff.log` |

ServiceRig 回歸使用正式 RPC，讓 bind 的通知因滿載逾時，暫停義務登記；放行 actor、把同一 by-id 改指新記憶體裝置並完成 READY，最後放行舊登記。要求 self-test 不誤報、新路徑的 current_real_path／attached_real_path 正確、舊義務丟棄且送達數 0、新裝置只開啟一次；RPC 欄位以 `RPC_WIRE` 序列化比對。

另新增三個 core 節點，涵蓋改綁後再綁回的舊世代拒絕、Pending 重播時路徑失效與 Permanent 丟棄、過期 watcher 觀測不污染診斷。現有 `selftest_device_rebound_required`、開埠改指／退休排空及 flash 重插的斷言保留，合法改指仍必須產生正確診斷／收尾。

既有兩個送達單測只替換內部通知 pattern（DeviceAttached → 帶世代的 DeviceAttachedRetry）；原本 by-id、路徑、冪等、凍結、時限與計數斷言全部保留，另增加 bind_gen 的精確斷言。沒有刪除節點、降低斷言或改判狀態。本項用 ServiceRig／記憶體替身即可重現，不需新增實體 regression case。

## 不成立主張

無；Medium 與本項指定 Low 均接受。

## SPEC-GAPS

本項沒有需要新裁決的缺口。沒有改 `attach_open_failed` 的回應形狀；brief 所列 S24 DEVICE_STILL_HELD 多 session 鍵等五項既有黑箱失敗未在本項重跑，未宣稱已修復。其他分支 finding 不處理；擁有者／硬體項目維持 BLOCKED-OWNER。

## 驗證與既有碼待辦

原始輸出均在 `target/s2-par/wp510r/`。所有測試直接在本工作樹前景執行，`TMPDIR=/proc/$$/cwd/target/t` 指回本樹；未建立 namespace、自製 runner，也未執行全套 pytest、slice gate、Docker 或出口 driver。沒有 production daemon、真實序列埠、Windows 原生或 com0com 證據。

| 檢查 | 結果 | 原始輸出 |
|---|---|---|
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast -- --test-threads=4` | **FAIL**：2141 passed（含 4 個 doc-tests）、1 failed、7 ignored；唯一失敗已在起點重現，見下列 | `i25-final-affected.log` |
| 本項新增回歸 | **PASS：5／5**，包含正式 bind → 過期登記 → self-test 的 ServiceRig 交錯 | 同上 |
| `cargo test --workspace --no-fail-fast -- --test-threads=4` | **FAIL**：1629 passed、26 failed、46 ignored；26 個失敗 ID 與起點 CLI 完全一致 | `i25-final-workspace.log`、`i25-baseline-cli.log` |
| `cargo build --workspace --all-targets` | **PASS**，exit 0；無新增警告 | `i25-final-build.log` |
| `cargo clippy --workspace --all-targets --features test-fakes` | **FAIL**，exit 101；起點相同的 `sw-proto` never_loop 錯誤 | `i25-final-clippy.log`、`i25-baseline-clippy.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | **PASS**，exit 0；無新增警告 | `i25-final-clippy-affected.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu`，另含 `--features test-fakes` 一次 | **PASS**，兩次 exit 0；僅交叉編譯，非 Windows 原生證據 | `i25-final-windows.log`、`i25-final-windows-features.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | **PASS**，exit 0 | `i25-final-cfg.log` |
| `python3 -m tools.contracts check` | **PASS**，ok:true、findings 0 | `i25-final-contracts.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 8c2320e9` | **PASS**，鎖結構、集合、案例屬性與統計一致 | `i25-final-lock.log` |
| changelog fragment pytest | **PASS**：1 passed、2 marker warnings、0 skipped | `i25-final-fragment.log` |
| `python3 -m policy_check --repo .` | **PASS**，25 pass、0 fail、2 warn | `i25-final-policy.log` |

以正式 `cargo_l1_results.py --list-only` 依原 selector 收集，再由 `regen_lock.py --l1-add-only` 登記 `s21_core.linux`：新增 5、移除 0。鎖檔 diff 只有這五個名稱，其他集合、平台與 selector（含 OD-55）未變；未使用 allow-removal。收集輸出為 `i25-collect.json`、`i25-collect.log`，登記紀錄為 `i25-regen-lock.log`；沒有執行 slice gate 或出口 driver。

為遵守不得探查 production daemon／設定／狀態的限制，fragment pytest 使用 `--noconftest --basetemp=target/s2-par/wp510r/pytest-i25-fragment`；測試本體不變、未跳過案例。兩個 warning 是 conftest 未註冊 `core_portable`、`memory_only` marker。

起點對照只在本工作樹進行：將本輪 8 個已修改 Rust 檔案保存到 target，以 `git show 8c2320e9:<path>` 逐檔還原；確認 `git diff 8c2320e9 -- crates` 為空後執行對照，最後逐位元組恢復本輪來源。沒有 reset、stash、其他 checkout 或其他 worktree。空差異為 `i25-baseline-source.diff`、`i25-baseline-source-final.diff`；恢復後來源雜湊為 `i25-restored-source.json`。

警告依訊息＋檔案比較，並檢查新增出現次數：build 18→18、受影響 clippy 61→61、Windows all-targets 32→32，新增集合與新增次數皆空。沒有加入 lint suppression。詳見 `i25-warning-comparison.json`。

保留的既有碼待辦：

1. `sw-core::custody::test_dr1_d_release_not_enqueued_keeps_ledger_then_next_release_completes`：最終失敗為預期 ok:true、實際 DRAIN_STALLED；`8c2320e9` 單跑同樣 **0 passed／1 failed**（`i25-baseline-dr1d.log`）。本項不修改其斷言或擴充修復。
2. CLI 26 個失敗：起點 `cargo test -p sw-cli --no-fail-fast -- --test-threads=4` 為 **705 passed／26 failed／3 ignored**；失敗集合與最終 workspace 完全相同。完整對照及 ID 見 `i25-test-results.json` 與下列清單。
3. workspace clippy 的 `crates/sw-proto/src/pyre/parse.rs:587` `clippy::never_loop`：起點與最終都 exit 101；本項不改此檔。

本項不把上述 FAIL 改成 PASS／SKIP，也不以「flaky」作為放行理由。

policy 的兩項 advisory 為 R-19 工作流程解析提示與 R-22 既有懸空引用；沒有改判或新增豁免。


CLI 既有失敗 ID（sw-cli）：

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

本提交包含產品修正、5 個回歸節點、L1 add-only 登記、changelog fragment 與本報告。實作及本項回歸完成；全 workspace 測試與 clippy 仍保留上述已證明的起點失敗，不宣稱全綠。未 push、開 PR、merge 或部署；整包送審由 driver 接續。

## REMAINING

REMAINING: none
