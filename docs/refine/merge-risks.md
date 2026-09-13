# #198 整合風險與回復方式

下表為 2026-09-12 文件階段的歷史風險；2026-09-13 已產生 production 候選，
最新狀態見下方更新。採用方案不代表真機放行。

| 項目 | 影響與邊界 | 後續驗證／責任 |
|---|---|---|
| Ownership coverage | file transfer contention、跨 recovery epoch 及完整 reconnect 尚缺本輪補強證據；不宣稱整體無競態 | #198 後續實作者增加 barrier／fake bridge 正反例，root 核對；重現缺陷列 P1 |
| 診斷相容性 | 新 trace 尚未實作，logger／errno／欄位白名單是否符合契約尚無新功能測試 | #171 實作與 root 驗收；既有 stderr 可能包含原始 path，本輪未改其契約 |
| Endpoint 去識別 | 雜湊可降低直接洩露，但不是匿名化保證；可猜測 endpoint 仍可能被比對 | 診斷文件按最小必要欄位設計，不承諾強匿名性；實作時驗證敏感值不直接輸出 |
| Reload race | template／target、inflight attach 與 generation 的新規則尚未實作；測試不能由文字取代 | #182 增加原子交換／同名修訂／stale candidate／無關 session 不變的測試；RELEASED／FLASHING 不被撤銷 |
| 平台與真機 | 本輪不驗證 Windows holder detection、真板長時行為或 Cloudflare tunnel | #199／#182／#197 分開做平台或真機驗收，不能把空 `/proc` 結果當排他成功 |
| 順位與效益 | 排序根據現有事故與 source，未取得使用頻率、耗時或收益比較 | 下次操作以有界樣本記錄定位、重試、人工協調及維護成本，再調整順序 |

回復方式：文件提交均在 `docs/198-refine-review` 分支，可用後續修訂提交更正；不需重啟 daemon 或回復 UART 狀態。#198 編修前已在本地工作目錄保存原文；若需撤回外部文字，先核對 issue 最新版本以保留他人後續編輯，再套用所需更正。未刪除 WAL／runtime／既有 untracked 檔案。

## 2026-09-13 更新

- CLI trace 與 holder stat 已有候選、隔離測試及 Sol 審查；不再列為「尚未實作」。
  CLI trace 不處理 daemon sink／rotation；holder 的 missing-attribute 測試不是
  native Windows 成功證據，空 `/proc` 結果也不是外部 holder 排他證明。
- Ownership 的新 console raw grant 與 raw RX 檢查至實際 write 窗口均已由 root
  重現並修正，另補 deferred 回放、鎖外 I/O 與 FLASHING 優先驗證。可支持的範圍
  是本輪決定性操作序列；不外推外部 process、所有未盤點 callback、真板背壓或
  整個 recovery 生命週期的物理排他，詳見 [ownership 交付](198-ownership-delivery.md)。
- #166 尚無實作：指定 Sonnet 額度不足；Opus 審查也未能執行。替換指定模型須由
  使用者決定。Ownership Sol 審查遭平台中止亦未取得有效結果。
- #182 reload 與 #197 Cloudflare 真 E2E 繼續分票追蹤，見
  [後續安排與成效量測界線](198-followup-validation.md)。本地候選尚未部署。
- 回復方式：所有 production 候選仍留在獨立 feature/fix 分支；若否決，可不採納
  個別候選或在整合分支以明確反向提交修正，不需動 live daemon／UART／WAL。
