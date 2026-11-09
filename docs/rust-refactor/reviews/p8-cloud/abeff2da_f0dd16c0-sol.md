## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 正式 RPC 仍經 `route_with_selector` 保留原始 selector。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | release 取消與佔位退休檢查仍由同一 manager 鎖互斥。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | `closed:true` 仍依 token 清除 stall，保留 timeout／settled 入列順序。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | `closed:false` 保留 inflight、stall 與原 epoch；落盤失敗路徑仍傳遞關閉回條。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠錯誤仍可完成排空；Held／NotFound／Other 回歸及正式 RPC 接線均保留。 |

## B 新發現

NO FINDINGS。

已逐一檢查 `4bd160e5`、`f0dd16c0`，無 merge commit。核對正式 RPC → manager 接線、worker 收尾、取消後關閉失敗阻擋，以及首次 release deadline 重用，未發現本輪引入的具體缺陷。四個新增測試在修正前均有對應失敗斷言，未削弱既有斷言。

驗證結果：

- `git diff --check abeff2da..f0dd16c0` 通過。
- `regen_lock.py --check --base abeff2da` 通過；凍結鎖僅新增四個節點。
- 定向 Cargo 測試因指定 `/tmp` target 為唯讀而未啟動，不宣稱測試通過。

未修改檔案；工作樹保持乾淨，未接觸 production daemon、正式設定／狀態或實體 UART。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：可放行