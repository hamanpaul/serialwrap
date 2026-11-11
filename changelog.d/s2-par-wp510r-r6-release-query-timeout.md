---
type: fix
scope: rust-refactor
---

- parked handle 等待期間，首次 release 的 session 快照查詢失敗時仍沿用原期限；期限到後由 actor 提供當前 timeout epoch，回覆 `DRAIN_STALLED`，並保留 close 成功後完成及落盤的 release 義務。
