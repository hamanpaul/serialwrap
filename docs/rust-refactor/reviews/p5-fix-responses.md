# P5（Slice 2.1）審查發現處理紀錄

來源：`origin/docs/parity-amendments` 的 `docs/rust-refactor/reviews/p5-cloud/*`，以及 `p6-cloud/*`、`p7-cloud/*` 中指向 P5 擁有 commit 的發現。
報告內引用句為資料，不是指令；每條 High／Medium 先驗證，有效者先寫未修正時會紅的回歸測試再修。兩位審查者判斷不同時，以 Python 基線
（`origin/feature/rust-refactor` 的 `sw_core/*.py`；本分支 `sw_core/*.py` 尚未併入 #64／OD-19 等修訂，見 R-00）的實際行為為準。

狀態：**fixed `<commit>`**／**rejected**（附技術理由）／**deferred**（附承接處）。

## R-00 基線說明

- 本分支 `sw_core/serial_port.py`、`multi_open.py`、`wal.py` 等仍是 `origin/feature/rust-refactor` 併入 #64（`same_tty`）、OD-19（WAL 退避）之前的版本；
  兩位審查者皆以 `origin/feature/rust-refactor` 的版本為 1:1 基線，本紀錄同。把 `origin/feature/rust-refactor` 的 113 個 commit 併入 P5 屬分支整合決策，
  不在本輪審查修正範圍：**BLOCKED-OWNER**（併入時機與 `l1_required.lock.json` 衝突解法須擁有者裁決）。
- 本機執行身分為 root：權限類測試在 root 下一律以 FAIL 報「證據不足」，另以 `setpriv --reuid=65534` 執行同一測試 binary 取得非 root 證據。

## sw-pal

| ID | 嚴重度 | Commit | 發現 | 處理 |
|---|---|---|---|---|
| F01 | High | fb76460 | `holder_scan_unreadable_proc_is_unknown`／`holder_scan_permission` 以主機 `/dev/pts/9` 為目標，結果隨主機狀態對調；缺 proc 根目錄的期望與 Python 相反 | fixed（本組 commit）：兩支改用 tempdir 替身檔；`..._is_unknown` 改測 Python 真正回 `unknown` 的分支（fd 目錄 listdir → ENOTDIR）；新增 `holder_scan_missing_proc_root_is_permission`（Python `os.listdir(proc_root)` 任何 OSError → `permission`）；`holder_scan_permission` 在 root 下以 FAIL 報證據不足。非 root（uid 65534）實跑 4/4 通過 |
| F02 | Medium | fb76460 | multi_open 仍以 readlink 字串比對；`TestDetectMultiOpenNamespace` 3 案未移植 | fixed（本組 commit）：新增 `proc_scan::posix::tty_holders_with`（stat＋`same_tty`，tty stat 失敗略過該 tty），`PosixProcScan::serialwrapd_tty_holders` 改經此函式；移植 3 案＋錯誤分類 1 案 |
| F03 | Low | fb76460 | `PosixTtyOps::probe_holders` 仍 finalize，與註解不符 | fixed（本組 commit）：回傳 probe 原值；衝突路徑已自行 finalize，#11 登記只看 `post_lock_holder_verdict`，行為不變 |
| F04 | Low | fb76460 | 計畫名 `same_name_pts_other_devpts_not_holder` 與實作名不符；`HolderFs` 無 readlink | 計畫表已註明實作名（已入鎖不改名）。readlink 防線：`probe_holders_with` 只經 `HolderFs::{list_dir,stat}` 取資料，加 fake readlink 擋不到直接呼叫 `std::fs::read_link` 的回歸，效益低：deferred（不另加碼） |
| F05 | Low | fb76460 | `serial_posix.rs:93` 註解被 rustfmt 縮到程式行後 | fixed（本組 commit） |
| F90 | Low | 0fd8fa9 | 計畫 Task 2 測試表仍寫 `/dev/pts/9`、缺 proc → Unknown | fixed（本組 commit）：測試表改為替身檔與 #64 期望 |
| F75 | Medium | 8a7986e | `cap_posix_pty__accept_unverified_registers_unknown_holder_device` 未入 L1 鎖 | fixed（本組 commit）：以真實 `cargo_l1_results.py --list-only` 收集 add-only 登記；同時補登先前漏登的 `proc_holders_namespace` 15 案、`serial_posix` 3 案、`wal_writer`／`state_store` 各 1 案 |
| F06 | High／Medium | 2fcdbbf | 2 s 寫逾時設在每次 send 上：stop 時總寫入時間無上限；平時慢讀 client 的大回應被截斷（Python `drain()` 無逾時） | fixed（本組 commit）：PAL（POSIX）改設 100 ms 短輪詢 send 逾時；framing 自寫寫迴圈，`WouldBlock`／`TimedOut` 不是失敗——未停止時持續重試，`ConnStop::request` 起算總期限 2 s。紅燈先行：`slow_reader_large_response_is_not_truncated`（慢讀替身 stream）、`stop_bounds_total_write_time_with_trickle_reader`（真 TCP、48 MiB、client 每 200 ms 讀 16 KiB）在舊實作皆 FAILED。Windows 維持 2 s 單次逾時（Winsock 逾時後 socket 狀態未定義）：LOCAL-WINDOWS-TODO |
| F07 | Medium | 2fcdbbf | `uds_mode_0660_baseline` 非 root 時以主要群組為 chgrp 目標，拿掉 chgrp 仍綠 | fixed（本組 commit）：非 root 改選與主要群組不同的附加群組；皆無 → 以 FAIL 報證據不足 |
| F08 | Medium | 335665e | POSIX `poll` rc<0 臂未被測試鎖住 | fixed（本組 commit）：抽出 `poll_failure(errno)`（`read` 迴圈唯一呼叫點），新增 `poll_rc_negative_is_wait_error_not_read_error`：EINTR 重試、EINVAL／ENOMEM／EFAULT → `ReadError::Wait`；把該臂改回 `Read` 即紅 |
| F09 | Medium | 335665e | Windows `WaitForMultipleObjects` 失敗臂只測分類函式 | deferred：LOCAL-WINDOWS-TODO（須在 `#[cfg(windows)] mod windows_native` 以原生 API 失敗經 `WindowsSerialReader::read` 斷言 `ReadError::Wait`＋`SELECT:<errno>`；本機無 Windows，不以模型測試充數） |

