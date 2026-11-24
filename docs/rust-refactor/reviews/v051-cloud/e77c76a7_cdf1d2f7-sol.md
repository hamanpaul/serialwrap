> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

無；本次指定無前輪報告。

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 處理六項 finding，取得紅綠燈或附證據說明不成立 | 完成 `ddf7ef9b`、`774dac00`、`cdf1d2f7` | 分段同步、pending 前置及換行 prompt 測試已補；[驗收報告](docs/rust-refactor/evidence/v051-i306-590e7bbc/acceptance-report.md:15)與紅綠燈索引逐項列出證據。本輪新引入的問題另列 B。 |
| 2. 不放寬既有斷言、不刪除或改名測試、不擴大範圍 | 完成 `ddf7ef9b` | 本輪 diff 未見既有測試被刪除或改名；pending 斷言改為明確驗證失敗保留、成功清除。RPC 測試刺激方式的變化另列 B 的 Low。 |
| 3. 回報 finding 對照與可追溯 commit 的驗收結果 | 完成 `774dac00`、`cdf1d2f7` | 驗收報告列出各 finding 的處置與受測程式候選 `590e7bbc`；證據檔 SHA-256 全部核對通過。Windows 原生收集與其他限制已列入 SPEC-GAPS。 |

本次獨立定向驗證：`sw-core` 的 `self_test_safety` 為 **25 passed**；`sw-daemon` 同檔為 **6 passed**。未執行完整 pytest、Docker 或 bundle 測試。

## B 新發現

**V051-cdf1d2f-1 [High]** — [op_runner.rs:1105](crates/sw-core/src/op_runner.rs:1105)：行界本身的首個 regex 命中會使 self-test 提早失敗。  
具體情境：設定 `prompt_regex: '(?m)^.*'`，DUT 已完整送出 marker，接著回傳 `\r\nroot@t:/# `。新搜尋從 CRLF 開始，第一個 match 是 `\r`，其結尾仍在 marker 結尾之前；第 1111 行立即回傳 `false`，不再搜尋同一批資料中的 `root@t:/# `，也不等待稍後到達的 prompt。舊搜尋從 marker 結尾開始，可命中該 prompt。本次以相同 regex 與切片實測確認首個 match 的結尾位置；程式碼的 regex 搜尋採第一個 match。結果是有效 probe 被回報 `probe_ok=false`，無法 confirm READY。  
**修法：**保留行界上下文，但跳過結尾未超過 marker 的 match，繼續搜尋或等待，直到找到符合條件的後續 match、逾時或取消；補同 chunk 與分 chunk 的 actor／RPC 回歸，並確認無 prompt 時仍不能成功。

**V051-cdf1d2f-2 [Low]** — [self_test_safety.rs:321](crates/sw-daemon/tests/self_test_safety.rs:321)：既有 RPC 完整 payload 回顯情境原本分兩次注入，本輪改為一次注入。雖然 core 測試已補決定性的分段同步，RPC 測試本身不再送出分段輸入，因此無法再觀察該入口的分段情境。  
**修法：**保留 RPC 的兩段注入，並以可觀察同步點確認第一段已處理後再送第二段；維持現有 pending 與 evidence 斷言。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正