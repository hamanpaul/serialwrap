> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p7 增量審查 `de7a95a..b85eec5`

本輪只有一個 commit：`b85eec5`（19 檔，+5878／−22），沒有 merge commit。這個 commit 沒有改 production code（`crates/`、`sw_core/` 沒有變動），也沒有改 `scripts/rust/p7_case_coverage.py`、`scripts/rust/p7_gate.sh`、`tests/test_p7_gate_win_argv.py`。改動範圍如下：

- 3 個 s23 情境 YAML
- `scripts/rust/p7_ab_report.py`
- L1 鎖：兩個 `p7_gate:*` 集合 add-only
- `contracts/behavior_changes.yaml`：新增 DF-23
- `.github/workflows/tests.yml`：OD-56 白名單加兩名
- 測試與文件

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | 本輪沒有改 production code，修正保留。 |
| 2 | Medium | 已修 `f150a24` | 0 | 同上。 |
| 3 | Medium | 已修 `f150a24` | 0 | 同上。 |
| 4 | Medium | 已修 `f150a24` | 0 | 同上。 |
| P7-71fe017-1 | Medium | 已修 `030d294` | 0 | 同上。 |
| P7-71fe017-2 | High | 已修 `7e2da7a` | 0 | 同上。 |
| P7-71fe017-3 | High | 已修 `8134806` | 0 | 同上。rulings/p7.md 已移交 P5，P5 修正合入後的狀態保留。 |
| P7-71fe017-4 | Medium | 已修 `afebff0` | 0 | 同上。 |
| P7-27b5bf4-1 | High | 已修 `20c9454` | 0 | 同上。 |
| P7-030d294-1 | High | 已修 `1677489` | 0 | 同上。 |
| P7-9065f14-1 | Medium | 未修 | 6 | `tests/acceptance/slice22_evidence_map.yaml:47` 仍是 `l15-delayed-reply: {status: PENDING}`。p7-fix-responses 維持 NEEDS-RULING。rulings/p7.md（origin/docs/parity-amendments `10bc4aa`）至今沒有對應裁決。 |
| P7-c5f0fc9-1 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-2 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-3 | Medium | 已修 `f9e90a9` | 0 | 同上（`p7_gate.sh` 本輪沒有變動）。 |
| P7-c5f0fc9-4 | High | 已修 `7bb9cfd` | 0 | 同上。 |
| P7-c5f0fc9-5 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-c5f0fc9-6 | Medium | 已修 `29aaa7b` | 0 | 同上。 |
| P7-7bb9cfd-1 | Medium | 已修 `b85eec5` | 0 | 新增 `_run_dir_label`（`scripts/rust/p7_ab_report.py:97`）。repo 外的 run_dir 改記為 `$EXTERNAL/<完整路徑>`，不再套用 `$RUN_DIR` 規則而固定變成 `$RUN_DIR/.`，表頭因此可以還原 `$RUN_DIR/…` 證據路徑的基準。回歸測試 `test_report_header_run_dir_is_restorable` 疊到 de7a95a 樹上會 FAILED。表頭格式沒有其他解析端。 |
| P7-cd7acf0-1 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-2 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-3 | High | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-4 | Medium | 未修 | 3 | L1 鎖仍沒有 `p7_gate:contract-blackbox-slice22-b`。實作者提出「不成立／BLOCKED-UPSTREAM」主張。經核對，`run_p7_ab.sh:84-86` 列出的 `test_console_*.py`、`test_interactive_lease.py`、`test_slice22_family_mirrors.py` 確實都不在本分支。gate 依 `blocked` 標記記為 BLOCKED-UPSTREAM／INCOMPLETE，不會誤判成 PASS（r12b 實跑也是 INCOMPLETE）。不過這項主張還沒有經編排者裁決，已累計 3 輪，依收斂規則 §4 應進 NEEDS-RULING。 |
| P7-cd7acf0-5 | Medium | 已修 `f9e90a9` | 0 | 同上。 |
| P7-cd7acf0-6 | Medium | 已修 `f9e90a9` | 0 | 同上。 |
| P7-f9e90a9-1 | High | 已修 `b85eec5` | 0 | 詳見表後說明。 |
| P7-f9e90a9-2 | Medium | 已修 `b85eec5` | 0 | `test_regression_phase_status` 以明確 `ids=` 保留六個舊 id，對應的參數語意與 cd7acf0 原版逐項相同（PASS／FAIL／skip→INCOMPLETE／空→INCOMPLETE／rc 2→INCOMPLETE／None→INCOMPLETE）。另外四個反例給了新 id，已入鎖。以 pytest 9.0.3 collect-only 核對，鎖內所有 `p7_gate:*` 集合的 Python nodeid 全部存在。 |
| P7-f9e90a9-3 | High | 已修 `b85eec5` | 0 | 詳見表後說明。 |
| P7-f9e90a9-4 | High | 未修 | 2 | 詳見表後說明。 |
| P7-f9e90a9-5 | High | 未修 | 2 | 詳見表後說明。 |
| P7-de7a95a-1 | Low | 未修 | 1 | 本輪沒有改 `p7_case_coverage.py`。fix-responses 記為依收斂規則轉 low-backlog（low-backlog.md 尚未收錄）。Low，不擋放行。 |
| P7-de7a95a-2 | Low | 未修 | 1 | 同上。 |
| P7-de7a95a-3 | Low | 未修 | 1 | 本輪沒有改 `tests/test_p7_gate_win_argv.py`。Low，不擋放行。 |

