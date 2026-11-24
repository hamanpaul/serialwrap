---
type: security
scope: daemon
---
- 自動登入密碼與 console unlock secret 的回顯在 reader 出口前以等長 `*` 遮罩，並新增只記 byte 長度的 WAL META 稽核事件。
