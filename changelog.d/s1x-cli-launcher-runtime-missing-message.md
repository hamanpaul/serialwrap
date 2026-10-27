---
type: fix
scope: rust-refactor
---

Windows `.py` daemon launcher 缺 manifest runtime 時，`daemon start` 訊息改為指出 launcher 與缺少的 runtime 路徑（仍為 `DAEMON_BINARY_NOT_FOUND`、fail-closed）。
