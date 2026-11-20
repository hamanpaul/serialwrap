---
type: fix
scope: rust-refactor
---

修正 S24 共用黑箱驗證，改以同 HEAD 的正式 Stage 2 release bundle 執行，並將 bundle tree、manifest、CLI、daemon 與 `s24_ab` 證據互相綁定；Rust 版本與 setup 指紋改依受測 manifest 精確比對。另將 supervision 測試暫存目錄移至固定短路徑，並使 `daemon.stop` 回覆後立即拒絕新的 RPC 連線。
