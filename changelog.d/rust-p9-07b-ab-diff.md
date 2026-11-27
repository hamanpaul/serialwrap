---
type: fix
scope: stage3
---
修正 L1.5 A/B 報告差分的身分、來源與 FAIL 歸屬：Python daemon 驗 package tree，Rust daemon 驗 executable SHA；非 bridge 的身分只有在原始 release manifest 存在、列於 `source_files`、位元組雜湊已核實且適用 artifact 欄位一致時才成立。Rust L1 依 Cargo 收集器的頂層 `runner`／`finished` 與 `_provenance()` 實際欄位驗證；pytest L1 fixture 對齊資源報告收集器。逐筆保留已映射的原始 FAIL，並獨立掃描 envelope verdict 與原始／envelope invariant FAIL；識別欄位無效或來源未核實時仍以原始索引留在 `unverified_failures`。CLI artifact 自身身分與 A/B CLI SHA 配對分開記錄；配對不符令 B 側 integrity 失敗，但不降級已核實的 B 產品 FAIL 或安全違規。比較每次呼叫的 logical argv，保留未列明的參數，只移除 CLI 執行檔與 `--endpoint`、`--socket`、`--bench` 及其值，並替換 instance root；命令差異以新增的 `kind: "argv"` 記錄，呼叫數差異逐筆以新增的 `kind: "call_count"` 記錄，兩者均含來源引用。缺少來源、完成狀態或必要觀測會令 integrity 失敗，但退出碼不單獨推導產品 FAIL。A 側一般 FAIL 列入 `baseline_failures`；B 側只有當側身分、來源及 required case 對應已核實的 FAIL 才列入 `product_failures`。T7B 保留差異與 `registered_change`，T7 依 `absolute_only` 處理，B 側 FAIL 保留。`foreign_holders` 子樹不套用通用 volatile 欄位刪除或 instance-root 替換；B11 自身 PID 快照仍是候選規則，待擁有者追認。
