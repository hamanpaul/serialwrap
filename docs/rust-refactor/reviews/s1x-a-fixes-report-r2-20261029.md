# Stage 1 出口審查 A 修正第 2 輪雲端回報（2026-10-29）

- 分支：`origin/feature/s1x-a-fixes`（起點 `6485e90`；本輪期間為唯一寫入者；未開 PR、未改 `VERSION`）。
- 依據：
  - 擁有者裁決 OD-62、OD-63（`rulings/s1.md` 末四列）。
  - 審查 A（`reviews/s1-exit/opus-a-20261029.md`）與上一輪回報（`reviews/s1x-a-fixes-report-20261029.md`）。
  - sol 增量報告 `reviews/s1-cloud/62ffec7_6485e90-sol.md`（開工後、做完 OD-62／OD-63 時 fetch 到；結論：需修正）。
  - 收斂規則 `reviews/review-convergence-rules.md`：只處理 A 段未修與 B 段新的 High／Medium，Low 不修。
- `origin/feature/rust-refactor`：開工時為 `62ffec7`（已含）；收尾 fetch 時新增 `897a3cd`（只改 `tests/test_rust_win_scripts.py`），以 `git merge --no-ff` 併入為 `9990b18`。凍結鎖未變更（每個 commit 的 `regen_lock.py --check` 都 OK）。

## 環境

- 一般使用者 `ubuntu`（uid 1000）跑測試；`/usr/bin/python3.12` venv（PyYAML、pytest、jsonschema、pyserial）；policy engine 依 CLAUDE.md 釘定 SHA `2dcf2f8`。
- 一般使用者沒有 cargo，Rust／bundle 類測試在起點就是紅的（見基準）。
- 全套 pytest 皆前景序列執行（同一時間只跑一套），每套開跑前確認沒有洩漏的 `serialwrapd`。

## 起點基準（`6485e90`）

`6 failed, 5296 passed, 197 skipped, 1 xfailed, 1 error`（32m01s），與上一輪相同的 7 個環境類節點：

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

| # | commit | 主旨 | pytest 全套 | policy_check | contracts check | regen_lock --check |
|---|---|---|---|---|---|---|
| 1 | `5b05626` | fix(file-transfer)：傳輸中 drain／世代過期／取消依 OD-62 回 FLUSHED_BY_RECOVERY 或 COMMAND_ABORTED_IN_FLIGHT | 6 failed／5306 passed／197 skipped／1 error，失敗集合＝基準 | 0 fail | ok | OK |
| 2 | `43700f2` | fix(session)：template 已不存在的 RELEASED 紀錄以佔位 session 由 device attach 解除 | 6／5315／197／1，＝基準 | 0 fail | ok | OK |
| 3 | `6c991f7` | fix(interactive)：原命令的取消於 TX fence 內擋下 interactive lease permit | 6／5318／197／1，＝基準 | 0 fail | ok | OK |
| 4 | `9990b18` | merge：併入 origin/feature/rust-refactor（`897a3cd`，只改 `tests/test_rust_win_scripts.py`） | 7／5317／197／1，多出 1 個（見下） | 0 fail | ok | OK |

#1～#3 的失敗集合都**等於**起點基準。

**#4 多出的失敗**：`tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_attach_restarts_cleared_session`，訊息為「逾時 75s；最後觀察值：None」。

- 依規則同範圍（`test_bb_daemon_e2e.py` 整檔）序列重跑兩次，兩次都是 `39 passed`（8m43s）；
- 開跑前確認沒有洩漏的 `serialwrapd`；
- #4 與 #3 的 `sw_core/` 完全相同（`git diff 6c991f7 9990b18` 只有 `tests/test_rust_win_scripts.py`），#3 的全套中這一案通過。

判讀：無法重現。沒有拿來當放行理由，列為待觀察：若 sol 或後續全套再紅，應以真實回歸處理。

#1～#3 每推一組都以 fast-forward 推到 `feature/s1x-a-fixes`；#4 在取證後推送。

Rust：以 root 的 cargo 跑 `cargo test -p sw-proto`（`error_codes.rs` 重生後），全數通過。

