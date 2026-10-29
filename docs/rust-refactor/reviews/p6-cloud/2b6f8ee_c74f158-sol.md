> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | High | 已修 `bd62919` | 0 | 本輪 sw-core 只變動 test-fakes 延遲探針（`actor.rs`／`mailbox.rs`，P5 合入），回放停放命令路徑未變；sw-core／sw-daemon 測試以一般使用者實跑通過。 |
| 2 | Medium | 已修 `bd62919` | 0 | raw owner／UART TX／本地回顯路徑本輪未變；相關 cargo 測試通過。 |
| 3 | High | 已修 `bd62919` | 0 | release close 世代防線本輪未變；相關 cargo 測試通過。 |
| P6-e9b5c61-1 | High | 已修 `7370b8f` | 0 | SelfTest suspend 憑證結算本輪未變。 |
| P6-e9b5c61-2 | High | 已修 `7370b8f` | 0 | 取消停放命令的 registry 結算本輪未變。 |
| P6-e9b5c61-3 | Medium | 已修 `0c20ee8` | 0 | 完整 pytest 中 `tests/test_slice21_case_freeze.py` 指紋斷言通過。 |
| P6-e9b5c61-4 | Medium | 依裁決不處理 | 9 | 依 rulings/p6.md（OD-55）不再累加。合入 P5 `86b54d8` 後依裁決重驗 `tests/test_l1_judge.py:882`：仍紅，剩 13 列，全屬 `s21_core.windows`（OD-55 待 Windows 原生收集，P5 LOCAL-WINDOWS-TODO）；linux 27 列已依 OD-55 逐名移出，`regen_lock --check --base origin/feature/rust-refactor`、`--base 86b54d8` 均 OK。 |
| P6-c6ddda1-1 | Medium | 已修 `bf61e3e` | 0 | `12318a5d` 只把推導不出的集合排除在逐份報告計數外，同份報告重複仍判 INSUFFICIENT；`tests/test_s22_gate_check.py` 21 passed。 |
| P6-c6ddda1-2 | Medium | 已修 `464adec` | 0 | 以釘定引擎重跑 `policy_check --repo .`：fail 0，R-21 pass。 |
| P6-bf61e3e-1 | Medium | 已修 `e0aab47` | 0 | 修訂鏈斷言本輪未變，完整 pytest 通過。 |
| P6-b64dad9-1 | High | 已修 `2b6f8ee` | 0 | `judge_parity` 與出口報告本輪未變；`test_process_evidence_row_requires_structured_red_and_green` 通過。 |

## B 新發現

- **P6-c74f158-1 [Medium] tests/test_contracts_slice22.py:48**
  - 問題：merge `4cc7f022` 合入主線 SC-39（OD-48）。衝突解法只把 `tests/test_contracts_catalog.py` 的 SC 集合斷言改成聯集，漏了本 P6 檔的同類精確集合斷言（`c74f1583` 也沒補）。
  - 失敗情境：`python3 -m pytest -q tests/test_contracts_slice22.py::test_sc30_reserved_for_p7`。
    - 2b6f8ee：passed。
    - c74f158：FAILED，`Extra items in the left set: 'SC-39'`。
    - 已在可寫的 c74f158 clone 以一般使用者重跑，仍紅，排除環境因素。
    - 違反「不得引入新的測試失敗」，也使 07 Task 13 Step 4「`tests/` 全套 0 failed」無法成立。
  - 修法：預期集合加入 `"SC-39"`，註解補「SC-39 依 OD-48」，保持精確相等、不放寬，與 `test_contracts_catalog.py::test_bc_real_sc_df_ranges` 一致；`SC-30` 不得出現的保留斷言不變。
  - 追蹤：與並行審查 `2b6f8ee_4cc7f02-sol.md` 的 P6-4cc7f02-1 為同一缺陷。rulings/p6.md（2026-10-29）已裁決「照修」，交付 P6 第 7 輪；裁決不屬不成立／延後，故仍計入本結論。
