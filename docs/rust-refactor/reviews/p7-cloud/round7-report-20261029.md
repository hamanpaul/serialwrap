# P7（Slice 2.3）第 7 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p7`，起點 `a04aa69` → 終點 `62e430d`（全數 fast-forward push，無被拒）。
- 執行身分：ubuntu（非 root）；4 CPU；Rust 1.96.1（rustfmt、x86_64-pc-windows-gnu＋mingw-w64）；CPython 3.12.3＋PyYAML 6.0.3＋pytest 9.0.3＋jsonschema 4.10.3＋pyserial 3.5（與 CI 預設 job 相同，不含 google-re2；另備 pytest 9.2.0 venv 只做收集比對）。
- 依據：review-convergence-rules.md、rulings/p7.md（末 7 列）、rulings/p5.md OD-64～OD-67、rulings/s1.md、round6-report、起點最新審查 `b85eec5_a04aa69-sol`；結束前 fetch 另見本輪增量審查 `a04aa69_38c150c-sol`（已處理，見下）、rulings/p6.md 的 `test_sc30_reserved_for_p7` 裁決（P7 已於 `47e1af7` 同修）與 rulings/s1.md S1X-a 第 4 輪裁決（與 P7 無關）。

## Commit 清單

| commit | 摘要 |
|---|---|
| `733381e` | merge：併入 origin/feature/rust-refactor `8963d07`（OD-48 legacy handoff、0.4.0 release 前置、Docker scope cgroup）。衝突：behavior_changes.yaml 聯集（SC-39 加在 SC-37 後、revision 保留 8）、`test_bc_real_sc_df_ranges` 聯集、`proc_scan/posix.rs`（P5 擁有）採 P5 合併同主線的解法 `d657a7c` 再加回 P7 的 `held_fd_targets` |
| `37471d3` | merge：併入 origin/feature/rust-refactor-p5 `1b83da9`（OD-55、OD-58／64／65／66、harness 修正）。語意衝突：`mailbox.rs` OD-64 探針不窮舉 P7 分支的 console DataEvent（test-fakes 編譯失敗），改為接受所有 data 變體（與 P6 `09115d7` 等價）。鎖對 P7 父節點少 27 筆 s21_core.linux，皆 P5 依 OD-55 移除 |
| `47e1af7` | test：merge 主線後 `test_sc30_reserved_for_p7` 精確 SC 集合補 SC-39（前例 `9065f14`；P6 端依 rulings/p6.md 同修） |
| `903c052` | merge：併入 P5 `3bbb40e`（OD-67 明確 ids），無衝突 |
| `38c150c` | feat：落實第 6 輪裁決——OD-59 control latency 案例、s23 interleave 以 WAL 先後判定、coverage 承擔來源（B-09～B-16＋先前切片證據匯入）、Windows COM 能力集合；計畫 08／CHG／LW 同步 |
| `766de94` | docs：gate r13b-261029 紀錄（`docs/rust-refactor/evidence/p7-gate-261029-r13.md`、`p7-ab-261029-r13.md`）；全套 pytest 集合 add-only 登記 merge 帶入的 147 個已通過節點 |
| `62e430d` | fix：coverage 匯入的先前切片證據須綁定本 gate 的 HEAD 與受測 daemon（sol a04aa69_38c150c-3） |

## High／Medium 與裁決的處理

起點最新審查 `b85eec5_a04aa69-sol`：

| 項目 | 處理 |
|---|---|
| P7-9065f14-1（Medium） | 依裁決不處理（P6 第 7 輪移除 `l15-delayed-reply`）；`test_slice22_case_set_frozen_and_mapped` 維持 BLOCKED-UPSTREAM |
| P7-cd7acf0-4（Medium，累計 5 輪） | 不成立主張維持（P6 檔案不在本分支、gate 記 BLOCKED-UPSTREAM 不誤判 PASS）；≥3 輪，列 OWNER-BATCH |
| P7-f9e90a9-4（High，3 輪） | **已修 `38c150c`**（SPEC-GAP #7 裁決：WAL 先後判定；審查 a04aa69_38c150c 核對為已修） |
| P7-b85eec5-1（Medium） | **已修 `903c052`**（OD-67；P7 集合舊 id → 新 id 恆等 5／4／4，9.0.3 與 9.2.0 收集皆缺 0） |
| P7-a04aa69-1／-2、P7-b85eec5-2、P7-de7a95a-1～3（Low） | 依收斂規則不修 |

