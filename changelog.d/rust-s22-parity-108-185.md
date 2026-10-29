---
type: fix
scope: acceptance
---
Slice 2.2 對照表依 rulings/p6.md 處理 #108／#185：#108 的 file 傳輸部分改列 P8，Windows TCP `attach_console` 不開 lease 改為 BLOCKED-OWNER（`slice22-owner-runbook.md` 新增 #108 節，LOCAL-WINDOWS-TODO）；#185 流程項以新增的 `docs/rust-refactor/slice22-exit-report.md` §#185（T0 `ae33e28` 紅燈紀錄與終態綠燈）為證據。`s22_gate_check.py` 新增 `PROCESS-EVIDENCE` 狀態，只限逐名核准的承接編號（#185），且報告段落須含 commit 與紅／綠燈紀錄，否則證據不足。
