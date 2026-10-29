# P6（Slice 2.2）第 6 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p6`（本輪唯一寫入者），起點 `2b6f8ee`，終點 `2fb3db0`；全部以 fast-forward 推送，未被拒。
- 環境：Rust 1.96.1（rust-toolchain 釘定，含 rustfmt、clippy、`x86_64-pc-windows-gnu`＋mingw-w64）、Python 3.12.3＋PyYAML 6.0.3＋pytest 9.0.3＋jsonschema 4.10.3（CI 釘定版）、無 pyserial；**全部測試與 gate 以一般使用者 `ubuntu`（uid 1000、CapEff=0）前景執行**，全套 pytest 起點與終點皆在獨立 clone、同一環境、序列執行，跑前確認無洩漏的 throwaway serialwrapd。
- 合併：主線 `897a3cd`（8caf6c4）、主線 `8963d07`（4cc7f02，本輪期間新到）、P5 `86b54d8`（c74f158；P5 第 6 輪回報於 13:12Z 出現，等待約 1 h 50 m）。L1 鎖只增，唯一例外為 OD-55 已授權、由 P5 以 `--allow-removal` 逐名移除的 `s21_core.linux` 27 列。
- 處理的審查與裁決：`45b82e4_2b6f8ee-sol.md`（開工時最新）與本輪期間到的 `2b6f8ee_4cc7f02-sol.md`（結束前再 fetch `docs/parity-amendments` `f469348`）、`rulings/p6.md` 末三列。結束前新到的裁決：rulings/p7.md P7-9065f14-1（交付點 P6 第 7 輪，見 §2；本分支早已落實）；rulings/p5.md 六列（py_parity_full、能力集合 section、明確 ids、OD-64～OD-66）與 s1.md／p7.md 其餘列皆屬 P5／P7／S1，P6 無動作。

## 1. Commit 清單（2b6f8ee..2fb3db0）

| commit | 內容 |
|---|---|
| 8caf6c4 | merge 主線 897a3cd（只改 `tests/test_rust_win_scripts.py`） |
| d72afac | test（紅燈）：OD-53 由 selector 推導 `--caps`、推導不出記 EVIDENCE_MISSING（4 案；現行 gate 對 `--include-ignored cap_posix_pty__` 仍判 PASS） |
| 12318a5 | fix：`s22_gate_check.derive_caps`／`collect_argv`／`--plan`；changelog `rust-s22-od53-caps-derive.md` |
| 4cc7f02 | merge 主線 8963d07（0.4.0 release 前置、OD-48）：`behavior_changes.yaml` SC-37＋SC-39 兩筆保留、`test_contracts_catalog.py` SC 集合取聯集、`proc_scan/posix.rs`（P5 檔）取 P5 `d657a7c` 解法＋套回 P6 Task 8 的 56 行；L1 鎖 +98 |
| c74f158 | merge P5 86b54d8：L1 鎖三方合併（P6 ∪ P5 − OD-55 移除 27 列），`test_contracts_catalog.py` 取 P6 版（P5 版為子集） |
| 04d5f25 | test：`test_contracts_slice22.py::test_sc30_reserved_for_p7` 的精確 SC 集合納入 SC-39（4cc7f02 引入的確定性回歸，終點全套抓到；即 sol P6-4cc7f02-1） |
| a78f106 | docs：p6-fix-responses 第六輪、slice22-exit-report 第 6 輪 gate 紀錄（#185 段落不變，重判仍 PASS） |
| 2fb3db0 | docs：記錄 `2b6f8ee_4cc7f02-sol` 與 P7-9065f14-1 的處理 |

每組檢查（非 root）：cargo test（8caf6c4／12318a5／04d5f25 無 crate 變更；4cc7f02 workspace 2179 passed；c74f158 workspace 首跑 `sw-pal::lock::lock_conformance_suite` 失敗 1 次、`--no-fail-fast` 重跑 2183 passed／0 failed，見 §7）、`cargo check --workspace --target x86_64-pc-windows-gnu`（皆綠）、`check_cfg_boundary.py`（0）、`tools.contracts check`（ok）、`policy_check --repo .`（每組 25 pass／0 fail／2 warn）、`regen_lock.py --check`（OK；c74f158 另跑 `--check --base origin/feature/rust-refactor` OK）、觸及的 Python 測試（`test_s22_gate_check.py` 21 passed、`test_l1_judge.py`／`test_contracts_catalog.py`／`test_acceptance_slice22.py`／`test_slice21_harness_shared_tty.py` 除 `test_l1_judge` 2 支（`s21_core.windows`，見 §2）外全綠）。

