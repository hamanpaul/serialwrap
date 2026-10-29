> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

## A 前輪 finding 核對

已修及依裁決結案者保留前輪累計未修輪數。本輪變更未觸及 `sw_core/`、`crates/sw-daemon/src`、`crates/sw-cli/src`；Rust 產品碼只改 `sw-core` 的 test-fakes 探針（`actor.rs`／`mailbox.rs`）與 `sw-pal/src/wal_fs/posix.rs`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `25ad390` | 2 | 本輪未動 interactive TX 的 `cmd_id` 與失敗映射；s21_core.linux 實跑 1728 passed。 |
| 2 | High | 已修 `25ad390` | 2 | 開埠與持久化仍在 actor 外，本輪未改。 |
| 3 | Medium | 已修 `25ad390` | 2 | alias 同步後持久化未變。 |
| 4 | Medium | 已修 `25ad390` | 2 | 啟動存檔失敗退出路徑未變。 |
| 5 | Medium | 已修 `25ad390` | 2 | 裸 `--` 解析未變。 |
| 6 | High | 已修 `25ad390` | 2 | PROBE fence 的 WAL 健康通知註冊未變；`wal_fs/posix.rs` 只加回寫提示，不影響健康判定。 |
| 7 | Medium | 已修 `25ad390` | 2 | capture owner 精確比對未變。 |
| 8 | Medium | 已修 `25ad390` | 2 | `assemble`／`judge` 仍共用 `build_expect()`。 |
| P5-1393573-1 | High | 已修 `8134806` | 2 | 折行回顯修正未變。 |
| P5-1393573-2 | Medium | 已修 `d8ac988` | 2 | `tests/test_slice21_case_freeze.py` 通過。 |
| P5-5cc4623-1 | High | 已修 `0a7d0929` | 3 | 同步套用 delta 與重啟斷言未變。 |
| P5-5cc4623-2 | Medium | 依裁決不處理 | 3 | 延後裁決仍適用。 |
| P5-70f6ce7-1 | High | 已修 `8134806` | 2 | 依裁決採用的逐字元與插入距離語意未變。 |
| P5-3c4e051-1 | Medium | 已修 `cca5ba8` | 2 | wire TX 獨立計量未變。 |
| P5-3c4e051-2 | Medium | 已修 `cca5ba8` | 2 | 副作用遞迴檢查未變。 |
| P5-3084388-1 | High | 已修 `2472f78` | 3 | `_sample_cancel` 本輪未改；`tests/test_event_latency_measure.py` 通過。 |
| P5-3084388-2 | Medium | 已修 `289f7573` | 4 | 現行證據 `p5-evidence/event-latency-od45-rust-r6-261029.json` 帶 `artifact_source_commit`／`artifact_build_run_id`（`c2f68820`／`r6-261029`）。這是 gate 同 run 的建置，建置當下以 `--artifact-sha256` 綁定 daemon sha256 `b3e385f5…`。工具 sha256 `aa8e8bb1…` 與契約 sha256 `b23f9a1a…` 和 `c2f68820`、HEAD 一致。以現版工具重判：18 輪全 PASS、無作廢、無負載問題，唯一原因為未凍結（INSUFFICIENT_EVIDENCE）。`110bd585` 為過渡版，該檔已在 `289f7573` 移除。 |
| P5-3084388-3 | Medium | 已修 `464adec` | 1 | policy R-21 pass。 |
| P5-2472f78-1 | Medium | 已修 `c4cfb96` | 1 | `_sha256_file` 分塊實作本輪未改；相容性測試通過。 |

## B 新發現

