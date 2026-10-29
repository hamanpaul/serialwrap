# P6（Slice 2.2）第 5 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p6`（本輪唯一寫入者），起點 `71b1183`，終點 `2b6f8ee`。
- 環境：Rust 1.96.1（rust-toolchain 釘定，含 rustfmt、x86_64-pc-windows-gnu＋mingw-w64）、Python 3.12.3＋PyYAML 6.0.3＋pytest 9.0.3＋jsonschema 4.10.3（CI 釘定版）、無 pyserial；**全部測試以一般使用者 `ubuntu`（非 root、CapEff=0）前景執行**。全套 pytest 起點與終點皆在獨立 clone、同一環境。
- 合併：主線 `origin/feature/rust-refactor` 無新 commit；P5 `464adec`（6419612，lock 未變）。
- 處理的 sol 報告：任務開始時最新為 `08323e8_c6ddda1-sol.md`；本輪期間另到 `c6ddda1_71b1183`（B 段 NO FINDINGS）、`71b1183_bf61e3e`（P6-bf61e3e-1）、`bf61e3e_b64dad9`（P6-b64dad9-1 High）、`b64dad9_45b82e4`（B 段 NO FINDINGS），皆已處理（§2）。結束前再 fetch `docs/parity-amendments`（3e75179）：新裁決 OD-55～OD-61 中只有 OD-55 涉及 P6 finding（P6-e9b5c61-4 的 `s21_core` 部分交 P5），其餘屬 P5／P7／S1。

## 1. Commit 清單（71b1183..2b6f8ee）

| commit | 內容 |
|---|---|
| 6419612 | merge P5 464adec（event_latency 證據去除本機路徑；R-21 解除） |
| 69584d0 | test（紅燈）：OD-52 指紋更新修訂、有效指紋取自修訂鏈 |
| 0c20ee8 | fix：`freeze_slice_cases.py --amend --fingerprint-update`；slice_2.1／slice_2.2 修訂鏈各加 #2（OD-52、dbbc1f7、`windows_gap_reason`） |
| 80556ea | test（紅燈）：同報告內重複不得被他報告 passed 遮蔽（OD-54） |
| bf61e3e | fix：OD-53 能力集合豁免、OD-54 報告內重複判 INSUFFICIENT |
| 7d60f64 | test（紅燈）：DF-20 recover／banner 後舊 agent id 須 INTERACTIVE_NOT_FOUND 且零 TX |
| ad823bc | fix：DF-20 範圍實作；DF-20 文字涵蓋三情境；conditional 案例；L1 鎖 +1 |
| e0aab47 | test：修正 bf61e3e 已 push 的 OD-52 測試前置（sol P6-bf61e3e-1） |
| 7549b31 | test：#107 `co-02-line-backspace`；新增 `slice22-chg-register.md` |
| 5ade76c | test：OD-53 測試區域變數改名（資源 marker 誤判，bf61e3e 引入） |
| db6689b | test（紅燈）：#185 PROCESS-EVIDENCE 判定 |
| 565369c | fix：#108 BLOCKED-OWNER、#185 PROCESS-EVIDENCE；新增 `slice22-exit-report.md` |
| b64dad9 | test：`attach_interactive_owner_false_during_replay_hold` 可達等價改寫；L1 鎖 +1 |
| 45b82e4 | docs：p6-fix-responses 第五輪、出口報告 gate 紀錄 |
| f014902 | fix：`slice21_scope.yaml` 2.2 func-test 列補 `co-02-line-backspace`（終點全套抓到的、我自己引入的回歸） |
| 2184235 | test（紅燈）：#185 PROCESS-EVIDENCE 改以結構化紅／綠燈區塊判定（sol P6-b64dad9-1） |
| 2b6f8ee | fix：`_process_evidence_problems` 解析 `json process-evidence` 區塊；出口報告 §#185 補區塊 |

