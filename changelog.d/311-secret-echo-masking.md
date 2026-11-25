---
type: security
scope: daemon
---
- 自動登入密碼與 console unlock secret 在 TX 前啟用內容遮罩：完整回顯在寫 WAL 前換成等長 `*`；可能是 secret 開頭的尾段會扣住、不發布也不參與 prompt 判定，待後續位元組證明不匹配才原樣放行，絕不因逾時放行。首次完整命中後停止遮罩；登入結束時未決前綴以 `*` 釋出。secret 等於 prompt，或 secret 以 prompt 開頭且 DUT 不回顯時，登入會以既有 prompt-timeout 錯誤碼結束；有回顯的重疊情境仍可完成登入。WAL META 僅記 byte 長度。
- issue 312：手動登入密碼改依 UART 寫出順序在 session 提示窗口內分類，TX payload 在寫出當下省略並立即記長度與 `human:<client_id>` source META；不保留待結算輸入或綁定 client lifetime。reader 在提示送到 console 前更新窗口；提示前已輸入的 bytes 屬窗口外，照常記錄。secret 在 UART 寫出前登記給 reader 增量遮罩回顯，每個窗口最多保留 1024 bytes，超出後遮罩該 RX 行至行尾且不再保存；沒有未決 echo 前綴時在後續 RX 行尾清除登記，未決前綴跨過較早行尾，完整 echo 行尾或 bridge epoch 結束時清除。raw 模式每個寫出 chunk 各有一筆 META，line-buffer 每次整行提交一筆；沒有匹配的 `password_regex` 且沒有可辨識的 console unlock 提示時，輸入仍可能明文記錄。
- i311-f6：line-buffer 的模式與 secret 分類共用出列當下的 password generation 快照，實際被當作 secret 的文字一律以一般 line 模式送出；先入列的普通 `vim` 仍可在 secret 窗口結束後建立 lease，反向排隊亦不得因文字開啟 lease。tracker 鎖只在分類與 secret echo 登記的短區段持有，TX would-block 時 RX reader、cancel 與 shutdown 仍可前進；已在途的 secret matcher 不會被先於 TX 完成的 RX 行尾清除。1024-byte 保存上限與超限後逐 byte sink 遮罩新增可觀測回歸。
