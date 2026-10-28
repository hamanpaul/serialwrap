### Added

- Rust 重構 P7 Task 10：agent log 改由 reader 端同步寫入（不經 session actor 佇列、不丟資料），文字模式換行與 Python 相同；`session.log_start`／`log_stop`／`log_status` 回應與 Python 位元組相容。
