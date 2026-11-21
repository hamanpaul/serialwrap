---
type: fix
scope: rust-daemon
---
- 新增 issue 282 停機行收尾所需的待凍結 Fence 狀態與傳輸持有者軟喚醒基礎。
- echo-paced 傳輸持有命令列並在停機期限內完成行尾或清除半行；待凍結時只放行該行持有者。
