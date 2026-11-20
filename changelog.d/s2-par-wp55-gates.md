---
type: fix
scope: sw-daemon
---
Rust TX fence 新增可信 human 命令類別 `TxClass::HumanLine`（對應 Python `human_line`）：`OpRunner` 的一般命令與 reboot 命令只依 `SubmitReq::is_trusted_human()` 選擇 `HumanLine` 或 `Command`，`HumanLine` 不受 quiet／reconfirm 擋，frozen、stale epoch、cancelled、flash 等共同檢查照常，reboot 提交後仍武裝 quiet。RPC 原始值 bare `human`（及空名 `human:`）於 boot quiet 期間提交或入列後才進 quiet 時，命令會真正送上 UART、寫入 WAL TX 並完成；明寫 `human:cli`、`human:spoof` 與 `agent:*` 仍以 `AUTOBOOT_QUIET` 拒絕且零 TX，RPC 參數 `trusted_human` 不授權（OD-74、DF-02）。
