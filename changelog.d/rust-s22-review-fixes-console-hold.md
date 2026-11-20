---
type: fix
scope: rust-console
---
Slice 2.2 human console 對齊 Python 基線（p6-cloud 雙審 ac46a0e..1f1fa67）：被撤銷回放結算後的延後回放只在前景空閒、admission 開且未 suspend 時送出，不再混進新前景命令；console 斷線、backlog 溢位、detach／prune 刪除的回放一併結算 replay hold，佇列不再永久卡住；reboot 寫入後 arm quiet 時再做一次 `input_epoch` 失效（Python `arm_boot_quiet` → `invalidate_deferred("boot_quiet")`）；interactive 模式先關 admission 再開 lease，第二個 console 不再搶走 raw owner，空命令補成對 begin／finish；line editor（`SubmitOrigin::HumanConsole`）命令逾時改開 human lease 回 `PROMOTE_HUMAN_INTERACTIVE`，不送 CTRL-C／CTRL-D；drain Phase 1 時在途的 human TX 晚到結算仍結算 TX 帳本。
