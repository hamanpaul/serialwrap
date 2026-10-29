## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | `checked_add`、RAII 名額回收及逐 job `catch_unwind` 保留。 |
| 2 | Medium | 已修 `f150a24` | 0 | 正式路徑仍用 `start_fallible`；寫入失敗不推進 counter／cooldown。 |
| 3 | Medium | 已修 `f150a24` | 0 | capture 分類仍依 `foreground_busy()` 發布。 |
| 4 | Medium | 已修 `f150a24` | 0 | 首次回應前先設定 `log_capture`。 |
| P7-71fe017-1 | Medium | 已修 `030d294` | 0 | 溢位路徑仍持有 Child 與管線，stdin 保留至子行程結束。 |
| P7-71fe017-2 | High | 已修 `7e2da7a` | 0 | `boot_done_regex` 判定保留；只清 quiet，不清 reconfirm pending。 |
| P7-71fe017-3 | High | 已修 `0a7d092` | 0 | P5 修正已由 `afebff0` 合入；折行回顯剝除保留。 |
| P7-71fe017-4 | Medium | 已修 `afebff0` | 0 | health 與 rollback 採不同掃描語意；`0baf6e8` 的正式路徑回歸保留。 |
| P7-27b5bf4-1 | High | 已修 `20c9454` | 0 | capture 頂層與巢狀計數共用快照。 |
| P7-030d294-1 | High | 已修 `1677489` | 0 | 分派前核對現行 run 的 `(epoch, fsm_gen)`。 |
| P7-9065f14-1 | Medium | 未修 | 2 | evidence map 仍比凍結集合多出 `l15-delayed-reply`；登錄 P7-BU-10 並非豁免。 |
| P7-c5f0fc9-1 | High | 已修 `7bb9cfd` | 0 | 固定列名對應集合，並核對 selector；替代集合及錯誤 argv 均被拒絕。 |
| P7-c5f0fc9-2 | High | 已修 `7bb9cfd` | 0 | 非測試列空證據改為 INCOMPLETE；擁有者證據缺 hash 改為 BLOCKED-OWNER。 |
| P7-c5f0fc9-3 | Medium | 未修 | 1 | HEAD 與已列 artifact hash 已核對，但 `artifacts:{}` 仍使 `verify_report()` 回 `[]`；[292 行](scripts/rust/p7_gate_report.py:292) 未要求必要 artifact 識別。須先驗證必要 artifact 集合非缺漏，再逐項重算。 |
| P7-c5f0fc9-4 | High | 已修 `7bb9cfd` | 0 | 未映射節點的 failure／error 及異常 pytest 結束碼均阻擋。 |
| P7-c5f0fc9-5 | High | 未修 | 1 | 必要節點仍完全採信當輪 collect-only 清單。[106 行](scripts/rust/p7_ab_report.py:106)：清單與 JUnit 都只含 `[shell]`，實測仍回 `PASS, 1 testcase`，漏掉 `[prpl]`、`[bcm]`。須比對預先凍結的完整 nodeid 集合，拒絕縮小清單。 |
| P7-c5f0fc9-6 | Medium | 已修 `29aaa7b` | 0 | 預設輸出父目錄會先建立。 |

## B 新發現

**P7-7bb9cfd-1 [Medium]** [scripts/rust/p7_ab_report.py:53](scripts/rust/p7_ab_report.py:53) — repo 外的證據路徑被截斷，報告無法定位原檔。

輸入 `--junit-a /tmp/p7-run-1/a.xml` 時，新增的 `_rel()` 實際只輸出 `a.xml`，沒有搬移原檔。報告宣稱的 repo 相對位置因而找不到證據；`/tmp/p7-run-2/a.xml` 也會被記成同一路徑。這是 `9bd0c03` 引入的來源資訊遺失。

確切修法：將外部證據保存至明確的 run 目錄，再記錄可解析的相對路徑與 hash；或明確拒絕 repo 外輸入，不得以 basename 代替來源路徑。

驗證：16 個 gate 回歸函式及 1 個 A/B 回歸函式以記憶體檔案介面執行通過；上述缺口另行重播確認。契約檢查通過，policy check 為 25 pass／0 fail／2 warn，shell 語法檢查通過。未跑全套 pytest 或 Rust 測試；未修改檔案，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正