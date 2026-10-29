# Stage 1 出口審查 A 修正雲端回報（2026-10-29）

- 分支：`origin/feature/s1x-a-fixes`（自 `origin/feature/rust-refactor` @ `62ffec7` 切出；只 push 此分支，未開 PR、未改 `VERSION`）。
- 依據：`docs/rust-refactor/reviews/s1-exit/opus-a-20261029.md`（審查 A）與 `docs/rust-refactor/reviews/rulings/s1.md` 的 S1X-a-* 裁決。報告內容當資料使用，每條都先寫在修正前會紅的回歸，再修。
- 範圍：S1X-a-1、a-3、a-4～a-8。未處理 S1X-a-2（待擁有者確認；擁有者已裁決 (a)，併入 OD-48 分支）與所有 Low（S1X-a-9～a-17，已收進 backlog）。
- 1:1 基準：`v0.3.2` 的外部可觀察行為；行為差異只落在已核准的 SC-03（`AUTOBOOT_QUIET`＋`retry_after_s` 用於 file.*）。

## 環境

- 一般使用者 `ubuntu`（uid 1000）跑測試；Python 3.12（venv：PyYAML、pytest、jsonschema、pyserial）；policy engine 依 CLAUDE.md 釘定 SHA `2dcf2f8` 安裝。
- Rust 1.96.1 只裝在 root；一般使用者的測試環境沒有 cargo，所以 Rust／bundle 類測試在起點就是紅的（見下方基準）。本範圍只改 Python。
- 每個 commit 都在獨立 `git worktree`（detached 於該 commit）跑全套 `python -m pytest -q tests/`，同時最多兩套並行；`policy_check --repo .` 與 `python -m tools.contracts check` 也在同一個 worktree 跑。

## 起點基準（`62ffec7`）

`6 failed, 5256 passed, 197 skipped, 1 xfailed, 1 error`（29m52s）。7 個失敗 nodeid 都是環境類（沒有 cargo／Rust 產物、沒有 bundle 或 P4 證據）：

```
ERROR  tests/contract_blackbox/test_mgmt_stubs.py::test_mkbundle_release_manifest_matches_parser_contract
FAILED tests/contract_blackbox/test_cli_mgmt_com0com.py::test_bundle_license_files_present
FAILED tests/contract_blackbox/test_cli_mgmt_doctor.py::test_doctor_ab_version_pairing_in_bundle
FAILED tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only
FAILED tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
FAILED tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields
FAILED tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
```

## Commit 清單與逐 commit 驗證

每個 commit 的失敗集合都**等於**起點基準，沒有多出的失敗，所以不需要同範圍重跑取證。三項檢查（policy_check、contracts check、pytest 失敗集合）在每個 commit 都通過。

| # | commit | 主旨 | pytest 全套 | policy_check | contracts check |
|---|---|---|---|---|---|
| 1 | `9cb3c4c` | fix(interactive)：interactive 命令改用 lease permit 並於送出失敗時關閉 lease（a-1） | 6 failed／5260 passed／197 skipped／1 error，失敗集合＝基準 | 0 fail | ok |
| 2 | `ad0c001` | fix(device)：attach 進行中再次 device attach 回 ok＋session（a-3） | 6／5267／197／1，＝基準 | 0 fail | ok |
| 3 | `9691182` | fix(file-transfer)：傳輸中 TX 被拒時映射為 file.* 既有錯誤碼（a-4） | 6／5275／197／1，＝基準 | 0 fail | ok |
| 4 | `97b52ed` | fix(setup)：舊 daemon 存活掃描略過他 uid 並以預設 endpoint 比對（a-5） | 6／5279／197／1，＝基準 | 0 fail | ok |
| 5 | `93ef5e1` | fix(diagnostics)：一般使用者 daemon 的 foreign_holders 回到 0.3.2 形狀（a-6） | 6／5292／197／1，＝基準 | 0 fail | ok |
| 6 | `6284a6b` | fix(session)：release／clear 收尾欄位對齊 0.3.2（a-7） | 6／5295／197／1，＝基準 | 0 fail | ok |
| 7 | `5446203` | fix(interactive)：送出失敗僅關閉仍屬本命令的 lease（a-1 補強） | 6／5296／197／1，＝基準 | 0 fail | ok |
| 8 | `6485e90` | test(s1x-a)：回歸測試模組在無 os.getuid 平台仍可收集 | 6／5296／197／1，＝基準 | 0 fail | ok |

