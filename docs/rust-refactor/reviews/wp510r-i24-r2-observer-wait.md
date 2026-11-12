# wp510r 第 24 項第 2 輪：觀察等待與關閉義務分流

## 審查處理與範圍

起點為 `5f679551`，分支為 `feature/rust-refactor-p8`。本輪接受並修正 **P8-5f67955-1（High）**：一般熱插的 OpenBridge 已提交 Bridged、BridgeOpened 尚未交付時，重複 device.attach 只需等待接手確認，不能因安全網登記了 token 就取消正常 bridge。

A 段沒有未修的 High／Medium。未修的 Low（P8-591d2ff-1、P8-0896933-1、P8-c89cd78-1）依 brief 留給 low-backlog，沒有修改。依裁決不處理的 P8-057a065-4 不重開。

依定稿設計第 5 版及其引用的 v1～v4、最後一輪設計審查與 `origin/docs/parity-amendments` 的 s1／s2 裁決工作。Python 基線以本輪 `origin/main:sw_core/session_manager.py:3427-3443` 核對：單純 attach 在途回 ok＋session，只有 drain／release 才回 SESSION_RECOVERING；單純重複 attach 不取消既有作業。D1／T-1 的安全網仍有約束力，等待不能靠「有沒有拿到 parts」或 owner 身分推定結束。

## 修正與盤點

每筆 token 等待新增 `CustodyWaitPurpose`，在既有 awaits／CustodyPoll 機制中區分 Observe 與 Close。這是本地等待義務，帳本仍是唯一的資源生命週期來源。Observe 仍保留未終態阻擋與補收，不能因 Tick／RX 沒有查詢帳本就提早回成功；只有配對接手確認或真實終態才解除。Close 才把遲到的 BridgeOpened 轉成退休排空，等待 epoch／stop_id 的真實關閉回條。

下表位置均在 `crates/sw-core/src/session_core.rs`：`wait_observed_custody:2665`、`attach_reply:2731`、`release_claimed:2926`、`bridge_opened:3859`、`custody_claimed:4363`、`retire_result:4781`、`retire_opened_bridge:5015`。前輪完整回覆點盤點保留於 `wp510r-i24-pending-safety.md`。

| 位置（函式名稱為定位依據） | 原本判斷 | 本輪安全網處理 |
|---|---|---|
| `attach_reply` | 所有延後 attach 回覆都透過相同的 retired token 等待 | 明確登記 Observe；仍等帳本終態並排 CustodyPoll，不取消開埠 |
| `wait_observed_custody`／`await_observed_custody` | token 已存在就不再處理 | 一般安全收尾使用 Close；既有 Observe 只升級為 Close，後來的 Observe 不降級 |
| `release_claimed` | 相同 token 已在表中便略過 | 明確升級為 Close，保留原等待、首次期限與 D1／T-1 |
| `custody_claimed` | 同 token 已存在便略過 | ClaimForStop 回饋將既有觀察升級為 Close；bind／停止已要求取消時不再正常登入 |
| `bridge_opened` | 只要同 token 在 retired_drains 就視為取消、退休 bridge | 僅 Close 走取消；Observe 允許正常 AdoptBridge，保留 token 到接手確認後補收解除 |
| `retire_result(Invalid)`／`retire_opened_bridge` | 真正的失效 attach／退休 bridge 關閉義務 | 明確標 Close；沒有放寬等待或回條斷言 |
| release 成功、already_released、持久化收尾、自身 bridge 回條、重開判定 | 前輪統一帳本查詢與收斂規則 | 沿用前輪安全網；本輪既有回歸原斷言保留，沒有略過 Closing／失敗／未知終態 |

沒有新增 production 事件、請求或效果。test-fakes 的既有 RetiredDrainsForTest 查詢增加 observers 欄位及 manager 查詢 helper，只供確定性同步，不改既有 custody／epochs 欄位。T14 的分類不變：新工作受凍結閘門控制，既有 CustodyPoll／終態補收依原分類處理。

## 先紅後綠與接線

在 production 行為仍為 `5f679551` 時先加入測試；唯一來源轉接是 test-fakes 查詢 observers，實際差異保存於 `target/s2-par/wp510r/i24-r2-red-tests-and-query.diff`。然後才修改等待用途與 BridgeOpened 的取消判定。

