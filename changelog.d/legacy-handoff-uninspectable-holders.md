---
type: fix
scope: setup
---

修正 0.3.x 執行中直接升級時，`setup` 在有 systemd user session 的機器（含 WSL）上一定等滿 330 秒、回 `TIMEOUT`／`legacy_stop_pending` 的問題（`alive_pids`、`holder_pids` 都是空的）。等待舊 daemon 結束時會掃描所有同使用者行程是否還持有 UART，但同使用者的 `(sd-pam)` 是 non-dumpable 行程，`/proc/<pid>/fd` 屬 root、整個讀不到；`systemd --user` 持有呼叫端沒有的 capability，fd 目錄列得出、條目卻全部被拒。整次掃描因此一律判為讀取失敗，「UART 已釋放」永遠不成立。現在只剩這類「整個無法檢查」的行程、且它們的 real、effective、saved、fs 四個 uid 都是同使用者、送出 stop 前就已存在（以 pid＋start_ticks 快照比對）時，照原本的 5 秒穩定窗放行，並在 `legacy_stop` 以 `holders_status: "partial"` 與 `uninspectable_procs`（pid、start_ticks、comm、reason）列出它們。stop 後才出現（含 pid 重用）的無法檢查行程、uid 只有部分相同的行程（例如同使用者執行的 setuid UART 工具）、目標 tty 讀不到、同一行程部分 fd 讀不到或其他讀取失敗仍維持原本的保守行為；掃描途中行程結束（fd 目錄 NotFound）與等待中拔除裝置（目標 tty 節點不存在）不再算讀取失敗，忙碌機器上的穩定窗不會因此一再重置。逾時回應附最後一次輪詢的 `holders_status`、`uninspectable_procs`（標 `seen_before_stop`）、`daemon_scan`，以及最後一次不穩定的 `last_unstable`（`waited_ms`、`reasons`）。其他使用 holder 掃描的路徑（`serialwrapd_tty_holders`，即 `update_confirm_released`）判定不變。
