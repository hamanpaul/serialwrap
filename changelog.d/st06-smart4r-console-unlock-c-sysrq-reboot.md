---
type: feat
scope: console-unlock
---

精確識別 `echo b > /proc/sysrq-trigger` 的立即 reboot，其他 profile 保持既有 boot quiet 行為；以 PTY 覆蓋實際 RX 靜默與各 recovery 入口。