## 2. High／Medium 與裁決的處理

| 來源 | 項目 | 處理 |
|---|---|---|
| rulings/p6.md「OD-53 selector 記錄 caps」 | 不改鎖 schema，gate 由 selector 推導 `--caps`，推導不出即 EVIDENCE_MISSING | **12318a5**（紅燈 d72afac）：test-binary 參數恰為 `--ignored cap_<cap>__…` → `--caps <cap>…`；不含 ignored 篩選 → 不需 caps；`--include-ignored`、無篩選、混非 cap 篩選或其他旗標、`--` 之前 → 推導不出 → 該集合 EVIDENCE_MISSING（result 併入 INSUFFICIENT），其 passed 列不充當逐案證據、failed 列仍判 FAIL。各集合輸出 `caps`／`collect_argv`；`--plan` 只依鎖印出 linux cargo 集合的收集命令。現行鎖全部可推導（console_conformance_pty → `posix_pty`，其餘不需）；本輪 gate 的 cargo 收集命令全取自 `--plan` |
| rulings/p6.md P6-e9b5c61-4 | P6 部分已修、剩餘由 P5 依 OD-55 交付 | 依裁決不處理。merge P5 後重驗 `test_l1_lock_cargo_sets_exclude_ignored_tests`：**linux 已清，仍紅，只剩 `s21_core.windows` 13 列**（P5 依 OD-55 待 Windows 原生收集，LOCAL-WINDOWS-TODO）；`test_l1_lock_frozen_set_excludes_capability_gated` 同屬 `s21_core.windows`（5 列 cap） |
| rulings/p6.md #185 PROCESS-EVIDENCE | 編排者確認符合原意 | 無動作；出口報告追加第 6 輪段落後重判 #185 仍 PASS |
| `45b82e4_2b6f8ee-sol.md` | B 段 NO FINDINGS；A 段只剩 P6-e9b5c61-4 | 無 High／Medium 待修 |
| rulings/p5.md `test_two_daemons_same_tty` harness（P5 照修） | P5 `4f8acdf` | merge 後 Python 基線 passed；Rust artifact 於 gate blackbox_rust passed |
| `2b6f8ee_4cc7f02-sol.md` B | P6-4cc7f02-1 Medium：`test_sc30_reserved_for_p7` 未納入 SC-39 | **已修 04d5f25**（終點全套先抓到，修法與審查建議相同：期望集合加 SC-39、不放寬為子集合比對） |
| 同上 | P6-4cc7f02-2 Low：OD-53 推導規則兩份實作（`test_l1_judge._selector_explicit_caps` 與 gate `derive_caps`） | 依收斂規則 Low 不修（gate 判準較嚴、失敗即擋，不會誤判 PASS） |
| rulings/p7.md P7-9065f14-1 | P6 自 `slice22_evidence_map.yaml` 移除 `l15-delayed-reply` 並補測試（交付點 P6 第 7 輪） | **本分支早已落實**：map 無此鍵（OD-34 修訂 #1），`test_contracts_slice22.py::test_slice22_case_set_frozen_and_mapped` 斷言其不在 `slice_2.2`、在 `slice_2.3`，且 map 鍵＝凍結 `slice_2.2`。P7 分支的 map 為舊版（`l15-delayed-reply: {status: PENDING}`），P7 merge 本分支即解除 |

## 3. Slice 2.2 出口 gate（`s22_gate_check.py`，非 dry-run，非 root）

HEAD c74f158；release artifact `cargo build --release -p sw-cli -p sw-daemon`（daemon sha256 `10326582050f…`）；cargo 集合帶 `--features test-fakes`、收集命令取自 `--plan`；報告 `target/s22-gate/r6-261029-133213/s22-gate.json`（未提交）。loadavg 開跑 0.25／0.41／0.48（13:32:13Z）、結束 0.51／0.30／0.39（13:50:01Z），步驟邊界取樣最高 2.67（release build 後）。**result = FAIL**。