- Core 紅燈：`i24-r2-red-core.log`，**0 passed／1 failed**，失敗為「純等待不得排空有效 bridge」。同一新節點逐一涵蓋 Opening、Taken、Bridged；要求補收同 token、未查帳本的 Tick 不提前回覆、正常接手後解除等待且不留關閉義務。
- 正式 ServiceRig RPC 紅燈：`i24-r2-red-rpc.log`，**2 passed／1 failed**。拔除、重插、OpenBridge 已提交 Bridged 後插入重複 device.attach，實際 `is_open=false`，失敗於「重複 attach 不得關閉有效新 bridge」。無重複 attach 的對照組通過；觀察後加入真正 release 的關閉回條對照也通過。所有鉤子與工作者已放行後才做主要紅燈斷言。
- 修正後本項定向測試：`i24-r2-green-core.log` **9 passed**；`i24-r2-green-rpc.log` **7 passed**。包含前輪安全網的成功、逾時後遲到成功、失敗維持首次期限、一般 Bridged release 與 attach 清理回歸。
- 新增 4 個 L1 節點，以 repo 收集器 list-only 後執行 `regen_lock.py --l1-add-only --set s21_core --platform linux` 登記；**新增 4、移除 0**。既有節點、selector、步驟與斷言沒有修改。輸出為 `i24-r2-l1-collected.json`、`i24-r2-l1-add.log`。

第一遍完整受影響 suite 曾使新對照 `wp510r_i24_rpc_observer_then_release_still_waits_close_receipt` 失敗（`i24-r2-affected.log`）：同步點只查 token 已在 awaits，但純觀察本來就有同 token，不能證明 release 已受理。已改成同時要求帳本的同 token 帶 `cancel=Release`，再放行 BridgeOpened；沒有改任何成功、回條、open／close 或 RPC 斷言。修正的是新測試的觸發順序，不把這個失敗歸為既有問題或用環境推測放行。補強同步後本項正式 RPC **7 passed／0 failed**（`i24-r2-green-rpc-synchronized.log`），再以完整受影響 suite 重驗。

正式接線：`Service::rpc(device.attach)` → `SessionManager::route_with_selector` → actor → `ManagerExec::pending_custody` → `CustodyLedger::pending_for` → `SessionCore::attach_reply`（Observe）→ `BridgeOpened` → 正常 `AdoptBridge`／帳本確認接手 → `CustodyPoll`／`PollCustody` 補收 → RPC 回覆。release 另經 `ClaimByIdForRelease`／`release_claimed` 升級 Close → token 轉真實 epoch 排空 → 配對關閉回條 → 成功回覆。ServiceRig 使用真實 actor／manager、記憶體 UART／state／WAL；RPC 以 `dumps(..., &RPC_WIRE)` 比對完整回覆，另斷言沒有額外 close、沒有額外 open、attached_real_path 保留、等待回覆與帳本清空。沒有以 READY 代替 handle 保留證據。

本缺陷可由記憶體 UART 確定性重現，不需要實體 regression case。沒有 Windows 原生、com0com 或硬體實跑證據。

## 不成立主張

無。

## SPEC-GAPS

本輪無新契約缺口。一般 session 的 DEVICE_STILL_HELD 回覆形狀及 S24 既有五項黑箱失敗未修改、未重跑：本輪沒有改 attach_open_failed 的回覆建構。其他分支／子項的 finding 不處理；擁有者／硬體項目維持 BLOCKED-OWNER。

## 既有碼待辦

下列審查背景不屬本輪新增 High，沒有另行結案：完整 shutdown 與 parked close 遲到回條的停止順序；無 release 時 parked 關閉期間重插被舊拔除事件覆蓋；保管開埠途中 clear 的 DETACHED／CLEARED 差異；一般 attach Held 錯誤形狀。

起點 `5f679551` 的 workspace＋test-fakes 已實跑完成：**3469 passed／27 failed／51 ignored**（`i24-r2-baseline-workspace-features.log`），失敗為 CLI 26 項及 custody DR1-d。另以起點行為單跑不含 test-fakes 的三個 CLI target，**117 passed／26 failed／3 ignored**（`i24-r2-baseline-cli.log`）。完整基線啟動時工作樹 diff 為空；沒有切換、修改其他 worktree 或還原本 worktree 的 production 來源。

DR1-d 的完整 id 為 `sw-core::custody::test_dr1_d_release_not_enqueued_keeps_ledger_then_next_release_completes`，起點已失敗於預期 ok:true、實際 DRAIN_STALLED。CLI 失敗集合：

