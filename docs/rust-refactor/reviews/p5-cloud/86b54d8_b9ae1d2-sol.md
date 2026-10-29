> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p5 增量審查 `86b54d8..b9ae1d2`

範圍：只有一個 commit `b9ae1d2e`（修 P5-86b54d8-1、P5-86b54d8-2），共 7 檔：
- `scripts/rust/slice21_gate_judge.py`
- `tests/acceptance/slice21_gate_expect.yaml`
- 三支測試檔
- changelog fragment
- `p5-fix-responses.md`

本輪沒有 Rust／`sw_core/` 變更：`git diff --stat 86b54d8 b9ae1d2 -- crates sw_core Cargo.lock` 為空。前輪已驗的 Rust 項目因此未重跑 cargo。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `25ad390` | 2 | 本輪無 Rust 變更。 |
| 2 | High | 已修 `25ad390` | 2 | 未改。 |
| 3 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 4 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 5 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 6 | High | 已修 `25ad390` | 2 | 未改。 |
| 7 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 8 | Medium | 已修 `25ad390` | 2 | 本輪只改 `judge()` 內的承擔計算，`build_expect()` 未動；`assemble`／`judge` 仍共用它。 |
| P5-1393573-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-1393573-2 | Medium | 已修 `d8ac988` | 2 | 未改。 |
| P5-5cc4623-1 | High | 已修 `0a7d0929` | 3 | 未改。 |
| P5-5cc4623-2 | Medium | 依裁決不處理 | 3 | 延後裁決仍適用。 |
| P5-70f6ce7-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-3c4e051-1 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3c4e051-2 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3084388-1 | High | 已修 `2472f78` | 3 | `tests/test_event_latency_measure.py` 通過。 |
| P5-3084388-2 | Medium | 已修 `289f7573` | 4 | 本輪 diff 不含 `scripts/rust/event_latency_measure.py` 與 `contracts/`，工具與契約 sha256 不變。 |
| P5-3084388-3 | Medium | 已修 `464adec` | 1 | 未改。 |
| P5-2472f78-1 | Medium | 已修 `c4cfb96` | 1 | 未改。 |
| P5-4f8acdf-1 | High | 已修 `86b54d8` | 1 | 本輪改寫承擔計算後仍有效。以現行 lock 與 expect（經 `build_expect`）重算，列出 `s21_gate:cap_{interactive_desktop,supervisor_runtime,sys_admin,windows_native}.linux` 四個（集合, 平台），verdict 為 INCOMPLETE。`test_uncarried_gate_set_blocks_ready` passed。 |
| P5-4f8acdf-2 | Medium | 已修 `86b54d8` | 1 | `test_imported_evidence_requires_clean_source_tree` 三案 passed。`test_wal_s1w_example_records_source_tree_dirty` 搬到 `test_slice21_wal_s1w_import_git.py`，仍 passed；該節點原本就未登記 lock，搬檔不影響鎖。 |
| P5-4f8acdf-3 | Medium | 已修 `86b54d8` | 1 | 本輪未動 sw-pal。 |
| P5-4f8acdf-4 | Low | 依裁決不處理 | 1 | 在 low-backlog。 |
| P5-4f8acdf-5 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-6 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-7 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-86b54d8-1 | Medium | 已修 `b9ae1d2` | 1 | 詳見下方「P5-86b54d8-1 核對」。豁免 section 仍有殘留缺口，另列 P5-b9ae1d2-2（Low）。 |
| P5-86b54d8-2 | Medium | 已修 `b9ae1d2` | 1 | 詳見下方「P5-86b54d8-2 核對」。熱路徑另有 sw-pal clock 與 `lib.rs` 未涵蓋，另列 P5-b9ae1d2-1。 |
| P5-86b54d8-3 | Low | 依裁決不處理 | 1 | 依收斂規則移入 low-backlog。本輪新增的 7 個 pytest 節點同樣未登記 `s21_gate:pytest_full`：`test_uncarried_gate_set_is_judged_per_platform`、`test_non_test_section_fragment_cannot_carry_gate_set`、`test_import_rejected_after_wal_dependency_change_in_real_repo` 的 4 個參數、搬檔後的 `test_wal_s1w_example_records_source_tree_dirty`。 |
| P5-86b54d8-4 | Low | 依裁決不處理 | 1 | 未改。 |
| P5-86b54d8-5 | Low | 依裁決不處理 | 1 | 新增的 git 測試驗的是 judge 端；`wal_s1w.rs:310-317` 的實際判定行為仍只有字串比對。 |