### 表中標「詳見表後說明」的項目

**P7-f9e90a9-1（已修）**

- `p7_gate:python3-m-pytest-q-tests` 由 49 筆擴成 5616 筆，舊的 49 筆全部保留（add-only），selector 不變。
- 以 pytest 9.0.3（CI 釘選版本）收集，共 5843 筆。凍結集合是其子集，差集 227 筆。把這 227 筆實跑，結果為 226 skipped、1 xfailed。skip 原因只有以下幾類，沒有「應在 Linux 執行，卻因環境缺件而被排除」的節點：
  - Windows-only／WSL／pwsh 95 筆
  - Python 標籤下的 rust-only contract 56 筆
  - 缺 L1.5 env 的 test_slice21／23_l15 53 筆，這部分另由 `p7_gate:l15-*` 列承擔
  - 真機序列埠 9 筆
  - docker 3 筆
  - re2 可選依賴 3 筆
  - DF／OD／SC-30 標註 4 筆
  - 其他 3 筆：rust-s21 專屬範圍清單、子行程專用、§5.3 凍結紀錄待審
- `tests/test_p7_gate_win_argv.py` 的 4 個節點，以及前輪新增的三支回歸，都已入鎖。
- 回歸測試 `test_full_pytest_row_needs_whole_frozen_set` 疊到 de7a95a 樹上會 FAILED。
- pytest 版本相依問題見 B-1。

**P7-f9e90a9-3（已修）**

以突變驗證：在 scratch 的 `git archive` 副本中，讓 Python daemon 在 `_update_boot_quiet_locked` 偵測到 banner 後，立即以 `human:` 來源送出一個 `\n`。改用 human 來源，是因為 `system:` 來源會被產品的 quiet gate 以 `not_submitted quiet` 擋下。

| 情境版本 | 結果 |
|---|---|
| 舊情境（de7a95a） | **PASS**（漏抓） |
| 新情境（revision 2026-10-29.2） | **FAIL**：`assert_zero_tx(quiet) 失敗: 操作期間 wire serial_read 增加 1 位元組` |

未突變時，Python 與 Rust 皆 PASS。結構回歸 `test_s23_oracle_windows_cover_the_violation_points` 疊到 de7a95a 樹上會 FAILED。

**P7-f9e90a9-4（未修）**

已修部分：原報告的反例（`HUMAN_P7`／`echo AGENT_P7` 夾在 secret 與 probe 之間）在新 payload `root\ns3cretP7\necho __READY__` 下次數不足 2，會轉紅。「agent 僅在 READY 後」這個子要求，實作者以 Python 實測撤回，經核對成立。Python wire 中 `HUMAN_P7` 在 seq 25、`echo AGENT_P7` 在 seq 29，都在第二次登入的 `root\n`（seq 45）之前，所以 1:1 基線本身不符合該要求。

仍未涵蓋：probe 送出後到 READY 之前這一段，以及 nonce 本身。原修法要求的是「包含 probe／nonce 的完整登入區間」。

具體反例：第二次登入的 wire 為 `root\ns3cretP7\necho __READY__90780d7c\necho AGENT_P7\n`，agent TX 早於 nonce 回應（也就是 READY 之前）。這份 wire 兩段 payload 仍各出現一次，`check_contiguous` 回 PASS。Python 的 agent 命令只在 READY 時派送，這一段在 Python 不會有 agent bytes（實測 seq 53 probe 之後到 READY 都沒有 TX）。

runner 的字面比對無法表達隨機 nonce 之後的界線，實作者也沒有把這一段列為 SPEC-GAP。建議依收斂規則 §3 補列 SPEC-GAP（runner 需支援帶 nonce 的樣式，或依 WAL 先後判定），交由編排者裁決。

**P7-f9e90a9-5（未修）**

已修部分：no-op 這一半。新增 `bridge_generation` 交接前為 1、交接後為 2，本機 Python 與 Rust 都觀察到 1→2。

仍未涵蓋：原修法明列「回條前重開」的錯誤實作也必須轉紅，本案仍做不到。實作者自列 SPEC-GAP（要 runner 新增 peer_state 順序斷言），沒有編排者裁決。

