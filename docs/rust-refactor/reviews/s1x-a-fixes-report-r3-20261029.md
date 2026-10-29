# Stage 1 出口審查 A 修正第 3 輪雲端回報（2026-10-29）

- 分支：`origin/feature/s1x-a-fixes`（起點 `9990b18`，終點 `109f659`；本輪期間為唯一寫入者；未開 PR、未 merge 到 main、未改 `VERSION`）。三組 commit 皆以 fast-forward 推到 `feature/s1x-a-fixes`，沒有被拒，未使用 `claude/s1x-a-fixes-r3`。
- 依據：
  - 最新增量審查 `reviews/s1-cloud/5b05626_9990b18-sol.md`（結論：需修正；B 段兩個 Medium）。
  - 擁有者裁決 OD-63（`rulings/s1.md`）、DF-04 `approval_conditions`。
  - 收斂規則 `reviews/review-convergence-rules.md`：只處理 High／Medium，Low 不修。
  - 上一輪回報 `reviews/s1x-a-fixes-report-r2-20261029.md`（其 SPEC-GAP 2 即本輪 S1-9990b18-1）。
- 收尾前再 fetch `docs/parity-amendments`（最新 `eb9ca65`）：`reviews/s1-cloud/` 沒有針對 `5b05626..9990b18` 之後本分支的新審查報告；`rulings/s1.md` 自上次審查後新增三列（Docker 預熱競態、environ 競態、S1X-b 第 4 輪機制主張），都不涉及本分支。

## 環境

- 一般使用者 `ubuntu`（uid 1000，dialout 群組）跑所有測試；`/usr/bin/python3.12`（3.12.3）venv，含 PyYAML、pytest 9.2、jsonschema、pyserial；policy engine 依 CLAUDE.md 釘定 SHA `2dcf2f8` 安裝。
- Rust：釘定工具鏈 1.96.1（含 rustfmt、clippy）、`x86_64-pc-windows-gnu` target、mingw-w64（gcc 13）。一般使用者可用 cargo，因此 Rust／bundle 類 pytest 在本環境實際執行（上一輪環境沒有 cargo 而直接紅）。
- 測試皆前景、序列執行，同一時間只跑一套；每次全套與重跑前都確認沒有洩漏的 `serialwrapd`（`pgrep` 皆為 0），load 皆 < 1。
- **全套的跑法**：工具單次前景呼叫上限 10 分鐘，全套約 41～43 分鐘，因此以驅動腳本**逐檔、序列**執行 `python3 -m pytest -q -p no:cacheprovider <檔案>`（每檔一個 pytest 行程、輸出 junit），分數次前景呼叫續跑。`tests/contract_blackbox/test_bb_daemon_e2e.py` 單檔約 9 分鐘，依 nodeid 均分三段（各 13 案）。與單一行程的 `pytest -q tests/` 相比，檔與檔之間多了行程隔離；若有跨檔狀態干擾，這種跑法不會呈現。
- 測試用 clone 在 `/home/ubuntu/sw`（完整歷史與 tag，`git fetch --unshallow` 後重新 clone），不影響工作目錄。

## 起點基準（`9990b18`）

`7 failed, 5355 passed, 160 skipped, 1 xfailed, 0 error`（逐檔累計 2561 s）：

```
FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01
FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op
FAILED tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python
FAILED tests/test_l15_smoke.py::test_release_inflight_runs_on_python
FAILED tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only
FAILED tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
FAILED tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
```

- 後三案與上一輪基準相同：Windows 原生 nodeid 不在 Linux collect 內、缺 Windows 原生 cargo L1 報告、缺 Windows P4 證據。
- 上一輪基準的 `test_mkbundle_release_manifest_matches_parser_contract`、`test_bundle_license_files_present`、`test_doctor_ab_version_pairing_in_bundle`、`test_manifest_schema_accepts_new_optional_fields` 因缺 cargo 而紅，本環境有 cargo，皆通過。
- 前四案是本 VM 的 PTY 案例，見下方「PTY 案例的 `DeviceHeldError`」。

## Commit 清單與逐 commit 驗證

| # | commit | 主旨 |
|---|---|---|
| 1 | `3fa4cd6` | merge：併入 `origin/feature/rust-refactor`（`8963d07`，25 個 commit） |
| 2 | `5e7c970` | fix(session)：session bind 不得以佔位 profile 實體化 RELEASED 佔位 session（S1-9990b18-1） |
| 3 | `109f659` | fix(device)：佔位 session 開埠遇一般 OSError 時交 dynamic 偵測，不讓裝置成為孤兒（S1-9990b18-2） |