每個 commit 另跑過受影響的測試檔（新增的回歸檔，加上 `test_stage0_tx_permit.py`、`test_stage0_session_drain.py`、`test_device_handoff.py`、`test_stage0_uart_exclusive.py`、`test_holder_probe_namespace.py`、`test_multi_open_detect.py`、`test_stage0_platform_mgmt.py`、`test_setup_cli.py`、`test_contracts_keep_baseline.py` 等），都通過。

每個 commit 都帶 `changelog.d/s1x-a<N>-*.md` fragment（#7 沿用 a-1 的 fragment，#8 只改測試）；trailer 含 `Co-authored-by: Copilot …` 與 `Co-Authored-By: Claude <noreply@anthropic.com>`。

**`contracts/daemon_logging.yaml` 行號同步**：`tests/test_contracts_keep_baseline.py::test_daemon_logging_all_calls_listed` 以 `source: 檔:行` 釘住每個 logger 呼叫的位置，所以在 `session_manager.py`／`serial_port.py` 增加程式行就會讓既有項目行號位移。#1～#7 各自只更新受影響 `messages[].source` 的行號，沒有增刪項目，也沒有改模板或等級，沿用 `d4ec3e4`、`91ed98a` 的前例。

## 各條處理

### S1X-a-1（High）— 照修，成立

- **紅燈**：真 `UARTBridge`＋`FakePort` 送 `mode=interactive`、`"top"`，回 `{'ok': False, 'error_code': 'SEND_FAILED'}`，`port.writes == []`，lease 遺留。
- **修法**（`sw_core/session_manager.py` `_execute_command_inner` interactive 分支）：
  - permit 改為 `TxPermit(f"interactive:{lease.interactive_id}", bridge_generation, "interactive", owner_gen=…)`。
  - 送出拋例外或被拒時關閉剛開的 lease，再把例外交給 `execute_command` 既有的 `_map_aborted_result` 映射。#7 補上條件：只在 session 現行 lease 仍是本命令開的那一個時才關。
  - **連帶修正**：permit op_id 改用 lease id 之後，`test_stage0_tx_permit.py::test_cancel_between_dequeue_and_manager_registration_zero_tx[interactive]`（L1 鎖內）轉紅。原本是靠 a-1 的缺陷「必失敗」才綠的。原因：取消發生在 lease 開啟之前，`revoke_tx_op` 只撤銷得到 `cmd_id`。修法是送出前若 bridge 撤銷帳本已有 `cmd_id`，就把撤銷延伸到本命令的 lease op。測試本身沒有改。
- **回歸**（`tests/test_s1x_a1_interactive_permit.py`）：
  - `test_agent_interactive_command_writes_bytes_and_returns_ok`：`ok:true`，寫出 `top\n`，WAL 有 TX。
  - `test_interactive_lease_usable_after_interactive_command`：lease 可續送；關閉後下一個命令不是 `SESSION_INTERACTIVE_BUSY`。
  - `test_human_line_console_interactive_program_reaches_uart`：human line console 的 `vim /tmp/x` 經 `_human_console_mode` 判為 interactive，寫出到 UART。
  - `test_rejected_interactive_send_closes_opened_lease`：送出被 drain fence 拒絕時零 TX，lease 與 gate owner 都清掉，下一個命令可送。
  - `test_rejected_send_does_not_close_lease_replaced_meanwhile`：#7 的補強。

### S1X-a-3（High）— 照修，成立

- **紅燈**：第一次 `attach_device` 進 probe 後，第二次呼叫回 `SESSION_RECOVERING`。
- **修法**：新增 `_attach_inflight_draining_locked`。只有下列情況才保留 `SESSION_RECOVERING`：
  - 有 `_pending_drain`；
  - by_id 在 `_draining_by_ids`；
  - 在途 op 帶 `release_request` 或已 `cancelled`；
  - session 為 RELEASED，或 by_id 在 `_released_by_ids`。

  其餘情況，第一段與第二段鎖內的在途檢查都回 `{"ok": True, "session": session.to_public_dict()}`，與 0.3.2 相同，也不會再 spawn 一次 attach。
