# Stage 1 出口審查 A 修正第 4 輪雲端回報（2026-10-29）

- 分支：`origin/feature/s1x-a-fixes`（起點 `109f659`，終點 `f457eb3`；本輪期間為唯一寫入者；未開 PR、未 merge 到 main、未改 `VERSION`）。三組 commit 皆以 fast-forward 推到 `feature/s1x-a-fixes`，沒有被拒，未使用 `claude/s1x-a-fixes-r4`。
- 開工時 `origin/feature/rust-refactor` 沒有本分支缺少的 commit，本輪不需 merge；收尾再 fetch 仍為 0 個。
- 依據：
  - `rulings/s1.md` 最新四列：S1X-a 第 3 輪 SPEC-GAP 1（補 schema）、SPEC-GAP 2（維持現行，文件）、第 2 輪 SPEC-GAP 1（file.* WAL META 比照 BC-03）、PTY 四案（不放行、本機為準）。
  - 上一輪回報 `reviews/s1x-a-fixes-report-r3-20261029.md`。
  - 增量審查 `reviews/s1-cloud/9990b18_109f659-sol.md`（`bdd00fb`，開工後才上架；結論：需修正；B 段 1 Medium、1 Low）。
  - 收斂規則 `reviews/review-convergence-rules.md`：只修 High／Medium，Low 不修。
- 收尾前再 fetch `docs/parity-amendments`（最新 `21cdcac`）：`reviews/s1-cloud/` 沒有針對 `109f659` 之後本分支的審查；`rulings/s1.md` 沒有新列。

## 環境

- 一般使用者 `ubuntu`（uid 1000，dialout 群組）跑所有測試；`/usr/bin/python3.12`（3.12.3）venv，含 PyYAML、pytest 9.2、jsonschema、pyserial；policy engine 依 CLAUDE.md 釘定 SHA `2dcf2f8` 安裝。
- Rust：釘定工具鏈 1.96.1（含 rustfmt、clippy）、`x86_64-pc-windows-gnu` target、mingw-w64（gcc 13，apt 安裝）。
- 測試用 clone 兩份：`~/sw`（全套，起點與終點各跑一次）、`~/swdev`（每組 commit 的門檻與目標測試）；工作目錄另在 root 的 clone，以檔案同步，不在測試 clone 內改碼。
- 測試皆前景、序列執行，同一時間只跑一套；每次全套前確認沒有洩漏的 `serialwrapd`（`pgrep` 皆為 0），load < 1（4 cores）。
- **全套的跑法**：與上一輪相同。工具單次前景呼叫上限 10 分鐘，全套約 37～39 分鐘，因此以驅動腳本**逐檔、序列**執行 `python3 -m pytest -q -p no:cacheprovider <檔案>`（每檔一個 pytest 行程、輸出 junit），分數次前景呼叫續跑；`tests/contract_blackbox/test_bb_daemon_e2e.py` 依 nodeid 均分三段。檔與檔之間多了行程隔離；若有跨檔狀態干擾，這種跑法不會呈現。

## 起點基準（`109f659`）

`3 failed, 5409 passed, 160 skipped, 1 xfailed, 0 error`（逐檔累計 2339 s）：

```
FAILED tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only
FAILED tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
FAILED tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
```

- 三案與上一輪、審查報告相同：Windows 原生 nodeid 不在 Linux collect 內、缺 Windows 原生 cargo L1 報告、缺 Windows P4 證據。
- 上一輪在雲端 VM 非決定性失敗的 PTY bridge 四案（`test_functest_bridge_smoke.py` sm01／sm04、`test_l15_smoke.py` reject_zero_tx／release_inflight），本輪起點與終點全套皆通過。依裁決「不放行、本機為準」，本輪未拿來判斷任何事。

## Commit 清單與逐 commit 驗證

| # | commit | 主旨 |
|---|---|---|
| 1 | `f32f6cf` | docs(contracts)：補記 OD-63 佔位 session bind 的回應 schema 與無 fallback 開埠失敗語意（裁決 (a)(b)） |
| 2 | `56af162` | fix(file-transfer)：file.* 回 COMMAND_ABORTED_IN_FLIGHT 時比照 BC-03 寫 WAL META（裁決 (c)） |
| 3 | `f457eb3` | docs(contracts)：session.bind 佔位早退另立 side_effects 一筆並依 P0 規則遞增 x-catalog-revision（S1-109f659-1 確切修法 2、3） |

