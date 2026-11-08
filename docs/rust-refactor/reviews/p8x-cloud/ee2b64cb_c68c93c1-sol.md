> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本次無前輪報告；commit 訊息引用的 P8X-ee2b64c-1 屬於前一包審查，本輪三個 commit 即為其修正，其對應驗證歸入 B 段）。

## B 新發現

審查範圍 ee2b64cb..c68c93c1，三個 commit，無 merge commit。

**核對結果摘要**
- c2908c0d（紅燈測試）：sw-core `registry_admission` 5 案與 sw-daemon `frozen_submit_validation_errors_precede_admission` 的斷言逐一對照 Python 基線 `sw_core/arbiter.py:196-275`（驗證順序：completion regex → expect_reboot → CMD_TOO_LONG → CMD_CONTAINS_NEWLINE → ticket → SESSION_NOT_READY → SESSION_QUEUE_FULL）與 `sw_core/service.py:1224-1252`（INVALID_ARGS、`_resolve_session_id` 狀態閘門皆在 arbiter 之前）；期望值與 Python 一致。`"expect_reboot":"yes"` 在 Python `sw_core/expect_reboot.py:24` 回 INVALID_EXPECT_REBOOT，與測試一致。
- 69e5d70c（修正）：
  - `crates/sw-core/src/arbiter.rs:202-224` 的 ticket 時點、SESSION_NOT_READY／SESSION_QUEUE_FULL 歸還（`:229`、`:239`）、human console 豁免，均對應 Python `arbiter.py:230-233`、`:266-267`、`:274-275`、`service.py:389`。
  - COM 來源：Rust 取 `session_id` 尾段，Python 取 `_admission_com(params)`＝`profile.com`。兩端 session_id 一律為 `profile_name:COM`（Python `session_manager.py:1029`／`:1634`，Rust `session_core.rs:606`／`session_manager.rs:689`；`sid_override` 亦同構），尾段即 COM，等價。
  - ticket 歸還時點：Python 於 worker 終態即時歸還；Rust 改為在每次 RPC submit 前與 `handoff_stop` 判定前（`service.rs:397`）回收。gate 的 live tickets 僅在 `freeze_if_idle` 被讀取，而該呼叫前必經 reconcile，故外部不可觀察差異。`shutdown_freeze` 為無條件凍結，flash sync-probe 只看 frozen，不受影響。
  - 鎖序：registry inner → gate（葉鎖，`admission.rs:137` 的 `done` 無回呼），`reconcile_admission` 先放 inner 再 `done`；無反向持鎖路徑。
  - `ADMISSION_METHODS` 移除 `command.submit` 後 32 項與 Python `_ADMISSION_METHODS` 逐項相同。`strip_frozen_session` 只對 `FLUSHED_BY_SHUTDOWN`＋`handoff_frozen` 移除 actor 補的 `session`，與 Python 凍結回應同形。
  - daemon 與 ServiceRig 皆只建一個 `CommandRegistry` 並以同一 Arc 注入 manager 與 Service（`daemon_main.rs:1109`／`:1131`／`:1253`），gate 共用成立；`flash_coord.set_admission(svc.admission())` 仍接同一 gate。
- c68c93c1（黑箱）：`Installed`／`HeldPush`／守衛皆沿用既有輔助；Rust CLI 的拒絕路徑在 `crates/sw-cli/src/rollback.rs:4136-4144`（`safety_check` 早於任何 hold／systemctl），stub 經 PATH 生效（`setup_cmd` 以 `systemctl`／`sudo` 相對名稱呼叫）。L1 lock 為純新增 9 筆、0 刪除；cargo nodeid `cargo:<crate>::<target>::<mod>::<fn>` 與 `scripts/rust/cargo_l1_results.py:133` 格式一致；pytest 案例同時登記 p7 與 s21，handoff_stop／admission 的 cargo 條目與既有者同在 p7 單一集合。
- 實跑：`cargo test -p sw-core --test admission` 13 passed；`cargo test -p sw-daemon --features test-fakes --test handoff_stop` 15 passed（含兩個新案）。

NO FINDINGS

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行
