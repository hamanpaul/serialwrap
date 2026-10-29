# P7（Slice 2.3）第 6 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p7`，起點 `7bb9cfd` → 終點 `a04aa69`（全數 fast-forward push，無被拒）。
- 執行身分：ubuntu（非 root）；Rust 1.96.1（rustfmt、x86_64-pc-windows-gnu＋mingw）、CPython 3.12.3＋PyYAML 6.0.3＋pytest 9.0.3＋jsonschema 4.10.3（與 CI 預設 job 相同，不含 google-re2）。
- 依據：review-convergence-rules.md、rulings/p7.md（含本輪新增 OD-56、OD-57 兩列）、OD-14～OD-63、p7-cloud 最新報告
  `9065f14_c5f0fc9-sol`（起點）、`7bb9cfd_cd7acf0-sol`、`cd7acf0_f9e90a9-sol`、`f9e90a9_de7a95a`、`de7a95a_b85eec5`（後兩份為雲端 Opus xhigh 代理 sol）；結束前 fetch 另見 OD-64／OD-65（P5 event_latency，與 P7 無關）。

## Commit 清單

| commit | 摘要 |
|---|---|
| `b93849e` | merge：併入 origin/feature/rust-refactor-p5（2472f78，OD-45～OD-47 與 sol P5-3084388）；無衝突，L1 鎖對兩個父節點皆 add-only（對 P5 父節點僅既有 P7-BO-1 兩筆，本輪 OD-56 追認） |
| `cd7acf0` | feat：Slice 2.3 出口 gate 編排 `p7_gate.sh`、`p7_case_coverage.py`、`run_p7_ab.sh` 併入先前切片回歸與 func-test 兩案、`cap_windows_com_p7.rs`＋`com_peer.rs`、`p7-b01-procedure.md` |
| `61edbd8` | test：l15 slice23 六案（`func-test/scenarios/s23-*.yaml`）、第 5 輪案例設計修正 CHG 登記（`slice23-chg-register.md`＋`p7_chg_probe.py`）、`p7_gate:*` 測試列必要集合 add-only |
| `f9e90a9` | fix：sol 7bb9cfd_cd7acf0——凍結必要集合核對、verify 必要 artifact、COM 案例逐位元組比對與 banner 前取樣、COM pair 互斥、win-test argv |
| `efb8580` | fix：gate 實跑揭露——func-test 列 comparison.json 查找深度 |
| `6df20d2` | fix：gate 實跑揭露——受測 CLI／daemon 於建置後快照（cargo L1 列會以 test-fakes 重建 target/debug） |
| `de7a95a` | feat：slice_2.3 涵蓋以 cargo L1 報告承擔 crates/ 來源案例 |
| `b85eec5` | fix：sol cd7acf0_f9e90a9／f9e90a9_de7a95a——s23 窗口與 oracle 收緊、全套 pytest 集合完整凍結；OD-56 CI 白名單、OD-57 DF-23；r12b gate 紀錄 |
| `a04aa69` | test：sol de7a95a_b85eec5——force recover fd 層級 oracle（關閉後才重開）、gate 記錄 tool_versions |

## High／Medium 與裁決處理

起點最新 sol（9065f14_c5f0fc9）的 B 段已於第 5 輪 `7bb9cfd`／`29aaa7b` 修正；本輪期間又出現三份報告，依「只處理最新報告 A 段未修與 B 段新的 High／Medium」逐一處理（詳表見分支 `docs/rust-refactor/reviews/p7-fix-responses.md` 第 12 輪兩節）。

