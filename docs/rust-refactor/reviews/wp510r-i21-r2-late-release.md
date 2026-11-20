# wp510r 第 21 項第 2 輪：Bridged 改綁與 release 的遲到收尾

起點 `e385ed0a`，分支 `feature/rust-refactor-p8`。本輪只處理增量審查的新 High `P8-e385ed0-1`；沒有跑整包審查或出口證據 driver。

## Finding 處理

| 項目 | 結果 |
|---|---|
| P8-e385ed0-1（High） | fixed（本提交）：保留 `PendingOpenStop` 的在途 release 語意，等待配對退休排空回條，由 `finish_pending_open_stop` 恢復 RELEASED、清 drain 並落盤 |
| 第 4 次整包審查 Medium 1、Medium 2 | already fixed `e385ed0a`；原有三支第 21 項 RPC 回歸保留並通過 |
| A 段 High／Medium | 沒有標「未修」而須本輪處理的項目；各項原結案與裁決沿用，不重做整套 review |
| P8-591d2ff-1、P8-0896933-1、P8-c89cd78-1（Low） | 依本輪指令不修，由編排者管理 low-backlog |
| P8-057a065-4（Medium） | 依既有裁決不處理 |

## 根因、盤點與修正

前輪兩項 Medium 的共通根因仍是「在途或關閉中的帳本狀態沒有被所有消費者一致辨識」；`e385ed0a` 已以 `CustodyPhase::is_in_flight()` 統一 Opening／Taken／Bridged／BridgeDraining／Closing 的辨識，並讓所有 Pending token 都可補收。盤點見[前輪回報](wp510r-i21-inflight-custody.md)。本輪沒有新增判定位置或新狀態。

這次是同一資源的等待對象轉換漏了生命週期語意。`bridge_opened` 移除被 bind 取消的 token 等待時，只有在沒有 `pending_open_stop` 才退休該 bridge；release 加入後反而走 `drain_opened_bridge`，把原停止改成一般 `StopOp`。bind 的期限將 session 設為 RECOVERING，而一般停止的 `already_released` 分支不恢復 RELEASED，也不清 `drain_timeout_epoch` 或補落盤；token 等待的退休鏡像也失去後續結算。

現在被取消的保管交接一律透過 `retire_opened_bridge` 把等待轉成同資源的 epoch／stop_id，沿用首次 release 期限，並保留原 `PendingOpenStop`、already_released、source 與 stalled。退休表鏡像隨表更新；配對回條經 `settle_retired` 移除等待後，`try_converge` 呼叫原有 `finish_pending_open_stop`，完成 release 的狀態、欄位與持久化。

Python 依據在本輪以 `git show origin/main:sw_core/session_manager.py` 核實（`origin/main=47097715`）：`:4339-4364` 在關閉成功後套 RELEASED、清 false/null、落盤再設 done；`:3188-3215` release 等作業完成／期限；`:3716-3726` release 已介入時 bind 不改綁。符合設計第 5 版 C3、C5、§3.1.3 收斂規則與 §3.1.6 T-1。沒有更動 Python、契約、golden 或凍結節點的名稱、步驟與斷言。

沒有新增事件、請求或效果；沿用既有 BridgeOpened、退休 drain、WorkerStopped、BindWait／ReleaseWait 及 T14 分類。資源收尾在既有凍結規則下執行，本輪沒有擴大 admission 或 TX 權限。

## 先紅後綠與接線證據

新增兩支 `sw-daemon::dispatch` ServiceRig 正式 RPC 回歸：

- `wp510r_i21_rpc_bind_release_bridged_before_deadlines`：期限內成功的對照，另驗等待鏡像與重新落盤。
- `wp510r_i21_rpc_bind_release_bridged_after_both_deadlines`：bind／release 都依各自首次期限回覆後才交付 BridgeOpened，遲到成功仍須完成 RELEASED。

未修改 production source 時，`i21-r2-red.log` 為 **0 passed／2 failed**：兩支都沒有配對退休 epoch。將新測試的觀察順序調整後，`i21-r2-red-state.log` 仍為 **0 passed／2 failed**：期限內案例報「退休等待鏡像同步清除」失敗；遲到案例實際 RECOVERING、預期 RELEASED。未刪除或放寬斷言。

