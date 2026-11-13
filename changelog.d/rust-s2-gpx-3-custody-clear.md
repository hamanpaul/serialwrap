---
type: fix
scope: rust-s2
---
保管開埠進行中呼叫 `session.clear` 時，裝置在線即回覆 ATTACHING／`last_error=null` 並清零 reprobe；開埠工作結束後，僅在保管 handle 回報 `closed:true` 時交由 manager 重解析並重試一次，`closed:false` 維持 by_id 關閉未確認封鎖。
