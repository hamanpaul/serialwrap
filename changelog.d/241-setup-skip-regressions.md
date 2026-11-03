---
type: fix
scope: rust-refactor
issue: 241
---

補齊 Rust CLI 正式 setup 入口的指紋重啟跳過回歸，鎖住 Python 基線的 action、原文 hint 與判定優先序，並驗證 flash 未帶 force 時在物化前拒絕，以及 --no-restart 仍保留模式轉換的 stop／start、不得追加 restart 或指紋輪詢。既有產品接線已符合這些行為，本次僅新增測試。
