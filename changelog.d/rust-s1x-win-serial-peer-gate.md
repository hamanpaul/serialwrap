---
type: fix
scope: test-infra
---
修正 `uart_stdio_bridge` Windows win-serial 端點缺少寫入閘門的問題（S1X，PENDING-OD-S1X-WIN-BANNER）：

- 端點以 COM-B 的 DSR 取樣對端（COM-A）是否開啟；`peer_gate=True` 時，對端未開啟或仍在 settle 窗口內不寫入（寫入串流長度為 0），以跨過 pySerial `open()` 的 `PurgeComm`。settle 窗口 0.2 s 為未經量測的選值，待原生實測校準。
- `peer_gate` 僅對 `virtual_com0com` 啟用（CLI 預設啟用，`--no-peer-gate` 可關閉）；`physical_usb_uart` 建構時拒絕啟用，因其 DSR 映射未實測。
- 寫入鎖等待期間 peer 可能關閉；端點在持有寫入串行鎖、呼叫 pySerial 前重新取樣 DSR，若已關閉則持續等待至呼叫期限並回 0，不送出 bytes。
- 需要 reconfigure `write_timeout` 時，取得寫入與讀取鎖後、呼叫 setter 前再取樣 DSR；等待期間 peer 關閉時不改寫 COM timeout。
- `peer_open()` 不等待 endpoint I/O lock，避免 peer-state 查詢被關閉中的寫入卡住。
- `peer_gate=False`（selftest 既有規格與 CLI 的 `--no-peer-gate`）時 `peer_open()` 回 None、不取樣，維持既有行為；若回 False 會使對端關閉時寫入逾時並 fail-stop（HARNESS_WRITE_UNCERTAIN）。
- 對外 `status.peer_open` 僅於端點宣告 `peer_state` 能力時揭露；win-serial 的 virtual com0com 且 `peer_gate=True` 時提供 DSR 狀態，其餘模式仍回 null。
- `assert_peer_closed` 會依執行端點能力判定；Windows com0com 啟用 `peer_gate` 時可執行，未啟用或使用實體 UART 時仍須回報 `cap_missing:peer_state`。
- `capabilities()` 僅在 virtual com0com 且 peer gate 啟用時宣告 `peer_state`；`transport_disconnect` 仍未宣告，避免把 peer close 誤當成傳輸斷線。
- 原生實測（COM20:COM21）：peer 開啟後 DSR 轉高並寫出 banner；peer 關閉後 1 s 內 `peer_open` 轉 False，關閉期間寫入回 0 且不到達 peer（`b-log/rust-refactor/g3-peer-gate/261023-061416-2646662`）。本 fragment 不代表 Windows L1.5 案例已重跑；PENDING-OD-S1X-WIN-BANNER 維持 OPEN。
- 本變更改動 `tools/uart_stdio_bridge/src`，`APPROVED_REVISION.json` 的核准 tree 失效，須經非實作者審查並重新核准後，Windows 與 Linux 的 L1.5 結果才可視為有效證據。