每個 commit 都另跑過受影響的測試（新增回歸檔、`test_s1x_*.py`、`test_contracts_*.py`、`test_stage0_tx_permit.py`、`test_stage0_session_drain.py`、`test_stage0_uart_exclusive.py`、`test_device_handoff.py`、`test_session_bind.py`、`test_com_rank.py`、`test_file_transfer.py`、`test_refine_transfer_integration.py`、`test_boot_quiet.py` 等），全數通過。每個 commit 都帶 `changelog.d/` fragment，trailer 含 `Co-authored-by: Copilot …`、`Review-pending: Codex sol 增量單審`、`Co-Authored-By: Claude <noreply@anthropic.com>`。

**`contracts/daemon_logging.yaml` 行號同步**：三個 commit 都只更新 `messages[].source` 的行號，沒有增刪項目、沒有改模板或等級，沿用 `d4ec3e4`、`91ed98a` 的前例。`tests/test_contracts_keep_baseline.py::test_last_error_logging_templates_match` 寫死的兩個行號（`1909→1915`、`2009→2015`）在 #2 依 `d4ec3e4` 的前例同步，斷言內容不變。

## 各條處理

### OD-62（S1X-a-4 第 2 輪；同 sol S1-6485e90-2 Medium）— 照裁決實作（`5b05626`，`6c991f7` 補 sol 重現）

- **紅燈**：新增 `tests/test_s1x_od62_file_tx_abort.py`。在 `6485e90` 上，drain／世代過期／取消的零寫出與已寫出案例都回 `TRANSFER_TIMEOUT`（10 案紅）。a-4 的兩個 drain 案例依裁決改驗 `COMMAND_ABORTED_IN_FLIGHT` 後也是紅。sol 所述「首次寫入只確認 1 byte 後 drain」的重現在 `6c991f7` 補進同一檔，在 `6485e90` 上回 `TRANSFER_TIMEOUT`，為紅。
- **修法**：
  - `UARTBridge.operation_permit` 累計本操作（file transfer 的所有子命令共用同一 permit）已確認寫出的 bytes。被拒時，把累計值附在 `TxIncompleteError.operation_confirmed` 上。所以「已寫出」是以整段傳輸計，不只看被拒的那一筆。
  - `SessionManager._file_tx_rejected_result` 的映射：
    - `quiet` 且被拒那筆零寫出 → 維持 `AUTOBOOT_QUIET`＋`retry_after_s`（SC-03）；
    - `draining`／`stale_generation`／`cancelled`，以及 `quiet` 但該筆已部分寫出 → 整段已寫出 ≥1 byte 回 `COMMAND_ABORTED_IN_FLIGHT`，否則回 `FLUSHED_BY_RECOVERY`；
    - 其他原因（結果不明、flashing 等）→ 維持 `TRANSFER_TIMEOUT`。
  - push 附 `chunks_sent`／`chunks_total`。
- **契約**（走既有程序，沒有手改產物）：
  - `contracts/rpc_schema.json`：`file.push`／`file.pull` 的 `errors`／`error_order` 在 `TRANSFER_TIMEOUT` 前加入兩碼。
  - `contracts/error_codes.yaml`：兩碼的 scope 加 `rpc` 介面與 `file.pull`／`file.push`，`meaning`／`side_effect`／`agent_action`／`contract_tests` 同步；`COMMAND_ABORTED_IN_FLIGHT` 的 `extra_fields` 加 `chunks_sent`／`chunks_total`；`FLUSHED_BY_RECOVERY` 的 `behavior_change` 關聯 `SC-04`。
  - `contracts/behavior_changes.yaml`：BC-03 與 SC-04 的 `compatibility_impact` 寫明 0.3.2 file.* 用戶端在此情境原本收到 `TRANSFER_TIMEOUT`，需要新增碼處理（ABORTED 不得自動重送）；`related_decisions` 加 OD-62。
  - SKILL 受管區段：以 `python3 -m tools.contracts skill-section --namespace error_code` 重生（`SKILL.md`、`SKILL_WINDOWS.md`）。
  - `crates/sw-proto/src/error_codes.rs`：以 `scripts/rust/gen_error_codes.py` 重生（兩碼 interfaces 改為 `Rpc, Rec`），`--check` 一致。
- **文件**：README（中英）、`docs/design-file-transfer.md`。
- **既有測試調整（依裁決）**：
  - `test_s1x_a4_transfer_tx_reject.py` 的兩個 drain 案例改驗 `COMMAND_ABORTED_IN_FLIGHT`，並改名為 `*_returns_aborted_in_flight`。該檔不在任何凍結鎖內，另外加了「drain 後不得再送 chunk」的斷言。
  - `tests/test_contracts_catalog.py::test_ec_bc03_status_tracks_change`（L1 鎖內 nodeid，nodeid 不變）的介面斷言從 `("rec",)` 改為 `("rpc", "rec")`。OD-62 明文要求加 rpc 介面，仍是精確相等，沒有放寬。