本輪增量審查 `a04aa69_38c150c-sol`（B 段）：

| 項目 | 處理 |
|---|---|
| P7-38c150c-1（Medium）`p7_gate:l15-slice21-b` 凍結 OD-58 已移出的 `s21-event-latency` 節點，列恆 INCOMPLETE | OWNER-BATCH #1（需確認 OD-58 涵蓋 P7 集合後才 `--allow-removal`） |
| P7-38c150c-2（Medium）OD-59 案例樣本量 200／20×1 少於契約 500×3 | OWNER-BATCH #2；CHG-S23-CTRL-LATENCY-OD59-01 改記「樣本量偏離、待裁決」 |
| P7-38c150c-3（Medium）先前切片匯入證據無來源綁定 | **已修 `62e430d`**：須 `git_revision`＝本 run `git_head` 且 daemon sha256＝快照，否則不採用；回歸修前紅 |
| P7-38c150c-4（Low） | 本輪紀錄筆誤（merge 編號、mailbox 未揭露、OD-67 過時），已順帶更正 |
| P7-38c150c-5／-6（Low） | 不修 |

rulings/p7.md 末 7 列與 OD-67：

| 裁決 | 處理 |
|---|---|
| OD-59 | `test_control_latency_under_flood_with_events`（`38c150c`）：工具與口徑沿用 `event_latency_measure.py`，stop／cancel 每輪最大值 ≤ `max_ms_flood` 100 ms，ALL 規則下 `event_dropped` 有界；A／B 皆 PASS（cancel max 1.703／2.358 ms、stop 0.706／0.367 ms；上限突變 0.5 ms → FAIL）。樣本量見 OWNER-BATCH #2 |
| P7-9065f14-1 由 P6 移除 | 依裁決不處理 |
| OD-55（P7-BO-4） | merge 後重驗：linux 已無 ignored 列（`test_s21_core_linux_capability_rows_moved_to_cap_sets_od55` PASS）；`test_l1_judge` 兩支 OD-24 防線仍紅，剩 `s21_core.windows`（P5，須 Windows 原生收集） |
| SPEC-GAP #4 coverage 承擔來源 | (i) 08 總表補 B-09～B-16（F1～F6、F8、F12；現行鎖 27 案，裁決文字記 29 案，以鎖為準）；(ii) 2.1／2.2 先前切片案例改以 slice21 gate run 目錄與 s22 gate 輸出逐案判定，並綁定本 gate HEAD／daemon（`62e430d`） |
| SPEC-GAP #5（OD-53） | `WIN_COM_ARGS` 改為 `-- --ignored cap_windows_com__`、`--caps windows_com`；原生收集登記仍為 P7-LW-9 |
| SPEC-GAP #6 | 計畫 08 路徑同步、CHG 註記裁決 |
| SPEC-GAP #7 | `s23_probe_ready_windows`：WAL seq 先後切 probe 窗口，窗口內不得有 system／system:* 以外來源 TX、已收窗口 ≥ 2；Python 基線 PASS（窗口 2、無問題；注入 agent TX → 報錯）；Rust 仍 FAIL 於 P6 console_attach |
| OD-67 | P5 `3bbb40e` 已給明確 ids 且與舊 id 恆等；P7 集合無移除 |

merge 後依 rulings/s1.md「S1X-a 修正對 Rust 的連帶」與 OD-62／OD-63 檢查：`origin/feature/s1x-a-fixes` 尚未合入主線（該分支領先主線 15 筆），依指示列待辦；OD-57「S1X-a 合入後重測 Python、DF-23 可能改 parity 結案」同。

## Gate 數字（r13b-261029，`38c150c`，非 dry-run、非 root；verify rc 0）

開跑前無洩漏 serialwrapd；loadavg 0.56（16:59Z）→ 0.71（19:32Z），約 2 小時 33 分。

