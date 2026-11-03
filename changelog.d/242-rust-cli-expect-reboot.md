type: change
scope: cli
---
Rust CLI 的 `cmd submit` 新增 `--expect-reboot`，互動模式拒絕訊息與 Python CLI 對齊，並只在旗標啟用時向 `command.submit` 傳送 `expect_reboot: true`。
