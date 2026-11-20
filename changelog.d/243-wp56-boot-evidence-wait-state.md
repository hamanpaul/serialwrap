---
type: fix
scope: sw-core
---
Rust `QuietState` 新增非同步 reboot 的開機證據等待狀態（對齊 Python `SessionRuntime.boot_evidence_wait_until`，#243）：`begin_boot_evidence_wait` 自起算點設 60 s 寬限（`BOOT_EVIDENCE_GRACE_S`，`sw-proto` 常數），`boot_evidence_wait_active` 以 `now < until` 判定，剩餘秒數照 Python `round(…, 1)` 投影；RX 命中 boot banner（含跨 chunk）時先解除等待再照舊重臂 quiet，`confirm_ready` 與真正翻入 `READY_UNCONFIRMED` 終態的 expire 一併清除。等待不延長 300 s reconfirm 期限、不動 pending／attempts，`clear_quiet` 與 `arm` 不清等待。本項只建立狀態，尚未接 reboot、probe 守衛與 public 欄位入口。
