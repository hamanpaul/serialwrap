---
type: feat
scope: doctor
---

Rust CLI 的 `serialwrap doctor` 新增 advisory 檢查 `profile_template_fields`：比對出貨資產與線上 `profiles/default.yaml`，僅在兩邊都存在同名 template 時比較頂層欄位，列出出貨資產有、線上缺少的欄位；不比較值與巢狀欄位。`serialwrap setup` 不覆寫既有 profile，舊安裝可能缺少新欄位，這種 drift 只做 advisory 提醒，不影響 doctor 整體 `ok`。本項僅在 Rust CLI 實作，Python 基線維持原樣。
