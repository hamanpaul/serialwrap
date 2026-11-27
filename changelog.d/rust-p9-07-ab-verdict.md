---
type: fix
scope: stage3
---
修正 A/B 判定的失敗優先與來源驗證：L1.5 先以 T7B 每筆差異的 `verified` 狀態核實來源；已核實且未豁免的差異保留為產品 FAIL，並在 notes 保留來源、完整性與 suite INFRA 診斷，未核實差異只導致 INFRA。L2 與非長跑 L3 在已成立的產品／安全 FAIL 後檢查 suite `INFRA_ERROR`，一般 suite `FAIL` 不推導逐案結果。XFAIL 僅在兩側都有有效且可比較結果時因差異列入 needs_review。L2 的 TestPilot 結果沒有逐案安全欄位，因此安全違規保持空清單，不從 `cases.yaml` 的 `invariants` 覆蓋項推論；歷史 L3 只採用 P2 gate `checks` 中明確的 `group: safety`、`result: FAIL`，並保留兩側來源。歷史 gate 依 P2 `release-gate.v1` 檢查必要欄位及每筆 check 的 `group`、`id`、`result` 列舉；先驗證 A `baseline`／B `candidate` mode，再收集對應側的失敗。L2 產品失敗 `source_ref` 指向相對側 `report_dir` 的 TestPilot／realhw `report.json#/results/<index>`，額外 evidence 引用另列於 `evidence_refs`。L3 一般逐案比較仍走 required case 與 sentinel 規則，不讀取長跑 gate；`case_changes` 展開所有 scope（包括 rust_only）。
