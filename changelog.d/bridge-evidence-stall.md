---
type: fix
scope: bridge
---

- 證據 writer 的 close、flush 與背壓期限改依成功寫入進度判斷停滯，並在 bridge 失敗 stderr 附上路徑細項。

- `uart_stdio_bridge` 核准 tree 更新為 `0021ba67…`（兩位非實作者審查皆 `BRIDGE: APPROVE`）
