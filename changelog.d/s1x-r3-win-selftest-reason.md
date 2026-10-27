---
type: fix
scope: rust-refactor
---

Windows bridge selftest 資格檢查輸出 raw verdict 與報告／目前／核准 tree 診斷，`selftest-result.json` 的 `reason` 區分 raw 失敗與核准 tree 不符；判定不變。
