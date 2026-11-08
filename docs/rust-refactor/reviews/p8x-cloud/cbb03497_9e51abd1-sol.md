## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8X-b837cfc-1 | High | 已修 cbb03497 | 0 | 本輪改用 FIFO 保留事件，交接撤銷後仍會套用重探結果，原修正保留。 |
| P8X-cbb0349-1 | High | 已修 9e51abd1 | 0 | banner 先於舊 Ready 重播時，SC-30 會移除原 login run，後續 Ready 無法通過身分檢查；d6829f54 新增對應回歸案例。 |

## B 新發現

**P8X-9e51abd-1 [High] crates/sw-core/src/session_core.rs:864 — 永久凍結丟棄 UART RX，造成背景結果遺失。**

`dispatch_handoff_permanent()` 的允許清單不包含 `UartRxChunk`；第 854 行的暫存事件清理也會丟棄它。因此 RX 不再進入 `capture_rx()`（第 4810 行）。

具體失敗情境：

1. READY session 以 background 模式執行 `(sleep 1; printf 'TAIL\n') &`，等待命令完成，background capture 仍為 active。
2. 呼叫 `daemon.handoff_stop` 成功，進入三秒排空窗口。
3. 設備在窗口內輸出 `TAIL\n`，再查詢該命令的 `command.result_tail`。

預期：回傳包含 `TAIL`；Python [`_on_bridge_rx()` 第 4662–4668 行](sw_core/session_manager.py:4662) 在凍結後仍更新 active background capture。實際：Rust 丟棄 RX 事件，結果缺少 `TAIL`。即使 WAL 保留原始 bytes，也無法補回此結果介面。一般 shutdown 的 admission freeze 同樣進入此分支。

確切修法：將 RX 擷取與 readiness／TX 推進分開；永久凍結時，暫存及新到的 RX 仍須更新有效 capture，並在 drain barrier 前完成擷取。新增上述排空窗口的結果完整性回歸測試。

驗證限制：本項依靜態呼叫鏈確認；定向 Cargo 測試因指定 target 遇到 `Read-only file system (os error 30)`，未能執行。未修改檔案，未跑完整 pytest、docker 或 bundle 測試。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正