---
type: fix
scope: rust-core
---

修正未被 actor 接受的 UART 交接改用非同步關閉後，release 可能在 handle 尚未關閉時提前成功：交出 handle 時同步轉移關閉責任，保留 attach 作業與 release 等待直到同 token 的實際回條；失敗終態維持永久阻擋與首次期限語意。
