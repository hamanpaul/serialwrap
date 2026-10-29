---
type: feat
scope: console-unlock
---

新增 `smart4R-profile`、`console_unlock_env` 與 `console_unlock_idle_s`，在實際 UART RX 靜默滿額後處理隱藏 console lock；已有 shell prompt 不送 secret，CFE prompt 不送 probe，缺來源以 `CREDENTIALS_UNRESOLVED` 停止。