- 每組都在推送前，於 `~/swdev` 的該 commit 上跑完門檻，全數通過：
  - `cargo check --workspace --target x86_64-pc-windows-gnu`：通過。
  - `cargo test`：三組都沒有 Rust 變更，沒有受影響的 crate。#1 因改 `contracts/rpc_schema.json`，另跑讀取該檔的 `cargo test -p sw-proto`：127 passed、0 failed。
  - `python3 scripts/rust/check_cfg_boundary.py`：rc 0。
  - `python3 -m tools.contracts check`：`ok:true`、0 findings（含 C14 SKILL 受管區段無漂移、C15 schema 碼一致、C18 欄位完整）。
  - `python3 tests/acceptance/regen_lock.py --check`：兩項 OK；凍結鎖未動。
  - `python3 -m policy_check --repo .`：rc 0，25 pass、0 fail、2 warn。R-19 是非 PR 上下文；R-22 是既有懸空引用 2902 項（advisory），與上一輪相同，本輪文件沒有新增。
- 受影響的 Python 測試：
  - #1：`test_s1x_r4_bind_placeholder_contract.py`、`test_s1x_r3_od63_placeholder_paths.py`、`test_s1x_od63_released_placeholder.py`、`test_contracts_*.py`（11 檔）、`test_session_bind.py`、`test_device_handoff.py`、`test_p1oc_sc34_last_error.py`、`test_stage0_uart_exclusive.py`、`test_s1x_od62_file_tx_abort.py`：646 passed、1 skipped。
  - #2：兩段。
    - 第一段：本輪兩個新檔、`test_s1x_od62_*`、`test_s1x_a4_*`、`test_s1x_od63_*`、`test_s1x_r3_*`、`test_file_transfer*.py`、`test_transfer_portability.py`、`test_refine_transfer_integration.py`、`test_refine_ownership.py`、`test_stage0_{session_drain,tx_outcome,tx_permit,gate_codes,captures,uart_exclusive,handoff_stop,wal_unlink}.py`、`test_contracts_*.py`：942 passed、3 skipped。
    - 第二段：`test_boot_quiet.py`、`test_bootloader_recovery.py`、`test_cli_timeout_robustness.py`、`test_issue52_rpc_concurrency.py`、`test_p1oc_sc35_commands.py`、`test_ready_reconfirm_bounds.py`、`test_rpc_blocking_offload.py`、`test_resource_report.py`、`test_packaging_entrypoints.py`、`test_session_bind.py`、`test_device_handoff.py`、`test_wal.py`、`test_s1x_a3_*`、`test_s1x_a7_*`、`test_multiagent_e2e.py`、`test_concurrency_races.py`：366 passed、3 skipped。
  - #3：本輪兩個新檔、`test_contracts_*.py`、`test_stage0_wal_unlink.py`：529 passed、2 skipped。
- 每組都帶 `changelog.d/` fragment：`s1x-r4-od63-bind-schema.md`（#1 新增，#3 補述）、`s1x-r4-file-abort-wal-meta.md`（#2）。trailer 依本輪指示為 `Co-authored-by: Copilot …`、`Review-pending: 非實作者增量單審`、`Co-Authored-By: Claude <noreply@anthropic.com>`。
- `contracts/daemon_logging.yaml`：只有 #2 同步 3 項 `messages[].source` 行號（difflib 映射，8355→8384、8360→8389、8382→8411）。沒有增刪項目、沒有改模板或等級；`test_contracts_keep_baseline.py` 寫死的行號在變動區段之前。

## 最終全套（`f457eb3`）

`3 failed, 5424 passed, 160 skipped, 1 xfailed, 0 error`（逐檔累計 2228 s）：

```
FAILED tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only
FAILED tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
FAILED tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
```

- 失敗集合與起點基準**完全相同**，沒有多出的失敗，因此不需要同範圍重跑取證。
- passed 多 15 案，即本輪新增的 15 案：`test_s1x_r4_bind_placeholder_contract.py` 7 案、`test_s1x_r4_file_abort_wal_meta.py` 8 案。

## 各裁決處理

### (a) S1X-a 第 3 輪 SPEC-GAP 1：`session.bind` 補 schema（`f32f6cf`、`f457eb3`）