- **回歸**（`tests/test_s1x_a3_attach_inflight.py`）：
  - `test_second_attach_during_login_returns_ok_session`：真 attach 流程，probe 卡住期間第二次呼叫。
  - `test_inflight_attach_tables_without_drain_return_ok[_attach_ops|_preopened_ports|_active_attach_ops|_attach_bridges]`。
  - 對照組（修正前後都綠，守住 SC-04）：`test_inflight_attach_with_release_request_keeps_recovering`、`test_inflight_attach_while_draining_keeps_recovering`。

### S1X-a-4（Medium）— 修正，錯誤碼映射受契約限制（見 OWNER-BATCH 1）

- **紅燈**：用 FakePort 模擬 shell，第 3 個 chunk 執行後 `arm_boot_quiet()`。結果拋出 `TxIncompleteError: tx incomplete: not_submitted 0/235 quiet`（echo 模式為 `0/64`），drain 時為 `… draining`；pull 也一樣。
- **修法**：
  - `sw_core/file_transfer.py` 的 chunk 迴圈捕捉 `TxIncompleteError`，然後 best-effort 執行 `cancel_input_line` 與 `_cleanup_remote`；這兩步再被拒就停，不會送 bytes 進 autoboot 倒數。接著在例外上附 `chunks_sent`／`chunks_total` 後再拋出。
  - `SessionManager.file_push`／`file_pull` 以 `_file_tx_rejected_result` 收斂：
    - `quiet` 且零寫出 → `AUTOBOOT_QUIET`，附 `selector`、`retry_after_s`。算法與 manager gate 相同：quiet 窗口取剩餘秒數，否則用 `READY_RECONFIRM_RETRY_S`。
    - 其餘情況（drain、世代過期、取消、flashing、部分寫出）→ `TRANSFER_TIMEOUT`，push 附 `chunks_sent`／`chunks_total`。
  - 例外不再穿越 RPC 邊界。
- **與報告修法的差異**：報告建議 drain 等情況回 `FLUSHED_BY_RECOVERY`、已寫出時回 `COMMAND_ABORTED_IN_FLIGHT`。但 `contracts/rpc_schema.json` 的 `x-methods.file.push`／`file.pull` `errors` 沒有這兩個碼；`contracts/error_codes.yaml` 也只把它們列在 `rec` 介面的 `command.get` 等方法；BC-03 的 approval 也只涵蓋命令。這兩個碼若要用在 file.*，就得改 oracle 契約，所以先回到 0.3.2 的傳輸結束碼 `TRANSFER_TIMEOUT`。它在 file.* 的既有錯誤碼內，也是報告所說的 0.3.2 行為。是否改用新碼列在 OWNER-BATCH 1。
- **回歸**（`tests/test_s1x_a4_transfer_tx_reject.py`）：
  - `test_push_quiet_mid_transfer_returns_autoboot_quiet[none|echo]`：`AUTOBOOT_QUIET`、`retry_after_s>0`、`chunks_sent==3`／`chunks_total==6`，quiet 後不再送 chunk。
  - `test_push_drain_mid_transfer_returns_transfer_timeout[none|echo]`。
  - `test_pull_quiet_before_read_returns_autoboot_quiet`、`test_pull_drain_before_read_returns_transfer_timeout`。
  - 模擬器自檢：`test_push_roundtrip_simulator_sanity[none|echo]`，修正前後都綠。

### S1X-a-5（Medium）— 照修，成立

- **紅燈**：同 uid、不帶 `--socket` 的 daemon，用預設 endpoint 查詢時回 `None`；另外三案在加入 `_proc_uid` seam 前為紅。
- **修法**（`sw_core/multi_open.py` `daemon_process_alive` 的 pid 未知分支）：
  - 以 `os.stat(/proc/<pid>).st_uid` 略過 uid ≠ `os.getuid()` 的行程。
  - 同 uid 但不帶 `--socket` 的 daemon，以 `constants.DEFAULT_ENDPOINT` 比對；daemon 不帶 `--socket` 時的 argparse 預設就是這個值。
  - 只有同 uid 行程讀取失敗，或 `/proc/<pid>` 仍在卻無法 stat，才算掃描不完整。
- **回歸**（`tests/test_s1x_a5_daemon_alive_scan.py`）：
  - `test_other_users_daemon_without_socket_is_ignored`
  - `test_other_users_unreadable_cmdline_is_ignored`
  - `test_same_uid_daemon_without_socket_uses_default_endpoint`
  - `test_wait_old_daemon_gone_completes_with_foreign_daemon`：舊 daemon 已停、pid 未知、主機上另有他人的 daemon 時，第一輪就回 True，不逾時。