| 集合 | 判定 | 數字／原因 |
|---|---|---|
| `s22_gate:cargo_workspace`/linux | FAIL | 2184 passed、1 failed、38 skipped；唯一失敗 `sw-pal::serial::posix::tests::invalid_serial_fd_is_a_poll_wait_error`（P5 檔，fd 重用競態，§7）。同範圍以同一命令重跑兩次皆 **2185 passed、0 failed**（失敗集合不同） |
| `s22_gate:console_conformance_pty`/linux | PASS | 31 passed、3 skipped（cap_windows_com）；`--caps posix_pty` 由 `--plan` 推導 |
| `s22_gate:blackbox_rust`/linux | **PASS** | 117 passed、8 skipped（上輪唯一失敗 `test_two_daemons_same_tty` 轉綠） |
| `s22_gate:py_parity`/linux | PASS | 12 passed |
| `s22_gate:parity_py`/linux | PASS | 34 passed |
| `s22_gate:win_test`/windows | INSUFFICIENT | 集合未登記（LOCAL-WINDOWS-TODO） |
| map 覆蓋／prereqs | PASS／PASS | 93／93；`check_p6_prereqs --strict` |

逐案 93：**PASS 31／BLOCKED-OWNER 62／FAIL 0／INSUFFICIENT 0**。L1.5／func-test 22 個參照（bridge 參照帶 `--selftest-report`，自測 PASS）於 Rust release artifact **全部 PASS**（26 份 manifest 皆 `artifacts.daemon.implementation=rust`、sha256 與 artifact 相符）。parity_rows 23：**PASS 22／BLOCKED-OWNER 1（#108）**。同一驅動於 merge P5 前（HEAD 12318a5）的預跑 `r6pre-261029-121059`：cargo_workspace PASS 2132／38、blackbox_rust FAIL 116／1（`test_two_daemons_same_tty`），其餘同上。

## 4. 全套 pytest（非 root、同環境、無 pyserial、獨立 clone、序列執行）

| | failed | passed | skipped |
|---|---|---|---|
| 起點 8caf6c4（起點 2b6f8ee＋主線 897a3cd） | 11 | 5585 | 221 |
| 終點 c74f158（含主線 8963d07、P5 86b54d8） | 11 | 5688 | 219 |

終點相對起點**轉綠 1 項**（`test_two_daemons_same_tty`），**超出基準 1 項**：`tests/test_contracts_slice22.py::test_sc30_reserved_for_p7`——4cc7f02 併入主線後多出 SC-39（OD-48），本檔精確 SC 集合未同步的確定性回歸（合併時漏跑本檔，我引入），04d5f25 修正（`test_contracts_slice22.py`＋`test_contracts_catalog.py`＋`test_resource_markers.py` 140 passed）；修正後失敗集合為起點的真子集。
其餘 10 項皆起點即紅：`test_l1_judge` 2（`s21_core.windows`，OD-55 windows）、pyserial 未安裝 4＋`test_doctor::test_pyserial_ok_when_importable`（環境）、`test_rust_bench_tools::test_cargo_workspace_tests_windows_native`（需 Windows）、`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence`（雲端無本機 Stage 1 證據，EVIDENCE_MISSING）、slice21 `test_queue_full_backpressure`（Python 基線，P5 既有碼待辦）。
04d5f25 之後只有 docs commit，未重跑全套。起點全套執行期間，主工作區另跑過輕量檢查（`test_s22_gate_check.py` 等 46 案、policy_check；步驟邊界 loadavg ≤ 1.2）；終點全套期間無其他測試。

## 5. 不成立主張

無。（參考：P5 第 6 輪把 P6 SPEC-GAP #4「非 root holder 掃描 fail-closed」列為不成立；本輪非 root gate 的 L1.5 22 參照與 blackbox_rust 全 PASS，與其「無法重現」一致，未見反例。）

## 6. SPEC-GAPS

1. 沿用：#108 file 傳輸部分 → P8 計畫須加必要案例（`test_existing_human_owner_window_is_deferred_before_suspend` 對應 Rust 案例）。
2. 沿用：`attach_request_timeout_closes_pty` 的「actor 卡住」fault hook 需改 P5 `actor.rs`；L1.5 結果檔（`s22-l15-results.v1`）仍無官方 producer，本輪以臨時驅動（逐參照執行 `runner.py`，bridge 參照加 `--selftest-report`，並記錄 manifest 的 daemon implementation／sha256）產生。
3. OD-53 推導目前只在 s22 gate；P5 的 `slice21_gate.sh` 仍手寫 `--caps posix_pty`。rulings/p5.md 新列（能力集合補 section、以 `--caps <cap>` 實跑）若要與 s22 一致，可改用同一推導規則（P5 檔案，P6 不改）。

