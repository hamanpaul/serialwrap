## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | `checked_add`、RAII 名額回收與逐 job `catch_unwind` 均保留。 |
| 2 | Medium | 已修 `f150a24` | 0 | 正式路徑仍用 `start_fallible`；事件寫入失敗不更新 counter／cooldown。 |
| 3 | Medium | 已修 `f150a24` | 0 | capture 分類仍依 `foreground_busy()` 發布。 |
| 4 | Medium | 已修 `f150a24` | 0 | 首次回應前已設定 `log_capture`。 |
| P7-71fe017-1 | Medium | 已修 `030d294` | 0 | 溢位分支仍保留 Child、管線及 stdin，待子行程結束後釋放。 |
| P7-71fe017-2 | High | 已修 `7e2da7a` | 0 | `boot_done_regex` 判定保留；只清 quiet，不清 reconfirm pending。 |
| P7-71fe017-3 | High | 已修 `8134806` | 0 | 經 `b93849e` 合入；折行回顯剝除依 P5 裁決調整，正式命令路徑仍有呼叫。 |
| P7-71fe017-4 | Medium | 已修 `afebff0` | 0 | health 與 rollback 仍採不同掃描語意；`0baf6e8` 的正式路徑回歸保留。 |
| P7-27b5bf4-1 | High | 已修 `20c9454` | 0 | capture 頂層與巢狀計數仍共用快照。 |
| P7-030d294-1 | High | 已修 `1677489` | 0 | 分派前仍核對現行 run 的 `(epoch, fsm_gen)`。 |
| P7-9065f14-1 | Medium | 未修 | 3 | evidence map 仍比凍結集合多出 `l15-delayed-reply`；未見豁免裁決。 |
| P7-c5f0fc9-1 | High | 已修 `7bb9cfd` | 0 | 列名固定對應必要集合，並核對 selector。 |
| P7-c5f0fc9-2 | High | 已修 `7bb9cfd` | 0 | 非測試列空證據仍為 INCOMPLETE；擁有者證據缺 hash 仍為 BLOCKED-OWNER。 |
| P7-c5f0fc9-3 | Medium | 未修 | 2 | [必要 artifact 集合仍未驗證](scripts/rust/p7_gate_report.py:296)；重播 `artifacts:{}`，`verify_report()` 仍回 `[]`。 |
| P7-c5f0fc9-4 | High | 已修 `7bb9cfd` | 0 | 未映射節點的 failure／error，以及異常 pytest 結束碼，仍會阻擋。 |
| P7-c5f0fc9-5 | High | 未修 | 2 | [必要節點仍採信當輪 collected](scripts/rust/p7_ab_report.py:130)；清單與結果只有 `[shell]`，仍回 `PASS, 1 testcase`。 |
| P7-c5f0fc9-6 | Medium | 已修 `29aaa7b` | 0 | 預設輸出父目錄仍先建立。 |
| P7-7bb9cfd-1 | Medium | 未修 | 1 | [外部路徑仍取 basename](scripts/rust/p7_ab_report.py:70)；`/tmp/p7-run-1/a.xml` 仍被記成 `a.xml`，無法定位原檔。 |

## B 新發現

**P7-cd7acf0-1 [High]** [scripts/rust/p7_ab_report.py:178](scripts/rust/p7_ab_report.py:178)、[scripts/rust/p7_case_coverage.py:79](scripts/rust/p7_case_coverage.py:79) — 新增回歸與 coverage 判定沒有核對完整必要節點。

獨立重播：回歸階段的 JUnit 只含無關的 `tests/unrelated.py::test_ok`、rc 為 0，`judge_phase()` 即回 `PASS, 1 testcase`。coverage 的 platform-matrix 在 A／B 都只有 `[shell]` 時，也回 `PASS`；重複節點同樣接受。預期均應為證據不足。這是新增判定路徑的缺口，與 A 中既有的 `judge_side()` 問題分開。

確切修法：各階段及各 case 都須對照預先凍結的完整 nodeid 集合，逐項要求恰一次成功；拒絕缺漏、重複及替代節點，並補上述反例。