- **P5-4f8acdf-1 [High]** `tests/acceptance/slice21_gate_expect.yaml:30-32`、`scripts/rust/slice21_gate.sh:194-200`（`7953f982`）
  - 問題：OD-55 准許把能力列自 `s21_core.linux` 移除，前提是移入 `s21_gate:cap_<cap>` 且「gate 以 `--caps <cap>` 實跑」。本輪只有 `s21_gate:cap_posix_pty` 接上 `linux_cap_posix_pty` section。`s21_gate:cap_{sys_admin,windows_native,interactive_desktop,supervisor_runtime}.linux` 共 5 列（`cap_sys_admin__tiocexcl_bypass_documented`、`cap_windows_native__launcher_gui_subsystem`、`cap_windows_native__launcher_parent_kill_kills_child`、`cap_interactive_desktop__exec_or_wait_ctrl_c`、`cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure`）沒有任何 section、BO 項或 runbook 步驟執行或判定。
  - 失敗情境：移出前，這 5 列在 `s21_core.linux` 是 required skipped，`l1_judge` 判 INSUFFICIENT，`linux_l1` 為 EVIDENCE_MISSING，gate 不得 READY。移出後，r6 的 `linux_l1` 已 PASS。其餘 section 全綠時，judge 會給 `READY_FOR_OWNER_EVIDENCE`，報告中沒有這 5 列的任何 EVIDENCE_MISSING 痕跡。`slice21-owner-runbook.md`、`slice21_bo.yaml` 也沒有對應步驟，Slice 出口可以在這 5 列從未執行的情況下放行。expect 註解自標的「SPEC-GAP」不是裁決。這是 gate 判準在裁決授權範圍外被放寬。
  - 確切修法（擇一並補測試）：
    1. 為每個 linux 能力集合新增 section：`cargo_l1_results.py --platform linux --caps <cap> -- --workspace --features test-fakes`，`required_set` 為 `s21_gate:cap_<cap>`。能力不可用時記 EVIDENCE_MISSING，以 `allowed_missing_bo` 綁到 BO 項，並在 runbook 補執行步驟。
    2. 在 judge 加檢查：lock 中有 `s21_gate:*` 集合未被任何 section（含 `additional_required_sets`）承擔時，判 EVIDENCE_MISSING。

- **P5-4f8acdf-2 [Medium]** `scripts/rust/slice21_gate_judge.py:345-398`（`wal_s1w_verdict`）、`tests/acceptance/slice21_gate_expect.yaml:19`（`relevant_paths`）、`crates/sw-core/examples/wal_s1w.rs:309`（`02bfebf4`）
  - 問題：OD-61 匯入證據的可追溯性只依 `env.git_revision`。`wal_s1w` 只取 `git rev-parse HEAD`，不記工作樹是否乾淨。`relevant_paths` 也缺 workspace `Cargo.toml`（`[profile.release]` 的 opt-level／lto／codegen-units）與 crate 的 `Cargo.toml`。
  - 失敗情境：本輪 `ba8bf0f4` 的 `wal-rotate-writeback-r6/rust-fix{1,2,3}.json` 就是在含未提交 sw-pal 修正的工作樹量得，`env.git_revision` 仍標 `289f7573`，也沒有 dirty 欄位。我取 `rust-fix1.json`，改 `git_revision=4f8acdfe…`、`virtualization=wsl2`、`verdict=PASS`，以現行 expect 的 `formal_params`／`relevant_paths` 呼叫 `wal_s1w_verdict`，得 `('PASS', ['匯入證據（owner-wsl2-ext4-vhdx）：全數達標'])`，預期為 EVIDENCE_MISSING。改 release profile 後，舊證據也照樣被採用。此做法與 event_latency 已建立的身分要求（`source_tree_dirty` 為 true 不得 PASS）不一致。
  - 確切修法：
    - `wal_s1w` 的 `env` 加 `source_tree_dirty`（`git status --porcelain`，至少涵蓋 `relevant_paths`）。
    - judge 對 dirty 為 true 或缺欄位判 EVIDENCE_MISSING。
    - `relevant_paths` 加 `Cargo.toml`、`crates/sw-core/Cargo.toml`、`crates/sw-pal/Cargo.toml`。
    - 補 dirty、缺欄位、Cargo.toml 變更三個案例。

