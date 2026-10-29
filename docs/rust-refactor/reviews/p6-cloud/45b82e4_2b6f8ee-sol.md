## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | High | 已修 `bd62919` | 0 | 回放結算前停放命令、Q → B 順序及恰送一次斷言均保留；本輪未變。 |
| 2 | Medium | 已修 `bd62919` | 0 | raw owner、UART TX 與本地回顯負對照保留；本輪未變。 |
| 3 | High | 已修 `bd62919` | 0 | release 推進 close 世代、拒絕舊世代晚到連線的防線保留；本輪未變。 |
| P6-e9b5c61-1 | High | 已修 `7370b8f` | 0 | 各 SelfTest 保存並結算自己的 suspend 憑證；本輪未變。 |
| P6-e9b5c61-2 | High | 已修 `7370b8f` | 0 | 取消停放命令的兩條退出路徑仍結算 registry 在途指標；本輪未變。 |
| P6-e9b5c61-3 | Medium | 已修 `0c20ee8` | 0 | 直接執行原指紋斷言，2.1／2.2 均通過。 |
| P6-e9b5c61-4 | Medium | 未修 | 9 | 直接執行[原稽核](tests/test_l1_judge.py:882)仍失敗：40 列皆屬 `s21_core`。OD-53 僅豁免明確執行 ignored 能力測試的集合，未豁免這些剩餘列。 |
| P6-c6ddda1-1 | Medium | 已修 `bf61e3e` | 0 | 直接執行原單元斷言通過：同份報告重複為 `INSUFFICIENT`，跨集合各一列 passed 為 `PASS`。 |
| P6-c6ddda1-2 | Medium | 已修 `464adec` | 0 | 移除 R-21 本機路徑的修正保留；相關檔案本輪未變，未重跑政策檢查。 |
| P6-bf61e3e-1 | Medium | 已修 `e0aab47` | 0 | 核准前修訂鏈前綴與完整後續斷言保留；本輪未變。 |
| P6-b64dad9-1 | High | 已修 `2b6f8ee` | 0 | [判定器](scripts/rust/s22_gate_check.py:279)改解析結構化證據。記憶體負對照確認：缺綠燈、綠燈失敗，舊版均誤判 `PASS`，新版均為 `INSUFFICIENT`；現行報告為 `PASS`。 |

## B 新發現

NO FINDINGS。

已逐一檢視 `f014902`、`2184235`、`2b6f8ee`，無 merge commit。跨切片清單追加符合現行 `cases.yaml`，直接執行一致性斷言通過。

正式 `judge_parity()` 的九組負對照均回 `INSUFFICIENT`，包含缺區塊、缺綠燈、綠燈失敗、非零退出及必要欄位缺漏。

驗證限於靜態審查與直接函式呼叫；未建置 Rust、未跑完整 pytest／E2E。全程未修改檔案，未接觸 production daemon 或實體 UART；工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正