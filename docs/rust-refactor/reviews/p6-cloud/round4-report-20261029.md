# P6（Slice 2.2）第 4 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p6`（本輪唯一寫入者），起點 `e9b5c61`，終點 `08323e8`。
- 環境：Rust 1.96.1（rust-toolchain 釘定，含 rustfmt、x86_64-pc-windows-gnu＋mingw）、Python 3.12.3＋PyYAML＋pytest 9.2.0＋jsonschema；**全部測試以一般使用者 `ubuntu`（非 root）執行**。全套 pytest 起點與終點同一環境（無 pyserial）；L1.5 bridge 情境另以 pyserial 3.5（`PYTHONPATH`，CI `tests.yml` 同樣安裝）執行。
- 合併：主線 `8ef93f4`（320329a）、P5 `cca5ba8`（73897df）；lock 只 add-only。回報撰寫時 P5 另有 `b8f25bc` 等 3 個新 commit，本輪未再併入。

## 1. Commit 清單（e9b5c61..08323e8，P6 自有）

| commit | 內容 |
|---|---|
| 320329a | merge 主線 8ef93f4：DF-20 依主線登錄為準；09 計畫取 P5 已解版本 |
| 73897df | merge P5 cca5ba8：哨兵／TCP listener 移入 P5 的 `OpenCtx` 開埠工作者；`regen_lock` 兩種延後核准並存；cases.yaml revision 2026-10-29.1；breaker 改用修訂鏈（slice_2.1 還原 P5 本體＋指紋旁置檔、slice_2.2 本體還原＋`slice_2.2.amendments.json` OD-34 兩筆） |
| 0123679 | test（紅燈）：P6-e9b5c61-1／-2 四支回歸 |
| 7370b8f | fix：self-test suspend 旗標隨各自 op；取消的停放命令結算在途指標 |
| d2a6e67 | test（紅燈）：ATTACHED 救援 worker 健康須取自 manager 注入的 bridge 快照（接線稽核 R3-8） |
| 30475b8 | fix：worker 健康改用現行 epoch IoWorker 快照 |
| fcbb564 | test：接線＋端到端驗收（daemon 級 4 支新測試、`p6-wiring-e2e.md`、M1～M11 no-op 變異皆紅） |
| 0fe0134 | test(acceptance)：對照表 `parity_rows` 23 列、新凍結集合 `s22_gate:parity_py`（34 nodeid） |
| 3036709 | docs：p6-fix-responses 第四輪紀錄 |
| 24b8012 | fix：`console_methods_no_longer_deferred` 改以 `CARGO_MANIFEST_DIR` 定位（gate 以 repo 根為 cwd 執行時 FAIL） |
| a8c57f0 | fix：同上改用 `include_str!`（24b8012 的執行期讀檔被 cfg 邊界檢查判為 test-data-path，全套 `test_rust_cfg_boundary::test_repo_clean` FAIL） |
| 08323e8 | docs：gate 實跑與全套 pytest 基準比對 |

每個 commit 群組：受影響 crate `cargo test --features test-fakes`（非 root）、`cargo check --workspace --target x86_64-pc-windows-gnu`、觸及的 Python 測試、`python3 -m tools.contracts check`（ok）、`python3 -m policy_check --repo .`（fail 0）、`scripts/rust/check_cfg_boundary.py`（rc 0）全綠後 fast-forward push。merge commit 320329a 漏了 CLAUDE.md 要求的 `Co-authored-by: Copilot` trailer（未 push 前未察覺，已 push 不改寫歷史）。

## 2. High／Medium 處理

### 最新 sol 報告 `p6-cloud/d6e3b3c_e9b5c61-sol.md`

| # | 等級 | 處理 |
|---|---|---|
| P6-e9b5c61-1 | High | **fixed 7370b8f**（紅燈 0123679）：移除共用 `self_test_suspended`，改存於各自 `PendingOp::SelfTest.suspended: Option<世代>`；Recover 等其他 side op 完成不再 resume；drain Phase 1 遞增世代使舊憑證失效。回歸：`session_core_console::overlapping_self_tests_each_resume_their_own_suspend`、`recover_side_op_does_not_resume_self_test_suspend`；daemon 級 `rpc_console::overlapping_self_tests_replay_human_input_end_to_end`（還原為修正前原始碼時 FAIL）。Python 依據 `session_manager.py:7599,7622,7693` |
| P6-e9b5c61-2 | High | **fixed 7370b8f**（紅燈 0123679）：`settle_parked_terminal` 以 `registry.complete` 清在途指標（不覆寫 canceled），`start_next` 跳過與 `abort_parked_command` 兩條退出路徑皆結算；取消命令仍零 TX（Python `revoke_tx_op`）。回歸：`canceled_parked_command_releases_pending_slot`（含後續命令容量恢復）、`…_then_drain_releases_pending_slot` |
| P6-e9b5c61-3 | Medium | **OWNER-BATCH #1**（見下）：`l15-release-inflight.windows_gap_reason` 的變更出自主線擁有者 commit `dbbc1f7`；本分支不自行還原 |
| P6-e9b5c61-4 | Medium | **OWNER-BATCH #2**：OD-24 (2) 與 `s22_gate:console_conformance_pty` 的 7 個 `#[ignore]` 列衝突 |