| 項目 | 處理 |
|---|---|
| P7-c5f0fc9-3（Medium） | **已修 `f9e90a9`**：verify 要求 `serialwrap`／`serialwrapd` artifact（`artifacts:{}` 不再通過） |
| P7-c5f0fc9-5（High） | **已修 `f9e90a9`**：A/B 必要節點改取 L1 鎖預先凍結的 `p7_gate:contract-blackbox-p7-a`／`-p7-b`；collect-only 只交叉核對 |
| P7-7bb9cfd-1（Medium，3 輪） | **已修 `f9e90a9`＋`b85eec5`**：repo 外證據記 `$RUN_DIR`／`$EXTERNAL` 完整相對路徑；表頭 run_dir 可還原 |
| P7-cd7acf0-1（High） | **已修 `f9e90a9`**：回歸各階段與 coverage 皆依凍結集合逐節點（缺漏、重複、替代節點不得 PASS） |
| P7-cd7acf0-2／-3（High） | **已修 `f9e90a9`**：逐位元組比對；quiet 取樣點移到送出 banner 前（兩支非 ignored oracle 測試，突變驗證紅） |
| P7-cd7acf0-4（Medium，3 輪） | **已修 `61edbd8`（6 個集合）**；`p7_gate:contract-blackbox-slice22-b` 不成立主張（審查者 de7a95a_b85eec5 已核對屬實，見下） |
| P7-cd7acf0-5／-6（Medium） | **已修 `f9e90a9`**：`WIN_*_ARGS`（cargo 參數全在 `--` 後，`--dry-run` 解析回歸）；`COM_PAIR_LOCK` |
| P7-f9e90a9-1（High） | **已修 `b85eec5`**：全套 pytest 集合 5616 筆（`-q tests/` 收集扣除 Linux skip／xfail 227 筆）；只 49 列不得 PASS 的回歸 |
| P7-f9e90a9-2（Medium） | **已修 `b85eec5`**：明確 ids 保留六個凍結 nodeid |
| P7-f9e90a9-3（High） | **已修 `b85eec5`**（de7a95a_b85eec5 核對為已修）：quiet 窗口於 emit banner 前開 |
| P7-f9e90a9-4（High，2 輪） | **部分已修 `b85eec5`＋SPEC-GAP**：已核到 `root\ns3cretP7\necho __READY__`；nonce 之後到 READY 前的界線 runner 字面比對無法表達（SPEC-GAP 7） |
| P7-b85eec5-1（Medium） | **依賴 P5 裁決＋部分已修 `a04aa69`**：3 個凍結 id 在 pytest 9.2.0 改名（本輪 9.0.3 皆存在）；報告記 tool_versions；請編排者把 rulings/p5.md「P5 第 6 輪 SPEC-GAP #3」擴及 `p7_gate:python3-m-pytest-q-tests`（OWNER-BATCH 5） |
| P7-b85eec5-2（Low） | 不修 |
| P7-f9e90a9-5（High，2 輪） | **已修 `b85eec5`＋`a04aa69`**：情境 `bridge_generation` 1→2（no-op 必紅）；黑箱 `test_force_recover_attached_closes_before_reopen` 以 5 ms fd 峰值 ==1 證明關閉後才重開（依審查提醒不採 wire peer_state：Rust 間隔約 1.6 ms），Python／Rust 皆 PASS |
| P7-9065f14-1（Medium，累計 6 輪） | **NEEDS-RULING／OWNER-BATCH**：`tests/acceptance/slice22_evidence_map.yaml` 屬 P6，依指示不 merge P6、不改他 Phase 檔；收斂規則 §4 累計 ≥3 輪升級 |
| P7-de7a95a-1～3（Low） | 依收斂規則不修（記 low-backlog） |
| rulings/p7.md 第 5 輪「案例設計修正」 | **完成**：`docs/rust-refactor/slice23-chg-register.md` 逐列 CHG（ATTACHED-PATH、REPROBE-ORDER、SC34-NONCE、CTRLC-FIELDS、STALL-OBS、NOISE-LINE、L15-PATH、REPLAY-BOUNDARY，及本輪情境收緊 QUIET-WINDOW-02、INTERLEAVE-02、FORCE-GEN-02），每列附 `scripts/rust/p7_chg_probe.py` Python 基線實測輸出（命令＋JSON）與受影響案例 Python 基線 PASS（14 passed）；**無一列改變對外欄位／錯誤碼／狀態語意，無改列 OWNER-BATCH** |
| OD-56（P7-BO-1） | **完成 `b85eec5`**：CI `--allow-removal` 只加兩個 s21_core.linux 舊 nodeid；windows 集合仍待原生替換（P7-LW-1） |
| OD-57（OWNER-BATCH #2） | **完成 `b85eec5`**：登記 DF-23；`test_release_during_login_waits_for_handle_close` 依 DF 分判，Python／Rust 皆 1 passed |

