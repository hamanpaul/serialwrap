---
type: feat
scope: doctor
---

`serialwrap doctor` 新增 advisory 檢查 `profile_template_fields`：比對出貨資產與線上 `profiles/default.yaml`，對兩邊都有的 template 列出出貨資產有、線上沒有的頂層欄位。`serialwrap setup` 不覆寫既有 profile，舊安裝因此拿不到新版 template 新增的欄位（例如 `boot_done_regex`），功能靜默不生效；過去只有 `profile_bootloader_prompts` 檢查單一欄位。只看欄位在不在（值為空或 `null` 算有設定），不比對值與巢狀欄位，線上自行多加的欄位或 template 不算；屬 advisory，不影響 doctor 整體 `ok`。Python 基線與 Rust CLI 同步實作，輸出逐字一致。
