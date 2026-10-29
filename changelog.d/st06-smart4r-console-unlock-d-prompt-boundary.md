---
type: fix
scope: console-unlock
---

Smart4 shell prompt 後立即接非同步 Wi-Fi log 時，保留 hostname 行首與 `#` 邊界而不要求行尾，避免已完成命令誤判 timeout／partial 並注入 recovery 按鍵。出貨模板 PTY 測試涵蓋 nonce 與 marker 命令；實機驗證由 TestPilot ST-06 案例執行。
