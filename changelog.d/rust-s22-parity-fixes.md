---
type: fix
scope: rust-console
---
Rust daemon console 依 1:1 稽核修正（parity-amendments-2026-10-26）：第二 console 行超過 16385 bytes 的溢位 WARNING 改在第 16386 個 byte 到達當下寫出、同一段兩行溢位寫兩筆（與 Python 相同，#177）。
出向 ring 在保護已部分寫出的 front 而丟掉剛推入的新項時改回報 `DroppedNewest`，不再誤報 `DroppedOldest`（#178）。
