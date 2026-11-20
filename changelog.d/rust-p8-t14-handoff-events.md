---
type: fix
scope: rust-core
---
- 交接凍結嘗試期間以有界 FIFO 暫存背景事件；TX fence 撤銷後依序重播，凍結成立後丟棄，避免舊重探結果覆寫較新的 boot 取消。
