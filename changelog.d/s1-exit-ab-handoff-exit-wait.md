---
type: fix
scope: cli-setup
issue: null
---

修正 update／rollback／`setup` 中斷復原的交接停止等待：daemon 回 `daemon.handoff_stop` 後先保留 3 秒排空窗口才關閉監聽，CLI 卻自 grant 起只等 10 秒，排空窗口吃掉 SC-12「ping 不通且行程消失，上限 10 秒」的時間，已凍結、正在有序關機（bridge drain、thread join、WAL fsync）的 daemon 稍慢結束時，on-demand 模式即回 `TIMEOUT`（`reason:"handoff_timeout"`）並保留 journal。仍回 ping 時維持 grant 起 10 秒上限；只在 ping 於連線階段以 `ConnectionRefused`／`NotFound` 失敗且舊行程仍在時，改自監聽關閉起算 10 秒；ping 恢復時清除此計時。recv 逾時等仍連得上的失敗維持 grant 起 10 秒上限。
