## A 前輪 finding 核對

無。

## B 新發現

**P8X-b837cfc-1 [High] [crates/sw-core/src/session_core.rs:3513](crates/sw-core/src/session_core.rs:3513) — 交接撤銷後，已成功的自動重探結果永久遺失。**

具體情境：COM0 為 `ATTACHED/PROMPT_UNAVAILABLE`，自動 probe 已取得成功結果；COM1 有不持 admission ticket 的背景 TX。交接先執行 `freeze_if_idle`，此時 COM0 收到 `LoginFinished(Ready)`，新增分支立即清除 `login_run` 並丟棄結果。接著 COM1 的 TX fence 超過 50 ms，交接回 `SESSION_BUSY` 並 thaw。

預期：交接撤銷後套用成功結果，COM0 進入 `READY`。實際：COM0 保留 `ATTACHED` 與舊錯誤，命令仍回 `SESSION_NOT_READY`，必須重新 probe；若後續持續有 RX，還會被重探的 idle 條件持續阻擋。Python 的結果回填與交接撤銷受同一把 manager lock 保護，沒有此結果遺失窗口。

確切修法：區分交接嘗試中的暫時凍結與已成立的凍結；暫存期間收到的 probe 結果，撤銷時套回，交接成立或正式 shutdown 才丟棄。補上「成功結果抵達暫時凍結期間、另一 session TX fence 逾時、撤銷後仍轉 READY」的回歸案例。

驗證限制：以上為靜態路徑確認；定向 Cargo 測試因指定 target 的 `.cargo-build-lock` 遇唯讀檔案系統而未能執行。凍結鎖確認僅新增集合，既有內容未變；全程未修改檔案。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正