- 既有的 `test_daemon_process_alive_requires_complete_evidence`（同 uid cmdline 不可讀 → None）照舊綠。

### S1X-a-6（Medium）— 照修，採報告第一個選項

- **紅燈**：
  - 他 uid 行程的 fd 不可讀時，`probe_holders` 回 `permission`／`holder_unknown=True`。
  - 開埠時對 `partial_other_uid` 寫出「無法確認…」WARNING。
  - f08 拋 `TypeError int() … not 'dict'`；p0 對 holder_unknown dict 判 FAIL `foreign_tty_holder`。
- **修法**：
  - `sw_core/serial_port.py`：
    - `probe_holders` 遇 `PermissionError` 時以 `_proc_owner_uid` 判斷擁有者。若只有他 uid 行程不可讀，回 `HolderReport(pids, False, "partial_other_uid")`。
    - 同 uid 不可讀，或擁有者無法判定（保守視同同 uid），仍回 `permission`／`holder_unknown=True`，符合 BC-04「/proc 不可讀時 holder_unknown:true」。
    - `_PosixSerialPort.open` 對 `partial_other_uid` 不寫 WARNING，也不 `_set_holder_unknown(True)`，所以 `foreign_holders` 回到 0.3.2 的 `{dev: pid}`。
    - 報告建議把 WARNING 降為 DEBUG；這裡改為不記錄。原因：daemon 只輸出 WARNING 以上（`daemon_logging.yaml` `min_level: WARNING`），而新增 logger 呼叫還要擴充 logging 契約。
  - f08 與 p0 照 `realhw/acceptance/gate.py::_foreign_holder_violations` 接受 `{device, holder_unknown, pids?}`：
    - f08：pids 為空 → 既有的 `tty_holder_not_reported` FAIL；pids 不等於主 daemon → `tty_holder_wrong_pid`。
    - p0：只有 pids 含非本 daemon 的 pid 才算外來持有者。
- **回歸**（`tests/test_s1x_a6_holder_shape.py`）：
  - `test_other_uid_unreadable_fd_is_partial_not_unknown`
  - `test_other_uid_fd_dir_unlistable_is_partial_not_unknown`
  - `test_same_uid_unreadable_fd_stays_unknown`（BC-04 守門）
  - `test_open_with_partial_other_uid_scan_is_not_holder_unknown`
  - `test_f08_tty_holders_reported_accepts_dict_entry[int|dict-own-pid|dict-other-pid|dict-no-pid]`
  - `test_p0_multiopen_accepts_dict_entry[int-own|dict-unknown|dict-own-pid|dict-other-pid|int-other]`
- 既有的 `test_holder_probe_namespace.py::test_permission_status_semantics_preserved` 照舊綠（假 /proc 為同 uid）。

### S1X-a-7（Medium）— 照修，成立

- **紅燈**：release 後 `detached_at` 仍是舊值；clear（裝置在場）後 `last_error` 為 `'CLEARED'`。
- **修法**（`_complete_drain`）：
  - release 分支：`session.detached_at = now_iso()`，並呼叫 `_reset_reprobe_progress_locked`。
  - clear 分支：裝置在場轉 ATTACHING 時 `last_error = None`。
  - oracle 取自 v0.3.2 的 `release_device`、`clear_session`、`_detach_session_locked`。
- **回歸**（`tests/test_s1x_a7_drain_fields.py`）：
  - `test_release_refreshes_detached_at_and_resets_reprobe`：另斷言 `last_error == "RELEASED"`，與 0.3.2 相同。
  - `test_clear_with_device_present_clears_last_error`
  - `test_clear_without_device_keeps_cleared`：對照組，修正前後都綠。

### S1X-a-8（Medium）— 停下，列入 OWNER-BATCH 2

- **重現成立**：state.json 預置 `released["gone-template:COM3"]={by_id:usb-Z}`，profile 內沒有該 template，用目前 HEAD 跑：

  ```
  run 0 sessions: [] blocked: ['/dev/serial/by-id/usb-Z']
  run 1 sessions: [] blocked: ['/dev/serial/by-id/usb-Z']
  device attach COM3: {'ok': False, 'error_code': 'SESSION_NOT_FOUND', 'selector': 'COM3'}
  after hotplug sessions: [] still blocked: True
  ```
