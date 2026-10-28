---
type: fix
scope: rust-refactor
---

`win-com-txrx-bridge` 情境改依規格送 `echo W_1`～`echo W_50`，斷言每個 payload 在 wire 依序恰一次、最後一個命令 done，並新增 console bytes 原樣出現的檢查；func-test runner 的 `repeat` 提供 `{repeat.index}`。舊情境以相同 payload 送 50 次卻預期 1 次，正確行為也會 FAIL。
