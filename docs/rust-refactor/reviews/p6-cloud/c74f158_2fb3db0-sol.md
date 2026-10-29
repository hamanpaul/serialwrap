> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p6 增量審查 c74f158..2fb3db0

範圍：first-parent 共 3 個 commit，沒有 merge。

- `04d5f25`：`tests/test_contracts_slice22.py` 補 SC-39。
- `a78f106`：`p6-fix-responses.md` 第六輪紀錄、`slice22-exit-report.md` 第 6 輪 gate 紀錄。
- `2fb3db0`：`p6-fix-responses.md` 補兩列。

`git diff --stat c74f158 2fb3db0` 只有上述 3 檔。`crates/`、`Cargo.lock`、`scripts/`、`sw_core/`、`tests/acceptance/` 都沒有變動，所以 Rust 產物與前輪 c74f158 的實跑結果相同，本輪不重跑 cargo。

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | High | 已修 `bd62919` | 0 | 本輪沒有改 Rust（`git diff c74f158 2fb3db0 -- crates Cargo.lock` 為空），沿用前輪實跑結果。 |
| 2 | Medium | 已修 `bd62919` | 0 | 同上。 |
| 3 | High | 已修 `bd62919` | 0 | 同上。 |
| P6-e9b5c61-1 | High | 已修 `7370b8f` | 0 | 同上。 |
| P6-e9b5c61-2 | High | 已修 `7370b8f` | 0 | 同上。 |
| P6-e9b5c61-3 | Medium | 已修 `0c20ee8` | 0 | 2fb3db0 全套 pytest 中 `test_freeze_case_fingerprints_match_current_cases[2.1]`／`[2.2]` 通過。 |
| P6-e9b5c61-4 | Medium | 依裁決不處理 | 9 | 依 rulings/p6.md（OD-55）不累加。2fb3db0 上 `test_l1_lock_cargo_sets_exclude_ignored_tests` 仍紅，斷言列出 13 列，全屬 `s21_core:windows`（OD-55 待 Windows 原生收集，P5 LOCAL-WINDOWS-TODO），與前輪相同。 |
| P6-c6ddda1-1 | Medium | 已修 `bf61e3e` | 0 | `s22_gate_check.py` 本輪未變；`tests/test_s22_gate_check.py` 21 passed。 |
| P6-c6ddda1-2 | Medium | 已修 `464adec` | 0 | 以釘定引擎（`2dcf2f8`）在 2fb3db0 重跑 `policy_check --repo .`：0 fail，R-19／R-22 warn，R-09 與 R-21 pass（`python3` 為 3.12）。 |
| P6-bf61e3e-1 | Medium | 已修 `e0aab47` | 0 | 修訂鏈與 freeze 相關測試本輪未變，全套 pytest 中通過。 |
| P6-b64dad9-1 | High | 已修 `2b6f8ee` | 0 | `_process_evidence_problems` 本輪未變；`test_process_evidence_row_requires_structured_red_and_green` 通過。#185 段落本輪未改。 |
| P6-c74f158-1 | Medium | 已修 `04d5f25` | 0 | 修法與前輪建議相同：預期集合加入 `"SC-39"`，註解補「SC-39 由主線依 OD-48 登記」，仍是精確相等，SC-30 保留語意不變（集合不含 SC-30）。以一般使用者實跑 `test_contracts_slice22.py`：c74f158 為 1 failed（`test_sc30_reserved_for_p7`），2fb3db0 為 8 passed。2fb3db0 的實際 SC 集合與 `test_contracts_catalog.py:1377` 的預期相同；主線 `origin/feature/rust-refactor` 與 P5 也沒有 P6 未納入的 SC 編號。 |
| P6-c74f158-2 | Low | 未修 | 1 | `scripts/rust/s22_gate_check.py:169` 本輪未變。`a78f106` 在 fix-responses 寫「推導不出 → EVIDENCE_MISSING，failed 列仍判 FAIL」，但這只對 AUTOMATED 條目引用到的 failed 列成立，未被引用的 failed 列仍只得 INSUFFICIENT，與本項同一根因。Low 不擋。依收斂規則 §1 應記入 low-backlog，但 `origin/docs/parity-amendments` `61946bf` 的 `low-backlog.md` 尚未收錄。 |
| P6-4cc7f02-1（並行審查 `4cc7f02_c74f158-sol.md`） | Medium | 已修 `04d5f25` | 0 | 與 P6-c74f158-1 是同一缺陷。 |
| P6-4cc7f02-2（並行審查） | Low | 未修 | 2 | `test_l1_judge._selector_explicit_caps` 與 gate 的 `derive_caps` 仍是兩份規則。實作者在 `2fb3db0` 註明依收斂規則不修。Low 不擋，`low-backlog.md` 同樣尚未收錄。 |

## B 新發現

NO FINDINGS

逐一檢視本輪 commit：

- `04d5f25`（`tests/test_contracts_slice22.py:45-49`）
  - 只改預期集合與註解，沒有放寬成子集合比對，其他斷言不變。
  - 本檔的其他 SC 斷言（`test_new_sc_rows_follow_p0_schema` 等）以 `NEW_SC` 逐項比對，不受 SC-39 影響。
  - rulings/p6.md 要求「每次 merge 後跑完整 `tests/test_contracts_*.py`」。本輪沒有 merge，所以以 2fb3db0 實跑 `tests/test_contracts_*.py`、`test_resource_markers.py`、`test_slice21_skeleton_guards.py`、`test_slice21_case_freeze.py`、`test_s22_gate_check.py`、`test_l1_judge.py`：581 passed、3 failed。3 個失敗全在 `test_l1_judge.py`，都是既有失敗，見下方全套比對。
