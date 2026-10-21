---
type: fix
scope: rust-stage1
---

Stage 1 出口收尾第 2 輪：黑箱 A/B 腳本可於腳本模式執行、A 輪不再要求 release-manifest、必要清單改為正規化形式；P4 證據登記拒絕髒樹；daemon EIO 回 LOGIN 語意與 POSIX `current-release.json` 路徑修正；L15 延遲回覆情境補 READY 等待，runner 對未解析樣板 fail-closed；Windows COM 閘門改讀真機 `sequence.json` 佈局。未放寬任何斷言，凍結鎖未變動。
