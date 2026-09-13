---
type: fix
issue: 166
scope: file-transfer
---
檔案傳輸新增 base64／OpenSSL 雙向工具 fallback 與可選的 `max_console_line_chars` 單行預算；
以實際成功 sentinel 驗證 probe、chunk、checksum 與搬移，避免命令回顯或 prompt 假成功。
profile 不足以容納固定命令時會在 TX 前回報 `CONSOLE_LINE_LIMIT_TOO_SMALL`。
