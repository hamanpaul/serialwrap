---
type: fix
scope: rust-refactor
---

R-21 整檔豁免防線在無 policy engine（CI）時改以釘選的 engine baseline markers 解析 marker 集合（與 `resolve_markers` 同式，集合為空即 FAIL），修正 CI 上 marker 維度空轉；新增無 engine 反證測試並登記進 `s1_pytest_full.linux`。
