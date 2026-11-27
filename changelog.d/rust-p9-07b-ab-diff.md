---
type: fix
scope: stage3
---
修正 L1.5 A/B 報告差分的身分、來源與 FAIL 歸屬：依 Python package tree 或 Rust daemon executable SHA 驗證適用的 artifact 欄位，檢查 bridge case/run 身分與 P2 manifest 宣告的 wire、CLI 原始檔雜湊；非 bridge runner 以 envelope 明列的 nodeid 和原始 L1 結果精確映射 required case，並獨立保全原始 invariant 結果。runner 非零退出、必要結果未執行、缺漏或格式不正確會令 integrity 失敗，但退出碼不單獨推導產品 FAIL。A 側一般 FAIL 列入 `baseline_failures`；B 側只有身分、來源與 required case 對應已核實的 FAIL 才列入 `product_failures`，其他可解析 FAIL 線索保留在 `unverified_failures`。T7B 保留差異與 `registered_change`，T7 依 `absolute_only` 處理，B 側 FAIL 保留。`foreign_holders` 子樹不套用通用 volatile 欄位刪除或 instance-root 替換；B11 自身 PID 快照仍是候選規則，待擁有者追認。
