> 契約獨立審閱（release-0.4.0-plan §8）。範圍 ef63f21c..038d8eba 的 contracts、比較器、哨兵；審查者 agy gemini-3.8-flash-high；只讀、未跑測試。

## A 範圍與檢查方法

1. **檢閱範圍（`ef63f21c..038d8eba`）**：
   - `contracts/`：`behavior_changes.yaml`、`error_codes.yaml`、`cli_schema.json`、`rpc_schema.json`、`release_manifest.schema.json`、`wal_meta_events.schema.json`、`daemon_logging.yaml`、`contracts/golden/pyre.tsv`、`contracts/cli_help/`（Linux、Windows、rust_only golden）。
   - **比較器與判定**：`scripts/rust/stage1a_gate.py`、`scripts/rust/l15_exit_driver.py`、`scripts/rust/stage1_exit_driver.sh`、`scripts/rust/python_baseline_defects.py`、`tests/acceptance/`（`cases.lock.json`、`cases.counts.json`、`cases.yaml`、`l1_required.lock.json`、`p4_required.lock.json`、`python_baseline_defects.json`、`regen_lock.py`）、`tools/contracts/`（確認無變更）。
   - **哨兵與守門**：`tests/contract_blackbox/`（`bb_harness.py`、`bb_l15.py`、`conftest.py`、`test_bb_harness_selftest.py`、`test_bb_l15_wire_payloads.py`、`test_cli_mgmt_doctor.py`、`test_cli_mgmt_supervision_switch.py`）、`scripts/docker/upgrade_checks.py`、`.github/workflows/release.yml`（bundle smoke 與測試符號守門）。

2. **檢查方法**：
   - 嚴格遵守**只讀**限制，未修改檔案、未 commit、未執行 pytest、cargo test、daemon 或 Docker 命令。
   - 使用 `git diff`、`git log`、`git show` 逐檔比對所有目標路徑變更。
   - 使用 Python 靜態腳本解析 `cases.lock.json`、`l1_required.lock.json`、`p4_required.lock.json`，對比版本間的鍵值、集合名單、案例屬性與凍結錯誤碼，驗證集合差集是否嚴格為空或符合 add-only。
   - 審查 OD-75 基線缺陷機制在 gate、exit driver 及 producer 中的判定、對帳與重判語意，核對 contracts 變更與 `main` 合入 commit 紀錄，確認哨兵斷言邏輯與例外處理。

---

## B 發現

NO FINDINGS

---

## C 已確認無問題的項目

### 1. 凍結鎖嚴格維持 add-only，無私自移除、改名或改判
- **`tests/acceptance/cases.lock.json` 與 `cases.yaml`**：
  - 以 Python 靜態比對 `ef63f21c` 與 `038d8eba` 的 `case_attrs` 與各集合清單：**移除案例數為 0，既有案例屬性修改數為 0**。
  - 新增案例共 2 項：`f9-expect-reboot-chained` 與 `f9-reboot-stale-ready`（屬於 L2 regression，均源自 `main` 合入之 PR #242 與 PR #243，提交 `185dad64`）。
  - `revision` 由 `2026-10-21.1` 遞增為 `2026-11-03.1`；`cases.counts.json` 精確同步（總數 154 -> 156，regression 42 -> 44，L2 43 -> 45，slice 2.3 27 -> 29）。
- **`tests/acceptance/l1_required.lock.json`**：
  - 既有 8 個集合（`s0_bc`、`s0_win_pytest`、`s1_pytest_full`、`s1a_blackbox`、`s1a_cargo`、`s1a_delegation`、`s1b_20groups`、`s1b_cargo`）跨平台項目的差集比對結果：**移除 0 項、修改 0 項、變更 0 項**。
  - 僅新增 2 個獨立集合：`s1m241_cargo` 與 `s1m241_pytest`（提交 `ca19dd18` 與 `b70bb35f`），用於 S1M-241 指紋平價與 D3/D4 回歸鎖定，符合 add-only 規範。
- **`tests/acceptance/p4_required.lock.json`**：
  - 案例清單：移除 0 項、新增 0 項、修改 0 項。
  - `error_codes_at_freeze`：移除 0 項；新增 3 項：`EXPECT_REBOOT_MODE_UNSUPPORTED`、`INVALID_EXPECT_REBOOT`（PR #242）、`SOURCE_RESERVED`（PR #240）。
  - `amended_commits` 依流程追加提交 `654c6b263aeffc19cca1cb0f7e087e3fcc64dc8b`。
