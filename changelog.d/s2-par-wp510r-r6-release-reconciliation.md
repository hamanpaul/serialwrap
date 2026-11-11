---
type: fix
scope: rust-refactor
---

- 修正 parked handle 的遲到 release 收尾：滿載時保留重送義務、改綁後使舊事件失效，並讓後續 release 沿用首次期限。
