---
type: fix
scope: rust-s2-gfx
---
對齊 Windows Stage 2 與 Linux release bundle 的 daemon 測試鉤子檢查：只逐名允許四個隔離測試鉤子，CLI 與其他測試鉤子仍拒絕；系統設定路徑覆寫在非隔離 daemon 中忽略。
