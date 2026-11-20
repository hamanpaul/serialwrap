---
type: refactor
scope: rust-refactor
---

將保管 owner、ActorExited、Reclaim、送達義務與開埠／停止路徑統一改用建立後不變的 ActorId；W6 改名只搬移 manager 的 sid 索引與 Entry，延遲舊 actor 退出通知不再命中新 handle。
