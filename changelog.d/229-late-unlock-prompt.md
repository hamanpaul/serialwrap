---
type: fix
scope: console-unlock
issue: 229
---

修正 console unlock 空行回應晚於一秒、於第二次 RX 靜默等待到達時仍送出解鎖值的問題；送 secret 前重新檢查此次空行後的新 RX，包含 prompt 後緊接非同步 log，已解鎖時只驗 nonce。加入真 PTY 延遲回覆回歸測試（#229）。
