---
type: fix
scope: sw-core
---

OD-63 佔位 session 經成功獨占開埠後會退休並交由 template 動態偵測；偵測與正式 bridge 共用預開 handle，保留原 COM，且不搶用離線 DETACHED 空槽。
