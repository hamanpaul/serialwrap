# OD-67：三支參數化測試的明確 ids（P5 第 7 輪）

- 擁有者裁決 OD-67（rulings/p5.md）：三支測試改用明確 `ids=`、add-only 登記新 id 後，各集合舊自動 id 可逐名 `--allow-removal`；每個集合列「舊 id → 新 id」並確認實際收集數不變。
- P5 選擇的 ids **沿用 pytest 9.0.3 的自動 id 形式**：鎖內已凍結的 nodeid 全數不變（舊 id → 新 id 為恆等），任何集合都**不需** `--allow-removal`；pytest 9.2 起被改名的過長參數（`invalid2`、`msg2-expected2`、`toml2-target-section`）改為與 9.0.3 相同的穩定 id。
- 收集：pytest 9.2.0 與 9.0.3 兩版本結果逐字相同（md5 `9850de42…`）；`test_invalid_regex_rpc_and_arbiter_reject_without_enqueue` 5、`test_cargo_toml_bypass` 4、`test_classify_pyserial_open_error` 4，與修改前收集數相同；13 筆實跑 13 passed。
- 附帶：`tests/test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only`（s0_bc.linux／s0_win_pytest.windows 的鎖內 nodeid 須在 collect-only 輸出）在 pytest 9.2.0 由 FAIL 轉為 passed（9.0.3 亦 passed）。
- `test_cargo_toml_bypass` 已於 `7facc75` 處理（F204），本檔一併列出。

欄位：「鎖內舊 id」＝修改前該集合凍結的 nodeid（— 表示未入鎖）；「pytest 9.2 舊自動 id」＝未給 ids 時 pytest 9.2.0 產生的 id；「新 id」＝明確 ids 的 nodeid（兩版本相同）。過長 id 以 … 省略中段。

## `s0_bc.linux`（主線擁有：未改；本變更後該集合無需任何動作）

- 修改前鎖內 4 筆 → 修改後 4 筆（新增 0、移除 0）；該集合涵蓋之測試的實際收集 4 筆，修改前後相同。

| 鎖內舊 id | pytest 9.2 舊自動 id | 新 id | 本集合 |
|---|---|---|---|
| `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[msg2-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | 不變 |

## `s0_bc.windows`（主線擁有：未改；本變更後該集合無需任何動作）

- 修改前鎖內 4 筆 → 修改後 4 筆（新增 0、移除 0）；該集合涵蓋之測試的實際收集 4 筆，修改前後相同。

| 鎖內舊 id | pytest 9.2 舊自動 id | 新 id | 本集合 |
|---|---|---|---|
| `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[msg2-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | 不變 |

## `s0_win_pytest.windows`（主線擁有：未改；本變更後該集合無需任何動作）

- 修改前鎖內 9 筆 → 修改後 9 筆（新增 0、移除 0）；該集合涵蓋之測試的實際收集 9 筆，修改前後相同。

| 鎖內舊 id | pytest 9.2 舊自動 id | 新 id | 本集合 |
|---|---|---|---|
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[(]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[(]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[(]` | 不變 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[]` | 不變 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[aaaa…aaaaaaaaaaaaaaaaaaaaaaaaaaaaa]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[invalid2]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[aaaa…aaaaaaaaaaaaaaaaaaaaaaaaaaaaa]` | 不變 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[12]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[12]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[12]` | 不變 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[.*]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[.*]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[.*]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[msg2-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | 不變 |

## `s21_gate:pytest_full.linux`（P5 擁有）

- 修改前鎖內 11 筆 → 修改後 13 筆（新增 2、移除 0）；該集合涵蓋之測試的實際收集 13 筆，修改前後相同。

| 鎖內舊 id | pytest 9.2 舊自動 id | 新 id | 本集合 |
|---|---|---|---|
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[(]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[(]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[(]` | 不變 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[]` | 不變 |
| — | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[invalid2]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[aaaa…aaaaaaaaaaaaaaaaaaaaaaaaaaaaa]` | add-only 新增 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[12]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[12]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[12]` | 不變 |
| `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[.*]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[.*]` | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[.*]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | `TestManagerAttach::test_classify_pyserial_open_error[could not… denied.', None, 5)-expected0]` | 不變 |
| `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…pired.', None, 121)-expected1]` | 不變 |
| — | `TestManagerAttach::test_classify_pyserial_open_error[msg2-expected2]` | `TestManagerAttach::test_classify_pyserial_open_error[could not…ecified.', None, 2)-expected2]` | add-only 新增 |
| `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | `TestManagerAttach::test_classify_pyserial_open_error[garbage-expected3]` | 不變 |
| `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …ools/build.rs"\n-build-script]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …ools/build.rs"\n-build-script]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …ools/build.rs"\n-build-script]` | 不變 |
| `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …nversion = "1"\n-build-script]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …nversion = "1"\n-build-script]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …nversion = "1"\n-build-script]` | 不變 |
| `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …= "../foo" }\n-target-section]` | `test_cargo_toml_bypass[toml2-target-section]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …= "../foo" }\n-target-section]` | 不變 |
| `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …ies]\ncc = "1"\n-build-script]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …ies]\ncc = "1"\n-build-script]` | `test_cargo_toml_bypass[[package]\nname = "sw-core"\nversion = …ies]\ncc = "1"\n-build-script]` | 不變 |

