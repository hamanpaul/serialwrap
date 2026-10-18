---
type: change
scope: regression
---

F12 `f12-wal-path-live` 與 L3 `p0-wal-live` 改以讀回含 marker 的新增 WAL 紀錄並驗證 CRC 判定活性，不再依賴 mtime。
