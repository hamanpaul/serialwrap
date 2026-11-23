> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5914e1c-1 | Medium | 已修 `ef2cebf3` | 0 | 先求共同 template，再檢查其結構；新增非共同 `null` template 的案例。 |
| V051-5914e1c-2 | Medium | 已修 `ef2cebf3` | 0 | 新比較器獨立檢查 `profiles` 是否為 mapping，涵蓋 `[]`、`null`、`false`。 |
| V051-5914e1c-3 | High | 未修 | 1 | 已補兩份紅燈紀錄，但執行時是手動把新程式的結果改成 `matched`，並非在修正前提交 `78d7c9a0` 上執行對應測試；因此仍缺前輪要求的、可追溯到修正前程式的紅燈證據。見[操作紀錄](~/b-log/rust-refactor/exec/v051/p289/p289-i2-L/round-2.log:6744)。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 具型別比較結果及四個結構化欄位 | 完成 `5914e1c2` | [doctor_cmd.rs](crates/sw-cli/src/doctor_cmd.rs:1376) 定義比較狀態與結果。 |
| 區分讀取、編碼、YAML、結構及不可比較狀態 | 完成 `ef2cebf3` | 獨立解析 `profiles`；定向測試通過。 |
| 只比較共同 template 的頂層欄位 | 完成 `ef2cebf3` | [doctor_cmd.rs](crates/sw-cli/src/doctor_cmd.rs:1549) 先求共同名稱；測試保障的限制見 B。 |
| 保留 bootloader 檢查並修正 fix 文案 | 完成 `5914e1c2` | 本輪未改既有 bootloader helper；fix 文案保留 p290 例外。 |
| 指定 Rust 回歸、p290 transition、唯讀及資產錯誤測試 | 完成 `5914e1c2` | 本次定向實跑 `mgmt_doctor` 12 passed；資產錯誤單元測試 1 passed。Windows 為模擬入口，非原生實跑。 |
| 兩項指定先紅證據 | 部分完成 | 紅燈與綠燈紀錄存在，但紅燈使用人工重播的行為，尚非修正前版本實跑；實作者回報 `SPEC-GAPS: none`。沿用 A 的 V051-5914e1c-3。 |

## B 新發現

1. **V051-ef2cebf-1 [Medium]** — [mgmt_doctor.rs:958](crates/sw-cli/tests/mgmt_doctor.rs:958) — 非共同 template 的回歸測試依賴出貨資產中的中文註解作為 `replacen` 錨點，卻未確認替換成功。若日後只改掉該註解，測試輸入就不含 `custom-template: null`；即使重新引入「驗證所有 template」的缺陷，這一案例仍會得到 `matched` 並通過。**修法：**使用明確的固定 YAML fixture，或先斷言替換確實發生且產生的 YAML 含指定 template。

完整性缺口 **[High]** 沿用 A 的 V051-5914e1c-3，未重複編號。應在隔離環境以 `78d7c9a0` 的實際產品程式搭配指定測試執行並保存兩項失敗輸出，再核對修正後轉綠。

## 既有碼待辦（不計入結論）

第 3～6 項的 OD-33 精確 gate、BC-08 與契約映射、A/B 差分及 driver 接線依編排者安排於後續完成。

## 對裁決的異議（不計入結論）

無。

結論：需修正