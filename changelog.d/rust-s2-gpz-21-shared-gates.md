---
type: fix
scope: rust-refactor-gates
---
依 OD-93 新增四個 Slice gate 共用 evidence manifest，綁定候選 HEAD、乾淨工作樹、debug/release artifact 與鎖定 selector；共用段拒收時保留 EVIDENCE_MISSING 且不重跑，測試失敗維持 FAIL。Windows L1 以既有 l1_judge 核對凍結 nodeid 集合與執行結果。補上 P7 與 Slice 2.4 dry-run，並匯入編排者提供的 Windows 原生集合。
