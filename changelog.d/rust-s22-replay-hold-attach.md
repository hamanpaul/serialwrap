---
type: fix
scope: daemon
---
補 07 Task 10 `attach_interactive_owner_false_during_replay_hold` 的 daemon 級回歸：回放 hold 期間（前景已清、無 lease）attach 的第二個 console 回 `interactive_owner:false` 並開 lease（owner pending），回放結算後前景空閒、按鍵恰寫出一次（可達前置改由 owner console 的 human lease 在前景命令期間關閉）。
