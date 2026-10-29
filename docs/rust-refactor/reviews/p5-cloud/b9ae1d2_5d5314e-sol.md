> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p5 增量審查 `b9ae1d2..5d5314e`

範圍：三個線性 commit，沒有 merge commit。
- `67b7e264` fix(sw-core,gate)：OD-64 drain_receipt 界限。涵蓋 mailbox／actor 探針、`contracts/resource_limits.yaml`、lock add-only 7 列、`drain_release_e2e`／`mailbox` 測試、`test_slice21_scope.py`。
- `335c9587` docs(evidence)：補存 M1～M7 變異原始輸出。
- `5d5314eb` feat(gate)：OD-66 wal_s1w_perf 依主機類型選門檻。涵蓋 judge、expect、`slice21_gate.sh`、測試，並匯入 86b54d8 的 WSL2 證據。

產品路徑：`mailbox.rs`／`actor.rs` 的改動全部在 `#[cfg(feature = "test-fakes")]` 內。非 test-fakes 建置的排空順序與行為不變（D,D,D,L,W 維持）。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `25ad390` | 2 | 本輪未觸及。 |
| 2 | High | 已修 `25ad390` | 2 | 未改。 |
| 3 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 4 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 5 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 6 | High | 已修 `25ad390` | 2 | 未改。 |
| 7 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 8 | Medium | 已修 `25ad390` | 2 | 本輪 judge 只改 wal_s1w 段，`build_expect()` 未動。 |
| P5-1393573-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-1393573-2 | Medium | 已修 `d8ac988` | 2 | 未改。 |
| P5-5cc4623-1 | High | 已修 `0a7d0929` | 3 | 未改。 |
| P5-5cc4623-2 | Medium | 依裁決不處理 | 3 | 延後裁決仍適用。 |
| P5-70f6ce7-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-3c4e051-1 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3c4e051-2 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3084388-1 | High | 已修 `2472f78` | 3 | `tests/test_event_latency_measure.py` 通過。 |
| P5-3084388-2 | Medium | 已修 `289f7573` | 4 | 本輪改了 `contracts/resource_limits.yaml`，契約 sha256 隨之改變；既有 event_latency 證據（r6）的 `contract_sha256` 因此不再相符，gate 須重新量測。這正是該修正的設計，非回歸。量測工具未改。 |
| P5-3084388-3 | Medium | 已修 `464adec` | 1 | R-21 本輪 pass。本輪新增證據 `host.txt` 又帶入本機絕對路徑，但 R-21 未命中，另列 P5-5d5314e-3（Low）。 |
| P5-2472f78-1 | Medium | 已修 `c4cfb96` | 1 | 未改。 |
| P5-4f8acdf-1 | High | 已修 `86b54d8` | 1 | 承擔計算未改，`test_slice21_gate_judge.py` 全過。gate 仍因四個能力集合未承擔判 INCOMPLETE。 |
| P5-4f8acdf-2 | Medium | 已修 `86b54d8` | 1 | `check()` 仍要求 `source_tree_dirty is False`，相關測試通過。 |
| P5-4f8acdf-3 | Medium | 已修 `86b54d8` | 1 | 本輪未動 sw-pal。 |
| P5-4f8acdf-4 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-4f8acdf-5 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-6 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-7 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-86b54d8-1 | Medium | 已修 `b9ae1d2` | 1 | 未改。 |
| P5-86b54d8-2 | Medium | 已修 `b9ae1d2` | 1 | sw-proto 仍在 `relevant_paths`。`test_slice21_wal_s1w_import_git.py` 夾具改為 native 主機，但仍斷言改 sw-proto 後得 EVIDENCE_MISSING；通過。 |
| P5-86b54d8-3 | Low | 依裁決不處理 | 1 | low-backlog。本輪新增的 pytest 節點同樣未登記 `s21_gate:pytest_full`：`test_slice21_wal_s1w_import.py` 新增的 OD-66 各案、`test_slice21_scope.py::test_event_latency_od64_od65_records`。 |
| P5-86b54d8-4 | Low | 依裁決不處理 | 1 | 未改。 |
| P5-86b54d8-5 | Low | 依裁決不處理 | 1 | 未改。 |
| P5-b9ae1d2-1 | Medium | 未修 | 1 | `wal_s1w.relevant_paths` 本輪未改，只新增 `host_thresholds`；仍缺 `crates/sw-pal/src/clock.rs`、`crates/sw-pal/src/clock`、`crates/sw-pal/src/lib.rs`、`crates/sw-pal/src/traits.rs`。`p5-fix-responses.md` 沒有對應條目。`git diff --stat 86b54d8 5d5314e -- crates/sw-pal` 為空，所以本輪匯入的 86b54d8 證據目前不受影響，但缺口仍在，修法同前輪。 |
| P5-b9ae1d2-2 | Low | 依裁決不處理 | 1 | 承擔收集碼未改，依收斂規則交 low-backlog。 |
| P5-b9ae1d2-3 | Low | 已修 `5d5314e` | 1 | 檔尾空行已被新段落吸收，`git diff --check b9ae1d2 5d5314e` 乾淨。 |

