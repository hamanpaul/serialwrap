---
type: fix
scope: rust-s2-gfx
---
對齊 Windows Stage 2 與 Linux release bundle 的 daemon 測試鉤子檢查：只逐名允許四個測試鉤子，CLI 與其他測試鉤子仍拒絕；以 NUL 結尾的 Rust CStr 保留執行檔 hook 名邊界，不放寬相鄰字串或同前綴變數；系統設定路徑覆寫在非隔離 daemon 中忽略。
