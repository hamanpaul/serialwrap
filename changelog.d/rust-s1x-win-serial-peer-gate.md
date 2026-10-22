---
type: fix
scope: test-infra
---
修正 `uart_stdio_bridge` Windows win-serial 端點缺少寫入閘門的問題（S1X，PENDING-OD-S1X-WIN-BANNER）：

- 端點以 COM-B 的 DSR 取樣對端（COM-A）是否開啟；`peer_gate=True` 時，對端未開啟或仍在 settle 窗口內不寫入（寫入串流長度為 0），以跨過 pySerial `open()` 的 `PurgeComm`。settle 窗口 0.2 s 為未經量測的選值，待原生實測校準。
- `peer_gate` 僅對 `virtual_com0com` 啟用（CLI 預設啟用，`--no-peer-gate` 可關閉）；`physical_usb_uart` 建構時拒絕啟用，因其 DSR 映射未實測。
- `peer_gate=False`（selftest 既有規格與 CLI 的 `--no-peer-gate`）時 `peer_open()` 回 None、不取樣，維持既有行為；若回 False 會使對端關閉時寫入逾時並 fail-stop（HARNESS_WRITE_UNCERTAIN）。
- 對外 `status.peer_open` 僅於端點宣告 `peer_state` 能力時揭露，win-serial 恆為 null，L1.5 的 `assert_peer_closed` 因此維持 SKIP `cap_missing:peer_state`。
- `capabilities()` 的 `peer_state` 與 `transport_disconnect` 維持未宣告，待 G4 peer-close 實測結果寫回後才可調整。
- 原生實測（COM20:COM21）：peer 開啟後 DSR 轉高並寫出 banner；peer 關閉後 1 s 內 `peer_open` 轉 False，關閉期間寫入回 0 且不到達 peer（`b-log/rust-refactor/g3-peer-gate/261023-061416-2646662`）。Windows L1.5 重跑尚未完成，PENDING-OD-S1X-WIN-BANNER 維持 OPEN。
- 本變更改動 `tools/uart_stdio_bridge/src`，`APPROVED_REVISION.json` 的核准 tree 失效，須經非實作者審查並重新核准後，Windows 與 Linux 的 L1.5 結果才可視為有效證據。