## 7. 既有碼待辦（不修）

- （P5）`crates/sw-pal/src/serial/posix.rs::invalid_serial_fd_is_a_poll_wait_error`：測試 `close()` pipe fd 後以同一號碼 poll 期望 POLLNVAL，同 binary 他執行緒會立即重用該號碼（實得 `Read(EINVAL)`）。sw_pal lib 測試整支連跑 100 次 **5 敗**、單跑該案 200 次 **0 敗**。建議改用不可能被配置的 fd 號碼（如 ≥ `RLIMIT_NOFILE`）。本輪 gate cargo_workspace FAIL 的唯一原因。
- （P5）`crates/sw-pal/src/conformance/lock_suite.rs` `[stale_socket_reclaimed]`（`sw-pal::lock::lock_conformance_suite`；第五輪 gate、P5 第 6 輪 gate、本輪 c74f158 首跑各失敗 1 次）：`lock` 測試整支連跑 150 次 **2 敗**（皆 `AlreadyRunning`）、單跑 150 次 **0 敗**。推測同 binary 其他測試 spawn 子行程時，fork 至 exec 之間子行程持有剛 drop 的 listener fd，使 probe 連線成功；建議 stale socket 於子行程建立或把 spawn 類測試與此案序列化。
- （P5）`tests/contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure`（Python 基線，P5 已列既有碼待辦）：起點基準即失敗。
- （計畫文件）`docs/rust-refactor/plans/07-slice22-console.md` Task 13 Step 4 的收集命令與 L1 鎖 selector 不一致（`console_conformance_pty` 仍寫 `--include-ignored cap_posix_pty__` 且無 `--caps`、`cargo_workspace` 少 `-- --skip cap_`；`2b6f8ee_4cc7f02-sol` 既有碼待辦）。照文件收集會判 INSUFFICIENT（失敗即擋）；本輪已改以 `s22_gate_check.py --plan` 收集，計畫文字未改。
- P5 於本輪 merge 之後推送 `54dd588`（lock_suite 失效 socket、佇列背壓案例、PTY fd 繼承）與 `1b83da9`，可能已處理上列兩項；本輪未再 merge，留待下一次 merge P5 重驗（`invalid_serial_fd_is_a_poll_wait_error` 是否涵蓋未確認）。
- 沿用：`changelog.d/p5-r4-od34-44.md`、`p5-r4-od39-runner.md` 的 `type` 不在 `TYPE_TO_SECTION`；workspace `cargo fmt --check` 既有差異。

## 8. OWNER-BATCH

無新增。

## 9. LOCAL-WINDOWS-TODO

- Task 12 全部（WSAPoll mux、Winsock TCP console、com0com Serial console、W-1～W-7、`windows_console_native.rs`、`win-blackbox.ps1 -Subset console`）、`s22_gate:win_test/windows` 集合登記與報告。
- parity #108：Windows TCP `attach_console` 不開 lease（`slice22-owner-runbook.md` #108 節）。
- `co-02-line-backspace`、`df20-recover-reboot-stale-interactive-id` 的 Windows 執行。
- （P5）OD-55 windows：`s21_core.windows` 13 列移入能力集合後 `test_l1_lock_cargo_sets_exclude_ignored_tests`、`test_l1_lock_frozen_set_excludes_capability_gated` 才會轉綠。

## 10. BLOCKED-OWNER

- 07 BLOCKED-OWNER 總表 #1～#7；對照表 62 案（BO-S22-01～05、BO-S21-07／08／13／14／16）；parity #108（Windows 原生）。
- `f4-interactive-line-cmd-defined` parity 判定（沿用）；SC-28／29／31 `IMPLEMENTED` 須 Step 4 全通過（含 win-test）。

## 11. 本 slice 距出口還缺

1. P5 修 sw-pal 兩個測試競態（§7）→ `cargo_workspace` 穩定 PASS。
2. P5 OD-55 windows（`s21_core.windows` 13 列）→ `test_l1_judge` 兩案轉綠。
3. Windows：Task 12、`s22_gate:win_test`、#108。
4. 62 案 BLOCKED-OWNER 實機證據；之後 SC-28／29／31 才能標 IMPLEMENTED。
5. P8 承接 #108 file 傳輸案例。
6. 出口整包審查（Codex sol＋Claude Opus 雙審）。
