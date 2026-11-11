---
type: fix
scope: rust-refactor
---

- 期限已過且首次 `DRAIN_STALLED` 回覆仍等待 actor 時，後續 `device.release` 改用首次等待前保存的完整 session 快照與 timeout epoch，立即回覆並讓背景完成義務繼續重試。