## Task 18／19 進度

- **Task 18**：`s23-*` 六案（Python 兩輪全 PASS；Rust 4 PASS、2 FAIL＝P6 human console）；`run_p7_ab.sh` 併入 slice21／slice22 黑箱、l15 slice21／slice23 與 func-test `brcm-01-login-flow`、`ev-01-spawn-handler`。
  `s23-reboot-recovery-no-replay` 首跑揭露 Python 在 agent 命令進行中即透傳 human raw 輸入（非 deferred），判定窗口改自重開機邊界起算（CHG-S23-REPLAY-BOUNDARY-01）。
- **Task 19**：`p7_gate.sh` 編排、`p7_gate_report.py init／record`、`p7_case_coverage.py`、gate 自身集合 add-only（p7-a 38、p7-b 39、slice21-b 79、l15-slice21-b 18、l15-slice23-b 21、python3-m-pytest-q-tests 5616）、`p7-b01-procedure.md`；`cap_windows_com_p7.rs`／`com_peer.rs` 於 Linux 建置、`cargo check --target x86_64-pc-windows-gnu` 綠（原生執行 LOCAL-WINDOWS-TODO P7-LW-8）。

## Gate 數字（Slice 2.3 出口 gate 試跑，非 dry-run、非 root）

兩次實跑：r12-261029（`61edbd8`）揭露三個 gate 自身缺陷（func-test 查找、artifact 被重建、crates/ 涵蓋），修正後 r12b-261029（`de7a95a`，紀錄 `docs/rust-refactor/evidence/p7-gate-261029.md`，`verify` 回空清單）：

| 列 | r12b | 說明 |
|---|---|---|
| check_cfg_boundary | PASS | |
| cargo test --workspace | PASS | p7_gate:cargo-test-workspace 272 筆 |
| win-test.sh (non-COM)／--caps windows_com | BLOCKED-OWNER B-06 | 非 WSL、無 SW_TEST_COM_PAIR |
| contract_blackbox p7 A／B | PASS／PASS | 38／39 筆 |
| contract_blackbox slice21 B | FAIL | `test_two_daemons_same_tty`（Python 起點基準同紅；rulings/p6 2c739605 已裁定為 P5 harness 缺陷，P5 照修） |
| contract_blackbox slice22 B | INCOMPLETE | P6 檔案不在本分支（BLOCKED-UPSTREAM） |
| l15 slice21 B | FAIL | `s21-event-latency`（daemon 無 test_fakes 計時欄位，OD-45 下恆 INSUFFICIENT_EVIDENCE；P5 OWNER-BATCH 已建議比照 OD-40／46 移出） |
| l15 slice23 B | FAIL | 4 筆皆 `session.console_attach` METHOD_NOT_FOUND（P6）：cowork-01、l15-dut-reboot-port-alive、s23-interleave、s23-reboot-recovery |
| func-test brcm-01-login-flow／ev-01-spawn-handler | PASS／PASS | Linux；ev-01 Windows 列 LOCAL-WINDOWS-TODO P7-LW-7 |
| regen_lock --check | PASS | |
| cases.lock slice_2.3 coverage | FAIL | 154 案：47 PASS、55 BLOCKED-OWNER、52 缺漏（10 個 2.2 案例、29 個無 B 編號的先前切片 L2 regression、P6 console 相關、`sm-01`／`sm-04`／`ha-05`／`l15-wal-interference` 等 2.1 案例本 gate 未執行、§11.3-2／-3 與 control-latency 三案未建立） |
| python3 -m pytest -q tests/ | FAIL | 7 failed、5605 passed、226 skipped（1:12:47）；**失敗集合為起點基準的子集**（基準 8 筆，`test_p7_goldens_fresh` 因環境改為 CI 預設不含 google-re2 而轉綠） |
| policy_check | PASS | 0 fail、2 warn（R-19、R-22，非 PR 情境） |

