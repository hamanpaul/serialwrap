# wp510r 第 20 項第 2 輪：未接受交接的關閉責任

起點：`849e605f9c5624ddf28cf595dbed2972350fa6b1`，分支 `feature/rust-refactor-p8`。

## 根因與規劃

接受 P8-849e605-1（High）。前輪將同步關閉移到獨立工作者，卻沒有在同一臨界區轉移帳本 owner；`finish_worker_op` 仍把 attach 執行緒返回當成可完成 release watch 的依據。這是前輪盤點中「資源移交與實際關閉終態混用」的遺漏，與原三次整包審查的共同根因相同；原完整盤點見 `wp510r-i20-close-lifecycle.md`。

| 位置／執行緒 | manager 鎖 | 現況與本輪規劃 |
|---|---|---|
| `rebind_detached`／attach 工作者 | 存 handle 時持鎖；binding 落盤與外部 attach 請求在鎖外 | 用記憶體落盤屏障插入正式 route 的 release，讓 BeginExternalAttach 被停止待決拒絕 |
| `close_unoffered_custody`／attach 工作者等呼叫者 | 取出 Held 時持鎖 | 在取出 handle、轉 Closing 的同一臨界區把 owner 改成 Reaper；保留 token、取消、首次期限與 watcher |
| `spawn_custody_closer`／獨立關閉工作者 | close 不持鎖 | 維持非同步；close 的成功／失敗回條才是終態依據 |
| `finish_custody_close`／關閉工作者 | 結算時持鎖 | 檢查 Failed 終態與 attach 收尾間的發布順序；原錯誤及告警只輸出一次 |
| `finish_worker_op`／attach 工作者 | Condvar 等待會釋鎖；結算時持鎖 | 等同 token 的 Closing 終態，成功才完成 release watch；失敗保留 Failed、永久阻擋及 T-1，不把工作者返回視為成功 |

統一規則：handle 交給 closer 時，關閉責任必須同步交給 Reaper。attach 作業及 release watch 存活到同 token 的真實終態；以帳本 Failed 終態為準，不依靠另一執行緒稍後才寫入的旗標。manager 鎖內只轉移所有權與結算，不執行 close、不等待 I/O。

驗證規劃（production 修改前記錄）：

1. 新增確定性回歸：正式流程產生 parked B3，COM0 原裝置拔除後由 bootstrap 接手，在 binding 落盤暫停；正式 route release 已進 RECOVERING 後放行 binding，再暫停 close。分別覆蓋期限內成功、逾期後遲到成功、關閉失敗。
2. 所有回歸先在起點 production 上確認行為紅燈；斷言回條前無成功、handle 仍開啟、帳本 Closing、在途作業仍存在、原 token 無 terminal，並檢查單次關閉與完整 release 回應。
3. 失敗依 T-1 等首次期限回 DRAIN_STALLED，Failed 與一次 by-id／原錯誤告警，後續 release 立即回 STALL、attach 不重開。成功回條才 RELEASED 並落盤。
4. 新 Rust 節點只以 regen_lock `--l1-add-only` 登記。跑受影響 core／daemon（含 test-fakes）、workspace、build、Clippy、Windows GNU 交叉檢查、cfg、contracts、lock、fragment pytest 及 policy；任何失敗須逐名在本輪起點重現。
5. 不改 Low、已裁決項目、Python 基線、contracts/golden 或範圍外既有碼；不跑出口 driver、全套 pytest、slice、Docker；不 push。

## 實作與審查 finding

| finding | 本輪處理 |
|---|---|
| P8-849e605-1（High） | 接受並修正：`close_unoffered_custody` 原子移交 Reaper owner；`finish_worker_op` 保留作業與 watch 等實際終態，並自同 token 的 Failed 終態讀取結果，避開 closer 解鎖後才發布旗標的窗口 |
| A 段已修 High／Medium | already fixed：保留現行認領、退休 drain、bind／clear 等待、ActorId、共用期限、停止回收與 wake sender 修正；本輪不重做實作，以完整受影響 crate 回歸核對 |
| P8-057a065-4（Medium） | 依 2026-11-10 裁決不處理不存在的 actor「panic 超限退出」；既有停止機制與回歸保留 |
| A 段標未修的 P8-591d2ff-1、P8-0896933-1、P8-c89cd78-1 | 均為 Low，依 brief 不修，交編排者 low-backlog |

A 段有逐名列出的 High／Medium 狀態（沿用審查已核對的提交，本輪完整回歸保留）：

| finding | 狀態 |
|---|---|
| P8-7bb9935-1、P8-7bb9935-2、P8-7bb9935-3 | already fixed `4fca6390` |
| P8-7bb9935-4 | already fixed `424aee24` |
| P8-a07fdfb-1 | already fixed `02bd822c` |
| P8-add3ca0-1、P8-2c5eb6b-1、P8-2c5eb6b-2、P8-2c5eb6b-3 | already fixed `c3f78960` |
| P8-6ca9bc8-1、P8-0c50830-1、P8-990a04b-1 | already fixed `c3f78960` |
| P8-398b85f-1、P8-21482ec-1 | already fixed `e35edc09` |
| P8-e35edc0-1 | already fixed `5e357c76` |
| P8-6536d16-1 | already fixed `9ab75ab0` |
| P8-9ab75ab-1 | already fixed `f661291b` |
| 原 r3 的持鎖 close、回收者同步 close、失敗無告警三項 Medium | already fixed `849e605f`；相關第 20 項回歸仍通過 |

