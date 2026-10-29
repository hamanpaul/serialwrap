# Rust 重構 Low 待辦

> 依 [`review-convergence-rules.md`](review-convergence-rules.md) §1：Low 不擋放行，統一收在這裡，Stage 3 Python daemon 退役前以一個收尾包處理。由編排者從審查報告收錄。
> 欄位：來源報告 | finding 編號 | 檔案:行 | 摘要 | 優先（安全／資料完整性疑慮標 ★）| 狀態

| 來源報告 | 編號 | 檔案:行 | 摘要 | 優先 | 狀態 |
|---|---|---|---|---|---|
| s1-exit/opus-c-20261029.md | S1X-c-3 | crates/sw-cli/src/rollback.rs:41 | 正式二進位仍編入 `crash_if` 注入點與 `pub fn set_test_crash_point`／`set_test_hook`（未設定時不作用） | ★（release bundle 不含故障注入的精神） | 待處理 |
| s1-exit/opus-c-20261029.md | S1X-c-4 | crates/sw-cli/src/setup_cmd.rs:1032 | `windows_task_definition` 固定 `register_held: false`，不理會 `ReconcileRequest.hold_target` |  | 待處理 |
