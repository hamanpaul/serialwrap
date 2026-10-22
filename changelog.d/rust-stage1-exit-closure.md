---
type: fix
scope: rust-stage1
---

Stage 1 出口收尾第 2 輪：黑箱 A/B 腳本可於腳本模式執行、A 輪不再要求 release-manifest、必要清單改為正規化形式；P4 證據登記拒絕髒樹；daemon EIO 回 LOGIN 語意與 POSIX `current-release.json` 路徑修正；L15 延遲回覆情境補 READY 等待，runner 對未解析樣板 fail-closed；Windows COM 閘門改讀真機 `sequence.json` 佈局。未放寬任何斷言，凍結鎖未變動。

Stage 1 出口收尾修正輪：黑箱 L1 報告 deployed_content 於 B 輪恆為 null 之根因修正（contract_blackbox 共用已載入 plugin；ab_blackbox B 輪明定 SERIALWRAP_DAEMON_EXE；Windows current-release 指向改為相對 bin）；Windows 黑箱 staging 補 owner_gated 並加入間接 import 覆蓋測試；doctor A/B 比對前於隔離 namespace 輪詢殘留 daemon；bc05 三拓樸改 A/B 雙輪（test oracle 變更，待非實作者審查）。凍結鎖未變動，未放寬任何斷言。
