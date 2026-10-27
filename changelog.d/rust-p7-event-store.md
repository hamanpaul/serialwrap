### Added

- Rust 重構 P7 Task 13：事件規則註冊表（`RuleRegistry`）、fire counter（`CounterStore`，write-through 快取）與 `events.ndjson`（`EventLogger`，10 MiB×3 輪替、`tail` 過濾與切片語意）的 Rust 承接；`DaemonDirs.events_log_path`（`SERIALWRAP_EVENTS_LOG_PATH`）。

### Fixed

- Rust 承接 DF-01（不合法 rule_id 不碰檔案系統）、DF-09（規則檔 ValueError／TypeError 收斂為 load 失敗、不崩潰）、DF-18（tmp→正式檔一律覆寫式取代）。
