---
type: fix
scope: test-infra
---

Spike 1 控制線隔離量測修正（S0-SPIKE1-REMAINING）：量測前改為把所有已開埠埠位的 RTS／DTR 全部拉低。pyserial 開埠預設 RTS／DTR 為 True，原先只拉低 COM20／COM22，未拉低的 COM23 會把 COM22 的 CTS／DSR／CD 釘在 true，使隔離比較無法偵測 console 端的洩漏。隔離判定改為基線與各 DUT 階段，兩個 console 埠的輸入線皆須全低，不再以「與基線相同」比對。新增 B→A（COM23→COM22）反向正向對照，確認兩個 console 埠的輸入線皆可觀測，缺此方向時不得判為隔離通過。報告新增 `outputs_cleared_before_baseline` 欄位與 `console_pair_positive_control` 的 `reverse_rts_only`／`reverse_dtr_only`。

證據登錄：Windows 原生重跑（DUT-side COM20:COM21、Console-side COM22:COM23）三項皆 PASS 後，將報告存入 `evidence/stage0/owner/p2-t18/spike1-remaining.json` 並登錄 `S0-SPIKE1-REMAINING` 為 present（sha256 已填）。證據索引相關測試的暫存根目錄改為一併複製既有 present 項目的 artifact，只調整夾具，不改動任何斷言。