- `a78f106`（文件）
  - 第 6 輪 gate 紀錄如實記為 result FAIL。
    - cargo_workspace 唯一失敗歸因於 P5 `sw-pal` 測試的 fd 重用競態。這裡附了同範圍重跑兩次的結果、失敗集合不同、以及 100 次連跑的統計，符合 CLAUDE.md 的舉證要求。
    - 文件沒有把 FAIL 改寫成 PASS。
  - gate 的 5 個 linux 集合 selector（`l1_required.lock.json` 的 `s22_gate:*`）都不含 `tests/test_contracts_slice22.py`。所以 c74f158 上 parity_py／py_parity PASS 與當時恆紅的 `test_sc30_reserved_for_p7` 不矛盾。
  - 全套 pytest 的數字（終點 c74f158 為 11 failed／5688 passed／219 skipped；修正後失敗集合為起點的真子集）與本審查實跑相符，只有一處差異：本審查多一項 `test_l1_lock_nodeids_subset_of_collect_only`。原因是 pytest 版本，見既有碼待辦。
- `2fb3db0`（文件）
  - P7-9065f14-1 的處理說明屬實。
    - `slice22_evidence_map.yaml` 的 `cases` 鍵中沒有 `l15-delayed-reply`。
    - `test_slice22_case_set_frozen_and_mapped`（`tests/test_contracts_slice22.py:105-107`）斷言兩件事：該案不在 `slice_2.2`、在 `slice_2.3`；map 鍵等於凍結的 `slice_2.2`。
    - 2fb3db0 實跑通過。
  - P6-4cc7f02-2 依收斂規則不修，屬 Low，可以接受。

### 實跑

環境：一般使用者 ubuntu（uid 1000）、Python 3.12.3、pytest 9.2.0、PyYAML 6.0.1、jsonschema 4.26.0，沒有 pyserial；`python3` 指向 3.12；Rust 1.96.1 已安裝但本輪不需要。

- 兩個 commit 各自一份可寫 clone（`~/run-2fb3db0`、`~/run-c74f158`），依序執行完整 `python3 -m pytest -q tests/`。審查 worktree `/home/user/rev` 沒有修改。
- 2fb3db0：11 failed、5688 passed、219 skipped、1 xfailed（57 分 44 秒）。
- c74f158：12 failed、5687 passed、219 skipped、1 xfailed（57 分 55 秒，同環境、接續執行；loadavg 0.73～0.84）。
- 失敗集合比對：c74f158 減 2fb3db0 恰為 `tests/test_contracts_slice22.py::test_sc30_reserved_for_p7`，2fb3db0 沒有多出任何失敗；2fb3db0 的失敗集合是 c74f158 的真子集，沒有新失敗。
- 2fb3db0 的 11 項失敗都是既有失敗或環境因素：
  - pyserial 未安裝 5 項：`test_stage0_tx_outcome` 1、`test_stage0_tx_permit` 2、`test_stage0_uart_exclusive` 1、`test_doctor::TestWindowsDoctor::test_pyserial_ok_when_importable`。
  - 證據缺席 2 項：`test_cargo_workspace_tests_windows_native`、`test_p4_required_cases_have_passing_evidence`。
  - `test_l1_judge` 3 項：
    - `s21_core.windows` 2 項（OD-55）。
    - `test_l1_lock_nodeids_subset_of_collect_only`，原因是 pytest 版本。
  - `slice21/test_f2_backpressure.py::test_queue_full_backpressure`：P5 既有碼待辦。
- `test_multiagent_e2e::test_five_agents_three_rounds_no_conflict`、`test_two_daemons_same_tty`、`test_bc_real_sc_df_ranges` 在 2fb3db0 皆通過。
- 全程未接觸 production daemon、`~/.config/serialwrap`、`~/.local/state/serialwrap`、`/dev/ttyUSB*`。

## 既有碼待辦（不計入結論）

- `tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only` 的紅燈取決於 pytest 版本。
  - 原因：`s0_bc.linux` 鎖的 `tests/test_stage0_uart_exclusive.py::TestManagerAttach::test_classify_pyserial_open_error[could not open port 'COM9': FileNotFoundError(2, ...)-expected2]` 是 pytest 9.0.3 產生的 id；pytest 9.2.0 對同一參數產生 `[msg2-expected2]`。兩版各自實際 collect 已確認。
  - 影響：`rust.yml` 釘 pytest 9.0.3，但 `tests.yml` 的 `pip install pytest` 沒有釘版本，安裝到 9.2.0 時 `pytest -q tests/` 會恆紅。兩個 commit 皆然，不是本輪引入。
  - 建議：該參數化加上明確 `ids=`，或在 `tests.yml` 釘 pytest 版本，再以擁有者授權的方式更新鎖列。
- policy_check R-16 依 Python 版本而定。
  - `python3` 為 3.13 時，`serialwrap session --help` 的 argparse 排版與 README 標記不同：說明欄起點由第 22 欄變為第 24 欄，`interactive-status` 不再換行，因此 R-16 fail。
  - `python3` 為 3.12 時 pass。2fb3db0 與 c74f158 結果相同，不是本輪引入。
- 前輪「既有碼待辦」各項（`plans/07-slice22-console.md:2693-2694` 的收集命令、`session_manager.rs:2348` 偶發失敗、DeviceHeldError 類失敗）本輪都沒有變動，不重列。另外，`a78f106` 已把 `sw-pal` 兩個測試競態記為 P5 既有碼待辦。

## 對裁決的異議（不計入結論）

無。

結論：可放行
