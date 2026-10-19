---
type: change
scope: session
---
`session.attach` 與 `session.recover` 失敗時固定以 `SESSION_NOT_READY` 作為 `error_code`，原有診斷值改由 `last_error`、`last_error_detail` 回報。
