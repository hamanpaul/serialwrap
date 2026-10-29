# P6（Slice 2.2）第 4 輪雲端回報・續（2026-10-29，第二個 cloud session）

> 同日另一個 cloud session 已提交 [`round4-report-20261029.md`](round4-report-20261029.md)（起點 e9b5c61、終點 08323e8）。本 session 與其**並行**執行同一份排程 prompt：起點為當時的分支頭 24b8012，push 時發現對方另推了 a8c57f0／08323e8，依指示未覆寫其回報，改以本檔補記；分支上已 merge 對方兩個 commit（feb984e）後 fast-forward push。兩份回報不一致之處於 §9 列明，請編排者擇一採用。

- 分支：`feature/rust-refactor-p6`，本 session 起點 `24b8012`，終點 `71b1183`。
- 環境：Rust 1.96.1（含 rustfmt、x86_64-pc-windows-gnu＋mingw-w64 13.2）、Python 3.12.3＋PyYAML 6.0.3＋pytest 9.0.3（CI 釘定版）＋jsonschema 4.10.3、無 pyserial；**全部測試以一般使用者 `ubuntu`（非 root、CapEff=0）執行**。
- 合併：主線 `62ffec7`（e14b4c8，只登錄 OD-45～OD-47）、P5 `3084388`（7faa3c6，lock 只增 5 列）、並行 session 的 `08323e8`（feb984e）。

## 1. Commit 清單（24b8012..71b1183）

| commit | 內容 |
|---|---|
| e14b4c8 | merge 主線 62ffec7（OD-45～OD-47 登錄） |
| 7faa3c6 | merge P5 3084388（OD-45 event_latency L1 結構 gate、OD-46／OD-47、折行回顯）；`l1_required.lock.json` +5（add-only） |
| 4e3cd8a | fix：`console_methods_no_longer_deferred` 改經 `support::data_root()`（24b8012 違反 `check_cfg_boundary.py` test-data-path）；與並行 session 的 a8c57f0 重複，合併時取 a8c57f0 |
| 9979795 | test（紅燈）：`rpc_console::attach_request_timeout_closes_pty`（07 Task 10 deferred 案），以 `--l1-add-only` 登記 `s22_gate:cargo_workspace` |
| 52d49d2 | fix：console_attach 逾時後遲到的 adopt 失敗改以 detach 語意撤回，一併關閉幽靈 human lease |
| 76c78b2 | test(acceptance)：依 OD-40 改以 WAL 黑箱／L1 承擔 `l15-wal-interference`；`s22_gate:blackbox_rust` +3、`s22_gate:cargo_workspace` +3（add-only） |
| 237b1c2 | test（紅燈）：s22_gate_check 跨集合重疊 nodeid |
| cd80448 | fix(gate)：「恰一列 passed」改逐份報告（集合×平台）計算 |
| 914d34b | docs：p6-fix-responses／p6-wiring-e2e（M12、M13） |
| feb984e | merge 並行 session 的 a8c57f0、08323e8（rpc_console.rs 取 a8c57f0；fix-responses 兩段並存） |
| c6ddda1 | test：`attach_request_timeout_closes_pty` 於 actor 搶先回覆時 detach 後重試（全 binary 約 1/10 偶發，修後連跑 30 次 0 失敗） |
| 71b1183 | docs：gate 實跑與全套 pytest 基準 |

每組檢查（非 root）：受影響 crate `cargo test --features test-fakes`（sw-core、sw-daemon 全綠）、`cargo check --workspace --target x86_64-pc-windows-gnu`（綠）、觸及的 Python 測試、`python3 -m tools.contracts check`（ok）、`scripts/rust/check_cfg_boundary.py`（rc 0）、`regen_lock.py --check`（OK）。`python3 -m policy_check --repo .` 有 **1 項 FAIL：R-21**（`docs/rust-refactor/reviews/p5-evidence/event-latency-od45-rust-20261029.json:2` 的 `"daemon": "/home/ubuntu/target/release/serialwrapd"` 被判 structural），該檔隨 P5 3084388 合入、屬 P5，P6 不得修改（SPEC-GAP #3）；P6 自有檔案無 policy 失敗。

## 2. High／Medium 處理