- **未做**：file.* 的 ABORTED 不寫 BC-03 命令層的 WAL META `command_aborted_in_flight`（見 SPEC-GAPS 1）。

### OD-63（S1X-a-8）— 照裁決實作（`43700f2`）

- **紅燈**：新增 `tests/test_s1x_od63_released_placeholder.py`（9 案）。在 `5b05626` 上：佔位不會建立，`device attach COM3` 回 `SESSION_NOT_FOUND`，阻擋永久存在。全部 9 案都紅。改寫後的 `test_released_unavailable_uboot_template_remains_blocked` 在修正前也是紅。
- **修法**（`sw_core/session_manager.py`）：
  - **載入**：所屬 template 已不存在的 RELEASED 紀錄，實體化為 RELEASED 佔位 session，標記 `SessionRuntime.released_placeholder`。佔位為 passthrough、`command_capable:false`，沿用原 sid／COM／alias，by_id 維持阻擋，經 `_save_state` 跨重啟保留。原本的「template 不存在…保留持久化阻擋」WARNING 照舊輸出（模板沒改，logging 契約不增刪）。`passthrough`／`others-template` 兩個通用名稱的既有還原路徑不變。
  - **`attach_device`**：holder 檢查、同步開埠等既有流程不變（SC-05）。開埠成功後，若 session 是佔位：
    - 以 `_retire_released_placeholder_locked` 移除佔位自身（含 alias 列、binding override、`_loaded_released`）並解除 by_id；
    - 以 `_pending_com` 保留原 COM；
    - 已取得的 UART 經 `_spawn_attach(by_id, dynamic_only=True)` 交給 `_attach_by_id` 的動態偵測（pin > sticky > detect > fallback）建立正式 session。`dynamic_only` 會略過 DETACHED 空槽改綁，所以不會以佔位的 passthrough profile attach，也不會借用其他 profile。
    - 完全沒有 template 時，比照現行行為不建立 session，並清掉預留的 COM。
  - 開埠時 `OSError`：比照既有語意回 ok＋DETACHED，佔位同樣移除。
  - 裝置不在線回 `DEVICE_NOT_PRESENT`、外部持有者存在回 `DEVICE_STILL_HELD`，這兩種情況都保留佔位與阻擋。
  - 熱插不會自行解除（RELEASED 語意）。
  - `device.attach` 回應仍為 `{ok, session}`。`session` 是佔位在交接當下的公開 dict（state `ATTACHING`、原 COM），正式 session 由偵測完成後建立。沒有新增 RPC／CLI。
- **契約／文件**：
  - DF-04 的 `compatibility_impact`／`approval_conditions` 補述佔位用途與「僅此用途放寬」，`related_decisions` 加 OD-63。
  - 同步 `openspec/specs/device-handoff/spec.md`（新增情境）與 README（中英）。
- **既有測試調整（依裁決）**：`tests/test_stage0_uart_exclusive.py::TestManagerAttach::test_released_unavailable_uboot_template_remains_blocked`（L1 鎖內，nodeid 不變）改驗下列各點：
  - 佔位存在、RELEASED、passthrough、`command_capable:false`，阻擋保留；
  - `file_push` 回 `PROFILE_NOT_COMMAND_CAPABLE`；
  - attach 後佔位移除、by_id 解除，以 `dynamic_only=True` 交接，UART 交給偵測，COM 保留。

### sol S1-6485e90-1（High）— 照修，成立（`6c991f7`）

- **紅燈**：`tests/test_s1x_r2_interactive_cancel_race.py::test_cancel_after_precheck_blocks_interactive_tx`。以 bridge 的 `_after_tx_authorize` 鉤子，讓 `revoke_ops([cmd_id])` 落在 pre-check 通過之後、fence 提交之前。修正前回 `ok:true` 並寫出 `b"top\n"`，與報告的重現一致。
- **修法**：
  - `TxPermit` 新增 `cancel_op_id`。`_tx_reject_reason` 是 pre-check 與 fence 內 `_submit_guard` 共用的判斷，現在同時檢查 `op_id` 與 `cancel_op_id` 是否已撤銷。
  - interactive 分支以 `cancel_op_id=cmd_id` 建立 lease permit，並移除 `9cb3c4c` 在鎖外做的一次性撤銷延伸。
  - 結果：取消後零 TX，經既有 `_map_aborted_result` 回 `FLUSHED_BY_RECOVERY`，送出失敗路徑關閉本命令開的 lease。
