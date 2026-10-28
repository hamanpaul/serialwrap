---
type: feat
scope: rust-refactor
---

Rust CLI 跟進 #234 `boot_done_regex`：`sw-proto` profile 載入新增 `boot_done_regex`（template 載入與 target 覆寫語意與 Python 一致），`serialwrap profile test` 回報 `checks.boot_done_regex` 是否命中，CLI help golden 依 Python 變更重產。
