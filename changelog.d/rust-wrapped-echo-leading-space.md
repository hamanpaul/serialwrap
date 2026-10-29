---
type: fix
scope: rust-refactor
issue: null
---

修正 bash readline 折行標記緊貼命令原有空白時，命令回顯未能剝除而混入 stdout 的問題；補上前置空行與多次折行回歸案例。
