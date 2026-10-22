---
type: fix
scope: rust-refactor
---

修正 Stage 1 P4 證據管線與 Windows 黑箱腳本的缺陷（G4-p4-evidence-pipeline-and-windows-bundle-pointer），各項皆先以紅燈測試界定再修正：

- (a) `win-blackbox.ps1` 的 `bin\current-release.json` 之 `dir` 回復為 plan 04 Task 17 的 `versions\<V>`（相對安裝根）。`realhw/deployed_content.install_dir` 的 Windows 分支與 `register_p4_evidence` 的 manifest 候選改以 `cli.parent.parent` 解析，反斜線先正規化；絕對路徑與 POSIX 行為不變。Rust launcher 對反斜線 `dir` 的拒絕規則與 plan 04 Task 17 的衝突另列 OPEN（PENDING-OD-S1X-WIN-DIR-SEP），未在本次裁定。
- (b) `pidns_rig` 子行程寫入的 pid 為 namespace 內部編號。`bb_daemon` 的 pidlog 列附 `pid_ns`，`bb_l15` 另記錄外層 unshare 的 host pid。`ab_blackbox_1b._release_check` 對不同 namespace 的列改以「host 上無行程屬於該 namespace」判定，仍有成員即 unreaped 並列出其 host pid；與 host 同 namespace 的列照原邏輯檢查，嚴格度不降低。**`bb_daemon.py` 與 `bb_l15.py` 為黑箱 oracle 基礎設施，待非實作者審查。**
- (c) `register_p4_evidence` 的 cargo 執行檔改由凍結 `s1b_cargo` selector 的 argv 執行 `cargo test --no-run --message-format=json`（Windows 加 `--target x86_64-pc-windows-gnu`）取得，依 package＋target 唯一對應；對應不到或不唯一仍 FAIL。取代先前以 glob 搜尋 `target/debug/deps` 的做法。
- (d)(e) 平台適用性共用規則（`check_p4_cases.platform_applicability`，`register`、`check`、`ab_blackbox_1b` 皆採用）：cargo 節點以是否在該平台 `s1b_cargo` 凍結鎖中判定；contract_blackbox pytest 節點以該平台 `s1b_20groups` 凍結 selector 的 markexpr 收集結果判定。不適用的（案例, 平台, 節點）組合不產生紀錄、不在該平台執行，並逐列列於 `summary.json` 與標準輸出的 `not_applicable`。兩平台皆不適用者一律 FAIL。**此規則縮小了必要證據的分母，須擁有者核准：見 CHG-S1X-P4-APPL-01（OPEN，共 14 組）。** Windows 巢狀 ab-suite 因此不再執行 `test_daemon_restart_ab`（依賴 /proc 與 sh，不在 Windows selector 中）。
- (d') Linux A/B 選取回復 HEAD 之負向標記過濾，`test_daemon_restart_ab` 仍在 Linux A/B 內（A/B 覆蓋不縮減，CHG-S1X-P4-LINUX-AB-01）。
- (f) `win-blackbox.ps1` 於 staging 刪除前把 `l1\windows\blackbox\ab-suite\` 複製回 OutDir；Windows dev bundle 匯出到 `<OutDir>\bundle\`（`bin\serialwrap.exe`、`bin\current-release.json`、`versions\<V>\release-manifest.json`，以及 manifest 的 `cli.path` 所指的 `versions\<V>\bin\serialwrap.exe`）。匯出失敗只記錄警告、不覆蓋原錯誤，且 scratch 一定刪除（巢狀 `finally`）；逐檔檢查來源存在，缺者略過並警告。`register_p4_evidence` 的 Rust CLI 以 `<OutDir>\bundle\bin\serialwrap.exe` 登錄時，Windows 穩定入口是 manifest 所指檔案的 byte 複本，故新增 `_windows_stable_entry_of`：僅限入口位於 `bin\`、同目錄指標解析到該 manifest 版本目錄，且內容仍須通過既有 sha256 比對；位於他處的同 byte 複本仍拒絕。

未修改 `p4_case_map.yaml`、`p4_required.lock.json` 與 `l1_required.lock.json`；未刪除案例、未放寬斷言或 judge 的判定規則。(d)(e) 之分母縮減與 `_windows_stable_entry_of` 的路徑放寬，皆待擁有者核准（見 CHG-S1X-P4-APPL-01 與 addenda）。

未完成項（OPEN，不得記為 PASS）：`test_daemon_restart_ab` 不在任一平台的凍結 selector 內，sc20-daemon-restart 兩平台皆 FAIL（PENDING-OD-S1X-P4-SC20）；bc05-three-topologies 在 Windows 無任何適用節點（PENDING-OD-S1X-P4-BC05-WIN）。兩者使 `check_p4_cases.py --check` 與 `test_p4_required_cases_have_passing_evidence` 恆為 FAIL，此為事先存在之缺口（sc20 於 HEAD 即無 Linux 紀錄來源），須由擁有者或計畫層級裁決。原生 Windows 黑箱實跑（2026-10-22）：巢狀 ab-suite 因 Windows selector 僅剩 rust-only 的 `test_autostart_l15_tx_once`，A 輪無受測行程而未留下 PID 日誌，B 輪未啟動；Windows COM 自測 ST-04 為 FAIL（bridge 工具，非本組）。`ab_blackbox_1b._bundle_identity` 於 Windows staging（無 `.git`）執行 `git rev-parse HEAD` 未捕捉 `CalledProcessError`，屬潛在缺陷，本次未修。
