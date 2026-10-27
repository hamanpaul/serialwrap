---
type: fix
scope: rust-refactor
---

Windows `exec_or_wait` 以原始標準 handle 建立行程時，與背景 daemon spawn 暫時取消繼承旗標的區段串行；並新增呼叫端以單一 pipe 合併 stdout／stderr 時 `daemon start` 須立即 EOF 的原生案例。
