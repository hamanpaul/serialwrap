---
type: fix
scope: setup
---

OD-48 第 2 輪修正：首次以 bundle 升級 pipx 安裝（沒有 `current-release.json`）時，若在寫完舊 daemon 擷取檔、尚未送出 stop 前中斷，下一次 `setup` 的 journal 復原改以行程辨識確認 endpoint 上仍是 0.3.x 舊 daemon，不再因「沒有 current 卻有 daemon」回 `restore_verify_failed` 並卡住 journal（S1-4baca8c-1）。跨監管模式升級（舊 endpoint 與目標 endpoint 不同）在啟動新版 daemon 後中斷時，journal 會記錄目標模式與 endpoint，復原先停止目標 endpoint 上的新版 daemon並確認釋放後才還原，避免新版 daemon 繼續持有 UART（S1-4baca8c-2）。Linux 改以 `/proc/<pid>/cgroup` 判定舊 daemon 是否由外部監管者管理：位於 serialwrap 以外的 systemd service unit 或其他監管者 scope 時，在送出任何 stop 之前以 `DEVICE_STILL_HELD`（`external_supervisor`）拒絕，父行程清單保留為輔助判定。舊 daemon 停止後還原本身失敗時，報告以 `unrestored` 逐項列出未還原的項目（`entries`／`config`／`unit`／`enabled`／`current`）。
