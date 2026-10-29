---
type: fix
scope: rust-console
---
Rust daemon：`session.interactive_open` 的 ATTACHED 救援分支改以現行 bridge 的 IoWorker 快照判定 worker 健康（running／serial_alive 任一為假回 `SESSION_NOT_READY`、零 TX），與 Python `bridge.snapshot()` 一致；先前正式路徑未注入健康探針而恆視為健康。