- **紅燈**：新增 `tests/test_s1x_r4_bind_placeholder_contract.py`，改契約前以下 6 案紅：
  - `test_bind_placeholder_runtime_response_matches_schema`：真 `SessionManager` 對佔位 bind，回應鍵 `["ok","released","recommended_action","session"]` 不被 result 接受。判準比照 `test_contracts_rpc_schema.py` 的 `_admits`：回應鍵為 properties 子集、`required` 齊備、鍵序為 `x-order` 子序列。
  - `test_bind_ok_literal_orders_admitted_by_result_schema`：以 AST 抽 `bind_session` 全部 `"ok": True` 字面回應（已綁定、佔位早退、一般成功三種），逐一以 `_admits` 判定。
  - `test_bind_result_mirrors_attach_released_branch`：兩個屬性的宣告與鍵序須與 `session.attach` 成功分支相同。
  - `test_bind_side_effects_note_placeholder_stays_released`
  - `test_df04_compatibility_impact_covers_bind_and_no_fallback_open_failure`
  - `test_openspec_od63_scenario_notes_no_fallback_open_failure`
  - schema 沒有 `additionalProperties:false`，單靠 jsonschema 驗證照過，所以另以 `_admits` 判定。
- **修法**（`contracts/rpc_schema.json`，add-only）：
  - `session.bind.result` 比照 `session.attach` 的 RELEASED 分支，properties 加 `released`（`boolean`、`const:true`）與 `recommended_action`（`string`）。
  - `x-order` 改為 `ok, already_bound, released, recommended_action, session`；另加 `x-note` 說明 `released`／`recommended_action` 與 `already_bound` 互斥。
  - `side_effects`（`f457eb3` 定形）：
    - 原一般路徑那筆加 `x-condition`：「非 OD-63 佔位 session 時才執行」。
    - 另補一筆佔位早退項：on_success、`idempotent:true`，「佔位不離開 RELEASED，無其他副作用」，`x-condition` 只限佔位。
  - `x-catalog-revision` 14→15（plan 02「修改 schema 檔時依 P0 規則遞增」）。
  - DF-04 `compatibility_impact` 補述：佔位 `session.bind` 比照 `session.attach` 的 RELEASED 早退，回 `ok:true`、`released:true`、`recommended_action:device_attach`，佔位不離開 RELEASED。
- **契約程序**：
  - `tools.contracts check` 通過，C14 確認 SKILL 受管區段無漂移。
  - `error_codes.yaml`、`session_last_errors.yaml` 未變，所以 SKILL 受管區段（`skill-section` 只取這兩個目錄）與 `crates/sw-proto/src/error_codes.rs` 都不需重生。
  - 雜湊：repo 內沒有任何程式或測試釘定 `rpc_schema.json`、`behavior_changes.yaml`、`wal_meta_events.schema.json` 的內容雜湊。以三檔在 `109f659` 的 sha256 與 git blob id 全文搜尋，皆無命中。`contract_revision` 是 build／capture 時計算的 tree SHA，依 plan 01 不寫入 `contracts/`。因此沒有需要更新的雜湊，也沒有手改。

### (b) S1X-a 第 3 輪 SPEC-GAP 2：維持現行，文件補記（`f32f6cf`）

- 補記位置：
  - DF-04 `compatibility_impact`
  - `openspec/specs/device-handoff/spec.md` 的 OD-63 情境
  - README（中英）
- 補記內容：
  - 佔位開埠遇一般 OSError 時，交 dynamic 偵測（這部分上一輪只寫在 README／openspec，本輪一併補進 DF-04）。
  - 無 passthrough fallback template 時不留列，`device attach COMx` 回 `SESSION_NOT_FOUND`。
  - `_pending_com` 保留原 COM 至裝置拔除或 daemon 重啟為止，期間不分配給其他裝置。
  - 之後由熱插或重啟重新偵測建立 session；COM 依 dynamic 規則重新分配，**不保證沿用原 COM**。見「不成立主張」1。
- 伴隨確認：`test_placeholder_open_failure_without_fallback_reserves_com_until_unplug`（修正前後皆綠，鎖住現行語意）。斷言：
  - 不留列，`device attach` 回 `SESSION_NOT_FOUND`。
  - 同一裝置集合重掃不重試開埠。
  - 拔除後 `_pending_com` 不再保留該 by_id。

### (c) S1X-a 第 2 輪 SPEC-GAP 1：file.* 的 WAL META 比照 BC-03（`56af162`）

