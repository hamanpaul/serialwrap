---
type: fix
scope: sw-core
---

Rust SessionManager 啟動時將缺失 template 的 RELEASED 記錄實體化為 passthrough 佔位 session，保留 session 身分、裝置綁定、alias 與 release metadata，並跨重啟保存阻擋；通用 `passthrough`／`others-template` 不標記為 OD-63 佔位。
