---
type: fix
scope: interactive
---

修正 `mode=interactive` 命令（含 human line console 輸入 `vim`／`top`／`less` 等被判為 interactive 的行）永遠送不到 UART 的回歸：interactive 分支的 TX permit 改用與 lease owner 一致的 `interactive:<lease_id>`，恢復 0.3.2 的 `ok:true` 並寫出命令；送出被拒或拋例外時會關閉剛開的 lease，後續命令不再被遺留 lease 卡成 `SESSION_INTERACTIVE_BUSY`（S1X-a-1）。