## B 新發現

- **P5-5d5314e-1 [High]** `scripts/rust/slice21_gate_judge.py:391-403`（`_wal_s1w_judge_numbers`）、`:474-485`（`judge_one`）、`:518`（本機路徑）
  - 問題：OD-66 改成「以證據數值重算、不採自報 verdict」，但重算只比 4 個延遲數值，加上 WSL2 的 Python 基線比較。`wal_s1w` 自報 FAIL 的另外兩個條件因此被丟掉：
    - `write_failures > 0`（`crates/sw-core/examples/wal_s1w.rs:539`）。
    - 「至少一流未跨越 rotate」（規格 §1.1.3 要求每流至少一次；`wal_s1w.rs:536`）。
  - `check()` 也不看這兩欄。`b9ae1d2` 版以自報 verdict 判定，這兩種情況都得 FAIL；`5d5314e` 版得 PASS。`--import` 與 `--local` 兩條路徑都受影響。
  - 失敗的 append 仍計入一般樣本（`wal_s1w.rs:466-476`）。所以像「WalWriter 每筆立即回 `ok:false`、從不 rotate」這類回歸，延遲數字反而更漂亮，會直接 PASS。
  - 結果：FAIL 被轉成 PASS、gate 判準被放寬；而 write_failures 代表 WAL 寫入失敗，也就是資料遺失。
  - 失敗情境（實跑，uid 1000）：
    - 設定：以 `p5-evidence/od61-wal-s1w-wsl2-86b54d8/wal_s1w-1.json` 為底，`env.git_revision` 改為 `5d5314eb…` 全 SHA。Python 基線用同目錄檔。`formal_params`／`relevant_paths`／`host_thresholds` 取自 5d5314e 的 expect，`host_label=owner-wsl2-ext4-vhdx`。
    - 舊版 judge 以 `git show b9ae1d2:scripts/rust/slice21_gate_judge.py` 另存，`ROOT` 指向審查 worktree。

    | 對證據的修改（皆自報 `verdict: FAIL`） | b9ae1d2 judge | 5d5314e judge | 預期 |
    |---|---|---|---|
    | 無（對照） | FAIL（§1.2） | PASS（OD-66） | PASS（OD-66） |
    | `write_failures=3`、`failures=["write_failures=3"]` | FAIL | **PASS** | FAIL |
    | `rotations_per_stream=[1,1,0,1]` | FAIL | **PASS** | FAIL |
    | `virtualization=native`、append 0.004／0.009／0.019 ms、rotate 2.5 ms、`write_failures=7` | FAIL | **PASS** | FAIL |
    | 40000 筆全失敗：native、append 0.001／0.002／0.003 ms、`rotate_max_ms=0`、`rotations_per_stream=[0,0,0,0]`、`write_failures=40000` | FAIL | **PASS** | FAIL |
    | 同上，但改 `wsl2` 並附基線 | FAIL | **PASS** | FAIL |

  - 確切修法：
    - 在 `_wal_s1w_judge_numbers`（或 `judge_one` 重算前）以數值補回兩個非門檻條件：
      - `write_failures`：須為非 bool 整數；缺欄或型別錯 → EVIDENCE_MISSING；> 0 → FAIL（`write_failures=N`）。
      - `rotations_per_stream`：須為長度等於 `params.streams` 的整數串列；缺欄或長度不符 → EVIDENCE_MISSING；任一為 0 → FAIL（§1.1.3）。
      - docstring／expect 註解改寫為：「不採自報 verdict」只限延遲門檻。
    - 測試：
      - 兩支 `_doc` 夾具補 `write_failures: 0`、`rotations_per_stream: [1, 1, 1, 1]`：`tests/test_slice21_wal_s1w_import.py:31` 與 `tests/test_slice21_wal_s1w_import_git.py`。
      - native 與 wsl2（附基線）各新增：`write_failures=3` → FAIL、`rotations_per_stream=[1,1,0,1]` → FAIL、缺 `write_failures` → EVIDENCE_MISSING。
      - 紅燈先行：新增案在 5d5314e judge 下得 PASS。

