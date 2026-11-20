---
type: fix
scope: rust-refactor
---
新增 `SubmitReq` 的 `expect_reboot` 與 `trusted_human` 資料欄位，讓旗標經 admission 與命令佇列傳遞時保留；可信 human 判定將明示欄位與 `HumanConsole` 來源取 OR。console 請求標記為可信 human。

RPC `command.submit` 把驗證後的旗標寫進 `SubmitReq`：`expect_reboot` 只在原始值為 bool true 時啟用（缺省、null、false 皆為 false），`trusted_human` 取來源檢查的空名 human 判定（`human`、`human:` 等），`origin` 仍維持 RPC；旗標經佇列送到 `OpSpec::Command`，命令照常執行並由 `command.get` 回報。旗標不繞過非 READY、boot quiet 與 READY_UNCONFIRMED 的提交時 gate，也不繞過入列後才武裝的 execute-time quiet gate。reboot 分支、WAL META 與可信 human 的 gate／timeout 消費端尚未接上。

共用驗證原始 `expect_reboot` 值：缺省、null、bool 合法，其他型別拒絕；`interactive` 搭配 true 依 Python 基線拒絕，並在命令長度檢查與入列前完成驗證。

RPC 入口依序驗證 completion regex、source、`expect_reboot` 型別與模式，再轉型 timeout 並解析 selector；非法旗標與不支援模式均在 selector 查找前以 Python 錯誤字典拒絕，不產生 UART TX 或命令副作用。