**P5-86b54d8-1 核對**
- `carried` 已改為（集合, 平台）對，只從 `TEST_SECTIONS` 收集，平台取 `TEST_SECTIONS[name]`。
- `uncarried` 逐一列出 lock 中每個 `s21_gate:*` 的（sid, platform）。
- 紅燈先行：換回 `86b54d8` 版 judge，`test_uncarried_gate_set_is_judged_per_platform` 與 `test_non_test_section_fragment_cannot_carry_gate_set` 都紅（得 READY）；`b9ae1d2` 版 passed。

**P5-86b54d8-2 核對**
- `relevant_paths` 已加入 `crates/sw-proto`。
- scratch clone 實跑：在 `b9ae1d2` 之上只改 `crates/sw-proto/src/wal_types.rs`，得 EVIDENCE_MISSING。
- 紅燈先行：換回 `86b54d8` 版 expect，`test_import_rejected_after_wal_dependency_change_in_real_repo` 的 sw-proto 兩案與 `test_relevant_paths_cover_build_profile_manifests` 都紅。

## B 新發現

- **P5-b9ae1d2-1 [Medium]** `tests/acceptance/slice21_gate_expect.yaml:20-22`（`relevant_paths`，`b9ae1d2` 修改）
  - 問題：本輪把 sw-proto 列入可追溯性路徑，但 WAL append／rotate 熱路徑另外依賴 sw-pal 的時鐘與組裝點，兩者都不在清單內：
    - `crates/sw-pal/src/clock/posix.rs` 的 `NativeClock`：每筆 append 都呼叫 `mono_ns()`（`crates/sw-core/src/wal.rs:516`）與 `wall_unix_us()`（`wal.rs:556`），rotate 的歸檔時間戳也用 `wall_unix_us()`（`wal.rs:656`）。
    - `crates/sw-pal/src/lib.rs` 的 `native()`：`wal_s1w.rs:397` 經它取得受測的 `wal_fs`（`lib.rs:208` `NativeWalFs`）與 `clock`（`lib.rs:178`、`:187` `NativeClock`）。只改這裡，就能換掉受測的 WalFs／Clock 實作。
    - 次要：`crates/sw-pal/src/traits.rs`，內有 `WalFs`／`WalFile`／`Clock` trait 與 `posix_io_error`；`wal_fs.rs:3` 從這裡 re-export。
  - 這些檔案確實會隨 WAL 工作變動。例如 `df66409f feat(sw-pal): WAL 常駐 handle…` 同時改過 `lib.rs` 與 `traits.rs`；OD-61 的 rotate 停頓修正也可能動到同一區域。
  - 失敗情境：在 scratch clone 實跑，未動審查 worktree。
    - 匯入證據設定：`env.git_revision=b9ae1d2e7324…`、`virtualization=wsl2`、`source_tree_dirty=false`、`verdict=PASS`。`relevant_paths`／`formal_params` 取自 `b9ae1d2` 的 expect。
    - 在 `b9ae1d2` 之上，各加一個只在檔尾加一行註解的 commit，結果如下：
      - `crates/sw-proto/src/wal_types.rs`：EVIDENCE_MISSING（對照組，正確）。
      - `crates/sw-pal/src/clock/posix.rs`：`PASS ['匯入證據（owner-wsl2）：全數達標']`，預期 EVIDENCE_MISSING。
      - `crates/sw-pal/src/lib.rs`：PASS，預期 EVIDENCE_MISSING。
      - `crates/sw-pal/src/traits.rs`：PASS。
  - 確切修法：
    - `wal_s1w.relevant_paths` 加入 `crates/sw-pal/src/clock.rs`、`crates/sw-pal/src/clock`、`crates/sw-pal/src/lib.rs`、`crates/sw-pal/src/traits.rs`。
    - `test_import_rejected_after_wal_dependency_change_in_real_repo` 的參數化補 `crates/sw-pal/src/clock/posix.rs`、`crates/sw-pal/src/lib.rs`，預期 EVIDENCE_MISSING；臨時 repo 的初始檔清單一併補上。
    - `test_relevant_paths_cover_build_profile_manifests` 補同樣的路徑。

