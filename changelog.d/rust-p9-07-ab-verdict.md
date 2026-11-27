---
type: fix
scope: stage3
---
新增 A/B 絕對契約與差分判定：判定表依序為安全違規或已核實產品失敗→FAIL、suite INFRA→INFRA、比較器完整性不足→INFRA，再依各層規則處理 baseline invalid、逐案比較與長跑 gate；FAIL 優先時 notes 仍保留失敗來源、所有完整性診斷及 INFRA 線索。單獨的未核實失敗導致 comparator INFRA，不升格為產品 FAIL，也不從 run rc 推導失敗。L3 一般逐案比較使用 `kind: cases` 與 required case 完整性，不讀取長跑 gate；歷史長跑才使用 `kind: longrun` 與 P2 gate 輸出。依 `ab_participation` 過濾 `rust_only` 差異並檢查 B 側絕對結果；`case_changes` 仍展開所有 scope（包括 rust_only），跨 change 重複 case 回報比較器錯誤。L2 保留 sentinel、BC、required case 與安全不變量判定；P2 release gate 僅讀 JSON，不重算門檻。
