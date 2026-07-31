---
type: fix
scope: session_manager
---
修復 `session recover`／命令逾時內部觸發 recovery 時，CTRL_C/CTRL_D 攔截成功路徑不 flush 舊佇列的缺口（#156）：`#128` 建立的 flush 機制唯一掛點是「detach」事件（`_on_detached` → `CommandArbiter.unregister_session`），但 `SessionManager._recover_after_failure()` 的 CTRL_C/CTRL_D 攔截成功分支語意是「session 沒有真的離線」，全程停留 `READY`、不觸發 detach，因而天生跳過既有 flush 路徑——該 session 尚未啟動的排隊命令永久卡在 `accepted`，佔用 `CMD_PENDING_MAX` 額度，之後每次 submit 連鎖 `SESSION_QUEUE_FULL`。`session.recover` RPC 在 `READY` 狀態下沿用同一段程式碼，受相同缺口影響。

修法：`SessionManager` 新增第 4 個建構子 callback `on_command_flush: Callable[[str, str], None] | None`（預設 `None`，向下相容）；CTRL_C/CTRL_D 攔截成功分支於鎖外顯式呼叫 `self._on_command_flush(session.session_id, "FLUSHED_BY_RECOVERY")`，補上與 detach 路徑相同的終態語意。`SerialwrapService` 新增 `_on_command_flush()` 方法，直接委派 `CommandArbiter.flush_session()`（既有公開方法，`arbiter.py` 本身無需改動）。刻意不動「human interactive promotion」分支（`source.startswith("human:")`）：那是暫時把 raw 控制權交給人類，不是宣告佇列作廢，其餘排隊命令在人類結束 interactive lease 後仍是合法待執行工作。

回歸 plugin：`f2-recovery-flushes-queue`（`issues` 疊加 `#156`）的排空 oracle 由首輪實測時因本缺口被迫放寬的 30s 收緊為 10s 嚴格斷言；相關歷史註解（`f2_queue_full_backpressure` 收尾、module docstring）同步更新為「已修復」。
