## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 發布綁定世代的接線保留；正式 RPC 逾時後重試及後續拔插兩項回歸均通過。 |

## B 新發現

**P8-b3ffd2a-1 [High] [crates/sw-daemon/tests/handoff_stop.rs:1341](crates/sw-daemon/tests/handoff_stop.rs:1341) — 凍結測試撤掉正式 CLI 交接路徑，改由測試自行交接並構造通過資料。**

原測試呼叫 `ensure_released_or_rehandoff`，驗證產品產生的交接紀錄；本輪改成直接送 daemon RPC，再固定回傳 `vec![HandoffLink { pid: Some(old_pid) }]`。因此後續筆數與 PID 斷言成為恆真，CLI 未執行交接也不會使此節點失敗。

具體反例已用記憶體測試副本驗證：讓正式 CLI 交接函式直接回 `Ok(())`、不送 RPC、不填 chain。起點未注入故障時通過；注入後起點 **1 failed**（chain 為空），候選卻 **1 passed**。這證實本輪削弱既有回歸判準。

**修法：**維持 crate 依賴方向，透過 CLI 子程序或獨立整合測試恢復正式 update／rollback 交接路徑；驗證產品實際產生的交接結果，不由 fixture 構造。上述故障注入須重新導致紅燈。

已逐筆審查本輪唯一提交 `b3ffd2a0`。正常定向測試 **12 passed、0 failed**；契約與依賴方向檢查通過。未跑完整 pytest、Docker／bundle 或 Windows 原生驗收；未修改工作樹檔案，建置產物位於指定 `/tmp` 目錄，工作樹仍乾淨。

## 既有碼待辦（不計入結論）

沿用前輪：bind RPC 逾時後，若未重試就直接拔插，仍有持久化缺口。RPC 的 `?` 位於 [session_manager.rs:6609](crates/sw-core/src/session_manager.rs:6609)，`Unreleased`／`Binding` 提交仍在其後。本輪未修改，未另行實測。

## 對裁決的異議（不計入結論）

無。

結論：需修正