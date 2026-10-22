---
type: change
scope: rust-cli
---

Stage 1 出口 gate 的會計與來源修正（S1X）：擁有者機台相依的 cargo 列（`cap_*` 與 `windows_switch_running_exe_rename`）不論是否在凍結鎖內，未執行時一律列為 BLOCKED-OWNER，不再靜默消失；pytest JUnit 必須帶 HEAD、`SW_REQUIRE_CARGO=1`、`git_clean=1`（工作樹含未追蹤檔乾淨，排除 `target/`）、以 pytest 實際生效設定（`effective`：keyword、markexpr、deselect、ignore、maxfail、collectonly、lf、ff，已涵蓋 `addopts`、`PYTEST_ADDOPTS` 與合併短旗標）判定未縮小範圍，以及涵蓋 `tests/` 的 argv provenance（`scripts/rust/junit_provenance.py`）；spike3 證據須 `artifact.git_clean=true`（`spike3_win_wal.py` 產出，schema 新增 `git_head`／`git_clean`）；spike3 Windows WAL 證據（`win-wal-av-*`）依 schema 與 git_head 讀入，EDR 未知不得 PASS。`func-test/runner.py` 的 `compare: none` 以斷言步驟全數 PASS 定義 gate_eligible，平台不適用的 step（`platforms`）記為 not_applicable 且不計 PASS；結果輸出結構化 `not_applicable_steps`、`na_only_gap`，gate 僅將「缺口純為 n/a」的列排除於阻擋之外，PASS 須由其他平台列 PASS＋eligible 提供；INSUFFICIENT／SKIP 對映 evidence-insufficient，不再記為 FAIL。`capture_cli_help.py --out` 不再改寫受版控的 `contracts/cli_help/_meta.json`。計畫 04／05／03 的 L1.5 指令改為 `--mode bridge`（提議待 OD-S1X-03），docker Rust-CLI L1.5 宣告為 BLOCKED-PREREQ。
