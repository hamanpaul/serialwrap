---
type: fix
scope: rust-refactor
issue: null
---

修正前置空行後 readline 折行命令回顯未剝除的問題。只接受命令原文加上符合間距的折行標記，保留其他 stdout。