結論：**NOT-PASS**（餘項皆為 BLOCKED-OWNER、BLOCKED-UPSTREAM 或 SPEC-GAP）。

全套 pytest 逐 commit 群組核對（序列執行，與起點基準比較）：

| commit | 結果 | 相對起點新增失敗 |
|---|---|---|
| 起點 `7bb9cfd` | 8 failed、5569 passed、214 skipped（1:10:25） | — |
| `61edbd8`（r12 gate 內） | 7 failed、5593 passed | 無 |
| `de7a95a`（r12b gate 內） | 7 failed、5605 passed | 無 |
| `b85eec5` | 7 failed、5609 passed（1:12:52） | 無 |
| `a04aa69`（終點） | 7 failed、5611 passed、226 skipped（1:13:33） | 無 |

起點 8 筆：slice21 黑箱 `test_queue_full_backpressure`、`test_two_daemons_same_tty`（P5）、`test_slice22_case_set_frozen_and_mapped`（P7-BU-10）、`test_l1_judge` 兩支 OD-24 防線（P7-BO-4）、`test_cargo_workspace_tests_windows_native`、`test_p4_required_cases_have_passing_evidence`（Windows／P4 擁有者證據），及 `test_goldens_exist_and_are_fresh`（環境：起點跑時誤裝 google-re2，移除後與 CI 預設相同並轉綠）。

## 不成立主張

1. **P7-cd7acf0-4（第 2 輪記「未修」）的 `p7_gate:contract-blackbox-slice22-b`**：該列的測試檔（P6 `test_console_*.py`、`test_interactive_lease.py`、`test_slice22_family_mirrors.py`）不在本分支，依指示不 merge P6，無從收集登記；gate 照實記 INCOMPLETE（不轉 PASS）。P6 合入後登記。

## SPEC-GAPS

1. `contracts/resource_limits.yaml` 無 `control_latency_flood_p99_ms`：`test_control_latency_under_flood_with_events` 未建立（擁有者批次裁決中，本輪未處理）。
2. P7-9065f14-1：`slice22_evidence_map.yaml`（P6）需 P6 移除 `l15-delayed-reply`。
3. （撤回）P7-f9e90a9-5 的 wire peer_state 順序斷言：審查確認對 Rust 不可行（1.6 ms 間隔 < 50 ms 取樣），改以黑箱 fd 層級 oracle 承擔（`a04aa69`）。
4. `p7_case_coverage`：08 BLOCKED-OWNER 總表只給 F9／F10／F11 B 編號，slice_2.3 的其他先前切片 L2 regression family（F1～F6、F8、F12，29 案）無 B 編號，coverage 只能記缺漏；需編排者補編號或指定承擔來源。另 2.1 的 `sm-01`／`sm-04`／`ha-05`、`l15-wal-interference`（OD-40 由 pytest 承擔）在 slice_2.3 集合內，08 未指定本 gate 由哪一列承擔。
5. Windows COM gate 列的必要集合：三案為 `#[ignore = "cap:windows_com"]`，與 OD-24「cargo 凍結集合不得含 ignored 列」衝突，`p7_gate:win-test-sh-caps-windows-com` 的登記方式需裁定（P7-LW-9）。
6. 計畫路徑 `tests/acceptance/l15/slice23/*.yaml` → `func-test/scenarios/s23-*.yaml`（P2 runner 只探索該目錄；CHG-S23-L15-PATH-01）。
7. P7-f9e90a9-4：s23 interleave 的「nonce probe 送出後到 READY 前不得有 agent／human bytes」需 runner 支援帶隨機 nonce 的樣式或依 WAL 先後判定（測試工具獨立 PR）。

## 既有碼待辦（不修）

