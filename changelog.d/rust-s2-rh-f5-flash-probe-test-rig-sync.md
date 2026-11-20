---
type: fix
scope: rust-s2-rh-f5
---

修正 flash 偵測測試 rig 的完成條件：等待 probe 終止事件由 session actor 套用、flash mode 回到 Off，且 flash-critical 標記清除。
