---
type: fix
scope: rust-command-completion
---
- 沒有 `completion_regex` 的 Rust 一般 agent line／background 命令以完整命令 echo 錨定 prompt 判定；echo 後的 prompt 必須是同一 snapshot 最後一行的完整尾端匹配，保留 prefix 型 prompt 與有串流證據的終端折行。搜尋從每個完整 echo 行尾反向比對命令，只在候選插入位置使用原始串流中的 `\r\r\n` 或 ` \r` 局部折行證據；空白同時可能是命令字元或 readline 插入時，依完整命令、該位置證據與既有間距限制驗證兩條路徑。候選 prompt 前綴以逐字元行首索引取得，直接 echo 不逐一掃描命令首字元候選；不設會被 RX 視窗內合法輸入耗盡的候選或 matcher 預算。ANSI、backspace 與控制字元清理後仍保留逐字元來源位元組範圍。stdout 從已確認的 echo 結束位置擷取，不以命令字串後綴剝除輸出；echo 前 human 輸出不納入正常 stdout。帶 `completion_regex`／marker 的 producer 擷取維持既有結果。相容限制：echo 之後的第一行恰好等於命令、而且後面還有輸出時，視為重複 echo 並刪除；需要保留這種輸出時，請使用 `completion_regex` 或 marker。
- 每次 human submit 都在 RX 鎖下先取候選 offset；只在 OS 接受 bytes 或提交結果不明時記錄該 offset，WouldBlock 不留下 TX 證據。submit 期間抵達的回應因此仍屬於該次 human 輸出。agent 換行結束當前 raw human 半行後，不再污染下一筆命令。
- 無 echo 且判定窗口沒有可信 human TX 時，保留尾端最後一行 prompt 的相容完成；只有 human 交錯且無 echo 時改回 `PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留輸出且不送救援鍵、不自動重送。RX reader 追加與可信 human TX 提交在同一 monotonic clock 下、於 RX 鎖內記錄事件時間；completion waiter 沿用外層絕對期限，並以 operation deadline 與靜默期限較早者結算。逾時時重新建立只含事件時間不晚於該期限的 snapshot，納入期限前已提交但 evaluator 尚未檢查的事件，排除期限後事件。
- i294 第一階段尚未處理 issue 262 的 prompt 身分與 issue 271 的同行輸出裁切，交由 i294-i2。