每組檢查（非 root）：受影響 crate `cargo test --features test-fakes`（sw-core 32 個 test binary、sw-daemon 11 個全綠；rpc_console 連跑 5 次 0 失敗）、`cargo check --workspace --target x86_64-pc-windows-gnu`（綠）、觸及的 Python 測試、`python3 -m tools.contracts check`（ok）、`python3 -m policy_check --repo .`（**25 pass／0 fail**／2 warn）、`scripts/rust/check_cfg_boundary.py`（rc 0）、`regen_lock.py --check`（OK）。L1 鎖只增 2 列、cases 鎖只增 2 案（revision 2026-10-29.2／.3），皆經 `regen_lock.py`。
注意：bf61e3e push 時帶了兩個我自己引入、該組驗證漏掉的紅燈（OD-52 測試前置、資源 marker 誤判），分別於 e0aab47、5ade76c 修正；終點全套已確認不再出現。

## 2. High／Medium 處理

| 來源 | # | 處理 |
|---|---|---|
| `08323e8_c6ddda1-sol` A | P6-e9b5c61-3 Medium | **已修 0c20ee8**（OD-52）；`71b1183_bf61e3e-sol` 已核對「已修」 |
| 同上 | P6-e9b5c61-4 Medium（累計 6 輪） | **P6 部分已修 bf61e3e**（OD-53：P6 七個能力節點豁免，sol 已確認）；稽核整體仍紅，剩餘 40 列全在 P5 `s21_core`（`--skip cap_` 集合，不屬 OD-53 能力集合，另含 3 個非 cap 的 ignored 列）——列 OWNER-BATCH #1／SPEC-GAP #2，P6 不改 P5 鎖列 |
| `08323e8_c6ddda1-sol` B | P6-c6ddda1-1 Medium | **已修 bf61e3e**（紅燈 80556ea；OD-54），sol 已確認 |
| 同上 | P6-c6ddda1-2 Medium | **已修**，合入 P5 464adec（6419612），policy_check fail 0 |
| `71b1183_bf61e3e-sol` B | P6-bf61e3e-1 Medium | **已修 e0aab47**：暫存目錄只複製修訂鏈中第一筆 `fingerprint_update` 之前的前綴（保留 OD-34 修訂，鏈雜湊不受影響），第 131 行前置與後續成功／拒絕／防竄改斷言全部保留並實際執行（3 passed） |
| `bf61e3e_b64dad9-sol` B | P6-b64dad9-1 High | **已修 2b6f8ee**（紅燈 2184235）：#185 只依 `## #185` 段落內的 `json process-evidence` 區塊判定——red 須為 ae33e28、非零退出、failed>0、列出失敗測試；green 須為另一 commit、同一命令、退出碼 0、failed==0、passed>0。sol 的兩個負對照（刪除綠燈紀錄、綠燈改 `0 passed、13 failed`）及其他 9 個變體皆判 INSUFFICIENT。兩端以一般使用者重跑取得退出碼（ae33e28 rc=1；2184235 rc=0） |
| `c6ddda1_71b1183-sol`、`b64dad9_45b82e4-sol` | B 段 NO FINDINGS | — |

## 3. OD／裁決

