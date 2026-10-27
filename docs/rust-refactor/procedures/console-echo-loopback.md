# Console 按鍵至回顯 SLO 量測（BLOCKED-OWNER #7）

對應規格 §1.2 與 `plans/07-slice22-console.md` BLOCKED-OWNER 總表 #7：human console 單鍵從寫入 console 到同一 console 讀回
UART 回顯的延遲，p50 ≤ 3 ms、p99 ≤ 10 ms。in-process 回歸門檻為 `crates/sw-core/tests/console_echo_latency.rs`；本程序量測實體鏈路，
**不得以 Docker 或 Python bridge 取代**。

## 前置條件

1. 實體 USB-UART，TX 與 RX 短接（loopback），115200 bps 8N1；by-id 已綁定為 COM0（profile `platform: passthrough`）。
2. Rust bundle 已部署，daemon 於該機以正式監管模式執行；機器無其他負載（記錄 load average）。
3. 證據目錄 `EVID=target/console-echo-loopback/<yymmdd-hhmmss>/`。

## 逐步驟

1. `serialwrap session console-attach --selector COM0 --label echo` 取得 `vtty`（Linux）或 `endpoint`（Windows Telnet）。
2. 以量測腳本開啟該 console（Linux：`O_RDWR|O_NOCTTY`；Windows：TCP 並完成 Telnet greeting），送 500 個可識別樣本：每個樣本為單一
   可列印 byte（循環 `0x21`～`0x7e`），送出後等待讀回同一 byte 再送下一個，以 monotonic clock 記錄送出與讀回時刻（ns）。
3. 樣本間隔 ≥ 20 ms；任一樣本 1 s 內未讀回記為 timeout（計入 p99）。
4. 計算 p50、p99、max，寫入 `summary.json`。

## 證據檔名清單

- `echo_latency_samples.csv`：500 列，欄位 `sample_id,sent_ns,echo_ns`
- `summary.json`：`{"p50_ms","p99_ms","max_ms","timeouts","baud","uart","host","bundle_version","load_avg"}`
- `env.json`：USB-UART 型號與 by-id、OS 與核心版本

## PASS 條件

500 列齊全、`timeouts == 0`、`p50_ms <= 3`、`p99_ms <= 10`。樣本不足 500、非實體 loopback 或未記錄環境 → `INSUFFICIENT`，不得 PASS。

## 失敗時記錄

保留全部樣本檔，於 `fail.md` 記錄量測時的負載、USB 拓樸（hub／直連）與 daemon stderr；不得刪除離群樣本後重算。