- **P5-4f8acdf-3 [Medium]** `crates/sw-daemon/tests/daemon_process.rs:527`（`f7e00db3`）；相關 `crates/sw-pal/src/serial/posix.rs:341`
  - 問題：新回歸測試斷言 `的外部 holder（status=Permission）`，把與 Python 基線不同的 WARNING 文字鎖定。Python `sw_core/serial_port.py:403-407` 以 `report.status`（`"permission"`）格式化，凍結契約 `contracts/daemon_logging.yaml:45` `uart_holder_scan_unknown` 的 args 也是 `report.status`。Rust 則以 `{:?}` 印出 enum 名稱。
  - 失敗情境（實測，非 root，同一 FakeTarget 經 harness 起 daemon）：
    - Python stderr：`serialwrap: 無法確認 /dev/pts/4 的外部 holder（status=permission），BC-04 僅保證 serialwrap 實例互斥`
    - Rust stderr：`serialwrap: 無法確認 /dev/pts/4 的外部 holder（status=Permission），BC-04 僅保證 serialwrap 實例互斥`

    P6 SPEC-GAP #4 裁決要求此路徑與 Python 權限不足時的實際行為一致。本 commit 宣稱「Rust 現行開埠路徑已一致」，卻以測試把差異固定下來；日後依 Python 修正 Rust，這支測試反而會轉紅。偏差碼本身是既有碼，本輪的缺陷是對齊未完成且新測試鎖住偏差，因此標 Medium 而非 High。
  - 確切修法：`posix.rs:341` 改用與 `crates/sw-daemon/src/health.rs:21` 相同的 Python 狀態字串（`permission`／`unknown`／`unsupported`），測試改斷言 `（status=permission）`，並以 `daemon_logging.yaml` 模板做 golden 對照。

- **P5-4f8acdf-4 [Low]** `scripts/rust/slice21_gate_judge.py:422`（`aggregate_case_verdicts`，`d373eabe`）
  - 問題：OWNER-BATCH #8 列 blackbox A/B 為逐案證據來源，但彙整沒有採用計畫 06 `:4723` 的 blackbox 測試↔case 對照表。`test_f2_backpressure.py::test_queue_full_backpressure` 在 blackbox_python 為 FAIL，`f2-queue-full-backpressure` 卻記 EVIDENCE_MISSING，不是 FAIL。`case_uptake` 只計 PASS，不影響承接率。
  - 修法：依對照表把 blackbox_python／blackbox_rust 逐列納入最差判定。

- **P5-4f8acdf-5 [Low]** `crates/sw-core/tests/mailbox.rs:604`（`239e025b`）
  - 問題：見證數只有 `stopped` 路徑的正向測試。Release／CancelQueued／Shutdown 依賴的 control／safety lane 影子佇列沒有正向測試，`drain_release_e2e.rs:301` 的 `assert_within_one_data_item` 只檢查 entries 非空。影子佇列計數若失效（恆為 0），L1 latency gate 仍會是綠的。
  - 修法：補「control lane 事件排隊期間連取 N 個 data → `take_control_witness()==Some(N)`」的 mailbox 測試，safety lane 同樣補一支。

- **P5-4f8acdf-6 [Low]** `scripts/rust/slice21_gate.sh:179`（`99591b3e`）
  - 問題：`setpriv` 降權沿用 root 的 `RUSTUP_HOME=/root/.rustup` 與 PATH（`/root/.cargo/bin`；`/root` 權限 0700）。本容器以 root 執行同一命令（uid 1000）實測：`cargo_l1_results.py: cargo 不存在，證據不足`、rc 2，`linux_l1` 只能是 EVIDENCE_MISSING。判定方向正確，但在文件所述的雲端 root 環境中，降權路徑實際不可用。
  - 修法：降權時設定目標使用者的 `RUSTUP_HOME`／`CARGO_HOME`／`PATH` 與可寫的 `CARGO_TARGET_DIR`，或在 runbook 註明前置條件。

