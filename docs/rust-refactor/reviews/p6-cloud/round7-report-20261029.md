# P6（Slice 2.2）第 7 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p6`（本輪唯一寫入者），起點 `2fb3db0`，終點 `81a9ed4`；全部以 fast-forward 推送，未被拒（未使用 `claude/rust-refactor-p6-r7`）。
- 環境：Rust 1.96.1（rust-toolchain 釘定，含 rustfmt、clippy、`x86_64-pc-windows-gnu`＋mingw-w64 13.2）、Python 3.12.3＋PyYAML 6.0.3＋pytest 9.0.3＋jsonschema 4.10.3（CI 釘定版，venv）、無 pyserial；**全部測試與 gate 以一般使用者 `ubuntu`（uid 1000、CapEff=0）前景序列執行**，跑前確認無洩漏的 throwaway serialwrapd。
  - 環境注意：起點全套 pytest 以 `runuser -u ubuntu` 執行，繼承了 root 的 PATH（含 ubuntu 無權進入的 `/root/.local/bin` 等）。第一次 workspace cargo test 因此在 `sw-pal::sysenv_mgmt::run_missing_exe_is_notfound_with_py_message` 得 `PermissionDenied`（execvp 遇 EACCES，非程式缺陷）。之後一律改用乾淨環境（`env -i`＋一般使用者 PATH）；終點全套、cargo、gate 皆在乾淨環境。
- 合併：P5 `3bbb40e`（2fc1486）、`8d824b3`（eb34a01）、`cc25b01`（09115d7）、`8c0c942`（81a9ed4），後三者為本輪期間新到。主線 `feature/rust-refactor` 無新 commit。L1 鎖相對 P6 與 P5 兩邊皆只增不減，`regen_lock --check` 與 `--check --base origin/feature/rust-refactor` 每組皆 OK。每次 merge 後依 rulings/p6.md 跑完整 `tests/test_contracts_*.py`。
- 處理的審查與裁決：rulings/p6.md 末兩列、rulings/p7.md P7-9065f14-1、`2b6f8ee_4cc7f02-sol.md`（開工時最新），以及本輪期間到的 `4cc7f02_c74f158-sol.md`、`2b6f8ee_c74f158-sol.md`。結束前再 fetch `docs/parity-amendments`（`05ad35e`）：新到 `c74f158_2fb3db0-sol.md`（A：P6-c74f158-1／P6-4cc7f02-1 已修 `04d5f25`，P6-c74f158-2／P6-4cc7f02-2 兩個 Low 未修；B：NO FINDINGS；結論：可放行），處理見 §2；rulings 只有 s1.md 新列，P6／P7 無新裁決；P5 第 7 輪回報與 P5 審查屬 P5，P6 無動作。結束前 P5 與主線無本分支未併的 commit。

## 1. Commit 清單（2fb3db0..81a9ed4，first-parent）

| commit | 內容 |
|---|---|
| 2fc1486 | merge P5 3bbb40e（P5 第 7 輪：OD-64／OD-66／OD-67、gate section、sw-pal 測試競態）。文字層無衝突；**語意衝突**：P5 `67b7e26` 把 `crates/sw-core/src/mailbox.rs` test-fakes witness 的 `Some(MailboxItem::Data(_))` 改為只比對 `DataEvent::UartRxChunk`，未涵蓋 P6 的 `ConsoleInputBytes`／`ConsoleDisconnected`，sw-core 編譯失敗（E0004）。於 merge 內解決：恢復比對全部 data item（console 事件同走 data lane，照舊計入超車與 OD-64 FIFO 位置 `remaining`），只有 UartRxChunk 且 epoch 相同者計入 `same_epoch`；只動量測碼 |
| 5cf0306 | test：`test_od34_deferred_cases_absent_from_slice22_evidence_map`（P7-9065f14-1，見 §2） |
| eb34a01 | merge P5 8d824b3（sw-pal fd 重用／fork 時窗競態、slice21 gate `--caps` 改 OD-53 推導），無衝突 |
| 09115d7 | merge P5 cc25b01（`cargo_l1_results.py --caps-only`；s22 gate 不帶此旗標、預設路徑不變），無衝突 |
| b5bfb6b | docs：p6-fix-responses 第七輪、slice22-exit-report 第 7 輪 gate 兩次實跑（#185 段落不變，重判仍 PASS） |
| 81a9ed4 | merge P5 8c0c942（T22 gate 第七輪紀錄、event_latency 證據、`breaker/slice_2.1.snapshots.ndjson` +1 列），無衝突 |

