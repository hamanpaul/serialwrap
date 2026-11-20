---
type: fix
scope: rust-core
---

統一保管帳本的在途判定；自身與 by-id 認領的等待都會補收遺失的關閉通知，成功後完成 RELEASED、清除 drain 欄位並落盤。
一般開埠已提交、actor 尚未接手時，改綁會取消原開埠並等待配對排空回條；期限內成功才改綁，逾時保留 RECOVERING，遲到的開埠不再觸發登入。
