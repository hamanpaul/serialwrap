---
type: fix
scope: sw-daemon
---
新增 `SubmitReq` 的 `expect_reboot` 與 `trusted_human` 資料欄位，讓旗標經 admission 與命令佇列傳遞時保留；可信 human 判定將明示欄位與 `HumanConsole` 來源取 OR。一般 RPC 請求預設為 false，console 請求標記為可信 human。
