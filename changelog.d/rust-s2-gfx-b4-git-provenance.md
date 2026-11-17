---
type: fix
scope: rust-s2-gfx-b
---

Slice 2.1 的 sys_admin Docker 收集器以唯讀掛載 linked worktree 的 Git metadata，讓容器內保留真實 HEAD 與乾淨工作樹 provenance。
