---
type: fix
scope: rust-core
---

區分保管帳本的純觀察等待與關閉義務，避免開埠在途的重複 device.attach 把正常 bridge 取消排空。
保留接手確認前的回覆等待與 CustodyPoll 補收；release、bind、shutdown 加入時只升級關閉義務，維持 D1／T-1。
