---
type: feat
scope: rust-console
---
新增不阻塞的 ConsoleHub thread（07 Task 9）：每 session 一條 `serialwrap-console-<COM>` thread 以 `ConsoleMux` 管理 PTY／Telnet console，RX 經 `HubTap` 依 WAL→console→data lane 順序 fan-out 到每 console 出向 ring（Telnet 做 IAC 轉義、greeting 先於 fan-out 送出）；console 輸入經 `TelnetFilter` 後帶 `input_epoch` 快照排入有界 pending（64 筆，滿時暫停讀取並寫 WARNING），斷線事件與輸入同一條 data lane；每 3.0 s peer 掃描送 `ConsoleScan{Hub}` 並依 D-05 清空無人持有 PTY 的 Display；Telnet console 上限 32（達上限暫停 accept）；`mux.wait` 失敗 20 ms 退避後重整（#37）、單一 console 處理 panic 只寫 WARNING（#89）；close／close_all／shutdown 走 critical 旁路不經 channel。
