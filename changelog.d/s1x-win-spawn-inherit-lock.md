---
type: fix
scope: rust-refactor
---

Windows `exec_or_wait` 以原始標準 handle 建立行程時，與背景 daemon spawn 暫時取消繼承旗標的區段串行；並新增呼叫端以單一 pipe 合併 stdout／stderr 時 `daemon start` 須立即 EOF 的原生案例。

此鎖修正沒有決定性回歸測試：合併 pipe 案例只覆蓋 `spawn_daemon` 的去重路徑，拿掉 `exec_or_wait_opts` 的鎖仍會通過（`exec_or_wait_opts` 成功時以 `process::exit` 結束行程，無法在測試內重現並行視窗）。
