---
type: fix
scope: test-infra
---

Windows serial endpoint 改用高解析度 `perf_counter` 計時；讀寫於鎖等待後重算剩餘期限。bridge core 以 50 ms 呼叫 `read()`；當端點期限較短時，endpoint 只取 driver buffer 現有 bytes，沒有可讀資料時每 10 ms 輪詢一次 `in_waiting`，不進入固定 50 ms 的 pySerial 阻塞讀取。寫入 timeout 依 1 ms driver 精度向下量化，降低亞毫秒抖動引起的 setter churn；setter 耗時後若已設定 timeout 超出呼叫剩餘預算，端點不送出資料。DSR 在 reconfigure setter 前與 write 前都重新取樣；保留 busy-reader、鎖等待期限與未送出資料回歸案例。

期限邊界以可控時鐘驗證，包括 read/write 鎖等待、setter 完全或部分耗盡期限、零期限空讀取與 DSR 在鎖等待期間關閉。Bridge core 的 Windows serial 測試替身補上 `in_waiting`，與短期限讀取使用的 pySerial 介面一致。
