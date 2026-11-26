---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，多次、混用、無局部證據及其他形態均按無 echo 規則處理。`completion_regex`／marker producer 維持既有結果。無 echo、窗口內 human submit 開始計數未變且尾端 prompt 合規時，維持相容完成。
- 無 echo 且窗口內有 human submit 時，不採相容完成；等待上限到達後按統一 timeout 規則回 `PROMPT_TIMEOUT`／`partial=true`／`recovery_action=NONE`，stdout 取結算 snapshot partial，救援 TX 為零。
- 事件預判採保守超集合，涵蓋直接 raw input、完成後 replay／backlog（含略過首項後送出後項）、handoff 解凍時先重放的 deferred event、OpFinished、lease expiry 與 request 收尾；不確定時設 guard。預先 guard 只增加 `human_in_flight`，transition 沒有 HumanTx effect 時立即 drop、不增加歷史 submit 計數；effect executor 接手實際 job 時才升級 guard 並增加 submit 計數。缺少 guard 在 test-fakes／debug 會 panic，release 使用 `begin_human_submit` 後備並只記一次 warning。guard 隨 job 傳入 pump，持有到 writer report，涵蓋成功、Fence 拒絕、取消、WouldBlock 與 channel 收尾。
- 所有 timeout 入口（含 30 秒靜默 chunk）在鎖內檢查 submit 開始計數、`human_in_flight`、`tx_submit_pending` 與 human TX generation；任一證據成立即回 human timeout。命令 TX 前已排隊者只要決策時仍在途也走 human 路徑；分流不依事件時間或 snapshot 過濾。無 human 時共享 submit gate 維持至第一個救援鍵提交到 OS；無 human timeout 的完整結果與 stdout 維持 `9af187ca` 基線逐位元組相同。保留既有狹義重複 echo 相容規則；i294 第一階段不涵蓋 issue 262 prompt 身分與 issue 271 同行 stdout 裁切。
