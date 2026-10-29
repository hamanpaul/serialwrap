> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

## A 前輪 finding 核對

本輪 `f9e90a9..de7a95a` 共三個 commit（`efb8580`、`6df20d2`、`de7a95a`），只改 `scripts/rust/p7_case_coverage.py`、`scripts/rust/p7_gate.sh`、`tests/test_p7_ab_report.py`、`tests/test_p7_gate_win_argv.py`。production code、情境 YAML、L1 鎖與 P6 檔案都沒有變動。下表的「已修」是前輪核對的結果，本輪確認修正仍在。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | 本輪未動 production code，修正保留。 |
| 2 | Medium | 已修 `f150a24` | 0 | 同上。 |
| 3 | Medium | 已修 `f150a24` | 0 | 同上。 |
| 4 | Medium | 已修 `f150a24` | 0 | 同上。 |
| P7-71fe017-1 | Medium | 已修 `030d294` | 0 | 同上。 |
| P7-71fe017-2 | High | 已修 `7e2da7a` | 0 | 同上。 |
| P7-71fe017-3 | High | 已修 `8134806` | 0 | P5 修正合入後的狀態保留（rulings/p7.md 移交 P5，由 P7 merge P5 後核對）。 |
| P7-71fe017-4 | Medium | 已修 `afebff0` | 0 | health 相容掃描與 rollback 保守掃描仍分開。 |
| P7-27b5bf4-1 | High | 已修 `20c9454` | 0 | 同上。 |
| P7-030d294-1 | High | 已修 `1677489` | 0 | 同上。 |
| P7-9065f14-1 | Medium | 未修 | 5 | `tests/acceptance/slice22_evidence_map.yaml:47` 仍有 `l15-delayed-reply: {status: PENDING}`。修正回覆標為 NEEDS-RULING，rulings/p7.md 至今沒有對應裁決。 |
| P7-c5f0fc9-1 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-2 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-3 | Medium | 已修 `f9e90a9` | 0 | `6df20d2` 改成快照 artifact 後，`init --artifact` 仍登記 CLI、daemon 兩個 artifact。 |
| P7-c5f0fc9-4 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-5 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-c5f0fc9-6 | Medium | 已修 `29aaa7b` | 0 | 同上。 |
| P7-7bb9cfd-1 | Medium | 未修 | 3 | `p7_ab_report.py` 本輪未變更。外部 run 的表頭仍缺可還原的基準路徑。gate 流程的 run-dir 在 repo 內，所以 gate 自己不受影響。 |
| P7-cd7acf0-1 | High | 已修 `f9e90a9` | 0 | 新增的 cargo 分支要求 `cargo:<pkg>::<target>::<fn>` 在實際報告中恰好一列 passed，缺列或重複都算缺漏，沒有放寬。 |
| P7-cd7acf0-2 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-3 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-4 | Medium | 未修 | 2 | L1 鎖的 `p7_gate:*` 集合仍沒有 `p7_gate:contract-blackbox-slice22-b`。 |
| P7-cd7acf0-5 | Medium | 已修 `f9e90a9` | 0 | `WIN_*_ARGS` 本輪未變更。 |
| P7-cd7acf0-6 | Medium | 已修 `f9e90a9` | 0 | 同上。 |
| P7-f9e90a9-1 | High | 未修 | 1 | `p7_gate:python3-m-pytest-q-tests` 仍只凍結 49 個節點。本輪新增的三個回歸 `test_gate_func_comparison_lookup_finds_nested_report`、`test_gate_snapshots_artifacts_before_cargo_rebuilds`、`test_coverage_crate_sourced_case_uses_cargo_l1` 也沒有入鎖。`tests/test_p7_gate_win_argv.py` 整個檔案沒有任何節點被凍結，日後刪掉也不會被 gate 察覺。 |
| P7-f9e90a9-2 | Medium | 未修 | 1 | 以 de7a95a 執行 collect-only 核對：鎖內 `test_regression_phase_status[None-0-INCOMPLETE]`、`[rows0-0-PASS]`、`[rows1-1-FAIL]`、`[rows2-0-INCOMPLETE]`、`[rows3-0-INCOMPLETE]`、`[rows4-2-INCOMPLETE]` 六個 nodeid 仍不存在。 |
| P7-f9e90a9-3 | High | 未修 | 1 | `func-test/scenarios/s23-quiet-mid-login-zero-tx.yaml` 本輪未變更。 |
| P7-f9e90a9-4 | High | 未修 | 1 | `func-test/scenarios/s23-interleave-login-agent-human.yaml` 本輪未變更。 |
| P7-f9e90a9-5 | High | 未修 | 1 | `func-test/scenarios/s23-recover-force-handoff.yaml` 本輪未變更。 |

## B 新發現

本輪三個 commit 沒有引入 High／Medium。三支新回歸在未修正的程式上都會轉紅：把 de7a95a 的兩支測試檔疊到 f9e90a9 樹上執行，結果是 3 failed。以下為 Low：

