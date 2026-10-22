---
type: fix
scope: test-infra
---

Stage 1B A/B 執行器的 Python 輪 skip 登記改為只核准 `rust_only` 節點，同一登記案例內的共用節點須實際執行並比較，不再因案例登記而整案拋出錯誤、導致 `summary.json` 未產生。`mkbundle.sh` 拒絕覆寫非空且非 mkbundle 建立的輸出目錄（exit 2，不寫入），mkbundle 建立的目錄則整個重建，避免 sentinel 與 dev 佈局混用、bundle 定位取到過期的頂層 manifest。

遷移（一次性，擁有者確認後執行）：舊版留在 `target/mkbundle-dev` 的無標記目錄會使 `mkbundle.sh --dev --out target/mkbundle-dev` 以 exit 2 拒絕，指令為 `rm -rf target/mkbundle-dev`。`ab_blackbox.sh` 已改為 `target/ab-runs/<run>/bundle`，不再讀取該目錄；pytest 的 `SW_BUNDLE` 預設改為 `target/mkbundle-pytest`（`test_fresh_install_blackbox` 與 `test_cli_mgmt_doctor`），該目錄不存在時測試明確失敗並提示建置指令，不再以舊目錄的過期 bundle 驗證。