- **#1 merge**：`git merge --no-ff`，無衝突。凍結鎖只增：
  - `l1_required.lock.json` 只增登記新節點。
  - `p4_required.lock.json` 只多一個來源 commit（原最後一項只多逗號）。
  - `regen_lock.py --check --base 9990b18` 判定「與基準比對一致」。
  - 本分支的 `sw_core/` 不受影響（rust-refactor 這 25 個 commit 未改 `sw_core/`）。
- **#2、#3 的驗證門檻**（每組皆跑，全數通過）：
  - `cargo check --workspace --target x86_64-pc-windows-gnu`：通過。本輪沒有 Rust 變更，沒有受影響的 crate，所以沒有跑 `cargo test`。
  - `python3 scripts/rust/check_cfg_boundary.py`：rc 0。
  - `python3 -m tools.contracts check`：`ok:true`，0 findings。
  - `python3 tests/acceptance/regen_lock.py --check`：兩項 OK。
  - `python3 -m policy_check --repo .`：rc 0，25 pass、0 fail，2 warn。R-19 是非 PR 上下文；R-22 是既有懸空引用 2902 項（advisory）。`3fa4cd6` 上同為 2902 項，本輪文件沒有新增。
  - 受影響的 Python 測試：
    - `test_s1x_r3_od63_placeholder_paths.py`、`test_s1x_od63_released_placeholder.py`、`test_s1x_a3_attach_inflight.py`、`test_s1x_a7_drain_fields.py`
    - `test_stage0_uart_exclusive.py`、`test_device_handoff.py`、`test_session_bind.py`、`test_com_rank.py`、`test_attach_error_surface.py`
    - `test_contracts_keep_baseline.py`、`test_contracts_gap_codes.py`、`test_contracts_rpc_schema.py`、`test_concurrency_races.py`；#3 另加 `test_stage0_handoff_stop.py`
    - 結果：#2 為 293 passed、4 skipped；#3 為 350 passed、4 skipped。
- 每個 commit 都帶 `changelog.d/` fragment：`s1x-r3-od63-placeholder-bind.md`、`s1x-r3-od63-placeholder-open-error.md`。trailer 依本輪指示為 `Co-authored-by: Copilot …`、`Review-pending: 非實作者增量單審`、`Co-Authored-By: Claude <noreply@anthropic.com>`。
- `contracts/daemon_logging.yaml`：#2、#3 各只同步 12 項 `messages[].source` 的行號，以 difflib 對未變動區段做映射。沒有增刪項目、沒有改模板或等級，沿用 `d4ec3e4`、`43700f2` 的前例。`test_contracts_keep_baseline.py` 寫死的兩個行號（1915、2015）在變動區段之前，不需要動。

## 最終全套（`109f659`）

`5 failed, 5407 passed, 160 skipped, 1 xfailed, 0 error`（逐檔累計 2410 s）：

```
FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01
FAILED tests/test_l15_smoke.py::test_release_inflight_runs_on_python
FAILED tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only
FAILED tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
FAILED tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
```

- 失敗集合是起點基準的**子集**，沒有多出的失敗。
- 案例總數多 50 案（merge 帶入 44 案、本輪新增 6 案）；passed 多 52 案，即這 50 案加上 2 個本次通過的 PTY 案例（見下）。

### PTY 案例的 `DeviceHeldError`（本 VM 環境的非決定性，非本輪引入）

四個 func-test bridge 模式案例都在 `wait_ready` 逾時。原因是 daemon 開埠時 `probe_holders` 判定 PTY slave 另有 holder，session 停在 `DETACHED`、`last_error=ATTACH_FAILED:DeviceHeldError`。依 CLAUDE.md「同範圍連跑兩次、失敗集合不同」取證：

| 執行 | commit | 失敗（四案中） |
|---|---|---|
| 基準全套 | `9990b18` | sm01、sm04、reject_zero_tx、release_inflight |
| 最終全套 | `109f659` | sm01、release_inflight |
| 同範圍重跑 1（兩檔） | `109f659` | sm01、sm04、reject_zero_tx、release_inflight |
| 同範圍重跑 2（兩檔） | `109f659` | sm01、sm04、reject_zero_tx |
| 同範圍（兩檔） | `6485e90`（上一輪起點，當時環境全綠） | sm01、reject_zero_tx |

- 每次開跑前沒有洩漏的 `serialwrapd`，1 分鐘 load < 1（4 cores），系統沒有飽和。
- 同一 commit 兩次重跑的失敗集合不同。上一輪起點 `6485e90` 在本 VM 也會紅。
- 這些案例不經過 OD-63 佔位路徑，本輪 `sw_core` diff 只觸及佔位分支。
- 判讀：本 VM 上 bridge 模式 PTY holder 判定的時序非決定性，起點就存在，不是本輪回歸。沒有拿來當放行理由；daemon.log 為空，未能取得 holder pid，根因未查明（見未完成項）。