### 本輪結束前新報告 `p6-cloud/e9b5c61_73897df-sol.md`（審到 73897df，即本輪修正之前）

A 段 P6-e9b5c61-1／-2 標「未修」——於其後的 7370b8f 修正（報告範圍外）；-3／-4 同上 OWNER-BATCH。B 段 NO FINDINGS。

### 本輪期間再到的 `p6-cloud/73897df_24b8012-sol.md`

A 段：P6-e9b5c61-1／-2 **已修 7370b8f**（審查者確認）；-3／-4 未修（累計 2 輪，待 OWNER-BATCH #1／#2）。B 段 NO FINDINGS。距 3 輪升級門檻只差一輪，請擁有者優先裁決 #1／#2。

### 上一輪全套多出的 4 項確定性失敗

| 測試 | 處理 |
|---|---|
| `test_slice21_case_freeze.py::test_freeze_case_fingerprints_match_current_cases[2.1]`／`[2.2]` | 合併帶入的 `l15-delayed-reply` 差異已依 OD-34 修訂鏈排除；剩 `l15-release-inflight`（OWNER-BATCH #1），仍紅 |
| `test_l1_judge.py::test_l1_lock_cargo_sets_exclude_ignored_tests` | OWNER-BATCH #2，仍紅 |
| `test_bb_l15_holder_observer.py::test_observer_does_not_ignore_zombie_leader_with_live_thread` | 環境因素確認：非 root 重跑 `tests/test_bb_l15_holder_observer.py` 17/17 passed |

### 接線稽核發現（本輪自查）

- R3-8（上輪 deferred）`set_worker_health` 只有測試呼叫：**fixed 30475b8**（紅燈 d2a6e67）。
- `console_methods_no_longer_deferred` 依賴 cwd：gate 實跑發現，**fixed 24b8012**。

## 3. OD／裁決處理

| 項目 | 處理 |
|---|---|
| OD-14～OD-44 | 照既有處理；OD-34：`cowork-01-agent-bad-cmd` 與合併帶入的 `l15-delayed-reply` 皆以修訂鏈自 slice_2.2 移出（分母 93），2.3／2.4 保留 |
| OD-37／DF-20 | 主線已登錄 DF-20（scope both），P6 自配條目撤回、以主線為準；P6 凍結案例 `df20-release-reattach-stale-interactive-id` 保留 |
| OD-45～OD-47（`rulings/p5.md`） | 皆屬 P5（event_latency、WAL unlink、gaps #9），P6 無對應工作 |
| `rulings/p6.md` | 目前無條目 |
| 審查收斂規則 §5 | 已做（`docs/rust-refactor/reviews/p6-wiring-e2e.md`） |

## 4. Slice 2.2 出口 gate（`s22_gate_check.py`，非 dry-run）

報告：`target/s22-gate/261029-r4b/s22-gate.json`（未提交；HEAD 24b8012 的 release／test-fakes artifact）。**result = FAIL**。

| 集合 | 判定 | 說明 |
|---|---|---|
| `s22_gate:cargo_workspace`/linux | PASS | 2123 passed（修 24b8012 後重跑） |
| `s22_gate:console_conformance_pty`/linux | PASS | 須以 `cargo_l1_results.py --caps posix_pty`；計畫 Step 4 的 `-- --include-ignored` 寫法 collector 不轉交（SPEC-GAP #3） |
| `s22_gate:py_parity`/linux | PASS | 12 passed |
| `s22_gate:parity_py`/linux | FAIL | 32 passed、2 failed（`test_freeze_case_fingerprints_match_current_cases[2.1／2.2]`，OWNER-BATCH #1） |
| `s22_gate:blackbox_rust`/linux | FAIL | 113 passed、2 failed、8 skipped：`slice21/test_safety_core.py::test_stale_context_not_replayed`、`::test_two_daemons_same_tty`（Slice 2.1／P5 案例；起點 Python 基準同樣失敗） |
| `s22_gate:win_test`/windows | INSUFFICIENT | 集合未登記（LOCAL-WINDOWS-TODO） |
| map 覆蓋 | PASS | 93／93 |
| `check_p6_prereqs --strict` | PASS | |