- **停下原因**：報告的兩個修法都會碰到既有契約或釘住的行為。
  - **選項 (A) RELEASED 佔位 session**：與 `session_manager.py` 載入路徑明文的設計原則衝突（「缺少定義時必須保留未實體化的 RELEASED 阻擋，不能降級成另一種 profile」）。`tests/test_stage0_uart_exclusive.py::test_released_unavailable_uboot_template_remains_blocked` 也直接斷言 `"uboot-template:COM0" not in mgr2._sessions`。要做，就得改這條 DF-04 實作原則並削弱該測試。
  - **選項 (B) `attach_device` 接受 by_id selector、直接清除**：沒有 session 可回。`contracts/rpc_schema.json` 的 `x-methods.device.attach.result` 是 `required: ["ok","session"]`，`session` 為 SessionPublic，所以要改 RPC 契約（oracle 語意）。
- 本條沒有改程式，也沒有新增測試（不能加會紅的測試）。

## 不成立主張

無。a-1、a-3～a-8 都以修正前會紅的測試或腳本重現成立，因此沒有建立 `docs/rust-refactor/reviews/s1x-a-fix-responses.md`。

## OWNER-BATCH

1. **S1X-a-4 的 file.* 錯誤碼**：傳輸中 drain、世代過期或取消時，目前回 `TRANSFER_TIMEOUT`（0.3.2 碼）。要不要照審查 A 改回 `FLUSHED_BY_RECOVERY`，已寫出 ≥1 byte 時改回 `COMMAND_ABORTED_IN_FLIGHT`（BC-03）？若要，須同步：
   - `contracts/rpc_schema.json` 的 `file.push`／`file.pull` `errors`／`error_order`；
   - `contracts/error_codes.yaml` 兩碼的 `scope`（加 `rpc` 介面與 file.* 方法）；
   - BC-03 與 SC-04 的 `compatibility_impact`。

   這會改 oracle 契約，所以實作者沒有自行決定。
2. **S1X-a-8 解除孤兒 RELEASED 紀錄**：請在下列方案中擇一。
   - (A) 放寬「缺 template 不得實體化」原則，為孤兒紀錄建立 RELEASED 佔位 session（passthrough、`command_capable:false`），讓 `device attach COMx` 解除。須調整 DF-04 說明與 `test_released_unavailable_uboot_template_remains_blocked`。
   - (B) 擴充 `device.attach` result 契約，允許孤兒紀錄回 `{ok:true, released_cleared:true, device_by_id}`，不帶 `session`，然後由 dynamic attach 接手。
   - (C) 新增專用解除面（例如 `device unblock <by_id>`），須新增 SC。

   實作者建議 (A) 的變形：佔位 session 被 attach 時先移除自身、解除 by_id，再交給 template 偵測建立正式 session，這樣不會降級 profile。不過仍要先修改上述原則與測試。

## 回歸 case 政策（#155）評估

- a-1、a-3、a-4、a-5、a-7 都能以 pytest／FakePort／假 procfs 完整覆蓋，不需要實機 case。
- a-6 的實機面就是 `regression/…/f08_daemon_singleton.py` 與 `realhw/cases/p0.py` 本身，本次已修正並以 pytest 驅動兩個 case 函式釘住。不另加 regression case。

## 對 Rust 承接的提醒（依 rulings/s1.md「S1X-a 修正對 Rust daemon 的連帶」）

P5～P8 若沿用了修正前的 Python 行為，merge 主線後要依修正後的基線對齊，尤其是：

- a-1：interactive permit 用 `interactive:<lease_id>`，送出失敗時關閉 lease；
- a-3：attach 進行中回 ok＋session；
- a-4：file.* 的 quiet／拒絕映射；
- a-7：release／clear 收尾欄位。

## 未完成項

- S1X-a-8：見 OWNER-BATCH 2，待擁有者裁決後另開修正。
- S1X-a-4 的錯誤碼細分：見 OWNER-BATCH 1。
- 本環境沒有 Windows 機器；新增回歸只在 Linux 實跑。Windows 收集相容性由 #8 預先處理（模組層不呼叫 `os.getuid`，依賴 `/dev/null` 的案例標 `posix_only`），但沒有在原生 Windows 實跑。
