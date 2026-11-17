---
type: fix
scope: rust-s2-gfx
---
對齊 Windows Stage 2 與 Linux release bundle 的 daemon 測試鉤子檢查：只逐名允許四個測試鉤子，CLI 與其他測試鉤子仍拒絕；辨識 Rust 執行檔中相鄰的靜態字串，不誤把 hook 名後接的 `config.yaml`／`Traceback` 併成環境變數名；系統設定路徑覆寫在非隔離 daemon 中忽略。
