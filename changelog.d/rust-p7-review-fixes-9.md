### Fixed

- Rust 重構 P7 審查第 9 輪（Codex sol 補審 p7-20261028）：event handler `timeout_ms` 超大（POSIX > INT_MAX ms、Windows > `threading.TIMEOUT_MAX`）時比照 Python `communicate` 的 OverflowError——子行程照常啟動且不被終止、不 emit、不更新 counter，名額以 RAII 釋放、worker 以 `catch_unwind` 存活（修前 `start + Duration::MAX` panic，worker 與併發名額永久流失）；完成事件寫入 events log 失敗時不推進 counter／cooldown（新增 `Dispatcher::start_fallible`，凍結的 `start` 不變）；interactive open／send 的 TX 不再把事件分類為 `foreground`；首次 `session.log_start` 回應的 `session.capture` 即為本次 capture。