| 項目 | 處理 |
|---|---|
| OD-52 | 見上。2.1 修訂鏈屬 P5 檔案：依 OD-52 逐名授權**只追加修訂列**，既有修訂 #1 位元組不變（已驗）。`test_freeze_case_fingerprints_match_current_cases[2.1／2.2]` 轉綠、parity #183 PASS |
| OD-53 | `test_l1_lock_cargo_sets_exclude_ignored_tests` 只豁免 selector 在 test-binary 參數以 `--ignored cap_<cap>__` 明確只跑 ignored 能力測試的集合，且只豁免該 cap 前綴；`--skip cap_`、`--include-ignored`、異 cap、能力集合內非 cap 列仍違規（新合成鎖測試）。「`--caps posix_pty` 記入 selector」未能照做：L1 鎖 schema `additionalProperties:false`、`regen_lock` 結構檢查與 CI `--check --base` 都拒絕 selector 變更，硬改即違反凍結鎖 add-only；改以 `_selector_explicit_caps` 由既有 selector 推導重現命令（`cargo_l1_results.py --caps posix_pty -- <selector argv>`），並列 SPEC-GAP #3 |
| OD-54 | cd80448 追認；P6-c6ddda1-1 已修（任一份報告內重複 → INSUFFICIENT，failed 仍優先）；補整合與單元判定器測試 |
| DF-20 範圍（round4-cont #4） | Rust：受理的 `session recover`（READY／ATTACHED 或 `--force`）與觸發 boot quiet 的 U-Boot banner，使既有 **agent** lease 失效（一般 close，舊 id → `INTERACTIVE_NOT_FOUND`、零 TX）；human console lease 跟著仍掛著的 console，不受影響。DF-20 `compatibility_impact`／`approval_conditions` 涵蓋三情境；新增 conditional `df20-recover-reboot-stale-interactive-id`。`test_stale_context_not_replayed` 於 Rust artifact **PASS** |
| #107（OWNER-BATCH #5） | 以 Python 基線實跑找到可達前置：owner console 先 detach → 第二個 console 仍走 line-buffer → `echo abx\b c`、`echo dex\x7f f` 整行提交，UART 只收到 `echo abc`／`echo def`。Python 基線與 Rust 皆 PASS；CHG-S22-107-CASE-01；對照表 #107 PASS |
| #108（OWNER-BATCH #4） | 對照表 → BLOCKED-OWNER，`slice22-owner-runbook.md` 新增 #108 節（Windows TCP `attach_console` 不開 lease，LOCAL-WINDOWS-TODO）；file 傳輸部分改列 P8（SPEC-GAP #4：P8 計畫須加必要案例） |
| #185（OWNER-BATCH #4） | 於 `ae33e28` 獨立 worktree 實跑：`test_resource_markers.py` 3 案、`test_slice21_skeleton_guards.py` 1 案 → 4 failed／9 passed；終態 13 passed。寫入 `slice22-exit-report.md` §#185；對照表 → `PROCESS-EVIDENCE`（gate 只對 #185 逐名接受，段落須含 commit 與紅／綠燈紀錄） |
| replay_hold 案（round4-cont #6） | **改寫為可達等價案例，不需撤列**：產生回放的 console 的 human lease 在前景命令期間以 `session.interactive_close` 關閉（console 仍掛著、deferred 保留，D-22(5)），回放停在 `PauseBeforeSubmit` 時 hold 存在、前景已清、無 lease；第二個 console attach 回 `interactive_owner:false` 並開 lease；結算後 `foreground_busy==false`、按鍵恰寫出一次。新增即綠（增補 #26 已實作），把 attach 的 owner 判定改成只看 `foreground` 的變異會紅。CHG-S22-REPLAYHOLD-01 |
| OD-14～OD-51 | 照既有處理；OD-45～OD-51 不涉 P6 |

## 4. Slice 2.2 出口 gate（`s22_gate_check.py`，非 dry-run，非 root）

HEAD b64dad9 的 release artifact（cargo 集合帶 `--features test-fakes`），報告 `target/s22-gate/r5-261029-051754/s22-gate.json`（未提交）。**result = FAIL**。

| 集合 | 判定 | 數字／原因 |
|---|---|---|
| `s22_gate:cargo_workspace`/linux | PASS | 2132 passed、38 skipped（ignored）。第一次實跑 `sw-pal::lock::lock_conformance_suite`（非 P6 檔案）失敗 1 次（報告只有 call failed、無輸出）；單獨連跑 10 次 10/10 綠，同範圍重跑 0 失敗——失敗集合不同，列既有碼待辦 |
| `s22_gate:console_conformance_pty`/linux | PASS | 31 passed、3 skipped（cap_windows_com）；`--caps posix_pty` |
| `s22_gate:py_parity`/linux | PASS | 12 passed |
| `s22_gate:parity_py`/linux | **PASS** | 34 passed（上輪 FAIL 的指紋 2 案轉綠） |
| `s22_gate:blackbox_rust`/linux | FAIL | 116 passed、1 failed、8 skipped；唯一失敗 `slice21/test_safety_core.py::test_two_daemons_same_tty`（P5 harness，SPEC-GAP #1）；`test_stale_context_not_replayed` 轉綠 |
| `s22_gate:win_test`/windows | INSUFFICIENT | 集合未登記（LOCAL-WINDOWS-TODO） |
| map 覆蓋／prereqs | PASS／PASS | 93／93；`check_p6_prereqs --strict` |