- **P6-c74f158-2 [Low] scripts/rust/s22_gate_check.py:169**
  - 問題：`caps is None` 分支直接記 EVIDENCE_MISSING，不判報告。報告中若有 failed 列、但沒有任何 AUTOMATED 條目引用它，整體只得 INSUFFICIENT；`2b6f8ee` 對同一輸入是 FAIL。這也與 `slice21_gate_judge` 的「FAIL 優先於 EVIDENCE_MISSING」不一致。
  - 失敗情境：selector 為 `--include-ignored cap_posix_pty__`，報告含 `cap_posix_pty__mux_mixed_pty_and_tcp` passed 與 `console_conformance::unmapped_test` failed（exit 101）。
    - 實得：section EVIDENCE_MISSING、result INSUFFICIENT。
    - 預期：FAIL。
  - 仍為 Low：現行凍結鎖 selector 皆可推導，沒有現實路徑。
  - 修法：該分支在報告存在且含 failed／error／xpassed 列時，判 FAIL 並列出 nodeid，否則維持 EVIDENCE_MISSING；補一則未被對照表引用的 failed 列測試。

已逐一檢視本輪 commit：
- `d72aface`（純新增測試）：四案在修正前確實紅，以 `git archive` 匯出實跑 4 failed。
- `12318a5d`（`derive_caps`）：與收集器一致。
  - argparse 於 3.12／3.13 皆保留第二個 `--`，`collect_argv` 產生的報告 argv 與 selector 相符。
  - 可推導集合的判定流程與前版相同。
- 三個 merge：
  - `8caf6c48`：與主線同檔逐字相同。
  - `4cc7f022`：`behavior_changes.yaml` 與 P5 `d657a7c` 解法逐字相同；`proc_scan/posix.rs` 等於 P5 解法加上 P6 的 `held_fd_targets`／`held_tty_paths_with`。
  - `c74f1583`：L1 鎖等於 P6 ∪ P5，扣除 P5 `7953f98` 依 OD-55 移除的 27 列。selector 全不變，`regen_lock --check` OK。

實跑環境：以一般使用者（ubuntu）執行，Rust 1.96.1，`CARGO_TARGET_DIR=/tmp/solrev-p6-target`。
- `cargo test --workspace --features test-fakes` 通過，以下兩項除外：
  - `emitted_codes` 須寫 worktree `target/`，屬環境因素。
  - session_manager 一案偶發失敗，見下節。
- `console_conformance -- --ignored cap_posix_pty__`：7 passed。
- 完整 pytest：31 failed／10 errors。
  - HEAD 與 2b6f8ee 的失敗集合逐一比對後，除 P6-c74f158-1 外，皆為環境因素（worktree 唯讀、無 pyserial／minicom／Windows／p4 evidence）或兩版皆有的既有現象。
  - 10 個 errors 未逐一列名，只確認 `test_f2_backpressure` 在兩版皆有。
- 全程未修改審查 worktree，未接觸 production daemon、~/.config、~/.local/state、/dev/ttyUSB*。

## 既有碼待辦（不計入結論）

- `docs/rust-refactor/plans/07-slice22-console.md:2693-2694`：Step 4 的 cargo 收集命令與鎖 selector 不符，這是本輪之前就有的文字，與並行審查同列。
  - cargo_workspace 缺 `-- --skip cap_`。
  - console_conformance_pty 用 `--include-ignored` 且沒有 `--caps`。
  - 照文執行會得 selector mismatch，屬 fail-closed，不會誤放行。
  - 建議改為引用 `s22_gate_check.py --plan` 的 `collect_argv`。
- `crates/sw-core/tests/session_manager.rs:2348`（`release_commit_paused_then_bind_keeps_binding_without_stale_released_row`，P5 區域）：
  - 現象：完整 workspace 實跑失敗 1 次，斷言為重啟後 `device_by_id`。
  - 重現：之後單獨或並行重跑 35 次皆通過。
  - 本輪未改該測試與 session_manager 程式，根因未定，不以 flaky 放行。
- 完整 pytest 中，以下各案於完整套件失敗一次，之後在 HEAD 與 2b6f8ee 重跑皆通過：
  - `tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`
  - `tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[CMD_TOO_LONG]`
  - `tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-PASSTHROUGH]`
- `tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python` 例外：在一次重跑中再度失敗（`ATTACH_FAILED:DeviceHeldError`）。
- 兩版皆有的 DeviceHeldError 失敗：`test_f2_backpressure`、`test_functest_bridge_smoke`。
- Python 基線本輪未變。依 CLAUDE.md 不當作已知 flaky，需另行排除 throwaway daemon 洩漏或 holder 掃描權限因素。

## 對裁決的異議（不計入結論）

無。

結論：需修正