## 各條處理

### S1-9990b18-1（Medium）— 照修，成立（`5e7c970`）

- **紅燈**：新增 `tests/test_s1x_r3_od63_placeholder_paths.py`。
  - `test_session_bind_on_placeholder_keeps_block_without_attach[same_by_id|other_by_id]`：修正前回 `{'ok': True, 'session': …}`，沒有 `released`，佔位以 passthrough profile 開埠。與審查重現一致。
  - `test_attach_by_id_never_builds_bridge_for_placeholder`：修正前開埠 1 次。
  - 以上三案在 `3fa4cd6` 上皆紅。
  - `test_session_bind_on_placeholder_leaves_device_attach_handoff_intact`：伴隨確認，確認 bind 早退後 `device attach` 交接不受影響；修正前也綠。
- **修法**（`sw_core/session_manager.py`）：
  1. `bind_session` 第一個鎖區，在取消在途 attach 之前，遇 `released_placeholder` 比照 `attach_session` 的 RELEASED 早退，回 `{"ok":true,"released":true,"recommended_action":"device_attach","session":…}`。不解除阻擋、不寫 override、不取消在途 `device attach`、不 spawn。
  2. `_attach_by_id` 防線：佔位 session 一律不建 bridge，若已 pop 到預開 handle 就以 `_finish_attach_op` 收尾。
  3. `_spawn_attach` 收尾的重試條件排除佔位，避免防線觸發時一再空轉重啟 worker。
- 審查的「確切修法」1～3 都做了；2 只多了第 3 點的一行重試排除。
- 沒有新增 RPC／CLI 方法或參數。回應鍵沿用 `session.attach` 的 RELEASED 分支；`session.bind` 的 schema 分支與 side_effects 尚未補記，見 SPEC-GAPS 1。
- **文件**：README（中英）、`openspec/specs/device-handoff/spec.md` 的 OD-63 情境補一行。

### S1-9990b18-2（Medium）— 照修，成立（`109f659`）

- **紅燈**（同一檔，在 `5e7c970` 上皆紅）：
  - `test_device_attach_oserror_hands_off_and_stays_reattachable`：有 passthrough fallback template。修正前只開埠 1 次，之後 `get_session("COM3")` 為 None，與審查重現一致。
  - `test_device_attach_oserror_without_fallback_template_still_hands_off`：只有 shell template。修正前沒有交 dynamic 偵測。
- **修法**：`attach_device` 的 `except OSError` 分支遇佔位時，比照成功分支交接：
  - `_retire_released_placeholder_locked` 後設 `_pending_com[by_id] = 原 COM`；
  - 在 `_finish_attach_op` 與 `_save_state` 之後呼叫 `_spawn_attach(by_id, dynamic_only=True)`；
  - dynamic 路徑自行重開埠；仍失敗時由 `_record_dynamic_open_failure` 依現行語意在原 COM 留下 `DETACHED`、`ATTACH_FAILED:PermissionError` 列（`profile_source=fallback`）。
- **Python 實測**：權限修好後，`device attach COM3` 回 `ok:true` 並 spawn attach（測試第二段）；fallback 列之後由 `_reresolve_profile_on_reattach` 重新偵測 template。
- 回應不變（`ok:true`＋佔位當下的 `DETACHED` session）。`DeviceHeldError`／`DeviceNotPresentError` 照舊保留佔位與阻擋，既有回歸照綠；cancelled／release 路徑不交接。
- **與審查預期的差異**：審查預期「COM3 必須仍可被 `device attach` 找到」。這只在有 passthrough fallback template 時成立。沒有 fallback template 時，現行 dynamic 開埠失敗本來就不留列（只保留 `_pending_com`，等熱插或重啟時沿用原 COM）。依 OD-63「無 template 可匹配時依現行 dynamic 偵測」照此實作，見 SPEC-GAPS 2。預設 `default.yaml` 有 `others-template`（passthrough），一般部署會留列。

### 審查 A 段

前輪 8 項（1～6、S1-6485e90-1、S1-6485e90-2）都標為已修，沒有未修的 High／Medium。

### 審查「既有碼待辦」（不計入結論）

`revoke_tx_op` 在 interactive 命令 TX 完成後才取消時，會一併撤銷 `interactive:<lease_id>`，lease 之後每次 `interactive send` 都回 `SESSION_BUSY`。依收斂規則 §6，這屬既有碼的新問題，不塞進本包，本輪沒有處理，請編排者開 issue 或記進 `low-backlog.md`。

