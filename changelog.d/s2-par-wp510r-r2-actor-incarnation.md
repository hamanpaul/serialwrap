---
type: fix
scope: rust-refactor
---

修正 W6 rematerialize 後 actor 世代與目標 sid 歷史世代碰撞，避免延遲退出通知關閉新 actor 的序列埠 handle。
