---
type: fix
scope: rust-s2
---
清除整合線重複的 console 事件分支、未使用 import 與無呼叫端的 P7 骨架函式／Slice 2.1 巨集；保留骨架測試引用的 `SelfTestFinal` 型別。安全黑箱測試維持填滿佇列期間的 RX 活動，force-spawn 互斥測試先確認第一個 daemon 持有測試 PTY，再啟動第二個；既有斷言不變。