**P7-de7a95a-1 [Low]** [scripts/rust/p7_case_coverage.py:109](scripts/rust/p7_case_coverage.py:109) — cargo 分支放在 `suite == "regression"` 的 family 對應之前。日後若有 F9／F10／F11 回歸案例（需實機）的 `source_ref` 指向 `crates/…/tests/*.rs`，該案會由 mock 單元測試判成 PASS，而不是 B-02／B-04／B-05 BLOCKED-OWNER。另外 docstring 第 18 行寫「windows-native 除外」，實際上 realhw 也被排除。目前 `slice_2.3` 共 8 個 crates/ 來源案例：4 個 contract-blackbox（layer L1）、4 個 windows-native，沒有現行失敗情境。修法：cargo 分支限定 `layer == "L1"`，或排除 `suite == "regression"`；docstring 改成「windows-native／realhw 除外」。

**P7-de7a95a-2 [Low]** [scripts/rust/p7_case_coverage.py:154](scripts/rust/p7_case_coverage.py:154) — `_cargo_results` 不檢查 `format == "l1-results.v1"`、`finished`、`exit_code`。cargo 報告中途中止或判為 `finished:false` 時，coverage 仍會逐案給 PASS。整體 gate 會被 cargo 列擋下（INCOMPLETE／FAIL），因此只是 coverage 列的明細偏樂觀。修法：`format` 不符或 `finished is not True` 時回 `None`，讓 crates/ 來源案例都記成「報告不完整」的缺漏。

**P7-de7a95a-3 [Low]** [tests/test_p7_gate_win_argv.py:55](tests/test_p7_gate_win_argv.py:55) — 測試在快照之後只斷言不再出現 `target/debug/serialwrapd`。如果快照後又把 `RUST_CLI` 改回 `$ROOT/target/debug/serialwrap`，這支測試抓不到。修法：改成斷言 `"target/debug/serialwrap" not in text[snap + 60:]`；此前綴同時涵蓋 CLI 與 daemon。

## 既有碼待辦（不計入結論）

- `scripts/rust/p7_gate.sh:95-98`：快照來源寫死 `$ROOT/target/debug/`，沒有遵從 `CARGO_TARGET_DIR`。外層設了 `CARGO_TARGET_DIR` 時，`cargo build` 會輸出到別處，`cp -p` 卻複製到 repo 內較舊的同名執行檔（或在 `set -e` 下無訊息中止）。md 登記的會是這個舊快照的 sha256，verify 照樣通過。改版前 `RUST_CLI="$ROOT/target/debug/serialwrap"` 也有同樣問題，不是本輪引入。建議改從 `cargo build --message-format=json` 的 `executable` 欄取路徑，或以 `${CARGO_TARGET_DIR:-$ROOT/target}` 為基準。

## 對裁決的異議（不計入結論）

無。

## 驗證

- 測試以一般使用者 `rev`、Python 3.12 venv（PyYAML 6.0.3、pytest 9.2.0、jsonschema）執行：`tests/test_p7_gate_win_argv.py`、`tests/test_p7_ab_report.py`、`tests/test_p7_gate_report.py` 共 43 passed；`tests/test_p7_*.py` 共 59 passed。
- 回歸紅綠：把 de7a95a 的兩支測試檔疊到 f9e90a9 的 `git archive` 樹上，新增的三個測試全部 FAILED。
- coverage 端到端：用 Rust 1.96.1、`CARGO_TARGET_DIR=/tmp/solrev-p7-target` 執行 `cargo_l1_results.py -- -p sw-core --features test-fakes --test deferred_bc02 --test out_ring --test event_store`，結果 finished、exit 0、55 passed，nodeid 與 `_cargo_nodeid` 的推導一致。以這份報告跑 coverage，`bc02-deferred-overflow-clear`、`df18-event-replace-overwrite`、`sc15-console-ring-drop-oldest` 為 PASS。`bc02-mailbox-stale-input-not-replayed` 因本分支沒有 `session_core_console.rs` 而記為缺漏，與 commit 記述的 44→47 相符。
- DF-18 的 Python 側（`tests/test_event_replace_df18.py` 兩節點）已凍結在 `p7_gate:py_parity_df18` 與全套 pytest 集合，cargo 節點也凍結在 `p7_gate:cargo-test-workspace`。coverage 只看 cargo 結果，不會讓缺了 Python 側證據的 run 通過 gate。
- 快照：`run_p7_ab.sh` 的各階段（bb_harness、func-test daemon_harness、l15 兩支測試）都讀 `SERIALWRAP_DAEMON_EXE`／`SERIALWRAP_CLI_EXE`／`SERIALWRAP_L15_RUST_DAEMON`，所以快照能傳到每個階段。CLI 與 daemon 放在同一目錄，`current_exe` 同層定位仍然成立。test-fakes 只提供 in-process hook，daemon 不讀測試 env。
- func-test：runner 的 `run_dir` 是 `report/<case>/<mode>/<run_index>`，兩案 `repeat` 皆為 1，只會產生一份 comparison.json。gate 的 `find | sort | head` 與 `judge_func` 的 `sorted(rglob)` 會取到同一份檔案。
- `bash -n scripts/rust/p7_gate.sh` 通過；`regen_lock.py --check` OK；本範圍 `git diff --check` 乾淨。
- policy check（Python 3.12 與 3.13 皆同）：24 pass／1 fail／2 warn。fail 是 R-16：`serialwrap session --help` 的欄寬與 README marker 不符。本輪沒有改 CLI 或文件，與本範圍無關；前輪報告為 25 pass。
- 未執行：全套 pytest、全 workspace cargo test、原生 Windows、`p7_gate.sh` 全流程。審查 worktree 未修改，HEAD `de7a95a`，工作樹乾淨。

結論：需修正
