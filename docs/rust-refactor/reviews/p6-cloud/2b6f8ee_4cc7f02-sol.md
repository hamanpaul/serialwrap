> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p6 增量審查 2b6f8ee..4cc7f02

範圍：

- 自有 commit：`d72afac`（紅燈）、`12318a5`（OD-53 由 selector 推導 `--caps`）。
- merge：`8caf6c4`（併 897a3cd）、`4cc7f02`（併 8963d07），只看衝突解法與合入影響。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | High | 已修 `bd62919` | 0 | 本輪 diff 沒有動到 session／console 相關 Rust 檔；workspace cargo 測試為綠。 |
| 2 | Medium | 已修 `bd62919` | 0 | 同上，本輪未變。 |
| 3 | High | 已修 `bd62919` | 0 | 同上，本輪未變。 |
| P6-e9b5c61-1 | High | 已修 `7370b8f` | 0 | 本輪未變。 |
| P6-e9b5c61-2 | High | 已修 `7370b8f` | 0 | 本輪未變。 |
| P6-e9b5c61-3 | Medium | 已修 `0c20ee8` | 0 | 4cc7f02 上 `test_freeze_case_fingerprints_match_current_cases[2.1／2.2]` 通過。 |
| P6-e9b5c61-4 | Medium | 依裁決不處理 | 9 | 依編排者裁決（OD-55，由 P5 交付）不再計入 P6 未修輪數，輪數停在 9。稽核仍紅：40 列全屬 `s21_core`，與 2b6f8ee 逐列相同。本輪併的是 feature/rust-refactor 而不是 P5，所以「merge P5 時重驗」尚未觸發。 |
| P6-c6ddda1-1 | Medium | 已修 `bf61e3e` | 0 | 新增的 `_evidence` 過濾只排除 EVIDENCE_MISSING 集合的列，同份報告內部重複仍判 INSUFFICIENT；`tests/test_s22_gate_check.py` 21 passed。 |
| P6-c6ddda1-2 | Medium | 已修 `464adec` | 0 | 4cc7f02 上 `policy_check --repo .` 的 R-21 pass、無 FAIL；R-19／R-22 為 warn，與 2b6f8ee 相同。 |
| P6-bf61e3e-1 | Medium | 已修 `e0aab47` | 0 | 本輪未變，相關測試通過。 |
| P6-b64dad9-1 | High | 已修 `2b6f8ee` | 0 | 本輪沒有改 `_process_evidence_problems`；相關判定器測試通過。 |

## B 新發現

- P6-4cc7f02-1 [Medium] tests/test_contracts_slice22.py:48
  - 問題：merge `4cc7f02` 合入 SC-39（OD-48）。衝突檔 `tests/test_contracts_catalog.py` 已把 SC-39 併進預期集合，但 P6 自有的契約測試 `test_sc30_reserved_for_p7` 仍逐筆釘定舊 SC 集合，結果引入一個新的測試失敗，違反 CLAUDE.md「不得引入新的測試失敗」。CI（`.github/workflows/tests.yml` 跑 `pytest -q tests/`）會紅。
  - 失敗情境：在 4cc7f02 執行 `python3 -m pytest -q tests/test_contracts_slice22.py::test_sc30_reserved_for_p7`，得到 `AssertionError … Extra items in the left set: 'SC-39'`。同一測試在 2b6f8ee 通過。我以一般使用者分別在兩個 commit 重跑全套失敗清單，這是唯一一個 base 綠、head 紅的項目。
  - 修法：預期集合改為 `({f"SC-{i:02d}" for i in range(1, 30)} | {"SC-31", "SC-34", "SC-35", "SC-36", "SC-37", "SC-39"}) - {"SC-08"}`，註解補「SC-39 依 OD-48 登記」。不得放寬成子集合比對。
- P6-4cc7f02-2 [Low] tests/test_l1_judge.py:845（對照 scripts/rust/s22_gate_check.py:99）
  - 問題：OD-53 的推導規則有兩份實作，判準不同。L1 稽核的 `_selector_explicit_caps` 會丟掉所有 `-` 開頭的 token，regex 為 `^cap_([a-z0-9_]+?)__$`；gate 的 `derive_caps` 則拒絕任何額外旗標。
  - 實測：selector 的 test-binary 參數為 `--ignored cap_posix_pty__ --nocapture` 時，稽核回 `['posix_pty']`（豁免 `#[ignore]` 列），gate 回 `None`（EVIDENCE_MISSING）。判準會漂移，但 gate 是失敗即擋，不會誤判 PASS。
  - 修法：測試以 importlib 載入 gate 的 `derive_caps`，豁免時用 `derive_caps(sel) or []`，刪除測試內的第二份規則。

