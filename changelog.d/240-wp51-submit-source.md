---
type: fix
scope: rust-daemon
---

Rust daemon：command.submit 來源驗證：`command.submit` 在命令 admission 前拒絕保留的 `system` 來源，並將 bare `human`／`human:` 正規化後寫入命令紀錄；非字串來源沿用 Python 的字串轉換規則。