| 來源 | # | 處理 |
|---|---|---|
| `d6e3b3c_e9b5c61-sol.md` | P6-e9b5c61-1 High | 已修 7370b8f（紅燈 0123679；前一 session）；`73897df_24b8012-sol.md` 已核對「已修」 |
| 同上 | P6-e9b5c61-2 High | 同上 |
| 同上 | P6-e9b5c61-3 Medium | OWNER-BATCH #1（未變） |
| 同上 | P6-e9b5c61-4 Medium | OWNER-BATCH #2（未變） |
| `e9b5c61_73897df-sol.md` | A 段 -1／-2「未修」 | 審查範圍止於 73897df，修正在其後；B 段 NO FINDINGS |
| `73897df_24b8012-sol.md`（本 session 期間到） | A 段 -3／-4 未修（累計 2 輪） | 距 3 輪升級門檻一輪，請擁有者優先裁決 OWNER-BATCH #1／#2；B 段 NO FINDINGS |

上一輪全套多出的 4 項：指紋 2 案、OD-24 1 案仍紅（OWNER-BATCH #1／#2）；holder observer 案在非 root 下通過（本 session 全套亦未出現）。

本 session 自行發現並修正：

1. **console_attach 逾時後幽靈 human lease**（52d49d2，紅燈 9979795）：handler 逾時後 core 才處理遲到的 `ConsoleAttach`、開出 `human:<cid>` lease；executor 找不到 PTY 時原送 data lane `ConsoleDisconnected`（斷線語意保留 lease，與 Python 拔線一致），結果 console 被移除但 lease 留下，之後 attach 的 console 不再取得 `interactive_session_id` 與 raw ownership（實測回應缺兩鍵）。改送 control lane `SessionRequest::ConsoleDetach`（ERR-7.5-b 一併關閉自己的 lease），lane 滿才退回原路徑。Python attach 為同步、無逾時出口，Python 行為不受影響；屬 Rust-only 錯誤路徑的修正。
2. **cfg 邊界違規**（4e3cd8a／a8c57f0）。
3. **gate 跨集合重疊**（cd80448）與 **`l15-wal-interference` 依 OD-40 改證據**（76c78b2），見 §4、§9。

## 3. OD／裁決

| 項目 | 處理 |
|---|---|
| OD-14～OD-44 | 照既有處理 |
| OD-40 | 套用到 Slice 2.2 對照表的繼承案例 `l15-wal-interference`（`l15_ab_excluded` 所列 Rust 側測試於 2.2 artifact 重跑；Python 側 pytest_full 由 P5 承擔） |
| OD-45～OD-47 | P5 承接，P6 無工作；合併無衝突 |
| OD-48～OD-50（`rulings/s1.md`，本 session 期間登錄） | Stage 1 安裝／升級，不涉 P6 |
| `rulings/p6.md` | 無條目 |
| 審查收斂規則 §5 | `p6-wiring-e2e.md` 補 M12（adopt 失敗撤回）、M13（handler 移除暫存 PTY），兩項 no-op 變異皆 RED |

## 4. Slice 2.2 出口 gate（`s22_gate_check.py`，非 dry-run）

HEAD c6ddda1，報告 `target/s22-gate/r4b-261029-035247/s22-gate.json`（未提交）。**result = FAIL**。

| 集合 | 判定 | 數字／原因 |
|---|---|---|
| `s22_gate:cargo_workspace`/linux | PASS | 2130 passed、38 skipped（ignored） |
| `s22_gate:console_conformance_pty`/linux | PASS | 31 passed、3 skipped（cap_windows_com）；須 `cargo_l1_results.py --caps posix_pty` |
| `s22_gate:py_parity`/linux | PASS | 12 passed |
| `s22_gate:parity_py`/linux | FAIL | 32 passed、2 failed（指紋 [2.1]／[2.2]，OWNER-BATCH #1） |
| `s22_gate:blackbox_rust`/linux | FAIL | 115 passed、2 failed、8 skipped（slice21＋slice22）；失敗皆 slice21：`test_stale_context_not_replayed`、`test_two_daemons_same_tty`（見 §7 OWNER-BATCH #4、既有碼待辦） |
| `s22_gate:win_test`/windows | INSUFFICIENT | 集合未登記（LOCAL-WINDOWS-TODO） |
| prereqs（`check_p6_prereqs --strict`） | PASS | |

