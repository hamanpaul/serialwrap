---
type: fix
scope: rust-refactor
---

Stage 1A gate 的 `s1_pytest_full` 逐列對帳保留參數化 id，`[linux]`／`[windows]` 各自恰一次 passed 才放行，不再合併成同 key 而恆判證據不足，也不再以同函式另一參數補位。
