---
type: feat
scope: setup
---

0.4.0 `setup` 遇到 0.3.x 舊 daemon（`daemon.handoff_stop` 回 `METHOD_NOT_FOUND`）時不再一律拒絕（OD-48）：確認版本低於 0.4.0、正向辨識為 Python 0.3.x daemon 行程且不在 bundle 的 `versions/` 底下後，先做停止前檢查（其他同使用者 daemon、flash／session 忙碌、外部監管者），把行程資訊寫進 `install-journal.legacy.json` 並 fsync，再於自動啟動 hold 之下停止舊 daemon（該 pid 為 systemd unit 的 MainPID 時用 `systemctl stop`，否則 RPC `daemon.stop`；不硬殺）。舊行程樹全部結束、沒有同使用者行程持有 UART、且沒有新出現的 serialwrapd，連續維持 5 秒才算停止（上限 330 秒；被重拉時對新行程重做檢查後再停，最多 3 次）。成功後照常切換並由 setup 啟動新版 daemon，報告新增 `legacy_stop`。舊 daemon 停止後若任何一步失敗，自動還原安裝狀態但不重啟任何 daemon，報告附 `legacy_daemon_stopped`、`restored` 與啟動指令（OD-48 修訂）；等待逾時回 `legacy_stop_pending`，保留 journal 與 hold，下一次 `setup` 先確認舊行程已結束再還原。`supervision-mode set` 維持拒絕。

同時修正 `setup` 更新／首次安裝與 `supervision-mode set` 一律以 `local_default_endpoint` 探測舊 daemon 的問題（S1X-c-1）：改依舊模式解析 daemon instance endpoint（systemd-system 固定 `/run/serialwrap/serialwrapd.sock`、Windows 模式用預設 endpoint、其餘優先取 config 中可連線的 unix `socket_path`），啟新／驗證改用目標模式的 endpoint；endpoint 不可 RPC 時，掃到任何同使用者的 serialwrapd 一律以 `DEVICE_STILL_HELD` 拒絕，systemd 監管顯示 running 卻連不上 daemon 時亦同。修正 systemd enabled 狀態誤判（D8）：enabled 改以 `systemctl is-enabled` 判定，撤銷自動啟動 hold 時還原 hold 取得當下的原值，原本 disabled 但正在執行的 unit 不再因一次升級或拒絕被改成 enabled。
