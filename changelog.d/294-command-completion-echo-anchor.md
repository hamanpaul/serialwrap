---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，多次、混用、無局部證據及其他形態均按無 echo 規則處理。`completion_regex`／marker producer 維持既有結果。無 echo、窗口內 human submit 開始計數未變且尾端 prompt 合規時，維持相容完成。
- 無 echo 且窗口內有 human submit 時，不採相容完成；等待上限到達後按統一 timeout 規則回 `PROMPT_TIMEOUT`／`partial=true`／`recovery_action=NONE`，stdout 取結算 snapshot partial，救援 TX 為零。
- 同一 actor 的 bridge epochs 共用 dispatch／decision gate 與 human 計數域。command TX 邊界、completion snapshot、stale recheck、timeout 判讀都讀取 session-wide submit 開始計數、human TX generation、`human_in_flight`；RX window 不另起人類計數。`pending_human` 獨立記錄 prompt 是否尚未返回，只由 human TX 所屬 epoch 的 prompt 清除；接管時仍未結案就保守延續，已在舊 epoch 結案則不污染新 epoch。取得鎖固定為 session gate state → RX inner；snapshot 在同一鎖區段擷取 RX bytes 與 human state，等待 condvar 前釋放 gate，因此不形成循環鎖序。`ManagerExec` 依 effect 順序追蹤 RX；`AdoptBridge` 後的 `HumanTx` guard 註冊在新 RX，並於 test-fakes／debug 驗證 guard RX 與 pump RX 指標相同。每次 actor dispatch 完成 transition 與 handoff deferred replay 後，才依實際 `Effect::HumanTx` 建 guard；沒有該 effect 時不改動在途訊號。guard 隨 effect、pump、writer job 保留至 report。
- timeout 先發布 decision intent；human producer 僅在 gate 內非阻塞 `try_send`，queue 滿時釋放 gate 並在 gate 外等容量訊號。writer 以 RAII 在正常退出或 panic unwind 時發布 closed 並喚醒容量等待者；pump／HumanLine 醒來後按既有拒絕路徑釋放 guard，worker panic receipt 維持原樣。決策不依賴 mutex FIFO；從 timeout 開始讀取四項訊號，最多等待一個進行中的非阻塞 enqueue 區段、一個 dispatch、及前一個 timeout 讀取區段，不等 writer 容量或 report。無 human 判讀後，晚到的 human job 排在第一個救援鍵後；救援鍵可於讀取完成後等容量。actor 不等待救援 gate 或 writer。
- 所有 timeout 入口（含 30 秒靜默 chunk）讀取同一 session 計數域中的 submit 開始計數、`human_in_flight`、`tx_submit_pending` 與 human TX generation；任一訊號成立即回 human timeout。無 human timeout 的完整結果與 stdout 維持 `9af187ca` 基線逐位元組相同。保留既有狹義重複 echo 相容規則；i294 第一階段不涵蓋 issue 262 prompt 身分與 issue 271 同行 stdout 裁切。
