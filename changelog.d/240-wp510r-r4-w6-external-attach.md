---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 9 項（設計第 5 版 §4 R4、§3.1.4 W6）把既有 session 的 #181 熱插再解析改走外部 attach 協定：
- W6 worker 先以預配 token 同步送 `BeginExternalAttach`（bypass 請求；凍結、RELEASED、有停止或已有開埠在途時拒絕，不開埠），actor 記下 token 後才以該 token 登記帳本紀錄 `Opening{Worker}`、owner＝該 session，再開埠、PROBE、rematerialize（同鎖更新 owner），最後存回 `Held{Session}` 並在鎖內 `try_send CustodyOffer`；送不出去時由 actor 的 `CustodyPoll` 計時器（500 ms）以 `PollCustody` 立即回饋補收。
- 停止者（release、bind、shutdown、拔除）經 actor 的 `ClaimForStop` 寫入紀錄的取消並一併凍結作業的 PROBE fence（零 TX）；紀錄尚未登記時寫入 tombstone，worker 登記即中止、不開埠。worker 於各檢查點關閉而不交出、以 `CustodyTerminal` 回報終態（closed:false 依 D1 iii 轉 CloseFailed 永久阻擋，release 依 T-1）。
- 移除 manager 對本 session 再解析作業的 release 等待與 bind 的 #26 等待迴圈：release 由 actor 依首次期限回 DRAIN_STALLED、遲到的成功回條完成 RELEASED 並落盤（P8-e25b69c-2）；bind 由 actor 的 BindWait 取消並等到終態。
- 既有 session 接手 parked 與 DETACHED-rebind 帶著的預開 handle 同樣先 `BeginExternalAttach`（parked 以既有 token），被接受後才 `claim_parked`／改 owner 並送 `CustodyOffer`；未接受時 parked 維持原狀。
- W6 等 offer 期間的 `session.clear` 比照 Python 不取消在途作業（回 ATTACHING），recover 與 reprobe 不另開第二個 handle，`DeviceAttached` 不提前認領。
- `CustodyOffer`、`CustodyPoll`、`PollCustody`、`BeginExternalAttach` 依 §3.1.7 完成 T14 凍結分類並補 DR-5(a)(d) 回歸；L1 凍結節點 #42、#57、#67 依 §6（H）只把在途查詢換成帳本紀錄。
