---
type: fix
scope: rust-cli
---
修正監管模式切換沿用 `HOME` 優先的家目錄解析，避免把 systemd user unit 寫到 passwd 家目錄。
