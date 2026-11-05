### Fixed

- 修正 hosted Windows pytest 的測試替身、部署版面與 golden 產生器可攜性，補齊固定 Rust 工具鏈；出口證據整合檢查仍由 driver 強制執行。
- WAL 測試先關閉常駐 writer 再清理暫存目錄，保留所有既有內容與輪替斷言。
