---
type: fix
scope: rust-refactor
---

收斂 Stage 1 出口 gate 的 cargo 證據綁定與 junit 計數（G2-cargo-evidence-binding-junit-suite-counts），各項皆先以會失敗的測試界定再修正：

- (1) cargo 執行檔綁定（sol 58e539d#5、3f05797#1）。`cargo_l1_results.py` 於建置後、任何測試執行前記錄每個測試執行檔的 path 與 sha256，執行結束後重新核對（改寫或移除即 `finished: false`），並在報告頂層寫入 `provenance`（`cargo-l1-provenance.v1`）：受測 git HEAD 與工作樹乾淨狀態（開始與結束各一次）、`rustc -vV` 逐字輸出與摘要、RUSTFLAGS／CARGO_ENCODED_RUSTFLAGS 的設定狀態與 sha256（旗標文字不落盤），以及每個執行檔所含的 nodeid。Cargo 固定在收集器所在 repo 執行，並拒絕指向其他 workspace 的 `--manifest-path`，避免把外部產物配上本 repo 的 provenance。`register_p4_evidence.py` 移除登錄時的 `cargo test --no-run` 重建與以 `--list` 推定身分的路徑，改為只核對報告內的紀錄：缺 provenance、HEAD 或工作樹狀態不符、缺 rustc 紀錄、執行檔未唯一對應、缺 sha256，或磁碟上檔案的 sha256 與報告不符（改寫或重建後未重跑）一律 FAIL 並要求重跑。Windows 仍由 `win-test.sh` 呼叫同一收集器，`win-stage-lib.sh` 未修改。
- (2) junit suite 宣告值（sol 11b1fd4）。P2 `tests/acceptance/check_evidence_index.py::_junit_counts` 同時讀取 testsuite 宣告的 failures／errors；宣告值大於 testcase 層計數即拋出 ValueError（fail-closed）。`check_p4_cases.check_l1` 將其判為 INVALID；pytest 集合的 junit 缺席或無法解析改判 INSUFFICIENT（原先會略過交叉核對）。`register_p4_evidence.py` 的 junit 列表與計數同樣 fail-closed。只拒收「宣告大於實際」方向，宣告值小於 testcase 層者不在此拒收（testcase 層失敗仍由 l1_judge 判 FAIL）。
- (3) 測試可攜性（sol 3f05797#3）。`tests/test_register_p4_evidence.py` 不再使用 shebang 假 cargo、假 win-runner 與文字 `.exe` 替身（登錄器已不執行任何測試執行檔）；每個測試改用自備的乾淨 git repo 作為受測 repo，使登錄結果不受開發中工作樹的變更影響。
- (4) manifest 指標邊界（58e539d#4 殘留）。`current-release.json` 的 `dir` 含 `..`、絕對路徑、空段、`.` 或磁碟代號即拒收，與 Rust launcher 的 `current_release_manifest_path` 同一規則；原「絕對路徑照用」的測試案例已依此改為拒收。

未修改凍結鎖（`l1_required.lock.json`、`p4_required.lock.json`）、selector、l1_judge 判定規則與 `win-stage-lib.sh`；未刪除或弱化既有斷言。plan 04／05 的對應文字由 G10 處理，addenda 列由 G8 處理，本次只在 commit 與本段說明。既有未含 provenance 的 cargo L1 報告將被拒收，須以新收集器重跑。
