### Added

- 新增 `smart4R-profile`、`console_unlock_env` 與 `console_unlock_idle_s`，在實際 UART RX 靜默滿額後處理隱藏 console lock；已有 shell prompt 不送 secret，CFE prompt 不送 probe，缺來源以 `CREDENTIALS_UNRESOLVED` 停止。
- Attach、recover、自動 reprobe 與 reboot recovery 支援只需 console unlock 的憑證；以獨立 nonce 輸出確認 READY，提供不含 secret 的解鎖時序日誌。
- 精確識別 `echo b > /proc/sysrq-trigger` 的立即 reboot，其他 profile 保持既有 boot quiet 行為；以 PTY 覆蓋實際 RX 靜默與各 recovery 入口。