```text
sw-cli::mgmt_doctor::overall_ok_ignores_advisory_failures_only
sw-cli::mgmt_doctor::python_runtime_nonzero_rc_is_unrunnable
sw-cli::mgmt_doctor::pyyaml_import_failure_is_not_installed_with_fix
sw-cli::mgmt_doctor::runtime_spawn_error_is_unrunnable_not_not_installed
sw-cli::mgmt_doctor::runtime_timeout_is_unrunnable_not_not_installed
sw-cli::mgmt_doctor::s1m241_doctor_code_mismatch_is_advisory
sw-cli::mgmt_layout::locate_manifest_env_override_first
sw-cli::mgmt_setup::s1m241_assessment_is_omitted_when_initial_ping_is_unreachable
sw-cli::mgmt_setup::s1m241_assessment_is_omitted_without_installed_fingerprint
sw-cli::mgmt_setup::s1m241_assessment_matching_ping_is_reported_once
sw-cli::mgmt_setup::s1m241_assessment_restart_after_unreachable_is_reported
sw-cli::mgmt_setup::s1m241_assessment_stale_systemd_pending_sudo_is_reported
sw-cli::mgmt_setup::s1m241_assessment_stale_systemd_restart_failure_is_reported
sw-cli::mgmt_setup::s1m241_assessment_stale_systemd_restarts_and_verifies
sw-cli::mgmt_setup::s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero
sw-cli::mgmt_setup::s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero
sw-cli::mgmt_setup::s1m241_failure_restart_error_preserves_python_hint_without_postcheck
sw-cli::mgmt_setup::s1m241_failure_retries_unreachable_then_reports_match
sw-cli::mgmt_setup::s1m241_setup_stale_daemon_without_fingerprint_is_restarted
sw-cli::mgmt_setup::s1m241_setup_stale_service_is_restarted_and_verified
sw-cli::mgmt_setup::s1m241_setup_system_scope_with_sudo_restarts_after_enable
sw-cli::mgmt_setup::s1m241_skips_decision_priority
sw-cli::mgmt_setup::s1m241_skips_flashing_without_force_before_materialize
sw-cli::mgmt_setup::s1m241_skips_no_restart_flag
sw-cli::mgmt_setup::s1m241_skips_no_restart_flag_preserves_mode_transition
sw-cli::mgmt_setup::s1m_c1_setup_verify_polls_reconciled_socket
```

全 workspace clippy 的起點亦為 FAIL：`crates/sw-proto/src/pyre/parse.rs:587` 的 `clippy::never_loop`（`i24-r2-baseline-clippy.log`）。這些失敗保留為 FAIL，不修改既有測試預期、不降級為 SKIP 或 PASS。

## 最終驗證

所有測試直接在本工作樹前景依序執行並等待結束；`TMPDIR=/proc/$$/cwd/target/t` 指回本樹，沒有 namespace 或自製 runner。沒有執行全套 pytest、slice gate、Docker 或出口證據 driver。原始輸出都在 `target/s2-par/wp510r/`，本輪檔名前綴為 `i24-r2-`；統計與完整失敗 id 另存 `i24-r2-results.json`。

| Gate | 最終結果 | 證據檔（上述前綴） |
|---|---|---|
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast -- --test-threads=4` | **PASS：2137 passed／0 failed／7 ignored**；新增 4／4，前輪安全網回歸保留 | `affected-final.log` |
| `cargo test --workspace --no-fail-fast -- --test-threads=4` | **FAIL：1629 passed／26 failed／46 ignored**；失敗 id 與起點不含 test-fakes 的 CLI 26 項完全相同 | `workspace.log`、`baseline-cli.log` |
| `cargo build --workspace --all-targets` | **PASS**，exit 0 | `build.log` |
| `cargo clippy --workspace --all-targets --features test-fakes` | **FAIL**，exit 101；與起點相同的 `sw-proto::pyre::parse` never_loop | `clippy.log`、`baseline-clippy.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | **PASS**，exit 0 | `clippy-affected.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu`，另含 `--features test-fakes` | **PASS**，兩者 exit 0；僅交叉編譯 | `windows.log`、`windows-features.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | **PASS**，exit 0 | `cfg.log` |
| `python3 -m tools.contracts check` | **PASS**，ok:true、findings 0 | `contracts.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 5f679551` | **PASS**；結構、集合、selector、原有順序與欄位保留，只新增 4 | `lock.log`、`l1-add.log` |
| `python3 -m pytest --noconftest -q --basetemp=target/s2-par/wp510r/i24-r2-pytest-fragment tests/test_changelog_fragments.py` | **PASS：1 passed／0 skipped／2 marker warnings** | `fragment.log` |
| `python3 -m policy_check --repo .` | **PASS：25 pass／0 fail／2 warn** | `policy.log` |
| `git diff --check` | **PASS** | 提交前核對 |

Rust 警告以訊息及檔案去重後比對：build **18→18**、受影響 clippy **61→61**、Windows all-targets＋test-fakes **32→32**，新增集合皆空（`warning-comparison.json`），沒有增加 lint suppression。policy 的兩個 advisory 為既有 R-19 工作流程解析提示及 R-22 懸空文件引用。fragment 是純檔案測試；為遵守不探查 production daemon／狀態的限制，以 `--noconftest` 執行，沒有刪除或略過案例；因此兩個 marker 未由全域 conftest 註冊而產生 warnings。

DR1-d 在起點及第一遍受影響 suite 為 FAIL，在最終完整受影響 suite 為 PASS；保留所有輸出，不以「flaky」或無證據的環境假設放行，也不宣稱本輪修復該既有問題。workspace 26 項及全 workspace clippy 的失敗均有本輪起點證據，相關 `sw-cli`／`sw-proto` 來源相對 `5f679551` 完全未改。

交付為本地 commit；未 push、PR、merge 或部署。Python 基線、contracts、golden、既有凍結斷言未修改。

## REMAINING

REMAINING: none