每組檢查（非 root、乾淨環境）：
- `cargo test --workspace --features test-fakes --no-fail-fast`：2fc1486 **2194 passed／0 failed／38 ignored**、eb34a01 同（09115d7、81a9ed4 無 crate 變更）。
- `cargo check --workspace --target x86_64-pc-windows-gnu`：每組綠。
- `check_cfg_boundary.py`（0）、`tools.contracts check`（ok）、`policy_check --repo .`（每組 25 pass／0 fail／2 warn：R-19、R-22）、`regen_lock.py --check`（OK）、`--check --base origin/feature/rust-refactor`（OK）。
- 觸及的 Python 測試：2fc1486 合跑 `test_contracts_*.py`＋P5 觸及的 9 檔＋`test_resource_markers`／`test_l1_judge`／`test_s22_gate_check`／`test_acceptance_slice22`／`test_slice21_skeleton_guards`／`test_slice21_freeze_amend`：800 passed、3 failed（皆起點既有：`test_l1_judge` 2 支〔`s21_core.windows`〕、pyserial 1 支）；eb34a01 579 passed、09115d7 606 passed、81a9ed4 604 passed（各含完整 `test_contracts_*.py`，0 failed）。

## 2. High／Medium 與裁決的處理

| 來源 | 項目 | 處理 |
|---|---|---|
| rulings/p6.md「P6 第 6 輪回歸 `test_sc30_reserved_for_p7`」（照修） | 精確 SC 集合同步 SC-39，不放寬為子集斷言 | 已於第六輪 `04d5f25` 修正（`ids == ({SC-01..SC-29} ∪ {SC-31,34,35,36,37,39}) − {SC-08}`）。本輪起點全套與終點全套皆 passed；本輪三次 merge P5 皆未新增 SC 列（`tools.contracts check` SC=34）。每次 merge 後跑完整 `tests/test_contracts_*.py` 已落實（§1） |
| rulings/p7.md P7-9065f14-1 | P6 自 `slice22_evidence_map.yaml` 移除 `l15-delayed-reply`（承擔依 OD-34 現行歸屬）並補測試 | 對照表自 OD-34 修訂 #1 起即無此鍵（93 鍵＝凍結 `slice_2.2`）。**5cf0306** 補明確測試：逐筆讀 `breaker/slice_2.2.amendments.json` 的 removed 列，斷言對照表無該鍵、`cases.lock.json` 不在 `slice_2.2` 而在目標 slice（`slice_2.3`）、decision＝OD-34；並以 P7 分支殘留的 `l15-delayed-reply: {status: PENDING}` 自檢判定器。紅燈證據：P7 分支舊版對照表（95 鍵皆 PENDING）代入判定器得 2 筆問題（cowork-01-agent-bad-cmd、l15-delayed-reply），本分支 0 筆。P7 merge 本分支即解除 P7 端的 BLOCKED-UPSTREAM |
| `2b6f8ee_4cc7f02-sol.md` A | P6-e9b5c61-4（依裁決不處理） | merge P5 後重驗：`test_l1_lock_cargo_sets_exclude_ignored_tests`、`test_l1_lock_frozen_set_excludes_capability_gated` 仍紅，只剩 `s21_core.windows` 列（OD-55 待 Windows 原生收集，P5 LOCAL-WINDOWS-TODO） |
| `2b6f8ee_4cc7f02-sol.md` B | P6-4cc7f02-1 Medium | 已修 `04d5f25`（第六輪） |
| `4cc7f02_c74f158-sol.md`（本輪期間到；A：P6-4cc7f02-1「未修 1 輪」；B：NO FINDINGS） | P6-4cc7f02-1 | 審查範圍止於 c74f158，早於修正 `04d5f25`；本輪 HEAD 上 passed。應標「已修 04d5f25」 |
| `2b6f8ee_c74f158-sol.md`（本輪期間到）B | P6-c74f158-1 Medium（同一缺陷） | 已修 `04d5f25` |
| 同上 | P6-c74f158-2 Low（`derive_caps` 推導不出時不判報告中的 failed 列，INSUFFICIENT 而非 FAIL） | 依收斂規則 Low 不修（現行鎖 selector 皆可推導；結果為 INSUFFICIENT，不會誤判 PASS） |
| `c74f158_2fb3db0-sol.md`（結束前 fetch 到；結論：可放行） | A：P6-c74f158-1、P6-4cc7f02-1 已修 `04d5f25`；P6-c74f158-2、P6-4cc7f02-2 Low 未修；B：NO FINDINGS | 無待修。審查指出兩個 Low 尚未收入 `low-backlog.md`，屬編排者維護檔，P6 不改 |
| `4cc7f02_c74f158-sol.md` A | P6-4cc7f02-2 Low（OD-53 推導規則多份實作） | Low 不修；P5 `8d824b3` 另在 `slice21_gate_judge.derive_caps` 實作第三份（見 SPEC-GAPS #3） |