- **P5-4f8acdf-7 [Low]** `docs/rust-refactor/reviews/p5-evidence/s21-gate-set-registration-r6.md:237`（`c2f68820`）
  - 問題：`git diff --check` 報檔尾多一個空行。

### 驗證紀錄（非 root uid 1000、Rust 1.96.1、Python 3.12.3／pytest 9.2.0；審查 worktree 為唯讀）

- 本輪改動的 Python 測試共 10 檔（gate_judge、case_verdicts、wal_s1w_import、event_latency_measure、cargo_l1_results、probe_daemon_version、harness_shared_tty、slice21_scope、l1_judge、l15 `test_case_list_matches_freeze`），結果 126 passed、3 failed。失敗的 3 支都在 `test_l1_judge.py`，屬既有項：s0_bc 的 pytest 版本相依 id，以及 s21_core.windows 能力列待原生收集；與 r6 紀錄一致。`test_rust_win_scripts.py`（合併帶入）39 skipped；`test_slice21_case_freeze.py`、`test_functest_oracles.py` 通過。
- `cargo_l1_results.py --platform linux -- --workspace --features test-fakes -- --skip cap_`：1728 passed、1 failed、28 skipped，s21_core.linux 1729 列無缺。唯一失敗 `emitted_codes::extracted_emitted_codes_documented_and_written_to_target` 是因為寫入唯讀 worktree 的 `target/`，屬環境因素。本輪新增的 `witness_counts_data_overtaking_stopped_receipt`、`non_root_holder_scan_permission_does_not_block_open`、`writes_initiate_async_writeback_every_chunk` 與 l1_gate 兩支皆 passed。
- `--caps posix_pty -- -p sw-pal --features test-fakes`：356 passed、0 failed，`s21_gate:linux_cap_posix_pty` 336 列與 `s21_gate:cap_posix_pty` 19 列全數 passed，證實 `289f7573` 的修正生效。
- `test_two_daemons_same_tty`：Python 與 Rust release 皆 passed。Rust blackbox 全套 48 passed、1 skipped。新增的 harness 測試在 `c4cfb96` 快照下紅燈（TypeError）。
- `test_queue_full_backpressure`（Python）：在 `c4cfb96` 快照與 HEAD 皆 FAIL，非本輪引入。
- 凍結鎖：只有 `s21_core.linux` 有刪除，共 27 列，與 OD-55 列舉相符（能力列 24 筆移入能力集合，另 3 筆非能力列）。其餘為 add-only，`s21_gate:py_parity` 結構不變。新登記的 pytest 集合 nodeid 全在 collect-only 內，cargo 能力集合 nodeid 全在收集結果內。
- 兩個 merge commit（`16c78acf`、`5d7fb2f2`）的 `--cc` 為空，沒有衝突解法。
- 其他檢查：policy check 25 pass、0 fail、2 warn（R-19、R-22）；`check_cfg_boundary.py` rc 0；OD-58 的 `release_request` 250／300 ms 依裁決套用。

## 既有碼待辦（不計入結論）

- `tests/contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure`：Python 基線在 `c4cfb96` 與 `4f8acdf` 皆 FAIL（前景 `sleep 30` 期間送 300 筆未見 `SESSION_QUEUE_FULL`）。此 case 已登記為 `s21_gate:blackbox_python`／`pytest_full` 必要列，根因修正前 gate 會一直 FAIL；實作者已列為未結。
- `crates/sw-cli/tests/emitted_codes.rs:394-401` 把輸出寫進 repo 的 `target/`，在唯讀 checkout 下會失敗；建議改寫到 `CARGO_TARGET_DIR` 或暫存目錄。

## 對裁決的異議（不計入結論）

無。

結論：需修正
