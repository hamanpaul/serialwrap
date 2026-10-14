### Added

- 新增可選 `cmd submit --completion-regex`，經 RPC、仲裁佇列與命令記錄傳到執行層；noisy console 無 shell prompt 時可依本命令的新 producer RX 完成，只裁切完成標記之後的 profile prompt、保留完整 stdout 與既有 reboot recovery。
- 在所有提交／執行入口檢查有界、可編譯且不匹配空串的 regex，無效值以 `INVALID_COMPLETION_REGEX` 零 TX 拒絕；命令記錄與 WAL META 提供完成設定稽核。
- PTY 覆蓋 numeric RC／END、echo-only、歷史標記、原 prompt fallback、CLI/RPC 傳遞及七參數 callback 相容性。
