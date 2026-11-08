---
type: fix
scope: rust-refactor
---

OD-63 佔位 session 開埠遇一般 `OSError` 時，先回傳舊佔位的 DETACHED／`ATTACH_FAILED` 快照，再退休佔位並以原 COM 交給 dynamic 重試；有 fallback 時留下可重新 attach 的 DETACHED 列，無 fallback 時僅在裝置仍在線且 daemon 未重啟期間暫留 COM，拔除後釋放。Held、Windows `AccessDenied` 與 NotFound 維持既有錯誤回應。