提醒：SPEC-GAP 提議的 peer_state 修法對 Rust 不可行。用 strace 追 Rust daemon 的 force recover，舊 worker 執行緒在 `13:03:41.729286` 做 `close(5)`，主執行緒在 `13:03:41.730920` 重開 `/dev/pts/3`（同一 fd 5）。順序正確，但只隔 1.6 ms，而 bridge 的 `peer_watch` 每 50 ms 才取樣一次（`tools/uart_stdio_bridge/src/uart_stdio_bridge/bridge/pumps.py:388`）。因此：

- Rust 的 wire 在 force 窗口內沒有 `peer_state open:False→True`。
- Python 的 wire 有（seq 6→7）。

以 peer_state 判順序會誤判一個正確的 Rust 實作。建議改用 fd 層級的 oracle，例如比照 `test_release_during_login_waits_for_handle_close` 以 5 ms 取樣 fd 峰值，但改從 ATTACHED＋CREDENTIALS_UNRESOLVED 起手。現有那支 L1 測試是從 READY 起手，起始狀態不同。

## B 新發現

**P7-b85eec5-1 [Medium]** [tests/acceptance/l1_required.lock.json:2121](tests/acceptance/l1_required.lock.json:2121)（另見 :4628、:5912；執行點 [scripts/rust/p7_gate.sh:198](scripts/rust/p7_gate.sh:198)）

**問題**：本輪擴充的全套 pytest 必要集合，新凍結了 3 個會隨 pytest 版本改名的參數化 nodeid。`p7_gate.sh` 用主機上任意版本的 `python3 -m pytest` 跑全套列。

**具體失敗情境**：gate 主機的 pytest 為目前 PyPI 最新版 9.2.0（P5 第 6 輪雲端 gate 即用此版），且全部測試通過。預期該列 PASS，實際為 INCOMPLETE `missing 3`。原因是 pytest 9.2.0 對過長的字串參數改用 `<argname><index>` 當 id：

| 鎖內行號 | 凍結的 nodeid | pytest 9.2.0 收集到的 id |
|---|---|---|
| 2121 | `tests/test_command_completion.py::test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[aaaa…（1025 字元）]` | `[invalid2]` |
| 4628 | `tests/test_rust_cfg_boundary_bypass.py::test_cargo_toml_bypass[[package]\n…[target.x86_64-pc-windows-gnu.dependencies]…-target-section]` | `[toml2-target-section]` |
| 5912 | `tests/test_stage0_uart_exclusive.py::TestManagerAttach::test_classify_pyserial_open_error[could not open port 'COM9': …-expected2]` | `[msg2-expected2]` |

實測：

- pytest 9.0.3、9.1.0、9.1.1 收集時，凍結的 5616 筆全部存在。
- pytest 9.2.0 收集時，上表 3 筆不存在。
- 以 9.2.0 的收集結果構造全 passed 的 l1-results（runner 為 pytest、argv 為 `-q tests/`），`p7_gate_report._test_row_status()` 回傳 `('INCOMPLETE', "missing 3；required case not satisfied; …")`。

另一個後果：rulings/p5.md「P5 第 6 輪 SPEC-GAP #3」已裁定對這三支測試補明確 `ids=`，交付點為主線或 P5 第 7 輪。該修正合入後，這 3 個舊形式 id 在任何 pytest 版本下都會消失，P7 全套列會持續 INCOMPLETE，直到重新登記。P5 第 6 輪的 pytest_full 登記已明確排除這 3 個 id，P7 本輪則把它們凍結進去。

**修法**：照該裁決處理，不要另外在 gate 或 CI 釘 pytest 版本。

1. 在 p7-fix-responses 登記這項依賴，並請編排者把 rulings/p5.md 的同一裁決擴及 `p7_gate:python3-m-pytest-q-tests`。
2. 明確 ids 修正合入 P7 後，以 `regen_lock --l1-add-only` 登記新 id。
3. 3 個舊 id 的移除逐名列入 `--allow-removal p7_gate:python3-m-pytest-q-tests:linux:<舊 id>`，若需擁有者授權則列 OWNER-BATCH。
4. 在此之前，gate 報告須註明受測 pytest 版本；9.2.0 下該列 INCOMPLETE 屬已知原因。

**P7-b85eec5-2 [Low]** [func-test/scenarios/s23-recover-force-handoff.yaml:65](func-test/scenarios/s23-recover-force-handoff.yaml:65)

