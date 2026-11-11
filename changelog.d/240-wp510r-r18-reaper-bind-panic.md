---
type: fix
scope: rust-refactor
---
Rust 保管回收者停止時排空已受理的回收命令並等候 worker 回條；actor 連續 panic 進入停止流程時以立即回饋路徑執行 `ClaimForStop`；`session.bind` 也會取消並等待一般開埠帳本紀錄終態，再套用改綁。
