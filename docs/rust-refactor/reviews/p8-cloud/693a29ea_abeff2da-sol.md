## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 正式 RPC 仍透過 `route_with_selector` 保留原始 selector。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | release 取消標記與佔位退休檢查仍由同一 manager 鎖互斥。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | `closed:true` 仍依 token 清除 stall，保留 timeout／settled 入列順序。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | `closed:false` 仍保留 inflight、stall 與原 epoch；落盤失敗路徑亦傳遞關閉回條。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠錯誤可完成排空；新增 Held／NotFound／Other manager 回歸，以及 Held 正式 RPC 回歸，涵蓋清除診斷、重複 release 與重新 attach。 |

## B 新發現

NO FINDINGS。

已逐一檢查本輪唯一提交 `abeff2da`，無 merge commit。核對所有 `finish_attach_op` 呼叫點、正式 RPC 接線及回歸斷言，未發現本輪引入的具體缺陷。

`git diff --check`、`regen_lock.py --check --base 693a29ea` 通過；凍結鎖僅新增 5 個集合節點，無移除或判準變更。定向 Cargo 測試因指定 `/tmp` target 為唯讀而未啟動，不宣稱測試通過。未修改檔案，未接觸 production daemon、正式設定／狀態或實體 UART。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：可放行