---
type: fix
scope: rust-daemon
---
S2-PAR-wp510r R6：parked handle 關閉逾時後，遲到 release 的完成義務保留至成功排入原 session actor；safety lane 滿載時在 manager lock 外退避重試，並核對 token、actor 世代與 by-id。
