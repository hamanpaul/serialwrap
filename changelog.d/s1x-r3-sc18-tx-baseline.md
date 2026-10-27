---
type: fix
scope: rust-refactor
---

SC-18 原生案例只比對自身開始後的 DUT TX（wire 為整個 B 輪共用），並要求該段恰為 daemon 實際送出的 `echo X\n` 一行；修正從未在原生跑到的 `echo X\r` 錯誤期望。