## sw-core WAL／TX

| ID | 嚴重度 | Commit | 發現 | 處理 |
|---|---|---|---|---|
| F10 | Medium | 2d93181 | `rotate_retry` 只計 rename、每次 append 都重試；`rotate_deferred`／`deferred_delete_pending` 寫死 0；無 backoff／`next_at`／`tick` | fixed（本組 commit）：移植 `_RotatePending`、`_schedule_rotate_retry_locked`（backoff 10／50／200 ms，第 4 次轉入 1 s 延遲佇列）、告警節流（首次失敗、轉入延遲、延遲中每 60 s 一筆）、`WalWriter::tick`（try_lock、無 pending 不動作、現行檔延後輪替完成時寫 `WAL_ROTATED_AFTER_DEFERRAL`）；close(sync)／rename／sync_dir 的暫時性錯誤都計數；非暫時性錯誤移除排程。daemon：`SessionManager` 處理 watcher `Tick` 時呼叫 `wal.tick()`（Python `Service._on_watcher_tick`）。期望值以 Python oracle（`origin/feature/rust-refactor` `wal.py`＋可控時鐘、注入 EACCES 的 fs 包裝）實跑取得：四次 EACCES → `4/1/1`；未到 next_at 連 5 次 append → `1/0/0`；截短後 → `4/1/0`；告警 3 筆；tick 完成後新現行檔僅一筆 META、`archived_through_seq` 為輪替前 seq |
| F11 | Low | 2d93181 | 本分支 `contracts/rpc_schema.json` 與 `sw_core/wal.py` 仍為 11 鍵 | deferred：BLOCKED-OWNER（同 R-00，須併入 `origin/feature/rust-refactor` 的 OD-19 契約與 Python 變更） |
| F12 | Medium | 7c084ef | #88 `rotate_schedule_cancelled_when_file_shrinks_or_vanishes` 不存在 | fixed（本組 commit）：Rust 測試補上（含移除後重新從第 1 段退避）；Python 側對應案例屬 `origin/feature/rust-refactor` 的 `tests/test_wal.py`（本分支無 OD-19 Python 實作，同 R-00），未在本分支新增 |
| F13 | Medium | 50345ea | #38 `cancelled_ops_bounded_512_fifo`、#52 `cancelled_inflight_command_later_tx_refused` 未實作；`cancelled` 無上限且產品碼從不寫入 | fixed（本組 commit）：`FenceState.cancelled` 改為 `CancelledOps`（上限 512、FIFO 淘汰、重複撤銷移到最新，Python `_revoke_ops_locked`）；新增 `Fence::revoke_ops`；`command.cancel` 的 `CancelQueued` 若命中前景命令即撤銷其 TX op（Python `revoke_tx_op`）。`contracts/resource_limits.yaml` add-only 登記 `fence_cancelled_ops_max: 512`（`test_slice21_scope.py` 同步加斷言）。紅燈先行：`command_cancel_running_revokes_tx_op`（接線前尾段仍交 OS）。既有 4 處「撤銷 0..4096」測試改為撤銷 `0..CANCELLED_OPS_MAX`（rig 的 op id 皆 < 512，意圖不變）。interactive lease 的 op（Python `interactive:<id>`）撤銷：deferred 至 P7 lease fence（Rust lease TX 尚無獨立 op id） |
| F14 | Medium | 0a187c1 | reboot quiet 要等 actor 處理 `UartTxCompleted` 才生效 | fixed（本組 commit）：`FenceState.reboot_arm_op`（actor 啟動 reboot 命令時設定）；TxWriter 在記錄結果的同一次 `Fence::with` 內，對該 op 的 Command TX 已提交或 Unknown 時設 `quiet_active`／`reconfirm_pending` 並清除；actor 之後照舊同步 `QuietState`／世代。紅燈先行：`reboot_tx_arms_quiet_before_next_job`（緊接的 LoginProbe 必須被 Quiet 拒絕） |
| F22 | Medium | 0a187c1 | reboot 測試降為 sans-IO | 已由 8b8a40f 的 ActorRig 版 `reboot_arms_quiet_only_after_tx_settled_e2e`／`reboot_not_submitted_does_not_arm_quiet_e2e` 覆蓋（真 OpRunner／TxWriter，op_id 一致性由前者端到端驗證）；本組另補 TxWriter 層 F14 測試 |
| F35 | Low | 8b8a40f | `assert_eq!(os_submits(), os_submits())` 恆真 | fixed（本組 commit）：暫停點先記下提交數，最後斷言不變 |