逐案：93 案中 PASS 23、BLOCKED-OWNER 62、FAIL 8。8 個 FAIL 皆為 Slice 2.1 繼承的 L1.5 情境（`l15-exclusive-open`、`-half-prompt`、`-multi-source-contention-cli`、`-reject-zero-tx`、`-release-inflight`、`-slow-reader-backpressure`、`-stale-event-delay`、`-wal-interference`）：Rust daemon 在**非 root** 下 session 停在 offline（wait_ready 20 s 逾時）；同一案例以 root 執行 Rust artifact PASS、非 root 執行 Python 基線 PASS——與 P5 合併帶入的 fail-closed holder 掃描（`/proc/<pid>/fd` 權限不足 → `Permission`）有關，屬 P5 檔案（SPEC-GAP #4）。func-test 十案全 PASS；Slice 2.2 自有情境 `l15-multi-source-contention-console`、`-context-switch-deferred-input`、`-transport-disconnect`、`-reboot-min-guard` PASS。

parity_rows 23 列：PASS 17、FAIL 1（#183，同 OWNER-BATCH #1）、INSUFFICIENT 5——#107、#108、#185 為 OWNER-DECISION-PENDING；#40、#109 的測試在 `cargo_workspace` 與 `console_conformance_pty` 兩份報告各 passed 一次，被「恰一列」規則判為重複（SPEC-GAP #5）。

## 5. 全套 pytest（非 root；起點 e9b5c61 基準 vs 終點）

| | failed | passed | skipped |
|---|---|---|---|
| 起點 e9b5c61 | 25 | 5528 | 226 |
| 終點 24b8012 | 20＋1 error | 5561 | 223 |

起點失敗中 12 項終點通過（多為 slice21 黑箱與 bridge smoke，屬不穩定）。超出基準 8 項：
- `tests/test_rust_cfg_boundary.py::test_repo_clean`：24b8012 引入的真實回歸，**a8c57f0 修正**（33 passed）。
- 其餘 7 項（`test_mgmt_stubs::test_live_daemon_fixture_serves_attached_console_list`、slice21 `test_queue_full_backpressure`、`test_reject_paths_zero_tx[CMD_TOO_LONG]`、`test_cli_mgmt_l15::test_restart_no_stale_console_replay`、`test_functest_bridge_smoke::test_bridge_smoke_sm01`、`test_l15_smoke::test_reject_zero_tx_runs_on_python`／`test_release_inflight_runs_on_python`）：皆為 **Python daemon 側** `ATTACH_FAILED:DeviceHeldError`。起點基準執行時洩漏的 throwaway Python daemon（pid 19168，持有 /dev/pts/1、/dev/pts/2）在整段終點全套期間存活。清除後同範圍連跑兩次：第一次 7/7 passed、第二次只有 `test_queue_full_backpressure` 失敗（失敗集合不同）→ 判定為環境（洩漏 daemon）加既有不穩定，非本輪回歸。
- 終點未在 a8c57f0／08323e8 上重跑全套（兩者只改一支測試的讀檔方式與文件）。

## 6. 不成立主張

無。四條 sol finding 經實測皆成立（-1／-2 修正；-3／-4 成立但修正須擁有者裁決）。

## 7. SPEC-GAPS

1. **#107 L1.5 func-test（0x08／0x7F）案例設計**：line-buffer 只出現在非 owner console，而其整行提交在 owner console 持有 human lease 時回 `SESSION_INTERACTIVE_BUSY`；以 Python 基線實跑草稿案例（第二個 console 打 `echo abx\x08c`）只見本地回顯、命令被拒。計畫須指定前置（例如 owner detach 後由誰持 lease、或在 agent 前景命令期間的 line 模式）才能寫出 1:1 案例。
2. **計畫 07 Task 11 Step 4／Task 13 Step 4 的命令**：`runner.py -c` 是 `--category`（上輪已報）；`cargo_l1_results.py -- … -- --include-ignored cap_posix_pty__` 的 test-binary 參數不會轉交，必須加 `--caps posix_pty` 才會實跑 cap 測試。
3. **`s22_gate_check.py` 跨集合重複**：`cargo_workspace` 與 `console_conformance_pty` 都執行 `console_conformance` 測試執行檔的非 ignored 測試，同一 nodeid 在兩份報告各一列 passed，`judge_tests` 的「恰一列」規則因而判 INSUFFICIENT（#40、#109）。改判定屬放寬 gate 判準，未自行修改（列 OWNER-BATCH #3）。
4. **P5：非 root 下 Rust daemon 的 holder 掃描 fail-closed**（`crates/sw-pal/src/proc_scan/posix.rs`，P5 擁有）：L1.5 bridge 情境非 root 時 session 停在 offline（Python 基線非 root 照常 READY；root 下 Rust PASS），daemon 印「無法確認 /dev/pts/N 的外部 holder（status=Permission）」。需 P5 對照 Python `serial_port.probe_holders` 在權限不足時的實際行為。
5. **P5 擁有的 `s21_core` 集合**同樣含 OD-24 禁止的 `#[ignore]` 列與 `cap_windows_*` 列（`test_l1_lock_frozen_set_excludes_capability_gated`），P6 不處理。

