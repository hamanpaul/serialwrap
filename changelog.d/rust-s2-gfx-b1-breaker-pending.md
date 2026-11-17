---
type: fix
scope: rust-refactor
---
- Slice 2.1 正式執行改將斷路器快照寫入 run 目錄，判定合併讀取已提交與本次待提交快照；新增四個 slice gate 完成後的快照收尾命令，並在正式執行前拒絕不乾淨的工作樹。
