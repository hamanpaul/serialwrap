---
type: fix
scope: setup
---

修正 0.3.x 執行中直接升級時，`setup` 在有 systemd user session 的機器（含 WSL）上一定等滿 330 秒、回 `TIMEOUT`／`legacy_stop_pending` 的問題（`alive_pids`、`holder_pids` 都是空的）。等待舊 daemon 結束時會掃描所有同使用者行程是否還持有 UART，但同使用者的 `(sd-pam)` 是 non-dumpable 行程，`/proc/<pid>/fd` 屬 root、整個讀不到；`systemd --user` 持有呼叫端沒有的 capability，fd 目錄列得出、條目卻全部被拒。整次掃描因此一律判為讀取失敗，「UART 已釋放」永遠不成立。現在只剩這類「整個無法檢查」、且已確認是同使用者的行程時，照原本的 5 秒穩定窗放行，並在 `legacy_stop` 以 `holders_status: "partial"` 與 `uninspectable_procs`（pid、comm、reason）列出它們；目標 tty 讀不到、同一行程部分 fd 讀不到、uid 未確認或其他讀取失敗仍維持原本的保守行為。逾時回應另附 `holders_status`，標出最後一次 holder 掃描的狀態。其他使用 holder 掃描的路徑（`serialwrapd_tty_holders` 等）判定不變。