| 列 | 狀態 | 說明 |
|---|---|---|
| check_cfg_boundary | PASS | |
| cargo test --workspace | PASS | p7_gate:cargo-test-workspace 272 筆 |
| win-test.sh (non-COM)／--caps windows_com | BLOCKED-OWNER B-06 | 非 WSL、無 SW_TEST_COM_PAIR |
| contract_blackbox p7 A／B | PASS／PASS | 40（1 skip＝SC-30 rust_only）／41 |
| contract_blackbox slice21 B | INCOMPLETE | 71 passed、8 skipped：凍結集合含 Linux 必 skip 的 7 個 windows_native 與 1 個 DF-20 deferred（P6）節點（OWNER-BATCH #4）；`test_two_daemons_same_tty` 經 P5 修正已轉綠 |
| contract_blackbox slice22 B | INCOMPLETE | P6 檔案不在本分支（BLOCKED-UPSTREAM） |
| l15 slice21 B | INCOMPLETE | 17 passed；缺 `s21-event-latency`（OWNER-BATCH #1） |
| l15 slice23 B | FAIL | 4 筆皆 P6 `session.console_attach`：cowork-01、l15-dut-reboot-port-alive、s23-interleave、s23-reboot-recovery（Python s23 六案含 WAL 判定皆 PASS） |
| func-test brcm-01-login-flow／ev-01-spawn-handler | PASS／PASS | Linux；ev-01 Windows 列 P7-LW-7 |
| regen_lock --check | PASS | |
| cases.lock slice_2.3 coverage | FAIL | 154 案：37 PASS、82 BLOCKED-OWNER（含新 B-09～B-16 27 案）、35 缺漏（2.2 匯入 19、2.1 匯入 13、P6 相關 3：l15-dut-reboot-port-alive 與 §11.3-2／-3 黑箱未建）；control-latency 已轉 PASS |
| python3 -m pytest -q tests/ | FAIL | 5 failed、5766 passed、224 skipped（1:16:51），失敗集合為起點基準子集 |
| policy_check | PASS | 0 fail、2 warn（R-19、R-22） |

結論：**NOT-PASS**（餘項皆為 BLOCKED-OWNER、BLOCKED-UPSTREAM、OWNER-BATCH 或先前切片證據缺）。

全套 pytest（序列執行）：

| 時點 | 結果 | 相對起點基準 |
|---|---|---|
| 起點 `a04aa69`（無 pyserial） | 12 failed、5599 passed、233 skipped（1:15:48） | 另 5 筆為 pyserial 缺漏所致 |
| 起點 `a04aa69` 對齊 CI（pyserial 3.5 重跑 11 個 pyserial 相關檔：317 passed、53 skipped、0 failed） | 7 failed：slice21 `test_queue_full_backpressure`、`test_two_daemons_same_tty`、`test_slice22_case_set_frozen_and_mapped`、`test_l1_judge` 兩支、`test_cargo_workspace_tests_windows_native`、`test_p4_required_cases_have_passing_evidence` | — |
| gate 內 `38c150c` | 5 failed、5766 passed、224 skipped | 子集（slice21 兩支轉綠） |
| 終點 `62e430d` | 5 failed、5767 passed、224 skipped、1 xfailed（1:15:25；開跑 loadavg 0.63、無洩漏 daemon） | 子集：`test_slice22_case_set_frozen_and_mapped`（P7-BU-10）、`test_l1_judge` 兩支（P5 `s21_core.windows`）、`test_cargo_workspace_tests_windows_native`（Windows）、`test_p4_required_cases_have_passing_evidence`（擁有者 P4 證據）；無新增失敗 |

其他驗證（每組 commit）：`cargo test --locked --workspace --features test-fakes` 2199 passed／0 failed／41 ignored；`cargo check --workspace --target x86_64-pc-windows-gnu`、`check_cfg_boundary`、`tools.contracts check`、`regen_lock --check`（各 commit `--base HEAD~1`；merge 對 PR base 主線 OK）、policy_check（0 fail）皆綠。

## 不成立主張

1. P7-cd7acf0-4（`p7_gate:contract-blackbox-slice22-b` 未登記）：測試檔屬 P6、不在本分支，無從收集登記；gate 照實 INCOMPLETE。審查者前輪已核對屬實，請裁定延後至 P6 合入（OWNER-BATCH #3）。

## SPEC-GAPS

1. s22 gate（`scripts/rust/s22_gate_check.py`，P6）輸出沒有來源身分（`identity.git_revision`／`daemon_sha256`）：依 P7-38c150c-3 的綁定規則，2.2 先前切片 19 案在 P6 補上身分欄位前一律缺漏。
2. 先前切片證據的 artifact 綁定：slice21 gate 以 release build 受測，P7 gate 快照為 debug build，daemon sha256 必不相同；要讓 2.1 匯入證據可被採用，需裁定程序（例如 slice21 gate 以 P7 快照為受測 artifact、或 P7 gate 接受指定 artifact 目錄）。
3. `tests/test_contracts_slice22.py` 為 P6 起源檔，`47e1af7` 依前例 `9065f14` 只在 P7 維護的 `test_sc30_reserved_for_p7` 精確集合補 SC-39（merge 造成的確定性回歸）；若編排者認定此函式屬 P6 擁有，請改裁。

