---
type: fix
scope: device
---

`device attach` 對 RELEASED 佔位 session 開埠遇一般 OSError（如 dialout 權限不足的 `PermissionError`、`EIO`）時不再讓裝置成為孤兒（sol 增量審查 S1-9990b18-2，OD-63）：比照成功分支交接，佔位移除自身並保留原 COM，交 dynamic 偵測重新開埠；仍開不了埠時依現行 dynamic 開埠失敗語意（有 passthrough fallback template 即在原 COM 留下 `DETACHED`／`ATTACH_FAILED:<例外名>` 列），修好後可再 `device attach`，不必拔插或重啟 daemon。回應仍為 `ok:true`＋`DETACHED` session，不新增 RPC／CLI 表面。
