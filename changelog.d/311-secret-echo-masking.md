---
type: security
scope: daemon
---
- 自動登入密碼與 console unlock secret 在 TX 前啟用內容遮罩：完整回顯在寫 WAL 前換成等長 `*`；可能是 secret 開頭的尾段會扣住、不發布也不參與 prompt 判定，待後續位元組證明不匹配才原樣放行，絕不因逾時放行。首次完整命中後停止遮罩；登入結束時未決前綴以 `*` 釋出。secret 等於 prompt，或 secret 以 prompt 開頭且 DUT 不回顯時，登入會以既有 prompt-timeout 錯誤碼結束；有回顯的重疊情境仍可完成登入。WAL META 僅記 byte 長度。