- **紅燈**：新增 `tests/test_s1x_r4_file_abort_wal_meta.py`，在 `f32f6cf` 的 `sw_core`／`contracts` 上 7 案紅、1 案綠。
  - 紅的 7 案：
    - push 世代過期（`ack_mode` none／echo 兩案）
    - push 取消
    - pull 首筆只確認 1 byte 後 drain
    - META 早於 foreground 操作釋放
    - 與一般命令 META 欄位相同
    - 契約註記
  - 綠的 1 案是伴隨確認：零寫出時不寫 META。
  - 修正前 WAL 裡沒有任何 `command_aborted_in_flight`。
- **一般命令的基準**（`_map_aborted_result`）：已確認寫出 ≥1 byte 而回 `COMMAND_ABORTED_IN_FLIGHT` 時，在映射當下寫一筆 META。
  - 時機：早於 `resume_interactive`、TX 帳本收尾與 foreground 操作釋放，也就是回應送出前。
  - 外層：`dir=META`、`source=system`、`cmd_id=<該命令>`、payload 為空。
  - meta：`{"event":"command_aborted_in_flight","confirmed_tx_bytes":N,"tx_outcome":complete|partial|unknown}`。
  - 零寫出（`FLUSHED_BY_RECOVERY`）不寫。
- **修法**：
  - `SessionManager` 把上述 META 寫入抽成 `_append_aborted_in_flight_meta`（`tx_outcome` 判定逐字不變），一般命令改呼叫它。
  - `_file_tx_rejected_result` 改為實例方法，回 `COMMAND_ABORTED_IN_FLIGHT` 時呼叫同一 helper，因此欄位與時機相同。
    - `confirmed_tx_bytes`：整段傳輸累計，即 OD-62 判定「已寫出」的同一個數字。
    - `tx_outcome`：依被拒那筆的 `TxResult` 套同一規則；實務上為 `partial`。
  - `cmd_id`：file.* 的「命令」是整段傳輸，WAL 上以傳輸的 cmd_id 前綴 `ft-<8hex>` 識別，各子命令 TX 紀錄為 `ft-<8hex>-<子命令>`。
    - `confirmed_tx_bytes` 因此等於這些 TX 紀錄的 payload 長度總和，回歸裡有斷言。
    - 實作上，`push_file`／`pull_file` 把產生前綴之後的本體抽成 `_push_file_tx`／`_pull_file_tx`；本體逐行不變，uuid 取用順序（先 tmp 後前綴）也不變。外層在 `TxIncompleteError` 上附 `transfer_cmd_id`。
    - 缺這個屬性時，退用傳輸 permit 的 op_id（防禦；正式路徑一律有）。
    - 這個取值的選擇另列 SPEC-GAPS 1。
- **契約與文件**：
  - `wal_meta_events.schema.json` 的 `command_aborted_in_flight` 分支：`description`、`x-outer-record.cmd_id`、`confirmed_tx_bytes.description` 寫明 file.* 的情形，結構不變；`x-catalog-revision` 4→5（在 `f457eb3`）。
  - 同步補述：BC-03 `compatibility_impact`、README（中英）、`docs/design-file-transfer.md`。

## 增量審查 `9990b18_109f659-sol.md` 的處理

### A 段

前輪 10 項皆標「已修」，沒有未修的 High／Medium。

### B 段

**S1-109f659-1（Medium）— 照修，成立（`f32f6cf`、`f457eb3`）。** 與裁決 (a) 是同一件事。對照審查的「確切修法」：

1. result 加 `released`／`recommended_action` 與 `x-order`：照做（取審查列的第一種寫法，不改 oneOf）。
2. side_effects 補一筆帶 `x-condition`（「OD-63 佔位：早退，無副作用」）：`f457eb3` 照做；原一般路徑那筆另加 `x-condition` 排除佔位。
3. 依檔內慣例處理 `x-catalog-revision`：`f457eb3` 依 plan 02 的 P0 規則遞增 14→15。檔內歷史並不一致，見 SPEC-GAPS 3。
4. DF-04 `compatibility_impact` 補述：照做。
5. 補契約回歸：`test_bind_ok_literal_orders_admitted_by_result_schema` 以 `_admits` 判準驗 `bind_session` 全部 `ok:true` 字面回應，改 schema 前紅。與審查寫法的差別見「不成立主張」2。

