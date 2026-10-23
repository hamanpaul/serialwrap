# 待 Codex gpt-6.1-sol 補審清單

Codex 帳號額度於 2026-10-21 約 18:28（台北）用完，恢復時間 2026-10-27 14:32。擁有者裁決（2026-10-21）：額度恢復前只推進非 codex 的 Task，保留原指定模型；期間 commit 僅經 Claude Sonnet（max）單審，**須於額度恢復後由 gpt-6.1-sol（medium）補審**，補審發現的問題依一般修正流程處理。

| 分支 | commit | Task | 單審者 | 補審狀態 |
|---|---|---|---|---|
| feature/rust-refactor-p5 | 8e39875 | P5-T0b Slice 2.1 必要案例凍結與斷路器 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 149e420 | P9-T2 Stage 3 共用測試替身 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor-p9a | 6075648 | P9-T3 A/B manifest 載入與凍結 | Sonnet max | 已補審（sol 2026-10-23）；修正 commit 20915da |
| feature/rust-refactor-p9a | 24f42d0 | P9-T7B L1.5 A/B 差分 | Sonnet max | 已補審（sol 2026-10-23）；修正 commit 4de1c36 |
| feature/rust-refactor-p9a | d6c08e0 | P9-T7 A/B 絕對契約與差分判定 | Sonnet max | 已補審（sol 2026-10-23）；修正 commit 99f4606 |
| feature/rust-refactor-p9a | 5a038a1 | P9-T14 Stage 0～3 出口證據索引 | Sonnet max | 已補審（sol 2026-10-23）；修正 commit c64e23e |
| feature/rust-refactor-p9a | cbf40e9 | P9-T15 OD-09 凍結結果的 Stage 3 消費端與唯讀核對 | Sonnet max | 已補審（sol 2026-10-23） |

p9a 補審殘餘待擁有者裁決（2026-10-23，未於上列修正處理）：(1) 24f42d0：`foreign_holders` 納入比對後，相鄰 run 因掃描時序出現或消失 holder 項目，實測約 45% 的相鄰配對非乾淨，需裁決是接受並以確定性案例覆蓋，或另定容忍規則（須獨立審閱）；(2) 24f42d0：同一 tty 群組內不同 PID 可能共用佔位名，持有者數量仍保留但「是否同一行程」的關聯會被遮蔽。

後續新增的單審 commit 一律追加於此表。

## 額度用完後未完成、需重新實作（原 implementer 為 codex gpt-6-luna）

- P5-T0c、T1～T9（P5 工作樹有未提交的中途改動，恢復後自 T0c 依序重跑）。P5-T0 的 sol 審查已完成，但其實作尚未獨立 commit，恢復時一併核對。
- P9：implementer 為 luna 的 Task（T5B、T6、T14B 等）。
- Stage 1 出口收尾：第 1～2 輪修正未執行；改以非 codex 實作者（agy gemini／haiku）重跑。