## 既有碼待辦（不修）

- `p7_gate.sh` 快照來源寫死 `$ROOT/target/debug/`，未遵從 `CARGO_TARGET_DIR`（前輪已列）。
- `crates/sw-cli/tests/emitted_codes.rs` 固定寫 repo 內 `target/`（非 P7；唯讀 checkout 會失敗）。
- `cargo fmt --all -- --check` 對既有檔案仍有差異（前輪記錄）。
- Low：P7-de7a95a-1～3、P7-b85eec5-2、P7-a04aa69-1／-2、P7-38c150c-5／-6（記 low-backlog）。

## OWNER-BATCH

1. P7-38c150c-1：OD-58 的逐名移除是否涵蓋 `p7_gate:l15-slice21-b` 的 `test_case[rust-SERIALWRAP_L15_RUST_DAEMON-s21-event-latency]`（PR base 為主線、主線鎖無 p7_gate 集合，CI 白名單不需改）。核准後與「p7_gate pytest 集合凍結節點皆可收集」回歸同 commit 交付。
2. P7-38c150c-2：OD-59 案例樣本量——(a) 跑滿契約 500×3（估 A 約 45 分、B 約 30 分，全套 pytest 與 CI 預設 job 各增約 45 分）、(b) 核准 200／20×1 並記為偏離、(c) 移入 gate 專屬 section 以契約值執行。
3. P7-cd7acf0-4（累計 5 輪）：裁定延後至 P6 合入。
4. `p7_gate:contract-blackbox-slice21-b` 凍結集合含 Linux 必 skip 的 8 節點（`test_windows_nocom.py` 7 個 windows_native、`test_safety_core.py::test_stale_context_not_replayed` DF-20 deferred），Linux gate 永不 PASS：請裁定逐名移出（windows_native 改由 Windows 原生集合承擔、DF-20 待 P6）或改 gate 對平台 skip 的判定。
5. （前輪審查既有碼待辦）Python `recover_session` force recover 期間持 `self._lock` 約 10 s（同窗 `session list` 6 s 逾時），Rust 0.01 s 回應：是否比照 OD-57 登記 DF。

## LOCAL-WINDOWS-TODO

- P7-LW-1～LW-8 維持。
- P7-LW-9 更新：COM 列已改為 OD-53 能力集合 argv（`-- --ignored cap_windows_com__`），原生收集後以 `regen_lock --l1-add-only --set p7_gate:win-test-sh-caps-windows-com --platform windows --argv-json …` 登記；non-COM 列同樣待原生 `--list`。

## BLOCKED-OWNER

- B-01～B-08（實機、bench、Windows 工作站）、新增 B-09～B-16（先前切片 L2 F1～F6、F8、F12，共 27 案，程序見 08 總表）、P7-BO-2；P7-BO-4 的 windows 部分（P5 `s21_core.windows`）。

## BLOCKED-UPSTREAM

- P6：P7-BU-1～BU-7、BU-10；`session.console_attach` 使 l15 四案 Rust FAIL；§11.3-2／-3 黑箱兩案未建；slice22 B 列；s22 gate 逐案證據（含 SPEC-GAP 1 的身分欄位）。
- 主線：S1X-a 修正尚未合入——rulings/s1.md 連帶（a-1／a-3／a-4＝OD-62／a-7）、OD-63、OD-57 的 Python 重測待合入後處理。

## 距 Slice 2.3 出口還缺

1. P6 合入：l15 四案與 §11.3-2／-3 黑箱 Rust PASS、slice22 B 登記並 PASS、P7-BU-10、s22 gate 證據（含身分）。
2. OWNER-BATCH #1、#2、#4 裁決後：l15 slice21 B、slice21 B 才可能 PASS，OD-59 案例口徑定案。
3. 先前切片證據：以 P7 HEAD 與同一 artifact 跑 slice21 gate 取得 2.1 的 13 案逐案證據（SPEC-GAP 2 程序）。
4. S1X-a 合入主線後依修正後基線對齊（OD-62／63、DF-23 重測）。
5. Windows 原生（LW-1～9、B-06）與擁有者證據 B-01～B-16。
6. slice 出口整包審查（Codex sol＋Claude Opus 雙審）。
