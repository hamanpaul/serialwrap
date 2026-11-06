---
type: fix
scope: rust-console
---
Rust daemon：並行的 `session.self_test` probe 各自保存並配對自己的 human suspend／resume（READY `session.recover` 完成不再解除另一個 probe 的 suspend），重疊 self-test 不再讓 human 按鍵永久停在 deferred；停在回放 hold 的命令被 `command.cancel` 後，回放結算或 drain 會結算在途指標，不再永久占用一格佇列額度（`SESSION_QUEUE_FULL` 提前一格）。
