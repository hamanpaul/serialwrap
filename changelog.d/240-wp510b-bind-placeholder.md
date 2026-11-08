---
type: fix
scope: rust-daemon
---
`session.bind` 遇到 OD-63 RELEASED 佔位時立即回傳 `released:true` 與 `device_attach` 建議，不取消或等待在途 attach，也不產生持久化、開埠或 UART 副作用。
