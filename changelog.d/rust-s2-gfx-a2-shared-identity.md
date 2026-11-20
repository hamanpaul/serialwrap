---
type: fix
scope: rust-s2-gfx
---

Slice 2.4 共用證據以候選 release bundle 身分標記逐案 Cargo 與 S24 結果，並在 S24 A/B 前後核對 bundle；候選檢查失敗時保留 `EVIDENCE_MISSING` 並繼續 Windows L1 匯入。先前 Slice 2.3 的案例改以 release daemon SHA 對齊本輪候選，同時在出口報告說明 2.3 debug 與 2.4 release 的差異。
