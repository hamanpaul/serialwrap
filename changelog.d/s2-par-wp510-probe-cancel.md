---
type: fix
scope: rust-refactor
---

- attach 作業的 PROBE 改用作業自有的 Fence 與 CancelToken；shutdown、release、bind 與裝置改指取消作業時會在 fence 鎖內凍結並取消 PROBE，已通過有效性檢查但尚未提交的 `\n` 不再送出（佔位交接與一般熱插都適用）。