- **`tests/acceptance/regen_lock.py`**：
  - 新增 `cmd_p4_error_codes_add_only`（[regen_lock.py:247-313](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/tests/acceptance/regen_lock.py#L247-L313)），計算 `removed = sorted(frozen_set - current_set)` 與 `unapproved = sorted(set(added) - approved)`；一旦有移除、未核准新增或未匹配核准，立即輸出 `REFUSED` 退出。命令列明確拒絕 `--allow-removal` 與錯誤碼鎖併用。

### 2. gate／比較器未放寬判定，完全符合 OD-75 規範
- **`scripts/rust/python_baseline_defects.py`**：
  - 嚴格實作 OD-75：要求 schema `python-baseline-defects.v1`、defect_id 必須符合 `B[1-9][0-9]*` 且存在於清單 B 段（`## B.` 表格）。
  - 嚴格驗證平台（`linux`/`windows`）、狀態、正則表達式、根因，且 `od75_basis` 必須包含 `OD-75` 字串。
  - 防偽防線：`_harness_stop_failed()`（[python_baseline_defects.py:94-98](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/scripts/rust/python_baseline_defects.py#L94-L98)）明確排除 `harness_stop_failed`，測試框架或 harness 故障絕不可判定為基線缺陷。
- **`scripts/rust/stage1a_gate.py`**：
  - 依 OD-75 規定，符合登記的失敗列**維持原判 FAIL**（`verdict` 與案例狀態不改寫為 PASS、SKIP 或 BLOCKED-OWNER），僅歸類於 `baseline_known_defects`（「基線已知缺陷（不擋）」）使整體出口不被阻擋（[stage1a_gate.py:575-626, 1902-1921](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/scripts/rust/stage1a_gate.py#L575-L626)）。
  - 未登記的失敗、只有單邊失敗的 A/B 案例、訊息 regex 不符者一律維持阻擋（overall FAIL）。
  - A/B 兩輪皆失敗但未登記者列為 `baseline_pending_classification`（「疑似基線問題，待分類」）並強制整體 FAIL（[stage1a_gate.py:657-674](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/scripts/rust/stage1a_gate.py#L657-L674)）。
  - `_l1_baseline_rows` 明確阻絕 Cargo 測試（`if subfolder == "cargo": return None`），且 JUnit 若有 provenance 異常亦一律不予豁免。
- **`scripts/rust/l15_exit_driver.py` 與 `scripts/rust/stage1_exit_driver.sh`**：
  - 第二層 producer 守門（[l15_exit_driver.py:284-365](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/scripts/rust/l15_exit_driver.py#L284-L365)）：對 rc=1 的 producer（pytest、A/B、L1.5），必須其原始報告（JUnit XML、resource-report.json、ab-report.json）內所有失敗項目均逐項與 gate 核准的已知缺陷對帳無誤，才計入 `suppressed`；任何未解釋失敗留置 `unresolved`，整體判定 `failed_steps > 0` 且 exit 非 0。
  - 修正空輸出誤判（提交 `2ae832c8`）：`stage1_exit_driver.sh` 引入 `parse_failed_steps`，避免 driver 拋例外或產生空字串時被 bash `[[ -eq 0 ]]` 誤判放行。
  - 保存 Stage 1B 原始報告（提交 `754f80f7`）：`snapshot_p4_evidence` 保存到 `$EV/p4`，遇目錄已存在或符號連結立即拒絕，防止沿用或混入舊資料。
  - `--rejudge` 唯讀重判：綁定受測 revision（`--expected-head`）且輸出至暫存檔，不竄改原始證據目錄檔案。
- **`scripts/docker/upgrade_checks.py`**：
  - 新增 `cli_version_identifies()`（[upgrade_checks.py:439-456](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/scripts/docker/upgrade_checks.py#L439-L456)）：嚴格以正則比對 `serialwrap <version>` 與 `serialwrap <version> (code <12 位小寫十六進位>)`，字元長度、空白數與括號格式任一不符皆判定為非該版本，無放寬情事。

### 3. contracts 變更有權威裁決與 Python 基線合入依據，無改 golden 掩蓋失敗
- **`contracts/behavior_changes.yaml`（rev 7 -> 8）**：
  - DF-02 更新引用依據為 OD-74（2026-11-03），明確規範 bare human 正規化為 `human:cli` 走 human 行為，明寫 `human:<x>` 依 DF-02 走 agent 行為，對齊安全模型。
  - 安全不變量 `zero_tx_on_reject` 補列新錯誤碼 `EXPECT_REBOOT_MODE_UNSUPPORTED`、`INVALID_EXPECT_REBOOT`、`SOURCE_RESERVED`。
- **`contracts/error_codes.yaml`（rev 1 -> 2）**：
  - 新增錯誤碼均具備完整規格與 contract_tests：`EXPECT_REBOOT_MODE_UNSUPPORTED` 與 `INVALID_EXPECT_REBOOT` 來自 PR #242（`expect-reboot`）；`SOURCE_RESERVED` 來自 PR #240（`source-prefix-validation`）。
- **`contracts/cli_schema.json`（rev 6 -> 10）與 `contracts/rpc_schema.json`（rev 5 -> 7）**：
  - 對齊 `main` 合入之新功能：`command.submit` 支援 `expect_reboot` 參數（PR #242）、`setup` 支援 `--no-restart` 旗標（PR #241）、`daemon.status` 提供 `code_fingerprint` 與 `code_path`（PR #241）、各命令補上 `SOURCE_RESERVED` 錯誤對應（PR #240）。
  - 提交 `1e9e4164` 補正漏計之 `x-catalog-revision`（9 -> 10），並有 `tests/test_contracts_cli_schema.py` 嚴格斷言鎖定。
- **`contracts/release_manifest.schema.json`（rev 2 -> 3）與 `contracts/wal_meta_events.schema.json`（rev 5 -> 6）**：
  - 分別新增 `code_fingerprint` 格式（PR #241）與 `command_expect_reboot` META 事件定義（PR #242）。
- **`contracts/golden/pyre.tsv`**：
  - 提交 `8059b178` 補齊 CPython 3.12.3 `re.error` 在不完整十六進位跳脫（`\x4`、`\u12`）時的錯誤文字與位置，屬於新增語料向量，並未修改既有金標。
- **`contracts/cli_help/` 各項 Linux/Windows/rust_only golden**：
  - 均由 Python 3.12 實測擷取產出，完全對齊 `main` 合入之 PR #240、PR #241、PR #242 所帶來的 `--version`、`--expect-reboot`、`--no-restart` 及 source 說明變更。`.project-policy.yml` 亦藉提交 `f59ce805` 明確鎖定直譯器為 `python3.12`，消除環境差異。

### 4. 哨兵與測試防線未被弱化，且有多處關鍵強化
- **`tests/contract_blackbox/conftest.py` 宿主環境哨兵強化**：
  - 提交 `78cbc7a3` 建立黑箱 user unit 防線（[conftest.py:92-108, 265-283](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/tests/contract_blackbox/conftest.py#L92-L108)）：於 sessionstart 唯讀記錄 `~/.config/systemd/user/serialwrap.service` 的 mtime 與 SHA256 摘要，sessionfinish 再次比對。若 Rust 測試污染或改動宿主單元檔，一律將 session 強制判為 `TESTS_FAILED`。
  - `test_bb_harness_selftest.py`（[test_bb_harness_selftest.py:128-259](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/tests/contract_blackbox/test_bb_harness_selftest.py#L128-L259)）補齊 6 種突變注入自測，確保哨兵具備攔截力。
- **`tests/contract_blackbox/bb_l15.py` NDJSON 快照競爭修復**：
  - 提交 `c84bb999` 等提交實作 `read_closed_ndjson_snapshot()`（[bb_l15.py:509-540](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/tests/contract_blackbox/bb_l15.py#L509-L540)）：重讀活躍檔案直至 LF 結尾，遇到殘段持續變動且超時未封閉時拋出 `AssertionError: ... 證據不足`，收緊測試條件，不吞例外。
  - `test_bb_l15_wire_payloads.py` 新增 269 行單元測試驗證此防線。
- **`tests/contract_blackbox/test_cli_mgmt_supervision_switch.py` 隔離斷言強化**：
  - 新增嚴格斷言：user unit 必須落在測試隔離的 `$HOME`，絕不可落在 `$XDG_CONFIG_HOME` 或真實使用者目錄，且交接被拒絕時強制斷言單元檔不得建立、`daemon.stop` 不得被送出。
- **`.github/workflows/release.yml` 符號守門強化**：
  - 在發佈 bundle 的 smoke 檢查中，除了既有的 `SW_TEST_UPDATE_*`，額外加入 `SERIALWRAP_TEST_` 標記檢查（[release.yml:266-273](file://~/prj_pri/serialwrap-worktrees/solrev-s1-contract/.github/workflows/release.yml#L266-L273)），嚴禁含有測試注入鉤子的 binary 流入正式候選。

結論：可放行
