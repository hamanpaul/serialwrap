---
type: change
scope: func-test
---
新增 L1.5 func-test `co-02-line-backspace`（#107）：owner console 先 detach 後，第二個 line-buffer console 以 0x08／0x7F 退格編輯整行，UART 只收到編輯後的命令；Python 基線與 Rust daemon 皆通過。Slice 2.2 測試／oracle 變更改登記於 `docs/rust-refactor/slice22-chg-register.md`。