接線：`Service::rpc(device.attach)` → `SessionManager` → actor `OpenBridge` → `register_plain_open` → 帳本持有 IoWorker 的 Bridged；鉤子暫停在 BridgeOpened 送出前。`Service::rpc(session.bind)` → `BindWithAck` → `bind_request` → `ClaimForStop`／BindWait，確認同 token 已寫 Bind 取消。接著正式 `device.release` RPC 建立 ReleaseWait；逾時案例等兩個 RPC 依期限返回 SESSION_RECOVERING／DRAIN_STALLED，再放行 BridgeOpened。

放行後：`bridge_opened` → `reply_cancelled_attach`＋`retire_opened_bridge` → `AdoptBridge`／`BeginDrain` → 真實 IoWorker 的 close 與 WorkerStopped → `retired_drain_receipt` → `settle_retired` → `try_converge` → `finish_pending_open_stop` → release 持久化。

兩支皆在 close 暫停時確認退休 epoch 與鏡像為 true、回條前沒有落盤；成功後確認 RELEASED、drain false/null、鏡像 false、原 metadata 保留、寫入次數增加、state.json 有 RELEASED 列、帳本與 awaits 清空、只關閉一次、不增開埠、零登入 TX。再次 release 以 `dumps(..., &RPC_WIRE)` 逐位元組比對完整回應。序列埠／WAL／state／行程掃描皆為 ServiceRig 替身，不需實體硬體 regression case。

## 不成立主張

無；接受新 High 並修正。

## SPEC-GAPS

本輪沒有需要擴權或新裁決的缺口。既有 S24 五項 Rust 差異不在本輪路徑，未重跑或改寫 `attach_open_failed`／DEVICE_STILL_HELD 形狀。owner／硬體項目維持 BLOCKED-OWNER；沒有 Windows 原生或 com0com 實跑證據，Windows gate 僅交叉編譯。

## 既有碼待辦

沿用審查列出的三項，不在本輪修：完整 shutdown 與 parked close 遲到回條的停止順序；沒有 release 介入時 parked 關閉期間重插被舊拔除事件覆蓋；保管開埠途中 clear 的 DETACHED／CLEARED 差異。其他分支的 graceful_stop.rs 等 finding 沒有處理。

workspace 的 CLI 既有失敗維持 FAIL；本輪開始、修改任何來源檔之前，已在 `e385ed0a` 完整執行 workspace，結果 **1629 passed／26 failed／46 ignored**。最終逐名對照及 gate 數字在下一節。

擴大 workspace Clippy 的三個既有錯誤保留 FAIL：`crates/sw-pal/tests/proc_scan.rs:431`、`:532` 的 `clippy::non_octal_unix_permissions`，以及 `crates/sw-proto/src/pyre/parse.rs:587` 的 `clippy::never_loop`。前兩項在換回起點的 workspace Clippy 直接重現；第三項在 workspace 提早中止時未顯示，另以 `cargo clippy -p sw-proto --all-targets --no-deps` 直接重現。sw-proto／sw-pal 全部內容與 `e385ed0a` 相同（`i21-r2-unchanged-clippy-crates.log` 為空），因此該命令受測來源即為起點。未修改範圍外程式或降低 lint 等級。baseline 編譯比對只在本 worktree 暫存／換回 `session_core.rs` 的起點內容，前景執行後完整恢復並逐位元組確認；`i21-r2-baseline-source-diff.log` 為空。

## 驗收結果

全部在本 worktree 前景執行並等待結束；原始輸出在 `target/s2-par/wp510r/i21-r2-*`。

