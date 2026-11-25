---
type: fix
scope: rust-command-completion
---
- 沒有 `completion_regex` 的 Rust 一般 agent line／background 命令以完整命令 echo 錨定 prompt 判定；echo 後的 prompt 必須是同一 snapshot 最後一行的完整尾端匹配，保留 prefix 型 prompt 與有串流證據的終端折行。折行證據只對應原始串流中候選插入點的 `\r\r\n` 或 ` \r`，不會借用 snapshot 其他位置的標記；空白同時可能是命令字元或 readline 插入時，會依完整命令、該位置的原始證據與既有間距限制驗證兩條路徑。ANSI、backspace 與控制字元清理後仍保留逐字元來源位元組範圍。echo 候選只從命令首字元位置檢查；每行 prompt 前綴比對最多掃描 2,000,000 個字元，折行 matcher 每次判定最多走訪 2,000,000 個狀態；耗盡上限時視為 echo 未確認並套用既有無 echo 規則。stdout 從已確認的 echo 結束位置擷取，不以命令字串後綴剝除輸出；echo 前 human 輸出不納入正常 stdout。帶 `completion_regex`／marker 的 producer 擷取維持既有結果。相容限制：echo 之後的第一行恰好等於命令、而且後面還有輸出時，視為重複 echo 並刪除；需要保留這種輸出時，請使用 `completion_regex` 或 marker。
- 每次 human submit 都在 RX 鎖下先取候選 offset；只在 OS 接受 bytes 或提交結果不明時記錄該 offset，WouldBlock 不留下 TX 證據。submit 期間抵達的回應因此仍屬於該次 human 輸出。agent 換行結束當前 raw human 半行後，不再污染下一筆命令。
- 無 echo 且判定窗口沒有可信 human TX 時，保留尾端最後一行 prompt 的相容完成；只有 human 交錯且無 echo 時改回 `PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留輸出且不送救援鍵、不自動重送。逾時結算只使用 deadline 前最後檢查的 RX snapshot；期限後新到的 echo／prompt 不會使命令改判成功。
- i294 第一階段尚未處理 issue 262 的 prompt 身分與 issue 271 的同行輸出裁切，交由 i294-i2。