**P7-cd7acf0-2 [High]** [crates/sw-daemon/tests/cap_windows_com_p7.rs:230](crates/sw-daemon/tests/cap_windows_com_p7.rs:230) — wire／WAL oracle 會放過不同的原始 bytes。

具體輸入：peer 為 `b"pw\n\xff"`、WAL 為 `b"\xfe"`。移除 secret 後應因 `ff != fe` 失敗，但兩邊經 `String::from_utf8_lossy()` 都變成 `�`，斷言通過，違反逐位元組一致的驗收要求。

確切修法：直接比較 `expect` 與 `wal` 的 byte slice；lossy 字串僅供失敗訊息顯示，並加入此反例。

**P7-cd7acf0-3 [High]** [crates/sw-daemon/tests/cap_windows_com_p7.rs:268](crates/sw-daemon/tests/cap_windows_com_p7.rs:268) — quiet 零 TX 案例漏掉 banner 到 quiet 狀態可見之間的 TX。

具體反例：錯誤實作收到 banner 後先送一個 `\n`，再發布 quiet 狀態。測試等到狀態可見才取 `peer.mark()`，因此排除這筆違規 TX；後續 submit 被拒且不再送 bytes，測試仍會通過。末尾 wire／WAL 比對也不會拒絕兩邊都有記錄的違規 TX。

確切修法：在 `peer.emit(BANNER)` 前建立取樣起點，涵蓋整個 banner 後窗口；補入「發布 quiet 前送一 byte」必紅的回歸。

**P7-cd7acf0-4 [Medium]** [scripts/rust/p7_gate.sh:154](scripts/rust/p7_gate.sh:154) — 新 gate 使用尚未登記的必要集合，成功執行也無法放行。

目前七個 Linux 測試列缺對應的 `p7_gate:*` 集合，涵蓋黑箱 A／B、先前切片、L1.5 與全套 pytest。以全 passed 的 `contract_blackbox p7 B` 報告重播，實際得到 `INCOMPLETE：鎖中無 p7_gate:contract-blackbox-p7-b.linux`。

確切修法：依各列正式 argv，以 `regen_lock.py --l1-add-only` 登記完整 nodeid 與 selector；不可借用較小集合。

**P7-cd7acf0-5 [Medium]** [scripts/rust/p7_gate.sh:132](scripts/rust/p7_gate.sh:132) — Windows COM 呼叫在 runner parser 階段失敗。

實際執行該 argv 的解析階段，回覆 `win-test.sh: 未知參數 --test`、exit 2；三個 COM 案例完全不會啟動。`win-test.sh` 只在 `--` 後接受 Cargo 參數。

確切修法：改為 `win-test.sh --caps windows_com --package sw-daemon -- --test cap_windows_com_p7 --features test-fakes`，同步修正新增執行程序，並加入 argv 解析回歸。

**P7-cd7acf0-6 [Medium]** [crates/sw-daemon/tests/cap_windows_com_p7.rs:241](crates/sw-daemon/tests/cap_windows_com_p7.rs:241) — 三案平行獨占同一 COM pair，缺少互斥。

具體失敗情境：Windows host 設定 `COM20:COM21`、至少兩個測試執行緒，修正上一項 argv 後執行三案。三者都直接獨占開啟 COM21；collector 一次選取全部 `cap_windows_com__`，沒有設定單執行緒。第一案尚持有 handle 時，其他案開啟即失敗。此為依正式呼叫路徑與 `share_mode:0` 得出的靜態結論，尚未原生實跑。

確切修法：以共用 mutex 鎖住整個 COM pair 測試生命週期，或由正式 runner 明確序列化三案。

驗證：31 個報告器測試案例以記憶體檔案介面直接執行通過，另行重播上述判定反例。契約檢查、cfg 邊界、shell 語法及 `git diff --check` 通過；policy check 為 25 pass／0 fail／2 warn。未跑全套 pytest、Rust 或原生 Windows 測試；未修改檔案，HEAD 仍為 `cd7acf0`，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正

