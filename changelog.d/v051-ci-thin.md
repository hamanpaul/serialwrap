---
type: fix
scope: tests
---
將長跑 observer 樣本測試改為完成六筆樣本後停止，以虛擬時鐘取代固定真實時間窗；同時保留至少四筆樣本須在原 0.3 秒窗口內完成的頻率判準與原有紀錄形狀斷言。

另將 UART writer 停滯測試的期限與每步等待等比例放大，並驗證總排空時間確實超過停滯期限；L1.5 update 測試改以 rig 私有 by-id 目錄的 inotify 事件確認連結移除與復原後各有一次新掃描，仍保留 3 秒窗口及零 TX 判準。console overflow 測試先持有兩個 console PTY，再以 h1 的明確 client_id detach，固定 line-buffered 測試前提。

依擁有者 OD-115，DF-23 黑箱測試改為只在 Rust 執行，並記錄 Python 0.4.x 的時序相依終態與偶發一次自動重試。