| gate | 結果／輸出 |
|---|---|
| 未修程式的新回歸 | **0 passed／2 failed**；`red.log`、`red-state.log` |
| 第 21 項正式 RPC 回歸 | **5 passed／0 failed**（新 2、原 3）；`green.log` |
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast` | **2116 passed／0 failed／7 ignored**；`test-affected.log` |
| `cargo test --workspace --no-fail-fast` | **FAIL：1629 passed／26 failed／46 ignored**；與完整起點 run 的數字、失敗 id 逐名相同；`test-workspace.log`、`baseline-workspace.log`、`test-comparison.json` |
| `cargo build --workspace --all-targets` | exit 0；`build.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | exit 0；`clippy-affected.log` |
| 額外 `cargo clippy --workspace --all-targets --features test-fakes --no-deps` | **FAIL：exit 101，3 個起點既有錯誤**；`clippy.log`、`baseline-clippy.log`、`baseline-clippy-proto.log`，診斷位置見上節 |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu` | exit 0；僅交叉編譯，`windows-check.log` |
| 新增 Rust 警告 | **0**；build 18／Clippy affected 61／Windows check 32 種診斷，與起點相同；排除摘要及行號位移，以警告文字＋來源檔名去重比較；`warning-comparison.json` 另核對上述 3 個既有 Clippy 錯誤位置完全相同 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0；`cfg.log` |
| `python3 -m tools.contracts check` | ok=true、findings 0；`contracts.log` |
| L1 收集＋`regen_lock.py --l1-add-only` | s21_core.linux **新增 2、移除 0**；其他欄位、selector 與順序不變；`l1-collected.json`、`l1-add.log` |
| `python3 tests/acceptance/regen_lock.py --check --base e385ed0a` | exit 0；`lock-check.log` |
| fragment pytest | **1 passed**；`fragment.log` |
| `python3 -m policy_check --repo .` | **25 PASS／2 WARN／0 FAIL**（R-19、R-22 advisory）；`policy.log` |
| `git diff --check` | 通過 |

輸出欄縮寫皆以 `i21-r2-` 為前綴。26 個 workspace／起點共同失敗 id（完整 id 的 crate 均為 sw-cli）：

- `mgmt_doctor::overall_ok_ignores_advisory_failures_only`
- `mgmt_doctor::python_runtime_nonzero_rc_is_unrunnable`
- `mgmt_doctor::pyyaml_import_failure_is_not_installed_with_fix`
- `mgmt_doctor::runtime_spawn_error_is_unrunnable_not_not_installed`
- `mgmt_doctor::runtime_timeout_is_unrunnable_not_not_installed`
- `mgmt_doctor::s1m241_doctor_code_mismatch_is_advisory`
- `mgmt_layout::locate_manifest_env_override_first`
- `mgmt_setup::s1m241_assessment_is_omitted_when_initial_ping_is_unreachable`
- `mgmt_setup::s1m241_assessment_is_omitted_without_installed_fingerprint`
- `mgmt_setup::s1m241_assessment_matching_ping_is_reported_once`
- `mgmt_setup::s1m241_assessment_restart_after_unreachable_is_reported`
- `mgmt_setup::s1m241_assessment_stale_systemd_pending_sudo_is_reported`
- `mgmt_setup::s1m241_assessment_stale_systemd_restart_failure_is_reported`
- `mgmt_setup::s1m241_assessment_stale_systemd_restarts_and_verifies`
- `mgmt_setup::s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero`
- `mgmt_setup::s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero`
- `mgmt_setup::s1m241_failure_restart_error_preserves_python_hint_without_postcheck`
- `mgmt_setup::s1m241_failure_retries_unreachable_then_reports_match`
- `mgmt_setup::s1m241_setup_stale_daemon_without_fingerprint_is_restarted`
- `mgmt_setup::s1m241_setup_stale_service_is_restarted_and_verified`
- `mgmt_setup::s1m241_setup_system_scope_with_sudo_restarts_after_enable`
- `mgmt_setup::s1m241_skips_decision_priority`
- `mgmt_setup::s1m241_skips_flashing_without_force_before_materialize`
- `mgmt_setup::s1m241_skips_no_restart_flag`
- `mgmt_setup::s1m241_skips_no_restart_flag_preserves_mode_transition`
- `mgmt_setup::s1m_c1_setup_verify_polls_reconciled_socket`

fragment 測試命令為 `TMPDIR="$PWD/target/t" python3 -m pytest --noconftest -q tests/test_changelog_fragments.py --basetemp target/s2-par/wp510r/i21-r2-pytest-tmp`，避免全域 conftest 探測 production daemon／設定／狀態。兩個 UnknownMarkWarning 來自沒有載入 conftest 的 marker 定義；沒有改測試判準或抑制診斷。未觸及其他 Python 測試；沒有跑全套 pytest、slice gate、Docker 或出口證據 driver。

Rust 測試需磁碟暫存時，以 `TMPDIR=/proc/<前景 shell PID>/cwd/target/t` 指回本 worktree，保持 Unix socket 短路徑；沒有 namespace 或自製隔離 runner。未操作 production daemon、其設定／狀態、實體序列埠、其他 worktree、主 checkout 或 thala 容器；未 push。實作、測試、L1 add-only、fragment 與本回報一併提交；Windows 原生與硬體證據沒有新增。


## REMAINING

REMAINING: none
