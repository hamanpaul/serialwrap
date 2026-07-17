---
type: fix
issue: 133
scope: windows
---
修復 Windows 上 `_save_state` 並發 `os.replace` 撞 `WinError 5`（存取被拒）導致 attach 執行緒靜默死亡、`state.json` 更新遺失：I/O 段以 instance 級 `_state_io_lock` 序列化（消除行程內並發 replace；仍為 last-writer-wins），並新增 `_replace_state_file()` 於 Windows 對 `PermissionError` 短退避重試（外部讀者／防毒瞬時 handle），重試耗盡才上拋。POSIX `rename()` 語意不受影響（不重試、行為不變）。原 flaky 的 `test_session_bind::test_multi_device_auto_bind_order`（clean main 2/4 重現）修復後 6/6 通過。
