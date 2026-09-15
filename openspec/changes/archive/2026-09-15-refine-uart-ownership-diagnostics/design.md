## Context

基準 main cf2aaba；先前盤點位於 docs/refine。使用者已批准依 #198 範圍開工，實作者為 Sonnet xhigh、Luna max、Copilot gpt-5.4 xhigh、agy gemini-3.8-flash high；Opus xhigh 與 Sol xhigh 審查。所有工作在獨立分支，root 合併及實測。

2026-09-13 接手更新：Sonnet xhigh 因週額度未開工的 #166，由使用者明確批准交予 Luna max。此批准不豁免指定審查或更動功能範圍。

2026-09-15 更新：使用者明確批准 Luna max 實作與兩個獨立 Sol xhigh 審查席次；另確認本機無 UART environment，實機驗證延後至有設備的發版前回歸／部署後驗收，不構成本輪本地開發與審查的硬門檻。技術取捨及測試分層由 root 負責，不要求使用者重新裁決既定實作。

## Goals / Non-Goals

2026-09-15 最後授權：使用者指定「由你審，把這個 goal 跑完」，由 root 接替
未完成的 ownership 審查及最終交付裁決，完成 PR／CI／merge／結案。
已完成的 Sol scoped 審查保留其範圍；原失敗席次不再作為等待門檻，不冒稱雙席 PASS。

目標是四個可獨立驗收切片。非目標是 generic ACL、daemon logging、reload、公開 tunnel 部署、安裝或操作現有 UART。#171 全票、#182、#197 維持獨立待辦。

## Decisions

- 傳輸偵測必須驗證工具真正可用，不以 command echo 的 substring 當成功；base64 優先、OpenSSL fallback，push/pull 一致。profile 可選 max_console_line_chars 表示命令 UTF-8 bytes（不含終止換行）；未設定維持既有 chunk 上限，設定後以實際 shell 樣板最壞開銷計算，過小 fail closed。505 僅為既有板量測與測試 fixture，不能由 platform 名稱套用。
- Ownership 先用 barrier/event 重現再修復。file.push/pull 與 command 操作不得 logical interleave；採 manager 鎖內 admission，忙時明確拒絕且不 TX，不新增外部 token API。每次操作持有原 session／bridge 身分，舊操作 finally 不得清除新 epoch 狀態。正常 command queue 多 client accepted 的契約保留。
- CLI trace 只增加 optional internal metadata 與專用 logger，不改 rpc_call response dict。endpoint resolution 一次產生 endpoint+source；不增加 probe/retry。errno 只屬最後主請求，TIMEOUT 不等於未執行。
- holder 僅對缺 st_rdev 使用 getattr(..., 0) 保守 fallback，保持既有 char device 比對。無 /proc 不能證明沒有 holder。
- 並行明確覆寫技能的單一 worktree 順序預設：各實作者獨立 worktree，session_manager 的傳輸參數、admission、holder 三個區塊分開編輯，root 唯一整合者。README 由 root 整合各自 docs/refine 使用說明。

## Risks / Trade-offs

- 真板 console、實際 Windows、Cloudflare 帳戶不由 mock 證明 → 列為獨立平台驗證，新增／對齊 regression 案例但不操作現有服務。本輪真 UART 測試明列 DEFERRED（no UART environment），不是 PASS；不可因此跳過可執行的離線測試，也不要求在這輪建立現場環境。詳細交付階段與證據欄位見 `docs/refine/198-followup-validation.md`。
- SHA-256 endpoint ID 僅減少 raw path 洩漏，不是強匿名 → 不記 params、command、response、owner、credential；既有 stderr error line 維持。
- 恢復交接較多共享狀態 → 用舊 callback 停住、重建 bridge 後釋放的決定性測試驗證；不能只用 source grep 宣稱安全。
- 額外工具 probe 增加少量傳輸控制命令 → 記錄 probe 次數與成本；不得降低 checksum／echo stall 判定。

## Migration Plan

維持預設 stdout/stderr；trace opt-in。profile 未設行長仍保持既有預設。程式以標準 PR 發布，這輪不自動安裝／重啟 daemon。回退為 revert 本 PR，不刪現場 state/WAL。OpenSpec 在測試及審查完成後 archive。

## Open Questions

無需使用者重複批准既定範圍。實作若發現超出上述共享資源邊界，回報 root 重裁，不靜默增加架構。
