---
type: fix
scope: rust-refactor
---

Stage 1A gate 撤除未經 OD-22 授權的「oplock rotation 完成 ≤ 600 ms」FAIL 條件；OD-22 上限只作用於 append 延遲樣本，rotation 仍須在某次受上限涵蓋的 append 內完成。