## 不成立主張

無。審查的兩條 Medium 都以 Python 實測重現（見紅燈）。S1-9990b18-2 的預期在無 fallback template 時不成立，列為 SPEC-GAPS 2，不是對 finding 本身的否定。

## SPEC-GAPS

1. **`contracts/rpc_schema.json` 的 `session.bind`**：
   - 現況：`result` 只列 `ok`／`already_bound`／`session`，`side_effects` 寫「離開 RELEASED」，`DF-04 compatibility_impact` 也沒提到 bind。
   - 本輪的佔位早退會多回 `released`、`recommended_action` 兩鍵。schema 沒有 `additionalProperties:false`，驗證照過；`x-order` 也沒有工具消費。
   - 照收斂規則「契約缺口列 SPEC-GAPS、不自行擴權」，本輪沒有改 schema／DF-04。
   - 建議：比照 `session.attach` 的 RELEASED 分支，補 `released`、`recommended_action` 屬性與 `x-order`；side_effects 註明「佔位不離開 RELEASED」。
2. **佔位開埠失敗、又沒有 passthrough fallback template 時**：
   - 交 dynamic 偵測後不會留下 session 列，`device attach COM3` 仍回 `SESSION_NOT_FOUND`，要等熱插或重啟才會在原 COM 重建（`_pending_com` 已保留原 COM）。這與現行 dynamic 對新裝置開埠失敗的行為相同。
   - 若要在這種情況另外保留可重試的列（例如不 retire 佔位、改回錯誤碼），需要裁決錯誤碼或回應形狀。
3. **沿用上一輪 SPEC-GAP 1**（file.* 的 `COMMAND_ABORTED_IN_FLIGHT` 是否寫 WAL META）：`rulings/s1.md` 尚無裁決，狀態不變。上一輪 SPEC-GAP 2 已由本輪 S1-9990b18-1 處理。

## OWNER-BATCH

無新項目。SPEC-GAPS 1、2 若編排者認為涉及 RPC 契約或 DF 語意，再升級。

## 回歸 case 政策（#155）評估

兩條都能以 pytest 完整覆蓋：真 `SessionManager` 載入 state.json、真 `attach_device`／`bind_session`／`_attach_by_id`／`_attach_by_id_dynamic`／`_record_dynamic_open_failure` 路徑，開埠以計數替身取代，`PermissionError` 由替身拋出。不需要真板時序或真實 dialout 權限，不新增 `regression/` case。

## 對 Rust 承接的提醒

P5～P8 merge 主線後，依修正後的 Python 基線對齊：

- **佔位上的 `session.bind`**：回 `{"ok":true,"released":true,"recommended_action":"device_attach","session":…}`。不解除阻擋、不寫 binding override、不取消在途 `device attach`、不開埠。attach worker 對佔位不建 bridge、不重試。
- **佔位上的 `device.attach` 開埠遇一般 OSError**（非 held、非 not-present）：
  - 回 `ok:true`＋`DETACHED`（`ATTACH_FAILED:<例外名>`）；
  - 佔位移除自身、解除阻擋、以 pending COM 保留原 COM，再交 dynamic 偵測（只走 dynamic，不改綁空槽）；
  - dynamic 仍開不了埠時，依現行 dynamic 開埠失敗語意留列。
- 若 SPEC-GAPS 1 補記 schema，Rust 的 `session.bind` 回應鍵序跟著 `x-order`。

## 未完成項

- PTY bridge 模式四案的 `DeviceHeldError` 在本 VM 的根因未查明（起點就存在、同範圍重跑失敗集合不同，見上）。daemon.log 為空，沒有取得 holder pid；需要時可在 func-test harness 保留 daemon 端的 holder 診斷再追。
- 本輪沒有 Rust 變更，只跑了 `cargo check --workspace --target x86_64-pc-windows-gnu`，沒有跑 `cargo test`、Windows 原生或 bundle 實測。
- `rulings/s1.md` 的「`sw-pal` `process_ctl::spawn_daemon_detached_new_session` 讀 `/proc/<pid>/environ` 的競態」裁決（交付點：下一個 Stage 1 修正輪），收尾 fetch 時已由 S1X-b 第 4 輪 `b09e6dee`（`feature/s1x-b-fixes`）承接並經 `a2de3686` 裁決，本輪不重複處理。
- SPEC-GAPS 1～3 待編排者裁決；審查「既有碼待辦」的 `revoke_tx_op` 待開 issue／記 low-backlog。
- 本環境沒有 Windows 機器；新增回歸只在 Linux 實跑，皆標 `core_portable`／`memory_only`，不依賴 `os.getuid` 或 `/dev/null`。