**S1-109f659-2（Low）— 依收斂規則不修。** 佔位的舊板永久不回來時，沒有 RPC／CLI 出口能移除佔位。審查建議由擁有者或編排者在 (a)「bind 目標在線時佔位 retire 並交 dynamic」與 (b)「追認已知限制並寫明處置」之間擇一，列入 OWNER-BATCH。

### 既有碼待辦（不計入結論，本輪未處理）

- `revoke_tx_op`：取消落在 interactive TX 完成後時，會一併撤銷 `interactive:<lease_id>`，lease 之後持續回 `SESSION_BUSY`。上一輪已列，依收斂規則 §6 待編排者開 issue 或記 `low-backlog.md`。
- `_record_dynamic_open_failure` 在無 fallback template 時不留列：即裁決 SPEC-GAP 2「維持現行」的情境，本輪只補文件。

## 不成立主張

1. **裁決 SPEC-GAP 2 括號內「（`_pending_com` 保留原 COM，熱插或重啟重建）」的「在原 COM 重建」部分不成立。**
   - Python 實測（`109f659` 的真 `SessionManager`，只有 shell template，以 `PermissionError` 替身開埠失敗）：
     - 失敗後：沒有 session 列，`_pending_com={by_id: "COM3"}`，`device attach COM3` 回 `SESSION_NOT_FOUND`。
     - 同一裝置集合重掃（`update_devices` 不變）：不重試開埠。
     - 熱插：拔除時 `update_devices` 會剪掉離線裝置的 `_pending_com`（`session_manager.py` `update_devices` 的 #100 M2/3a 修剪）。權限修好後重插，新 session 落在 **COM0**（`_next_dynamic_com` 取最低空號）。
     - 重啟：`_pending_com` 不持久化，新 daemon 的 `prepare_dynamic_rank` 預配為 **COM0**。
   - 也就是說，原 COM 只在裝置未拔、daemon 未重啟期間保留（期間 `_next_dynamic_com` 不會分給其他裝置）。熱插或重啟後依 dynamic 規則重新分配，不保證沿用原 COM。
   - 這與 dynamic 對新裝置開埠失敗的現行語意相同，與裁決「維持現行、不新增錯誤碼或回應形狀」一致。
   - 文件依實測行為撰寫，沒有照括號原文寫「在原 COM 重建」。若編排者要的是跨熱插／重啟保留原 COM，屬語意變更，見 SPEC-GAPS 2。
2. **S1-109f659-1 確切修法 5「驗每個字面回應都被 `session.bind` 的 schema 接受」只對 `ok:true` 字面回應成立。**
   - `bind_session` 的錯誤字面回應（`INVALID_ARGS`、`SESSION_NOT_FOUND`＋`selector`、`FLASHING_BUSY`、`DEVICE_ALREADY_BOUND`、`SESSION_RECOVERING`＋drain 欄位）本來就不屬 `result`。這些分屬 `x-error-results`（`SESSION_RECOVERING`）與通用錯誤形狀，以 `_admits(result, …)` 判定必為 False。
   - 因此回歸只取 `"ok": True` 的三種字面回應（已綁定、佔位早退、一般成功），全部被接受；改 schema 前，佔位早退那筆不被接受（紅）。

## SPEC-GAPS

1. **file.* 的 `command_aborted_in_flight` META 的 `cmd_id` 取值。** 裁決只定「欄位與時機相同」。
   - 本輪採傳輸的 cmd_id 前綴 `ft-<8hex>`，`confirmed_tx_bytes` 取整段累計，兩者描述同一個單位（整段傳輸），且 bytes 等於該前綴下各子命令 TX 紀錄的總和。
   - 另一個候選是被拒那筆子命令的 cmd_id（如 `ft-<8hex>-3`）。好處是精確等於某筆 WAL 紀錄的 cmd_id；但該子命令本身可能零寫出，META 的 bytes（整段 ≥1）就會與該 cmd_id 下的 TX 總和不符。
   - 已寫進 `wal_meta_events.schema.json` 的 `x-outer-record.cmd_id`。請編排者確認；若改用子命令 cmd_id，只需改 `push_file`／`pull_file` 附屬性處與文件。