- `p7_gate.sh` 快照來源寫死 `$ROOT/target/debug/`，未遵從 `CARGO_TARGET_DIR`（f9e90a9_de7a95a 既有碼待辦；改版前同樣）。
- `tests/contract_blackbox/slice21/test_safety_core.py::test_two_daemons_same_tty`、`test_f2_backpressure.py::test_queue_full_backpressure`：起點基準即紅（前者已裁定 P5 harness 缺陷）。
- `s21-event-latency` 於非 test-fakes daemon 恆 INSUFFICIENT（P5 OWNER-BATCH）。
- `cargo fmt --all -- --check` 對既有檔案仍有差異（第 5 輪記錄）。
- Low：P7-de7a95a-1～3（coverage cargo 分支順序與報告完整性檢查、快照測試只查 daemon 名）。

## OWNER-BATCH

1. `control_latency_flood_p99_ms` 數值門檻凍結（維持待簽，本輪未處理）。
2. P7-9065f14-1（累計 5 輪，收斂規則 §4 升級）：由 P6 移除 slice22 evidence map 的 `l15-delayed-reply`，或裁定 P7 可在不 merge P6 的前提下豁免該測試。
3. P7-BO-4（OD-24 防線對 P5 s21_core `#[ignore]` 列）：rulings/p6 已記 OD-55 由 P5 交付，P7 待 merge P5 後重驗。
4. slice_2.3 coverage 的 29 個無 B 編號 L2 regression 案例與 2.1 先前切片案例的承擔來源（SPEC-GAP 4）。
5. P7-b85eec5-1：rulings/p5.md「P5 第 6 輪 SPEC-GAP #3」（三支測試補明確 ids）擴及 `p7_gate:python3-m-pytest-q-tests`，合入後 P7 以 add-only 登記新 id、逐名 `--allow-removal` 舊 id（3 筆）。

## LOCAL-WINDOWS-TODO

- P7-LW-1～LW-7 維持（LW-1：s21_core.windows 一對一替換，OD-56 已明示 Windows gate 照報缺測）。
- 新增 P7-LW-8：`cap_windows_com_p7` 三案與 `com_peer.rs` 原生執行（B-06）。
- 新增 P7-LW-9：`p7_gate:win-test-sh-non-com`（原生 `--list` 收集）與 COM 列集合登記（見 SPEC-GAP 5）。

## BLOCKED-OWNER

- B-01～B-08（實機、bench、Windows 工作站；B-01 程序已交付 `docs/rust-refactor/evidence/p7-b01-procedure.md`）、P7-BO-2、P7-BO-4。P7-BO-1 依 OD-56 結案（linux）。

## BLOCKED-UPSTREAM

- P6：P7-BU-1～BU-7、BU-10；`session.console_attach` 使 `cowork-01-agent-bad-cmd`、`l15-dut-reboot-port-alive`、`s23-interleave-login-agent-human`、`s23-reboot-recovery-no-replay` 於 Rust FAIL；§11.3-2／-3 黑箱兩案未建立；slice22 B 列。
- P5：`test_two_daemons_same_tty` harness 修正（rulings/p6 2c739605）；`s21-event-latency` 處置；本輪起點後 P5 新增的 `99591b3`、`02bfebf`、`f7e00db` 與主線 `897a3cd` 尚未併入（下一輪 merge；OD-62／OD-63 與 S1X-a 連帶裁決於 P7 merge 主線後依修正後 Python 基線重測，含 DF-23 是否改 parity 結案）。

## 距 Slice 2.3 出口還缺

1. P6 human console 合入：上列 4 個 l15 案例與 §11.3-2／-3 黑箱 Rust PASS；slice22 B 列登記並 PASS；P7-BU-10 map 同步。
2. P5：`test_two_daemons_same_tty` harness 修正、`s21-event-latency` 處置後 slice21 B／l15 slice21 B 轉綠。
3. `control_latency_flood_p99_ms` 凍結後補案例。
4. slice_2.3 coverage 缺漏的承擔來源裁定（SPEC-GAP 4）。
5. Windows 原生（LW-1～9、B-06）、擁有者證據 B-01～B-08。
6. slice 出口整包審查（Codex sol＋Claude Opus 雙審）。
