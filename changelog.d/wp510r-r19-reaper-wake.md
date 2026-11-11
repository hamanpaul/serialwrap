---
type: fix
scope: rust-core
---

修正回收執行緒在關機期限逾時並 detach 後，因 wake 通道斷線而空轉耗用 CPU；回條仍可送達並完成收尾。
