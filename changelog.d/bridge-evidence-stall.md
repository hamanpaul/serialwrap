---
type: fix
scope: bridge
---

- 證據 writer 的 close、flush 與背壓期限改依成功寫入進度判斷停滯，並在 bridge 失敗 stderr 附上路徑細項。

- `uart_stdio_bridge` 核准 tree 更新為 `85b55b6c…`（兩位非實作者審查皆 `BRIDGE: APPROVE`）

- Windows 序列埠寫入在 timeout 設定耗時後，只要剩餘期限還夠再設定一次就重試，不再提前回 0
