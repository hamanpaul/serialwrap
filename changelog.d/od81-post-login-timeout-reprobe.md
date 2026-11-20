---
type: fix
scope: rust-p7
---

- Rust daemon：登入後命令逾時（`POST_LOGIN_CMD_TIMEOUT`）改為與其他可重探的登入錯誤一樣，依既有退避規則（2／4／8／15 秒、10 次耗盡、READY 後重置）自動重探，不再永久停在 DETACHED；ATTACHED 既有 bridge 的再探失敗同樣可重探。Python 基線維持 DETACHED 不重探（OD-75），差異依 OD-81 登記為 DF-27。
