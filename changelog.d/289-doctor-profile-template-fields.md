---
type: feat
scope: doctor
---

Rust CLI 的 `serialwrap doctor` 新增 advisory 檢查 `profile_template_fields`：比對出貨資產與線上 `profiles/default.yaml`，僅在兩邊都存在同名 template 時比較頂層欄位，列出出貨資產有、線上缺少的欄位；不比較值與巢狀欄位。不可讀、不可解析、結構錯誤、未物化或沒有共同 template 時明確標示不可比較，不會冒稱一致。`serialwrap setup` 不覆寫既有 profile，舊安裝可能缺少新欄位，這種 drift 只做 advisory 提醒，不影響 doctor 整體 `ok`。OD-33 升級驗收使用受版控的固定階段政策，僅豁免 devices advisory，並嚴格核對 Rust check 的頂層結構化 payload、共同 template 與完整缺漏集合。本項僅在 Rust CLI 與升級判定 harness 實作，Python 產品基線維持原樣。
