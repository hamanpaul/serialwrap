> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`479f8d2b`。`a81ff33b..479f8d2b` 僅一筆提交、無 merge，已逐項檢查 diff。前輪修正提交皆為 HEAD 祖先；產品碼、契約與凍結鎖本輪未變。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消的鎖內互斥保留。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉的阻擋與持久化重試保留。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗排空路徑保留。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 保留。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業的路徑保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限保留。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成 RELEASED 與持久化路徑保留。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條保留。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 由 wp510／wp510r 承接，不增加輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 保留。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | 明確指定 artifact 的解析與環境還原邏輯未變，selector 自測通過。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | 改以 `dir == "TX"` 串接；正式欄位的 TX／RX／TX 交錯案例已攔截。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 自測使用臨時假 repo／artifact，隔離並驗證 `CARGO_TARGET_DIR`，不再依賴工作樹二進位。 |

直接相關驗證：

- 設定 `CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target`，兩個 helper 自測：**2 passed，0.07 秒**。
- 記憶體對照：`a81ff33b` helper 漏判前輪 TX／RX／TX 樣本；`479f8d2b` 正確拒絕。將新版負向自測接回舊 helper，測試確實轉紅。
- 使用 `--noconftest`、停用第三方 plugin 自動載入、pytest cache 與 bytecode，避免 production 探測；產生 3 個未註冊 marker 警告，此結果僅作 helper 定向證據。
- 未執行完整 pytest、daemon 情境、docker／bundle；未接觸 production daemon、正式設定／狀態或實體 UART。未修改來源檔，結束時工作樹乾淨。

## B 新發現

NO FINDINGS。

## 既有碼待辦（不計入結論）

- wp510／wp510r：承接 P8-c4913ae-1。
- b6 後續子項：Rust post-login 逾時退避重探、DF-27／案例與測試節點登記、接線及整體驗收。
- wp73、df25、od79、od80：依指定工作包另行承接。
- 沿用前輪待辦：一般 RELEASED session 開埠失敗後再遇落盤失敗的 Rust／Python 終態差異；本輪未重新實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行