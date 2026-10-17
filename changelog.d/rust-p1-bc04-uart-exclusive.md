---
type: security
scope: uart
---

POSIX 開啟 UART 時取得非阻塞 flock 並設定 TIOCEXCL；開啟衝突會拒絕收送並回報可識別的 holder，掃描不完整時在日誌與 daemon status.foreign_holders 標示 holder_unknown。Windows 存取拒絕映射為 DEVICE_STILL_HELD。device.attach 改為同步取得獨占後才進入 ATTACHING，`--force` 不會略過實際開檔與鎖後 holder 檢查；release、rebind 與 hot-unplug 會等在途 PROBE 安全收斂後才交還 handle。動態 session 的 RELEASED 狀態跨重啟保留，裝置離線時不會降為 DETACHED。
