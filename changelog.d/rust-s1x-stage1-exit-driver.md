---
type: change
scope: rust-cli
---

Stage 1 出口驅動腳本與計畫命令修正（S1X G2）：新增 `scripts/rust/stage1_exit_driver.sh` 作為 Stage 1A＋1B 一併出口的可重現入口。自測報告改以 `tools/uart_stdio_bridge/APPROVED_REVISION.json` 的核准 tree（c4c22c0e）挑選（`selftest-linux-20261022`、`selftest-windows-20261022`），找不到即非 0 結束，不再退回 tree 9eaed6de 的舊報告；`SW_BUNDLE` 明確 export；完整 pytest 改為唯一一次，並於 1B 證據登記與 `check_p4_cases` 之後執行（`SW_RESOURCE_REPORT` 寫入 `l1/linux/pytest-full/`，供 gate 第 7 列）；register 帶入 Windows CLI 與 junit；Windows L1.5 改接 G3 wrapper `win-func-test.sh`（帶 `--bundle <OutDir>/bundle` 與 `--selftest-report`，不帶 `--case`）、spike3 改接 `win-spike3.sh`（`--evidence-dir <run>`，單次執行四案，證據寫入 `<run>/spike3/<case>.json`），gate 帶 `--l15-report windows=` 與 `--spike3-evidence`；未設 `SW_EDR_*` 時 spike3 為 evidence-insufficient（整體 FAIL），非 BLOCKED-OWNER，驅動啟動時警示；既有 `target/p4-evidence` 與 `p4-bench-*.json` 於開頭整個搬到證據目錄（搬不刪），並以 mtime 檢查各報告產生於本次開跑之後（`cp -p` 保留來源時間）；報告路徑一律取工具輸出的 run_id，不以 `ls -t` 猜測；補跑 plan 05 Step 4 的兩步 bench（隔離 throwaway serialwrapd 與 bb_stubd）；移除重複的 `pytest_final` 與同一 HEAD 的重複 cargo 測試。plan 04 Task 19 Step 5 與 plan 05 Task 16 Step 4 已附 S1X 修訂說明。
