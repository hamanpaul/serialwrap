---
type: fix
scope: uart
---

TX 區分未提交、已確認 N bytes、結果不明；TX WAL 只記確認寫出的 bytes 並依實際寫入順序。寫入等待可被取消，RX 觀察者取得該 chunk 的精確 WAL seq。
