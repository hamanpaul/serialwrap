## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e` | 0 | [SC-18 案例](tests/contract_blackbox/test_cli_mgmt_daemon.py:504)先停止 daemon，再經 barrier 排空及 checkpoint 長度／雜湊核對，最後精確比對完整 TX；`897a3cd` 同步測試替身。 |
| 2 | Medium | 已修 `51bd8fa` | 0 | [Ctrl 事件測試](crates/sw-pal/tests/exec_delegate.rs:254)改由實際受委派孫行程發布 PID，確認存活才送事件，並確認事件後孫行程結束；委派前停住會逾時失敗。 |
| 3 | Medium | 已修 `4e3ba13` | 0 | [出口 driver](scripts/rust/stage1_exit_driver.sh:419)已接第二層判定，核對 scenarios、basic、metadata 與逐列核准原因；其他失敗、缺報告及 harness 關閉失敗仍阻擋。 |
| 4 | High | 已修 `12b3a92` | 0 | [舊版安裝夾具](scripts/docker/run_upgrade_container.py:190)移除系統套件共用，改用離線 wheelhouse；[bundle](scripts/rust/mkbundle.sh:287)自行封裝並驗證從隔離 runtime 載入 PyYAML。 |
| 5 | Medium | 已修 `12b3a92` | 0 | [建置腳本](scripts/rust/mkbundle.sh:156)僅在 `--dev` 加入 `sw-cli/test-fakes`；正式配方已改用 `--release-bundle`。 |
| 6 | Low | 已修 `12b3a92` | 0 | [發佈前置條件](docs/rust-refactor/release-0.4.0-plan.md:10)已改為 `VERIFIED`，或僅剩明確核准延後項的 `IMPLEMENTED`。 |

## B 新發現

NO FINDINGS。

已逐一審查指定區間：共 **1 個非 merge commit（`897a3cd`），0 個 merge commit**。替身改以 barrier 完成延遲寫入、checkpoint 封住完整前綴；原有重複 TX、拒絕請求零 TX 與 autostart 證據斷言均保留。

驗證：三支相關純來源檢查直接呼叫通過；Python 語法、契約檢查、cfg 邊界及 `git diff --check` 通過；policy check 無 FAIL、兩項 WARN。未重跑完整 pytest／Cargo、Windows 原生或 Docker；前輪修正狀態依 HEAD 程式核對。全程未修改檔案、未接觸 production，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：可放行