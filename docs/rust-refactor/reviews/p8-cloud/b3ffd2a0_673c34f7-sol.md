## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | 綁定世代發布接線及兩項回歸測試均保留，本輪未變動；沿用前輪實測證據。 |
| P8-b3ffd2a-1 | High | 未修 | 1 | `673c34f7` 移除假鏈結與相關斷言，但 [handoff_stop.rs:1350](crates/sw-daemon/tests/handoff_stop.rs:1350) 仍由測試直接送 RPC，未恢復正式 CLI 交接路徑。 |

`P8-b3ffd2a-1` 的反例仍成立：若正式 `ensure_released_or_rehandoff` 直接回 `Ok(())`，不送 RPC、不填交接鏈，此節點仍自行交接並啟動繼任者，無法偵測 CLI 缺陷。本輪刪除恆真斷言，尚未補回原有覆蓋。

確切修法：透過 CLI 子程序或獨立整合測試執行正式 update／rollback 交接，驗證產品實際產生的交接結果，並確認上述故障注入會使測試失敗。未找到適用的豁免裁決。

## B 新發現

NO FINDINGS。

已逐筆審查範圍內唯一提交 `673c34f7`，未發現獨立的新缺陷；上述未修問題沿用原編號。

定向實測 `handoff_then_successor_never_double_held`：**1 passed、0 failed，18.68 秒**。建置產物位於指定 `CARGO_TARGET_DIR`；未修改檔案，工作樹仍乾淨。未跑完整 pytest、Docker／bundle 或 Windows 驗收，未接觸 production daemon、正式設定／狀態目錄或實體 UART。

## 既有碼待辦（不計入結論）

沿用前輪：bind RPC 逾時後，若未重試便直接拔插，仍有持久化缺口。[session_manager.rs:6609](crates/sw-core/src/session_manager.rs:6609) 的 `?` 會提前返回，`Unreleased`／`Binding` 提交仍位於其後。本輪未修改，未另行實測。

## 對裁決的異議（不計入結論）

無。

結論：需修正