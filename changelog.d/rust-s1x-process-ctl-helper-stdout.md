---
type: fix
scope: sw-pal
---

`process_ctl` 測試中以重新執行自身方式驗證 SIGCHLD 忽略（ECHILD）的 helper 改以 `output()` 擷取子行程 stdout／stderr，避免子 libtest 的 `running 1 test`、`.` 與 `test result:` 列繼承到父行程，與並行的 `test … ok` 結果列交錯，造成 L1 結果列解析缺漏；子行程失敗時輸出放入斷言訊息，斷言條件與 helper 行為不變。

同類寫法 `crates/sw-pal/tests/exec_delegate.rs` 的 `exec_or_wait_propagates_exit_code_and_env` 改以 `output()` 擷取子行程輸出（原 `status()` 繼承 stdout），斷言條件不變；`windows_process_ctl_preserves_unsigned_exit_code`（cfg(windows)）之 spawn 路徑仍繼承 stdio，屬 production 行為，未在本次修改。
