---
type: feat
scope: test-infra
---
新增 `uart_stdio_bridge` bridge 核心：TX／RX 獨立 worker、有界背壓、DUT stderr 持續排空、loopback 權杖控制通道、checkpoint 與 DUT／傳輸故障動作。資料以 binary bytes 轉送並依實際接受長度留證；不確定寫入與證據失敗會 fail-stop。