逐案 93：**PASS 31／BLOCKED-OWNER 62／FAIL 0／INSUFFICIENT 0**。L1.5／func-test 21 個 runner 參照以 c6ddda1 release artifact 執行**全部 PASS**（非 root）。parity_rows 23：PASS 19／OWNER-DECISION-PENDING 3（#107、#108、#185）／FAIL 1（#183 指紋）。

## 5. 全套 pytest（非 root、同環境、無 pyserial）

| | failed | passed | skipped |
|---|---|---|---|
| 起點 24b8012（獨立 clone） | 13 | 5569 | 223 |
| 終點 cd80448 | 12 | 5578 | 221 |

終點失敗集合為起點的**真子集**（`test_rust_cfg_boundary.py::test_repo_clean` 轉綠），無超出基準者，未需重跑取證。12 項：指紋 2、`test_l1_judge` 2（OD-24、capability-gated，後者為 P5 `s21_core`）、pyserial 未安裝 5（環境）、`test_doctor::test_pyserial_ok_when_importable`（環境）、`test_rust_bench_tools::test_cargo_workspace_tests_windows_native`、`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence`、slice21 `test_two_daemons_same_tty`——皆起點即紅。c6ddda1／71b1183 只改一支 Rust 測試與文件，未再跑全套（rpc_console 全 binary 連跑 30 次 0 失敗）。

## 6. 不成立主張

無。

## 7. SPEC-GAPS

1. **`attach_interactive_owner_false_during_replay_hold`（07 Task 10）前置狀態不可達**：`console_attach` 只在無 lease 時開 lease；產生回放的 human console 自己的 attach 必已開 lease（PTY），soft preempt／lazy expiry 的 PostClose 也會還原它；TCP 後端 attach 不開 lease；哨兵 primary 輸入被丟棄。實測 ATTACHED 救援 lazy expiry 變體：c2 attach 時 c1 lease 已還原，回應無 lease 欄位。Python 同理。請計畫擁有者改寫或撤列。
2. **`attach_request_timeout_closes_pty` 的「actor 卡住」**須在 P5 的 `actor.rs` 加 fault hook；本 session 改以 0 逾時（含重試）走同一個 handler 逾時出口，hub 佇列滿的 AdoptPty 失敗仍需 hook（R3-4／R5-L6）。
3. **R-21（P5 檔案）**：`p5-evidence/event-latency-od45-rust-20261029.json` 含家目錄絕對路徑，`policy_check` FAIL；需 P5 改為相對路徑或標記。
4. 並行 session 列的 #107 案例設計、計畫命令（`runner.py -c`、`--include-ignored` 不轉交、需 `--caps posix_pty`）照舊成立。

## 8. 既有碼待辦（不修）

- `test_two_daemons_same_tty`（P5 slice21 黑箱）：本環境下 Python daemon 與 P5 tip（3084388）Rust artifact **同樣失敗**（第二個 daemon 成功開啟同一 pts 並 READY）；PAL 層 `cap_posix_pty__second_process_open_is_held` 非 root 通過。屬 P5／環境，與 P6 無關。
- `test_l1_judge.py::test_l1_lock_frozen_set_excludes_capability_gated`：P5 `s21_core` 含 `cap_windows_*` 列。

## 9. 與並行 session 回報（`round4-report-20261029.md`）的差異

| 項目 | 並行 session | 本 session | 說明 |
|---|---|---|---|
| gate 跨集合重複（#40、#109） | 列 OWNER-BATCH #3，未修 | **已修 cd80448**（紅燈 237b1c2）：逐份報告（集合×平台）計「恰一列」，單一報告內重複仍 INSUFFICIENT、任一 failed 仍 FAIL | 本 session 判為 P6 自有腳本的誤判修正（集合層級 `l1_judge` 判定不變），但並行 session 視為放寬判準。**請編排者裁決**；若裁決為須擁有者核准，revert cd80448 即回到其選項 (b)／(c) |
| `l15-wal-interference` | FAIL | **依 OD-40 改證據（76c78b2）後 PASS** | OD-40 明文「由對應工作包逐名更新必要案例歸屬與凍結鎖」 |
| 8 個 Slice 2.1 L1.5 情境 | 非 root 下 FAIL（holder 掃描 fail-closed，SPEC-GAP #4） | 非 root（CapEff=0）下**全部 PASS** | 環境差異（並行 session 另以 pyserial 3.5 跑 bridge 情境）；P5 holder 掃描議題仍建議 P5 釐清 |
| slice21 `test_stale_context_not_replayed` | 列為 P5 案例 | 釐清為 **DF-20 範圍問題**（見 OWNER-BATCH #4） | `ab_rows.RUST_S21_DEFERRED` 明載 Rust 實作屬 P6 |
| 全套基準 | 起點 e9b5c61：25／5528／226 | 起點 24b8012：13／5569／223 | 兩者環境不同（pytest 9.2.0 vs 9.0.3 等） |

