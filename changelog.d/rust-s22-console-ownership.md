---
type: feat
scope: rust-console
---
第一 console raw ownership、suspend／resume 巢狀計數、admission gate、單筆在途的 human TX lane 與 OS 寫入前 input_epoch／ownership 重驗（07 Task 6）：`OwnershipState` 移植 `uart_io.py` 的 owner／pending／suspend 規則；`SessionCore` 處理 `ConsoleInputBytes`／`ConsoleDisconnected`／`ConsoleConnected`／`HumanTxCompleted`，提供 `fg_begin`／`fg_finish`、`human_suspend_begin`／`end`、`invalidate_human_input`（BC-02 唯一入口）與 `foreground_busy`（含 D-22 回放保留）；`Fence::admit` 依 D-03 對 HumanRaw 比對 input_epoch→owner_gen→admission、對 HumanReplay 比對 input_epoch→replay_auth；新增 `HumanTxPump`（`serialwrap-human-tx-<COM>`）。drain Phase 1 撤銷回放、清 deferred、重設 ownership 並記 retained human owner，Release 關閉全部 console；self-test probe 期間 suspend human；banner／reboot 命中點遞增 input_epoch。
