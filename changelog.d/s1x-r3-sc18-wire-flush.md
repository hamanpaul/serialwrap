---
type: fix
scope: rust-refactor
---

SC-18 原生案例在成功送出後有界等待 bridge 證據 writer 落盤再比對 TX，並把被拒請求的零 TX 檢查移到 READY 等待之後。