- **對照**：`test_lease_op_cancel_identity_does_not_leak_to_later_sends` 確認取消身分只綁在本命令的 permit 上，命令完成後撤銷原 cmd_id 不影響同一 lease 的 `interactive_send`。
- L1 鎖內的 `test_stage0_tx_permit.py::test_cancel_between_dequeue_and_manager_registration_zero_tx[interactive]` 改由 fence 檢查擋下，照舊綠，測試沒有改。

### sol 第二份增量報告（`6485e90_5b05626-sol.md`，本輪後段 fetch 到）

這份報告只審 `5b05626`。B 段 NO FINDINGS；A 段把 S1-6485e90-2 標為已修 `5b05626`，S1-6485e90-1 標為未修 1 輪。S1-6485e90-1 已由其後的 `6c991f7` 修正（見上），下一輪增量審查請核對 `5b05626..9990b18`。

### sol 報告 A 段

列出的 6 項都標為已修（涉及其他分支的 commit），沒有本分支未修項。

## 不成立主張

無。

## SPEC-GAPS

1. **file.* 的 `COMMAND_ABORTED_IN_FLIGHT` 是否寫 WAL META**：BC-03 的 `_map_aborted_result` 對命令會寫 META `command_aborted_in_flight`（`confirmed_tx_bytes`、`tx_outcome`，以 cmd_id 為鍵）。file.* 沒有單一 cmd_id（子命令為 `ft-<hex>-N`、操作 permit 為 `file:<uuid>`），而 OD-62 只裁決回應碼，所以本輪不寫 META。若要對齊，須決定 META 的 cmd_id 鍵與 `wal_meta_events.schema.json` 是否需要擴充。
2. **佔位 session 上的 `session bind`／`profile pin`**：OD-63 只規定 `device attach` 為解除路徑。現行 `bind_session` 對 RELEASED session 會解除阻擋，並以 session 自身的 profile 重新 attach。若對佔位執行 bind，會以 passthrough 佔位 profile attach 到新裝置，等於降級。本輪沒有加 guard，因為要回哪個錯誤碼或改走交接，需要另行裁決。`session clear` 對 RELEASED 回 `released:true` 且不改變狀態，不受影響。

## OWNER-BATCH

無新項目。SPEC-GAPS 1、2 若編排者認為涉及 SC／DF 語意，再升級。

## 回歸 case 政策（#155）評估

- OD-62、OD-63、S1-6485e90-1 都能以 pytest（真 `UARTBridge`＋`FakePort`＋shell 模擬器、真 `SessionManager` 狀態載入／attach 流程、TX fence 鉤子）完整覆蓋，不需要真板時序。
- 不新增 `regression/` case。

## 對 Rust 承接的提醒

P5～P8 merge 主線後依修正後的 Python 基線對齊：

- **OD-62**：file.* 傳輸中 drain／世代過期／取消，整段零寫出回 `FLUSHED_BY_RECOVERY`，已寫出 ≥1 byte 回 `COMMAND_ABORTED_IN_FLIGHT`；quiet 且該筆零寫出維持 `AUTOBOOT_QUIET`。「已寫出」以整段操作 permit 累計。`sw-proto` 的 `ErrorMeta` 兩碼介面已重生為 `Rpc, Rec`。
- **OD-63**：載入時孤兒 RELEASED 紀錄建立佔位；attach 時佔位移除、解除阻擋、保留 COM，只走動態偵測，不改綁空槽。
- **S1-6485e90-1**：interactive lease permit 帶原命令的取消身分，在 fence 內的提交點檢查，不能只在送出前檢查一次。

## 未完成項

- 一般使用者環境沒有 cargo；只以 root 跑了 `cargo test -p sw-proto`，沒有跑整個 workspace、Windows 或 bundle 的實測。
- `test_session_attach_restarts_cleared_session` 單次逾時（無法重現，見上）。
- SPEC-GAPS 1、2 待編排者裁決。
- 本環境沒有 Windows 機器；新增回歸只在 Linux 實跑（皆標 `core_portable`／`memory_only`，不依賴 `os.getuid` 或 `/dev/null`）。