## 10. OWNER-BATCH

1. **凍結指紋 `l15-release-inflight`**（P6-e9b5c61-3、parity #183）：主線擁有者 `dbbc1f7` 改 `windows_gap_reason`。選項 (a) 逐名核准，於 `slice_2.1`／`slice_2.2` 修訂鏈各加一筆指紋更新（不改凍結本體）；(b) 本分支還原舊文字（會與主線／P5 再衝突）。**建議 (a)**。
2. **OD-24 與 `s22_gate:console_conformance_pty` 的 7 個 `#[ignore]` 列**（P6-e9b5c61-4）：選項 (a) 裁定以 `--ignored cap_*` 為 selector、gate 以 `--caps` 實跑的能力集合豁免 OD-24 (2)，並把 `--caps posix_pty` 記入 selector 以利重現；(b) `--allow-removal` 逐名移除、改由 gate 外能力報告承擔。**建議 (a)**（本輪 7/7 passed）。
3. **gate 跨集合重複的處理方式**（§9 第一列）：追認 cd80448，或 revert 後改採其他選項。**建議追認**。
4. **DF-20 範圍**：P5 黑箱 `test_stale_context_not_replayed`（06 計畫）要求 release→attach、`session recover`、U-Boot banner 三種情境後舊 `interactive_id` 皆 `INTERACTIVE_NOT_FOUND`、零 TX；Rust 現況 release→attach 已符合（`od37_*` L1 鎖住），**recover 後舊 id 照送**（實測 `ok:true, bytes:10`）、reboot 步驟因前一步失敗未驗。DF-20 的 `approval_conditions` 只涵蓋「release→重新 attach」，Python 三種情境皆照送。選項 (a) 擴大 DF-20（含 recover、reboot banner），P6 實作並先寫紅燈；(b) 維持 DF-20 原範圍，由 P5 把該黑箱縮為 release 步驟、另兩步標 1:1（Python 行為）。在裁決前 P6 不改 recover／reboot 的 lease 行為（否則與 Python 不 1:1 且無核准 DF）。
5. **#107／#108／#185**：同並行 session OWNER-BATCH #4／#5。
6. **`attach_interactive_owner_false_during_replay_hold`**（SPEC-GAP #1）：改寫或撤列。

## 11. LOCAL-WINDOWS-TODO

- Task 12 全部（WSAPoll mux、Winsock TCP console、com0com Serial console、W-1～W-7、`windows_console_native.rs`、`win-blackbox.ps1 -Subset console`）、`s22_gate:win_test/windows` 集合凍結與報告、parity #108 的 Windows TCP attach 不開 lease。

## 12. BLOCKED-OWNER

- 07 BLOCKED-OWNER 總表 #1～#7；對照表 62 案（BO-S22-01～05、BO-S21-07／08／13／14／16）；`f4-interactive-line-cmd-defined` parity 判定；SC-28／29／31 `IMPLEMENTED` 須 Step 4 全通過（含 win-test）。

## 13. 本 slice 距出口還缺

1. OWNER-BATCH #1（指紋）→ `parity_py`、#183 轉綠。
2. OWNER-BATCH #2（OD-24）→ `test_l1_judge` 轉綠。
3. OWNER-BATCH #3 追認 cd80448（否則 #40、#109 回到 INSUFFICIENT）。
4. OWNER-BATCH #4（DF-20 範圍）＋ P5／環境的 `test_two_daemons_same_tty` → `blackbox_rust` 轉綠。
5. P5 修 R-21（evidence 檔絕對路徑）→ policy_check 零 FAIL。
6. #107 案例設計、#108／#185 承接、Task 10 replay_hold 案改寫。
7. Windows：Task 12 與 `s22_gate:win_test`。
8. 62 案 BLOCKED-OWNER 實機證據，之後 SC-28／29／31 才能標 IMPLEMENTED。
9. 出口整包審查（Codex sol＋Claude Opus 雙審）。
