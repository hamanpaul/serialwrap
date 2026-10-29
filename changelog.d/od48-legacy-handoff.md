---
type: fix
scope: setup
---

修正 `setup` 更新／首次安裝與 `supervision-mode set` 一律以 `local_default_endpoint` 探測舊 daemon 的問題（S1X-c-1）：改依舊模式解析 daemon instance endpoint（systemd-system 固定 `/run/serialwrap/serialwrapd.sock`、Windows 模式用預設 endpoint、其餘優先取 config 中可連線的 unix `socket_path`），啟新／驗證改用目標模式的 endpoint；endpoint 不可 RPC 時，掃到任何同 uid 的 serialwrapd（socket 不符或無 `--socket`）一律以 `DEVICE_STILL_HELD` 拒絕，systemd 監管顯示 running 卻連不上 daemon 時亦同。修正 systemd enabled 狀態誤判（D8）：enabled 改以 `systemctl is-enabled` 判定，撤銷自動啟動 hold 時還原 hold 取得當下的原值，原本 disabled 但正在執行的 unit 不再因一次升級或拒絕被改成 enabled。
