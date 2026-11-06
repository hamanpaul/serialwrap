---
type: fix
scope: bridge
---

- 證據 writer 的 close、flush 與背壓期限改依成功寫入進度判斷停滯，並在 bridge 失敗 stderr 附上路徑細項。