- **P5-b9ae1d2-2 [Low]** `scripts/rust/slice21_gate_judge.py:686-691`（`b9ae1d2`）
  - 問題：`windows_cap_windows_com` 屬於 `TEST_SECTIONS`，也在 `ALLOWED_MISSING`（BO-S21-01）。它片段中自帶的 `required_set` 與 `additional_required_sets` 照樣算承擔，而它的 EVIDENCE_MISSING 又不阻擋。結果是報告片段可以把任意 windows 集合納入 BO-S21-01 豁免：集合從未執行，gate 仍然 READY。這與本輪 (b) 修正要貫徹的「不接受報告自行宣告承擔」原則不一致。
  - 失敗情境：以 `tests/test_slice21_gate_judge.py` 的 `_Case` 實跑，在 lock 加一個 windows-only 的 `s21_gate:cap_example`（1 列）。
    - 基準：INCOMPLETE，列出 `s21_gate:cap_example.windows`（正確）。
    - 在 `windows_cap_windows_com` 片段加 `"additional_required_sets": ["s21_gate:cap_example"]`：得 `READY_FOR_OWNER_EVIDENCE`，`verdict_reasons` 為空。
    - 若比照現行正式 lock，移除 `s21_gate:windows_cap_windows_com`，再把該片段的 `required_set` 改指 `s21_gate:cap_example`：同樣得 READY。
    - 兩種情況的預期都是 INCOMPLETE。
  - 判 Low 的理由：`record-section`／`slice21_gate.sh` 從不寫片段的 `additional_required_sets`，`required_set` 也由 gate.sh 寫死，只有手改 report 才會觸發。
  - 修法：收集 `carried` 時，對 ALLOWED_MISSING 且狀態為 EVIDENCE_MISSING 的 section，只計 expect 設定的 `additional_required_sets[name]`，不計片段自帶的 `required_set`／`additional_required_sets`。這樣 OD-55 日後把 `s21_gate:cap_windows_com.windows` 經 expect 設定交給該 section 時，BO-S21-01 豁免仍然成立。另補上述兩案的測試。

- **P5-b9ae1d2-3 [Low]** `tests/test_slice21_wal_s1w_import.py:104`
  - 問題：搬走 `test_wal_s1w_example_records_source_tree_dirty` 後，檔尾多留一個空行；`git diff --check 86b54d8 b9ae1d2` 回報 `new blank line at EOF`。
  - 修法：刪掉該空行。

**驗證紀錄**（測試以非 root uid 1000 執行；Python 3.12.3、pytest 9.2.0；審查 worktree 最後 `git status --porcelain` 為空）
- 本輪三支測試檔：61 passed。
- Python 子集共 343 passed、3 failed、1 skipped。涵蓋：
  - `tests/test_slice21_*.py` 全部（含 harness_shared_tty、case_verdicts、gate_report）
  - `test_l1_judge.py`、`test_resource_markers.py`、`test_contracts_catalog.py`、`test_event_latency_measure.py`
- 3 支失敗都在 `test_l1_judge.py`，皆為前輪已知原因，本輪未改 lock：
  - s0_bc 的 pytest 版本相依參數化 id（SPEC-GAP #3）。
  - `s21_core.windows` 的能力列待 Windows 原生收集（OD-55 LOCAL-WINDOWS-TODO）。
- 紅燈先行：以 `git archive b9ae1d2` 覆蓋 `86b54d8` 版的 judge／expect 實跑，結果見 A 節的兩項核對。
  - 非 git 副本中，`test_slice21_wal_s1w_import.py` 另有 4 支依賴 ROOT git 歷史的測試失敗；`b9ae1d2` 原樣副本也有同樣 4 支失敗，屬副本環境因素。
- policy_check（pinned `2dcf2f8`）：24 pass、1 fail、2 warn。
  - 2 warn 為 R-19、R-22。
  - 唯一 fail 是 R-16：`serialwrap session -h` 的 argparse 欄寬為 24，README 標記內為 22。
  - `86b54d8` 在同一環境的 R-16 失敗完全相同；本輪也沒有 CLI／README 變更，屬環境因素（前輪環境為 pass），非本輪引入。
- 其他檢查：
  - `git diff --check 86b54d8 b9ae1d2`：只有上述 EOF 空行。
  - changelog fragment 有 frontmatter。

## 既有碼待辦（不計入結論）

1. 前輪既有碼待辦 1～3 本輪未觸及，仍然存在：
   - `crates/sw-pal/src/conformance/mod.rs` 的 `posix_pty_pair()` 沒有設 `FD_CLOEXEC`，`s21_gate:cap_posix_pty` 會間歇紅燈。
   - `scripts/rust/cargo_l1_results.py` 失敗時不轉印 libtest 的 `failures:` 段。
   - `crates/sw-cli/tests/emitted_codes.rs` 寫入 repo 的 `target/`。
2. 編排者裁決中列為「P5 第 7 輪」的項目都不在本範圍內：
   - OD-64：drain_receipt 界限。
   - OD-66：WSL2 的 wal_s1w 門檻，以及同時段 Python 基線匯入。
   - SPEC-GAP #1：`s21_gate:py_parity_full`。
   - SPEC-GAP #2：四個能力集合補 section。
   - SPEC-GAP #3：明確 `ids=`。
   - `test_two_daemons_same_tty` harness 修正。

   因此 gate 現況仍會因四個能力集合未承擔而判 INCOMPLETE，這是正確的。OD-66 落地時，`test_slice21_wal_s1w_import_git.py` 的 `_doc()`（wsl2 證據帶 linux 門檻、無 Python 基線，預期 PASS）需要同步調整。

## 對裁決的異議（不計入結論）

無。

結論：需修正
