---
type: refactor
scope: rust-refactor
---

S2 gate 前置衛生：preflight 全套 pytest 上限依 6449.73 秒實測加 15% 餘裕，向上取整為 7500 秒；將條件 backref 的多分支錯誤檢查改為單次條件判斷，保留原有錯誤行為；Unix 權限測試使用八進位 mode；daemon 目錄預設測試依 Python 的平台路徑分隔符精確比對。
