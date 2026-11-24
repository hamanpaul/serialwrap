---
type: security
scope: daemon
---
- 自動登入密碼與 console unlock secret 的回顯只在 secret TX 後到首個 `\r`／`\n` 的行內以等長 `*` 遮罩；未決前綴不參與登入 prompt 判定，行終止符後資料原樣可見。WAL META 僅記 byte 長度。
