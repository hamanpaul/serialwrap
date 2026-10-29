## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | High | 已修 `bd62919` | 0 | 回放結算前命令仍停放，Q → B 順序斷言保留。 |
| 2 | Medium | 已修 `bd62919` | 0 | raw owner、UART TX 與本地回顯負對照均保留。 |
| 3 | High | 已修 `bd62919` | 0 | release 推進 close 世代，拒絕舊世代晚到連線的防線保留。 |
| P6-e9b5c61-1 | High | 已修 `7370b8f` | 0 | suspend 憑證仍由各自 SelfTest 保存與結算。 |
| P6-e9b5c61-2 | High | 已修 `7370b8f` | 0 | 取消停放命令的兩條退出路徑仍結算 registry 在途指標。 |
| P6-e9b5c61-3 | Medium | 已修 `0c20ee8` | 0 | 兩條 OD-52 修訂鏈的有效指紋均符合現行案例。 |
| P6-e9b5c61-4 | Medium | 未修 | 7 | 直接執行原稽核仍失敗，命中 P5 `s21_core` 的 ignored 列；P6 能力集合豁免測試通過。 |
| P6-c6ddda1-1 | Medium | 已修 `bf61e3e` | 0 | 直接執行原負對照：單份報告重複回 `INSUFFICIENT`，跨集合各一列 passed 回 `PASS`。 |
| P6-c6ddda1-2 | Medium | 已修 `464adec` | 0 | 本輪政策檢查為 25 pass／0 fail／2 warn，R-21 通過。 |
| P6-bf61e3e-1 | Medium | 已修 `e0aab47` | 0 | 改取指紋更新前的修訂鏈前綴，保留 OD-34；以記憶體檔案替身直接執行 2.1／2.2 完整斷言，兩者皆通過。 |

## B 新發現

### P6-b64dad9-1

[High] [scripts/rust/s22_gate_check.py:271](scripts/rust/s22_gate_check.py:271) — **#185 缺少終態綠燈證據，甚至記錄終態失敗，仍判 PASS。**

`_process_evidence_problems()` 僅檢查段落是否包含 commit、`紅燈`、`綠燈` 字串。現行報告的說明與結論已包含這些字樣，無須實際通過紀錄即可滿足判定。

以現行報告作記憶體替換，直接呼叫正式 `judge_parity()`，重現兩案：

- 刪除整行「綠燈紀錄」：預期 `INSUFFICIENT`，實際 `#185 verdict=PASS`。
- 將終態 `13 passed、0 failed` 改成 `0 passed、13 failed`：預期不得 PASS，實際仍為 `PASS`。

裁決允許以報告提供流程證據，但仍要求終態綠燈；目前判定會把缺漏或失敗證據轉成通過。

**確切修法：**將紅／綠燈紀錄改為可解析的結構，分別記錄 commit、命令與結果；紅燈須引用核准的 `ae33e28` 並附失敗測試或 log，綠燈須明確記錄完成、成功退出及零失敗。補上上述兩個負對照，不再以說明文字中的關鍵字判定。

已逐一檢視本輪 8 個提交，無 merge commit。契約檢查通過；未建置 Rust、未跑完整 pytest／E2E。全程未修改檔案，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正