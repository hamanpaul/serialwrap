## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 978aa1e | 0 | checkpoint／bridge barrier 修正保留，本輪未變更相關程式。 |
| 2 | Medium | 已修 51bd8fa | 0 | 委派孫行程 ready、PID 與結束確認保留。 |
| 3 | Medium | 已修 ddcbdde、4e3ba13 | 0 | 出口 driver 逐列判定與延後條件檢查保留。 |
| 4 | High | 已修 12b3a92 | 0 | bundle 自帶 PyYAML、離線 wheelhouse 與依賴隔離修正保留。 |
| 5 | Medium | 已修 12b3a92 | 0 | 正式配方使用 `--release-bundle`，本輪未變更。 |
| 6 | Low | 已修 12b3a92 | 0 | 前置條件依實際 gate 狀態及核准延後條件判定，修正保留。 |
| S1-4baca8c-1 | Medium | 未修 | 1 | `rollback.rs:806` 仍呼叫 `verify_restored`；`:3389–3391` 仍拒絕「沒有 current、daemon 存在」。首次 pipx 升級於 `legacy_stop:after_capture` 中斷後，恢復仍會回 `restore_verify_failed` 並保留 journal。需加入不要求 bundle current 的 legacy 恢復驗證，以及無 current 的首次安裝回歸。OD-49 的降級限制不涵蓋此情境。 |
| S1-4baca8c-2 | Medium | 未修 | 1 | `rollback.rs:784` 仍取舊 endpoint，`:823` 仍用它停止新版 daemon。舊 socket X、新 socket Y，於 `start_and_verify:started` 中斷且新版持有 UART 時，恢復仍無法停止 Y，繼而回 `tty_holder_remains`。需保存目標模式與 endpoint，先停止目標 instance，再驗證釋放及還原。 |

## B 新發現

NO FINDINGS

指定區間只有 `90da902` 一個非 merge commit。凍結鎖只追加 SC-39；既有案例、原始凍結 commit 與錯誤碼集合均未變更。新增映射涵蓋全部 27 個 `mgmt_legacy_handoff` 測試節點，既有斷言未削弱。

驗證：契約檢查通過；policy 為 25 pass、0 fail、2 warn（非 PR 上下文）；映射結構、凍結雜湊及兩支契約登記函式通過。受唯讀沙箱限制，未建置或執行 Cargo／完整 pytest；前輪兩項為靜態路徑核對，未做行為重現。未修改檔案，未接觸 production，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正