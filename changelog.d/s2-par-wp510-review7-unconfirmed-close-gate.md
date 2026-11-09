---
type: fix
scope: rust-refactor
---

- UART handle 關閉回條為 `closed:false`（attach 作業、正式 bridge 接手前、正式 bridge 自身）後，Rust daemon 以全 daemon 共用的「未確認關閉 by_id 集合」擋下所有重新開埠入口（對齊 Python `_draining_by_ids`）：`device.attach` 回 `SESSION_RECOVERING` 並附 drain 欄位；`session.attach`、`session.recover`、`session.clear`、`session.bind` 與 reprobe 自動 attach 照 Python 停在 ATTACHING、不開埠；hotplug／watcher、drain 收尾的再 attach 與 attach worker 的 PROBE 皆不再開埠或送出 TX。
