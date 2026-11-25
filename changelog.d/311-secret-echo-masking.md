---
type: security
scope: daemon
---
- 自動登入密碼與 console unlock secret 在 TX 前啟用內容遮罩：完整回顯在寫 WAL 前換成等長 `*`；可能是 secret 開頭的尾段會扣住、不發布也不參與 prompt 判定，一旦已比對 prefix，後續失配或行尾前仍以 `*` 結算，真正的非 prefix byte 依原順序發布，絕不因逾時明文放行。首次完整命中後停止遮罩；登入結束時未決前綴以 `*` 釋出。secret 等於 prompt，或 secret 以 prompt 開頭且 DUT 不回顯時，登入會以既有 prompt-timeout 錯誤碼結束；有回顯的重疊情境仍可完成登入。WAL META 僅記 byte 長度。
- issue 312：手動登入密碼改在 actor 接收當下依 session 提示窗口分類，結果原樣隨 backlog、deferred replay、lease 與 writer 傳遞；TX payload 省略 secret bytes 並在寫出當下立即記長度與 `human:<client_id>` source META，不在 writer 重判、不保留待結算輸入或綁定 client lifetime。reader 在提示送到 console 前更新窗口；提示前已輸入的 bytes 屬窗口外，照常記錄。secret 在 UART 寫出前登記給 reader 增量遮罩回顯，每個窗口最多保留 1024 bytes，超出後遮罩該 RX 行至行尾且不再保存；沒有未決 echo 前綴時在後續 RX 行尾清除登記，未決前綴跨過較早行尾，完整 echo 行尾或 bridge epoch 結束時清除。raw 模式每個接收 chunk 各有一筆判定與 META，line-buffer 每次出列整行判定一次；沒有匹配的 `password_regex` 且沒有可辨識的 console unlock 提示時，輸入仍可能明文記錄。
- i311-f6：line-buffer 的模式與 secret 分類共用出列當下的 password generation 快照，實際被當作 secret 的文字一律以一般 line 模式送出；先入列的普通 `vim` 仍可在 secret 窗口結束後建立 lease，反向排隊亦不得因文字開啟 lease。tracker 鎖只在分類與 secret echo 登記的短區段持有，TX would-block 時 RX reader、cancel 與 shutdown 仍可前進；已在途的 secret matcher 不會被先於 TX 完成的 RX 行尾清除。1024-byte 保存上限與超限後逐 byte sink 遮罩新增可觀測回歸。
- i311-f7：human secret 分類改在 actor 接收當下只判定一次；line mode 出列與 raw chunk、backlog、deferred replay、interactive lease、TxWriter、WAL/META 及 echo matcher 共用同一判定，writer 不再依提交當下窗口重新分類。RX 已比對 secret 前綴後，失配或行尾只能先以等長 `*` 結算，不得明文釋出；遮罩前綴、行尾與後續 bytes 對 WAL、event、console、agent log 維持相同位元組順序。
