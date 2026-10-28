### Added

- Rust 重構 P7 Task 9：`session.recover` 的分支順序與回應組裝對齊 Python（手動 recover 重置自動重探進度）；force recover 依 1 秒輪詢點判定 READY、最多 10 次，並發呼叫各自輪詢。