- **P5-5d5314e-2 [Low]** `crates/sw-core/tests/drain_release_e2e.rs:493`
  - 問題：`assert_eq!(handle.probe.entries("Control").len(), 1, "OpFinished 應先於 receipt 被處理…")` 只在看到 WorkerStopped 紀錄之後計數 Control，並不驗兩者的先後。actor 在 W 之後才處理 L 時，同樣通過。
  - 情境：在 scratch 副本套用變異 M9。M9 讓 `stopped` 分支排空 data 後先回 receipt、再回同 epoch 依賴事件，順序變成 D,D,D,W,L。
    - `drain_receipt_dependency_wait_on_{release,shutdown}_within_od64_bound` 3/3 綠。
    - 順序實際由 mailbox 層測試抓到（例如 `stopped_receipt_records_pre_dependency_data`，已在 l1_gate），但方式是卡住：`recv_next(..., deadline=None)` 等不到下一項，測試 hang 而不是 FAIL。
  - 判 Low 的理由：drain_receipt 的 l1_gate 整體不會靜默通過。
  - 修法：
    - 讓 `ProbeEntry` 帶全域序號（或提供依 log 順序的列舉），斷言 OpFinished 序號小於 WorkerStopped 序號。
    - 本輪新增的 mailbox 測試，`recv_next` 改給有限 deadline，或先斷言回傳非 `None`，使順序回歸得 FAIL 而不是 hang。

- **P5-5d5314e-3 [Low]** `docs/rust-refactor/reviews/p5-evidence/od61-wal-s1w-wsl2-86b54d8/host.txt:7`
  - 問題：含本機絕對路徑 `/tmp/claude-1000/-home-paul-chen-prj-pri-serialwrap/…/target/release/examples/wal_s1w`，路徑中帶使用者帳號名。policy_check R-21 現行判 pass，但這與 P5-3084388-3 的原則（證據檔不留本機絕對路徑）不一致。
  - 修法：改成相對路徑 `target/release/examples/wal_s1w`，保留 sha256。

- **P5-5d5314e-4 [Low]** `scripts/rust/slice21_gate_judge.py`（`python_baseline_problems`）
  - 問題：OD-66 要求基線為「同主機同時段」，judge 只核對主機與工作量。兩個工具的輸出都沒有時間戳，所以同主機其他時段的基線也會被接受。同主機的 Python rotate 隨時段變動很大：前次 108～132 ms，本次 884～902 ms。
  - 情境（構造）：Rust rotate 60 ms、同時段 Python 50 ms，應 FAIL；若改匯入另一時段 902 ms 的基線，得 PASS。目前只能靠 `host.txt` 的程序紀錄把關。
  - 判 Low 的理由：需人工誤配；固定門檻 ≤100 ms 仍壓住上界。
  - 修法：`wal_s1w` 與 `wal_microbench.py` 輸出量測起訖時間（或由 gate 帶入 `host.txt` 的逐輪起訖）。judge 要求基線與各輪的時間差在設定視窗內。