前輪未逐名列出的 37 項，以及 P8-566d90b-1、P8-d7af8df-1／-2，維持所提供審查的既有結案狀態；沒有虛構編號或重開範圍。

本輪沒有新增 RPC、事件或效果；原關閉工作者與終態結算仍屬資源收尾，在 Open／Pending／Permanent 都執行，不啟動新 attach。

## 先紅後綠與接線證據

production 尚未修改時，先在 `849e605f` 新增三個 core 回歸及一個 ServiceRig 回歸，記錄行為紅燈：

| 新節點（皆以 `wp510r_i20_r2_` 開頭） | 起點紅燈 |
|---|---|
| `unoffered_close_success_waits_for_receipt` | close 仍暫停，RELEASED 已落盤 |
| `unoffered_close_late_success_settles_stalled_release` | 同上，首次期限尚未到就提前完成 |
| `unoffered_close_failure_keeps_release_stalled` | 同上，失敗回條前就回成功 |
| `rpc_release_waits_for_unoffered_close_receipt` | 完整正式 RPC 回 `ok:true`、`state:RELEASED`、`drain_stalled:false`、`drain_timeout_epoch:null`，但 handle 仍開啟、原 token 沒有終態 |

紅燈原文：`target/s2-par/wp510r/i20-r2/red-custody.log`（0 passed／3 failed）、`red-rpc.log`（0 passed／1 failed）。修正後同一組測試全綠，未改斷言。

三個 core 節點涵蓋期限內成功、close 暫停超過首次期限後遲到成功，以及 close 失敗。回條前都檢查 Closing、Reaper owner、handle 開啟、原 token 無終態、attach 作業存活、release 不回覆且未落盤。成功回條後同 token Closed、關閉一次、不重開、RELEASED 落盤及 drain false/null；失敗檢查原錯誤 Failed、CloseFailed、永久阻擋、告警恰好一次，等滿首次期限才回 DRAIN_STALLED，期限後的 release 小於一秒回 STALL。

RPC 路徑：`Service::rpc("device.release")` → `route_with_selector` → actor Release／`ClaimByIdForRelease` 登記 attach 作業 watch → `rebind_detached` 的 BeginExternalAttach 被停止待決拒絕 → `close_unoffered_custody` 原子交出 Reaper 所有權 → 獨立 closer 的實際 close → `finish_custody_close` 配對原 token 終態 → `finish_worker_op` 結算 watch → actor `CustodyTerminal`／`CustodyPoll` 完成 RELEASED 與落盤。ServiceRig 在 close 暫停期間透過 `session.get_state` 確認 RECOVERING，放行後以 RPC_WIRE（`j`）逐位元組比對完整成功回應；UART 替身只開一次、關一次、零 PROBE／登入 TX。

記憶體 RigFs 新增一次性落盤屏障，只供測試，呼叫 hook 前會先釋放其鎖。沒有新增 production 測試鉤子。這個錯誤能由記憶體序列埠、真 actor／manager／Service 的交錯完整覆蓋，不需要硬體 regression case。

## 不成立主張

無。

## SPEC-GAPS

本輪沒有需要擴權或新增裁決的缺口。既有 S24 attach 回應形狀缺口未涵蓋、未重跑；本輪未改 attach 回應形狀。owner／硬體項目維持 BLOCKED-OWNER，沒有原生 Windows 或 com0com 實跑證據。

## 既有碼待辦

保留前輪審查列出的完整 shutdown／parked 遲到回條停止順序、無 release 介入的 parked 關閉重插交錯、保管開埠途中 clear 的狀態差異；不以本輪回歸宣告全部結案。CLI 既有失敗逐名證據列於下節；不修改範圍外既有碼。


## 驗收補充：起點的 pin／clear 間歇失敗

第一次完整受影響測試出現 `session_manager::pin_then_clear_on_attached_session_applies_pin`：等待「clear 後套用 pin」逾時（`tests/support/mod.rs:1917`）。沒有把它直接歸因環境或降低斷言。保存本輪檔案後，以 `git show 849e605f:<path>` 完全還原 `sw-core/src/session_manager.rs` 與原測試支援 `tests/support/mod.rs`，測試檔本來就未修改；`baseline-pin-source-diff.log` 為空。前景重跑相同完整 `--test session_manager`：第 1～9 次各 143 passed，第 10 次 **142 passed／1 failed**，相同 id、相同等待逾時。輸出 `baseline-session-manager-{1..10}.log` 全數保留。已恢復本輪檔案並以 cmp 確認與保存內容一致。

這證明該失敗在本輪起點已存在；不宣稱已查明其根因，也不以「環境 flaky」放行未證明的失敗。本輪不修改該案例或 pin／clear 實作，列入既有碼待辦。


