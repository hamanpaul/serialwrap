### Added

- 新增 `smart4R-profile`、`console_unlock_env` 與 `console_unlock_idle_s`，在實際 UART RX 靜默滿額後處理隱藏 console lock；已有 shell prompt 不送 secret，CFE prompt 不送 probe，缺來源以 `CREDENTIALS_UNRESOLVED` 停止。
- Attach、recover、自動 reprobe 與 reboot recovery 支援只需 console unlock 的憑證；以獨立 nonce 輸出確認 READY，提供不含 secret 的解鎖時序日誌。
- 精確識別 `echo b > /proc/sysrq-trigger` 的立即 reboot，其他 profile 保持既有 boot quiet 行為；以 PTY 覆蓋實際 RX 靜默與各 recovery 入口。

### Fixed

- Smart4 shell prompt 後立即接非同步 Wi-Fi log 時，保留 hostname 行首與 `#` 邊界而不要求行尾，避免已完成命令誤判 timeout／partial 並注入 recovery 按鍵。出貨模板 PTY 測試涵蓋 nonce 與 marker 命令；實機驗證由 TestPilot ST-06 案例執行。