**問題**：描述與註解寫「實際關閉並重開 bridge **恰一次**」，但 `wait_cli_until … bridge_generation equals: 2` 只要輪詢期間看到一次 2 就通過，之後的步驟不再檢查 `bridge_generation`。假設回覆 FORCE_RECOVER_TIMEOUT 後又自動重開一次（1→2→3，第三次發生在輪詢看到 2 之後），本案仍會 PASS。重開 tty 可能造成 DTR／RTS 等裝置側副作用。目前 Python 與 Rust 實測都只到 2，沒有現行失敗情境。

**修法**：在最後一個 `last_error == CREDENTIALS_UNRESOLVED` 步驟之後，隔數秒再加一次 `wait_cli_until bridge_generation equals: 2`（若已變成 3，輪詢會逾時轉紅）。

## 既有碼待辦（不計入結論）

- `scripts/rust/p7_gate.sh:95-98`：快照來源仍寫死 `$ROOT/target/debug/`，沒有遵從 `CARGO_TARGET_DIR`。此項前輪已列，本輪沒有變動。
- `s0_bc`（linux／windows）與 `s0_win_pytest`（windows）早已凍結 `test_classify_pyserial_open_error[could not open port 'COM9'…]` 等同類的 pytest 版本相依 id。`tests.yml:30` 的 Linux pytest job 安裝的 pytest 沒有釘版本。這部分已在 rulings/p5.md 同一裁決範圍內（涉及 s0／s1 鎖者先查擁有權）。

## 對裁決的異議（不計入結論）

無。

## 驗證

**環境與建置**

- 測試以一般使用者 `rev` 執行，使用 Python 3.12 venv：pytest 9.0.3、PyYAML 6.0.3、jsonschema 4.10.3，與 CI 釘選一致。另用 pytest 9.1.0、9.1.1、9.2.0 做收集比對。
- Rust 1.97.0，`CARGO_TARGET_DIR=/tmp/solrev-p7-target`，`cargo build --locked -j 2 --workspace --bins`；artifact 快照到 worktree 外。

**L1 鎖**

- de7a95a→b85eec5 只動了兩個集合，皆為 add-only，selector 不變，沒有任何移除：
  - `p7_gate:python3-m-pytest-q-tests`：49→5616
  - `p7_gate:l15-slice23-b`：20→21
- 以 tests.yml 的 `--allow-removal` 清單（552 名）執行 `regen_lock.py --check --base 2472f78` 與 `--base de7a95a`，皆 OK。
- 對目前的 `origin/feature/rust-refactor` 比對，出現 `s1a_cargo`／`s1b_cargo` linux 共 98 筆 FAIL。原因是 feature 分支在 P7 之後新增了節點，與本輪無關；`s21_core` 沒有缺漏，OD-56 兩名的豁免有效。

**L1.5 s23 情境**

- bridge 自測 PASS。
- 在 `SERIALWRAP_GATE=1` 下執行 `test_slice23_l15.py -k "quiet-mid or interleave or force-handoff or oracle_windows"`，結果 1 failed、6 passed，與 commit 記述一致。失敗的是 Rust interleave，原因是 `session.console_attach` 回 METHOD_NOT_FOUND（P6 BLOCKED-UPSTREAM）。

**DF-23（OD-57）**

- `test_release_during_login_waits_for_handle_close` 的 A（Python）與 B（Rust）各 1 passed。
- DF-23 的 `scope: both`／`diff`／`python.min_version 0.4.0` 與 DF-20／21 的既有慣例一致，catalog 檢查 OK。

**觸及的測試**

- 本輪觸及的輕量測試：`test_resource_report`、`test_p7_od56_ci_allow`、`test_contracts_catalog`、`test_p7_ab_report`、`test_p7_gate_report`、`test_p7_gate_win_argv`，以及 s23 結構與案例清單兩支，共 198 passed。
- 四支新回歸疊到 de7a95a 樹上：4 failed。

**其他檢查**

- `tools.contracts check`：ok。
- `check_cfg_boundary`：無輸出，與 gate 紀錄的空 log 相符。
- `bash -n p7_gate.sh`：通過。
- `git diff --check de7a95a b85eec5`：乾淨。
- 證據檔 `p7-gate-261029.md` 的 `l1_lock_sha256` 等於 de7a95a 鎖的 sha256。
- policy_check：24 pass／1 fail／2 warn，與前輪相同。fail 為 R-16（CLI help 欄寬），與本範圍無關。

**未執行**

- 完整全套 pytest：4 核環境跑 25 分鐘只到 9%，已中止。另外本機 system `python3` 為 3.13，contract_blackbox 的 Python 基線 golden 方言要求 3.12.3，`test_bb_cli_help.py` 417 筆會在 setup 出錯，屬環境差異。
- 全 workspace cargo test、原生 Windows、`p7_gate.sh` 全流程。

審查 worktree 未修改：HEAD 為 `b85eec5`，工作樹乾淨。突變實驗只在 worktree 外的 `git archive` 副本中進行。

結論：需修正
