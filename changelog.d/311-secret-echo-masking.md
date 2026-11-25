---
type: security
scope: daemon
---
- 自動登入密碼與 console unlock secret 在 TX 前啟用內容遮罩：完整回顯在寫 WAL 前換成等長 `*`；可能是 secret 開頭的尾段會扣住、不發布也不參與 prompt 判定，待後續位元組證明不匹配才原樣放行，絕不因逾時放行。首次完整命中後停止遮罩；登入結束時未決前綴以 `*` 釋出。secret 等於 prompt，或 secret 以 prompt 開頭且 DUT 不回顯時，登入會以既有 prompt-timeout 錯誤碼結束；有回顯的重疊情境仍可完成登入。WAL META 僅記 byte 長度。
- issue 312：手動登入時，reader 單獨依序維護密碼提示狀態；raw 與 line-buffer 輸入的 secret 行綁定 console 連線生命週期，TX 只記長度 META，換行與 UART 位元組維持原樣。其他 console detach 不會結算該輸入者；明確 detach、自然斷線及 session stop 會以正確的 `human:<client_id>` source 結算未完成輸入。reader 依內容遮罩 human secret echo，並在提示前綴含無效 UTF-8 時繼續處理同 chunk 後續文字。profile 沒有匹配的 `password_regex` 且沒有可辨識的 console unlock 提示時，仍可能明文記錄。