### 本輪兩個自有 commit（`d72afac`、`12318a5`）

- `d72afac` 對修正前的 gate 有 4 項紅燈。其中 `--include-ignored cap_posix_pty__` 那項紅的原因正是 section 被判 `PASS`，修正後 21 passed。
- 推導規則與收集器實際行為一致：收集器只以 `<exe> --ignored cap_<cap>__` 補跑，不轉交 test-binary 參數。
- `collect_argv` 經 `cargo_l1_results.py` 的 `parse_known_args` 解析後，report 的 `argv` 等於鎖的 selector（python3.11、3.12、3.13 皆驗證）。
- 以 `--plan` 輸出的命令實跑 `console_conformance_pty`：31 passed、3 skipped，`l1_judge` 判 PASS。
- 現行鎖的 `s22_gate:*` 集合全部可推導，`--plan` exit 0。
- EVIDENCE_MISSING 集合的 passed 列不當逐案證據、failed 列仍判 FAIL，`result` 併入 INSUFFICIENT，符合裁決。

### 兩個 merge（`8caf6c4`、`4cc7f02`）

- `8caf6c4` 無衝突，與自動合併結果相同。
- `4cc7f02` 三個衝突檔的解法正確：
  - `behavior_changes.yaml` 同時保留 SC-37 與 SC-39，檔頭範圍已更新。
  - `test_contracts_catalog.py` 的 SC 集合取聯集。
  - `proc_scan/posix.rs` 的 `uart_holders_same_uid` 改用 `tty_holders_fail_closed_with` 並刪除 `scan_fd_holders`。逐支比對後，兩者對 target stat、fd 目錄、fd stat 錯誤的 Permission／Unknown 降級，以及 NotFound 略過，語意等價。
  - L1 鎖為純追加。
  - Windows target 上 `cargo check -p sw-pal -p sw-cli` 通過。
- 問題出在非衝突的連帶影響，即上面的 P6-4cc7f02-1。

### 測試（一般使用者）

- workspace `cargo test --features test-fakes -- --skip cap_`：2178 passed、1 failed。
  - 失敗項 `sw-pal::rpc_listener::uds_mode_0660_baseline` 是環境因素：測試帳號沒有附加群組，`usable_group()` 回「證據不足」。本輪沒有動到該檔。
- 全套 `pytest -q tests/`：13 failed、5630 passed、222 skipped。
  - 其中 12 項在 2b6f8ee 同樣失敗：pyserial 未安裝 5 項、Windows 原生 1 項、本機 p4 證據檔缺 1 項、l1_judge 3 項、黑箱 `test_two_daemons_same_tty`（P5 harness 已知）、`test_queue_full_backpressure`。
  - 只有 `test_sc30_reserved_for_p7` 是新失敗。

## 既有碼待辦（不計入結論）

- 計畫 07 Task 13 Step 4 的收集命令與 L1 鎖的 selector 不一致。
  - 位置：本分支 docs/rust-refactor/plans/07-slice22-console.md:2693-2694；origin/docs/parity-amendments 修訂後計畫的同段在 2670-2671。
  - 問題：`console_conformance_pty` 仍手打 `--include-ignored cap_posix_pty__` 且沒有 `--caps`，`cargo_workspace` 也少了 `-- --skip cap_`。照文件執行時 report 的 argv 不等於 selector，會判 INSUFFICIENT（失敗即擋，不會誤判 PASS）。
  - 建議：改成「以 `s22_gate_check.py --plan` 輸出的 `collect_argv` 收集」。
- `test_l1_lock_frozen_set_excludes_capability_gated` 在 2b6f8ee 與 4cc7f02 都紅（`s21_core.windows` 含能力 SKIP 列，屬 P5 集合），在 8963d07 為綠。與 P6-e9b5c61-4 同屬 P5 交付範圍。
- `test_l1_lock_nodeids_subset_of_collect_only` 在 python3.12.3 下，2b6f8ee、4cc7f02、8963d07 三者都紅。原因是 `s0_bc` 有一筆參數化 id 含 `FileNotFoundError(..., None, 2)` 的 repr，疑似隨 Python 版本或環境而變。
- `contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure` 在 2b6f8ee 與 4cc7f02 都紅（「前景 sleep 30 期間連送 300 筆」沒有出現 SESSION_QUEUE_FULL），非本輪引入，建議另查。

## 對裁決的異議（不計入結論）

無。

結論：需修正