## 8. 既有碼待辦（不修）

- `daemon_process::sigterm_graceful_exit_0_and_flushes_queue`（P5）：全套 pytest 併跑負載下失敗一次（排隊命令之一已出列，flush 少一筆），單獨連跑兩次 32/32 綠；負載敏感。
- `test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`：pytest 9.2.0 對長字串參數改產 `msg2` id；以 CI 釘定的 9.0.3 通過（環境因素）。
- slice21 黑箱 `test_safety_core.py` 數案在起點 Python 基準即失敗（見 §5），屬 P5。

## 9. OWNER-BATCH

1. **凍結指紋 `l15-release-inflight`**（sol P6-e9b5c61-3；`test_freeze_case_fingerprints_match_current_cases[2.1／2.2]`、parity #183）。主線 `dbbc1f7`（擁有者）把 `windows_gap_reason` 改為「Windows L1.5 案例尚未重跑；virtual com0com 啟用 peer_gate 時可提供 peer_state…」。選項：(a) 逐名核准此欄位變更，於 `slice_2.1`／`slice_2.2` 修訂鏈各加一筆「指紋更新」修訂（不改本體）；(b) 本分支還原舊文字、說明移至非凍結文件（會與主線／P5 分歧並於下次合併再衝突）。**建議 (a)**。
2. **OD-24 與 `s22_gate:console_conformance_pty`**（sol P6-e9b5c61-4）。該集合 selector 以 `--ignored cap_posix_pty__`（實跑時 `--caps posix_pty`）真正執行 7 個 `#[ignore]` 測試，與 OD-24 立論不同。選項：(a) 裁定 selector 為 ignored 能力集合者豁免 OD-24 (2)（修改 `test_l1_lock_cargo_sets_exclude_ignored_tests` 的範圍）；(b) 依 OD-24 以 `--allow-removal` 逐名移除 7 列，改由 gate 外的能力報告承擔。**建議 (a)**（本輪實跑 7/7 passed，有逐案證據）。
3. **gate 跨集合重複判定**（SPEC-GAP #3）。選項：(a) 同一 nodeid 在多個集合報告中皆 passed、無 failed 時視為 PASS（每份報告內部重複仍 INSUFFICIENT）；(b) 維持現狀，由 #40／#109 改指其他測試；(c) 讓 `cargo_workspace` selector 排除 `console_conformance`（selector 變更需重凍結）。**建議 (a)**。
4. **#108／#185 承接**：#108 的 file 傳輸部分屬 P8、Windows TCP 部分屬 Windows 原生；#185 為流程項（T0 單獨紅燈）。請指定承接 Phase 或證據形式。
5. **#107 L1.5 案例設計**（SPEC-GAP #1）。

## 10. LOCAL-WINDOWS-TODO

- Task 12 全部：W-1～W-7（Winsock TCP console、WSAPoll mux、com0com Serial console、testutil nodelay／reuseaddr 等）、`windows_console_native.rs`、`win-blackbox.ps1 -Subset console`、`s22_gate:win_test/windows` 集合凍結與報告。
- parity #108 的 Windows TCP `attach_console` 不開 lease。

## 11. BLOCKED-OWNER

- 07 BLOCKED-OWNER 總表 #1～#7（Windows Telnet A/B、com0com Serial console、互動桌面 frontend、L2 F5／F6／F8 bench、L3 realhw、跨 reboot 實板、echo SLO 實體 loopback）。
- 對照表 62 案 BLOCKED-OWNER（BO-S22-01～05、BO-S21-07／08／13／14／16）。
- `f4-interactive-line-cmd-defined` parity 判定（沿上輪）。
- SC-28／SC-29／SC-31 `IMPLEMENTED`：依計畫須 Step 4 全通過（含 win-test）。

## 12. 本 slice 距出口還缺

1. OWNER-BATCH #1 裁決（指紋）→ `parity_py` 與 #183 轉綠。
2. OWNER-BATCH #2 裁決（OD-24）→ 全套 `test_l1_judge` 零失敗。
3. OWNER-BATCH #3 裁決或改指測試 → #40、#109。
4. P5 修正非 root holder 掃描（SPEC-GAP #4）與 slice21 黑箱兩案 → `blackbox_rust` 與 8 個 L1.5 案例。
5. #107 案例設計、#108／#185 承接裁決。
6. Windows：Task 12 與 `s22_gate:win_test`（LOCAL-WINDOWS-TODO）。
7. 62 案 BLOCKED-OWNER 實機證據；之後才能把 SC-28／29／31 標 IMPLEMENTED。
8. 出口整包審查（Codex sol＋Claude Opus 雙審）。
