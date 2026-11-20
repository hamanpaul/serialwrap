---
type: fix
scope: rust-core
---

修正 RELEASED 佔位 session 在預開成功、退休驗證因裝置離線而失敗後，關閉通知遇到滿載佇列時未啟動補收，造成 device.attach 無法完成的問題。登記關閉等待後啟動 CustodyPoll，補收成功終態後回覆 DEVICE_NOT_PRESENT、清除等待，裝置恢復上線即可重新交接。