- **P5-5d5314e-5 [Low]** `crates/sw-core/src/actor.rs:147`
  - 問題：註解寫 `at_most_pre_dependency_data_plus_one`，契約識別字是 `at_most_pre_dependency_data_plus_one_after_receipt`。
  - 修法：註解改用完整識別字。

## 驗證紀錄

- 測試以非 root（uid 1000）執行；Rust 依 `rust-toolchain.toml`（1.96.1）、Python 3.12.3、pytest 9.2.0、`CARGO_TARGET_DIR=/tmp/solrev-p5-target`。審查 worktree 最後 `git status --porcelain` 為空。
- `cargo test --locked -p sw-core --features test-fakes`：16 個測試 binary 全過，557 passed、0 failed。其中 `drain_release_e2e` 12、`mailbox` 22。
- 變異（在 `git archive 5d5314e` 的 scratch 副本中執行，未動審查 worktree）：
  - 對照組全綠。
  - M7（N_pre 不分 epoch）：兩支依賴 gate 紅，與提交的證據一致。
  - M8（自訂：`remaining = usize::MAX`，等待後才到的 data 也算入 N_pre）：`mailbox::pre_dependency_data_excludes_later_and_other_epoch_data` 紅。
  - M9：見 P5-5d5314e-2。
- Python 子集共 313 passed、3 failed、1 skipped。
  - 涵蓋：`tests/test_slice21_*.py`、`test_l1_judge.py`、`test_resource_markers.py`、`test_contracts_catalog.py`、`test_event_latency_measure.py`，共 317 收集。
  - 本範圍沒有刪除任何測試，新增 17 個 test 定義。
  - 3 支失敗都在 `test_l1_judge.py`，原因與前輪相同：s0_bc 參數化 id（SPEC-GAP #3）、`s21_core.windows` 能力列（OD-55）。
- OD-66 判定器直接重判 86b54d8 三輪＋基線：PASS，與 F201 一致。但此 PASS 未檢查 write_failures 與 rotations（三輪證據本身皆為 0 與 [1,1,1,1]，因此結論不變）。
- policy_check（pinned `2dcf2f8`）：25 pass、0 fail、2 warn（R-19、R-22）。
- `git diff --check b9ae1d2 5d5314e`：乾淨。兩個 changelog fragment 皆有 frontmatter。lock 變更只有 add-only（`s21_core.linux` 加 7 列）。

## 既有碼待辦（不計入結論）

1. 前輪既有碼待辦 1～3 本輪未觸及，仍然存在：
   - `posix_pty_pair()` 未設 `FD_CLOEXEC`。
   - `cargo_l1_results.py` 失敗時不轉印 `failures:` 段。
   - `emitted_codes.rs` 寫入 repo 的 `target/`。
2. 編排者裁決中列為「P5 第 7 輪」的項目，本輪只落地 OD-64 與 OD-66。以下仍未做：
   - SPEC-GAP #1：`s21_gate:py_parity_full`。
   - SPEC-GAP #2：四個能力集合補 section。
   - SPEC-GAP #3：明確 `ids=`。
   - `test_two_daemons_same_tty` harness 修正。

   因此 gate 現況仍會因四個能力集合未承擔而判 INCOMPLETE，這是正確的。
3. `tests/acceptance/slice21_gate_expect.yaml:76` 的 `PA-OD-EVLAT` 仍寫「五事件 at_most_one_data_item_before_control」。這是 OD-45 的歷史紀錄，可加註 drain_receipt 已依 OD-64 改界限。

## 對裁決的異議（不計入結論）

無。

結論：需修正
