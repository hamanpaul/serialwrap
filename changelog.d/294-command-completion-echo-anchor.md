---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，多次、混用、無局部證據及其他形態均按無 echo 規則處理。`completion_regex`／marker producer 維持既有結果。無 echo、窗口內 human submit 開始計數未變且尾端 prompt 合規時，維持相容完成。
- 無 echo 且窗口內有 human submit 時，不採相容完成；等待上限到達後按統一 timeout 規則回 `PROMPT_TIMEOUT`／`partial=true`／`recovery_action=NONE`，stdout 取結算 snapshot partial，救援 TX 為零。
- actor 每次先完成 transition 與同次 handoff deferred event 重放，再依實際輸出的 `Effect::HumanTx` 建立在途 guard；無 HumanTx effect 的 dispatch 不改動 `human_in_flight`。timeout 訊號讀取與 dispatch 區段透過公平 gate 線性化，不使用事件預判；guard 在 writer enqueue 成功時才增加 submit 起點計數，並隨 effect、pump、writer job 保留至 report。test-fakes／debug 缺 guard 會 panic，release 後備 guard 只 warning 一次；成功、拒絕、取消、WouldBlock 與 channel 收尾都以 RAII 清理。
- 所有 timeout 入口（含 30 秒靜默 chunk）在線性化區段讀取 submit 開始計數、`human_in_flight`、`tx_submit_pending` 與 human TX generation；任一訊號成立即回 human timeout。決策最多等候一個進行中的 dispatch；actor 只等候訊號讀取區段，不等待救援鍵或 writer。無 human 時共享 submit gate 維持至第一個救援鍵提交到 OS；無 human timeout 的完整結果與 stdout 維持 `9af187ca` 基線逐位元組相同。保留既有狹義重複 echo 相容規則；i294 第一階段不涵蓋 issue 262 prompt 身分與 issue 271 同行 stdout 裁切。
