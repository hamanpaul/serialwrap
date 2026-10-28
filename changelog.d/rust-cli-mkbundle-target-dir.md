---
type: change
scope: rust-cli
---

`mkbundle.sh` 接受呼叫端指定的 `CARGO_TARGET_DIR`，並只在 `--dev` bundle 啟用 `sw-cli/test-fakes` 更新測試鉤子；正式 bundle 不含該 feature。Cargo L1 凍結清單排除 ignored 與 `cap_*` 測試，完整執行報告仍保留 ignored 結果；cfg 邊界檢查器不設原生平台測試例外，POSIX／Windows 測試改由 pytest 明確驅動。