逐案 93：**PASS 31／BLOCKED-OWNER 62／FAIL 0／INSUFFICIENT 0**。L1.5／func-test 22 個參照（含新 co-02）在 Rust release artifact 上**全部 PASS**（bridge selftest PASS；manifest 確認 daemon implementation=rust）。parity_rows 23：**PASS 22／BLOCKED-OWNER 1（#108）**（上輪 PASS 19／PENDING 3／FAIL 1）。

## 5. 全套 pytest（非 root、同環境、無 pyserial、獨立 clone）

| | failed | passed | skipped |
|---|---|---|---|
| 起點 6419612（獨立 clone，71b1183＋P5 merge） | 12 | 5578 | 221 |
| 終點 b64dad9（獨立 clone） | 11 | 5585 | 221 |

終點相對起點**轉綠 2 項**（`test_freeze_case_fingerprints_match_current_cases[2.1]`／`[2.2]`），**超出基準 1 項**：`tests/test_slice21_scope.py::test_scope_matches_cases_yaml`——確定性的真實回歸（7549b31 新增 2.2 func-test 案例未同步列入 `slice21_scope.yaml`），已於 f014902 修正（`test_slice21_scope.py`、`test_slice21_case_freeze.py`、`test_acceptance_cases.py` 72 passed／1 skipped，`deferred_methods`／`rpc_console` 綠）；修正後失敗集合為起點的真子集。
其餘 10 項皆起點即紅：`test_l1_judge` 2（P5 `s21_core`，OD-55 交 P5）、pyserial 未安裝 5＋`test_doctor::test_pyserial_ok_when_importable`（環境）、`test_rust_bench_tools::test_cargo_workspace_tests_windows_native`（需 Windows）、`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence`（雲端無本機 Stage 1 證據，依 P5 裁決為 EVIDENCE_MISSING）、slice21 `test_two_daemons_same_tty`（P5 harness，SPEC-GAP #1）。
b64dad9 之後的 commit（45b82e4 文件、f014902、2184235、2b6f8ee）未重跑全套，只跑受影響測試（`test_s22_gate_check.py` 17 passed 等）。

## 6. 不成立主張

無。

## 7. SPEC-GAPS

1. **`test_two_daemons_same_tty`（P5：`tests/contract_blackbox/slice21/harness.py`、`test_safety_core.py`）**：不是 TIOCEXCL 或環境問題。`start_daemon` 對第二個 daemon 也建立新的 FakeTarget，並用 `target.start(by_id_dir, BY_ID_NAME)` 把 symlink 寫進被 `extra_env` 覆寫成第一個 daemon 的 by-id 目錄，蓋掉第一個 daemon 的 `fake-uart0`。重現（非 root）：第一個 daemon 持 `/dev/pts/0`；第二個 daemon 啟動後 READY 在自己的 `/dev/pts/3`，`/proc/*/fd` 已看不到第一個 daemon 持有 `/dev/pts/0`。另驗證非 root 下 TIOCEXCL 有效（同 tty 第二次 open 回 EBUSY）。Python 與 Rust 都同樣失敗。修法建議：`start_daemon` 加參數共用既有 target（不建 target、不寫 symlink）。
2. **P5 `s21_core` 的 `#[ignore]` 列（40 列：linux 27、windows 13；已由 OD-55 交 P5）**：屬 `--skip cap_` 集合，不符合 OD-53 的豁免條件；含 3 個非 cap ignored 列（`mgmt_layout::real_mkbundle_dev_output_verifies`、`windows_switch_running_exe_rename`、`serial_posix::plain_holder_child`）。需 P5 以 `--allow-removal` 逐名移除，或把 cap 列移到明確能力集合（如 `s21_gate:linux_cap_posix_pty`，selector `--ignored cap_posix_pty__`）。`test_l1_lock_frozen_set_excludes_capability_gated` 同屬 P5 `s21_core.windows`。
3. **OD-53「`--caps posix_pty` 記入 selector」**：需 P2 擁有的 `l1_required.schema.json`（selector 加選填 `caps`）、`regen_lock._spec_shape_problems` 與 `--check --base` 的 selector 比對規則一起改，否則 CI 會拒絕。本輪未改（改了等於放寬凍結鎖 base 比對），以 selector 推導代替。
4. **#108 file 傳輸部分 → P8**：`test_existing_human_owner_window_is_deferred_before_suspend` 對應的 Rust 必要案例需寫進 09 計畫（P8 擁有），P6 不改。
5. 沿用：`attach_request_timeout_closes_pty` 的「actor 卡住」fault hook 需改 P5 `actor.rs`；計畫 07 Task 13 Step 4 的 `--include-ignored` 寫法 collector 不轉交，須用 `--caps posix_pty`；L1.5 結果檔（`s22-l15-results.v1`）沒有官方 producer，本輪以臨時腳本逐參照執行 `runner.py`（bridge 參照加 `--selftest-report`）產生。