CLI 起點與 workspace 的失敗集合逐名相同（`failure-comparison.json`，26 項）；mgmt_doctor 6、mgmt_layout 1、mgmt_setup 19：

- `locate_manifest_env_override_first`
- `overall_ok_ignores_advisory_failures_only`
- `python_runtime_nonzero_rc_is_unrunnable`
- `pyyaml_import_failure_is_not_installed_with_fix`
- `runtime_spawn_error_is_unrunnable_not_not_installed`
- `runtime_timeout_is_unrunnable_not_not_installed`
- `s1m241_assessment_is_omitted_when_initial_ping_is_unreachable`
- `s1m241_assessment_is_omitted_without_installed_fingerprint`
- `s1m241_assessment_matching_ping_is_reported_once`
- `s1m241_assessment_restart_after_unreachable_is_reported`
- `s1m241_assessment_stale_systemd_pending_sudo_is_reported`
- `s1m241_assessment_stale_systemd_restart_failure_is_reported`
- `s1m241_assessment_stale_systemd_restarts_and_verifies`
- `s1m241_doctor_code_mismatch_is_advisory`
- `s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero`
- `s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero`
- `s1m241_failure_restart_error_preserves_python_hint_without_postcheck`
- `s1m241_failure_retries_unreachable_then_reports_match`
- `s1m241_setup_stale_daemon_without_fingerprint_is_restarted`
- `s1m241_setup_stale_service_is_restarted_and_verified`
- `s1m241_setup_system_scope_with_sudo_restarts_after_enable`
- `s1m241_skips_decision_priority`
- `s1m241_skips_flashing_without_force_before_materialize`
- `s1m241_skips_no_restart_flag`
- `s1m241_skips_no_restart_flag_preserves_mode_transition`
- `s1m_c1_setup_verify_polls_reconciled_socket`

## 驗收結果

所有命令都在本工作樹前景完成；輸出位於 `target/s2-par/wp510r/i20-r2/`。

| gate | 實際結果／輸出 |
|---|---|
| 新 core 回歸 | 3 passed；`green-custody.log` |
| 新正式 RPC 回歸 | 1 passed；`green-rpc.log` |
| `cargo test -p sw-core -p sw-daemon --features test-fakes --no-fail-fast` | core **1718 passed／1 failed／1 ignored**，唯一失敗 pin／clear 已在起點重現；daemon **389 passed／0 failed／6 ignored**；`test-affected.log` |
| 修正來源恢復後，同範圍 session_manager 重跑 | **143 passed／0 failed**；`session-manager-final.log`。core 去重共 1719 個案例皆已通過，但保留初跑失敗紀錄，不把初跑命令改記 PASS |
| `cargo test --workspace --no-fail-fast` | **FAIL：1629 passed／26 failed／46 ignored**；`test-workspace.log`。與起點 CLI 失敗集合逐名完全相同 |
| 起點 CLI 三檔 | **FAIL：117 passed／26 failed／3 ignored**；`baseline-cli.log`，production diff 為空 |
| `cargo build --workspace --all-targets` | exit 0；`build.log` |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | exit 0；`clippy.log` |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu` | exit 0；`windows-check.log`，只代表交叉編譯 |
| 新增 Rust 警告 | **0**；對照起點 849e605f 的前輪最終輸出，build 18／Clippy 61／Windows check 32 種診斷均未增加；`warning-comparison.json` 排除摘要與行號位移，比對警告文字及來源檔案 |
| cfg boundary | exit 0；`cfg.log` |
| contracts | ok=true、findings 0；`contracts.log` |
| L1 add-only | **新增 4／移除 0**，只改 s21_core.linux；其他節點、selector、順序保留；`l1-add.log` |
| `regen_lock.py --check --base 849e605f` | exit 0，案例鎖與 L1 結構／基準比對通過；`lock-check.log` |
| fragment pytest | **1 passed**；`fragment.log` |
| policy | **25 PASS／2 WARN／0 FAIL**；R-19、R-22 既有 advisory，`policy.log` |
| 增量差異 | `git diff --check` 通過；不改 Python、contracts／golden 或既有測試斷言 |

Python fragment 測試以 `python3 -m pytest --noconftest -q tests/test_changelog_fragments.py --basetemp target/s2-par/wp510r/i20-r2/pytest-tmp` 執行，避開會探測 production daemon／設定／狀態的 conftest；測試僅讀取 fragment 並在指定 basetemp 寫測試資料。兩筆 UnknownMarkWarning 來自未載入 conftest 的 marker 定義，測試判定不變。

TMPDIR 使用 `/proc/<前景 shell PID>/cwd/target/t`，實際落在本工作樹，維持 daemon 測試 socket 的短絕對路徑；沒有 namespace、自製隔離 runner 或出口證據 driver。L1 收集器只用 `--list-only` 登記節點，沒有執行出口驗收。沒有新增 FAIL→SKIP、刪節點或放寬 judge；既有 ignored 保持原狀。

## REMAINING

REMAINING: none
