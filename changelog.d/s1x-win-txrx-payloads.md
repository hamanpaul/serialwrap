---
type: fix
scope: rust-refactor
---

`win-com-txrx-bridge` 情境改依規格送 `echo W_1`～`echo W_50`，斷言每個 payload 在 wire 依序恰一次、50 個命令逐一 done（burst 送出後以第二個 `repeat` 逐一檢查），並新增 console bytes 原樣出現的檢查；func-test runner 的 `repeat` 提供 `{repeat.index}`，cli 的 `save_as` 可含參照逐次另存，並支援一層巢狀參照 `{w_{repeat.index}.cmd_id}`。舊情境以相同 payload 送 50 次卻預期 1 次，正確行為也會 FAIL。