## 8. 既有碼待辦（不修）

- `sw-pal::lock::lock_conformance_suite`：gate 第一次實跑失敗 1 次（無輸出），單獨 10/10 與同範圍重跑皆綠；未判為 flaky，下次重現時須保存輸出。
- `changelog.d/p5-r4-od34-44.md`、`p5-r4-od39-runner.md`（P5）的 `type` 不在 `policy_check.changelog.TYPE_TO_SECTION`，release collate 時會被拒。
- `cargo fmt --all -- --check` 在整個 workspace 有大量既有差異（非本輪檔案）。

## 9. OWNER-BATCH

1. **P6-e9b5c61-4（sol 累計 8 輪「未修」）**：P6 部分已修（bf61e3e）；剩下的 P5 `s21_core` 40 列已由新裁決 **OD-55**（rulings/p5.md）交 P5 移到 `s21_gate:cap_<cap>` 能力集合並 `--allow-removal`。請編排者在 rulings/p6.md 登記「P6 部分已修、剩餘依 OD-55 由 P5 交付」，讓審查者把此項標為「依裁決不處理」，不再計入 P6 未修輪數。3 個非 cap 的 ignored 列（SPEC-GAP #2）是否也在 OD-55 範圍內，請一併確認。
2. **OD-53 selector 記錄 caps**（SPEC-GAP #3）：要不要授權 P2 鎖 schema／regen_lock 新增選填 `selector.caps`（只增欄位、base 比對允許新增 caps）？不授權的話，維持由 selector 推導。
3. **`PROCESS-EVIDENCE` 狀態**（#185）：已依 rulings/p6.md 實作，只限 #185，並依 sol P6-b64dad9-1 改為結構化區塊判定；請確認這個 gate 判定擴充（流程項以報告內的結構化紅／綠燈紀錄判 PASS）符合裁決原意。

## 10. LOCAL-WINDOWS-TODO

- Task 12 全部（WSAPoll mux、Winsock TCP console、com0com Serial console、W-1～W-7、`windows_console_native.rs`、`win-blackbox.ps1 -Subset console`）、`s22_gate:win_test/windows` 集合凍結與報告。
- parity #108：Windows TCP `attach_console` 不開 lease（`slice22-owner-runbook.md` #108 節）。
- 新案例 `co-02-line-backspace`、`df20-recover-reboot-stale-interactive-id` 的 Windows 執行。

## 11. BLOCKED-OWNER

- 07 BLOCKED-OWNER 總表 #1～#7；對照表 62 案（BO-S22-01～05、BO-S21-07／08／13／14／16）；parity #108（Windows 原生）。
- `f4-interactive-line-cmd-defined` parity 判定（沿用）；SC-28／29／31 `IMPLEMENTED` 須 Step 4 全通過（含 win-test）。

## 12. 本 slice 距出口還缺

1. P5 修 `test_two_daemons_same_tty` 的 harness（SPEC-GAP #1）→ `blackbox_rust` 轉綠。
2. P5 依 OD-55 處理 `s21_core` ignored／cap 列 → `test_l1_judge` 兩案轉綠、P6-e9b5c61-4 結案。
3. Windows：Task 12、`s22_gate:win_test`、#108。
4. 62 案 BLOCKED-OWNER 實機證據；之後 SC-28／29／31 才能標 IMPLEMENTED。
5. P8 承接 #108 file 傳輸案例（SPEC-GAP #4）。
6. 出口整包審查（Codex sol＋Claude Opus 雙審）。
