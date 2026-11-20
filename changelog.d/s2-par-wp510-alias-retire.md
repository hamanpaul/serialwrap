---
type: fix
scope: rust-refactor
---

- `alias.set` 在持久化前於 manager 鎖內重新確認 session 仍存在並持鎖提交；佔位已退休時回 `SESSION_NOT_FOUND`，不會把 alias 寫回已退休的 sid。
