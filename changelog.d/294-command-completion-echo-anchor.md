---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令以完整命令 echo 錨定 prompt 判定；echo 後的 prompt 必須是同一 snapshot 最後一行的完整尾端匹配，保留 prefix 型 prompt 與有串流證據的終端折行。stdout 從已確認的 echo 結束位置擷取，不以命令字串後綴剝除輸出；echo 前 human 輸出不納入正常 stdout。
- command RX 起點只在 OS 接受寫入時記錄；WouldBlock 與未接受的 human 位元組不形成 TX 證據。agent 換行結束當前 raw human 半行後，不再污染下一筆命令。
- 無 echo 且判定窗口沒有可信 human TX 時，保留尾端最後一行 prompt 的相容完成；只有 human 交錯且無 echo 時改回 `PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留輸出且不送救援鍵、不自動重送。
- i294 第一階段尚未處理 issue 262 的 prompt 身分與 issue 271 的同行輸出裁切，交由 i294-i2。