本輪無其他 High／Medium 待修。

## 3. Slice 2.2 出口 gate（`s22_gate_check.py`，非 dry-run，非 root）

作法同第六輪：release artifact `cargo build --release -p sw-cli -p sw-daemon`；cargo 集合帶 `--features test-fakes`、收集命令取自 `--plan`（OD-53 推導 `--caps`）；pytest 集合依 L1 鎖 selector 以 `SW_RESOURCE_REPORT` 收集（blackbox_rust 帶 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`＝release artifact、`SERIALWRAP_AB_LABEL=rust-s22`）；bridge 自測 `uart_stdio_bridge selftest --endpoint posix-pty`；對照表 22 個 func-test／scenario 參照逐一以 Rust artifact 執行（bridge 參照帶 `--selftest-report`），產生 `s22-l15-results.v1` 並記錄 manifest 的 daemon implementation／sha256。報告目錄 `target/s22-gate/<tag>/`（未提交）。兩次實跑的 release artifact 相同（daemon sha256 `ba7b7f4d7b004cb0…`、CLI `63e281ccde61ce3b…`；26 份 manifest 皆 `artifacts.daemon.implementation=rust`、sha256 與 artifact 相符）。

| 集合 | `r7-261029-163920`（HEAD eb34a01） | `r7b-261029-170104`（HEAD 09115d7） |
|---|---|---|
| `s22_gate:cargo_workspace`/linux | PASS（2194 passed、38 skipped） | **FAIL**（2193 passed、1 failed、38 skipped） |
| `s22_gate:console_conformance_pty`/linux | PASS（31 passed、3 skipped；`--caps posix_pty`） | PASS（同左） |
| `s22_gate:blackbox_rust`/linux | PASS（117 passed、8 skipped） | PASS（同左） |
| `s22_gate:py_parity`/linux | PASS（12 passed） | PASS（同左） |
| `s22_gate:parity_py`/linux | PASS（34 passed） | PASS（同左） |
| `s22_gate:win_test`/windows | INSUFFICIENT（集合未登記，LOCAL-WINDOWS-TODO） | INSUFFICIENT（同左） |
| map 覆蓋／prereqs | PASS（93）／PASS | 同左 |
| **result** | **INSUFFICIENT** | **FAIL** |

- 兩次皆：逐案 93：**PASS 31／BLOCKED-OWNER 62／FAIL 0／INSUFFICIENT 0**；L1.5／func-test 22 參照全 PASS；parity_rows 23：**PASS 22**（含 #185 PROCESS-EVIDENCE）／**BLOCKED-OWNER 1（#108）**。
- loadavg：`r7` 開跑 0.70／0.85／0.92（16:39:20Z）、結束 0.24／0.23／0.52（16:56:24Z），步驟邊界 1 分鐘值最高 1.46；`r7b` 開跑 0.84／0.55／0.57（17:01:04Z）、結束 0.36／0.32／0.40（17:16:58Z），最高 0.64。
- `r7b` 唯一失敗：`sw-core::lease_selftest::selftest_probe_drain_race_golden`（P5 測試，`8b8a40f`），`lease_selftest.rs:1320` release 回 `ok=false`，約第 250 輪即中止（該執行檔 7.15 s 結束；正常整支 28～32 s）。取證：
  - 同一收集命令（`cargo_l1_results.py … -- --workspace --features test-fakes -- --skip cap_`）重跑兩次：皆 **2194 passed／0 failed**（失敗集合不同）。
  - 該測試執行檔單獨連跑 60 次（每次 1000 輪）0 敗；加註 release 回覆內容的診斷版（未提交）4 路並行連跑 140 次（loadavg ≈ 6）0 敗——合計約 20 萬輪無法重現，未取得 release 拒絕原因。
  - 跑前無洩漏的 serialwrapd；失敗當下 loadavg ≤ 0.64，非系統飽和。
  - 根因未定，**不作放行理由**；`r7b` 的 result 維持 FAIL。P6 本輪未改 session_core／lease／release 路徑（只在 merge 內動 test-fakes witness 量測碼）。
- sw-pal 兩個測試競態（`serial::posix::tests::invalid_serial_fd_is_a_poll_wait_error`、`lock::lock_conformance_suite [stale_socket_reclaimed]`）：本輪乾淨環境 6 次 workspace 執行（commit 驗證 2、gate 2、同範圍重跑 2）皆未出現（P5 `54dd588`／`8d824b3` 已修）。

## 4. 全套 pytest（非 root、無 pyserial、獨立 clone、序列執行）

| | failed | passed | skipped | 其他 | 時間 |
|---|---|---|---|---|---|
| 起點 2fb3db0（繼承 root PATH，見環境注意） | 10 | 5689 | 219 | 1 xfailed | 55 m 46 s（loadavg 0.41→0.82） |
| 終點 81a9ed4（乾淨環境） | 9 | 5754 | 219 | 1 xfailed | 53 m 05 s（loadavg 0.72→0.72） |

終點失敗集合為起點的**真子集**：超出基準 **0 項**；轉綠 1 項（`contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure`，隨 P5 `54dd588` 合入）。終點 9 項皆起點即紅：`test_l1_judge` 2（`s21_core.windows`，OD-55 windows）、pyserial 未安裝 4＋`test_doctor::test_pyserial_ok_when_importable`（環境）、`test_rust_bench_tools::test_cargo_workspace_tests_windows_native`（需 Windows）、`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence`（雲端無本機 Stage 1 證據）。起點與終點的 PATH 差異未在失敗集合上留下痕跡：唯一差異項的轉綠原因為 P5 修正。終點全套執行期間無其他測試。

## 5. 不成立主張

- `4cc7f02_c74f158-sol.md` A 段 P6-4cc7f02-1「未修（累計 1 輪）」：不成立（時序）。該審查範圍為 4cc7f02..c74f158，修正 `04d5f25` 在 c74f158 之後；HEAD 實測 `tests/test_contracts_slice22.py::test_sc30_reserved_for_p7` passed。`2b6f8ee_c74f158-sol.md` 的 P6-c74f158-1 同理。
- `4cc7f02_c74f158-sol.md`「`DataEvent::{ConsoleInputBytes, ConsoleDisconnected}` 由 `Data(_)` 分支涵蓋」：於 c74f158 成立，但 P5 其後 `67b7e26` 把該分支改窄，於本輪 merge 成為編譯失敗（已於 2fc1486 解決，見 §1）。供後續審查參照，非對 P6 的主張。

## 6. SPEC-GAPS

1. 沿用：#108 file 傳輸部分 → P8 計畫須加必要案例（`test_existing_human_owner_window_is_deferred_before_suspend` 對應 Rust 案例）。
2. 沿用：`attach_request_timeout_closes_pty` 的「actor 卡住」fault hook 需改 P5 `actor.rs`；L1.5 結果檔（`s22-l15-results.v1`）仍無官方 producer，本輪仍以臨時驅動產生（作法見 §3）。
3. OD-53 推導規則現有三份實作：`tests/test_l1_judge._selector_explicit_caps`（P6 測試，P5 亦使用）、`scripts/rust/s22_gate_check.derive_caps`（P6）、`scripts/rust/slice21_gate_judge.derive_caps`（P5 `8d824b3`）。判準尚未漂移出誤判 PASS 的路徑；若要收斂為單一實作需跨 P5／P6 檔案，請編排者指定歸屬。
4. P5 檔案 `crates/sw-core/src/mailbox.rs` 的 test-fakes witness（`recv_next`）於本輪 merge 2fc1486 由 P6 調整（涵蓋 P6 的兩個 console DataEvent 變體），請 P5 下次 merge P6 時保留；日後 P5 再改 witness 比對時需涵蓋 P6 的 DataEvent 變體。

## 7. 既有碼待辦（不修）

- （P5）`crates/sw-core/tests/lease_selftest.rs::selftest_probe_drain_race_golden` 間歇失敗（§3 取證）：建議該測試在 release `ok=false` 時印出完整回覆（目前只印 `Bool(false)`），以利下次重現時定位。前一份 sol 報告另記 `session_manager.rs:2348`（P5 區域）於完整 workspace 失敗一次；本輪 6 次 workspace 執行皆未見。
- （計畫文件）`docs/rust-refactor/plans/07-slice22-console.md` Task 13 Step 4 的收集命令與 L1 鎖 selector 不一致（三份 sol 報告皆列為既有碼待辦）；本輪仍以 `s22_gate_check.py --plan` 收集，計畫文字未改。
- （P5，Python 基線）`contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure`：起點基準失敗；P5 `54dd588` 合入後本輪觸及測試與終點全套皆 passed（轉綠）。
- `c74f158_2fb3db0-sol.md` 既有碼待辦：`test_l1_lock_nodeids_subset_of_collect_only` 依 pytest 版本（9.2.0 產生不同 id 而紅）。本環境 pytest 9.0.3，起點與終點皆 passed；P5 `3bbb40e`（OD-67）已為 `test_classify_pyserial_open_error` 加明確 `ids=`，已併入本分支。同報告另記 policy_check R-16 在 Python 3.13 下因 argparse 排版而 fail；本輪以 3.12 執行皆 pass。
- 沿用：`changelog.d/p5-r4-od34-44.md`、`p5-r4-od39-runner.md` 的 `type` 不在 `TYPE_TO_SECTION`；workspace `cargo fmt --check` 既有差異（如 `mailbox.rs` 的 `ConsoleInputBytes` 單行定義）。

## 8. OWNER-BATCH

無新增。

## 9. LOCAL-WINDOWS-TODO

- Task 12 全部（WSAPoll mux、Winsock TCP console、com0com Serial console、W-1～W-7、`windows_console_native.rs`、`win-blackbox.ps1 -Subset console`）、`s22_gate:win_test/windows` 集合登記與報告（本輪 gate INSUFFICIENT 的唯一原因）。
- parity #108：Windows TCP `attach_console` 不開 lease（`slice22-owner-runbook.md` #108 節）。
- `co-02-line-backspace`、`df20-recover-reboot-stale-interactive-id` 的 Windows 執行。
- （P5）OD-55 windows：`s21_core.windows` 列移入能力集合後 `test_l1_lock_cargo_sets_exclude_ignored_tests`、`test_l1_lock_frozen_set_excludes_capability_gated` 才會轉綠。

## 10. BLOCKED-OWNER

- 07 BLOCKED-OWNER 總表 #1～#7；對照表 62 案（BO-S22-01～05、BO-S21-07／08／13／14／16）；parity #108（Windows 原生）。
- `f4-interactive-line-cmd-defined` parity 判定（沿用）；SC-28／29／31 `IMPLEMENTED` 須 Step 4 全通過（含 win-test）。

## 11. 本 slice 距出口還缺

1. P5 查明 `selftest_probe_drain_race_golden` 間歇失敗（sw-pal 兩個競態已修）→ `cargo_workspace` 穩定 PASS。
2. P5 OD-55 windows（`s21_core.windows`）→ `test_l1_judge` 兩案轉綠。
3. Windows：Task 12、`s22_gate:win_test`、#108。
4. 62 案 BLOCKED-OWNER 實機證據；之後 SC-28／29／31 才能標 IMPLEMENTED。
5. P8 承接 #108 file 傳輸案例。
6. 出口整包審查（Codex sol＋Claude Opus 雙審）。