2. **無 fallback 時跨熱插／重啟保留原 COM**（承「不成立主張」1）。目前不保留。若要保留，需新增語意（例如拔除時不剪佔位交接留下的 `_pending_com`，或持久化），且要裁決與 #100 rank 規則的優先序。本輪不做。
3. **`x-catalog-revision` 的遞增慣例不一致。**
   - plan 02 寫「修改 schema 檔時依 P0 規則遞增」，本輪照做（rpc_schema 14→15、wal_meta_events 4→5）。
   - 但本分支先前的 `5b05626`（OD-62 改 file.* errors）、`ef99949`、`99e5db3` 改了 `rpc_schema.json` 而沒有遞增。
   - repo 內沒有程式或測試讀這兩個值。是否追補、或明訂「只有結構變更才遞增」，請編排者定。

## OWNER-BATCH

1. **S1-109f659-2（Low，不擋放行）**：佔位的舊板永久離線時沒有移除佔位的出口。請擇一：
   - (a) `session bind` 的目標 by_id 在線時，佔位 retire 自身，並以 `_pending_com[target]=原 COM` 交 `_spawn_attach(target, dynamic_only=True)`，不新增 RPC／CLI 表面。
   - (b) 追認為已知限制，在 README（中英）與 openspec 寫明處置方式：把舊 template 名稱加回 profiles 後 `device attach`，或停 daemon 手改 state.json。
   - 依收斂規則，未裁決前記 `low-backlog.md`。

## 回歸 case 政策（#155）評估

三組都能以 pytest 完整覆蓋，不新增 `regression/` case：

- (a)：純契約／文件。執行期回應由真 `SessionManager` 驗。
- (b)：真 `SessionManager`＋計數開埠替身，`PermissionError` 由替身拋出。
- (c)：真 `UARTBridge`＋`FakePort`＋shell 模擬器，以世代前進、permit 撤銷、首筆部分寫出重現中止，WAL 以真 `WalWriter` 寫檔後讀回，並以 `contracts/wal_record.schema.json` 驗證。

不涉及真板 boot 時序、USB 列舉、真實登入或長時負載。新增回歸皆標 `core_portable`／`memory_only`。

## 對 Rust 承接的提醒

P5～P8 merge 主線後，依修正後 Python 基線對齊：

- **`session.bind` 對佔位**：回應鍵序 `ok, released, recommended_action, session`（依 `x-order`）。佔位不離開 RELEASED：不 detach、不解除阻擋、不寫 binding override、不取消在途 attach、不開埠。side_effects 兩筆依 `x-condition` 分流。
- **file.push／file.pull 回 `COMMAND_ABORTED_IN_FLIGHT`**：寫 `command_aborted_in_flight` META，規格如下。零寫出（`FLUSHED_BY_RECOVERY`）不寫。
  - 外層：`dir=META`、`source=system`、`cmd_id=ft-<8hex>`（傳輸前綴；Rust plan 09 的 `prefix=ft-{ids.hex(8)}`）、payload 空。
  - meta：`confirmed_tx_bytes` 為整段傳輸累計（等於該前綴下各子命令 TX 紀錄總和），`tx_outcome` 規則同一般命令。
  - 時機：映射當下，早於回應送出與 foreground 操作釋放。
  - Rust 的 slice 2.1 `Effect::WalMeta` 可共用一般命令的建構點。若 SPEC-GAPS 1 改裁為子命令 cmd_id，Rust 跟著改。
- **佔位開埠失敗且無 fallback template**：
  - 不留列，`device attach` 回 `SESSION_NOT_FOUND`。
  - 原 COM 只在裝置未拔、daemon 未重啟期間保留（不分給其他裝置）。
  - 熱插或重啟後依 dynamic 規則重新分配（熱插取最低空號或繼承 DETACHED 空槽，重啟依 by-id 排序預配）。
- `rpc_schema.json` `x-catalog-revision` 現為 15，`wal_meta_events.schema.json` 為 5。

## 未完成項

- 本輪沒有 Rust 變更，只跑了 `cargo check --workspace --target x86_64-pc-windows-gnu` 與 `cargo test -p sw-proto`；沒有跑 Windows 原生、bundle 實測或全 workspace `cargo test`。
- SPEC-GAPS 1～3、OWNER-BATCH 1 待編排者／擁有者裁決；`revoke_tx_op` 待開 issue 或記 low-backlog。
- PTY bridge 四案本輪在雲端 VM 起點與終點皆通過；依裁決以本機 Stage 1 出口實跑為準，本輪未另取證。
- 本環境沒有 Windows 機器；新增回歸只在 Linux 實跑，不依賴 `os.getuid` 或 `/dev/null`。
