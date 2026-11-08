## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8X-b837cfc-1 | High | 已修 cbb03497 | 0 | 暫時凍結期間保留重探結果，撤銷後由 tick 回填；eb14bb92 新增核心與 service 回歸案例。原情境已修正，另有下述取消交錯缺陷。 |

## B 新發現

**P8X-cbb0349-1 [High] [crates/sw-core/src/session_core.rs:3566](crates/sw-core/src/session_core.rs:3566) — 暫時凍結延後 SC-30 取消，舊成功結果可重新解除 boot gate。**

具體情境：

1. COM0 為 `ATTACHED/PROMPT_UNAVAILABLE`，自動重探已算出 `Ready`，但工作執行緒尚未送出 `LoginFinished`；COM1 的背景 TX 阻住交接 fence。
2. admission 進入 `Pending`，COM0 收到 `U-Boot 2024.04` banner。`sc30_cancel_login_run()` 呼叫 `auto_reprobe_finished(...Aborted(Quiet))`，新增分支僅暫存取消結果，保留原 `login_run`。
3. 舊 `LoginFinished(Ready)` 隨後抵達，仍通過原 run 的 `(epoch, fsm_gen)` 檢查，覆寫暫存的取消結果。
4. COM1 fence 逾時，交接撤銷；下一個 tick 套用舊 `Ready`，呼叫 `confirm_ready()`。

預期：依 **OD-35／SC-30**，banner 已取消該 run，舊成功結果不得推進 READY，`ready_reconfirm_pending` 必須保留。實際：COM0 進入 `READY`，quiet／再確認 gate 被清除，agent 命令可進入重啟中的設備。工作執行緒在取得結果後直接送出事件，沒有再次檢查取消（`session_manager.rs:2244–2254`），因此此交錯可達。修正前凍結分支會取走 `login_run`，晚到成功結果無法通過檢查。

確切修法：SC-30 取消時立即使原 run 身分失效；暫時凍結只延後狀態回填。另存取消結果及等待者，拒收該 run 的後續成功事件，並新增「Pending → banner 取消 → 舊 Ready 抵達 → 交接撤銷」回歸案例。

驗證限制：以上為靜態呼叫鏈確認。定向 Cargo 測試因指定 target 的 `debug/.cargo-build-lock` 遇到 `Read-only file system (os error 30)`，未能執行；未跑完整 pytest、docker 或 bundle 測試。凍結鎖僅新增兩筆，未修改任何檔案。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正