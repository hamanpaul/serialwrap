---
type: fix
scope: rust-daemon
---

`device.attach` 對 OD-63 RELEASED 佔位改走專用交接：成功時保留原 COM、退休舊身份並交給 template 動態偵測；離線或 holder 衝突維持佔位，一般開埠錯誤則交給 dynamic fallback。
