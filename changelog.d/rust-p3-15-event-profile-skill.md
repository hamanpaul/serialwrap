---
type: feat
scope: rust-cli
---
實作 Rust CLI 的 `event` 群組（10 個子指令的 RPC 組包、`add --file` 讀檔與 JSON 解析、非 UTF-8 參數與 timeout 例外的 INTERNAL_ERROR 訊息）、離線 `profile test`（regex 命中行、`UNKNOWN_PROFILE`、`SAMPLE_READ_FAILED` 與 Python 未攔截例外的 exit 1）與內嵌 `skill` 操作指南輸出（Windows 上 `\n` 轉 `\r\n`）；stdout、stderr 與 exit code 對齊 Python 基線。
