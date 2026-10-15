# P9：Stage 3 前置（A/B 執行器、Canary 帳本、回滾演練、退役切換）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 Stage 3 所需的全部「可執行程序、腳本、gate 判定器與證據 schema」：L1.5／L2／L3 的 Sequential A/B 執行器（凍結 manifest、乾淨 DUT 狀態、A→B 之間的資源釋放驗證）、Canary 帳本工具（活躍日、`cmd submit` 計數與去重、干擾／交接分類、每平台 14 活躍日／5,000 次提交／50 次循環配額、含非預期重啟的事故追蹤）、實機回滾演練程序、packaging 中的 Python daemon 退役切換（保留 Python 測試工具）、Stage 0～3 出口證據索引、OD-09 延後凍結程序。證據本身由擁有者以硬體與時間產出（BLOCKED-OWNER），本計畫不得將其標為 PASS。

**Architecture:** 新增 stdlib 為主的測試工具套件 `tools/stage3/`（以 `python3 -m tools.stage3 <子命令>` 執行），比照 `realhw/`：不 import `sw_core`、不連結任何 Rust crate，只經部署 artifact 的 CLI subprocess＋JSON、WAL 檔與 OS 程序資訊觀測；既有 `func-test`、TestPilot `serialwrap_regression`、`realhw` 引擎保留為實際執行者，`tools/stage3` 只做外層編排、判定 envelope 與帳本（規格 §3.2、§11.4「不建立另一個排程、重試或報告引擎」）。退役切換跨 `packaging/`、`scripts/rust/mkbundle.sh`、`contracts/release_manifest.schema.json`、`crates/sw-proto`、`crates/sw-cli`、`crates/sw-daemon` 與 `.github/workflows/release.yml`。

**Tech Stack:** Python 3.10+（`tools/stage3` 的 Canary collector／harvest／policy／ledger 限 stdlib，以便在 Windows 測試工作站以 zipapp 執行；A/B、OD-09、evidence 工具允許 PyYAML）、pytest、jsonschema（只在測試側）、Rust 1.96（edition 2021；僅 Task 16）、bash（`mkbundle.sh`）、GitHub Actions YAML。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §0.3、§9.5、§11.4、§11.5、§11.6、§12.1、§12.2「Stage 3」、§14.2、§14.3、§15.2、§15.9、§15.11、§15.12；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-08、OD-09、OD-10、OD-12、OD-13；`docs/rust-refactor/plans/00-master-plan.md` §2、§3；`docs/rust-refactor/plans/rust-architecture.md` §9.2、§9.3、§10；`docs/rust-refactor/plans/spec-reconciliation.md` SC-23、SC-25、DF-03、DF-05、TST-D-1、TST-D-2、TST-D-10、TST-D-12；盤點 `inventory/08-tests-assets.md` §0、§3、§4、§7，`inventory/07-platform-mgmt.md` §5、§6、§10，`inventory/04-wal-state.md` §1.3～§1.9，`inventory/10-gaps.md` G-04、G-05。

---

## Global Constraints

- repo 政策：不得 commit 到 `main`；每個 code PR 附 `changelog.d/` direct-child fragment；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 黑箱邊界：`tools/stage3/**` 與 `tests/test_stage3_*.py` 不得 import `sw_core`，不得連結 `sw-core`／`sw-pal`，不得用產品內部邏輯計算預期結果（規格 §11.3、§15.4.2）。允許讀 `contracts/` 的語言無關資料。
- CLI JSON 輸出：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`；持久化檔（帳本、證據索引、報告）一律 `sort_keys=True`。
- `--retries` 只對 11 個唯讀方法重試；`session.interactive_status` 絕不重試。Canary collector 與演練觀測**不得**呼叫 `session interactive-status`（會 touch Lease，規格 §10.2、§15.11.3）。
- 長跑門檻唯一來源 `realhw/acceptance/longrun_acceptance.yaml`；OD-09 三項已凍結（錯誤率 ≤1% 且不高於基線、每板每來源吞吐 ≥ 基線 95%、`stuck_attached` 總時長與最長區段不高於基線），RSS／資源預算、最低有效負載與樣本、比較容許誤差延後凍結：於 Stage 0 修正後 Python 48h 基線完成後、任何 Rust 候選長跑開始前凍結。不得看完候選結果再改門檻；安全不變量（未授權 TX、雙重持有、跨上下文回放、危險自動重送）為零且不可豁免（§14.2、§15.11.4）。
- 既有引擎 FAIL 判準保留為 `engine_result`，發版判定以 `release_gate_result` 表達；不得改寫原始結果（OD-09）。
- Canary（OD-10）：Linux、Windows **各** 14 個活躍測試日、≥5,000 次 `cmd submit`、≥50 次適用干擾／交接循環，以真實使用累計（Windows＝測試團隊工作站、Linux＝擁有者日常使用），不佔 bench；不同平台不得合併；循環類型配額須預先定義；非預期重啟即使被 supervisor 修復仍計事故；Windows 不要求 `/dev/ttyMCU`。
- 48h A/B 長跑在擁有者 bench 上 Linux、Windows **依序**執行（同機同板經 usbipd，不可並行）；`lr-mixed` 故障保留模式不得被外部監管自動重啟抵銷（§15.11.2）。
- 退役範圍只限產品執行期 Python daemon；Python 測試、bridge、DUT 模擬器、TestPilot 與 `realhw` 引擎保留（§12.2 Stage 3 末段）。
- 測試工具原因碼一律 `HARNESS_*`，屬測試工具契約，不得混入 `contracts/error_codes.yaml`（§11.4）。
- 證據不足、擷取失敗、`unknown`、未執行均不得轉為 PASS（§11.3、§11.4）。

## Review Focus

1. **A→B 釋放判定**：任何 `unknown` 或逾時一律不是 released；A 未確認釋放前絕不啟動 B，且 B 使用全新實例目錄；每側執行後以 suite 自身證據核對「實際受測 artifact＝該側 manifest」（`HARNESS_ARTIFACT_MISMATCH`），不得兩側都跑到預設 Python 基線（§15.9.1 第 3 步、§11.4）。
2. **門檻凍結時序**：OD-09 延後項 freeze 必須早於任何 Rust 候選長跑（含 Stage 1B 的 `S1B-L3-MIXED-48H-WINDOWS`）；Canary policy 以 `canary policy freeze` 凍結且早於第一個計數紀錄，`tally`／`gate` 逐筆核對 `policy_ref.policy_sha256`；兩者都以程式拒絕事後凍結或事後改 policy（§12.2、§14.2）。
3. **Canary 計數保守**：只計 `^[0-9a-f]{32}$` 的 arbiter `cmd_id`（排除 `ft-*` 檔案傳輸分塊）、`cmd_id` 去重、跨 rotate／reset 不重算、結果不明不計、artifact 歸屬須經 CLI 與 daemon 執行檔 hash 雙重核實，模糊即不計；不同 artifact／平台不自動合併；循環類型有單一類型上限，不得全由 Recovery 湊滿。
4. **事故不可消失**：PID 變更（含 supervisor 自動恢復、中間夾著失聯快照）在無計畫維護紀錄或可佐證的主機重開時一律開事故；collector 觀測缺口、WAL 擷取異常、自動偵測到的安全訊號自成事故；未結案即阻擋 gate。
5. **證據不可由人工宣稱**：PASS 只能由 `evidence ingest` 從產生工具的報告推導並複製、逐檔 hash；`evidence_root` 必填；target 以 (stage, platform) 分別指定，並同時凍結 `tool_revision`、`case_list_sha256`、`runtime_digest`。
6. **退役切換不可提前生效、也不可重建受測物**：Stage 3 bundle 以靜態 `retirement` 標記（無核准欄位）建置與受測；核准只寫在 `packaging/python_daemon_retirement.json`；發佈時 `retirement verify --mode release --bundle-manifest` 要求所建 manifest 的 sha256 等於該平台 Stage 3 target，且直接發佈受測 bundle 本身；回滾演練以真實串口釋放／重新取得（舊程序退出、新程序 READY 時間晚於演練開始、裝置綁定不變、smoke 命令）以及「啟動新版失敗自動回到完整 Previous」判定，而非版本字串或跨重啟的 `bridge_generation`。

---

## 0. Consumes：前置 Phase 交付物（本計畫以此為介面；Task 1 的 `test_stage3_consumes.py` 守住）

| 來源 Phase | 交付物 | 本計畫使用方式 |
|---|---|---|
| P0 | `contracts/error_codes.yaml`；`contracts/behavior_changes.yaml`（BC／SC／DF，每項 `scope` ∈ P0 `CHANGE_SCOPES`（`both`、`rust_only`、`python_when_manifest_present`——SC-25 經 P1 T18 改為後者）、`applies_to.{python,rust}`、`case_ids`、`ab_participation`（P0 01 T1 必填，`scope == "both"` ⇔ `diff`）） | 原因碼不撞名檢查；A/B 差異歸屬：本計畫直接讀 P0 的 `ab_participation`（並核對與 `scope` 一致）——`diff`（必須在 A/B 差分中出現且已登記）、`rust_only`→`absolute_only`（不參與差分，只驗 B 側絕對契約，arch §0 第 2 點） |
| P0／P2／P3 | `contracts/release_manifest.schema.json`、`release-manifest.json`（arch §10.1 欄位：`bundle_version`、`stage`、`target`、`git_revision`、`contract_revision`、`components.{cli,daemon,python_cli_delegate,assets,supervisor}`、`pairing`；Rust daemon 形狀為 `components.daemon{implementation:"rust",version,git_revision,launcher:{path,sha256}}`（同一個 `bin/serialwrapd` 依 implementation 走原生，P5 `main.rs`），**沒有** `daemon.path`；Python 部署碼以 `daemon.package.tree_sha256`、`python_cli_delegate.tree_sha256` 表示） | artifact 身分（`artifact_id = sha256(release-manifest.json bytes)`，每平台各一）、`runtime_digest`（Task 14）、A/B 側別檢查；Task 16 新增靜態 `retirement` 標記 |
| P2 | `realhw/acceptance/longrun_acceptance.yaml`：頂層 `schema_version`、`revision`、`workload.{engine_case,engine_files,min_continuous_duration_s,allow_segment_stitching,snapshot_interval_s,observer_interval_s,max_observation_gap_factor}`、`counting`、`thresholds.{unexpected_cmd_error_rate,effective_throughput,stuck_attached}`（`status: frozen`）、`thresholds.{resources,min_effective_load,comparison_uncertainty}`（key 層級 `status: deferred`；數值只在 `per_platform.{linux,windows}` 之下：`resources`→`rss_peak_kb`／`rss_steady_kb`／`rss_slope_kb_per_h`，`min_effective_load`→`min_completed_per_board_source`（`{"<com>\|<source>": int}`）／`max_idle_gap_s`，`comparison_uncertainty`→`error_rate_abs_tolerance`／`throughput_ratio_tolerance`／`stuck_s_tolerance`／`max_reruns`，初值 null；P2 Task 15「延後項讀寫契約」）、`safety`（含 `allowed_tx_source_regex`）、`liveness`、`evidence`；schema `longrun_acceptance.schema.json`；gate 已定義 `status: frozen` 時的判定（P2 Task 15 checks 表） | OD-09 freeze 只寫三個延後項的 `per_platform.<os>.<field>`（兩平台一次）與凍結紀錄欄位並遞增 `revision`，不改名、不另設子樹、不改 gate 判定；A/B L3 前置檢查；時長與缺口係數只從 `workload.*` 讀，不在本計畫重複數值（§15.11.4、P2 `test_single_source_of_thresholds`） |
| P2 | `realhw/acceptance/gate.py`：`GateInputs(report_dir, case_dir, acceptance_path, platform, mode, observer_path, wal_dir, baseline_gate_path, excl_evidence_path=None)`（`excl_evidence_path`：Windows 原生 COM 獨占證據目錄 `excl/`，Windows 必要，R1-F7）、`evaluate(inputs) -> dict`、`load_acceptance(path) -> tuple[dict, dict]`（內容、`{"path","revision","sha256","git_blob_sha1"}`）、`RESULTS = ("PASS","FAIL","INSUFFICIENT_EVIDENCE","BLOCKED","NOT_APPLICABLE_BASELINE")`；CLI `python3 -m realhw.acceptance.gate --report-dir D --case-dir C --acceptance A --platform P --mode baseline\|candidate [--observer O] [--wal-dir W] [--baseline-gate B] [--excl-evidence E] --out release-gate.json`（exit：PASS／NOT_APPLICABLE_BASELINE 0、FAIL 1、INSUFFICIENT 4、BLOCKED 5）；輸出 `release-gate.v1`：`engine_result` 為 lr-mixed 結果 **dict** 原樣（`verdict` 等鍵）、`release_gate_result`、`baseline_result`（只在 baseline 模式）、`acceptance.revision`、`run.cli_identity`、`checks`；`baseline_compatible` 比對基線 gate 的 `platform`、`run.engine_files_sha256`、`acceptance.workload_contract_sha256` 與基線 `baseline_result=="PASS"`，**不**要求 `acceptance.revision` 相同（P2 Task 15 checks 表；OD-09 於基線後以新 revision 凍結延後項屬正常流程）；observer CLI `python3 -m realhw.acceptance.observer`；`walscan.scan_wal` | L3 A/B 判定（Task 5B、7）；L3 gate 報告 ingest（Task 14B） |
| P2 | `tests/acceptance/cases.yaml`（`invariant_ids` 六項、每案 `case_id`、`suite`、`layer`、`platforms`、`invariants`、`necessity`、`destructive`、`expected: PASS\|XFAIL`、`sentinel: null\|{issue,signature,expires}`、`stage0_required`、`cli_stage`、`daemon_slice`）、`tests/acceptance/cases.lock.json`（`sets.{stage0,cli_1A,cli_1B,slice_2.1,…,slice_2.4}`） | 必要案例集合、XFAIL 核准來源、六項不變量案例完整性（Task 3、7、7B、14B）；`f4-background-result-tail-consistent` 依 TST-D-6 目前為 `expected: PASS`，本計畫不另列手寫 XFAIL 清單 |
| P2 | L1.5：`python3 func-test/runner.py --mode bridge --dut-mode local-process\|docker --report-dir DIR --selftest-report PATH [--scenarios]`，以 env `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`（arch §9.2）選 artifact；**runner 每個 case 自行啟停 daemon 與 bridge 拉起的 DUT**，證據目錄 `<report-dir>/<case_id>/<mode>/<run_index>/`：`manifest.json`（`l15-manifest.v1`：`transport.mode`、`artifacts.{cli.sha256,daemon.implementation,daemon.sha256,daemon.tree_sha256,release_manifest_sha256}`、`tool.tree_sha256`、`dut.{mode,image_id}`）、`comparison.json`（`comparison.v1`：`verdict`（含 `INSUFFICIENT_EVIDENCE`）、`streams[]`、`invariants[]`）、`bridge-manifest.json`（`closed_cleanly`、`failure`）、`wire.ndjson`（`dut_generation`）、`cli.ndjson`、`daemon.log`、`wal/` | L1.5 suite adapter（Task 5B）與 A/B 差分（Task 7B）；**P2 沒有獨立的模擬器 launch／stop／state-digest CLI，也沒有 A/B 比較器**——P2 的比較器是單次 run 的 WAL↔wire 工具（`comparison.v1`），A/B 編排與差分由本計畫的 `tools/stage3/ab_diff.py`（Task 7B）提供（P2 第 29 行） |
| P2 | L2：`testpilot run serialwrap_regression`（testbed `serialwrap_exe`、`bench_lock_path`；報告目錄由引擎自選 `~/b-log/regression-reports/tp-<ts>/report.json`，不接受外部 report dir，盤點 08 §3.1）；L3：`python3 -m realhw --tier <t> --report-dir DIR`（`realhw/config.json` 的 `serialwrap_exe`、#154 版本 gate TST-D-10；`report.json.meta.cli_identity.{resolved,sha256,pinned}`、`meta.started_at_utc`；`lr-mixed` PID 取 `daemon status.pid`，TST-D-12） | L2／L3 suite adapter（Task 5B）指向各側 artifact 並核對實際受測身分 |
| P0／P2 | 測試檔平台／資源分類＝P0 01 Task 14 的**檔內** `pytestmark`（恰一個平台 marker `core_portable`／`posix_only`／`windows_native`＋至少一個資源 marker `memory_only`、`process_rpc`、`posix_pty`、`windows_com`、`physical_uart`、`supervisor_runtime`、`interactive_desktop`；同檔混合時放 class／函式層），由 `tests/test_resource_markers.py` 稽核；P2 03 Task 13 **不建立** `tests/platform_resources.yaml`（只產 `SW_RESOURCE_REPORT` 與 Windows `pytest_ignore_collect`）。`tests/acceptance/*.schema.json` 必含 `"$schema": "https://json-schema.org/draft/2020-12/schema"` 與 `$id`（`https://serialwrap.local/schemas/<name>`） | 每個 Task 新增測試檔時在檔頭寫對應 `pytestmark`；下文各 Task 的「Marker」行即該檔應帶的 class／resources（跨計畫審查：原稿寫成登錄 `tests/platform_resources.yaml`，該檔不存在），平台不同的測試拆成不同檔 |
| P4 | `serialwrap setup`（從 bundle 安裝／啟用；版本配對、health、smoke 任一失敗自動回到完整 Previous，P4 Review Focus 5）、`serialwrap setup --rollback`（SC-23 七步流程）、`doctor` 的 `checks: [{"check","ok","detail","fix"}]` 中 `check=="version_pairing"` 一列（SC-25）、`service status`、安裝佈局（arch §10.2：Linux `~/.local/bin/serialwrap` symlink 到 `versions/<v>/bin/*`；Windows `bin\current-release.json`） | 回滾演練、A/B L2/L3 的 `activate_argv` |
| P3／P4 | `scripts/rust/mkbundle.sh`：P3 旗標 `--dev --out <dir> [--target <triple>] [--profile debug\|release]`；P4 加 `--delegate-sentinel`、`--print-dir`、`--version-override`（限 `--dev`）；**目前沒有 `--stage`、`--plan-only`**（arch §10.1 文字提到 `--stage`，但 P3／P4 未實作） | Task 16 新增 `--stage`、`--plan-only` 並更新 arch §10.1 |
| P3／P4 | `crates/sw-proto/src/release_manifest.rs`：P3 自由函式 `parse_manifest(&str) -> Result<ReleaseManifest, ManifestError>`、P4 `ReleaseManifest::parse(&str)`／`ReleaseManifest::locate`；`ReleaseManifest{schema_version,bundle_version,stage,target,git_revision,python_cli_delegate_entry: Vec<String>,raw}`；`ManifestError{InvalidJson(String),Schema(String)}`；P3 parser 在缺 `components.python_cli_delegate.entry` 時回 `Schema`。`crates/sw-cli`：P4 DF-05 `locate_daemon_argv(ctx: &Ctx) -> Result<Vec<String>, DaemonLocateError>`（依序 `components.daemon.launcher.path` → 同目錄 `serialwrapd<suffix>` → `components.daemon.runtime.path` ＋ `-I -m sw_core.daemon` → `DAEMON_BINARY_NOT_FOUND`）；`crates/sw-daemon/src/launcher.rs`（P4，P5 加 `implementation=="rust"` 分支） | Task 16 加入退役守門（`python_cli_delegate_entry` 改為 `Option`、新增 `ManifestError::RetirementInconsistent`） |

若前置 Phase 實際命名與上表不同，**改本計畫的消費端程式與本表**，不得要求前置 Phase 改名以遷就本計畫；修改須經審閱。

**本計畫在 Phase 序列中最後建置**：Stage 1A～2.4 的出口（如 `S1A-L15-DATAPLANE`、`S21-L15-SAFETY`）在各自 Phase 期間以該 Phase 的工具產出證據（P3 的 `scripts/rust/ab_blackbox.sh`／`ab_compare.py`、func-test、TestPilot、`realhw`），**不**依賴 `tools/stage3`。A/B manifest 的 `stage` enum 保留 `1A`～`2.4`，只供 Stage 3 期間重跑或核對舊階段證據；那些舊證據一律經 Task 14B 的 `evidence ingest` 以其原生報告登錄，不得改用手寫 entry。

## 0.1 執行順序（TDD 兩輪）

| 輪次 | 內容 |
|---|---|
| 第一輪（測試先行） | Task 1、Task 2 完整執行（Step 1～5：契約檔、基礎模組與**只含基礎替身**的共用替身是其他測試的前置，必須先能 import 且全綠）；其餘 Task 只執行 Step 1～2（寫測試與該 Task 自己的替身檔 `tests/fakes_*.py`→執行→確認紅燈且紅燈原因是「模組／符號不存在」或斷言失敗，不是語法錯或 fixture 錯）。第一輪不得 commit 任何紅燈測試：紅燈測試只留在工作目錄，於該 Task 第二輪 Step 5 與實作一起 commit |
| 第二輪（實作） | 依相依順序執行各 Task 的 Step 3～5：3 → 4 → 5 → 5B → 7B → 7 → 14 → 15 → 14B → 6 → 8 → 10 → 11 → 9 → 12 → 13 → 16 → 17（Task 10、11 先於 Task 9：collector zipapp 需打包 `canary_classify`、`canary_incidents`，`collector_main` 的 `note`／`incident` 子命令也依賴 Task 11） |
| 收尾 | 全套 `python3 -m pytest -q tests/`、`cargo test --workspace`、`scripts/rust/check_cfg_boundary.py`、`python3 -m policy_check --repo .`；Phase 層級雙審（sonnet max＋codex gpt-6.1-sol medium，master plan §1）；高風險 Task 4、5B、6、7B、8、10、13、14B、16 另各做一次單審（見 §2） |

第一輪結束時須提交一個「全部紅燈清單」紀錄（各測試檔的失敗數與原因摘要）到 PR 描述，供審查者確認測試先於實作存在。

## 0.2 BLOCKED-OWNER 總表（本計畫不得產出、不得標 PASS）

| ID | 項目 | 需要的擁有者資源 | 對應出口項 | 相關 Task |
|---|---|---|---|---|
| BO-P9-01 | Windows L1.5 Sequential A/B（原生 COM：com0com DUT-side pair 或 USB-UART 直連，OD-08；隔離實例設 `SERIALWRAP_TEST_DEVICE_ALLOWLIST`，SC-19／OD-11） | 擁有者 Windows 自架 runner＋COM 裝置 | `S3-L15-RELEASE-AB-WINDOWS` | 4、5B、6 |
| BO-P9-02 | Linux L2 Sequential A/B（`serialwrap_regression` 全部適用案例，含破壞集；manifest `authorization` 列出授權決策與限定板子） | live bench、兩塊板、破壞性授權 | `S3-L2-FULL-AB-LINUX` | 5、5B、6 |
| BO-P9-03 | Windows L2 適用案例 A/B（testbed 明確 bench lock 路徑） | Windows bench | `S3-L2-AB-WINDOWS` | 5、5B、6 |
| BO-P9-04 | Linux L3 48h A/B（Python 基線→Rust 候選，依序；明確 endpoint、無外部監管） | bench 約 4 天 | `S3-L3-AB-48H-LINUX` | 5B、6、7 |
| BO-P9-05 | Windows L3 48h A/B（依序；與 BO-P9-04 合計約 9 天 bench，§12.2） | bench 約 5 天 | `S3-L3-AB-48H-WINDOWS` | 5B、6、7 |
| BO-P9-06 | Canary policy 凍結（`canary policy freeze`）：活躍日認定、提交計數與去重、干擾／交接分類與各類型最低配額／單一類型上限、各平台證據責任人；早於任何計數紀錄 | 擁有者裁決（附加至 `owner-decisions-addenda.md`，不覆寫 OD 表） | 前置於 `S3-CANARY-*` | 1、12 |
| BO-P9-07 | Linux Canary：14 活躍日／5,000 提交／50 循環（擁有者日常使用） | 實際使用時間 | `S3-CANARY-LINUX` | 8～12 |
| BO-P9-08 | Windows Canary：測試團隊**單人使用**工作站真實使用（OD-12：共用機不得部署）；工作站可執行 Python 3.10+（stdlib）以跑 `sw-canary.pyz` collector，或擁有者核准其他部署方式 | 測試團隊工作站與時間 | `S3-CANARY-WINDOWS` | 8～12 |
| BO-P9-09 | 事故分診與結案（含 `unexplained_pid_change`、`collector_gap`、`harvest_integrity`、自動偵測的 `safety_violation`） | 擁有者／證據責任人判斷 | `S3-INCIDENTS-CLOSED` | 11、12 |
| BO-P9-10 | Linux 實機回滾演練（破壞性：會重啟 daemon、重新取得 UART；含 D8 啟動失敗自動回復，需擁有者以 `mkbundle.sh --dev --version-override` 建出 start-fail 來源 bundle） | bench、兩塊板 | `S3-ROLLBACK-LINUX` | 13 |
| BO-P9-11 | Windows 實機回滾演練（`windows-task`；含 D8） | Windows bench | `S3-ROLLBACK-WINDOWS` | 13 |
| BO-P9-12 | OD-09 延後凍結：Linux／Windows 修正後 Python 48h 基線證據＋擁有者核准數值；必須早於任何 Rust 候選長跑（最早的是 Stage 1B 的 `S1B-L3-MIXED-48H-WINDOWS`，因此實務上於 Stage 0 出口完成） | bench 約 5 天＋裁決 | `S0-OD09-DEFERRED-FROZEN`、`S3-OD09-ALL-FROZEN` | 15 |
| BO-P9-13 | 退役裁決：Stage 3 證據 PASS 後將 `packaging/python_daemon_retirement.json` 改為 `retired:true` 並記錄兩平台 target artifact id | 擁有者裁決 | 退役生效 | 16 |
| BO-P9-14 | Stage 0～2.4 所有 `owner_blocked:true` 出口項的證據產出與登錄（含 Spike 2、Spike 3） | 各階段 bench／Windows／時間 | `stage_exits.json` 全部 owner 項 | 14、14B |
| BO-P9-15 | Windows `WindowsProbe` 在真實 COM 上的釋放判定實測（`windows_com`） | COM pair | `S3-L15-RELEASE-AB-WINDOWS` 前置 | 4 |
| BO-P9-16 | A→B 之間 bench 獨占保留（跨 TestPilot 套件／跨主機的 bench 排程） | 擁有者排程 | L2／L3 A/B 有效性 | 6 |
| BO-P9-17 | Windows 原生 build 的 Stage 3 release bundle（arch §1.1：正式 artifact 在 Windows 自架 runner 原生 build/link），以及 release workflow 中自架 runner 的 bundle job | Windows runner | 退役 bundle | 16 |
| BO-P9-18 | Release candidate 的 L3 完整穩定性集合（P0＋P1＋remote，§11.5），Linux 與 Windows 各一 | bench | `S3-L3-P0P1-REMOTE-LINUX`、`S3-L3-P0P1-REMOTE-WINDOWS` | 14B |

---

## Task 1：契約檔、schema 與基礎模組

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-01-stage3-contracts.md`
**BLOCKED-OWNER:** BO-P9-06（`canary_policy.json` 只交付 `status: "draft"` 預設值；凍結由擁有者執行）。

**Files:**
- Create: `tools/__init__.py`（空檔，讓 `tools` 成為一般套件，避免被 site-packages 同名套件遮蔽）
- Create: `tools/stage3/__init__.py`、`tools/stage3/jsonio.py`、`tools/stage3/proc.py`、`tools/stage3/reason_codes.py`、`tools/stage3/canary_policy.py`
- Create: `tools/stage3/stage3_reason_codes.json`（套件資料，以 `importlib.resources` 讀取，zipapp 內同樣可用）
- Create: `tests/acceptance/ab_manifest.schema.json`、`tests/acceptance/examples/ab_manifest.l15.example.json`、`tests/acceptance/examples/ab_manifest.l2.example.json`、`tests/acceptance/examples/ab_manifest.l3.example.json`
- Create: `tests/acceptance/canary_policy.json`、`tests/acceptance/canary_policy.schema.json`
- Create: `tests/acceptance/canary_ledger_record.schema.json`、`tests/acceptance/examples/canary_records.example.ndjson`
- Create: `tests/acceptance/stage_exits.json`、`tests/acceptance/stage3_evidence_index.schema.json`（P2 03 Task 12 已有 `evidence_index.schema.json` 驗 `stage0_evidence_index.yaml`，不得覆寫）
- Create: `docs/rust-refactor/evidence/index.json`（初始 `{"entries":[],"schema_version":2,"stage_targets":{},"target_history":[]}` 加換行）
- Create: `docs/rust-refactor/owner-decisions-addenda.md`（附加式紀錄檔，初始只有表頭 `| decision_id | 主題 | 日期 | 內容 |`；Task 12 的 Canary policy 凍結與 Task 15 的 OD-09 凍結都附加到這裡）
- Create: `packaging/python_daemon_retirement.json`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（登錄下列三個測試檔，皆 `{class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_contracts.py`、`tests/test_stage3_base.py`、`tests/test_stage3_consumes.py`

四個新 schema（`ab_manifest`、`canary_policy`、`canary_ledger_record`、`stage3_evidence_index`）皆為 draft 2020-12，帶 `"$schema": "https://json-schema.org/draft/2020-12/schema"` 與 `"$id": "https://serialwrap.local/schemas/<檔名去掉 .schema.json>"`（P2 `test_all_acceptance_schemas_are_valid_draft2020`）。

**Interfaces — Produces:**

```python
# tools/stage3/jsonio.py
def utc_now_iso() -> str                       # datetime.now(timezone.utc).isoformat(timespec="microseconds")，形如 "2026-11-02T01:00:00.000000+00:00"
def parse_iso(ts: str) -> datetime              # 接受 "+00:00" 與 "Z"；naive 一律拒絕（ValueError）
def dumps_compact(obj: Any) -> str              # ensure_ascii=False, separators=(",",":"), sort_keys=True
def dumps_report(obj: Any) -> str               # ensure_ascii=False, indent=1, sort_keys=True, 尾端換行
def sha256_bytes(b: bytes) -> str
def sha256_file(path: Path) -> str
def append_ndjson(path: Path, obj: dict) -> None   # 以 "a" 開檔、寫 dumps_compact(obj)+"\n"、flush、os.fsync
def read_ndjson(path: Path) -> tuple[list[dict], list[int]]   # (records, malformed_line_numbers)；不丟例外
def atomic_write_text(path: Path, text: str) -> None          # 同目錄 tmp → os.replace
def record_id(obj: dict) -> str                 # sha256(dumps_compact(obj 去掉 "record_id")) 前 20 hex

# tools/stage3/proc.py
@dataclass(frozen=True)
class CmdResult:
    rc: int            # timeout 時為 -1
    stdout: str
    stderr: str
    elapsed_s: float
    timed_out: bool
Runner = Callable[..., CmdResult]   # run(argv: Sequence[str], *, env: Mapping[str,str]|None=None, timeout: float|None=None, cwd: Path|None=None) -> CmdResult
def run_cmd(argv: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None, cwd: Path | None = None) -> CmdResult
def run_json(run: Runner, argv: Sequence[str], **kw) -> tuple[CmdResult, dict | None]   # stdout 不是 JSON object → None
class ProcHandle(Protocol):
    pid: int
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
Spawner = Callable[..., ProcHandle]  # spawn(argv, *, env=None, cwd=None) -> ProcHandle
def spawn(argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> ProcHandle  # subprocess.Popen，stdout/stderr 導向 cwd 下 spawn-<pid>.log

# tools/stage3/reason_codes.py
REASON_CODES_RESOURCE: tuple[str, str] = ("tools.stage3", "stage3_reason_codes.json")
def load_reason_codes() -> dict[str, dict[str, str]]   # importlib.resources.files("tools.stage3").joinpath(...).read_text()；code -> {"meaning","action"}；zipapp 內亦可讀
class HarnessError(Exception):
    def __init__(self, code: str, detail: str = "") -> None   # code 不在登錄表 → ValueError
    code: str
    detail: str

# tools/stage3/canary_policy.py
QUOTA_FLOOR: Mapping[str, int] = {"active_days": 14, "submits": 5000, "cycles_total": 50}   # OD-10，不可下修
CYCLE_TYPES: tuple[str, ...] = ("handoff_release_attach", "hotplug_reattach", "dut_reboot", "recovery", "human_agent_coexist", "flash", "planned_restart")
MIN_TYPES_WITH_QUOTA: int = 3        # §12.2「不能全部以同一種容易的 Recovery 取代」
ARBITER_CMD_ID_REGEX: str = "^[0-9a-f]{32}$"   # sw_core/arbiter.py:166 uuid4().hex；排除 file_transfer 的 "ft-<8hex>-<suffix>"
@dataclass(frozen=True)
class PlatformQuota:
    active_days: int
    submits: int
    cycles_total: int
    cycles_min_per_type: Mapping[str, int]
    cycles_max_type_share: float       # 每種類型計入 cycles_total 的上限比例（0 < x ≤ 0.5）
    evidence_owner: str | None
    utc_offset_minutes: int
    utc_offset_changes: tuple[tuple[str, int], ...]   # [(from_iso, offset_minutes)]，依 from 升冪；DST 或搬遷時由擁有者於凍結前列出
@dataclass(frozen=True)
class CanaryPolicy:
    status: str                       # "draft" | "frozen"
    frozen_by: str | None
    frozen_at: str | None
    decision_ref: str | None
    platforms: Mapping[str, PlatformQuota]   # 鍵恰為 {"linux","windows"}
    active_day_min_distinct_submits: int
    active_day_min_snapshot_coverage_s: int
    exclude_source_prefixes: tuple[str, ...]
    cmd_id_regex: str                 # 必須等於 ARBITER_CMD_ID_REGEX
    result_poll_max_per_cycle: int    # 每次 collect 最多查幾個新 cmd_id 的 `cmd status`
    collector_interval_s: int
    gap_incident_s: int
    stuck_incident_s: int
    coexist_bucket_s: int
    coexist_cap_per_day: int
    episode_max_gap_factor: int
    recover_cluster_s: int
    post_boot_grace_s: int
    unreachable_incident_polls: int
    reboot_corroboration_s: int       # boot 身分變更須有 ≥ 此秒數的 collector 缺口才算主機重開
    allowed_tx_source_regex: str      # 與 longrun_acceptance.yaml safety 段同語意；Canary 為真實使用，值由擁有者凍結
    resend_window_s: int
    human_replay_min_len: int
    sha256: str                       # 檔案 bytes 的 sha256
def offset_at(quota: PlatformQuota, ts: str) -> int       # utc_offset_changes 中 from ≤ ts 的最後一筆，否則 utc_offset_minutes
def local_date(quota: PlatformQuota, ts: str) -> str      # "YYYY-MM-DD"
def load_policy(path: Path, *, addenda_path: Path | None = None) -> CanaryPolicy
    # 違規 → HarnessError("HARNESS_CANARY_POLICY_INVALID", detail)；addenda_path 給定且 status=="frozen" 時，
    # addenda 檔必須有一列以 f"| {decision_ref} |" 開頭，否則同一錯誤碼（detail "decision_ref not in addenda"）
def assert_frozen_before(policy: CanaryPolicy, first_counted_wall_ts: str) -> None   # 未凍結或 frozen_at > ts → HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN")
```

`load_policy` 的拒絕規則：任一平台 `active_days`／`submits`／`cycles_total` 低於 `QUOTA_FLOOR`；`cycles_min_per_type` 含 `CYCLE_TYPES` 以外的鍵；`sum(cycles_min_per_type) > cycles_total`；`cycles_min_per_type` 中值 ≥1 的類型少於 `MIN_TYPES_WITH_QUOTA`；`cycles_max_type_share` 不在 `(0, 0.5]`；任一類型的 `cycles_min_per_type[t] > floor(cycles_max_type_share × cycles_total)`；`windows.cycles_min_per_type.flash > 0`（Windows 不適用 `/dev/ttyMCU`）；`cmd_id_regex != ARBITER_CMD_ID_REGEX`；`allowed_tx_source_regex` 無法編譯；`status == "frozen"` 但 `frozen_by`／`frozen_at`／`decision_ref` 任一為 null；`platforms.*.evidence_owner` 在 `frozen` 時為 null；`utc_offset_minutes` 或任一 `utc_offset_changes` 值不在 `-720..840`、`from` 非升冪或為 naive 時間。

**`tests/acceptance/canary_policy.json`（draft 預設值，擁有者可在凍結前修改，凍結後不得改；凍結只經 Task 12 的 `canary policy freeze`）：**

```json
{
  "active_day": {"min_distinct_submits": 20, "min_snapshot_coverage_s": 3600},
  "collector": {"episode_max_gap_factor": 3, "gap_incident_s": 900, "interval_s": 30, "post_boot_grace_s": 300, "reboot_corroboration_s": 60, "result_poll_max_per_cycle": 50, "unreachable_incident_polls": 3},
  "cycles": {"coexist_bucket_s": 600, "coexist_cap_per_day": 5, "recover_cluster_s": 120},
  "decision_ref": null,
  "frozen_at": null,
  "frozen_by": null,
  "incidents": {"stuck_incident_s": 900},
  "platforms": {
    "linux": {"evidence_owner": null, "quotas": {"active_days": 14, "cycles_max_type_share": 0.4, "cycles_min_per_type": {"dut_reboot": 10, "flash": 0, "handoff_release_attach": 10, "hotplug_reattach": 5, "human_agent_coexist": 10, "planned_restart": 0, "recovery": 5}, "cycles_total": 50, "submits": 5000}, "utc_offset_changes": [], "utc_offset_minutes": 480},
    "windows": {"evidence_owner": null, "quotas": {"active_days": 14, "cycles_max_type_share": 0.4, "cycles_min_per_type": {"dut_reboot": 10, "flash": 0, "handoff_release_attach": 10, "hotplug_reattach": 5, "human_agent_coexist": 10, "planned_restart": 0, "recovery": 5}, "cycles_total": 50, "submits": 5000}, "utc_offset_changes": [], "utc_offset_minutes": 480}
  },
  "safety": {"allowed_tx_source_regex": "^(agent:[A-Za-z0-9._:-]+|human:[A-Za-z0-9._:-]+|system|system:[a-z_]+|flash)$", "human_replay_min_len": 8, "resend_window_s": 3600},
  "schema_version": 1,
  "status": "draft",
  "submit_counting": {"cmd_id_regex": "^[0-9a-f]{32}$", "dedup_key": ["host_id", "cmd_id"], "exclude_source_prefixes": ["human:", "system", "flash"], "require_terminal_result": true, "source": "wal_tx_cmd_id"}
}
```

時區以固定 `utc_offset_minutes` 加上預先列出的 `utc_offset_changes` 表達（Windows 的 stdlib `zoneinfo` 沒有 tzdata，不可依賴）；已知限制：未事先列出的 DST／時區變更會把該日紀錄歸到錯誤的本地日期，`canary.md` 須要求擁有者在凍結前列出 Canary 期間的所有變更日。

**`tools/stage3/stage3_reason_codes.json`**：`{"schema_version":1,"codes":{...}}`，每碼 `{"meaning":"…","action":"…"}`，初始集合恰為：`HARNESS_MANIFEST_INVALID`、`HARNESS_MANIFEST_DRIFT`、`HARNESS_RELEASE_UNCONFIRMED`、`HARNESS_DUT_NOT_CLEAN`、`HARNESS_SUITE_INFRA_ERROR`、`HARNESS_COMPARATOR_ERROR`、`HARNESS_BASELINE_INVALID`、`HARNESS_OD09_NOT_FROZEN`、`HARNESS_FAILURE_RETENTION_UNVERIFIED`、`HARNESS_ABORTED`、`HARNESS_ARTIFACT_MISMATCH`、`HARNESS_CASE_SET_INCOMPLETE`、`HARNESS_AUTHORIZATION_MISSING`、`HARNESS_CANARY_POLICY_INVALID`、`HARNESS_CANARY_POLICY_NOT_FROZEN`、`HARNESS_CANARY_POLICY_CHANGED`、`HARNESS_LEDGER_CORRUPT`、`HARNESS_INCIDENT_INVALID_CLOSE`、`HARNESS_EVIDENCE_UNKNOWN_ITEM`、`HARNESS_EVIDENCE_DUPLICATE`、`HARNESS_EVIDENCE_INGEST_INVALID`、`HARNESS_OD09_BASELINE_INVALID`、`HARNESS_OD09_TOO_LATE`、`HARNESS_DRILL_PRECONDITION`、`HARNESS_DRILL_STEP_FAILED`、`HARNESS_DRILL_INCOMPLETE`、`HARNESS_RETIREMENT_NOT_APPROVED`、`HARNESS_RETIREMENT_INCONSISTENT`（28 碼）。

**`tests/acceptance/ab_manifest.schema.json` 欄位**（draft 2020-12，`additionalProperties:false`）。FrozenFile＝`{"path":str,"sha256":64hex}`；FrozenTree＝`{"path":str,"tree_sha256":64hex}`（演算法同 arch §10.1：sorted `"<relpath>\0<sha256>\n"` 的 sha256，排除 `__pycache__`、`*.pyc`）：

| 欄位 | 型別／約束 |
|---|---|
| `schema_version` | const 1 |
| `run_id` | `^[a-z0-9][a-z0-9._-]{2,63}$` |
| `layer` | enum `l1.5`、`l2`、`l3` |
| `platform` | enum `linux`、`windows` |
| `stage` | enum `1A`、`1B`、`2.1`、`2.2`、`2.3`、`2.4`、`3` |
| `contract_revision` | 40 hex |
| `case_list` | FrozenFile（`tests/acceptance/cases.yaml`） |
| `case_lock` | FrozenFile（`tests/acceptance/cases.lock.json`） |
| `required_case_set` | string，必須是 `cases.lock.json` 的 `sets` 鍵；`stage=="3"` 時必須為 `slice_2.4` |
| `case_ids` | [str]，本 run 執行的案例；Task 3 驗證其涵蓋性 |
| `tools` | (FrozenFile \| FrozenTree) 陣列：至少含 `tools/stage3`（tree）；L1.5 另含 `tools/uart_stdio_bridge`、`func-test`（tree）；L2 含 `regression`（tree）；L3 含 `realhw`（tree） |
| `allowed_differences` | FrozenFile（指向 `contracts/behavior_changes.yaml`） |
| `acceptance` | FrozenFile；`layer=="l3"` 時必填 |
| `profile_files` | FrozenFile 陣列 |
| `seed` | integer ≥0 |
| `platform_settings` | object（原樣記入報告） |
| `dut` | `{"kind": "local-process"\|"docker"\|"physical-substitute"\|"hardware", "transport": "posix-pty"\|"com0com-pair"\|"physical-usb-uart", "com_pair": {"daemon_side":str,"bridge_side":str}\|null, "image": str\|null, "image_digest": str\|null, "boards": [str], "reset_argv": [str], "fingerprint_argv": [str], "expected_fingerprint": {str: regex}}`（`local-process`／`docker` 為 P2 L1.5 模式；`physical-substitute` 為 OD-08 以實體 USB-UART 直連取代 com0com 的 L1.5；`hardware` 為 L2／L3 bench） |
| `authorization` | `{"allow_destructive": bool, "decision_ref": str\|null, "boards": [str]}`；`case_ids` 中任一案例在 `cases.yaml` 為 `destructive: true` 時必填，且 `allow_destructive` 為 true、`decision_ref` 非 null、`boards ⊆ dut.boards`（§11.5） |
| `test_device_allowlist` | [str]；`platform=="windows"` 且 `dut.transport=="com0com-pair"` 時必填且非空（SC-19／OD-11） |
| `sides` | 恰 2 項，`{"label":"A"\|"B","role":"baseline"\|"candidate","bundle_dir":str,"release_manifest":FrozenFile,"cli_exe":str,"daemon_exe":str,"activate_argv":[str],"deactivate_argv":[str]}` |
| `suite` | `{"adapter": "l15_functest"\|"l2_regression"\|"l3_realhw", "timeout_s": number>0, "selftest_report": FrozenFile\|null, "testbed_template": FrozenFile\|null, "realhw_config_template": FrozenFile\|null, "tier": str\|null, "extra_args": [str]}`；adapter 必須與 layer 對應（Task 5B） |
| `endpoint` | str \| null；`layer=="l3"` 時必填（明確 endpoint，SC-18 不自動拉起） |
| `release` | `{"timeout_s":number,"poll_s":number,"endpoints":[str],"device_paths":[str],"lock_paths":[str],"device_open_probe":bool,"accept_unreadable_holders":bool}` |
| `reverify` | `{"enabled":bool,"order":["B","A"]}` |
| `failure_retention` | enum `on-demand`；`layer=="l3"` 時必填 |

**`tests/acceptance/stage_exits.json`**：`{"schema_version":2,"items":[...]}`，每項 `{"item_id","stage","platform":"linux"|"windows"|"both","layer","title","spec_ref","owner_blocked":bool,"candidate_longrun":bool,"evidence_kind":str,"required_case_set":str|null}`。`platform:"both"` 的項目必須兩個平台各自有 SATISFIED 的證據（Task 14）。XFAIL 是否可接受只依 `cases.yaml` 的 `expected: XFAIL`＋`sentinel{issue,signature,expires}`（Task 14），目錄不再手列 `allow_xfail`。`evidence_kind` 依層級固定（Task 14B 的 ingest 種類）：L1→`junit`；L1.5 且 stage≠3→`functest-report`；L2 且 stage≠3→`regression-report`；L3 且 `candidate_longrun:false`、非 A/B→`realhw-report`；`S0-L3-BASELINE-*`、`S1B-L3-MIXED-48H-WINDOWS`、`S24-L3-LRMIXED-48H-*`→`gate-report`；Stage 3 的 L1.5／L2／L3-48H A/B→`ab-report`；canary 與 `S3-INCIDENTS-CLOSED`→`canary-report`；hw→`drill-report`；OD-09 兩項→`od09-freeze`；`S0-L15-BRIDGE`→`bridge-selftest`（P2 bridge 自測報告）；perf／supervisor／spike／manual→`manual-procedure`。`required_case_set` 為 L1.5／L2／L3 項所對應的 `cases.lock.json` 集合名（Stage 0→`stage0`；1A→`cli_1A`；1B→`cli_1B`；2.x→`slice_2.x`；3→`slice_2.4`），其餘為 null。內容恰為下表 67 項（跨計畫審查補 `S0-SPIKE1-WIN-VCOM`、`S21-PERF-WAL`、`S22-PERF-CONSOLE-ECHO`）（L2 以 38 案例為準，TST-D-1）：

| item_id | stage | platform | layer | title | spec_ref | owner_blocked | candidate_longrun |
|---|---|---|---|---|---|---|---|
| S0-L1-BC | 0 | both | L1 | BC-01～05 契約與安全案例 | §11.6 | false | false |
| S0-L1-WIN-PYTEST | 0 | windows | L1 | Windows Python core_portable／windows_native | §11.2 | false | false |
| S0-L15-BRIDGE | 0 | linux | L1.5 | bridge 獨立自測（Spike 4） | §15.4.3 | false | false |
| S0-L15-FUNCTEST | 0 | linux | L1.5 | 13 個 func-test 與外部模式 | §15.5 | false | false |
| S0-WIN-NATIVE-SERIAL | 0 | windows | L1.5 | Windows 原生 serial 路徑（OD-08） | §11.6 | true | false |
| S0-L2-NONDESTRUCTIVE | 0 | linux | L2 | 全部適用非破壞案例 | §11.6 | true | false |
| S0-L2-F12-ORACLE | 0 | linux | L2 | F12 WAL oracle 修訂 | §15.10 | true | false |
| S0-L2-BC02-BC03 | 0 | linux | L2 | BC-02／03 對應案例 | §11.6 | true | false |
| S0-L3-P0P1 | 0 | linux | L3 | P0＋P1 適用集合 | §11.6 | true | false |
| S0-L3-BASELINE-LINUX-48H | 0 | linux | L3 | 修正後 Python 48h 基線 | §15.11.5 | true | false |
| S0-L3-BASELINE-WINDOWS-48H | 0 | windows | L3 | 修正後 Python 48h 基線 | §15.11.5 | true | false |
| S0-OD09-DEFERRED-FROZEN | 0 | both | gate | OD-09 延後項凍結 | §14.2 | true | false |
| S0-SPIKE2-WINTASK-72H | 0 | windows | spike | Spike 2：`windows-task` 登入觸發、唯一實例、失敗重啟、`PT0S`、>72h 實際運行與 on-demand 退路 | §13 Spike 2 | true | false |
| S0-SPIKE1-WIN-VCOM | 0 | windows | spike | Spike 1：com0com 驅動、Secure Boot／HVCI、DUT-side／Console-side pair、原生開埠、控制線隔離（OD-01／OD-08） | §13 Spike 1 | true | false |
| S0-SPIKE3-WIN-WAL-EDR | 0 | windows | spike | Spike 3：EDR 開啟下 WAL 寫入、rotate、unlink、外部 reader、重建 timeout | §13 Spike 3 | true | false |
| S1A-L1-BLACKBOX-LINUX | 1A | linux | L1 | Rust CLI→Python daemon 黑箱 | §11.6 | false | false |
| S1A-L1-BLACKBOX-WINDOWS | 1A | windows | L1 | Rust CLI→Python daemon 黑箱 | §11.6 | false | false |
| S1A-L1-DELEGATION | 1A | both | L1 | 透明委派 | §4.3 | false | false |
| S1A-L15-DATAPLANE | 1A | linux | L1.5 | 資料面 L1.5 | §11.6 | false | false |
| S1A-L2-DATAPLANE | 1A | linux | L2 | 資料面 L2 | §11.6 | true | false |
| S1A-L3-P0P1 | 1A | linux | L3 | Rust CLI 執行 P0／相關 P1 | §11.6 | true | false |
| S1B-L1-20GROUPS | 1B | both | L1 | 20 群組承接 | §11.6 | false | false |
| S1B-L15-INSTALL-ROLLBACK | 1B | linux | L1.5 | 版本閘門、安裝、監管、remote、回滾 | §11.6 | false | false |
| S1B-PERF | 1B | both | perf | 效能指標 | §1.2 | false | false |
| S1B-WIN-TASK-SUPERVISE | 1B | windows | supervisor | `windows-task` 監管 | §6.1 | true | false |
| S1B-L2-FULL | 1B | linux | L2 | Rust CLI pinned 全部適用案例含破壞集 | §11.6 | true | false |
| S1B-L3-P0P1-REMOTE | 1B | linux | L3 | P0＋P1＋remote | §11.6 | true | false |
| S1B-L3-MIXED-48H-WINDOWS | 1B | windows | L3 | Rust CLI＋Python daemon 48h | §11.6 | true | true |
| S1B-ROLLBACK-HW | 1B | both | hw | 實機回滾 | §12.2 | true | false |
| S21-PERF-WAL | 2.1 | both | perf | Rust WAL S1-W 單筆 p50／p95／p99 與 rotate 最大停頓（06 Task 22 `wal_s1w_perf`） | §1.2 | true | false |
| S21-L1-CORE | 2.1 | both | L1 | PAL／WAL／Arbiter／Shell 核心 | §11.6 | false | false |
| S21-L15-SAFETY | 2.1 | linux | L1.5 | 零 TX、部分送出、Drain／Release、獨占 | §11.6 | false | false |
| S21-NATIVE-SERIAL-LINUX | 2.1 | linux | L1.5 | 原生 serial（實體 UART） | §11.6 | true | false |
| S21-NATIVE-SERIAL-WINDOWS | 2.1 | windows | L1.5 | 原生 COM | §11.6 | true | false |
| S21-L2-F1-F2-F3-F4-F12 | 2.1 | linux | L2 | 切片承接清單 | §11.6 | true | false |
| S21-L3-P0-CORE-P1 | 2.1 | linux | L3 | P0 核心＋WAL／裝置衝突／Recovery P1 | §11.6 | true | false |
| S22-PERF-CONSOLE-ECHO | 2.2 | both | perf | Console 按鍵至回顯 p50／p99（115200 bps 實體 loopback、500 樣本，07 BLOCKED-OWNER #7） | §1.2 | true | false |
| S22-L15-CONSOLE | 2.2 | linux | L1.5 | Console 共存、Lease、DeferredBuffer | §11.6 | false | false |
| S22-WIN-NATIVE-CONSOLE | 2.2 | windows | L1.5 | Windows-native Console | §11.6 | true | false |
| S22-L2-F5-F6-F8 | 2.2 | linux | L2 | 切片承接清單 | §11.6 | true | false |
| S22-L3-CONSOLE-P1 | 2.2 | linux | L3 | Console／Lease P1 | §11.6 | true | false |
| S23-L15-LOGIN-EVENT | 2.3 | linux | L1.5 | Login FSM、Quiet、Event、Stall | §11.6 | false | false |
| S23-L2-F9-F10-F11 | 2.3 | linux | L2 | 切片承接清單 | §11.6 | true | false |
| S23-L3-LOGIN-REMOTE | 2.3 | linux | L3 | 登入／Recovery／事件 P1＋remote | §11.6 | true | false |
| S24-L15-ALL-CONTRACTS-LINUX | 2.4 | linux | L1.5 | 全部凍結契約 | §11.6 | false | false |
| S24-L15-ALL-CONTRACTS-WINDOWS | 2.4 | windows | L1.5 | 全部凍結契約（原生 COM） | §11.6 | true | false |
| S24-L2-ALL-38 | 2.4 | linux | L2 | 全部適用 38 案例含 F7 | §11.6 | true | false |
| S24-P2-1-MCU-FLASH | 2.4 | linux | manual | P2-1 MCU flash 手動程序 | §11.6 | true | false |
| S24-L3-P0P1-REMOTE | 2.4 | linux | L3 | P0＋P1＋remote | §11.6 | true | false |
| S24-L3-LRMIXED-48H-LINUX | 2.4 | linux | L3 | Rust daemon 48h `lr-mixed` | §11.6 | true | true |
| S24-L3-LRMIXED-48H-WINDOWS | 2.4 | windows | L3 | Rust daemon 48h `lr-mixed` | §11.6 | true | true |
| S3-L1-RELEASE-CONTRACT-LINUX | 3 | linux | L1 | release artifact 完整契約黑箱（`tests/contract_blackbox`） | §11.6 | false | false |
| S3-L1-RELEASE-CONTRACT-WINDOWS | 3 | windows | L1 | release artifact 完整契約黑箱（Windows 原生） | §11.6 | false | false |
| S3-L15-RELEASE-AB-LINUX | 3 | linux | L1.5 | release artifact 契約＋L1.5 A/B | §11.6 | false | false |
| S3-L15-RELEASE-AB-WINDOWS | 3 | windows | L1.5 | release artifact L1.5 A/B（原生 COM） | §11.6 | true | false |
| S3-L2-FULL-AB-LINUX | 3 | linux | L2 | 發版前全部適用案例含破壞集 A/B | §11.6 | true | false |
| S3-L2-AB-WINDOWS | 3 | windows | L2 | Windows 適用案例 A/B | §15.8.4 | true | false |
| S3-L3-P0P1-REMOTE-LINUX | 3 | linux | L3 | release candidate 的 L3 完整穩定性集合（P0＋P1＋remote） | §11.5 | true | false |
| S3-L3-P0P1-REMOTE-WINDOWS | 3 | windows | L3 | release candidate 的 L3 完整穩定性集合（Windows 適用） | §11.5 | true | false |
| S3-L3-AB-48H-LINUX | 3 | linux | L3 | 48h Sequential A/B | §15.9.2 | true | true |
| S3-L3-AB-48H-WINDOWS | 3 | windows | L3 | 48h Sequential A/B | §15.9.2 | true | true |
| S3-CANARY-LINUX | 3 | linux | canary | 14 日／5,000／50 | §12.2 | true | false |
| S3-CANARY-WINDOWS | 3 | windows | canary | 14 日／5,000／50 | §12.2 | true | false |
| S3-ROLLBACK-LINUX | 3 | linux | hw | 實機回滾演練 | §12.1 | true | false |
| S3-ROLLBACK-WINDOWS | 3 | windows | hw | 實機回滾演練 | §12.1 | true | false |
| S3-INCIDENTS-CLOSED | 3 | both | gate | 事故全部結案、無安全不變量違反 | §12.2 | true | false |
| S3-OD09-ALL-FROZEN | 3 | both | gate | OD-09 全部項目已凍結且早於候選長跑 | §14.2 | true | false |

**`packaging/python_daemon_retirement.json`（初始值）：**

```json
{"approved_at": null, "approved_by": null, "decision_ref": null, "effective_bundle_version": null, "evidence_index": "docs/rust-refactor/evidence/index.json", "evidence_index_sha256": null, "retired": false, "schema_version": 2, "stage3_target_artifact_ids": {"linux": null, "windows": null}}
```

核准與 hash 只存在這個檔案；Stage 3 bundle 的 manifest 只帶靜態標記 `retirement: {"python_daemon": "removed"}`（Task 16），因此受測 bundle 在核准前後位元組不變。

**帳本紀錄型別（`canary_ledger_record.schema.json`，以 `type` 區分的 `oneOf`；每筆皆含 `type`、`record_id`、`host_id`、`platform`，其餘欄位如下，`additionalProperties:false`）：**

| type | 產生者 | 欄位（除共同欄位外） |
|---|---|---|
| `snapshot` | Task 9 | `t_wall`、`boot_id`（str\|null）、`daemon_ok`、`daemon_pid`、`daemon_version`、`daemon_exe_sha256`（str\|null）、`artifact_id`（str\|null）、`artifact_check`（`"verified"`\|`"cli_mismatch"`\|`"daemon_mismatch"`\|`"version_mismatch"`\|`"unresolved"`）、`bundle_version`、`daemon_implementation`、`wal_dir`、`wal_healthy`、`multi_open`、`sessions`（`[{com,state,bridge_generation,boot_quiet_remaining_s,ready_reconfirm_pending,ready_reconfirm_failed,command_capable,platform,profile,last_error,last_ready_at}]`）、`error`（str\|null） |
| `submit` | Task 9 | `cmd_id`（符合 `cmd_id_regex`）、`com`、`source`、`wall_ts`、`seq`、`wal_file` |
| `submit_result` | Task 9 | `cmd_id`、`status`（`"done"`\|`"error"`\|`"timeout"`\|`"unknown"`）、`error_code`（str\|null）、`t_wall` |
| `wal_signal` | Task 9 | `kind`（Task 8 `WalSignal.kind`）、`com`、`wall_ts`、`seq`、`source`、`detail`（object，不含 payload） |
| `harvest_anomaly` | Task 9 | `detail`（Task 8 anomaly 字串）、`t_wall` |
| `note` | Task 11 | `kind`（`planned_restart`\|`planned_downtime`）、`from`、`to`、`reason`、`author`、`t_wall` |
| `incident_open` | Task 11 | `incident_id`、`kind`、`artifact_id`、`opened_at`、`detected_by`（`"manual"`；自動事故不寫紀錄、每次重算）、`summary`、`evidence`（[str]） |
| `incident_close` | Task 11 | `incident_id`、`resolution`、`issue_ref`（str\|null）、`note`、`closed_by`、`closed_at` |
| `carryover` | Task 12 | `from_artifact`、`to_artifact`、`counters`（`["submits"\|"cycles"]`）、`approved_by`、`decision_ref`、`approved_at` |
| `policy_ref` | Task 9 | `policy_sha256`、`status`、`frozen_at`、`decision_ref`、`collector_revision`（zipapp 的 sha256，或 repo 執行時 `git rev-parse HEAD`）、`t_wall` |

**Steps:**

- [ ] **Step 1：寫失敗測試**（agy）

`tests/test_stage3_contracts.py`：

```python
from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema,example", [
    ("ab_manifest.schema.json", "examples/ab_manifest.l15.example.json"),
    ("ab_manifest.schema.json", "examples/ab_manifest.l2.example.json"),
    ("ab_manifest.schema.json", "examples/ab_manifest.l3.example.json"),
    ("canary_policy.schema.json", "canary_policy.json"),
    ("stage3_evidence_index.schema.json", "../../docs/rust-refactor/evidence/index.json"),
])
def test_example_validates(schema, example):
    jsonschema.validate(_load(ACC / example), _load(ACC / schema))


def test_ledger_example_records_validate():
    schema = _load(ACC / "canary_ledger_record.schema.json")
    lines = (ACC / "examples" / "canary_records.example.ndjson").read_text(encoding="utf-8").splitlines()
    kinds = set()
    for line in lines:
        rec = json.loads(line)
        jsonschema.validate(rec, schema)
        kinds.add(rec["type"])
    assert kinds == {"snapshot", "submit", "submit_result", "wal_signal", "harvest_anomaly", "note",
                     "incident_open", "incident_close", "carryover", "policy_ref"}
    assert not any("payload_b64" in line for line in lines)


@pytest.mark.parametrize("name", ["ab_manifest", "canary_policy", "canary_ledger_record", "evidence_index"])
def test_new_schemas_have_schema_and_id(name):
    doc = _load(ACC / f"{name}.schema.json")
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == f"https://serialwrap.local/schemas/{name}"


def test_canary_policy_draft_keeps_od10_floor():
    pol = _load(ACC / "canary_policy.json")
    assert pol["status"] == "draft" and pol["frozen_at"] is None and pol["frozen_by"] is None
    for plat in ("linux", "windows"):
        q = pol["platforms"][plat]["quotas"]
        assert (q["active_days"], q["submits"], q["cycles_total"]) == (14, 5000, 50)
        assert sum(q["cycles_min_per_type"].values()) <= q["cycles_total"]
        assert sum(1 for v in q["cycles_min_per_type"].values() if v >= 1) >= 3
        assert 0 < q["cycles_max_type_share"] <= 0.5
    assert pol["platforms"]["windows"]["quotas"]["cycles_min_per_type"]["flash"] == 0
    assert pol["submit_counting"]["cmd_id_regex"] == "^[0-9a-f]{32}$"


def test_stage_exit_catalog_shape():
    items = _load(ACC / "stage_exits.json")["items"]
    ids = [i["item_id"] for i in items]
    assert len(ids) == 67 and len(set(ids)) == 67
    assert {i["stage"] for i in items} == {"0", "1A", "1B", "2.1", "2.2", "2.3", "2.4", "3"}
    assert {i["item_id"] for i in items if i["candidate_longrun"]} == {
        "S1B-L3-MIXED-48H-WINDOWS", "S24-L3-LRMIXED-48H-LINUX", "S24-L3-LRMIXED-48H-WINDOWS",
        "S3-L3-AB-48H-LINUX", "S3-L3-AB-48H-WINDOWS"}
    for i in items:
        if i["layer"] in ("L2", "L3", "hw", "canary", "manual", "supervisor", "spike"):
            assert i["owner_blocked"] is True, i["item_id"]
        assert "allow_xfail" not in i
        assert i["evidence_kind"] in ("junit", "functest-report", "bridge-selftest", "regression-report", "realhw-report",
                                      "gate-report", "ab-report", "canary-report", "drill-report", "od09-freeze",
                                      "manual-procedure"), i["item_id"]
    assert {i["item_id"] for i in items if i["stage"] == "3" and i["layer"] == "L3"} >= {
        "S3-L3-P0P1-REMOTE-LINUX", "S3-L3-P0P1-REMOTE-WINDOWS"}
    assert {"S0-SPIKE2-WINTASK-72H", "S0-SPIKE3-WIN-WAL-EDR"} <= set(ids)


def test_reason_codes_cover_sources_and_do_not_collide():
    reg = _load(ROOT / "tools" / "stage3" / "stage3_reason_codes.json")["codes"]
    used: set[str] = set()
    for p in (ROOT / "tools" / "stage3").rglob("*.py"):
        used |= set(re.findall(r"HARNESS_[A-Z0-9_]+", p.read_text(encoding="utf-8")))
    assert used <= set(reg), sorted(used - set(reg))
    product = (ROOT / "contracts" / "error_codes.yaml").read_text(encoding="utf-8")
    for code, desc in reg.items():
        assert code.startswith("HARNESS_") and code not in product
        assert desc["meaning"] and desc["action"]


def test_retirement_switch_default_off():
    r = _load(ROOT / "packaging" / "python_daemon_retirement.json")
    assert r["retired"] is False
    assert r["evidence_index"] == "docs/rust-refactor/evidence/index.json"
    assert r["stage3_target_artifact_ids"] == {"linux": None, "windows": None}
```

`tests/test_stage3_base.py` 測試表（每列一個 test function）：

| 測試名 | 斷言 |
|---|---|
| `test_dumps_compact_stable` | `jsonio.dumps_compact({"b":1,"a":"中"}) == '{"a":"中","b":1}'` |
| `test_dumps_report_trailing_newline` | `jsonio.dumps_report({}).endswith("\n")` 且 `json.loads` 成功 |
| `test_parse_iso_rejects_naive` | `pytest.raises(ValueError)`：`parse_iso("2026-11-02T01:00:00")` |
| `test_parse_iso_accepts_z` | `parse_iso("2026-11-02T01:00:00Z") == parse_iso("2026-11-02T01:00:00+00:00")` |
| `test_read_ndjson_reports_malformed` | 檔內三行（合法、`{bad`、合法）→ `(len(recs), bad) == (2, [2])` |
| `test_record_id_ignores_own_field` | `record_id({"a":1}) == record_id({"a":1,"record_id":"x"})`，長度 20 |
| `test_atomic_write_replaces` | 寫兩次後內容為第二次，目錄內無 `*.tmp` 殘留 |
| `test_run_cmd_timeout` | `run_cmd([sys.executable,"-c","import time;time.sleep(5)"], timeout=0.2)` → `rc == -1 and timed_out` |
| `test_run_json_non_object` | stdout `"[1]"` → 第二個回傳值為 `None` |
| `test_harness_error_unknown_code` | `pytest.raises(ValueError)`：`HarnessError("HARNESS_NOPE")` |
| `test_load_policy_rejects_floor` | 把 linux `submits` 改 4999 → `HarnessError`，`.code == "HARNESS_CANARY_POLICY_INVALID"` |
| `test_load_policy_rejects_windows_flash` | windows `flash: 1` → 同上 |
| `test_load_policy_rejects_frozen_without_owner` | `status:"frozen"`、`frozen_by:null` → 同上 |
| `test_load_policy_rejects_single_type_quota` | 只有 `recovery` 的 min ≥1 → 同上（`MIN_TYPES_WITH_QUOTA`） |
| `test_load_policy_rejects_share_above_half` | `cycles_max_type_share: 0.6` → 同上 |
| `test_load_policy_rejects_other_cmd_id_regex` | `cmd_id_regex: ".*"` → 同上 |
| `test_load_policy_requires_decision_in_addenda` | frozen policy、`addenda_path` 檔無該 `decision_ref` 列 → 同上；有該列 → 成功；`addenda_path=None` → 成功 |
| `test_local_date_uses_offset_changes` | `utc_offset_changes=[["2026-11-03T00:00:00+00:00", 0]]`、預設 480：`2026-11-02T17:00:00+00:00` → `2026-11-03`；`2026-11-03T17:00:00+00:00` → `2026-11-03` |
| `test_reason_codes_loaded_via_resources` | monkeypatch 工作目錄到 `tmp_path` 後 `load_reason_codes()` 仍成功（不依賴 repo 相對路徑） |
| `test_assert_frozen_before_draft` | draft policy → `.code == "HARNESS_CANARY_POLICY_NOT_FROZEN"` |
| `test_assert_frozen_before_late` | `frozen_at` 晚於 `first_counted_wall_ts` → 同上；早於 → 不丟例外 |

`tests/test_stage3_consumes.py`（前置 Phase 介面守門）：

```python
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFERRED = ("resources", "min_effective_load", "comparison_uncertainty")
P2_DEFERRED_FIELDS = {   # P2 Task 15「延後項讀寫契約」逐字；Task 15 od09_freeze.PER_PLATFORM_FIELDS 必須相等（test_per_platform_fields_match_p2）
    "resources": ("rss_peak_kb", "rss_steady_kb", "rss_slope_kb_per_h"),
    "min_effective_load": ("min_completed_per_board_source", "max_idle_gap_s"),
    "comparison_uncertainty": ("error_rate_abs_tolerance", "throughput_ratio_tolerance", "stuck_s_tolerance", "max_reruns"),
}

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_p2_gate_api():
    from realhw.acceptance import gate
    assert [f.name for f in dataclasses.fields(gate.GateInputs)] == [
        "report_dir", "case_dir", "acceptance_path", "platform", "mode", "observer_path", "wal_dir", "baseline_gate_path"]
    assert callable(gate.evaluate) and callable(gate.main)
    assert gate.RESULTS == ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED", "NOT_APPLICABLE_BASELINE")
    doc, meta = gate.load_acceptance(ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml")
    assert {"path", "revision", "sha256"} <= set(meta)


def test_p2_acceptance_deferred_and_workload_keys():
    import yaml
    doc = yaml.safe_load((ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml").read_text(encoding="utf-8"))
    assert "revision" in doc
    assert {"min_continuous_duration_s", "snapshot_interval_s", "max_observation_gap_factor", "engine_files"} <= set(doc["workload"])
    for k in DEFERRED:
        assert doc["thresholds"][k]["status"] in ("deferred", "frozen")
        # OD-09 延後項讀寫契約（P2 Task 15）：數值只在 per_platform.<os> 之下，欄位名即 Task 15 freeze 的寫入介面
        assert set(doc["thresholds"][k]["per_platform"]) == {"linux", "windows"}
        for os_name in ("linux", "windows"):
            assert set(doc["thresholds"][k]["per_platform"][os_name]) == set(P2_DEFERRED_FIELDS[k])
        assert not (set(doc["thresholds"][k]) & {f for fs in P2_DEFERRED_FIELDS.values() for f in fs})   # 無直接掛在 key 下的數值欄位
        assert not ({"tolerance", "rerun_rule", "deferred"} & set(doc["thresholds"][k]))
    assert "deferred" not in doc and "deferred" not in doc["thresholds"]


def test_p2_cases_yaml_and_lock():
    import yaml
    cases = yaml.safe_load((ROOT / "tests" / "acceptance" / "cases.yaml").read_text(encoding="utf-8"))
    assert len(cases["invariant_ids"]) == 6
    assert {"case_id", "invariants", "destructive", "expected", "sentinel", "necessity", "layer", "platforms"} <= set(cases["cases"][0])
    lock = json.loads((ROOT / "tests" / "acceptance" / "cases.lock.json").read_text(encoding="utf-8"))
    assert "slice_2.4" in lock["sets"]


def test_p0_behavior_changes_scope():
    import yaml
    doc = yaml.safe_load((ROOT / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8"))
    entries = doc["changes"] if isinstance(doc, dict) else doc
    assert entries and all(e["scope"] in ("both", "rust_only", "python_when_manifest_present") for e in entries)
    assert all(e["ab_participation"] == ("diff" if e["scope"] == "both" else "absolute_only") for e in entries)


def test_p0_markers_on_stage3_files():
    import ast
    platform = {"core_portable", "posix_only", "windows_native"}
    for p in (ROOT / "tests").glob("test_stage3_*.py"):
        names = {n.attr for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
        assert names & platform, p.name   # 平台 marker 細部規則由 P0 tests/test_resource_markers.py 稽核
    assert not (ROOT / "tests" / "platform_resources.yaml").exists()   # P2 03 Task 13：不建此檔
```

- [ ] **Step 2：執行並確認紅燈**

Run: `python3 -m pytest -q tests/test_stage3_contracts.py tests/test_stage3_base.py tests/test_stage3_consumes.py`
Expected: FAIL（`FileNotFoundError` 指向 `tests/acceptance/*.json`、`ModuleNotFoundError: No module named 'tools.stage3'`）。`test_stage3_consumes.py` 在 P0／P2 已合併時應只因本計畫檔案缺失而紅；若因 P2 介面不符而紅，依 §0 規則改本計畫消費端。

- [ ] **Step 3：實作**（haiku）

依上方 Interfaces 與 JSON 內容建立全部檔案。`proc.run_cmd` 以 `subprocess.run(capture_output=True, encoding="utf-8", errors="replace")`；`TimeoutExpired` → `CmdResult(rc=-1, stdout=exc.stdout or "", stderr=exc.stderr or "", elapsed_s=…, timed_out=True)`。`jsonio.append_ndjson` 在 Windows 也需 `os.fsync`。`reason_codes.load_reason_codes` 以 `functools.lru_cache` 讀一次。`canary_records.example.ndjson` 每種 `type` 至少一行，`record_id` 以 `jsonio.record_id` 計算後寫入。`canary_ledger_record.schema.json` 依上方「帳本紀錄型別」表；`stage3_evidence_index.schema.json` 依 Task 14 的 `EvidenceEntry` 與 target 形狀（`stage_targets[stage][platform]`）。本 Task 三個測試檔依 P0 T14 帶 `pytestmark`。

- [ ] **Step 4：執行並確認綠燈**

Run: `python3 -m pytest -q tests/test_stage3_contracts.py tests/test_stage3_base.py tests/test_stage3_consumes.py`
Expected: PASS。

- [ ] **Step 5：commit**

```bash
git add tools/__init__.py tools/stage3/ tests/acceptance/ docs/rust-refactor/evidence/index.json docs/rust-refactor/owner-decisions-addenda.md packaging/python_daemon_retirement.json tests/test_stage3_contracts.py tests/test_stage3_base.py tests/test_stage3_consumes.py changelog.d/rust-p9-01-stage3-contracts.md
git commit -m "feat(stage3): 新增 Stage 3 契約檔、schema 與基礎模組" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Fragment 內容：

```markdown
---
type: feat
scope: stage3
---
新增 Stage 3 前置工具的契約檔：A/B manifest、Canary policy（draft，OD-10 配額下限不可下修）與帳本紀錄 schema、Stage 0～3 出口項目錄（67 項）、證據索引、`HARNESS_*` 原因碼登錄表與 Python daemon 退役開關（預設關閉）。
```

---

## Task 2：共用基礎測試替身（`tests/stage3_fakes.py`）

**Test author model:** haiku-xhigh
**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p9-02-stage3-fakes.md`
**BLOCKED-OWNER:** 無（純 L1）。

本 Task 屬測試基礎設施，於第一輪完整執行並全綠後 commit。只放多個 Task 共用、且只依賴 Task 1 模組的**基礎**替身；各 Task 專屬替身與其測試放在該 Task 自己的 `tests/fakes_<主題>.py`，隨該 Task 一起寫、一起 commit（`fakes_suites.py`＝Task 5B、`fakes_canary.py`＝Task 12、`fakes_drill.py`＝Task 13、`fakes_evidence.py`＝Task 14、`fakes_od09.py`＝Task 15、`fakes_retirement.py`＝Task 16）。替身不得 import `sw_core`，也不得 import 尚未存在的 `tools.stage3` 模組（例如 `FakeProbe` 只以 duck typing 實作 Task 4 的 `ReleaseProbe` 方法集合，符合性由 Task 4 的測試斷言）。替身檔名不以 `test_` 開頭，不需依 P0 T14 在測試檔頭寫 `pytestmark`。

**Files:**
- Create: `tests/stage3_fakes.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_fakes.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_fakes.py`

**Interfaces — Consumes:** `tools.stage3.proc.CmdResult`、`tools.stage3.jsonio`（Task 1）。
**Interfaces — Produces:**

```python
def ts(day: int, hh: int, mm: int = 0, ss: int = 0) -> str          # f"2026-11-{day:02d}T{hh:02d}:{mm:02d}:{ss:02d}+00:00"
def wal_record(seq: int, *, dir: str = "TX", com: str = "COM0", source: str = "agent:a", cmd_id: str | None = None,
               wall_ts: str = "2026-11-02T01:00:00+00:00", meta: dict | None = None, loss_flag: bool = False,
               payload: bytes = b"x") -> dict
    # 欄位集合與 inventory 04 §1.3 相同：cmd_id, com, crc32, dir, len, loss_flag, meta, mono_ts_ns, payload_b64, seq, source, wall_ts；
    # crc32／len 依 payload 真實計算
def uuid_cmd_id(n: int) -> str                                        # f"{n:032x}"，符合 ^[0-9a-f]{32}$
def write_wal(path: Path, records: Iterable[dict], *, trailing_newline: bool = True, mode: str = "w") -> None   # 每行 json.dumps(sort_keys=True, ensure_ascii=False)
def make_bundle(root: Path, *, version: str = "0.9.0", stage: str = "3", cli_impl: str = "rust", daemon_impl: str = "rust",
                retirement_marker: bool | None = None, target: str = "x86_64-unknown-linux-gnu") -> Path
    # 建 bundle 目錄：release-manifest.json（arch §10.1 形狀）、bin/serialwrap、bin/serialwrapd（stub 腳本）、SHA256SUMS；sha256 皆真實計算。
    # daemon_impl=="rust"：components.daemon={implementation,version,git_revision,launcher:{path:"bin/serialwrapd",sha256}}；
    # daemon_impl=="python"：另建 py/ 樹，daemon 帶 entry、runtime{path,sha256}、package{tree_sha256}，並帶 python_cli_delegate{entry,tree_sha256}（tree_sha256 依 arch §10.1 演算法真實計算）。
    # retirement_marker 預設＝(stage=="3")：True 時寫 "retirement":{"python_daemon":"removed"}，且不寫 python_cli_delegate
def tree_sha256(root: Path) -> str                                   # arch §10.1 演算法（排除 __pycache__、*.pyc）
def write_fake_repo(root: Path, *, layer: str = "l1.5") -> Path
    # 建立假 repo：contracts/behavior_changes.yaml（BC-01 scope both、SC-15 scope rust_only 等）、tests/acceptance/cases.yaml（含六個 invariant_ids、
    # 每個 invariant 至少一個 case、一個 destructive case、一個 expected XFAIL＋sentinel 的 case）、tests/acceptance/cases.lock.json、
    # tools/stage3/、tools/uart_stdio_bridge/、func-test/runner.py、regression/、realhw/（各放一個 stub 檔，供 FrozenTree）、profiles/default.yaml、
    # realhw/acceptance/longrun_acceptance.yaml 與 longrun_acceptance.schema.json：自本 repo 實際 P2 檔原樣複製（不自造 fixture；三個延後項 status deferred、per_platform 值為 null）
def write_ab_manifest(root: Path, *, layer: str = "l1.5", platform: str = "linux", stage: str = "3", **overrides: Any) -> Path
    # 呼叫 write_fake_repo；A=baseline（daemon python）、B=candidate（daemon rust）兩個 bundle；依 Task 1 schema 寫 ab-manifest.json，
    # case_ids 預設＝該 layer／platform 的 required_case_set 案例；計算全部 FrozenFile／FrozenTree；overrides 以 dotted key 覆寫（如 "reverify.enabled"=True）
class FakeClock:
    def __init__(self, start_mono: float = 1000.0, start_wall: str = "2026-11-02T00:00:00+00:00") -> None
    def now(self) -> float
    def sleep(self, s: float) -> None        # 推進 mono 與 wall
    def iso(self) -> str
class FakeProbe:   # duck typing：方法集合同 Task 4 ReleaseProbe（pid_alive、descendants、pids_by_exe、endpoint_connectable、lock_acquirable、device_holders、device_exclusive_open）
    def __init__(self, *, pid_alive: dict[int, list[bool | None]] | None = None, descendants: dict[int, list[tuple[int, ...] | None]] | None = None,
                 endpoint: dict[str, list[bool | None]] | None = None, lock: dict[str, list[bool | None]] | None = None,
                 holders: dict[str, list[tuple[tuple[int, ...], int] | None]] | None = None, exclusive: dict[str, list[bool | None]] | None = None,
                 by_exe: dict[str, list[tuple[int, ...] | None]] | None = None) -> None
    # 每次呼叫取序列下一個值，耗盡後重複最後一個；pids_by_exe 以 match_paths[0] 為鍵；holders 值為 (pids, unreadable_count)；
    # 未設定的鍵：pid_alive→False、descendants→()、endpoint→False、lock→True、holders→((), 0)、exclusive→True、by_exe→()
    calls: list[tuple[str, object]]
class FakeProc:
    pid: int
    def poll(self) -> int | None
    def terminate(self) -> None
    def kill(self) -> None
    def wait(self, timeout: float | None = None) -> int
class RecordingRunner:
    def __init__(self, script: Mapping[str, list[dict]] | None = None) -> None
    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None, cwd: Path | None = None) -> CmdResult
    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> FakeProc
    def raise_on(self, prefix: str, exc: BaseException) -> None     # 符合 f"{kind}:{sub}" 前綴的呼叫丟出 exc
    def on(self, prefix: str, effect: Callable[[list[str], Mapping[str, str]], None]) -> None   # 符合前綴的呼叫先執行 effect（例如寫報告檔），再回 script 結果
    calls: list[dict]   # {"kind": str, "sub": str, "argv": list[str], "env": dict, "side": env.get("SW_STAGE3_SIDE"), "round": env.get("SW_STAGE3_ROUND")}
    # 程式位置 prog_idx：argv[0] basename 符合 ^(python[0-9.]*|py)(\.exe)?$ 且 len(argv)>1 時為 1，否則 0
    # kind：argv[prog_idx]=="-m" 時為其下一個 token（例："realhw"、"realhw.acceptance.observer"、"realhw.acceptance.gate"）；
    # 否則依 argv[prog_idx] basename：git→"git"、runner.py→"functest"、testpilot→"testpilot"、serialwrap/serialwrap.exe→"cli"、docker→"docker"、schtasks/schtasks.exe→"schtasks"、systemctl→"systemctl"，其餘 "other"
    # sub＝kind 之後依序取前 2 個「不以 "-" 開頭、不含 "/" 或 "\\"、且不是前一個 "-" 旗標之值」的 token，以空白相接（例："daemon status"、"rev-parse HEAD:contracts"、"setup"；無則 ""）
    # 旗標值判定：只把 --selector、--cmd、--cmd-id、--cmd-timeout、--instance、--report-dir、--seed、--side、--round、--source、--reason、
    #   --a、--b、--out、--tier、--mode、--dut-mode、--selftest-report、--endpoint、--socket、--case-dir、--acceptance、--platform、-C 的下一個 token 視為值
    # script 鍵 f"{kind}:{sub}"，值為 {"rc","stdout","stderr"} 序列（逐次取用，耗盡重複最後一個）；未定義的鍵回 rc=0、stdout="{}"
def make_stub_cli(dir: Path, scenario: Mapping[str, list[dict]], *, name: str = "serialwrap") -> Path
    # 寫可執行 Python 腳本（POSIX：shebang＋chmod 0755；Windows：另寫同名 .cmd 包裝），依 " ".join(非全域旗標 argv) 查 scenario；
    # 每次呼叫追加 dir/"calls.ndjson" 一行 {"argv": [...]}；序列狀態存 dir/"stub-state.json"
def read_calls(dir: Path) -> list[dict]
def snapshot(t_wall: str, *, host: str = "h1", platform: str = "linux", boot: str = "b1", pid: int | None = 100, ok: bool = True,
             artifact: str | None = "art-1", artifact_check: str = "verified", version: str = "0.9.0", wal_healthy: bool = True,
             multi_open: bool = False, sessions: Sequence[tuple] = (("COM0", "READY", 1),)) -> dict
    # sessions 元素 (com, state, bridge_generation) 或 (com, state, bridge_generation, extra: dict)；extra 可設 boot_quiet_remaining_s、
    # ready_reconfirm_pending、ready_reconfirm_failed、command_capable（預設 True）、platform（預設 "shell"）、profile、last_ready_at；
    # 回傳 type="snapshot" 帳本紀錄（含 record_id），欄位依 Task 1 帳本紀錄型別表
def policy_dict(**over: Any) -> dict
    # 以 canary_policy.json 為底，status="frozen"、frozen_by="owner"、frozen_at="2026-11-01T00:00:00+00:00"、decision_ref="OD-10-C1"、
    # evidence_owner="owner"、兩平台 utc_offset_minutes=0（讓合成資料一個 UTC 日恰為一個本地日）；dotted key 覆寫
def write_policy(path: Path, **over: Any) -> Path
def sha256_text(s: str) -> str
def sha256_file(path: Path) -> str          # 轉呼叫 tools.stage3.jsonio.sha256_file
```

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_fakes.py`（`pytestmark = [core_portable, memory_only]`）：

| 測試名 | 斷言 |
|---|---|
| `test_wal_record_fields` | `set(wal_record(1)) == {"cmd_id","com","crc32","dir","len","loss_flag","meta","mono_ts_ns","payload_b64","seq","source","wall_ts"}`；`wal_record(1, payload=b"abc")["len"] == 3` |
| `test_write_wal_no_trailing_newline` | `trailing_newline=False` 時檔案不以 `\n` 結尾 |
| `test_make_bundle_hashes_real` | rust daemon：`components.daemon.launcher.sha256 == sha256_file(bundle/"bin"/"serialwrapd")`、無 `daemon.path`；python daemon：`package.tree_sha256 == tree_sha256(bundle/"py")` |
| `test_make_bundle_stage3_marker` | `stage="3"` → `retirement == {"python_daemon": "removed"}` 且無 `python_cli_delegate`；`stage="2.4"` → 無 `retirement` |
| `test_write_ab_manifest_validates_schema` | 以 `tests/acceptance/ab_manifest.schema.json` 驗證通過（l1.5、l2、l3 各一） |
| `test_fake_probe_sequences` | `pid_alive={7:[True,False]}` → 連三次呼叫 `True, False, False` |
| `test_recording_runner_kind_and_side` | `runner(["/x/serialwrap","daemon","status"], env={"SW_STAGE3_SIDE":"A"})` → `kind=="cli"`、`sub=="daemon status"`、`side=="A"`；`runner(["python3","-m","realhw","--tier","longrun","--report-dir","/r"])` → `kind=="realhw"`、`sub==""`；`runner(["python3","func-test/runner.py","--mode","bridge"])` → `kind=="functest"` |
| `test_recording_runner_on_effect` | `on("functest:", f)` 後呼叫 → `f` 收到 argv 與 env，回傳值仍依 script |
| `test_stub_cli_logs_calls` | subprocess 執行 stub `session list` 兩次 → `len(read_calls(dir)) == 2`，stdout 依 scenario 順序 |
| `test_fake_clock_sleep_advances_both` | `sleep(30)` 後 `now()` 增 30 且 `iso()` 晚 30 秒 |
| `test_snapshot_record_shape` | `snapshot(ts(2,1))["type"] == "snapshot"` 且通過 `canary_ledger_record.schema.json` |
| `test_policy_dict_loads` | `canary_policy.load_policy(write_policy(tmp/"p.json"))` 成功、`status=="frozen"`、兩平台 `utc_offset_minutes==0` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_fakes.py` → Expected: FAIL（`ModuleNotFoundError: No module named 'stage3_fakes'`）。
- [ ] **Step 3：實作**（agy）依 Interfaces。`make_stub_cli` 的腳本只用 stdlib；全域旗標（`--socket`、`--endpoint`、`--retries`、`--timeout` 及其值、`-v`）在查 scenario 前移除。依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS（本 Task 不含任何依賴後續 Task 的測試）。
- [ ] **Step 5：commit** `test(stage3): 新增 Stage 3 工具共用基礎測試替身`（files：`tests/stage3_fakes.py tests/test_stage3_fakes.py changelog.d/rust-p9-02-stage3-fakes.md`，trailer 同 Task 1）。

---

## Task 3：A/B manifest 載入、凍結與案例涵蓋檢查（`tools/stage3/ab_manifest.py`）

**Test author model:** haiku-xhigh
**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p9-03-ab-manifest.md`
**BLOCKED-OWNER:** 無（純 L1）。

**Files:**
- Create: `tools/stage3/ab_manifest.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_ab_manifest.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_ab_manifest.py`

**Interfaces — Consumes:** Task 1 schema、`reason_codes.HarnessError`、`proc.Runner`；P2 `cases.yaml`／`cases.lock.json`（只讀資料）。
**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class FrozenFile:
    path: str        # 相對 repo_root（或絕對）
    sha256: str
@dataclass(frozen=True)
class FrozenTree:
    path: str
    tree_sha256: str
@dataclass(frozen=True)
class DutSpec:
    kind: str                         # "local-process" | "docker" | "physical-substitute" | "hardware"
    transport: str                    # "posix-pty" | "com0com-pair" | "physical-usb-uart"
    com_pair: Mapping[str, str] | None
    image: str | None
    image_digest: str | None
    boards: tuple[str, ...]
    reset_argv: tuple[str, ...]
    fingerprint_argv: tuple[str, ...]
    expected_fingerprint: Mapping[str, str]
@dataclass(frozen=True)
class Authorization:
    allow_destructive: bool
    decision_ref: str | None
    boards: tuple[str, ...]
@dataclass(frozen=True)
class SuiteSpec:
    adapter: str                      # "l15_functest" | "l2_regression" | "l3_realhw"
    timeout_s: float
    selftest_report: FrozenFile | None
    testbed_template: FrozenFile | None
    realhw_config_template: FrozenFile | None
    tier: str | None
    extra_args: tuple[str, ...]
@dataclass(frozen=True)
class ReleaseSpec:
    timeout_s: float
    poll_s: float
    endpoints: tuple[str, ...]
    device_paths: tuple[str, ...]
    lock_paths: tuple[str, ...]
    device_open_probe: bool
    accept_unreadable_holders: bool
@dataclass(frozen=True)
class ReverifySpec:
    enabled: bool
    order: tuple[str, ...]
@dataclass(frozen=True)
class AbSide:
    label: str
    role: str
    bundle_dir: Path
    release_manifest: FrozenFile
    cli_exe: Path
    daemon_exe: Path
    activate_argv: tuple[str, ...]
    deactivate_argv: tuple[str, ...]
    manifest_doc: Mapping[str, Any]   # 讀入的 release-manifest.json
@dataclass(frozen=True)
class AbManifest:
    schema_version: int
    run_id: str
    layer: str
    platform: str
    stage: str
    contract_revision: str
    case_list: FrozenFile
    case_lock: FrozenFile
    required_case_set: str
    case_ids: tuple[str, ...]
    tools: tuple[FrozenFile | FrozenTree, ...]
    allowed_differences: FrozenFile
    acceptance: FrozenFile | None
    profile_files: tuple[FrozenFile, ...]
    seed: int
    platform_settings: Mapping[str, Any]
    dut: DutSpec
    authorization: Authorization | None
    test_device_allowlist: tuple[str, ...]
    sides: tuple[AbSide, AbSide]       # 依序 A、B
    suite: SuiteSpec
    endpoint: str | None
    release: ReleaseSpec
    reverify: ReverifySpec
    failure_retention: str | None
    source_path: Path
    source_sha256: str
LAYER_ADAPTER: Mapping[str, str] = {"l1.5": "l15_functest", "l2": "l2_regression", "l3": "l3_realhw"}
TEMPLATE_KEYS: frozenset[str] = frozenset({"cli_exe", "daemon_exe", "bundle_dir", "report_dir", "instance_dir", "seed", "side", "round",
                                         "out_dir", "repo_root", "endpoint"})
def load_manifest(path: Path) -> AbManifest
def verify_frozen(m: AbManifest, *, repo_root: Path, run: Runner = run_cmd) -> list[str]
def required_cases(m: AbManifest, *, repo_root: Path) -> tuple[set[str], dict[str, dict]]
    # (required_case_set ∩ {layer 對應、platform ∈ platforms 的案例}, case_id → cases.yaml 條目)
def verify_case_coverage(m: AbManifest, *, repo_root: Path) -> list[str]
def runtime_digest(manifest_doc: Mapping[str, Any]) -> str
    # sha256(dumps_compact({"cli": components.cli.sha256, "daemon_impl": components.daemon.implementation,
    #   "daemon": components.daemon.launcher.sha256, "runtime": components.daemon.runtime.sha256 或 None,
    #   "package_tree": components.daemon.package.tree_sha256 或 None, "assets": components.assets,
    #   "supervisor": components.supervisor.definition_sha256 或 None}))；不含版本字串、stage、created_at、retirement，供跨 stage 證據沿用比對（Task 14）
def render_argv(template: Sequence[str], values: Mapping[str, str]) -> list[str]
```

`load_manifest` 規則（違反任一 → `HarnessError("HARNESS_MANIFEST_INVALID", detail)`）：schema 驗證（以 stdlib 手寫檢查，不依賴 jsonschema）；`sides[0]` 為 `A`／`baseline`、`sides[1]` 為 `B`／`candidate`；讀兩側 `release-manifest.json` 並依 stage 檢查——`1A`／`1B`：兩側 `components.daemon.implementation == "python"`，A 的 `components.cli.implementation == "python"`、B 為 `"rust"`；`2.1`～`3`：兩側 CLI implementation 相同且 `components.cli.sha256` 相同，A daemon `"python"`、B daemon `"rust"`；`stage=="3"` 時 B 的 manifest `stage=="3"` 且帶 `retirement.python_daemon=="removed"`、`required_case_set=="slice_2.4"`；`suite.adapter == LAYER_ADAPTER[layer]`；`layer=="l1.5"` → `dut.kind ∈ {"local-process","docker","physical-substitute"}`、`suite.selftest_report` 非 null；`dut.kind=="docker"` → `image_digest` 非 null；`dut.transport=="com0com-pair"` → `com_pair` 非 null；`layer in ("l2","l3")` → `dut.kind=="hardware"`、`dut.boards` 非空且兩側 `activate_argv`、`deactivate_argv` 非空；`layer=="l2"` → `suite.testbed_template` 非 null；`layer=="l3"` → `acceptance`、`endpoint`、`suite.realhw_config_template`、`suite.tier` 與 `failure_retention=="on-demand"` 必填；`reverify.enabled` 時 `order == ("B","A")`；所有 argv 模板中的 `{name}` 須屬 `TEMPLATE_KEYS`；`cli_exe` 的 realpath 必須等於 `bundle_dir / components.cli.path`、`daemon_exe` 的 realpath 必須等於 `bundle_dir / components.daemon.launcher.path`（Python 基線若無 launcher，則等於 `components.daemon.entry` 解析後的腳本或 runtime 路徑）——實際執行的檔案即已驗證的 component（arch §10.1）。

`verify_frozen` 回傳違規字串清單（空＝已凍結），逐項格式固定：`"case_list sha256 mismatch: <path>"`、`"case_lock sha256 mismatch: <path>"`、`"tool sha256 mismatch: <path>"`、`"tool tree mismatch: <path>"`、`"allowed_differences sha256 mismatch: <path>"`、`"acceptance sha256 mismatch: <path>"`、`"profile sha256 mismatch: <path>"`、`"suite input sha256 mismatch: <path>"`（selftest_report／testbed_template／realhw_config_template）、`"release manifest sha256 mismatch: side <A|B>"`、`"release component sha256 mismatch: side <A|B> <component>"`（逐一核對 manifest 中帶 `path`＋`sha256` 的物件；`<component>` 為其在 `components` 下的點路徑，例如 `cli`、`daemon.launcher`、`daemon.runtime`）、`"release tree mismatch: side <A|B> <component>"`（依 arch §10.1 演算法重算 `daemon.package.tree_sha256` 與 `python_cli_delegate.tree_sha256` 所涵蓋的已部署 Python 檔案樹，不只看 `python.exe` hash）、`"contract_revision mismatch: manifest <x> repo <y>"`（以 `run(["git","-C",repo_root,"rev-parse","HEAD:contracts"])`）、`"dut image digest mismatch"`（`docker` 時 `run(["docker","image","inspect","--format","{{.Id}}",image])`）、`"file missing: <path>"`，以及 `verify_case_coverage` 的全部結果。Task 6 在每一側執行前**再呼叫一次** `verify_frozen`，確認兩側之間沒有漂移。

`verify_case_coverage`（§11.3「不得刪除案例、縮小分母」、§11.6 Stage 3「完整契約＋L1.5」）回傳：`"required case missing: <id>"`（`required_cases` 中不在 `case_ids` 者）；`"unknown case: <id>"`（`case_ids` 中不在 `cases.yaml` 者）；`"invariant not covered: <invariant_id>"`（`cases.yaml.invariant_ids` 六項中，沒有任何 `case_ids` 內、且 layer 相符的案例引用者；該 layer 在 `cases.yaml` 原本就沒有任何案例引用的 invariant 不列）；`"destructive case without authorization: <id>"`（`authorization` 為 null、`allow_destructive` 為 false 或 `decision_ref` 為 null）；`"authorization board not in dut: <com>"`；`"windows com0com instance without test_device_allowlist"`。任何一筆都使 Task 6 以 `HARNESS_CASE_SET_INCOMPLETE`（授權類為 `HARNESS_AUTHORIZATION_MISSING`）拒絕執行。

`render_argv`：每個元素以 `str.format_map` 取代；遇到 `TEMPLATE_KEYS` 以外或缺值的鍵 → `HarnessError("HARNESS_MANIFEST_INVALID")`。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_ab_manifest.py`，關鍵測試：

```python
from __future__ import annotations

import json

import pytest

import stage3_fakes as fakes
from tools.stage3 import ab_manifest
from tools.stage3.reason_codes import HarnessError

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _git_ok(rev):
    return fakes.RecordingRunner({"git:rev-parse HEAD:contracts": [{"rc": 0, "stdout": rev + "\n", "stderr": ""}]})


def test_frozen_manifest_has_no_violations(tmp_path):
    m = ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path))
    assert ab_manifest.verify_frozen(m, repo_root=tmp_path, run=_git_ok(m.contract_revision)) == []


def test_case_list_drift_detected(tmp_path):
    m = ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path))
    p = tmp_path / m.case_list.path
    p.write_text(p.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    v = ab_manifest.verify_frozen(m, repo_root=tmp_path, run=_git_ok(m.contract_revision))
    assert f"case_list sha256 mismatch: {m.case_list.path}" in v


def test_candidate_launcher_swapped_detected(tmp_path):
    m = ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path))
    (m.sides[1].bundle_dir / "bin" / "serialwrapd").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    v = ab_manifest.verify_frozen(m, repo_root=tmp_path, run=_git_ok(m.contract_revision))
    assert "release component sha256 mismatch: side B daemon.launcher" in v


def test_baseline_python_tree_tamper_detected(tmp_path):
    m = ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path))
    victim = next((m.sides[0].bundle_dir / "py").rglob("*.py"))
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# tamper\n", encoding="utf-8")
    v = ab_manifest.verify_frozen(m, repo_root=tmp_path, run=_git_ok(m.contract_revision))
    assert "release tree mismatch: side A daemon.package" in v


def test_trimmed_case_list_rejected(tmp_path):
    path = fakes.write_ab_manifest(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    dropped = doc["case_ids"].pop()
    path.write_text(json.dumps(doc), encoding="utf-8")
    m = ab_manifest.load_manifest(path)
    assert f"required case missing: {dropped}" in ab_manifest.verify_case_coverage(m, repo_root=tmp_path)


def test_stage3_requires_python_baseline_daemon(tmp_path):
    path = fakes.write_ab_manifest(tmp_path, stage="3")
    doc = json.loads(path.read_text(encoding="utf-8"))
    a_manifest = tmp_path / doc["sides"][0]["release_manifest"]["path"]
    md = json.loads(a_manifest.read_text(encoding="utf-8"))
    md["components"]["daemon"]["implementation"] = "rust"
    a_manifest.write_text(json.dumps(md), encoding="utf-8")
    with pytest.raises(HarnessError) as ei:
        ab_manifest.load_manifest(path)
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"
```

其餘以參數化表展開（`test_load_rejects[case]`，每列以 `fakes.write_ab_manifest(tmp_path, **overrides)` 建出後預期 `HarnessError.code == "HARNESS_MANIFEST_INVALID"`）：

| case | overrides |
|---|---|
| `a_not_baseline` | `{"sides.0.role": "candidate"}` |
| `l3_missing_acceptance` | `{"layer": "l3", "acceptance": None}` |
| `l3_missing_endpoint` | `{"layer": "l3", "endpoint": None}` |
| `l3_failure_retention_systemd` | `{"layer": "l3", "failure_retention": "systemd-user"}` |
| `l2_without_activate` | `{"layer": "l2", "sides.0.activate_argv": []}` |
| `l15_with_hardware` | `{"dut.kind": "hardware"}` |
| `adapter_layer_mismatch` | `{"suite.adapter": "l2_regression"}` |
| `docker_without_digest` | `{"dut.kind": "docker", "dut.image_digest": None}` |
| `cli_exe_not_component` | `{"sides.1.cli_exe": "/usr/bin/serialwrap"}` |
| `reverify_wrong_order` | `{"reverify.enabled": True, "reverify.order": ["A", "B"]}` |
| `unknown_template_key` | `{"suite.extra_args": ["{python}"]}` |
| `stage2_cli_differs` | `{"stage": "2.1", "sides.1.cli_sha_override": "0"*64}`（替身把 B 的 CLI 換成不同檔） |
| `stage3_wrong_case_set` | `{"required_case_set": "slice_2.1"}` |

另加：`test_render_argv_substitutes`（`render_argv(["{cli_exe}","x"],{"cli_exe":"/b"}) == ["/b","x"]`）、`test_contract_revision_mismatch`（git 回不同 rev → 違規字串前綴 `"contract_revision mismatch:"`）、`test_docker_digest_mismatch`（`dut.kind="docker"`、docker inspect 回不同 Id → `"dut image digest mismatch"` 在清單中）、`test_tool_tree_drift`（改 `func-test/runner.py` → `"tool tree mismatch: func-test"`）、`test_invariant_not_covered`（`case_ids` 去掉唯一引用 `no_auto_resend` 的案例，並同步從 lock 外加入 → `"invariant not covered: no_auto_resend"`）、`test_destructive_needs_authorization`（`layer="l2"`、`case_ids` 含 destructive 案例、`authorization=None` → `"destructive case without authorization: <id>"`）、`test_windows_com0com_requires_allowlist`（`platform="windows"`、`dut.transport="com0com-pair"`、`test_device_allowlist=[]` → 違規）、`test_runtime_digest_ignores_stage_and_marker`（同一組 binary 以 `stage="2.4"` 與 `stage="3"` 建 bundle → `runtime_digest` 相同；換 CLI binary → 不同）。

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_ab_manifest.py` → Expected: FAIL（`ImportError: cannot import name 'ab_manifest'`）。
- [ ] **Step 3：實作**（agy）依 Interfaces 與規則；sha256 一律 `jsonio.sha256_file`；tree hash 與 `fakes.tree_sha256` 同演算法但獨立實作（不 import 替身）；`source_sha256` 為 manifest 檔本身的 sha256，寫入報告。依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 A/B manifest 載入、凍結與案例涵蓋檢查`。

---

## Task 4：A→B 資源釋放驗證（`tools/stage3/release_check.py`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-04-release-check.md`
**BLOCKED-OWNER:** BO-P9-15（`WindowsProbe.device_exclusive_open` 在真實 COM 上的實測）；BO-P9-01 的前置。

規格 §15.9.1 第 3 步：「停止 A 的 daemon，確認程序、handle、鎖及相關子程序釋放；釋放不明時立即停止，不開始 B」。

**Files:**
- Create: `tools/stage3/release_check.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_release_check.py: {class: core_portable, resources: [memory_only]}`、`test_stage3_release_check_posix.py: {class: posix_only, resources: [posix_pty, process_rpc]}`、`test_stage3_release_check_windows.py: {class: windows_native, resources: [memory_only, windows_com]}`）
- Test: `tests/test_stage3_release_check.py`（替身驅動的判定邏輯）、`tests/test_stage3_release_check_posix.py`（`PosixProbe` 實測）、`tests/test_stage3_release_check_windows.py`（`WindowsProbe` 實測）

**Interfaces — Consumes:** `ab_manifest.ReleaseSpec`、`proc.run_json`。
**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class PreStopSnapshot:
    daemon_pid: int | None              # 自 `daemon status` 取得；daemon 未執行為 None
    descendant_pids: tuple[int, ...]
    match_paths: tuple[str, ...]        # daemon_exe realpath，加上 manifest daemon.entry 中的腳本絕對路徑（Python daemon）
    endpoints: tuple[str, ...]
    device_paths: tuple[str, ...]
    lock_paths: tuple[str, ...]
    captured_at: float
@dataclass(frozen=True)
class CheckOutcome:
    name: str        # "daemon_pid" | "descendant" | "leaked_daemon" | "endpoint" | "lock" | "device_holders" | "device_exclusive"
    target: str
    status: str      # "released" | "held" | "unknown"
    detail: str
@dataclass(frozen=True)
class ReleaseResult:
    released: bool
    checks: tuple[CheckOutcome, ...]
    elapsed_s: float
class ReleaseProbe(Protocol):
    def pid_alive(self, pid: int) -> bool | None: ...
    def descendants(self, pid: int) -> tuple[int, ...] | None: ...
    def pids_by_exe(self, match_paths: Sequence[str]) -> tuple[int, ...] | None: ...
    def endpoint_connectable(self, endpoint: str) -> bool | None: ...
    def lock_acquirable(self, path: str) -> bool | None: ...
    def device_holders(self, path: str) -> tuple[tuple[int, ...], int] | None: ...   # (持有者 pids, 無法讀取的他 uid 程序數)
    def device_exclusive_open(self, path: str) -> bool | None: ...
class PosixProbe: ...     # 實作 ReleaseProbe
class WindowsProbe: ...   # 實作 ReleaseProbe（ctypes）
def default_probe() -> ReleaseProbe        # os.name == "nt" → WindowsProbe()，否則 PosixProbe()
def capture_pre_stop(cli_exe: Path, env: Mapping[str, str], spec: ReleaseSpec, *, daemon_exe: Path, entry_scripts: Sequence[str] = (),
                     run: Runner = run_cmd, probe: ReleaseProbe | None = None, clock: Callable[[], float] = time.monotonic) -> PreStopSnapshot
def snapshot_from_endpoints(endpoints_json: Path, spec: ReleaseSpec, *, match_paths: Sequence[str],
                            clock: Callable[[], float] = time.monotonic) -> PreStopSnapshot
    # L1.5：func-test 自管 daemon，suite 結束時 daemon 已停；daemon_pid=None，endpoints／device_paths／lock_paths 取 spec 與
    # suite adapter 寫的 endpoints.json（{"endpoints":[…],"device_paths":[…],"lock_paths":[…]}，Task 5B）之聯集（保序去重）
def verify_released(snap: PreStopSnapshot, spec: ReleaseSpec, *, probe: ReleaseProbe | None = None,
                    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> ReleaseResult
```

判定規則（每次輪詢全部重測；全部 `released` 即提早結束；逾 `spec.timeout_s` 以最後一次結果為準）：

| check | released 條件 | held 條件 | unknown |
|---|---|---|---|
| `daemon_pid` | `pid_alive(pid) is False` | `True` | `None` |
| `descendant`（每個 pid） | `pid_alive is False` | `True` | `None` |
| `leaked_daemon` | `pids_by_exe(match_paths) == ()`（排除本程序 pid） | 非空 | `None` |
| `endpoint`（每個） | `endpoint_connectable is False` | `True` | `None`（例如連線逾時） |
| `lock`（每個） | `lock_acquirable is True` | `False` | `None` |
| `device_holders`（POSIX，每個） | `((), 0)`；或 `((), n>0)` 且 `spec.accept_unreadable_holders` | pids 非空 | `None`；或 `unreadable>0` 且未設 `accept_unreadable_holders`（部分掃描不是安全證明，§6.2） |
| `device_exclusive`（Windows，且 `spec.device_open_probe`） | `True` | `False`（`ERROR_ACCESS_DENIED`=5） | `None`（裝置不存在或其他錯誤） |

`released = all(c.status == "released")`；**任何 `unknown` 即 `released=False`**。每個 `device_holders` outcome 的 `detail` 記 `unreadable=<n>` 與 `accept_unreadable_holders=<bool>`，Task 6 原樣寫入報告。

平台實作：
- `PosixProbe.pid_alive`：讀 `/proc/<pid>/stat`；不存在 → False；狀態 `Z` → False；其他 → True；`PermissionError` → True（存在）。`descendants`：掃 `/proc/[0-9]*/stat` 建 ppid 樹。`pids_by_exe`：`/proc/<pid>/exe` realpath 或 `/proc/<pid>/cmdline` 任一參數 realpath 屬於 `match_paths`。`endpoint_connectable`：`unix://` 前綴或絕對路徑走 `AF_UNIX`；`tcp://h:p` 走 `AF_INET`；`ConnectionRefusedError`／`FileNotFoundError` → False；`socket.timeout`（0.5 s）→ None。`lock_acquirable`：檔案不存在 → True；`os.open(O_RDWR)` 後 `fcntl.flock(LOCK_EX|LOCK_NB)` 成功即 `LOCK_UN` → True；`BlockingIOError` → False；其他 `OSError` → None。`device_holders`：比對 `/proc/<pid>/fd/*` readlink 與 `os.path.realpath(path)`；`PermissionError` 的程序計入 `unreadable`，回 `(pids, unreadable)`；掃描本身失敗（`/proc` 不可列舉）→ None。`device_exclusive_open`：`spec.device_open_probe` 為 True 時 `os.open(path, O_RDWR|O_NOCTTY|O_NONBLOCK)` 成功即關閉 → True，`EBUSY` → False，其他 → None；為 False 時不呼叫。
- `WindowsProbe`：`pid_alive` 以 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE)`＋`GetExitCodeProcess`（`STILL_ACTIVE`=259 → True；`ERROR_INVALID_PARAMETER` → False）；`descendants`／`pids_by_exe` 以 `CreateToolhelp32Snapshot`＋`QueryFullProcessImageNameW`；`endpoint_connectable` 只接受 `tcp://`；`lock_acquirable` 以 `msvcrt.locking(fd, LK_NBLCK, 1)`（與 SC-10 的 `LockFileEx` 互斥相容）；`device_holders` 恆回 None（Windows 無免權限 holder 列舉）且 Windows 上此 check 不納入，改由 `device_exclusive`；`device_exclusive_open` 以 `CreateFileW("\\\\.\\COMn", GENERIC_READ|GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)`，成功即 `CloseHandle`。
- 實機（`dut.kind=="hardware"`）預設 `device_open_probe: false`：開啟真實 USB-UART 可能改變 DTR／RTS 而影響 DUT；改由 `daemon_pid`、`descendant`、`leaked_daemon` 推定 handle 已隨程序結束關閉。com0com DUT-side pair 的 L1.5 設為 true。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）三個檔案；`tests/test_stage3_release_check.py`（`pytestmark = [core_portable, memory_only]`）與 `tests/test_stage3_release_check_posix.py`（`pytestmark = [posix_only, posix_pty, process_rpc]`）的關鍵測試：

```python
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import stage3_fakes as fakes
from tools.stage3 import release_check
from tools.stage3.ab_manifest import ReleaseSpec


def _spec(**kw):
    base = dict(timeout_s=1.0, poll_s=0.2, endpoints=("/tmp/sw-x.sock",), device_paths=(), lock_paths=(), device_open_probe=False,
                accept_unreadable_holders=False)
    base.update(kw)
    return ReleaseSpec(**base)


def _snap(pid=4242, **kw):
    base = dict(daemon_pid=pid, descendant_pids=(), match_paths=("/b/bin/serialwrapd",), endpoints=("/tmp/sw-x.sock",),
                device_paths=(), lock_paths=(), captured_at=0.0)
    base.update(kw)
    return release_check.PreStopSnapshot(**base)


def test_fake_probe_conforms_to_protocol():
    names = {n for n in dir(release_check.ReleaseProbe) if not n.startswith("_")}
    assert names <= set(dir(fakes.FakeProbe))


def test_unreadable_holders_are_unknown():
    clock = fakes.FakeClock()
    probe = fakes.FakeProbe(holders={"/dev/ttyUSB0": [((), 2)]})
    snap = _snap(device_paths=("/dev/ttyUSB0",))
    r = release_check.verify_released(snap, _spec(device_paths=("/dev/ttyUSB0",)), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False and [c.status for c in r.checks if c.name == "device_holders"] == ["unknown"]
    r2 = release_check.verify_released(snap, _spec(device_paths=("/dev/ttyUSB0",), accept_unreadable_holders=True), probe=probe,
                                       clock=clock.now, sleep=clock.sleep)
    assert r2.released is True


def test_unknown_is_never_released():
    clock = fakes.FakeClock()
    probe = fakes.FakeProbe(pid_alive={4242: [None]})
    r = release_check.verify_released(_snap(), _spec(), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert {c.name: c.status for c in r.checks}["daemon_pid"] == "unknown"


def test_polls_until_released():
    clock = fakes.FakeClock()
    probe = fakes.FakeProbe(pid_alive={4242: [True, True, False]}, endpoint={"/tmp/sw-x.sock": [True, False]})
    r = release_check.verify_released(_snap(), _spec(timeout_s=5.0), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is True
    assert 0.4 <= r.elapsed_s <= 0.6


def test_timeout_reports_held_lock():
    clock = fakes.FakeClock()
    probe = fakes.FakeProbe(lock={"/r/serialwrapd.lock": [False]})
    r = release_check.verify_released(_snap(lock_paths=("/r/serialwrapd.lock",)), _spec(lock_paths=("/r/serialwrapd.lock",)),
                                      probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert [c.status for c in r.checks if c.name == "lock"] == ["held"]
    assert r.elapsed_s >= 1.0


def test_device_open_probe_not_called_when_disabled():
    clock = fakes.FakeClock()
    probe = fakes.FakeProbe()
    release_check.verify_released(_snap(device_paths=("COM7",)), _spec(device_paths=("COM7",), device_open_probe=False),
                                  probe=probe, clock=clock.now, sleep=clock.sleep)
    assert not any(name == "device_exclusive_open" for name, _ in probe.calls)


# tests/test_stage3_release_check_posix.py
def test_posix_probe_sees_pty_holder_then_release():
    import pty
    master, slave = pty.openpty()
    slave_path = os.ttyname(slave)
    os.close(slave)
    child = subprocess.Popen([sys.executable, "-c",
                              f"import os,time; fd=os.open({slave_path!r}, os.O_RDWR|os.O_NOCTTY); time.sleep(30)"])
    probe = release_check.default_probe()
    try:
        deadline = time.monotonic() + 5
        while child.pid not in (probe.device_holders(slave_path) or ((), 0))[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.pid in probe.device_holders(slave_path)[0]
        assert probe.pid_alive(child.pid) is True
        child.kill()
        child.wait()
        assert probe.pid_alive(child.pid) is False
        assert child.pid not in (probe.device_holders(slave_path) or ((), 0))[0]
    finally:
        if child.poll() is None:
            child.kill()
        os.close(master)
```

其餘：

| 測試名 | 檔案 | 斷言 |
|---|---|---|
| `test_descendant_alive_blocks` | core | `descendant_pids=(9,)`、`pid_alive={9:[True]}` → `released is False`，`descendant` 為 `held` |
| `test_leaked_daemon_blocks` | core | `by_exe={"/b/bin/serialwrapd":[(77,)]}` → `leaked_daemon` 為 `held` |
| `test_endpoint_timeout_is_unknown` | core | `endpoint={...:[None]}` → `unknown`、`released False` |
| `test_capture_pre_stop_reads_daemon_pid` | core | RecordingRunner `cli:daemon status` 回 `{"ok":true,"running":true,"pid":55}` → `snap.daemon_pid == 55` |
| `test_capture_pre_stop_daemon_down` | core | `daemon status` rc=2 → `daemon_pid is None`，`match_paths` 仍含 daemon_exe realpath |
| `test_snapshot_from_endpoints_merges` | core | `endpoints.json` 列 `/tmp/a.sock`、spec 列 `/tmp/b.sock` → `snap.endpoints == ("/tmp/b.sock","/tmp/a.sock")`、`daemon_pid is None` |
| `test_posix_unreadable_counted` | posix | 以 monkeypatch 讓一個 `/proc/<pid>/fd` 丟 `PermissionError` → 回傳 `unreadable == 1` |
| `test_posix_lock_probe` | posix | 子程序 `flock` 持有暫存檔 → `lock_acquirable is False`；子程序結束 → `True` |
| `test_posix_unix_socket_probe` | posix | 綁定 AF_UNIX 監聽 → `True`；關閉並刪檔 → `False` |
| `test_posix_pids_by_exe_matches_cmdline_script` | posix | 子程序 `python3 /tmp/x/serialwrapd.py` → `pids_by_exe(["/tmp/x/serialwrapd.py"])` 含其 pid |
| `test_windows_lock_probe` | windows | 子程序 `msvcrt.locking` 持有 → `False`；結束 → `True` |
| `test_windows_pid_alive` | windows | 子程序存活 `True`、結束後 `False` |
| `test_windows_com_exclusive` | windows | 讀 `SW_TEST_COM_PAIR=COMa:COMb`；未設定 → `pytest.skip("cap:windows_com 證據不足")`；子程序以 share=0 開 COMa → `device_exclusive_open("COMa") is False`；關閉後 `True` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_release_check.py tests/test_stage3_release_check_posix.py` → Expected: FAIL（`ImportError`）。Windows 部分以 `py -3.12 -m pytest -q tests/test_stage3_release_check.py tests/test_stage3_release_check_windows.py`（經 WSL interop 在擁有者 Windows 主機本機目錄執行，arch §9.4）→ Expected: FAIL。
- [ ] **Step 3：實作**（luna）依判定表與平台實作。輪詢迴圈：`start = clock()`；每輪 `outcomes = _evaluate(...)`；全部 released → 回傳；`clock() - start >= timeout_s` → 回傳最後 outcomes；否則 `sleep(poll_s)`。`elapsed_s = clock() - start`。ctypes 宣告集中在 `WindowsProbe` 類別內，模組頂層 import 不得在 POSIX 失敗。三個測試檔依 P0 T14 各帶 `pytestmark`（core_portable／posix_only／windows_native 分檔）。
- [ ] **Step 4：Run** Linux（core＋posix 檔）與 Windows（core＋windows 檔；`windows_com` 案例無 COM pair 時 skip）→ Expected: PASS；`test_windows_com_exclusive` 在無 COM pair 時顯示 skipped 並於 PR 描述標「證據不足（BO-P9-15）」，不得記為 PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 A/B 之間的程序、端點、鎖與裝置釋放驗證`。

---

## Task 5：乾淨 DUT 狀態與實例隔離（`tools/stage3/dut_state.py`）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-05-dut-state.md`
**BLOCKED-OWNER:** BO-P9-02、BO-P9-03（實機 `dut.reset_argv`／`fingerprint_argv` 由擁有者依 bench 提供並實跑）。

L1.5 的 DUT 與 daemon 由 func-test runner 每個 case 自行啟停（P2 Task 10），本模組**不**啟動模擬器；只負責每側的全新實例目錄、artifact 選定 env，以及 L2／L3 bench 的乾淨狀態檢查。

**Files:**
- Create: `tools/stage3/dut_state.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_dut_state.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_dut_state.py`

**Interfaces — Consumes:** `ab_manifest.AbManifest`／`AbSide`／`render_argv`、`proc.Runner`。
**Interfaces — Produces:**

```python
ISOLATED_DIR_KEYS: tuple[str, ...] = ("SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_WAL_DIR", "SERIALWRAP_CONFIG_DIR",
                                       "SERIALWRAP_LOG_DIR", "SERIALWRAP_EVENTS_DIR", "SERIALWRAP_EVENTS_RUNTIME_DIR",
                                       "SERIALWRAP_BY_ID_DIR", "SERIALWRAP_BY_PATH_DIR")   # 與 tests/conftest.py 強制覆寫的 9 個一致
EXPLICIT_KEYS: tuple[str, ...] = ("SERIALWRAP_PROFILE_DIR", "SERIALWRAP_DATA_DIR", "SERIALWRAP_TTYMCU_PATH", "SERIALWRAP_EVENTS_LOG_PATH")
ARTIFACT_KEYS: tuple[str, ...] = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL")   # arch §9.2；P2 func-test／contract_blackbox 依此選 artifact
POPPED_KEYS: tuple[str, ...] = ("SERIALWRAP_ENDPOINT", "SERIALWRAP_TEST_DEVICE_ALLOWLIST")
@dataclass(frozen=True)
class Instance:
    side: str
    round: int
    root: Path
    env: Mapping[str, str]
    report_dir: Path
def side_env(base_env: Mapping[str, str], *, side: str, round: int) -> dict[str, str]   # 加 SW_STAGE3_SIDE、SW_STAGE3_ROUND
def artifact_env(m: AbManifest, side: AbSide) -> dict[str, str]
    # {"SERIALWRAP_CLI_EXE": str(side.cli_exe), "SERIALWRAP_DAEMON_EXE": str(side.daemon_exe), "SERIALWRAP_AB_LABEL": f"{m.run_id}-{side.label}"}
def fresh_instance(m: AbManifest, side: AbSide, *, round: int, base_dir: Path, base_env: Mapping[str, str] | None = None) -> Instance
def verify_clean(m: AbManifest, inst: Instance, side: AbSide, *, run: Runner = run_cmd) -> list[str]
def cleanup_instance(inst: Instance) -> list[str]
```

行為：
- `fresh_instance`：`root = base_dir / f"r{round}-{side.label}"`；`root` 已存在 → `HarnessError("HARNESS_DUT_NOT_CLEAN","instance root reused")`（保證 B 不沿用 A 的目錄）；`report_dir = root / "report"`。env＝`base_env` 去除 `POPPED_KEYS` 後加 `side_env` 與 `artifact_env`（三層皆設，避免任何層退回預設 Python 基線，Task 6 另以 `verify_run_identity` 核對）。
  - `l1.5`：建立 `root/{state,run,wal,config,log,events,events-run,by-id,by-path,profiles,data}`；設定 9 個 `ISOLATED_DIR_KEYS` 與 `EXPLICIT_KEYS`（`TTYMCU_PATH=root/run/dev/ttyMCU`、`EVENTS_LOG_PATH=root/events/events.ndjson`）；POSIX `RUN_DIR` 改用 `tempfile.mkdtemp(prefix="sw-ab-", dir="/tmp")`（AF_UNIX 107 字元上限，比照 regression `throwaway_env`）並記錄於 `root/run-dir.txt`；複製 `m.profile_files` 到 `root/profiles`；`platform=="windows"` 且 `m.test_device_allowlist` 非空 → `SERIALWRAP_TEST_DEVICE_ALLOWLIST=",".join(...)`（SC-19／OD-11）。
  - `l2`／`l3`：不隔離目錄（使用 bench 已安裝實例），只設 side／artifact env；artifact 的實際選定由 Task 5B adapter 寫 testbed／realhw config 完成。
- `verify_clean` 回傳違規清單：
  - `l1.5`：`state.json` 存在 → `"state.json present"`；`wal/` 有檔 → `"wal not empty"`；`run/` 除 `dev/` 外有檔 → `"run dir not empty"`。
  - `l2`／`l3`：先執行 `reset_argv`（rc≠0 → `"dut reset failed rc=<n>"`）；`fingerprint_argv` stdout 解析為 JSON object，對 `expected_fingerprint` 每鍵 `re.fullmatch`，不符 → `"fingerprint mismatch: <key>"`；`[cli,"session","list"]`（`--endpoint m.endpoint`，若有）中 `m.dut.boards` 每塊板 `state == "READY"`，否則 `"board not READY: <com>"`；`state.json` 的 `bindings` 不得含 `/tmp/`（與 realhw `_state_polluted` 同規則）→ `"state polluted"`。
- `cleanup_instance`：POSIX 刪除 `run-dir.txt` 記錄的暫存 RUN_DIR；失敗回 `"run dir cleanup failed"`。

**Steps:**

- [ ] **Step 1：寫失敗測試**（agy）`tests/test_stage3_dut_state.py`（`pytestmark = [core_portable, memory_only]`）：

```python
def test_b_instance_never_reuses_a_root(tmp_path):
    m = ab_manifest.load_manifest(fakes.write_ab_manifest(tmp_path))
    a = dut_state.fresh_instance(m, m.sides[0], round=1, base_dir=tmp_path / "out")
    b = dut_state.fresh_instance(m, m.sides[1], round=1, base_dir=tmp_path / "out")
    assert a.root != b.root
    for k in dut_state.ISOLATED_DIR_KEYS:
        assert a.env[k] != b.env[k]
    assert "SERIALWRAP_ENDPOINT" not in a.env
    assert a.env["SERIALWRAP_DAEMON_EXE"] == str(m.sides[0].daemon_exe)
    assert b.env["SERIALWRAP_DAEMON_EXE"] == str(m.sides[1].daemon_exe)
    assert a.env["SERIALWRAP_CLI_EXE"] == str(m.sides[0].cli_exe)
```

| 測試名 | 斷言 |
|---|---|
| `test_reused_root_rejected` | 對同一 side、round 呼叫兩次 → 第二次 `HarnessError.code == "HARNESS_DUT_NOT_CLEAN"` |
| `test_l15_clean_passes` | 新實例 `verify_clean == []` |
| `test_leftover_state_json_detected` | 在 `root/state/state.json` 寫入 `{}` → 清單含 `"state.json present"` |
| `test_windows_allowlist_env` | `platform="windows"`、`dut.transport="com0com-pair"`、`test_device_allowlist=["COM20"]` → `env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] == "COM20"`；外層 env 帶舊值時被移除後重設 |
| `test_hardware_fingerprint_mismatch` | l2；fingerprint 回 `{"uptime_s":"9999"}`、expected `{"uptime_s":"[0-9]{1,3}"}` → `"fingerprint mismatch: uptime_s"` |
| `test_hardware_board_not_ready` | l2、`dut.boards=["COM0","COM1"]`；`cli:session list` 回 COM1 `ATTACHED` → `"board not READY: COM1"` |
| `test_hardware_reset_failure` | l2；reset rc=3 → `"dut reset failed rc=3"` |
| `test_side_env_marks_side` | `side_env({}, side="B", round=2) == {"SW_STAGE3_SIDE":"B","SW_STAGE3_ROUND":"2"}` |
| `test_cleanup_removes_tmp_run_dir` | POSIX：`cleanup_instance` 後 `run-dir.txt` 指的目錄不存在 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_dut_state.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（haiku）依行為描述；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 A/B 實例隔離、artifact 選定與乾淨 DUT 狀態檢查`。

---

## Task 5B：Suite adapter（`tools/stage3/suites/`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-05b-suite-adapters.md`
**BLOCKED-OWNER:** BO-P9-02～05（L2／L3 實跑）、BO-P9-18（release candidate 的 L3 P0＋P1＋remote）。

把既有引擎包成「一側一次」的固定呼叫，讓 Task 6 不用自由形式的 `suite_argv`（引擎各自決定報告位置、L3 還需 observer 與 gate）。adapter 只用 subprocess 呼叫既有引擎，不重寫排程、重試或報告（§3.2、§11.4）。

**Files:**
- Create: `tools/stage3/suites/__init__.py`、`tools/stage3/suites/l15_functest.py`、`tools/stage3/suites/l2_regression.py`、`tools/stage3/suites/l3_realhw.py`
- Create: `tests/fakes_suites.py`（L1.5 報告樹、TestPilot `tp-*` 目錄、realhw 報告與 release-gate.json 的產生器，供 `RecordingRunner.on` 使用；另提供 Task 6 用的 `ab_env(root, *, layer="l1.5", a_functest_rc=0, b_functest_rc=0, functest_identity="per_env"|"python_default", a_pid=4101, b_pid=4202, **manifest_overrides) -> AbEnv`，`AbEnv` 屬性 `repo`、`manifest`、`m`（已載入的 `AbManifest`）、`runner`、`a_daemon_pid`、`b_daemon_pid`、`recorded()`）
- Modify: `tools/stage3/stage3_reason_codes.json`（無新碼；沿用 `HARNESS_ARTIFACT_MISMATCH`、`HARNESS_SUITE_INFRA_ERROR`、`HARNESS_FAILURE_RETENTION_UNVERIFIED`）
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_suites.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_suites.py`

**Interfaces — Consumes:** Task 3、5；P2 func-test／TestPilot／realhw／`realhw.acceptance.{observer,gate}` 的 CLI（§0 表）。
**Interfaces — Produces:**

```python
# tools/stage3/suites/__init__.py
@dataclass(frozen=True)
class SuiteRun:
    status: str                  # "PASS" | "FAIL" | "INFRA_ERROR"
    rc: int | None
    report_dir: Path             # 本側證據根目錄（adapter 保證所有引擎產出都在這裡）
    identity: Mapping[str, Any]  # 實際受測身分（自引擎報告讀出）
    notes: tuple[str, ...]
def run_side(m: AbManifest, side: AbSide, inst: Instance, *, run: Runner, spawn: Spawner, now: Callable[[], str]) -> SuiteRun
    # 依 m.suite.adapter 分派到下列三個模組
def verify_run_identity(m: AbManifest, side: AbSide, run: SuiteRun) -> list[str]
    # 比對 run.identity 與 side.manifest_doc.components；違規字串：
    # "cli sha256 mismatch"、"daemon implementation mismatch"、"daemon sha256 mismatch"、"cli not pinned"、"release manifest mismatch"
REPORT_ENDPOINTS = "endpoints.json"   # 由 adapter 寫在 report_dir：{"endpoints":[…],"device_paths":[…],"lock_paths":[…]}

# l15_functest.py
def run(m, side, inst, *, run, spawn, now) -> SuiteRun
# l2_regression.py
def run(m, side, inst, *, run, spawn, now) -> SuiteRun
# l3_realhw.py
def run(m, side, inst, *, run, spawn, now) -> SuiteRun
```

各 adapter 行為：
- **L1.5（`l15_functest`）**：`run([sys.executable, repo/"func-test/runner.py", "--mode", "bridge", "--dut-mode", <docker 時 "docker" 否則 "local-process">, "--report-dir", report_dir, "--selftest-report", m.suite.selftest_report.path, *case 選擇旗標, *extra_args], env=inst.env, timeout=m.suite.timeout_s)`；rc 0→PASS、1→FAIL、其他或逾時→INFRA_ERROR。之後四項檢查（任一不符→INFRA_ERROR，notes 列出）：(1) leaked-daemon 掃描——以 Task 4 `pids_by_exe([side.daemon_exe, side.cli_exe 的 realpath, 及 entry 腳本])` 為空；(2) 每個 `<case>/<mode>/<n>/bridge-manifest.json` 的 `closed_cleanly is True` 且 `failure is None`；(3) 每個 case 的 `wire.ndjson` 中 `dut_generation` 只出現 1（該 case 宣告 DUT restart／reboot 的除外：`cases.yaml` 的 case 屬性決定），即 DUT 為每 case 全新；(4) 收集所有 `manifest.json.transport.port_id`／daemon socket 路徑寫入 `report_dir/endpoints.json` 供 Task 4 `snapshot_from_endpoints`。`identity`：所有 case `manifest.json.artifacts` 必須一致，取 `cli.sha256`、`daemon.implementation`、`daemon.sha256`、`daemon.tree_sha256`、`release_manifest_sha256`、`transport.mode`、`tool.tree_sha256`。
- **L2（`l2_regression`）**：從 `m.suite.testbed_template` 產生本側 `inst.root/testbed.yaml`（只覆寫 `serialwrap_exe=side.cli_exe`、`bench_lock_path`；其他鍵原樣）；記錄 `~/b-log/regression-reports/` 下既有 `tp-*` 清單；`run(["testpilot","run","serialwrap_regression","--testbed",testbed, *case 選擇旗標, *extra_args], env=inst.env, timeout=...)`；結束後找出**新出現且唯一**的 `tp-*` 目錄（0 或 >1 個 → INFRA_ERROR `"testpilot report dir ambiguous"`），整個複製到 `report_dir/testpilot/`。status 依 `report.json` 逐案判定交給 Task 7（rc 只作 INFRA 判斷：非 0 且無 report.json → INFRA_ERROR）。`identity`：`report.json.meta.cli_identity`（`resolved`、`sha256`、`pinned`）與 preflight 記錄的 `daemon status.version`。
- **L3（`l3_realhw`）**：依 P2 Task 15 README 的固定程序：(1) 從 `m.suite.realhw_config_template` 產生 `inst.root/realhw-config.json`（覆寫 `serialwrap_exe`、`endpoint=m.endpoint`、`bench_lock_path`）；(2) 監管檢查——`[cli,"supervision-mode"]` stdout 必須為 `on-demand`，且 `[cli,"service","status"]` 回報無已註冊監管（Linux 無 systemd unit 啟用；Windows 另以 `schtasks /Query /TN serialwrap*` 確認無工作），否則 `HARNESS_FAILURE_RETENTION_UNVERIFIED`；全部 RPC 類 CLI 呼叫（`daemon`／`session`／`cmd`）與引擎設定皆帶明確 `--endpoint m.endpoint`，使 SC-18 首次需求自動啟動不觸發（死掉的 daemon 不會被 worker 下一個命令救活）；(3) `spawn([sys.executable,"-m","realhw.acceptance.observer","--serialwrap-exe",side.cli_exe,"--boards",",".join(m.dut.boards),"--interval-s",str(workload.observer_interval_s),"--out",report_dir/"observer.ndjson"])`（`m.platform=="windows"` 另加 `"--excl-dir",report_dir/"excl","--excl-probe-interval-s",str(workload.windows_excl_probe_interval_s)`）；(3a) Windows：`run([sys.executable,"-m","realhw.acceptance.winexcl","handoff","--phase","pre","--serialwrap-exe",side.cli_exe,"--boards",…,"--observer-header",report_dir/"observer.ndjson","--excl-dir",report_dir/"excl"])`，exit 0 才進 (4)；exit 1（`violated`）→ 不跑 (4)、證據照存並直接執行 (7)，由 gate 判 `safety_double_holder` FAIL；exit 4 → `INFRA_ERROR`（不得跳過 pre 直接長跑）（P2 Task 15 Windows 獨占證據契約，R1-F7）；(4) `run([sys.executable,"-m","realhw","--config",cfg,"--tier",m.suite.tier,"--report-dir",report_dir/"realhw", *extra_args], timeout=m.suite.timeout_s)`；(4a) Windows：同 (3a) 但 `--phase post`，結果照存（判定交給 gate）；(5) 停止 observer（terminate→wait 10 s→kill）；(6) 複製 daemon `wal_path` 所在目錄到 `report_dir/wal/`；(7) `run([sys.executable,"-m","realhw.acceptance.gate","--report-dir",report_dir/"realhw","--case-dir",<lr-mixed case dir>,"--acceptance",m.acceptance.path,"--platform",m.platform,"--mode","baseline" if side.role=="baseline" else "candidate","--observer",…,"--wal-dir",…,("--baseline-gate",<A 側 release-gate.json>) 僅 B 側,("--excl-evidence",report_dir/"excl") 僅 Windows,"--out",report_dir/"release-gate.json"])`。gate exit 0／1／4／5 皆屬正常完成（判定交給 Task 7），其他 → INFRA_ERROR。`identity`：`report.json.meta.cli_identity` 與 `release-gate.json.run.{cli_identity,daemon_version}`；報告記錄 `"explicit_endpoint": m.endpoint`、`"supervisor_absent": true`。`m.suite.tier` 不是 `longrun`（例如 `p0,p1,remote`）時跳過 (3)、(3a)、(4a)、(6)、(7)，供 `S3-L3-P0P1-REMOTE-*` 以 `evidence ingest --kind realhw-report` 登錄。
- `verify_run_identity`：`identity.cli.sha256 != components.cli.sha256` → `"cli sha256 mismatch"`；L1.5 的 `daemon.implementation` 或 `daemon.sha256`（Rust：等於 `components.daemon.launcher.sha256`；Python：`daemon.tree_sha256` 等於 `components.daemon.package.tree_sha256`）不符 → 對應字串；L2／L3 `pinned is not True` → `"cli not pinned"`；`release_manifest_sha256`（有記錄時）≠ `side.release_manifest.sha256` → `"release manifest mismatch"`。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_suites.py`（`pytestmark = [core_portable, memory_only]`），以 `fakes_suites` 透過 `RecordingRunner.on` 產生引擎輸出：

| 測試名 | 斷言 |
|---|---|
| `test_l15_passes_artifact_env_and_report_dir` | functest 呼叫的 env 含兩側各自的 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`，argv 含 `--report-dir <inst.report_dir>` |
| `test_l15_identity_mismatch_detected` | 替身 functest 一律寫 Python 基線身分（模擬兩側都跑到預設 Python）→ B 側 `verify_run_identity` 含 `"daemon implementation mismatch"` |
| `test_l15_unclean_bridge_is_infra` | 某 case `bridge-manifest.json.closed_cleanly=false` → `status == "INFRA_ERROR"` |
| `test_l15_reused_dut_is_infra` | 某 case `wire.ndjson` 出現 `dut_generation: 2` 且該 case 未宣告 restart → INFRA_ERROR |
| `test_l15_leaked_daemon_is_infra` | FakeProbe `by_exe` 回 `(91,)` → INFRA_ERROR、notes 含 `leaked daemon` |
| `test_l15_writes_endpoints_json` | `report_dir/endpoints.json` 存在且列出 case 的 socket 路徑 |
| `test_l2_copies_new_tp_dir` | 替身在 `regression-reports/` 新建 `tp-1`（既有 `tp-0`）→ `report_dir/testpilot/report.json` 存在，且 `tp-0` 未被複製 |
| `test_l2_ambiguous_tp_dir` | 新建兩個 `tp-*` → INFRA_ERROR `"testpilot report dir ambiguous"` |
| `test_l2_testbed_pins_side_exe` | 產生的 `testbed.yaml` 的 `serialwrap_exe == str(side.cli_exe)`，其餘鍵與模板相同 |
| `test_l3_runs_observer_engine_wal_gate_in_order` | 呼叫序：`supervision-mode` → `service status` → observer spawn → `realhw` → observer terminate → gate；B 側 gate argv 含 `--baseline-gate` 指向 A 側檔案；`m.platform=="linux"` 時 argv 皆不含 `winexcl`／`--excl-dir`／`--excl-evidence` |
| `test_l3_windows_exclusive_evidence_steps`（R1-F7） | `m.platform=="windows"`：呼叫序為 observer spawn（argv 含 `--excl-dir <report>/excl` 與 `--excl-probe-interval-s 300`）→ `winexcl handoff --phase pre` → `realhw` → `winexcl handoff --phase post` → observer terminate → gate（argv 含 `--excl-evidence <report>/excl`）；pre exit 4 → 結果 `INFRA_ERROR` 且 `realhw` 未被呼叫；pre exit 1 → `realhw` 未被呼叫、gate 仍被呼叫 |
| `test_l3_explicit_endpoint_everywhere` | 所有 `daemon`／`session`／`cmd` 類 `cli` 呼叫 argv 含 `--endpoint m.endpoint`；`realhw-config.json.endpoint == m.endpoint` |
| `test_l3_supervisor_present_refused` | `service status` 回已註冊 unit → `HARNESS_FAILURE_RETENTION_UNVERIFIED`，不執行 realhw |
| `test_l3_unpinned_cli_mismatch` | `report.json.meta.cli_identity.pinned=false` → `verify_run_identity` 含 `"cli not pinned"` |
| `test_l3_p0p1_tier_skips_gate` | `tier="p0,p1,remote"` → 無 observer、無 gate 呼叫 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_suites.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依行為；`case 選擇旗標` 依各引擎既有參數（func-test `--case`、TestPilot 的 case filter、realhw `--case`）由 `m.case_ids` 產生，引擎無單案選擇時改以完整集合執行並在 notes 記錄。依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 L1.5／L2／L3 suite adapter 與實際受測身分核對`（files 含 `tests/fakes_suites.py`）。

---

## Task 6：Sequential A/B 執行器（`tools/stage3/ab_runner.py`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-06-ab-runner.md`
**BLOCKED-OWNER:** BO-P9-01（Windows L1.5 原生 COM 實跑）、BO-P9-02／03（L2 實跑）、BO-P9-04／05（L3 48h 實跑，約 9 天 bench）、BO-P9-16（A→B 之間 bench 保留）。本環境只能產出 Linux L1.5（PTY／Docker DUT）A/B 證據，登錄 `S3-L15-RELEASE-AB-LINUX`。

**Files:**
- Create: `tools/stage3/ab_runner.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_ab_runner.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_ab_runner.py`

**Interfaces — Consumes:** Task 3、4、5、5B、7、7B；Task 15 `od09_freeze.check_frozen_before`。
**Interfaces — Produces:**

```python
VERDICT_PRECEDENCE: tuple[str, ...] = ("FAIL", "BASELINE_INVALID", "INFRA_ERROR", "ABORTED", "PASS")
@dataclass(frozen=True)
class SideResult:
    label: str
    role: str
    round: int
    suite: SuiteRun | None              # 未執行為 None
    identity_violations: tuple[str, ...]
    report_dir: Path
    clean_violations: tuple[str, ...]
    pre_stop: PreStopSnapshot | None
    release: ReleaseResult | None
    cleanup_violations: tuple[str, ...]
    started_at: str
    finished_at: str
    abort_code: str | None
@dataclass(frozen=True)
class RoundResult:
    round: int
    order: tuple[str, str]
    diff: dict | None                   # Task 7B（l1.5）、compare_l2_reports（l2）、compare_l3（l3）的輸出
    verdict: str
    reason_code: str | None
    notes: tuple[str, ...]
@dataclass(frozen=True)
class AbRunResult:
    verdict: str
    reason_code: str | None
    manifest_sha256: str
    sides: tuple[SideResult, ...]
    rounds: tuple[RoundResult, ...]
    report_path: Path
def run_ab(manifest_path: Path, *, out_dir: Path, repo_root: Path, run: Runner = run_cmd, spawn: Spawner = proc.spawn,
           probe: ReleaseProbe | None = None, clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep, now: Callable[[], str] = jsonio.utc_now_iso) -> AbRunResult
```

**執行序（逐字實作；任何 abort 都寫出 `out_dir/ab-report.json` 後回傳）：**

1. `m = load_manifest(manifest_path)`；`out_dir.mkdir(parents=True, exist_ok=False)`（已存在 → `HARNESS_MANIFEST_INVALID`，避免覆寫舊證據）。
2. `violations = verify_frozen(m, repo_root=repo_root, run=run)`；非空 → verdict `INFRA_ERROR`，reason 依第一筆違規類別：案例涵蓋類 → `HARNESS_CASE_SET_INCOMPLETE`、授權類 → `HARNESS_AUTHORIZATION_MISSING`、其他 → `HARNESS_MANIFEST_DRIFT`；此時**不得**執行任何 `activate`、suite、`cli` 命令。
3. `m.layer == "l3"` → `od09_freeze.check_frozen_before(repo_root / m.acceptance.path, now())` 非空 → `INFRA_ERROR`、`HARNESS_OD09_NOT_FROZEN`（在 A 開始前拒絕；OD-09「任何 Rust 候選長跑開始前凍結」）。
4. `rounds = [("A","B")] + ([("B","A")] if m.reverify.enabled else [])`。複驗是否執行在 manifest 預先決定，不看第一輪結果。
5. 每輪每側依序 `_run_side(label)`：
   1. 再次 `verify_frozen`（兩側之間的漂移，例如 bundle 檔案在 A 執行期間被改）；非空 → `abort_code = "HARNESS_MANIFEST_DRIFT"`，不執行本側。
   2. `inst = dut_state.fresh_instance(...)`。
   3. `l2`／`l3`：`run(side.activate_argv, env=inst.env, timeout=600)`，rc≠0 → `abort_code = "HARNESS_SUITE_INFRA_ERROR"`。
   4. `clean = dut_state.verify_clean(...)`；非空 → `HARNESS_DUT_NOT_CLEAN`。
   5. 無 abort 時 `suite = suites.run_side(m, side, inst, ...)`（L3 的監管檢查在 adapter 內，失敗回 `HARNESS_FAILURE_RETENTION_UNVERIFIED`）；`identity_violations = suites.verify_run_identity(m, side, suite)`，非空 → `abort_code = "HARNESS_ARTIFACT_MISMATCH"`（兩側都跑到預設 Python 基線屬此類）。
   6. 不論前面成敗（`try/finally`）：`l1.5` → `pre = snapshot_from_endpoints(inst.report_dir/"endpoints.json", m.release, match_paths=…)`（檔案不存在時只用 spec，並在 notes 記 `"endpoints.json missing"`）；`l2`／`l3` → `pre = capture_pre_stop(...)` 後執行 `deactivate_argv`；`rel = verify_released(pre, m.release, ...)`；`cleanup = dut_state.cleanup_instance(inst)`。
   7. `rel.released is False` 或 `cleanup` 非空 → `abort_code = "HARNESS_RELEASE_UNCONFIRMED"`（若已有 abort_code，保留原碼並在 notes 加 `"release unconfirmed"`）。
   8. 有 `abort_code` → **不啟動下一側**、不跑比較；但已取得的產品結果不得遺失：若本輪任一已完成側為 candidate 且 `suite.status == "FAIL"`，或該側自身證據已有安全違規（L1.5 任一 `comparison.json.invariants[*].result == "FAIL"`；L3 `release-gate.json` 的 safety check FAIL），最終 verdict 為 `FAIL`、`reason_code` 為該 abort 碼、notes 加 `"aborted after product failure"`；否則 verdict `INFRA_ERROR`。
   9. `KeyboardInterrupt`：仍完成第 6 步的停止與釋放驗證，verdict `ABORTED`、`HARNESS_ABORTED`（若已有第 8 步的產品 FAIL，verdict 仍為 `FAIL`），然後重新拋出。
6. 每輪兩側完成後產生差分：`l1.5` → `ab_diff.diff_l15(a.report_dir, b.report_dir, registered=…, required_case_ids=…)`（Task 7B）；`l2` → `ab_verdict.compare_l2_reports(a_report, b_report, required_case_ids=…, cases=…, registered_case_changes=…, now=now())`（`a_report`／`b_report` 為各側 `report_dir/testpilot/report.json`）；`l3` → `ab_verdict.compare_l3(a.report_dir/"release-gate.json", b.report_dir/"release-gate.json")`。差分工具丟 `HarnessError` → `INFRA_ERROR`、`HARNESS_COMPARATOR_ERROR`。
7. `round.verdict, reason, notes = ab_verdict.combine(m.layer, a_status=a.suite.status, b_status=b.suite.status, diff=<第 6 步輸出>, registered=load_registered_changes(repo_root / m.allowed_differences.path))`；`required_case_ids, cases = ab_manifest.required_cases(m, repo_root=repo_root)`；`registered_case_changes` 為 `behavior_changes.yaml` 中 `scope=="both"` 條目的 `case_ids` 展開（case_id → change_id）。
8. 最終 verdict：所有輪 PASS 才 PASS；否則依 `VERDICT_PRECEDENCE` 取最嚴重者（FAIL 不被 INFRA 遮蔽）。
9. 寫 `ab-report.json`（`dumps_report`），鍵：`ab_manifest_sha256`、`run_id`、`layer`、`platform`、`stage`、`contract_revision`、`case_list_sha256`、`case_lock_sha256`、`required_case_set`、`case_ids`、`tools`（每項 path 與 sha256／tree_sha256）、`tool_revision`（`git rev-parse HEAD`）、`tools_stage3_tree_sha256`、`dut`（kind、transport、com_pair、image、image_digest、boards）、`authorization`、`endpoint`、`host`（`platform.platform()`、`platform.machine()`、hostname 的 sha256 前 16 hex）、`sides`（每側：`label`、`role`、`release_manifest_sha256`（artifact 身分）、`runtime_digest`、`cli_exe` realpath、`cli_sha256`（實算）、`daemon_exe` realpath、`daemon_sha256`（實算）、`bundle_version`、`daemon_status_version`（suite 期間觀測）、`suite.status`、`suite.rc`、`suite.identity`、`identity_violations`、`clean_violations`、`release.released`、`release.checks`、`cleanup_violations`）、`rounds`、`verdict`、`reason_code`、`started_at`、`finished_at`。鍵名一律 snake_case，報告與測試使用同一鍵 `release_manifest_sha256`。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_ab_runner.py`，關鍵測試：

```python
from __future__ import annotations

import json

import pytest

import fakes_suites
import stage3_fakes as fakes
from tools.stage3 import ab_runner

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _run(tmp_path, env, probe, clock):
    return ab_runner.run_ab(env.manifest, out_dir=tmp_path / "out", repo_root=env.repo, run=env.runner,
                            spawn=env.runner.spawn, probe=probe, clock=clock.now, sleep=clock.sleep, now=clock.iso)


def test_happy_path_order(tmp_path):
    env = fakes_suites.ab_env(tmp_path)
    res = _run(tmp_path, env, fakes.FakeProbe(), fakes.FakeClock())
    assert res.verdict == "PASS"
    seq = [(c["kind"], c["side"]) for c in env.recorded() if c["kind"] == "functest"]
    assert seq == [("functest", "A"), ("functest", "B")]
    a_env = [c["env"] for c in env.recorded() if c["kind"] == "functest"][0]
    assert a_env["SERIALWRAP_DAEMON_EXE"] == str(env.m.sides[0].daemon_exe)


def test_b_not_started_when_a_release_unconfirmed(tmp_path):
    env = fakes_suites.ab_env(tmp_path)
    probe = fakes.FakeProbe(by_exe={str(env.m.sides[0].daemon_exe): [(env.a_daemon_pid,)]})
    res = _run(tmp_path, env, probe, fakes.FakeClock())
    assert (res.verdict, res.reason_code) == ("INFRA_ERROR", "HARNESS_RELEASE_UNCONFIRMED")
    assert [c["side"] for c in env.recorded() if c["kind"] == "functest"] == ["A"]
    report = json.loads((tmp_path / "out" / "ab-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INFRA_ERROR"
    assert report["sides"][0]["release"]["released"] is False


def test_both_sides_on_default_python_rejected(tmp_path):
    env = fakes_suites.ab_env(tmp_path, functest_identity="python_default")
    res = _run(tmp_path, env, fakes.FakeProbe(), fakes.FakeClock())
    assert res.reason_code == "HARNESS_ARTIFACT_MISMATCH"


def test_manifest_drift_runs_nothing(tmp_path):
    env = fakes_suites.ab_env(tmp_path)
    p = env.repo / "tests" / "acceptance" / "cases.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    res = _run(tmp_path, env, fakes.FakeProbe(), fakes.FakeClock())
    assert (res.verdict, res.reason_code) == ("INFRA_ERROR", "HARNESS_MANIFEST_DRIFT")
    assert {c["kind"] for c in env.recorded()} <= {"git", "docker"}


def test_b_product_fail_survives_release_unknown(tmp_path):
    env = fakes_suites.ab_env(tmp_path, b_functest_rc=1)
    probe = fakes.FakeProbe(by_exe={str(env.m.sides[1].daemon_exe): [None]})
    res = _run(tmp_path, env, probe, fakes.FakeClock())
    assert res.verdict == "FAIL" and res.reason_code == "HARNESS_RELEASE_UNCONFIRMED"


def test_release_checked_even_when_suite_raises(tmp_path):
    env = fakes_suites.ab_env(tmp_path)
    env.runner.raise_on("functest:", RuntimeError("boom"))
    res = _run(tmp_path, env, fakes.FakeProbe(), fakes.FakeClock())
    assert res.sides[0].release is not None
    assert res.verdict == "INFRA_ERROR"
```

（`fakes_suites.ab_env` 由 Task 5B 提供：建 manifest、`RecordingRunner`，並以 `on("functest:", …)` 依 env 中的 artifact 寫出報告樹；`functest_identity="python_default"` 時不論 env 一律寫 Python 基線身分。）

其餘：

| 測試名 | 設定 | 斷言 |
|---|---|---|
| `test_reverify_always_runs_second_round` | `reverify.enabled=True` | `len(res.rounds) == 2`、第二輪 functest 側別順序 `["B","A"]`，即使第一輪 FAIL 也執行 |
| `test_reverify_requires_both_rounds_pass` | 第二輪 B functest rc=1 | `res.verdict == "FAIL"` |
| `test_fail_not_masked_by_infra` | 第一輪 B FAIL、第二輪 ab_diff 丟 `HarnessError` | `res.verdict == "FAIL"` |
| `test_drift_between_sides_aborts_b` | A 執行期間（`on` effect）改寫 B 的 `bin/serialwrapd` | `reason_code == "HARNESS_MANIFEST_DRIFT"`、無 B functest |
| `test_trimmed_case_ids_refused` | `case_ids` 少一個必要案例 | `reason_code == "HARNESS_CASE_SET_INCOMPLETE"`、無任何 functest |
| `test_l3_od09_not_frozen_refuses_before_a` | `layer="l3"`、acceptance `thresholds.resources.status: deferred` | `reason_code == "HARNESS_OD09_NOT_FROZEN"`、無任何 `activate`／suite 呼叫 |
| `test_l3_supervisor_present_refused` | `layer="l3"`、`cli:service status` 回已註冊 | `reason_code == "HARNESS_FAILURE_RETENTION_UNVERIFIED"`，A 仍執行停止與釋放驗證 |
| `test_dut_not_clean_aborts_before_suite` | `layer="l2"`、`cli:session list` 回 COM1 ATTACHED | `reason_code == "HARNESS_DUT_NOT_CLEAN"`、無 testpilot 呼叫 |
| `test_suite_rc_unknown_is_infra` | A functest rc=5 | 該側 `suite.status == "INFRA_ERROR"`、最終 `INFRA_ERROR` |
| `test_out_dir_exists_refused` | 先建立 out 目錄 | `HarnessError.code == "HARNESS_MANIFEST_INVALID"` |
| `test_side_env_propagated` | — | 所有 side 命令的 `side` 欄位非 None，B 的 `round` 為 `"1"` |
| `test_report_records_identity` | — | `report["sides"][1]["release_manifest_sha256"] == m.sides[1].release_manifest.sha256`；`cli_sha256`、`daemon_sha256`、`runtime_digest`、`tool_revision`、`tools_stage3_tree_sha256`、`dut.transport` 存在 |
| `test_keyboard_interrupt_still_releases` | functest 丟 `KeyboardInterrupt` | `pytest.raises(KeyboardInterrupt)`；之後 `ab-report.json` 存在且 verdict `ABORTED`，A 的釋放驗證已執行 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_ab_runner.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依執行序；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。接著在本環境以 P2 func-test bridge 模式與 Linux bundle 實跑一次 Linux L1.5 A/B（manifest 範本 `tests/acceptance/examples/ab_manifest.l15.example.json` 填入實際路徑），報告存 `~/b-log/stage3-ab/<run_id>/`，以 Task 14B `evidence ingest --kind ab-report` 登錄為 `S3-L15-RELEASE-AB-LINUX` 的一筆 entry（結果照實記錄；Stage 3 target artifact 尚未設定前 ingest 會拒絕，此時只保存報告、不登錄，PR 描述照實說明）。
- [ ] **Step 5：commit** `feat(stage3): 新增 Sequential A/B 執行器`。

---

## Task 7B：L1.5 A/B 差分（`tools/stage3/ab_diff.py`）

**Test author model:** codex-gpt-6-luna-max
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-07b-ab-diff.md`
**BLOCKED-OWNER:** 無（純 L1；真實報告屬 BO-P9-01）。

P2 的比較器是單次 run 的 WAL↔wire 工具（`comparison.v1`），P3 的 `ab_compare.py` 是 junit 比較；兩者都不產生 A/B 差分。本 Task 補上 L1.5 A/B 差分，只讀兩側 func-test 報告目錄，不 import P2 模組。

**Files:**
- Create: `tools/stage3/ab_diff.py`
- Modify: `contracts/ab_normalization.yaml`（P5 06 Task 20 已建立〔具名映射、`approved_revision`、`reviewed_by`〕；本 Task 只追加下列鍵、不改既有映射，變更需獨立審閱：`g1_exception_message: true`；`g2_set_paths`：依 `spec-reconciliation.md` 標為 G2 的條目列出順序未定義的 JSON 路徑（例：`event.reload` 回傳清單）；`volatile_keys: [pid, cmd_id, wall_ts, elapsed_ms, socket, created_at, started_at, finished_at, last_rx_at, last_tx_at, last_probe_at, last_state_change_at, last_ready_at, detached_at]`；`instance_root_placeholder: "<ROOT>"`；新增前需審閱，變更即改變 `contract_revision`）
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_ab_diff.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_ab_diff.py`

**Interfaces — Produces:**

```python
DIFF_SCHEMA = "stage3-ab-diff.v1"
def load_case_runs(report_dir: Path) -> dict[str, list[Path]]       # case_id -> [<case>/<mode>/<n>/ 目錄]，只取 mode=="bridge"
def normalize_response(obj: Any, *, rules: Mapping[str, Any], instance_root: str) -> Any
    # G1：error_code=="EXCEPTION" 時刪 message；G2：g2_set_paths 的清單排序；volatile_keys 刪除；字串中的 instance_root 換成 placeholder
def diff_l15(a_dir: Path, b_dir: Path, *, registered: Mapping[str, str], case_changes: Mapping[str, str],
             required_case_ids: set[str], rules_path: Path) -> dict
    # {"schema_version":DIFF_SCHEMA,"ok":bool,"integrity":{"a":bool,"b":bool},"integrity_problems":{"a":[…],"b":[…]},
    #  "safety_violations":[str],"differences":[{"case","path","kind","registered_change"}],
    #  "a_cases":{case: verdict},"b_cases":{case: verdict}}
```

規則：
- **完整性**（每側獨立）：每個 `required_case_ids` 恰出現（至少一個 run_index，取全部 repeat）；每個 run 的 `comparison.json.verdict != "INSUFFICIENT_EVIDENCE"`、`bridge-manifest.json.closed_cleanly is True`、`manifest.json` 存在；任一不符 → 該側 `integrity=false` 並列入 `integrity_problems`。
- **安全**：任一側任一 run 的 `comparison.json.invariants[*].result == "FAIL"` → `safety_violations` 加 `"<A|B>:<case>:<invariant_id>"`（Python 與 Rust 同時違反仍是違反，§11.3）。
- **差異**：同一 case 兩側 `comparison.json.verdict` 不同 → `{"kind":"verdict","path":"comparison.verdict"}`；`cli.ndjson` 中第 i 次 CLI 呼叫的 `rc` 不同 → `kind:"rc"`、stdout 解析成 JSON 後經 `normalize_response` 仍不同 → `kind:"value"`、`path` 為第一個不同的 JSON 點路徑（`call[<i>].<dotted>`）；`registered_change` 為 `case_changes.get(case)`（`behavior_changes.yaml` 的 `case_ids` 反查）。比較只用 `contracts/ab_normalization.yaml` 的規則，不得另寫寬鬆比對（arch §9.3）。
- `ok = integrity.a and integrity.b and not safety_violations and not differences`。

**Steps:**

- [ ] **Step 1：寫失敗測試**（luna）`tests/test_stage3_ab_diff.py`（`pytestmark = [core_portable, memory_only]`），以 `fakes_suites` 產生兩側報告樹：

| 測試名 | 斷言 |
|---|---|
| `test_identical_reports_ok` | 兩側相同 → `ok is True`、`differences == []` |
| `test_missing_required_case_breaks_integrity` | B 缺一個必要 case → `integrity.b is False`、problem 含 case id |
| `test_insufficient_comparison_breaks_integrity` | A 某 run `comparison.json.verdict == "INSUFFICIENT_EVIDENCE"` → `integrity.a is False` |
| `test_invariant_fail_both_sides_reported` | 兩側同一 case `zero_tx_on_reject` FAIL → `safety_violations` 含 `A:<case>:zero_tx_on_reject` 與 `B:…` |
| `test_exception_message_ignored_g1` | 兩側 stdout 只差 `EXCEPTION` 的 `message` → 無差異 |
| `test_set_path_order_ignored_g2` | `g2_set_paths` 的清單順序不同 → 無差異；非 G2 清單順序不同 → `kind:"value"` |
| `test_volatile_and_root_normalized` | 只差 `pid` 與 instance root 路徑 → 無差異 |
| `test_difference_attributed_to_case_change` | case 屬 `SC-15` 的 `case_ids` → `registered_change == "SC-15"` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_ab_diff.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（haiku）依規則；`contracts/ab_normalization.yaml` 的 `g2_set_paths` 逐項標註來源 SC／RPC 編號；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 L1.5 A/B 差分工具與正規化規則`。

---

## Task 7：A/B 判定（`tools/stage3/ab_verdict.py`）

**Test author model:** codex-gpt-6-luna-max
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-07-ab-verdict.md`
**BLOCKED-OWNER:** 無（純 L1；真實 L2／L3 報告的判定屬 BO-P9-02～05）。

**Files:**
- Create: `tools/stage3/ab_verdict.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_ab_verdict.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_ab_verdict.py`

**Interfaces — Consumes:** P0 `contracts/behavior_changes.yaml`（`scope`、`case_ids`）；Task 7B 差分；P2 `release-gate.json`（`release-gate.v1`，只讀 JSON，不 import gate、不重算門檻）；TestPilot `report.json`（`{"meta":…,"results":[{"id","verdict","reason","category","reason_code","duration_s","evidence"}]}`，inventory 08 §3.7、§4.4）；P2 `cases.yaml`（`expected`、`sentinel`）。
**Interfaces — Produces:**

```python
L2_VERDICTS: frozenset[str] = frozenset({"PASS", "FAIL", "SKIP", "XFAIL", "XPASS", "ERROR"})
def load_registered_changes(path: Path) -> dict[str, str]
    # change_id -> "diff"（scope both）| "absolute_only"（scope rust_only）
def case_changes(path: Path) -> dict[str, str]          # case_id -> change_id（依 case_ids 反查）
def combine(layer: str, *, a_status: str, b_status: str, diff: dict | None, registered: Mapping[str, str]) -> tuple[str, str | None, list[str]]
def compare_l2_reports(a_report: dict, b_report: dict, *, required_case_ids: set[str], cases: Mapping[str, dict],
                       registered_case_changes: Mapping[str, str], now: str) -> dict
    # {"verdict": "PASS"|"FAIL"|"INCOMPLETE", "missing":{"a":[id],"b":[id]}, "duplicates":{"a":[…],"b":[…]},
    #  "unknown_verdicts":{"a":[…],"b":[…]}, "required_skips":{"a":[…],"b":[…]}, "b_failures":[id], "xfail_accepted":[id],
    #  "expired_sentinels":[id], "needs_review":[{"case","a","b"}], "safety_violations":[], "integrity":{"a":bool,"b":bool}}
def compare_l3(a_gate_path: Path, b_gate_path: Path) -> dict
    # {"a_mode","b_mode","a_engine_verdict","a_baseline_result","b_engine_verdict","b_release_gate_result",
    #  "b_baseline_compatible","acceptance_revision","safety_violations":[…],"integrity":{"a":bool,"b":bool}}
```

`combine` 判定表（由上而下第一個符合者勝出；`diff` 對 L1.5 為 Task 7B 輸出、L2 為 `compare_l2_reports`、L3 為 `compare_l3`）：

| 條件 | verdict | reason_code |
|---|---|---|
| `diff.safety_violations` 非空 | FAIL | None（notes 列出違規） |
| `a_status == "INFRA_ERROR"` 或 `b_status == "INFRA_ERROR"` | INFRA_ERROR | `HARNESS_SUITE_INFRA_ERROR` |
| `diff.integrity.a` 或 `.b` 為 False | INFRA_ERROR | `HARNESS_COMPARATOR_ERROR`（證據不足不得轉 PASS） |
| `layer=="l2"` | `diff.verdict`（`INCOMPLETE` 映射為 INFRA_ERROR）；L2 的 A／B 狀態只依 `compare_l2_reports` 逐案判定，不用 run 層級 rc | INCOMPLETE 時 `HARNESS_SUITE_INFRA_ERROR` |
| `layer=="l1.5"` 且 `a_status == "FAIL"` 且 `b_status == "FAIL"` | FAIL | None |
| `layer=="l1.5"` 且 `a_status == "FAIL"` 且 `b_status == "PASS"` | BASELINE_INVALID | `HARNESS_BASELINE_INVALID` |
| `layer=="l1.5"` 且 `b_status == "FAIL"` | FAIL | None |
| `layer=="l1.5"` 且某 difference 的 `registered_change` 在 `registered` 且為 `absolute_only`（`rust_only`） | 該差異略過（不參與差分）；但 `diff.b_cases[case] != "PASS"` → FAIL（notes `"rust_only case failed absolute: <case>"`） | None |
| `layer=="l1.5"` 且有任一 difference 的 `registered_change` 為 None、不在 `registered`、或為 `diff`（`both`：已先在 Python 實作，A/B 不應有差） | FAIL | None（notes：`"unregistered difference: <case> <path>"` 或 `"both-scope change differs: <change> <case>"`） |
| `layer=="l3"` 且 `a_engine_verdict != "PASS"` 或 `a_baseline_result != "PASS"` | BASELINE_INVALID | `HARNESS_BASELINE_INVALID` |
| `layer=="l3"` 且 `b_engine_verdict != "PASS"` | FAIL | None（引擎原始 FAIL 判準保留，即使 suite 或 gate 顯示 PASS） |
| `layer=="l3"` 且 `b_status == "FAIL"` 而 `b_engine_verdict == "PASS"` | INFRA_ERROR | `HARNESS_SUITE_INFRA_ERROR`（notes `"suite/engine inconsistent"`） |
| `layer=="l3"` 且 `b_release_gate_result == "BLOCKED"` | INFRA_ERROR | `HARNESS_OD09_NOT_FROZEN` |
| `layer=="l3"` 且 `b_release_gate_result == "INSUFFICIENT_EVIDENCE"` 或 `b_baseline_compatible is not True` | INFRA_ERROR | `HARNESS_SUITE_INFRA_ERROR` |
| `layer=="l3"` 且 `b_release_gate_result != "PASS"` | FAIL | None |
| 其他 | PASS | None |

`compare_l2_reports` 規則（先完整性，後判定）：
1. 每側：`results` 為空、`id` 重複、`verdict` 不在 `L2_VERDICTS`、`required_case_ids` 中有缺項、必要案例為 `SKIP` 或 `ERROR` → 列入對應欄位且該側 `integrity=false`、verdict `INCOMPLETE`（§11.4：必要能力缺失、案例未完整執行不得 PASS）。
2. B 的 `FAIL`：若 `cases[id].expected == "XFAIL"`、`sentinel` 非 null、`sentinel.expires > now`、且該結果 `reason` 含 `sentinel.signature` → 列入 `xfail_accepted`；`expires <= now` → 列入 `expired_sentinels`；其他 → `b_failures`。
3. A `FAIL`、B `PASS`：若案例 id 在 `registered_case_changes` → 不列；否則列入 `needs_review`。`expected == "XFAIL"` 的案例兩側結果不同 → 一律列入 `needs_review`（§15.10：哨兵轉綠須由外部結果證實並登記）。
4. `b_failures`、`expired_sentinels` 或 `needs_review` 非空 → FAIL；否則 PASS。

`compare_l3`：讀兩個 `release-gate.json`；`a.mode != "baseline"`、`b.mode != "candidate"`、兩者 `acceptance.revision` 或 `platform` 不同 → `HarnessError("HARNESS_COMPARATOR_ERROR")`；`a_engine_verdict = a["engine_result"]["verdict"]`（`engine_result` 是 dict）；`safety_violations` 為兩側 `checks` 中 `group=="safety"` 且 `result=="FAIL"` 的 id（加側別前綴）；`integrity.<side>` 為該側 `group=="evidence"` 的 check 皆非 `INSUFFICIENT_EVIDENCE`；`b_baseline_compatible` 取 B 的 `baseline_compatible` check 是否 PASS。

**Steps:**

- [ ] **Step 1：寫失敗測試**（luna）`tests/test_stage3_ab_verdict.py`（`pytestmark = [core_portable, memory_only]`）：`combine` 以上表逐列參數化（`test_combine[row]`，每列建最小輸入、斷言 `(verdict, reason_code)`），再加：

```python
def test_python_and_rust_both_violate_safety_is_fail():
    diff = {"ok": False, "integrity": {"a": True, "b": True}, "safety_violations": ["A:c1:zero_tx_on_reject", "B:c1:zero_tx_on_reject"],
            "differences": [], "a_cases": {}, "b_cases": {}}
    v, code, notes = ab_verdict.combine("l1.5", a_status="FAIL", b_status="FAIL", diff=diff, registered={})
    assert (v, code) == ("FAIL", None) and "zero_tx_on_reject" in " ".join(notes)


def test_rust_only_difference_skipped_but_absolute_must_pass():
    diff = {"ok": False, "integrity": {"a": True, "b": True}, "safety_violations": [],
            "differences": [{"case": "sc15-x", "path": "call[0].state", "kind": "value", "registered_change": "SC-15"}],
            "a_cases": {"sc15-x": "PASS"}, "b_cases": {"sc15-x": "PASS"}}
    assert ab_verdict.combine("l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"})[0] == "PASS"
    diff["b_cases"]["sc15-x"] = "FAIL"
    assert ab_verdict.combine("l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"SC-15": "absolute_only"})[0] == "FAIL"


def test_both_scope_difference_is_fail():
    diff = {"ok": False, "integrity": {"a": True, "b": True}, "safety_violations": [],
            "differences": [{"case": "bc01-x", "path": "call[0].ok", "kind": "value", "registered_change": "BC-01"}],
            "a_cases": {"bc01-x": "PASS"}, "b_cases": {"bc01-x": "PASS"}}
    v, _, notes = ab_verdict.combine("l1.5", a_status="PASS", b_status="PASS", diff=diff, registered={"BC-01": "diff"})
    assert v == "FAIL" and notes == ["both-scope change differs: BC-01 bc01-x"]
```

| 測試名 | 斷言 |
|---|---|
| `test_l2_empty_report_incomplete` | B `results == []` → `verdict == "INCOMPLETE"` |
| `test_l2_missing_required_case_incomplete` | B 缺一個必要 id → `INCOMPLETE`、`missing.b == [id]` |
| `test_l2_duplicate_or_unknown_verdict_incomplete` | B 同 id 兩筆、或 verdict `"WEIRD"` → `INCOMPLETE` |
| `test_l2_required_skip_incomplete` | B `p0-doctor` SKIP 且列為 required → `INCOMPLETE` |
| `test_l2_baseline_red_case_not_run_level_fail` | A 有一個非必要案例 FAIL、其餘全同 → 不因 A run 層級失敗而 `BASELINE_INVALID`；該案例 B PASS → `needs_review` → FAIL；登記於 `registered_case_changes` → PASS |
| `test_l2_xfail_requires_sentinel_and_signature` | `cases` 該案 `expected: XFAIL`、`sentinel{issue:"#159",signature:"tail mismatch",expires:"2027-01-01T00:00:00+00:00"}`、B FAIL 且 reason 含 signature → `xfail_accepted`；`expires` 早於 `now` → `expired_sentinels`、FAIL；`expected: PASS` 的 f4 FAIL → `b_failures` |
| `test_l3_reads_gate_json_only` | 兩個 release-gate.json 替身 → 回傳欄位正確；`engine_result` 為 dict 時取 `["verdict"]` |
| `test_l3_b_engine_fail_with_gate_pass` | B `engine_result.verdict == "FAIL"`、`release_gate_result == "PASS"`、suite PASS → combine `FAIL` |
| `test_l3_baseline_engine_fail` | A `engine_result.verdict == "FAIL"` → `BASELINE_INVALID` |
| `test_l3_revision_mismatch_error` | 兩側 `acceptance.revision` 不同 → `HarnessError.code == "HARNESS_COMPARATOR_ERROR"` |
| `test_load_registered_changes_maps_scope` | 替身 repo 的 behavior_changes.yaml → `{"BC-01":"diff","SC-15":"absolute_only",...}` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_ab_verdict.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（haiku）依判定表；不 import `realhw`，只讀其 JSON 輸出（門檻唯一來源仍是 P2 gate）；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 A/B 絕對契約與差分判定`。

---

## Task 8：Canary WAL 收割與提交計數（`tools/stage3/canary_harvest.py`）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-08-canary-harvest.md`
**BLOCKED-OWNER:** BO-P9-07、BO-P9-08（真實使用 WAL 的收割）。

計數口徑（BO-P9-06 由擁有者凍結；以下為工具實作的唯一口徑，policy `submit_counting.source == "wal_tx_cmd_id"`）：一次「`cmd submit`」＝WAL 中 `dir=="TX"`、`cmd_id` 符合 `policy.cmd_id_regex`（`^[0-9a-f]{32}$`，arbiter 的 `uuid4().hex`，`sw_core/arbiter.py:166`）、`source` 不以 `exclude_source_prefixes` 開頭的**相異 `cmd_id`**。`file push/pull` 的 TX 以 `cmd_id="ft-<8hex>-<idx|mv|b64|md5|cleanup|probe>"`、`source=agent` 寫入（`sw_core/file_transfer.py:114,191,208,248,308,558,588`），不符合 regex，一律不計（1 MB 傳輸會產生數千筆分塊 TX）。被拒絕而未送出 bytes 的提交不計（保守、不灌水）。是否最終計入配額另需 Task 9 的結果紀錄為終態（`require_terminal_result`）。WAL 檔內容只取欄位，不複製 `payload_b64`。

**Files:**
- Create: `tools/stage3/canary_harvest.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_canary_harvest.py: {class: core_portable, resources: [memory_only]}`、`test_stage3_canary_harvest_posix.py: {class: posix_only, resources: [memory_only]}`、`test_stage3_canary_harvest_windows.py: {class: windows_native, resources: [memory_only]}`）
- Test: `tests/test_stage3_canary_harvest.py`、`tests/test_stage3_canary_harvest_posix.py`（RSS 量測）、`tests/test_stage3_canary_harvest_windows.py`（讀取中 rename）

**Interfaces — Consumes:** WAL 格式（inventory 04 §1.3、§1.6～§1.9）；rotate／reset 歸檔名 `raw.wal.ndjson.<YYYYmmdd-HHMMSS>`，DF-03 碰撞時加 `.1`、`.2`；BC-01 META 事件 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK`（規格 §8.2）；`CanaryPolicy`（Task 1）。
**Interfaces — Produces:**

```python
WAL_CURRENT = "raw.wal.ndjson"
HEAD_BYTES = 4096
@dataclass
class FileCursor:
    identity: str          # f"{st_dev}:{st_ino}"（Python 3.10+ 在 Windows 提供 volume serial／file index）
    head_sha256: str       # 檔案前 min(size, HEAD_BYTES) bytes 的 sha256；身分＝(identity, head_sha256)
    head_len: int
    path_last_seen: str
    offset: int
    size_last_seen: int
@dataclass
class Cursor:
    files: dict[str, FileCursor]          # 鍵 f"{identity}|{head_sha256}"
    tx_window: dict[str, list]            # cmd_id -> [crc32, len, wall_ts]；只保留 resend_window_s 內
    human_window: dict[str, str]          # f"{com}|{crc32}|{len}" -> wall_ts；只保留 resend_window_s 內
    human_buckets: dict[str, int]         # f"{com}|{bucket}" -> 1；只保留最近 2 天
@dataclass(frozen=True)
class HarvestRules:
    exclude_source_prefixes: tuple[str, ...]
    cmd_id_regex: str
    allowed_tx_source_regex: str
    resend_window_s: int
    human_replay_min_len: int
    coexist_bucket_s: int
    @staticmethod
    def from_policy(policy: CanaryPolicy) -> "HarvestRules"
@dataclass(frozen=True)
class SubmitObservation:
    cmd_id: str
    com: str | None
    source: str
    wall_ts: str
    seq: int
    wal_file: str
@dataclass(frozen=True)
class WalSignal:
    kind: str      # "human_tx" | "recover_tx" | "flash_tx" | "wal_loss" | "wal_recreated" | "credentials_unresolved" | "transport_stall"
                   # | "unauthorized_tx" | "resend_suspect" | "human_replay_suspect"
    com: str | None
    wall_ts: str
    seq: int
    source: str
    detail: Mapping[str, Any]
@dataclass(frozen=True)
class HarvestResult:
    submits: tuple[SubmitObservation, ...]     # 本次呼叫內以 cmd_id 去重，保留最小 (wall_ts, seq)
    signals: tuple[WalSignal, ...]
    anomalies: tuple[str, ...]
    cursor: Cursor
def file_identity(path: Path) -> tuple[str, str, int]   # (identity, head_sha256, head_len)
def open_shared(path: Path) -> BinaryIO
    # POSIX：open(path, "rb")；Windows：ctypes CreateFileW(GENERIC_READ, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_EXISTING)
    # ＋msvcrt.open_osfhandle＋os.fdopen("rb")；讓 daemon 在讀取期間仍可 rename／unlink（spec §8.2／§8.3「外部 reader 不得阻擋 rotate」）
def wal_files(wal_dir: Path) -> list[Path]          # WAL_CURRENT 與 WAL_CURRENT + ".*"；排除 *.tmp；依 (mtime, name) 排序
def harvest(wal_dir: Path, cursor: Cursor, *, rules: HarvestRules) -> HarvestResult
def load_cursor(path: Path) -> Cursor
def save_cursor(path: Path, cursor: Cursor) -> None   # jsonio.atomic_write_text
```

規則：
- 一律以 `open_shared` 開檔、讀完即關；以二進位模式自 `offset` 讀取，只處理以 `b"\n"` 結尾的完整行；最後不完整行不前進 offset。
- 每行 `decode("utf-8", errors="replace")` 後 `json.loads`；失敗 → anomaly `f"malformed_line:{identity}:{offset}"`，略過並前進。合法紀錄另驗 `len == len(b64decode(payload_b64))` 與 `crc32` → 不符 anomaly `f"crc_mismatch:{identity}:{seq}"`（不計 submit、不出訊號）；同一檔內 `seq` 非嚴格遞增 → anomaly `f"seq_regress:{identity}:{seq}"`。
- 檔案身分＝(`st_dev:st_ino`, 前 4 KiB 的 sha256)：同 inode 但 head hash 不符（外部 unlink 後重建而 inode 被重用）→ 視為新檔、從 0 讀，並記 anomaly `f"replaced:{identity}"`；同身分而 `size < offset`（被截斷）→ anomaly `f"truncated:{identity}"`，offset 歸 0 重讀（重讀造成的重複由 `cmd_id` 去重吸收）。`head_len < HEAD_BYTES` 的檔案在變長後以原 `head_len` 長度重算比對。
- 新身分（rotate 後的新現行檔、或外部刪除後重建）從 0 開始；舊身分的檔案改名為歸檔仍沿用原 offset，不重算。
- cursor 中已不存在的身分移除，並記 anomaly `f"vanished:{identity}"`（例如使用者刪歸檔）。
- signals（每筆紀錄至多一個訊號種類，依下列優先序）：
  - `wal_recreated`：META 且 `meta.event == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"`（即使同筆 `loss_flag:true`，也只出這一個訊號）；`wal_loss`：其他任一紀錄 `loss_flag` 為 true。
  - `unauthorized_tx`：TX 且 `source` 不符 `allowed_tx_source_regex`。
  - `credentials_unresolved`（META `meta.event == "credentials_unresolved"`）、`transport_stall`（META `meta.event == "transport_stall_suspected"`）。
  - `recover_tx`：TX 且 `source == "system:recover"`。
  - `flash_tx`：TX 且 `source == "flash"`；同一 com 每 60 s 至多一筆。
  - `human_tx`：TX 且 source 以 `human:` 開頭；每 (com, `floor(epoch / coexist_bucket_s)`) 至多一筆（raw mode 逐鍵寫一筆 TX，不得逐鍵入帳；跨呼叫以 `cursor.human_buckets` 去重）。另：`len ≥ human_replay_min_len` 的 human TX 若 (com, crc32, len) 已在 `cursor.human_window` 內出現 → `human_replay_suspect`（detail 帶前次 `wall_ts`），由 Task 11 與快照比對是否跨 RECOVERING／RELEASED。
  - `resend_suspect`：符合 `cmd_id_regex` 的非 human TX，若同 `cmd_id` 已在 `cursor.tx_window` 中且 (crc32, len) 相同 → 訊號（detail 帶前次 `wall_ts`）；由 Task 11 判定是否跨 `bridge_generation` 變更或 Recovery。
- `detail` 只放 `crc32`、`len`、前次時間等摘要，不放 payload。

**Steps:**

- [ ] **Step 1：寫失敗測試**（agy）`tests/test_stage3_canary_harvest.py`（`pytestmark = [core_portable, memory_only]`），關鍵測試：

```python
from __future__ import annotations

import os

import pytest

import stage3_fakes as fakes
from tools.stage3 import canary_harvest as h
from tools.stage3 import canary_policy

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
C1, C2, C3 = fakes.uuid_cmd_id(1), fakes.uuid_cmd_id(2), fakes.uuid_cmd_id(3)


@pytest.fixture
def rules(tmp_path):
    return h.HarvestRules.from_policy(canary_policy.load_policy(fakes.write_policy(tmp_path / "p.json")))


def _empty():
    return h.Cursor(files={}, tx_window={}, human_window={}, human_buckets={})


def test_rotation_does_not_double_count(tmp_path, rules):
    wal = tmp_path / "raw.wal.ndjson"
    fakes.write_wal(wal, [fakes.wal_record(1, cmd_id=C1), fakes.wal_record(2, cmd_id=C1, payload=b"y"), fakes.wal_record(3, cmd_id=C2)])
    r1 = h.harvest(tmp_path, _empty(), rules=rules)
    assert sorted(s.cmd_id for s in r1.submits) == [C1, C2]
    os.replace(wal, tmp_path / "raw.wal.ndjson.20261102-010000")
    fakes.write_wal(wal, [fakes.wal_record(4, cmd_id=C3)])
    r2 = h.harvest(tmp_path, r1.cursor, rules=rules)
    assert [s.cmd_id for s in r2.submits] == [C3]


def test_file_transfer_chunks_not_counted(tmp_path, rules):
    fakes.write_wal(tmp_path / "raw.wal.ndjson",
                    [fakes.wal_record(i, source="agent", cmd_id=f"ft-1a2b3c4d-{i}") for i in range(1, 200)]
                    + [fakes.wal_record(300, source="agent", cmd_id="ft-1a2b3c4d-md5")])
    assert h.harvest(tmp_path, _empty(), rules=rules).submits == ()


def test_non_uuid_cmd_id_ignored(tmp_path, rules):
    fakes.write_wal(tmp_path / "raw.wal.ndjson", [fakes.wal_record(1, cmd_id="abc"), fakes.wal_record(2, cmd_id=C1.upper())])
    assert h.harvest(tmp_path, _empty(), rules=rules).submits == ()


def test_partial_line_waits(tmp_path, rules):
    wal = tmp_path / "raw.wal.ndjson"
    fakes.write_wal(wal, [fakes.wal_record(1, cmd_id=C1)], trailing_newline=False)
    r1 = h.harvest(tmp_path, _empty(), rules=rules)
    assert r1.submits == ()
    with open(wal, "ab") as f:
        f.write(b"\n")
    r2 = h.harvest(tmp_path, r1.cursor, rules=rules)
    assert [s.cmd_id for s in r2.submits] == [C1]


def test_excluded_sources_and_meta_not_counted(tmp_path, rules):
    fakes.write_wal(tmp_path / "raw.wal.ndjson", [
        fakes.wal_record(1, source="human:cli-1", cmd_id=fakes.uuid_cmd_id(11)),
        fakes.wal_record(2, source="system:recover", cmd_id=fakes.uuid_cmd_id(12)),
        fakes.wal_record(3, source="flash", cmd_id=fakes.uuid_cmd_id(13)),
        fakes.wal_record(4, dir="META", source="system", cmd_id=fakes.uuid_cmd_id(14), meta={"event": "command_completion", "completion_regex": "x"}),
        fakes.wal_record(5, dir="RX", source="device"),
        fakes.wal_record(6, source="agent:x", cmd_id=""),
    ])
    r = h.harvest(tmp_path, _empty(), rules=rules)
    assert r.submits == ()
    assert {s.kind for s in r.signals} == {"human_tx", "recover_tx", "flash_tx"}
```

| 測試名 | 斷言 |
|---|---|
| `test_reset_archive_collision_suffix` | 歸檔 `raw.wal.ndjson.20261102-010000` 與 `.20261102-010000.1` 皆被 `wal_files` 列出、皆收割一次 |
| `test_truncated_file_rereads_with_anomaly` | 同身分截短 → anomalies 含 `truncated:`，重讀後 submits 仍以 cmd_id 去重 |
| `test_replaced_file_same_inode_detected` | monkeypatch `os.stat` 使新檔回同一 `st_ino`、但內容不同且 size ≥ 舊 offset → anomaly `replaced:`，新檔從 0 收割 |
| `test_malformed_line_skipped` | 中間一行 `{bad` → anomaly `malformed_line:`，其後紀錄仍收割 |
| `test_crc_mismatch_not_counted` | 一筆 crc32 被改 → anomaly `crc_mismatch:`、該筆不出 submit |
| `test_vanished_archive` | 刪除已收割的歸檔 → anomaly `vanished:`，cursor 移除該身分 |
| `test_recreated_meta_single_signal` | META `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 且 `loss_flag:true` → signals 只有一個 `wal_recreated`；另一筆一般 `loss_flag:true` → `wal_loss` |
| `test_human_tx_deduped_per_bucket` | 同 com 同 600 s 桶內 50 筆 human TX → 1 個 `human_tx`；跨兩次 harvest 呼叫同桶 → 仍 1 個 |
| `test_unauthorized_source_signal` | TX `source="weird"` → `unauthorized_tx` |
| `test_resend_suspect_same_cmd_same_payload` | 同 uuid cmd_id、同 payload 兩筆 TX（跨兩次 harvest）→ 1 個 `resend_suspect`；payload 不同 → 無 |
| `test_human_replay_suspect` | `human:` TX payload 長 10、相同 (com, crc32, len) 兩次 → `human_replay_suspect`；長 1 的按鍵重複 → 無 |
| `test_cursor_roundtrip` | `save_cursor` 後 `load_cursor` 相等；寫入為原子（無 `.tmp` 殘留） |
| `test_no_payload_in_observations` | `dataclasses.asdict` 結果不含 `payload_b64` 鍵 |
| `test_min_seq_kept_within_call` | 同 cmd_id 在 seq 7、3 → 觀測 `seq == 3` |
| `test_large_file_streams_posix`（`test_stage3_canary_harvest_posix.py`） | 產生 20,000 筆紀錄（約 6 MB）→ 收割結果 20,000 個 cmd_id，峰值 RSS 增量（`resource.getrusage` 前後差）< 64 MB |
| `test_rename_while_reading_windows`（`test_stage3_canary_harvest_windows.py`） | 以 `open_shared` 開啟現行 WAL 未關閉時，另一執行緒 `os.replace` 成歸檔成功；以內建 `open` 開啟時 `os.replace` 丟 `PermissionError`（證明 share flag 的必要）；`file_identity` 在 rename 前後相同 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_canary_harvest.py tests/test_stage3_canary_harvest_posix.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依規則；逐檔以 64 KiB 區塊讀取，自行切行（保留跨區塊殘段）。僅用 stdlib；Windows ctypes 宣告收在 `open_shared` 的 `os.name == "nt"` 分支內，模組在 POSIX 可 import。登錄三個測試檔。
- [ ] **Step 4：Run** 同上（Linux），並以 `py -3.12 -m pytest -q tests/test_stage3_canary_harvest.py tests/test_stage3_canary_harvest_windows.py` 在 Windows 本機目錄執行 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 Canary WAL 收割、cmd_id 去重計數與安全訊號`。

---

## Task 9：Canary collector 與 zipapp（`tools/stage3/canary_collect.py`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max（含 Windows registry／ctypes／事件記錄的 `host_id`、`boot_id`、daemon 執行檔解析；master plan §2 平台工作歸 luna）
**Changelog fragment:** `changelog.d/rust-p9-09-canary-collect.md`
**BLOCKED-OWNER:** BO-P9-07（Linux 擁有者日常機器安裝並長期執行 collector）、BO-P9-08（Windows 測試團隊單人工作站：Python 3.10+ 與排程方式）。

**Files:**
- Create: `tools/stage3/canary_collect.py`、`tools/stage3/collector_main.py`、`tools/stage3/pack_collector.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_canary_collect.py: {class: core_portable, resources: [process_rpc]}`、`test_stage3_canary_collect_windows.py: {class: windows_native, resources: [memory_only]}`）
- Test: `tests/test_stage3_canary_collect.py`、`tests/test_stage3_canary_collect_windows.py`

**Interfaces — Consumes:** Task 1 `canary_policy`、`jsonio`、`proc`、`reason_codes`；Task 8 `harvest`；Task 11 `canary_incidents`（`note`／`incident` 子命令）；CLI JSON 欄位（inventory 08 §7.6）：`daemon status` 的 `ok`、`running`、`pid`、`version`、`wal_path`、`wal.healthy`、`multi_open`；`session list` 的 `sessions[].{com,state,bridge_generation,boot_quiet_remaining_s,ready_reconfirm_pending,ready_reconfirm_failed,command_capable,platform,profile,last_error,last_ready_at}`（`sw_core/session_manager.py:569-606`）；`cmd status --cmd-id` 的 `command.{status,error_code}`；安裝佈局（arch §10.2）。
**Interfaces — Produces:**

```python
READONLY_COMMANDS: tuple[tuple[str, ...], ...] = (("daemon", "status"), ("session", "list"), ("cmd", "status"))   # collector 只允許這三個子命令（皆不 touch Lease）
TERMINAL_RESULTS: frozenset[str] = frozenset({"done", "error", "timeout"})
@dataclass(frozen=True)
class SessionObs:
    com: str
    state: str
    bridge_generation: int | None
    boot_quiet_remaining_s: float | None
    ready_reconfirm_pending: bool | None
    ready_reconfirm_failed: bool | None
    command_capable: bool | None
    platform: str | None
    profile: str | None
    last_error: str | None
    last_ready_at: str | None
@dataclass(frozen=True)
class Snapshot:
    t_wall: str
    host_id: str
    platform: str
    boot_id: str | None
    daemon_ok: bool
    daemon_pid: int | None
    daemon_version: str | None
    daemon_exe_sha256: str | None
    artifact_id: str | None
    artifact_check: str           # "verified" | "cli_mismatch" | "daemon_mismatch" | "version_mismatch" | "unresolved"
    bundle_version: str | None
    daemon_implementation: str | None
    wal_dir: str | None
    wal_healthy: bool | None
    multi_open: bool | None
    sessions: tuple[SessionObs, ...]
    error: str | None
def host_id() -> str            # sha256(socket.gethostname() + "\0" + machine_id)[:16]；machine_id：Linux /etc/machine-id，Windows HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid（winreg），讀不到用 ""
def boot_id() -> str | None
    # Linux：/proc/sys/kernel/random/boot_id。
    # Windows：`wevtutil qe System /q:"*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and (EventID=27)]]" /c:1 /rd:true /f:xml`
    #   取最新一筆的 EventRecordID＋TimeCreated（Kernel-Boot 27 於冷開機、Fast Startup 混合開機與休眠恢復皆記錄，BootType 欄區分）；
    #   失敗 → None。**不得**以 time.time()−GetTickCount64() 的四捨五入推算（次秒量化與 NTP 調整會翻動，Fast Startup 也不重置 tick）。
def daemon_exe_sha256(pid: int) -> str | None
    # Linux：sha256(realpath(/proc/<pid>/exe))；Windows：OpenProcess＋QueryFullProcessImageNameW 後 sha256；失敗 None
def resolve_artifact(cli_exe: Path, *, daemon_pid: int | None, daemon_version: str | None,
                     daemon_sha: Callable[[int], str | None] = daemon_exe_sha256) -> tuple[str | None, dict | None, str]
    # 位置：Linux realpath(cli_exe).parent.parent / "release-manifest.json"；Windows cli_exe.parent / "current-release.json" 的 "dir" / "release-manifest.json"
    # 找不到 → (None, None, "unresolved")；sha256(realpath(cli_exe)) != components.cli.sha256 → (None, m, "cli_mismatch")；
    # daemon_version != bundle_version → (None, m, "version_mismatch")（arch §10.3：配對不符只警告不阻擋 RPC，因此新 CLI 可能連到舊 daemon）；
    # daemon_sha(pid) != components.daemon.launcher.sha256（Python 基線無 launcher 時比 runtime.sha256）或為 None → (None, m, "daemon_mismatch")；
    # 全部相符 → (sha256(manifest bytes), m, "verified")
def take_snapshot(cli_exe: Path, *, platform: str, run: Runner = run_cmd, now: Callable[[], str] = jsonio.utc_now_iso) -> Snapshot
def collect_once(cli_exe: Path, ledger_dir: Path, policy: CanaryPolicy, *, platform: str, run: Runner = run_cmd,
                 now: Callable[[], str] = jsonio.utc_now_iso) -> dict
    # policy.status != "frozen" → HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN")（draft policy 不得開始收集）；
    # 附加 snapshot、新 submit、submit_result、wal_signal、harvest_anomaly 紀錄到 ledger_dir/"ledger.ndjson"；cursor 存 ledger_dir/"cursor.json"；
    # 已見 cmd_id 集合存 ledger_dir/"seen.txt"（每行一個，附加寫入）；待查結果存 ledger_dir/"pending_results.json"；
    # 回 {"snapshot_ok":bool,"new_submits":int,"results":int,"signals":int,"anomalies":int}
def drain_results_once(cli_exe: Path, ledger_dir: Path, policy: CanaryPolicy, *, run: Runner = run_cmd,
                       now: Callable[[], str] = jsonio.utc_now_iso) -> int
    # 只做結果收斂（不取 snapshot、不 harvest）：依 first_seen 先後對至多 result_poll_max_per_cycle 個 pending 查 `cmd status`；回剩餘 pending 數
def collect_loop(cli_exe: Path, ledger_dir: Path, policy: CanaryPolicy, *, platform: str, stop: threading.Event,
                 run: Runner = run_cmd, now: Callable[[], str] = jsonio.utc_now_iso, sleep: Callable[[float], None] = time.sleep) -> None
    # 每 collector_interval_s 一次 collect_once；pending 仍 > 0 時，在下一次 collect 前連續呼叫 drain_results_once（每次之間 sleep 1 s），
    # 直到 pending 清空或下一次 collect 到期（產品 history 只留 512 筆，backlog 不得累積到被淘汰）
def collector_revision() -> str      # 由 zipapp 執行時為 .pyz 檔 sha256；repo 內執行為 `git rev-parse HEAD`（失敗 "unknown"）
def write_policy_ref(ledger_dir: Path, policy: CanaryPolicy, *, now: Callable[[], str] = jsonio.utc_now_iso) -> None
    # type="policy_ref" 紀錄（policy_sha256、status、frozen_at、decision_ref、collector_revision）；collect_once 在 policy sha 或 revision 與帳本最後一筆 policy_ref 不同時自動寫入

# tools/stage3/collector_main.py
def main(argv: Sequence[str] | None = None) -> int   # 子命令 collect（--cli、--ledger-dir、--policy、--platform、--once）、note、incident open|close|list（Task 11）；HarnessError → stdout {"ok":false,"reason_code":…,"detail":…}、exit 2

# tools/stage3/pack_collector.py
COLLECTOR_MODULES: tuple[str, ...] = ("__init__", "jsonio", "proc", "reason_codes", "canary_policy", "canary_harvest", "canary_collect", "canary_classify", "canary_incidents", "collector_main")
def pack(out: Path) -> Path    # zipapp：tools/__init__.py＋tools/stage3/ 的上列模組＋tools/stage3/stage3_reason_codes.json（套件資料）；entry "tools.stage3.collector_main:main"
```

`python3 -m tools.stage3 canary …`（Task 17）與 zipapp 共用 `collector_main` 的同一組函式。

結果收斂：每個新 submit 加入 `pending_results.json`（`{cmd_id: first_seen_wall}`）；每次 `collect_once` 依 first_seen 先後對至多 `policy.result_poll_max_per_cycle` 個呼叫 `[cli,"cmd","status","--cmd-id",id]`（timeout 10 s、不帶 `--retries`）；`command.status ∈ TERMINAL_RESULTS` → 寫 `submit_result` 並移出 pending；回應為 not-found 類錯誤，或 first_seen 已超過 24 h 仍非終態 → 寫 `status:"unknown"` 並移出。`require_terminal_result` 為 true 時只有終態結果的 submit 計入配額（Task 12），`unknown` 不計但保留紀錄（§12.2「每筆累計須關聯結果」）。

**history 淘汰約束**：daemon 只保留 `CMD_HISTORY_MAX=512` 筆命令紀錄，並依 `done_at` 淘汰最舊的已完成者（`sw_core/arbiter.py:233-251`；Rust 照搬），淘汰後查詢回 `CMD_NOT_FOUND`。因此結果必須在執行期持續收斂，不得累積 backlog 後才補查。

- `collect_once` 本身仍受 `result_poll_max_per_cycle` 限制（`test_result_poll_rate_limited` 不變）。
- `collect_loop` 在兩次 collect 之間以 `drain_results_once` 連續清 backlog。
- not-found 寫 `submit_result{status:"unknown", reason:"CMD_NOT_FOUND"}`，另寫一筆 `harvest_anomaly{kind:"result_evicted", cmd_id}`。24 h 逾時寫 `reason:"STALE_NON_TERMINAL"`。
- Task 12 `tally` 把 `reason=="CMD_NOT_FOUND"` 的筆數另列 `submits_result_evicted`，它是 `submits_unknown_result` 的子集。這些筆數不計入 `submits`，也**不得**視為完成。
- 不得調大產品 history 來遷就 collector。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_canary_collect.py`（`pytestmark = [core_portable, process_rpc]`）：

```python
def test_collector_never_calls_lease_touching_commands(tmp_path):
    stub_dir = tmp_path / "bin"
    c1 = fakes.uuid_cmd_id(1)
    cli = fakes.make_stub_cli(stub_dir, {
        "daemon status": [{"rc": 0, "stdout": {"ok": True, "running": True, "pid": 11, "version": "0.9.0",
                                                "wal_path": str(tmp_path / "wal" / "raw.wal.ndjson"), "wal": {"healthy": True},
                                                "multi_open": False}}],
        "session list": [{"rc": 0, "stdout": {"ok": True, "sessions": [{"com": "COM0", "state": "READY", "bridge_generation": 3}]}}],
        f"cmd status --cmd-id {c1}": [{"rc": 0, "stdout": {"ok": True, "command": {"status": "done"}}}],
    })
    (tmp_path / "wal").mkdir()
    fakes.write_wal(tmp_path / "wal" / "raw.wal.ndjson", [fakes.wal_record(1, cmd_id=c1)])
    pol = canary_policy.load_policy(fakes.write_policy(tmp_path / "policy.json"))
    out = canary_collect.collect_once(cli, tmp_path / "ledger", pol, platform="linux")
    assert out["snapshot_ok"] is True and out["new_submits"] == 1
    canary_collect.collect_once(cli, tmp_path / "ledger", pol, platform="linux")
    argvs = [c["argv"] for c in fakes.read_calls(stub_dir)]
    assert all("interactive" not in " ".join(a) for a in argvs)
    assert {tuple(a[:2]) for a in argvs} <= set(canary_collect.READONLY_COMMANDS)
```

| 測試名 | 斷言 |
|---|---|
| `test_draft_policy_refused` | draft policy → `HarnessError.code == "HARNESS_CANARY_POLICY_NOT_FROZEN"`，不寫任何帳本紀錄 |
| `test_collect_twice_no_duplicate_submit` | 連跑兩次 `collect_once` → 第二次 `new_submits == 0`；`ledger.ndjson` 中 `type=="submit"` 只有 1 筆 |
| `test_submit_result_recorded` | `cmd status` 回 `done` → 一筆 `submit_result{status:"done"}`；回 not-found → `status:"unknown"` |
| `test_result_poll_rate_limited` | 一次 120 個新 submit、`result_poll_max_per_cycle=50` → 該次只 50 個 `cmd status` 呼叫 |
| `test_canary_results_survive_history_eviction` | 注入 `run` 為測試端 fake `EvictingCmdStore(history_max=512)`，行為與 P2 Task 14 同名 fake 相同：照 `sw_core/arbiter.py:233-251`，依 `done_at` 淘汰最舊已完成者，淘汰後回 `CMD_NOT_FOUND`。WAL 每個 collect 週期新增 300 個相異 agent `cmd_id`，這些命令於 store 中立即 `done`；每週期之間 store 另完成 100 筆非 canary 命令。`result_poll_max_per_cycle=50`、`collector_interval_s=30`；`sleep` 以假時鐘推進。跑 4 個週期（共 1,200 筆 canary submit），每週期之間依 `collect_loop` 規則呼叫 `drain_results_once`。斷言：`store.evicted_count >= 1600 - 512`；1,200 筆皆有 `submit_result{status:"done"}`；`reason=="CMD_NOT_FOUND"` 的紀錄 0 筆；每週期結束時 `pending_results.json` 為空。對照組：同負載但只呼叫 `collect_once`、不 drain → 出現 `reason:"CMD_NOT_FOUND"` 的 `unknown`，且每筆都有對應的 `harvest_anomaly{kind:"result_evicted"}`；Task 12 `tally` 將這些筆數計入 `submits_result_evicted`，不計入 `submits` |
| `test_daemon_down_snapshot_recorded` | `daemon status` rc=2 → snapshot `daemon_ok is False`、`error` 非空、不呼叫 harvest（ledger 無 submit） |
| `test_resolve_artifact_linux_symlink_verified` | `make_bundle` 後建立 `~/.local/bin/serialwrap` symlink 指向 bundle `bin/serialwrap`；`daemon_sha` 回 launcher sha、version 相符 → `(sha256_file(bundle/"release-manifest.json"), m, "verified")` |
| `test_resolve_artifact_windows_current_release` | 模擬 `bin/current-release.json` `{"bundle_version":"0.9.0","dir":"<bundle>"}`（純函式，`layout="windows"`）→ 同上 |
| `test_new_cli_old_daemon_not_attributed` | CLI 指向新 bundle，`daemon_version="0.8.0"` → `artifact_id is None`、`artifact_check == "version_mismatch"`；version 相同但 `daemon_sha` 回舊 launcher sha → `"daemon_mismatch"` |
| `test_cli_binary_swapped_not_attributed` | 改寫 bundle `bin/serialwrap` → `"cli_mismatch"` |
| `test_resolve_artifact_missing` | 無 manifest → `(None, None, "unresolved")` |
| `test_snapshot_parses_sessions` | `boot_quiet_remaining_s`、`ready_reconfirm_pending`、`ready_reconfirm_failed`、`command_capable`、`platform`、`last_ready_at` 正確映射；缺欄位為 None |
| `test_host_id_stable` | 連續兩次相同、長度 16 |
| `test_policy_ref_written_with_revision` | 第一次 collect 後帳本第一筆為 `policy_ref`，`policy_sha256 == pol.sha256`、`collector_revision` 非空；policy 不變再 collect 不重寫 |
| `test_pack_collector_runs_outside_repo` | `pack(tmp/"sw-canary.pyz")`；以 `cwd=tmp_path/"elsewhere"` 執行 `[sys.executable, pyz, "collect", "--help"]` rc 0；再執行 `collect --once --policy <draft policy> …` → rc 2 且 stdout JSON `reason_code == "HARNESS_CANARY_POLICY_NOT_FROZEN"`（證明 zip 內原因碼登錄表可讀）；zip 內不含 `ab_runner.py`、不含任何 `sw_core` 路徑 |
| `test_collector_modules_stdlib_only` | 以 AST 掃 `COLLECTOR_MODULES` 的 import，頂層模組名全屬 `sys.stdlib_module_names` 或 `tools` |

`tests/test_stage3_canary_collect_windows.py`（`pytestmark = [windows_native, memory_only]`）：`test_boot_id_from_kernel_boot_event`（回傳非 None，且連續兩次相同）、`test_boot_id_not_tick_derived`（monkeypatch `time.time` 前進 0.6 s → 值不變）、`test_daemon_exe_sha256_of_child`（子程序 python.exe 的 sha 等於 `sys.executable` 的 sha）、`test_host_id_reads_machine_guid`。

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_canary_collect.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依 Interfaces。CLI 呼叫：`[cli, "daemon", "status"]`、`[cli, "session", "list"]`、`[cli, "cmd", "status", "--cmd-id", id]`，timeout 10 s，不帶 `--retries`。`wal_dir = dirname(daemon status.wal_path)`。`collect_loop` 每 `policy.collector_interval_s` 秒一次，單次例外記 `harvest_anomaly` 後續跑。登錄兩個測試檔。
- [ ] **Step 4：Run** 同上（Linux），Windows `py -3.12 -m pytest -q tests/test_stage3_canary_collect.py tests/test_stage3_canary_collect_windows.py` → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 Canary collector 與 Windows 可攜 zipapp`。

---

## Task 10：干擾／交接分類與活躍日（`tools/stage3/canary_classify.py`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-10-canary-classify.md`
**BLOCKED-OWNER:** BO-P9-06（分類規則與各類型最低配額由擁有者凍結）；BO-P9-07／08（真實資料）。

**Files:**
- Create: `tools/stage3/canary_classify.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_canary_classify.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_canary_classify.py`

**Interfaces — Consumes:** 帳本 `snapshot`／`submit`／`submit_result`／`wal_signal`／`note` 紀錄（dict）、`CanaryPolicy`、`canary_policy.{CYCLE_TYPES,local_date}`。

本模組限 stdlib（`steady` 被 Task 11 使用並打包進 collector zipapp）。

產品事實（決定分類規則）：DUT reboot 時 session **維持 `READY`**，只設 `boot_quiet_until` 與 `ready_reconfirm_pending`（`sw_core/session_manager.py:450-470`、`:2327-2372`；規格 §5.1.1、§5.3），`session list` 以 `boot_quiet_remaining_s`、`ready_reconfirm_pending`、`ready_reconfirm_failed` 呈現；`bridge_generation` 不持久化、每次 attach 才遞增（`:310`、`:2708`），daemon 重啟後從 0 開始。
**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class Episode:
    host_id: str
    com: str
    daemon_pid: int | None
    states: tuple[str, ...]
    start_wall: str          # 第一個非穩態快照
    end_wall: str            # 回到穩態的快照
    before_generation: int | None
    after_generation: int | None
    boot_signal: bool        # 期間任一 READY／ATTACHED 快照 boot_quiet_remaining_s > 0、ready_reconfirm_pending 或 ready_reconfirm_failed 為 True
    ended_steady: bool       # False：序列末端、pid 變更或觀測缺口時仍非穩態
@dataclass(frozen=True)
class Cycle:
    type: str
    host_id: str
    platform: str
    com: str | None
    start_wall: str
    end_wall: str
    artifact_id: str | None
    result: str                   # "recovered_ready" | "ended_non_ready"（後者只留作紀錄，不計入配額）
    evidence: tuple[str, ...]     # "snapshot:<record_id>" 或 "wal:<seq>"
def steady(sess: Mapping[str, Any]) -> bool
    # state=="READY" 且 boot_quiet_remaining_s 為 None 或 ≤0 且 ready_reconfirm_pending 不為 True 且 ready_reconfirm_failed 不為 True；
    # 或 state=="ATTACHED" 且（command_capable is False 或 platform=="passthrough"）——不依 profile 名稱
def episodes(snapshots: Sequence[dict], policy: CanaryPolicy) -> list[Episode]
def classify_episode(ep: Episode, platform: str, recover_tx_seen: bool) -> str | None
def attribute_artifact(ts: str, host_snapshots_sorted: Sequence[dict]) -> str | None
def classify(snapshots: Sequence[dict], submits: Sequence[dict], signals: Sequence[dict], notes: Sequence[dict],
             policy: CanaryPolicy, *, platform: str) -> list[Cycle]
def active_days(submits: Sequence[dict], snapshots: Sequence[dict], policy: CanaryPolicy, *, platform: str,
                artifact_id: str, results: Sequence[dict] = ()) -> list[str]     # "YYYY-MM-DD"（canary_policy.local_date 換算），升冪
```

規則：
- `episodes`：每 (host, com) 依 `t_wall` 排序；只取 `daemon_ok` 為 true 的快照；一個 episode＝前一個快照為穩態（`steady`）、之後連續 ≥1 個非穩態、再回到穩態（`ended_steady=True`）；相鄰快照間隔 > `collector_interval_s × episode_max_gap_factor` 時切斷、跨 daemon pid 變更時切斷、序列末端仍非穩態——這三種情形輸出 `ended_steady=False` 的 episode（只作紀錄）。`READY → READY(boot_quiet_remaining_s=12, ready_reconfirm_pending) → READY` 即形成一個 episode（非穩態不必改變 state）。
- `classify_episode` 優先序：含 `RELEASED` → `handoff_release_attach`；含 `FLASHING` → linux 為 `flash`、windows 回 None；含 `DETACHED` 且 `after_generation > before_generation`（同一 daemon pid 內才比較）→ `hotplug_reattach`；`boot_signal` → `dut_reboot`；含 `RECOVERING` 或 `recover_tx_seen` → `recovery`；否則 None（不計）。`ended_steady=False` 時 cycle `result="ended_non_ready"`，否則 `"recovered_ready"`；只有 `recovered_ready` 計入配額。
- `recover_tx_seen`：episode 期間（`start_wall - collector_interval_s` 至 `end_wall`）同 com 有 `recover_tx` 訊號。
- 未被任何 episode 覆蓋的 `recover_tx` 訊號：同 com 以 `recover_cluster_s` 聚成一群，每群一個 `recovery` cycle（快速恢復未被快照捕捉）。
- `human_agent_coexist`：同 com、同 `floor(epoch / coexist_bucket_s)` 桶內同時有 `human_tx` 訊號與該 com 的 submit → 一個 cycle；每 (host, com, 本地日期) 至多 `coexist_cap_per_day` 個。
- `planned_restart`：同 host 的「前一個 `daemon_ok` 快照」與「本 `daemon_ok` 快照」（中間的失聯快照略過）daemon pid 不同、同 boot_id，且某筆 `note.kind == "planned_restart"` 的 `[from, to]` 覆蓋本快照時間 → 一個 cycle。
- 每個 cycle 的 `artifact_id = attribute_artifact(start_wall, …)`。
- `attribute_artifact`：找同 host 中 `t_wall <= ts` 的最後快照 p 與 `t_wall >= ts` 的第一個快照 n；兩者都存在、`daemon_ok`、`artifact_check == "verified"`、`artifact_id` 相同且非 None、`daemon_pid` 相同 → 該 artifact；否則 None（模糊不計）。
- `active_days`：本地日期一律以 `canary_policy.local_date(quota, ts)`（含 `utc_offset_changes`）換算。某本地日期符合 (a) 該日歸屬 `artifact_id` 的相異 submit 數 ≥ `active_day_min_distinct_submits`（`require_terminal_result` 時只計有終態 `submit_result` 者），且 (b) 該日快照覆蓋秒數 ≥ `active_day_min_snapshot_coverage_s`，覆蓋＝同日、同 artifact、`daemon_ok` 的相鄰快照對，間隔 ≤ `3 × collector_interval_s` 者的間隔總和。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_canary_classify.py`，關鍵測試：

```python
from __future__ import annotations

import pytest

import stage3_fakes as fakes
from tools.stage3 import canary_classify as cc
from tools.stage3 import canary_policy

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


@pytest.fixture
def pol(tmp_path):
    return canary_policy.load_policy(fakes.write_policy(tmp_path / "p.json"))


def _snaps(states, start_min=0, step_s=30, gens=None):
    out = []
    for i, st in enumerate(states):
        gen = (gens or [1] * len(states))[i]
        total = start_min * 60 + i * step_s
        out.append(fakes.snapshot(fakes.ts(3, 1, total // 60, total % 60), sessions=(("COM0", st, gen),)))
    return out


def test_release_attach_counts_once(pol):
    snaps = _snaps(["READY", "RELEASED", "RELEASED", "ATTACHING", "READY"])
    cycles = cc.classify(snaps, [], [], [], pol, platform="linux")
    assert [c.type for c in cycles] == ["handoff_release_attach"]
    assert cycles[0].artifact_id == "art-1"


def test_episode_across_gap_not_counted(pol):
    snaps = _snaps(["READY", "RELEASED"]) + _snaps(["RELEASED", "READY"], start_min=30)
    assert cc.classify(snaps, [], [], [], pol, platform="linux") == []


def test_hotplug_requires_generation_bump(pol):
    same = _snaps(["READY", "DETACHED", "READY"], gens=[4, 4, 4])
    bump = _snaps(["READY", "DETACHED", "READY"], gens=[4, 4, 5])
    assert cc.classify(same, [], [], [], pol, platform="linux") == []
    assert [c.type for c in cc.classify(bump, [], [], [], pol, platform="linux")] == ["hotplug_reattach"]


def test_flash_not_counted_on_windows(pol):
    snaps = [dict(s, platform="windows") for s in _snaps(["READY", "FLASHING", "ATTACHED", "READY"])]
    assert cc.classify(snaps, [], [], [], pol, platform="windows") == []


def test_ambiguous_artifact_is_none():
    a = fakes.snapshot(fakes.ts(3, 1, 0), artifact="art-1", pid=10)
    b = fakes.snapshot(fakes.ts(3, 1, 1), artifact="art-2", pid=11)
    assert cc.attribute_artifact(fakes.ts(3, 1, 0, 30), [a, b]) is None
```

| 測試名 | 斷言 |
|---|---|
| `test_dut_reboot_from_boot_quiet` | 序列 `READY, READY(boot_quiet_remaining_s=12, ready_reconfirm_pending=True), READY(ready_reconfirm_pending=True), READY` → `["dut_reboot"]`、`result == "recovered_ready"` |
| `test_reconfirm_failed_is_not_steady` | `READY, READY(ready_reconfirm_failed=True)` 結尾 → 一個 `ended_non_ready` cycle，不計入配額 |
| `test_boot_quiet_ready_forms_episode` | 全程 state 皆為 `READY`、僅中間一筆帶 `boot_quiet_remaining_s` → `episodes` 非空（守住上述產品事實：不得把 `READY＋boot quiet` 當穩態） |
| `test_recovery_from_state` | `READY, RECOVERING, READY` → `["recovery"]` |
| `test_recovery_from_unsnapshotted_recover_tx` | 全 READY 快照＋兩筆 `recover_tx` 相隔 30 s → 1 個 `recovery`；相隔 300 s → 2 個 |
| `test_recover_tx_inside_episode_not_double_counted` | `READY, ATTACHED, READY` 期間有 `recover_tx` → 只 1 個 `recovery` |
| `test_unfinished_episode_dropped` | 序列末端停在 `RELEASED` → 無 cycle |
| `test_pid_change_splits_episode` | episode 中途 daemon pid 改變 → 無 cycle |
| `test_passthrough_attached_is_steady` | `command_capable=False` 的 COM 恆 ATTACHED → 無 episode；`platform="passthrough"` 同；使用者自訂名稱的 passthrough profile 亦同（不依 profile 名稱） |
| `test_coexist_bucket_and_cap` | 同 com 同日 7 個桶都有 human＋agent → 恰 5 個 `human_agent_coexist`（cap） |
| `test_planned_restart_cycle` | pid 100→101 且 note 覆蓋 → `["planned_restart"]`；中間夾一個 `daemon_ok=False` 快照仍成立；無 note → 無 cycle（由 Task 11 開事故） |
| `test_unverified_artifact_not_attributed` | 兩側快照 `artifact_check="version_mismatch"` → `attribute_artifact` 為 None |
| `test_active_day_threshold` | 某日 19 個（皆有 `done` 結果）submit → 非活躍；20 個且覆蓋 3,600 s → 活躍；20 個但其中 1 個結果 `unknown` → 非活躍 |
| `test_active_day_uses_utc_offset` | policy 覆寫 `platforms.linux.utc_offset_minutes=480`，submit 在 `2026-11-03T17:00:00+00:00` → 計入本地 `2026-11-04` |
| `test_active_day_other_artifact_excluded` | 20 個 submit 歸屬 art-2，查 art-1 → `[]` |
| `test_coverage_ignores_gaps` | 快照間隔 10 分鐘（> 3×30 s）→ 覆蓋為 0，非活躍 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_canary_classify.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依規則；所有時間比較以 `jsonio.parse_iso`；排序穩定（同時間以 `record_id` 次序）；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 Canary 干擾／交接分類與活躍日判定`。

---

## Task 11：事故追蹤（`tools/stage3/canary_incidents.py`）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-11-canary-incidents.md`
**BLOCKED-OWNER:** BO-P9-09（分診與結案）。

**Files:**
- Create: `tools/stage3/canary_incidents.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_canary_incidents.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_canary_incidents.py`

**Interfaces — Consumes:** Task 1 `canary_policy`、`jsonio`、`reason_codes`；Task 10 只取 `canary_classify.steady`（純函式；本模組被打包進 zipapp，故 `canary_classify` 亦列入 Task 9 的 `COLLECTOR_MODULES`——Task 9 實作時一併加入）。

**Interfaces — Produces:**

```python
INCIDENT_KINDS: tuple[str, ...] = ("unexpected_restart", "unexplained_pid_change", "supervisor_failed_to_start", "daemon_unreachable",
                                   "stuck_attached", "wal_unhealthy", "wal_loss", "harvest_integrity", "multi_open", "collector_gap",
                                   "safety_violation", "other")
CLOSE_RESOLUTIONS: tuple[str, ...] = ("fixed_in_artifact", "not_product_defect", "environment", "duplicate")
SAFETY_ALLOWED_RESOLUTIONS: tuple[str, ...] = ("fixed_in_artifact", "duplicate")
NOTE_KINDS: tuple[str, ...] = ("planned_restart", "planned_downtime")
@dataclass(frozen=True)
class Incident:
    incident_id: str            # sha256(f"{kind}|{host_id}|{key}")[:12]
    kind: str
    platform: str
    host_id: str
    artifact_id: str | None
    opened_at: str
    detected_by: str            # "auto" | "manual"
    summary: str
    evidence: tuple[str, ...]
    status: str                 # "open" | "closed"
    closed_at: str | None
    resolution: str | None
    issue_ref: str | None
    closed_by: str | None
    note: str | None
def detect(snapshots: Sequence[dict], signals: Sequence[dict], notes: Sequence[dict], policy: CanaryPolicy,
           anomalies: Sequence[dict] = ()) -> list[Incident]
def write_note(ledger_dir: Path, *, kind: str, start: str, end: str, reason: str, author: str, host_id: str, platform: str) -> dict
def open_manual(ledger_dir: Path, *, kind: str, platform: str, host_id: str, artifact_id: str | None, summary: str,
                evidence: Sequence[str], now: str) -> Incident
def close(ledger_dir: Path, incident_id: str, *, resolution: str, issue_ref: str | None, note: str, closed_by: str, now: str,
          known: Mapping[str, Incident]) -> dict
def current_incidents(records: Sequence[dict], detected: Sequence[Incident]) -> dict[str, Incident]
```

本模組限 stdlib（會被打包進 Task 9 的 collector zipapp）。

自動偵測規則（`I = collector_interval_s`）。PID 規則以每 host「上一個 `daemon_ok` 快照 p」與「本 `daemon_ok` 快照 c」比較，**中間的失聯快照一律略過**，時間差用兩者實際 `t_wall` 差（不是相鄰快照）：

| kind | 規則 | key |
|---|---|---|
| `unexpected_restart` | p、c 的 `daemon_pid` 不同、`t_c - t_p ≤ 3I`，未被 `planned_restart` note 覆蓋 t_c，且**不是**已佐證的主機重開 | c `t_wall` |
| `unexplained_pid_change` | 同上但 `t_c - t_p > 3I`；或 p、c 的 `daemon_pid` 相同但兩者之間有 `error` 為 `not_running`／`connect_refused` 的快照（daemon 確定曾不存在，不能排除 PID 重用），未被 note 覆蓋 | c `t_wall` |
| `supervisor_failed_to_start` | 已佐證的主機重開後，該 boot 第一個快照起 `post_boot_grace_s` 內沒有任何 `daemon_ok` 快照，且該期間至少有一個快照 | 新 boot_id |
| `daemon_unreachable` | 同 boot 連續 ≥`unreachable_incident_polls` 個 `daemon_ok` 為 false 的快照、跨度 ≥2I，不在 post-boot grace 內，且未被 `planned_downtime` note 覆蓋 | 該段第一個 `t_wall` |
| `stuck_attached` | 同 com、同 daemon pid，連續 `canary_classify.steady(sess)` 為 false 的快照（排除 state `RELEASED`、`FLASHING`、`DETACHED`；passthrough／`command_capable=false` 的 ATTACHED 已由 `steady` 視為穩態）的 `last_t - first_t ≥ stuck_incident_s` | com＋段起點 |
| `wal_unhealthy` | `wal_healthy is False` 段的第一個快照 | 段起點 |
| `wal_loss` | `wal_loss` 或 `wal_recreated` 訊號（Task 8 已保證同一筆 META 只出一個訊號） | `f"{kind}:{seq}:{wall_ts}"` |
| `harvest_integrity` | `harvest_anomaly` 紀錄中 `malformed_line`、`crc_mismatch`、`seq_regress`、`truncated`、`replaced`、`vanished` 任一（擷取異常是證據完整性問題，不可只記錄後忽略） | anomaly `detail` |
| `multi_open` | `multi_open is True` 段的第一個快照 | 段起點 |
| `collector_gap` | 同 boot 相鄰快照間隔 > `gap_incident_s` | gap 起點 |
| `safety_violation`（auto） | (a) `unauthorized_tx` 訊號；(b) `resend_suspect` 訊號，且前次與本次之間該 com 有任一快照 state 為 `RECOVERING`／`RELEASED`／`DETACHED`、`bridge_generation` 與前次之前的值不同，或 daemon pid 變更；(c) `human_replay_suspect` 訊號，且兩次之間該 com 有 `RECOVERING` 或 `RELEASED` 快照（§11.3、§14.2；P2 `walscan` 的同義規則） | `f"{signal kind}:{seq}:{wall_ts}"` |
| `other`（auto） | `resend_suspect` 未符合 (b) 的跨上下文條件（同 cmd_id 同 payload 重送，仍須分診） | 同上 |

**已佐證的主機重開**＝p、c 的 `boot_id` 皆非 None 且不同，且 p 與「新 boot 第一個快照」之間沒有其他快照、間隔 ≥ `reboot_corroboration_s`（主機重開必然造成 collector 缺口）；任一條件不成立時 PID 變更一律開事故。非預期重啟即使 c 已恢復（supervisor 自動拉起）仍開事故（OD-10、§15.11.2）；已佐證的主機重開本身不是 daemon 事故。

`close` 驗證（違反 → `HarnessError("HARNESS_INCIDENT_INVALID_CLOSE")`）：id 未知或已關閉；`resolution` 不在 `CLOSE_RESOLUTIONS`；`kind=="safety_violation"` 而 resolution 不在 `SAFETY_ALLOWED_RESOLUTIONS`（安全不變量不可以環境或非產品缺陷豁免）；`fixed_in_artifact` 時 `issue_ref` 為 None；`note` 為空字串。結案寫入 `type="incident_close"` 紀錄（不修改既有紀錄）。

**Steps:**

- [ ] **Step 1：寫失敗測試**（agy）`tests/test_stage3_canary_incidents.py`，關鍵測試：

```python
def test_supervisor_recovered_restart_is_incident(pol):
    s0 = fakes.snapshot(fakes.ts(3, 1, 0, 0), pid=100)
    s1 = fakes.snapshot(fakes.ts(3, 1, 0, 30), pid=101)
    inc = canary_incidents.detect([s0, s1], [], [], pol)
    assert [i.kind for i in inc] == ["unexpected_restart"]
    assert inc[0].status == "open" and inc[0].detected_by == "auto"


def test_planned_note_suppresses(pol):
    s0 = fakes.snapshot(fakes.ts(3, 1, 0, 0), pid=100)
    s1 = fakes.snapshot(fakes.ts(3, 1, 0, 30), pid=101)
    note = {"type": "note", "kind": "planned_restart", "from": fakes.ts(3, 0, 55), "to": fakes.ts(3, 1, 5), "host_id": "h1"}
    assert canary_incidents.detect([s0, s1], [], [note], pol) == []


def test_safety_violation_cannot_close_as_environment(tmp_path, pol):
    inc = canary_incidents.open_manual(tmp_path, kind="safety_violation", platform="linux", host_id="h1", artifact_id="art-1",
                                       summary="unauthorized TX", evidence=["wal:120"], now=fakes.ts(3, 2))
    with pytest.raises(HarnessError) as ei:
        canary_incidents.close(tmp_path, inc.incident_id, resolution="environment", issue_ref=None, note="x",
                               closed_by="owner", now=fakes.ts(3, 3), known={inc.incident_id: inc})
    assert ei.value.code == "HARNESS_INCIDENT_INVALID_CLOSE"
```

| 測試名 | 斷言 |
|---|---|
| `test_host_reboot_not_incident` | boot_id b1→b2、pid 變更、兩快照間隔 120 s（≥ `reboot_corroboration_s`）→ 無 PID 類事故 |
| `test_boot_change_without_gap_is_incident` | boot_id b1→b2 但間隔 30 s（例如 Windows boot 身分誤翻）→ `unexpected_restart` |
| `test_boot_id_none_is_incident` | 兩快照 `boot=None`、pid 變更 → `unexpected_restart` |
| `test_pid_change_across_down_polls` | `pid=100 ok → down → down → pid=101 ok`（共 90 s）→ `unexpected_restart`；四筆共 20 分鐘 → `unexplained_pid_change`（另有 `daemon_unreachable`） |
| `test_pid_reuse_after_not_running` | `pid=100 → error not_running → pid=100` → `unexplained_pid_change` |
| `test_harvest_anomaly_is_incident` | 一筆 `crc_mismatch` anomaly → `harvest_integrity` |
| `test_unauthorized_tx_auto_safety` | `unauthorized_tx` 訊號 → `safety_violation`、`detected_by == "auto"` |
| `test_resend_across_generation_is_safety` | `resend_suspect` 的前次與本次之間 COM0 `bridge_generation` 1→2 → `safety_violation`；無跨越 → `other` |
| `test_human_replay_across_recovering_is_safety` | `human_replay_suspect` 前後之間 COM0 有 `RECOVERING` 快照 → `safety_violation` |
| `test_supervisor_failed_after_boot` | 新 boot 後 6 分鐘內皆 `daemon_ok=False` → `["supervisor_failed_to_start"]` |
| `test_gap_pid_change_unexplained` | 相隔 20 分鐘 pid 變更 → `unexplained_pid_change`＋`collector_gap` |
| `test_unreachable_three_polls` | 連 3 個 down 快照 → `daemon_unreachable`；2 個 → 無 |
| `test_stuck_attached_900s` | ATTACHED（`command_capable=True`）持續 15 分鐘 → `stuck_attached`；RELEASED 持續 1 天 → 無；使用者自訂名稱的 passthrough profile（`platform="passthrough"`）ATTACHED 1 天 → 無 |
| `test_wal_loss_signal` | `wal_loss` 訊號 → 一個 `wal_loss`；`wal_recreated` 訊號 → 一個 `wal_loss`（不重複） |
| `test_detect_is_deterministic` | 同輸入兩次 → incident_id 序列相同 |
| `test_close_requires_issue_for_fix` | `fixed_in_artifact`、`issue_ref=None` → `HARNESS_INCIDENT_INVALID_CLOSE` |
| `test_current_incidents_applies_close` | detect＋close 紀錄 → 該 id `status == "closed"`、`resolution` 正確 |
| `test_double_close_rejected` | 已關閉再 close → 例外 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_canary_incidents.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（haiku）依規則表；`write_note` 驗證 `kind ∈ NOTE_KINDS`、`start < end`、`reason` 非空；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 Canary 事故偵測與結案流程`。

---

## Task 12：Canary policy 凍結、帳本彙總與配額 gate（`tools/stage3/canary_ledger.py`）

**Test author model:** codex-gpt-6-luna-max
**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p9-12-canary-ledger.md`
**BLOCKED-OWNER:** BO-P9-06（policy 凍結）、BO-P9-07／08（配額累積）、BO-P9-09（事故結案）。

**Files:**
- Create: `tools/stage3/canary_ledger.py`
- Modify: `tools/stage3/canary_policy.py`（新增 `freeze_policy`）
- Create: `tests/fakes_canary.py`（`synthetic_canary`）
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_canary_ledger.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_canary_ledger.py`

**Interfaces — Consumes:** Task 1、8、10、11。
**Interfaces — Produces:**

```python
# tools/stage3/canary_policy.py（新增）
def freeze_policy(policy_path: Path, *, decision_id: str, frozen_by: str, evidence_owners: Mapping[str, str],
                  addenda_path: Path, now: str) -> dict
    # 前置：檔案現為 draft 且 load_policy 通過；evidence_owners 鍵恰為 {"linux","windows"}；否則 HarnessError("HARNESS_CANARY_POLICY_INVALID")
    # 寫回：status="frozen"、frozen_by、frozen_at=now、decision_ref=decision_id、platforms.*.evidence_owner（jsonio.atomic_write_text(dumps_report)）
    # addenda 附加一列：| <decision_id> | Canary policy 凍結 | <now 日期> | frozen_by=<…>；policy_sha256=<寫回後 sha>；evidence_owner linux=<…>、windows=<…> |
    # 回 {"policy_sha256","frozen_at","decision_ref"}

# tests/fakes_canary.py
def synthetic_canary(*, days: int, submits_per_day: int, cycles: Mapping[str, int], platform: str, artifact: str,
                     policy_sha256: str, start_day: int = 2, host: str = "h1", pid: int = 100) -> list[dict]
    # 開頭一筆 policy_ref（policy_sha256）；每日 06:00～18:00（UTC，配合 policy_dict 的 utc_offset_minutes=0，每日恰落在一個本地日）
    # 每 30 s 一筆全 READY、artifact_check="verified" 的快照（同 pid、同 artifact、同 boot）；submit 以 uuid_cmd_id 平均分布於該時段，
    # 每筆附 submit_result{status:"done"}；依 cycles 產生對應快照序列（RELEASED／DETACHED＋generation 遞增／READY＋boot_quiet_remaining_s＋
    # ready_reconfirm_pending／RECOVERING）或訊號（human_tx＋同 com submit，每桶一個），平均分配到各日；全部帶 record_id

# tools/stage3/canary_ledger.py
@dataclass(frozen=True)
class PlatformTally:
    platform: str
    artifact_id: str
    active_days: tuple[str, ...]
    submits: int                            # 有終態結果、且歸屬本 artifact 的相異 (host_id, cmd_id)
    submits_unknown_result: int             # 已送出但結果 unknown／未收斂者（不計入 submits）
    submits_result_evicted: int             # 其中 reason=="CMD_NOT_FOUND"（daemon 512 筆 history 淘汰前未取得結果，Task 9）；子集，不另加總
    cycles_by_type: Mapping[str, int]
    cycles_total: int                       # Σ_t min(cycles_by_type[t], floor(cycles_max_type_share × quota.cycles_total))
    open_incidents: tuple[str, ...]
    failing_incidents: tuple[str, ...]      # 本 artifact 的 safety_violation（不論是否結案），或以 fixed_in_artifact 結案者
    pre_freeze_ignored: int
    policy_mismatch: int                    # 歸屬於不同 policy_sha256 的 policy_ref 之後的紀錄數
    unreferenced: int                       # 前面沒有任何 policy_ref 的紀錄數（不計）
    carried_over: Mapping[str, int]
    first_counted_wall: str | None
@dataclass(frozen=True)
class CanaryGate:
    platform: str
    artifact_id: str
    result: str           # "PASS" | "INCOMPLETE" | "FAIL" | "POLICY_NOT_FROZEN" | "POLICY_CHANGED"
    reasons: tuple[str, ...]
    tally: PlatformTally
    policy_sha256: str
def load_records(paths: Sequence[Path]) -> list[dict]       # 合併多個 host 帳本；以 record_id 去重；malformed 行 → HarnessError("HARNESS_LEDGER_CORRUPT")
def tally(records: Sequence[dict], policy: CanaryPolicy, *, platform: str, artifact_id: str,
          carryovers: Sequence[dict] = ()) -> PlatformTally
def gate(records: Sequence[dict], policy: CanaryPolicy, *, platform: str, artifact_id: str,
         carryovers: Sequence[dict] = ()) -> CanaryGate
def report_md(gates: Sequence[CanaryGate]) -> str
```

`tally`：只納入 `platform` 相符的紀錄；每筆紀錄歸屬於同 host 帳本中時間在它之前的最後一筆 `policy_ref`——無 → 計入 `unreferenced`、不計；該 `policy_ref.policy_sha256 != policy.sha256` → 計入 `policy_mismatch`、不計（凍結後不得以修改過的 policy 重新解讀歷史資料，§12.2）；`wall_ts`／`t_wall` 早於 `policy.frozen_at` 的 submit／cycle 不計，計入 `pre_freeze_ignored`；submit 以 (`host_id`, `cmd_id`) 去重，且 `attribute_artifact` 必須等於 `artifact_id`，`require_terminal_result` 時只有 `submit_result.status ∈ {done,error,timeout}` 者計入 `submits`、其他計入 `submits_unknown_result`；cycles 由 `canary_classify.classify` 計算、只取 `result=="recovered_ready"` 並過濾 artifact，`cycles_total` 套每類型上限；incidents 由 `canary_incidents.detect`（含 `harvest_anomaly` 紀錄）＋紀錄計算，`artifact_id` 為本 artifact 或 None 者納入；`carryovers` 每筆 `{"type":"carryover","from_artifact","to_artifact","counters":[...],"approved_by","decision_ref","approved_at","platform"}`，只在 `to_artifact == artifact_id`、`platform` 相符、`approved_by`／`decision_ref` 非空時，把 `from_artifact` 的對應計數（`submits`、`cycles`，**不含 `active_days` 與事故**）加入 `carried_over` 並計入合計；`first_counted_wall` 為所有被計入紀錄的最早時間。

`gate` 判定（依序）：
1. `policy.status != "frozen"` → `POLICY_NOT_FROZEN`；`first_counted_wall` 非 None 時呼叫 `canary_policy.assert_frozen_before(policy, first_counted_wall)`，丟 `HARNESS_CANARY_POLICY_NOT_FROZEN` → `POLICY_NOT_FROZEN`。
2. `policy_mismatch > 0` → `POLICY_CHANGED`（reasons 含 `HARNESS_CANARY_POLICY_CHANGED` 與筆數）。
3. `failing_incidents` 非空 → `FAIL`。
4. 收集 INCOMPLETE 理由：`open_incidents` 非空（`"open incidents: <ids>"`）；`len(active_days) < quota.active_days`；`submits < quota.submits`；`cycles_total < quota.cycles_total`；任一 `cycles_by_type[t] < cycles_min_per_type[t]`（`f"cycle type {t}: {n}/{min}"`）；理由非空 → `INCOMPLETE`。
5. 否則 `PASS`。報告另列 `submits_unknown_result`（含子集 `submits_result_evicted`）、`unreferenced`、各類型被上限截掉的數量。

Linux 與 Windows 各自獨立呼叫；工具不提供跨平台合計（OD-10：不得以 Linux 量掩蓋 Windows）。CLI（Task 17）的 `canary report` 以 `load_policy(path, addenda_path=docs/rust-refactor/owner-decisions-addenda.md)` 載入，decision_ref 不在 addenda 即拒絕。

**Steps:**

- [ ] **Step 1：寫失敗測試**（luna）`tests/test_stage3_canary_ledger.py`（`pytestmark = [core_portable, memory_only]`），關鍵測試：

```python
def _pol(tmp_path, **over):
    return canary_policy.load_policy(fakes.write_policy(tmp_path / "p.json", **over))


def test_unfrozen_policy_never_passes(tmp_path):
    pol = _pol(tmp_path, status="draft", frozen_at=None, frozen_by=None, decision_ref=None,
               **{"platforms.linux.evidence_owner": None, "platforms.windows.evidence_owner": None})
    g = canary_ledger.gate([], pol, platform="linux", artifact_id="art-1")
    assert g.result == "POLICY_NOT_FROZEN"


def test_quota_complete_passes(tmp_path):
    pol = _pol(tmp_path)
    recs = fakes_canary.synthetic_canary(days=14, submits_per_day=360, cycles={"handoff_release_attach": 10, "hotplug_reattach": 5,
                                         "dut_reboot": 10, "recovery": 10, "human_agent_coexist": 15}, platform="linux",
                                         artifact="art-1", policy_sha256=pol.sha256)
    g = canary_ledger.gate(recs, pol, platform="linux", artifact_id="art-1")
    assert g.result == "PASS", g.reasons
    assert g.tally.submits == 5040 and len(g.tally.active_days) == 14 and g.tally.cycles_total == 50


def test_policy_edit_after_freeze_detected(tmp_path):
    pol = _pol(tmp_path)
    recs = fakes_canary.synthetic_canary(days=14, submits_per_day=360, cycles={"recovery": 20, "dut_reboot": 15, "handoff_release_attach": 15},
                                         platform="linux", artifact="art-1", policy_sha256=pol.sha256)
    edited = _pol(tmp_path, **{"active_day.min_distinct_submits": 5})
    g = canary_ledger.gate(recs, edited, platform="linux", artifact_id="art-1")
    assert g.result == "POLICY_CHANGED" and any("HARNESS_CANARY_POLICY_CHANGED" in r for r in g.reasons)


def test_other_artifact_not_merged(tmp_path):
    pol = _pol(tmp_path)
    recs = fakes_canary.synthetic_canary(days=14, submits_per_day=360, cycles={"recovery": 50}, platform="linux",
                                         artifact="art-1", policy_sha256=pol.sha256)
    g = canary_ledger.gate(recs, pol, platform="linux", artifact_id="art-2")
    assert g.result == "INCOMPLETE" and g.tally.submits == 0
```

| 測試名 | 斷言 |
|---|---|
| `test_per_type_minimum_blocks` | 50 個 cycle 全為 `recovery` → `INCOMPLETE`，reasons 含 `"cycle type handoff_release_attach: 0/10"`，`cycles_total == 20`（上限 0.4×50） |
| `test_type_share_cap` | `recovery` 40、`dut_reboot` 10、`handoff_release_attach` 10 → `cycles_total == 40`（recovery 只計 20） |
| `test_unknown_result_not_counted` | 某日 submit 的 `submit_result` 改為 `unknown` → `submits` 減少、`submits_unknown_result` 增加；其中 `reason:"CMD_NOT_FOUND"` 者另使 `submits_result_evicted` 增加，`submits` 不因此增加 |
| `test_unreferenced_records_not_counted` | 去掉開頭 `policy_ref` → `submits == 0`、`unreferenced > 0` |
| `test_open_incident_blocks` | 加一筆 pid 變更快照對 → `INCOMPLETE`，reasons 以 `"open incidents:"` 開頭 |
| `test_harvest_anomaly_blocks` | 加一筆 `crc_mismatch` 的 `harvest_anomaly` → `INCOMPLETE`（`harvest_integrity` 未結案） |
| `test_closed_not_product_defect_unblocks` | 上述 pid 事故以 `environment` 結案 → `PASS` |
| `test_fixed_in_artifact_fails_same_artifact` | 以 `fixed_in_artifact` 結案 → 對該 artifact `FAIL` |
| `test_safety_violation_fails` | 手動或自動 `safety_violation`（不論是否結案）→ `FAIL` |
| `test_pre_freeze_records_ignored` | `frozen_at` 設為 `ts(6, 0)`（synthetic 從第 2 天開始）→ 第 2～5 天（4 天）不計、`pre_freeze_ignored > 0`、`active_days` 只有 10 → `INCOMPLETE` |
| `test_gate_asserts_frozen_before_first_counted` | 以 monkeypatch 讓 `tally` 回傳 `first_counted_wall` 早於 `frozen_at` → `POLICY_NOT_FROZEN`（證明 `gate` 確實呼叫 `assert_frozen_before`） |
| `test_carryover_excludes_active_days` | 核准 carryover `counters:["submits"]` → submits 合計增加、`active_days` 不變 |
| `test_carryover_without_approval_ignored` | `approved_by` 為空 → 不計入 |
| `test_windows_independent` | linux 滿額、windows 無資料 → windows `INCOMPLETE` |
| `test_merge_two_hosts_dedup` | 同一帳本檔載入兩次 → `load_records` 長度不變 |
| `test_corrupt_ledger_rejected` | 帳本含 `{bad` 行 → `HARNESS_LEDGER_CORRUPT` |
| `test_report_md_lists_reasons` | `report_md` 含平台、artifact、每個 reason 字串、`submits_unknown_result` |
| `test_freeze_policy_writes_and_appends` | draft policy＋`freeze_policy(...)` → 檔案 `status=="frozen"`、`decision_ref`、兩平台 `evidence_owner` 已寫；addenda 新增恰一列含 `policy_sha256=<新 sha>`；`load_policy(path, addenda_path=addenda)` 成功 |
| `test_freeze_policy_refuses_refreeze` | 已 frozen 再 freeze → `HARNESS_CANARY_POLICY_INVALID` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_canary_ledger.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（agy）依規則；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 Canary policy 凍結、帳本彙總與分平台配額 gate`（files 含 `tests/fakes_canary.py`）。

---

## Task 13：實機回滾演練（`tools/stage3/rollback_drill.py`）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-13-rollback-drill.md`
**BLOCKED-OWNER:** BO-P9-10（Linux 實機）、BO-P9-11（Windows `windows-task` 實機）。演練具破壞性（會停止並重啟 daemon、重新取得 UART），只能由擁有者在指定 bench 明確授權後執行。

演練驗證規格 §12.1：「回滾驗收須包括真實串口釋放與重新取得，不只確認版本字串改變」；`setup --rollback` 不得只切 CLI；「啟動新版本失敗時回到完整 Previous 組合」（第 7 步）。

**產品事實**：`bridge_generation` 預設 0、不持久化、只在 attach 時遞增（`sw_core/session_manager.py:310`、`:2708`、`:3132`；盤點 05 §1.6；Rust 同語意，arch §5.3）。daemon 重啟後第一個 READY 的 generation 通常是 1，可能等於或小於重啟前的值，因此**跨重啟**不得以 generation 遞增判定重新取得；generation 只用於同一 pid 內的比較（D7）。

**Files:**
- Create: `tools/stage3/rollback_drill.py`
- Create: `tests/fakes_drill.py`：`drill_runner(root, *, boards, current, previous, d1_generation=3, reacquire=True, pairing_ok=True, flashing=False, smoke_ok=True, corrupt_refused=True, start_fail_reverts=True) -> RecordingRunner`（依呼叫次序切換：`setup --rollback` 前回 current／pid 100；之後回 previous／pid 101、`bridge_generation` 重設為 1、`last_ready_at` 為切換後時間（`reacquire=False` 時維持 D1 的舊時間）；forward setup 後回 current／pid 102；corrupt bundle 的 setup 依 `corrupt_refused` 回 rc=2 `ok:false` 或 rc=0 `ok:true`（後者並改 version）；start-fail bundle 的 setup 回 rc=2 `ok:false`，並依 `start_fail_reverts` 之後回 current／pid 103／新 `last_ready_at`，或回 start-fail 版本／daemon 不存活）；`drill_config(root, runner, *, corrupt=False, start_fail=False, **over) -> DrillConfig`（預設 boards=("COM0","COM1")、expect_current="0.9.0"、expect_previous="0.8.0"）
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_rollback_drill.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_rollback_drill.py`

**Interfaces — Consumes:** P4 `setup --rollback`、`setup`（失敗自動回到完整 Previous）、`doctor` 的 `checks: [{"check","ok","detail","fix"}]`、安裝佈局；Task 4 `release_check`；CLI 欄位 `daemon status.{pid,version,multi_open,foreign_holders}`、`session list.sessions[].{com,alias,state,bridge_generation,device_by_id,attached_real_path,last_ready_at}`、`mcu status.flashing`、`cmd submit`→`cmd_id`、`cmd status`→`command.{status,stdout}`。
**Interfaces — Produces:**

```python
STEP_IDS: tuple[str, ...] = ("D0-preflight", "D1-record", "D2-rollback", "D3-verify-previous", "D4-smoke-previous",
                             "D5-roll-forward", "D6-verify-current", "D7-invalid-artifact-refusal", "D8-start-failure-revert")
@dataclass(frozen=True)
class DrillConfig:
    cli: Path                        # 穩定入口
    platform: str
    boards: tuple[str, ...]
    expect_current: str
    expect_previous: str
    forward_argv: tuple[str, ...]    # 預設 (str(<current bundle>/bin/serialwrap), "setup")，由 D1 推得；可由參數覆寫
    corrupt_from: Path | None        # 給定時執行 D7
    start_fail_from: Path | None     # 給定時執行 D8：擁有者以 `mkbundle.sh --dev --version-override <較新版號>` 建出的 bundle
    ready_timeout_s: float = 180.0
    setup_timeout_s: float = 300.0
    out_dir: Path
@dataclass(frozen=True)
class StepResult:
    step_id: str
    ok: bool
    detail: Mapping[str, Any]
    started_at: str
    finished_at: str
@dataclass(frozen=True)
class DrillResult:
    verdict: str                     # "PASS" | "FAIL" | "PRECONDITION_FAILED" | "INCOMPLETE"
    reason_code: str | None
    steps: tuple[StepResult, ...]
    restore: StepResult | None
    report_path: Path
def make_corrupted_bundle(src: Path, dst: Path) -> Path
    # 複製 bundle；把 release-manifest.json 的 components.cli.sha256 最後一個 hex 字元改成另一值；不重算
def make_start_fail_bundle(src: Path, dst: Path) -> Path
    # 複製 bundle；把 components.daemon.launcher.path 指的檔案換成「立即 exit 3」的 stub（POSIX shell 腳本；Windows 為最小 .exe 包裝或 .cmd，
    # 依 bundle target）；重算該 component 的 sha256 與 SHA256SUMS，使 manifest 自洽、驗證通過，但新版 daemon 啟動必定失敗
def run_drill(cfg: DrillConfig, *, run: Runner = run_cmd, probe: ReleaseProbe | None = None, clock=time.monotonic,
              sleep=time.sleep, now=jsonio.utc_now_iso, marker: Callable[[], str] = lambda: secrets.token_hex(3)) -> DrillResult
```

`reacquired(board, since)`（D3、D6、D8 共用）：該板 `state=="READY"`、`last_ready_at` 晚於 `since`（該步驟開始時間）、`attached_real_path`／`device_by_id`／`alias` 與 D1 記錄相同。

步驟（任一步失敗即停止後續步驟、執行 restore、verdict `FAIL`、`HARNESS_DRILL_STEP_FAILED`；D0 失敗為 `PRECONDITION_FAILED`、`HARNESS_DRILL_PRECONDITION`，不執行任何變更）：

| 步驟 | 動作 | 通過條件 |
|---|---|---|
| D0-preflight | `daemon status`、`mcu status`、`session list` | `ok` 且 `running`；`flashing` 為 false；所有 `boards` 為 READY；`--version` 含 `expect_current` |
| D1-record | `--version`、`daemon status`、`doctor`、`session list`；解析安裝佈局（Linux `realpath(cli)`；Windows `bin/current-release.json`） | `doctor.checks` 中 `check=="version_pairing"` 的那一列 `ok is True`；記錄 old_pid、每個 session 的 `alias`／`device_by_id`／`attached_real_path`／`state`（含非 board 的 `RELEASED` session 集合）、current bundle 目錄與其 `release-manifest.json` sha256 |
| D2-rollback | 記錄 `t2=now()`；`[cli, "setup", "--rollback"]`（timeout `setup_timeout_s`） | rc 0 且 stdout JSON `ok:true` |
| D3-verify-previous | 輪詢 `daemon status`、`session list`（每 2 s，至 `ready_timeout_s`）；`doctor`；`release_check` 確認 old_pid 已結束 | `--version` 與 `daemon status.version` 均含 `expect_previous`；`pid != old_pid`；`probe.pid_alive(old_pid) is False`；`version_pairing.ok`；安裝佈局指向 previous；每板 `reacquired(board, t2)`；D1 的 `RELEASED` session 仍為 `RELEASED`、所有 alias 不變（持久化狀態跨回滾保留，§12.1）；`multi_open` 為 false；Linux `foreign_holders` 除新 pid 外為空 |
| D4-smoke-previous | 每板 `cmd submit --selector <com> --cmd "echo SW''DRILL_<marker>" --cmd-timeout 12`，再輪詢 `cmd status --cmd-id` | `command.status == "done"` 且 `stdout` 含 `SWDRILL_<marker>`（命令回顯是 `SW''DRILL`，不會誤中） |
| D5-roll-forward | 記錄 `t5`；`forward_argv` | rc 0 且 `ok:true` |
| D6-verify-current | 同 D3＋D4，預期 `expect_current`、pid 再變更、`reacquired(board, t5)` | 同 D3＋D4 |
| D7-invalid-artifact-refusal（`corrupt_from` 給定時） | `make_corrupted_bundle(corrupt_from, out_dir/"corrupt")` 後執行其 `bin/serialwrap setup` | rc ≠ 0 或 `ok:false`；之後 `--version`、`daemon status.version` 仍為 `expect_current`、`pid` 未變、各板 READY 且 `bridge_generation` 未變（同一 pid 內比較；拒絕發生在啟用前，規格 §12.1 第 1 步） |
| D8-start-failure-revert（`start_fail_from` 給定時） | 記錄 `t8` 與當下 pid；`make_start_fail_bundle(start_fail_from, out_dir/"startfail")` 後執行其 `bin/serialwrap setup` | rc ≠ 0 或 `ok:false`；之後在 `ready_timeout_s` 內：`--version`、`daemon status.version` 含 `expect_current`、安裝佈局指向 current、`version_pairing.ok`、t8 時的 pid 已結束（`release_check`）且新 pid 存活、每板 `reacquired(board, t8)`、D4 smoke 通過、`versions/` 下 start-fail 版本未成為 Current（§12.1 第 7 步：失敗時回到完整 Previous 組合並可操作） |
| restore（失敗時） | 依目前佈局：若為 previous → 執行 `forward_argv`；若為其他 → 不動作並記錄 | 結果寫入報告；restore 成功不改變 FAIL 判定 |

全部步驟通過且 D7、D8 都執行 → `PASS`；全部通過但 `corrupt_from` 或 `start_fail_from` 未提供 → `INCOMPLETE`、`HARNESS_DRILL_INCOMPLETE`，報告 `limitations` 列 `"step1_refusal_not_exercised"`／`"step7_revert_not_exercised"`。`INCOMPLETE` 經 Task 14B ingest 為 `INSUFFICIENT`，`S3-ROLLBACK-*` 因此維持 BLOCKED，不得當作 PASS。

不得呼叫 `session interactive-status`、`interactive-open`、`device release`、`--force` 類旗標。

**Steps:**

- [ ] **Step 1：寫失敗測試**（agy）`tests/test_stage3_rollback_drill.py`（`pytestmark = [core_portable, memory_only]`；以 `fakes_drill.drill_runner` 腳本模擬 CLI 回應：重啟後 `bridge_generation` 一律重設為 1、`last_ready_at` 為重啟後時間），關鍵測試：

```python
def test_happy_drill_order_and_reacquire_checks(tmp_path):
    runner = fakes_drill.drill_runner(tmp_path, boards=("COM0", "COM1"), current="0.9.0", previous="0.8.0", d1_generation=7)
    cfg = fakes_drill.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    clock = fakes.FakeClock()
    res = rollback_drill.run_drill(cfg, run=runner, probe=fakes.FakeProbe(), clock=clock.now, sleep=clock.sleep,
                                   now=clock.iso, marker=lambda: "abc123")
    assert res.verdict == "PASS"                      # D1 generation 7、重啟後 1：不得因此判失敗
    assert [s.step_id for s in res.steps] == list(rollback_drill.STEP_IDS)
    subs = [c["sub"] for c in runner.calls if c["kind"] == "cli"]
    assert subs.index("setup") < subs.index("cmd submit")
    assert all("interactive" not in s for s in subs)


def test_version_flip_without_reacquire_fails(tmp_path):
    runner = fakes_drill.drill_runner(tmp_path, boards=("COM0",), current="0.9.0", previous="0.8.0", reacquire=False)
    cfg = fakes_drill.drill_config(tmp_path, runner, corrupt=True, start_fail=True)
    clock = fakes.FakeClock()
    res = rollback_drill.run_drill(cfg, run=runner, probe=fakes.FakeProbe(), clock=clock.now, sleep=clock.sleep, now=clock.iso)
    assert res.verdict == "FAIL" and res.reason_code == "HARNESS_DRILL_STEP_FAILED"
    d3 = [s for s in res.steps if s.step_id == "D3-verify-previous"][0]
    assert d3.ok is False and "last_ready_at" in json.dumps(d3.detail)
    assert res.restore is not None and res.restore.step_id == "restore"
```

（`reacquire=False`：回滾後版本字串已變，但各板 `last_ready_at` 仍是 D2 之前的時間——串口未真正重新取得。）

| 測試名 | 斷言 |
|---|---|
| `test_flashing_precondition` | `flashing=True` → `PRECONDITION_FAILED`、`HARNESS_DRILL_PRECONDITION`、無 `setup` 呼叫 |
| `test_board_not_ready_precondition` | COM1 ATTACHED → 同上 |
| `test_pairing_failure_after_rollback` | `pairing_ok=False`（D3 時 doctor 的 `version_pairing` 列 `ok:false`）→ D3 失敗 |
| `test_doctor_checks_list_shape` | doctor 回 `{"checks":[{"check":"python","ok":true},{"check":"version_pairing","ok":true}]}` → D1 通過；缺 `version_pairing` 列 → D1 失敗 |
| `test_old_pid_still_alive_fails` | `FakeProbe(pid_alive={old_pid:[True]})` → D3 失敗 |
| `test_device_binding_changed_fails` | 回滾後 COM0 `device_by_id` 改變 → D3 失敗 |
| `test_released_session_lost_fails` | D1 時 COM2 為 `RELEASED`，回滾後變 `READY` → D3 失敗 |
| `test_smoke_marker_missing_fails` | `smoke_ok=False` → D4 失敗 |
| `test_smoke_marker_not_matched_by_echo` | stdout 只有回顯 `echo SW''DRILL_abc123` → D4 失敗 |
| `test_d7_corrupt_bundle_refused` | `corrupt_refused=True` → D7 通過 |
| `test_d7_corrupt_bundle_accepted_fails` | `corrupt_refused=False` → D7 失敗 |
| `test_d8_start_failure_reverted` | `start_fail_reverts=True` → D8 通過，且 D8 之後 pid 變更、`last_ready_at` 晚於 t8 |
| `test_d8_not_reverted_fails` | `start_fail_reverts=False`（版本停在 start-fail 版本或 daemon 不存活）→ D8 失敗 |
| `test_missing_start_fail_is_incomplete` | `start_fail=False` → `INCOMPLETE`、`HARNESS_DRILL_INCOMPLETE`、`limitations` 含 `"step7_revert_not_exercised"` |
| `test_make_corrupted_bundle_changes_one_digest` | 原、新 manifest 只有 `components.cli.sha256` 不同，且不同處恰一字元 |
| `test_make_start_fail_bundle_self_consistent` | 新 bundle 的 `components.daemon.launcher.sha256` 等於 stub 實際 sha256、`SHA256SUMS` 相符；其他 component 不變 |
| `test_no_forbidden_commands` | 所有 cli 呼叫的 sub 不在 `{"session interactive-status","session interactive-open","device release"}`，argv 不含 `--force` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_rollback_drill.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依步驟表；報告 `out_dir/drill-report.json`（`dumps_report`），含每步 detail、使用的 cli 路徑與其 sha256、current／previous bundle 的 `release-manifest.json` sha256（供 ingest 核對 artifact）、D1 記錄值、`limitations`；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。【BLOCKED-OWNER BO-P9-10／11】實機執行指令見 Task 17 的 `docs/rust-refactor/procedures/rollback-drill.md`；本 Task 不執行實機。
- [ ] **Step 5：commit** `feat(stage3): 新增實機回滾演練程序（串口重新取得與啟動失敗自動回復）`（files 含 `tests/fakes_drill.py`）。

---

## Task 14：Stage 0～3 出口證據索引（`tools/stage3/evidence_index.py`）

**Test author model:** codex-gpt-6-luna-max
**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p9-14-evidence-index.md`
**BLOCKED-OWNER:** BO-P9-14（所有 `owner_blocked:true` 項目的證據產出與登錄）。

**Files:**
- Create: `tools/stage3/evidence_index.py`
- Create: `tests/fakes_evidence.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_evidence_index.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_evidence_index.py`

**Interfaces — Consumes:** `tests/acceptance/stage_exits.json`、`docs/rust-refactor/evidence/index.json`、P2 `cases.yaml`、Task 3 `FrozenFile`／`runtime_digest`。
**Interfaces — Produces:**

```python
RESULTS: tuple[str, ...] = ("PASS", "FAIL", "SKIP", "XFAIL", "XPASS", "INFRA_ERROR", "INSUFFICIENT")
STAGES: tuple[str, ...] = ("0", "1A", "1B", "2.1", "2.2", "2.3", "2.4", "3")
PLATFORMS: tuple[str, ...] = ("linux", "windows")
STAGE_EXITS_PATH: Path
INDEX_PATH: Path
@dataclass(frozen=True)
class ExitItem:
    item_id: str
    stage: str
    platform: str                # "linux" | "windows" | "both"
    layer: str
    title: str
    spec_ref: str
    owner_blocked: bool
    candidate_longrun: bool
    evidence_kind: str
    required_case_set: str | None
@dataclass(frozen=True)
class EvidenceEntry:
    entry_id: str               # record_id(其餘欄位)
    item_id: str
    platform: str               # "linux" | "windows"
    result: str
    artifact_id: str            # sha256(release-manifest.json bytes)
    runtime_digest: str         # Task 3 runtime_digest(manifest)
    contract_revision: str
    tool_revision: str
    case_list_sha256: str       # cases.lock.json 的 sha256（不適用者為 "0"*64）
    workload_sha256: str | None # L3：sha256(dumps_compact([[path, sha256_file(path)] for path in workload.engine_files]))；其他 None
    environment_id: str         # environment_id(env_doc)
    produced_at: str
    started_at: str
    producer: str               # "ingest:<kind>" | "manual"
    files: tuple[FrozenFile, ...]   # 相對 evidence_root；PASS／XFAIL 不得為空
    xfail_cases: tuple[str, ...]
    reuse_of: str | None
    note: str
ENV_KEYS: tuple[str, ...] = ("os_build", "cpu", "ram_mb", "filesystem", "av_product", "drivers", "board_firmware", "toolchains")
def environment_id(env_doc: Mapping[str, Any]) -> str      # 鍵必須恰為 ENV_KEYS；sha256(dumps_compact(env_doc))
def load_exit_catalog(path: Path = STAGE_EXITS_PATH) -> dict[str, ExitItem]
def load_index(path: Path = INDEX_PATH) -> dict
def set_target(index_path: Path, *, stage: str, platform: str, release_manifest: Path, contract_revision: str, tool_revision: str,
               case_list_sha256: str, set_by: str, now: str) -> None
    # 寫 stage_targets[stage][platform] = {"artifact_id": sha256(manifest bytes), "runtime_digest", "contract_revision", "tool_revision",
    # "case_list_sha256", "set_by", "set_at"}，並附加 target_history（不刪舊紀錄）。Linux 與 Windows bundle 的 manifest 必然不同，因此各自一個 target
def add_entry(index_path: Path, entry: EvidenceEntry, *, catalog: Mapping[str, ExitItem]) -> None
    # item 不在目錄 → HARNESS_EVIDENCE_UNKNOWN_ITEM；entry_id 重複 → HARNESS_EVIDENCE_DUPLICATE；
    # platform 不屬該 item（item.platform 為單一平台而不同）、或 result ∈ {PASS, XFAIL} 且 files 為空、或 result ∈ {PASS, XFAIL} 且
    # producer=="manual" 而 item.evidence_kind != "manual-procedure" → HARNESS_EVIDENCE_INGEST_INVALID；只附加
def item_status(index: dict, item: ExitItem, *, evidence_root: Path, cases: Mapping[str, dict]) -> tuple[str, list[str]]
    # "SATISFIED" | "MISSING" | "FAIL" | "STALE" | "HASH_MISMATCH" | "NO_TARGET"
def stage_status(index: dict, stage: str, *, catalog: Mapping[str, ExitItem], evidence_root: Path, cases: Mapping[str, dict]) -> dict
    # {"stage","targets":{platform:{…}|None},"result":"PASS"|"BLOCKED"|"FAIL",
    #  "items":{item_id:{"status","reasons","owner_blocked","satisfying_entries":[entry_id]}}}
def render_report(index: dict, catalog: Mapping[str, ExitItem], *, evidence_root: Path, cases: Mapping[str, dict]) -> str   # Markdown：每 stage 一表
```

`evidence_root` 為必填（沒有可解析的證據位置就不可能 SATISFIED）；`cases` 為 `cases.yaml` 的 `case_id → 條目` 對照。

`item_status` 規則——對 item 適用的每個平台 P（`platform:"both"` 時為 linux 與 windows 兩者）以 `stage_targets[item.stage][P]` 分別判定，再合併：
1. 無該平台 target → `NO_TARGET`。
2. 取 `item_id` 與 `platform` 皆相符的 entries；`artifact_id`、`contract_revision`、`tool_revision` 都等於 target，且 `item.required_case_set` 非 null 時 `case_list_sha256` 也等於 target 者為「有效集合」，其他為過期（§0.3、§11.6：契約、artifact、工具或案例清單改變後不得沿用舊 PASS）。
3. 有效集合中有 `FAIL` → `FAIL`（同一 artifact 的 FAIL 不被後來的 PASS 抵銷；不得挑選最好一輪）。
4. 有效集合中存在 `PASS`，或 `XFAIL` 且每個 `xfail_cases` 在 `cases` 中為 `expected: "XFAIL"`、`sentinel` 非 null 且 `sentinel.expires > entry.produced_at`（§11.4：明確 issue、固定特徵、核准期限）→ 再驗：`files` 為空 → `MISSING`；逐一比對 `evidence_root / files[].path` 的 sha256，缺檔或不符 → `HASH_MISMATCH`；`reuse_of` 非 None 時，被引用 entry 必須存在、本身依本規則（遞迴，以已走訪集合偵測循環）為 `PASS` 且檔案驗證通過，且與本 entry 的 `runtime_digest`、`contract_revision`、`tool_revision`、`case_list_sha256`、`workload_sha256`、`environment_id` 全等（比對 `runtime_digest` 而非 `artifact_id`：Stage 3 bundle 的 manifest 因 `stage`／`retirement`／`created_at` 必然與 2.4 不同，但受測 binary 相同時可沿用，§11.6 末段），否則 `STALE`（reasons 說明：`reused entry missing`／`reused entry not PASS`／`reuse cycle`／`reuse identity differs: <field>`）；通過 → `SATISFIED`，`satisfying_entries` 記下該 entry_id。
5. 有效集合僅有 `SKIP`／`INFRA_ERROR`／`INSUFFICIENT`／`XPASS`，或 XFAIL 不符條件 → `MISSING`（reasons 列出結果）；只有過期 entries → `STALE`；完全沒有 → `MISSING`。
6. 合併：任一平台 `FAIL` → `FAIL`；全部 `SATISFIED` → `SATISFIED`；否則依 `HASH_MISMATCH` > `STALE` > `NO_TARGET` > `MISSING` 取最嚴重者；reasons 以 `"<platform>: "` 為前綴。

`stage_status.result`：任一 item `FAIL` → `FAIL`；全部 `SATISFIED` → `PASS`；其他 → `BLOCKED`。`satisfying_entries` 供擁有者把 `behavior_changes.yaml`／`error_codes.yaml` 的對應項目由 `IMPLEMENTED` 改為 `VERIFIED` 時引用（§0.3 生命週期；狀態變更本身屬擁有者步驟，見 Task 17 `evidence-index.md`）。

**`tests/fakes_evidence.py`：** `evidence_index(root, *, stage, artifact_manifest: Path | None = None, contract, tool="t"*40, case_list="0"*64, platforms=("linux","windows")) -> Path`（建 index.json、`evidence/` 目錄，並為每個平台以 `make_bundle(target=…)` 建出不同的 manifest 後設定 target）；`entry(root, item_id, result, *, platform, n=0, files=None, **over) -> EvidenceEntry`（預設 artifact／runtime_digest／contract／tool／case_list 取自該平台 target；`files=None` 時在 `root/evidence/<item>-<platform>-<n>.json` 寫一個 stub 報告並記錄真實 sha256；`environment_id=environment_id(固定 env_doc)`、`produced_at=ts(10+n,0)`、`producer="ingest:test"`）。

**Steps:**

- [ ] **Step 1：寫失敗測試**（luna）`tests/test_stage3_evidence_index.py`（`pytestmark = [core_portable, memory_only]`），關鍵測試：

```python
def _status(idx, root, cat, item_id, cases=None):
    return evidence_index.item_status(evidence_index.load_index(idx), cat[item_id], evidence_root=root, cases=cases or {})


def test_fail_is_sticky_for_same_artifact(tmp_path):
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(idx, fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "FAIL", platform="linux", n=1), catalog=cat)
    evidence_index.add_entry(idx, fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=2), catalog=cat)
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "FAIL"


def test_both_platform_item_needs_both_targets_and_entries(tmp_path):
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(idx, fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="linux"), catalog=cat)
    assert _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")[0] == "MISSING"
    evidence_index.add_entry(idx, fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="windows"), catalog=cat)
    assert _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")[0] == "SATISFIED"   # 兩平台 artifact_id 不同仍可全數通過


def test_contract_change_makes_pass_stale(tmp_path):
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="d" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(idx, fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", contract_revision="c" * 40),
                             catalog=cat)
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "STALE"
```

| 測試名 | 斷言 |
|---|---|
| `test_unknown_item_rejected` | `add_entry` item `S9-X` → `HARNESS_EVIDENCE_UNKNOWN_ITEM` |
| `test_duplicate_entry_rejected` | 同 entry 加兩次 → `HARNESS_EVIDENCE_DUPLICATE` |
| `test_pass_without_files_rejected` | `files=()` 的 PASS → `HARNESS_EVIDENCE_INGEST_INVALID` |
| `test_manual_pass_rejected_for_tool_items` | `producer="manual"` 的 PASS 於 `S3-ROLLBACK-LINUX` → `HARNESS_EVIDENCE_INGEST_INVALID`；於 `S24-P2-1-MCU-FLASH`（`manual-procedure`）→ 接受 |
| `test_wrong_platform_rejected` | `S3-ROLLBACK-LINUX` 的 entry `platform="windows"` → `HARNESS_EVIDENCE_INGEST_INVALID` |
| `test_skip_is_not_pass` | 只有 SKIP → `MISSING` |
| `test_tool_revision_change_stale` | target `tool_revision` 與 entry 不同 → `STALE` |
| `test_case_list_change_stale` | `S3-L2-FULL-AB-LINUX` 的 entry `case_list_sha256` 與 target 不同 → `STALE` |
| `test_xfail_requires_unexpired_sentinel` | `cases` 該案 `expected:"XFAIL"`、sentinel `expires` 晚於 produced_at → `SATISFIED`；過期 → `MISSING`；`expected:"PASS"` 的案例（例如目前的 f4）→ `MISSING` |
| `test_hash_mismatch_detected` | `evidence_root` 中檔案被改 → `HASH_MISMATCH`；檔案被刪 → `HASH_MISMATCH` |
| `test_reuse_by_runtime_digest` | S3 longrun entry `reuse_of` 一筆 S24 entry：兩者 `artifact_id` 不同但 `runtime_digest` 等其餘欄位全同、S24 entry 檔案完整 → `SATISFIED`；`workload_sha256` 不同 → `STALE` |
| `test_reuse_of_failed_or_missing_entry_stale` | 被引用 entry 為 FAIL、不存在、或其檔案被刪 → `STALE` |
| `test_reuse_cycle_stale` | A `reuse_of` B、B `reuse_of` A → `STALE`（reasons 含 `reuse cycle`） |
| `test_stage_blocked_until_all_items` | Stage 3 只有部分 SATISFIED → `result == "BLOCKED"` |
| `test_stage_status_lists_satisfying_entries` | SATISFIED 項的 `satisfying_entries` 含對應 entry_id |
| `test_set_target_per_platform_appends_history` | linux、windows 各設一次、linux 再設一次 → `target_history` 長 3、`stage_targets["3"]["linux"]` 為最後一次、`["windows"]` 不受影響 |
| `test_environment_id_requires_keys` | 缺 `av_product` → `ValueError`；鍵順序不同 → 同一 id |
| `test_index_file_stable_format` | 寫出的 index.json `== dumps_report(json.loads(...))` |
| `test_render_report_marks_owner_blocked` | 報告中 owner_blocked 項目帶「BLOCKED-OWNER」字樣 |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_evidence_index.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（agy）依規則；index 寫入用 `atomic_write_text(dumps_report(...))`；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增 Stage 0～3 出口證據索引與分平台判定`（files 含 `tests/fakes_evidence.py`）。

---

## Task 14B：證據 ingest（`tools/stage3/evidence_ingest.py`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-14b-evidence-ingest.md`
**BLOCKED-OWNER:** BO-P9-14（真實報告的登錄）。

出口證據的 `result` 不得由操作者自行宣稱：ingest 解析產生工具自己的報告、推導結果、核對身分、把報告複製進 `evidence_root` 並逐檔記錄 sha256。

**Files:**
- Create: `tools/stage3/evidence_ingest.py`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_evidence_ingest.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_evidence_ingest.py`

**Interfaces — Consumes:** Task 14；Task 6 `ab-report.json`；Task 12 `canary report` 輸出（`CanaryGate` 的 JSON）；Task 13 `drill-report.json`；Task 15 `od09 freeze --out` 輸出；P2 `release-gate.json`、func-test 報告目錄、bridge 自測報告；TestPilot／realhw `report.json`；pytest junit XML＋`SW_RESOURCE_REPORT` JSON。
**Interfaces — Produces:**

```python
KINDS: tuple[str, ...] = ("junit", "functest-report", "bridge-selftest", "regression-report", "realhw-report", "gate-report",
                          "ab-report", "canary-report", "drill-report", "od09-freeze", "manual-procedure")
@dataclass(frozen=True)
class Derived:
    result: str                     # RESULTS 之一
    artifact_id: str | None         # 報告內記錄的受測 artifact（無則由 --release-manifest 提供並交叉核對）
    contract_revision: str | None
    tool_revision: str | None
    case_list_sha256: str | None
    workload_sha256: str | None
    xfail_cases: tuple[str, ...]
    started_at: str
    files: tuple[Path, ...]         # 需要複製的檔案（報告目錄時為整棵樹）
    note: str
def derive(kind: str, report: Path, *, item: ExitItem, cases: Mapping[str, dict], lock: Mapping[str, Any],
           release_manifest: Path | None) -> Derived
def ingest(index_path: Path, *, kind: str, item_id: str, platform: str, report: Path, evidence_root: Path,
           catalog: Mapping[str, ExitItem], cases: Mapping[str, dict], lock: Mapping[str, Any], environment: Path,
           release_manifest: Path | None = None, reuse_of: str | None = None, now: str) -> EvidenceEntry
```

`derive` 推導表（任何解析失敗、必要欄位缺漏或檔案不存在 → `HarnessError("HARNESS_EVIDENCE_INGEST_INVALID")`）：

| kind | 讀取 | result |
|---|---|---|
| `junit` | junit XML＋同目錄 `resource-report.json`（`SW_RESOURCE_REPORT`） | `tests>0`、`failures==0`、`errors==0`、`core_portable_platform_skips==[]` → PASS；有 failure → FAIL；其他 → INSUFFICIENT |
| `functest-report` | func-test `--report-dir` 樹 | 每個 `required_case_set` 中適用 L1.5 案例都有 run，且全部 `comparison.json.verdict=="PASS"`、`bridge-manifest.closed_cleanly` → PASS；任一 FAIL／invariant FAIL → FAIL；缺案例或 `INSUFFICIENT_EVIDENCE` → INSUFFICIENT |
| `bridge-selftest` | P2 bridge 自測報告 | 自測報告 `qualified is True` → PASS；否則 FAIL |
| `regression-report`／`realhw-report` | TestPilot／realhw `report.json` | 以 Task 7 `compare_l2_reports` 的單側完整性規則（缺項、重複、未知 verdict、必要 SKIP → INSUFFICIENT）；B 側邏輯判 FAIL／XFAIL；全部必要案例 PASS（或核准 XFAIL）→ PASS |
| `gate-report` | P2 `release-gate.json` | baseline 模式：`baseline_result`（PASS／FAIL／INSUFFICIENT_EVIDENCE→INSUFFICIENT）；candidate 模式：`release_gate_result`（PASS→PASS、FAIL→FAIL、INSUFFICIENT_EVIDENCE／BLOCKED→INSUFFICIENT）；`engine_result.verdict=="FAIL"` 一律 FAIL；artifact 由 `--release-manifest` 提供，且其 `components.cli.sha256` 必須等於 `run.cli_identity.sha256` |
| `ab-report` | Task 6 `ab-report.json` | `verdict` PASS→PASS、FAIL→FAIL、其他→INFRA_ERROR；artifact＝B 側 `release_manifest_sha256`；`layer` 必須對應 item.layer、`platform` 相符 |
| `canary-report` | `canary report` JSON | `S3-CANARY-*`：PASS→PASS、FAIL→FAIL、其他→INSUFFICIENT；`S3-INCIDENTS-CLOSED`：`tally.open_incidents==[]` 且 `tally.failing_incidents==[]` 且 `result ∉ {POLICY_NOT_FROZEN, POLICY_CHANGED}` → PASS，有 failing → FAIL，否則 INSUFFICIENT |
| `drill-report` | Task 13 `drill-report.json` | PASS→PASS、FAIL→FAIL、PRECONDITION_FAILED／INCOMPLETE→INSUFFICIENT；artifact＝報告記錄的 current bundle manifest sha256 |
| `od09-freeze` | Task 15 freeze 輸出 | 三個延後項皆 `frozen` 且 `check_frozen_before(acceptance, 最早 candidate_longrun entry 的 started_at)` 為空 → PASS；否則 FAIL |
| `manual-procedure` | 程序紀錄 JSON `{procedure_path, procedure_sha256, result, executed_by, executed_at, artifacts:[…]}` | 原樣取 `result`，但 `procedure_sha256` 必須等於現行程序檔 sha256；只允許 `evidence_kind=="manual-procedure"` 的項目 |

`ingest` 流程：`kind` 必須等於 `item.evidence_kind`；`platform` 必須屬於該 item；取 `stage_targets[item.stage][platform]`（無 → `HARNESS_EVIDENCE_INGEST_INVALID "no target"`）；`Derived.artifact_id`、`contract_revision`、`tool_revision`、`case_list_sha256`（有記錄時）任一與 target 不同 → `HARNESS_EVIDENCE_INGEST_INVALID`（列出欄位）；`environment` 為 `ENV_KEYS` JSON，計算 `environment_id`；把 `Derived.files` 複製到 `evidence_root/<item_id>/<platform>/<now 去冒號>/`（保留相對結構，複製後重算 sha256）；組 `EvidenceEntry(producer=f"ingest:{kind}", runtime_digest=target.runtime_digest, …)` 後呼叫 `add_entry`。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）`tests/test_stage3_evidence_ingest.py`（`pytestmark = [core_portable, memory_only]`）：

| 測試名 | 斷言 |
|---|---|
| `test_ab_report_pass_ingested_and_copied` | 合法 `ab-report.json`（B 側 artifact＝linux target）→ entry `result=="PASS"`、`producer=="ingest:ab-report"`、檔案已複製到 `evidence_root` 且 sha 相符；`item_status` 為 `SATISFIED` |
| `test_ab_report_artifact_mismatch_rejected` | B 側 `release_manifest_sha256` ≠ target → `HARNESS_EVIDENCE_INGEST_INVALID` |
| `test_ab_report_tool_revision_mismatch_rejected` | `tool_revision` ≠ target → 同上 |
| `test_kind_must_match_item` | `kind="ab-report"` 用於 `S3-ROLLBACK-LINUX` → 同上 |
| `test_canary_incomplete_is_insufficient` | `result=="INCOMPLETE"` → entry `INSUFFICIENT` |
| `test_incidents_closed_from_canary_report` | `open_incidents==[]`、`failing_incidents==[]` → `S3-INCIDENTS-CLOSED` 的 entry PASS |
| `test_drill_incomplete_is_insufficient` | `verdict=="INCOMPLETE"` → `INSUFFICIENT` |
| `test_gate_report_engine_fail_wins` | `release_gate_result=="PASS"` 但 `engine_result.verdict=="FAIL"` → FAIL |
| `test_gate_report_cli_identity_checked` | `--release-manifest` 的 cli sha 與 `run.cli_identity.sha256` 不同 → `HARNESS_EVIDENCE_INGEST_INVALID` |
| `test_regression_report_missing_case_insufficient` | 少一個必要案例 → `INSUFFICIENT` |
| `test_junit_platform_skip_insufficient` | `core_portable_platform_skips` 非空 → `INSUFFICIENT` |
| `test_manual_procedure_hash_checked` | `procedure_sha256` 與現行程序檔不同 → `HARNESS_EVIDENCE_INGEST_INVALID` |
| `test_no_target_rejected` | 該 (stage, platform) 無 target → `HARNESS_EVIDENCE_INGEST_INVALID` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_evidence_ingest.py` → Expected: FAIL（`ImportError`）。
- [ ] **Step 3：實作**（luna）依推導表；junit 以 `xml.etree.ElementTree` 解析；依 P0 T14 在測試檔頭寫 `pytestmark`。
- [ ] **Step 4：Run** 同上 → Expected: PASS。
- [ ] **Step 5：commit** `feat(stage3): 新增出口證據 ingest（由工具報告推導結果並逐檔 hash）`。

---

## Task 15：OD-09 延後凍結程序與凍結輸出對正式 gate 的讀寫契約（`tools/stage3/od09_freeze.py`）

**Test author model:** codex-gpt-6-luna-max
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-15-od09-freeze.md`
**BLOCKED-OWNER:** BO-P9-12（兩平台修正後 Python 48h 基線證據；擁有者核准數值；時序必須早於任何 Rust 候選長跑，最早者為 Stage 1B 的 `S1B-L3-MIXED-48H-WINDOWS`，因此實務上於 Stage 0 出口完成）。

P2 的 `longrun_acceptance.yaml` 把三個延後項放在 `thresholds.{resources,min_effective_load,comparison_uncertainty}`，key 層級 `status: deferred`，數值只在 `per_platform.{linux,windows}.<field>` 之下（初值 null）；P2 gate 對 deferred 一律 `BLOCKED`，對 `status: frozen` 已有判定規則（P2 Task 15 checks 表的 `resources`／`min_effective_load`／`comparison_uncertainty`／`error_rate_relative`／`throughput_relative`／`stuck_relative` 列），且只讀 `per_platform[platform]`。讀寫契約（路徑、欄位名、型別）以 P2 Task 15「延後項讀寫契約」表為唯一來源。本 Task 只補上**寫入端**：凍結程序（stats → propose → freeze）、時序守門 `check_frozen_before`，以及「凍結輸出能被正式 gate 原樣讀取」的整合測試；**不改 gate 判定、不改名、不另設 `deferred`／`tolerance`／`rerun_rule` 等子樹**。

**Files:**
- Create: `tools/stage3/od09_freeze.py`
- Create: `tests/fakes_od09.py`（`od09_env`）
- Modify: `realhw/acceptance/longrun_acceptance.schema.json`（只新增凍結紀錄欄位：`status: frozen` 時 `decision_id`、`approved_by`、`overrides`、`proposal_sha256`、`baseline_entry_ids` 為必填；`per_platform` 形狀與欄位型別沿用 P2，不放寬；同一 commit 讓 P2 `tests/test_longrun_gate.py` 的 `LongrunFixture`「填好延後項的 acceptance 副本」補上這五欄，P2 既有測試維持綠燈）
- 不修改 `realhw/acceptance/gate.py`：若整合測試因 gate 未依 `per_platform[platform]` 讀值而紅，屬 P2 Task 15 缺陷，回 P2 修正並補 P2 測試；不得在 `od09_freeze` 內加相容層或寫第二份值
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_od09.py: {class: core_portable, resources: [memory_only]}`、`test_longrun_gate_frozen.py: {class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_od09.py`、`tests/test_longrun_gate_frozen.py`

**Interfaces — Consumes:** P2 `realhw/acceptance/longrun_acceptance.yaml`（`revision`、`workload.*`、`thresholds.*`）與 `gate.load_acceptance`；`lr-mixed` 產出 `snapshots.ndjson`（`{"t","sessions","rss_kb","pid"}`，t 為相對秒）、`events.ndjson`（`{"t","source","kind","com","mode",…}`，inventory 08 §4.7）；Task 14 證據索引與目錄（`item_status`）；`docs/rust-refactor/owner-decisions-addenda.md`（Task 1 建立）。
**Interfaces — Produces:**

```python
DEFERRED_KEYS: tuple[str, ...] = ("resources", "min_effective_load", "comparison_uncertainty")
PLATFORMS: tuple[str, ...] = ("linux", "windows")
PER_PLATFORM_FIELDS: Mapping[str, tuple[str, ...]] = {   # 必須等於 P2 Task 15「延後項讀寫契約」與 tests/test_stage3_consumes.py 的 P2_DEFERRED_FIELDS
    "resources": ("rss_peak_kb", "rss_steady_kb", "rss_slope_kb_per_h"),
    "min_effective_load": ("min_completed_per_board_source", "max_idle_gap_s"),
    "comparison_uncertainty": ("error_rate_abs_tolerance", "throughput_ratio_tolerance", "stuck_s_tolerance", "max_reruns"),
}
BASELINE_ITEMS: Mapping[str, str] = {"linux": "S0-L3-BASELINE-LINUX-48H", "windows": "S0-L3-BASELINE-WINDOWS-48H"}
PROPOSAL_RULES: Mapping[str, str]   # 公式的人類可讀描述，原樣寫入 proposal
@dataclass(frozen=True)
class BaselineStats:
    platform: str
    artifact_id: str
    daemon_implementation: str
    duration_s: int
    snapshot_interval_s: int
    max_snapshot_gap_s: int
    rss_peak_kb: int
    rss_steady_median_kb: int
    rss_steady_slope_kb_per_h: float
    hourly_done: Mapping[str, Mapping[str, int]]    # "<com>|<source>" -> {"min": n, "p10": n, "hours": h}
    completed_total: Mapping[str, int]              # "<com>|<source>" -> 全程 completed（P2 counting.definitions.completed）
def workload(acceptance_path: Path) -> Mapping[str, Any]          # 回 acceptance 的 workload 段（時長、間隔、缺口係數唯一來源）
def baseline_stats(case_dir: Path, *, platform: str, release_manifest: Path) -> BaselineStats
def validate_baseline(stats: BaselineStats, *, acceptance_path: Path) -> list[str]
def propose(linux: BaselineStats, windows: BaselineStats) -> dict
def freeze(acceptance_path: Path, proposal: dict, *, decision_id: str, approved_by: str, overrides: Mapping[str, Any],
           index_path: Path, catalog: Mapping[str, ExitItem], evidence_root: Path, cases: Mapping[str, dict],
           addenda_path: Path, now: str) -> dict
def check_frozen_before(acceptance_path: Path, run_started_at: str) -> list[str]
```

`baseline_stats`：`duration_s = last_t - first_t`；`max_snapshot_gap_s` 為相鄰快照最大間隔；`snapshot_interval_s` 為間隔中位數；RSS：`peak = max(rss_kb)`，穩態窗＝最後 24 h 快照（與 P2 gate `resources` 的 steady／slope 窗相同），`median`、`slope`（最小平方法，kb/h）；`hourly_done`：依 `t // 3600` 分桶，每 (`com`, `source`) 只計 `kind=="done"`，只統計完整小時（排除最後不滿一小時的桶），取 `min` 與第 10 百分位（nearest-rank）；`completed_total`：每 (`com`, `source`) 全程 completed 數，定義與 P2 `counting.definitions.completed` 相同（`kind=="done"`，或 `kind=="bg_result"` 且 `status=="done"`）。

`validate_baseline` 違規（時長與係數一律讀 `workload(acceptance_path)`，本模組不重複數值，§15.11.4 唯一來源）：`daemon_implementation != "python"`（基線必須是 Python）；`duration_s < workload.min_continuous_duration_s`；`max_snapshot_gap_s > workload.max_observation_gap_factor × snapshot_interval_s`（不得拼接 segments）；任一 (`com`,`source`) 的 `min == 0`（負載中斷）。

`propose` 固定公式（本計畫先於基線存在而定，**不得在看到基線後修改公式；擁有者只能在 freeze 時以 `overrides` 明示覆寫，覆寫會原樣記錄**）。`I = max(linux.snapshot_interval_s, windows.snapshot_interval_s)`；路徑與欄位名逐字等於 P2 的 `thresholds.<key>.per_platform.<os>.<field>`（`<os>` 取 `PLATFORMS` 兩者，`s` 為該平台的 `BaselineStats`）：

| 路徑（`thresholds.` 之下） | 值 |
|---|---|
| `resources.per_platform.<os>.rss_peak_kb` | `ceil(1.25 × s.rss_peak_kb)` |
| `resources.per_platform.<os>.rss_steady_kb` | `ceil(1.25 × s.rss_steady_median_kb)` |
| `resources.per_platform.<os>.rss_slope_kb_per_h` | `max(ceil(2 × s.rss_steady_slope_kb_per_h), 256)` |
| `min_effective_load.per_platform.<os>.min_completed_per_board_source` | `{"<com>\|<source>": max(1, floor(0.5 × s.completed_total[key]))}`，鍵＝該平台基線出現的全部 (`com`,`source`)（全程 completed 下限，P2 gate 依此鍵逐一判定） |
| `min_effective_load.per_platform.<os>.max_idle_gap_s` | `3600`（同一 `<com>\|<source>` 相鄰 completed 事件間隔上限） |
| `comparison_uncertainty.per_platform.<os>.error_rate_abs_tolerance` | `0.002` |
| `comparison_uncertainty.per_platform.<os>.throughput_ratio_tolerance` | `0.02` |
| `comparison_uncertainty.per_platform.<os>.stuck_s_tolerance` | `I`（一個快照間隔的量化誤差） |
| `comparison_uncertainty.per_platform.<os>.max_reruns` | `1` |

proposal 形狀與 acceptance 子樹同構：`{"thresholds": {<key>: {"per_platform": {"linux": {...}, "windows": {...}}}}, ...}`，`propose` 產出後以 `PER_PLATFORM_FIELDS` 自我檢查（每個 key、每個平台欄位集合恰等於 `PER_PLATFORM_FIELDS[key]`、值非 null）。proposal 另含 `baseline_artifacts`、`proposal_rules`（`PROPOSAL_RULES`）、`generated_at`、`acceptance_revision`（產生時的 `revision`）、`proposal_sha256`（不含自身欄位：`sha256(dumps_compact)`）。

`freeze` 前置（任一不符 → `HarnessError`）：兩平台 `BASELINE_ITEMS` 在 Stage 0 對應平台 target 下 `item_status == "SATISFIED"`，否則 `HARNESS_OD09_BASELINE_INVALID`；證據索引中任何 `candidate_longrun:true` 項目的 entry（不論結果、不論平台）存在 → `HARNESS_OD09_TOO_LATE`（已有 Rust 候選長跑開始，不得事後凍結）；acceptance 檔任一 `thresholds.<DEFERRED_KEYS>.status` 已為 `frozen` → `HARNESS_OD09_TOO_LATE`；`proposal_sha256` 重算不符或 `proposal.acceptance_revision` 不等於現行 `revision` → `HARNESS_OD09_BASELINE_INVALID`；**介面一致性**：acceptance 檔每個 `DEFERRED_KEYS` 必須有 `per_platform`，其平台集合恰為 `PLATFORMS`、每平台欄位集合恰為 `PER_PLATFORM_FIELDS[key]`，proposal 的 `thresholds.<key>.per_platform` 亦同，且 `overrides` 的每個鍵必須是 `"<key>.per_platform.<os>.<field>"` 且指向上述既有欄位——任一不符 → `HARNESS_OD09_BASELINE_INVALID`（`detail` 列出多出／缺少的路徑），檔案不得被改動。通過後對每個 `DEFERRED_KEYS` 改寫 `thresholds.<key>`：`status: frozen`、`frozen_at: now`、保留 `decision: OD-09` 與 `freeze_deadline`、新增 `decision_id`、`approved_by`、`overrides`、`proposal_sha256`、`baseline_entry_ids`，並把 proposal 值（套上 overrides）逐欄寫進**既有的** `thresholds.<key>.per_platform.<os>.<field>`（兩平台一次寫入；不新增其他數值欄位或子樹，不寫任何 `deferred` 子樹）；頂層 `revision` 改為 `f"{now 日期}.{n}"`（同日第 n 次）；其他鍵逐值保留；以 PyYAML `safe_dump(sort_keys=False, allow_unicode=True)` 寫回並以 `gate.load_acceptance` 驗證。在 `addenda_path` 附加一列 `| <decision_id> | OD-09 延後項凍結 | <now 日期> | approved_by=<…>；revision=<新 revision>；proposal_sha256=<…>；overrides=<鍵清單或 無> |`。回傳 `{"thresholds":{三鍵寫入後內容},"revision","acceptance_sha256","requires_baseline_gate_rerun":true}`；CLI `od09 freeze --out` 把它寫成 ingest 用的報告。凍結改變 `revision`，但 P2 `baseline_compatible` **不**要求候選與基線 gate 的 `acceptance.revision` 相同（只比對 `platform`、`run.engine_files_sha256`、`workload_contract_sha256` 與基線 PASS；凍結只改 `thresholds`，不影響 `workload`＋`counting` 的 sha）。凍結後仍以新 revision 對兩平台已保存的基線證據重跑 `python3 -m realhw.acceptance.gate --mode baseline`（離線、不重跑長跑），作為凍結值可被正式 gate 讀取的自檢並供候選引用（`requires_baseline_gate_rerun:true` 保留），程序寫在 `od09-deferred-freeze.md`。

`check_frozen_before(path, ts)`：讀與正式 gate 相同的路徑，回傳違規清單——任一 `DEFERRED_KEYS` 的 `thresholds.<key>.status != "frozen"` → `"<key> not frozen"`；`frozen_at >= ts` → `"<key> frozen after run start"`；`status == "frozen"` 但任一 `thresholds.<key>.per_platform.<os>.<field>`（`PLATFORMS` × `PER_PLATFORM_FIELDS[key]`）缺少或為 null → `"<key>.per_platform.<os>.<field> missing"`（不得只看 `status` 就宣告已凍結）；`gate.load_acceptance(path)` 拋出 schema 錯誤 → `"acceptance schema invalid: <訊息>"`。

**凍結後 gate 判定**：沿用 P2 Task 15 gate 既有規則，本 Task 不另定義、不修改 `gate.py`。對照（皆讀 `thresholds.<key>.per_platform[platform]`）：`resources` → `rss_peak_kb`／`rss_steady_kb`／`rss_slope_kb_per_h`（峰值、最後 24 h 中位數、最後 24 h 斜率，任一超過 → FAIL）；`min_effective_load` → `min_completed_per_board_source` 的每個鍵與 `max_idle_gap_s`（不足 → INSUFFICIENT_EVIDENCE）；`comparison_uncertainty` → `error_rate_abs_tolerance` 用於 `error_rate_relative`、`throughput_ratio_tolerance` 用於 `throughput_relative`、`stuck_s_tolerance` 用於 `stuck_relative`、`max_reruns` 為重跑上限。baseline 模式三項回 `NOT_APPLICABLE_BASELINE`，且不受 `pre_frozen` 檢查影響（延後項本來就在基線之後凍結，§11.6）。

**Steps:**

- [ ] **Step 1：寫失敗測試**（luna）`tests/test_stage3_od09.py`（`pytestmark = [core_portable, memory_only]`），關鍵測試：

```python
def test_freeze_refused_after_candidate_longrun(tmp_path):
    env = fakes_od09.od09_env(tmp_path)   # 複製 repo 實際 P2 acceptance＋schema（三延後項 deferred）、兩平台基線 case_dir、Stage 0 兩平台 target 與 SATISFIED 基線 entries
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(env.index, fakes_evidence.entry(tmp_path, "S1B-L3-MIXED-48H-WINDOWS", "INFRA_ERROR", platform="windows"),
                             catalog=cat)
    prop = od09_freeze.propose(env.linux_stats, env.windows_stats)
    with pytest.raises(HarnessError) as ei:
        od09_freeze.freeze(env.acceptance, prop, decision_id="OD-09-F1", approved_by="owner", overrides={}, index_path=env.index,
                           catalog=cat, evidence_root=env.evidence_root, cases={}, addenda_path=env.addenda, now=fakes.ts(20, 9))
    assert ei.value.code == "HARNESS_OD09_TOO_LATE"


def test_proposal_formula_fixed(tmp_path):
    env = fakes_od09.od09_env(tmp_path, linux_rss_peak=40000, linux_slope=10.0, interval=300)
    prop = od09_freeze.propose(env.linux_stats, env.windows_stats)
    lin = prop["thresholds"]["resources"]["per_platform"]["linux"]
    assert lin["rss_peak_kb"] == 50000
    assert lin["rss_slope_kb_per_h"] == 256
    cu = prop["thresholds"]["comparison_uncertainty"]["per_platform"]["linux"]
    assert cu == {"error_rate_abs_tolerance": 0.002, "throughput_ratio_tolerance": 0.02, "stuck_s_tolerance": 300, "max_reruns": 1}
    load = prop["thresholds"]["min_effective_load"]["per_platform"]["windows"]
    assert load["max_idle_gap_s"] == 3600
    key = "COM0|agent:rhw0"
    assert load["min_completed_per_board_source"][key] == max(1, env.windows_stats.completed_total[key] // 2)
    for k in od09_freeze.DEFERRED_KEYS:
        assert "tolerance" not in prop["thresholds"][k] and "rerun_rule" not in prop["thresholds"][k]


def test_real_p2_acceptance_roundtrip_propose_freeze_check_gate(tmp_path):
    # 介面一致性：以 repo 實際 P2 門檻檔（非自造 fixture）走 propose → freeze → check_frozen_before → gate.evaluate
    real = ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml"
    env = fakes_od09.od09_env(tmp_path)
    assert env.acceptance.read_bytes() == real.read_bytes()            # 替身不得改寫或自造 acceptance
    before = yaml.safe_load(real.read_text(encoding="utf-8"))
    assert sorted(od09_freeze.check_frozen_before(env.acceptance, fakes.ts(21, 0))) == sorted(
        f"{k} not frozen" for k in od09_freeze.DEFERRED_KEYS)
    cat = evidence_index.load_exit_catalog()
    prop = od09_freeze.propose(env.linux_stats, env.windows_stats)
    od09_freeze.freeze(env.acceptance, prop, decision_id="OD-09-F1", approved_by="owner", overrides={}, index_path=env.index,
                       catalog=cat, evidence_root=env.evidence_root, cases={}, addenda_path=env.addenda, now=fakes.ts(20, 9))
    after = yaml.safe_load(env.acceptance.read_text(encoding="utf-8"))
    assert "deferred" not in after and "deferred" not in after["thresholds"]
    for k in od09_freeze.DEFERRED_KEYS:
        t = after["thresholds"][k]
        assert t["status"] == "frozen"
        assert set(t["per_platform"]) == set(before["thresholds"][k]["per_platform"]) == {"linux", "windows"}
        for os_name in ("linux", "windows"):
            assert set(t["per_platform"][os_name]) == set(before["thresholds"][k]["per_platform"][os_name])   # 欄位名沿用 P2，無改名
            assert all(v is not None for v in t["per_platform"][os_name].values())
        assert set(t) - {"per_platform"} <= {"status", "decision", "freeze_deadline", "frozen_at", "decision_id", "approved_by",
                                            "overrides", "proposal_sha256", "baseline_entry_ids"}
    gate.load_acceptance(env.acceptance)                                 # 正式 schema 驗證通過
    assert od09_freeze.check_frozen_before(env.acceptance, fakes.ts(21, 0)) == []
    # 以凍結後新 revision 重跑基線 gate，再以正式 gate 評估健康候選：延後項不得仍為 BLOCKED，且數值確實被讀到
    run = fakes_od09.longrun_case(tmp_path / "cand", platform="linux", started_at=fakes.ts(21, 0),
                                  rss_peak_kb=after["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"] - 1)
    out = gate.evaluate(run.inputs(mode="candidate", acceptance_path=env.acceptance,
                                   baseline=fakes_od09.rerun_baseline_gate(env, platform="linux")))
    res = {c["id"]: c["result"] for c in out["checks"]}
    assert res["resources"] == res["min_effective_load"] == res["comparison_uncertainty"] == "PASS"
    assert out["release_gate_result"] == "PASS"
    over = fakes_od09.longrun_case(tmp_path / "over", platform="linux", started_at=fakes.ts(21, 0),
                                   rss_peak_kb=after["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"] + 1)
    out2 = gate.evaluate(over.inputs(mode="candidate", acceptance_path=env.acceptance,
                                     baseline=fakes_od09.rerun_baseline_gate(env, platform="linux")))
    assert {c["id"]: c["result"] for c in out2["checks"]}["resources"] == "FAIL"   # 證明 gate 讀的正是 freeze 寫入的欄位
```

（`fakes_od09.od09_env(root, *, linux_rss_peak=40000, linux_slope=10.0, interval=300, duration_s=None, rust_baseline=False)`：acceptance 與 schema 一律自 repo 實際 `realhw/acceptance/longrun_acceptance.yaml`、`longrun_acceptance.schema.json` **原樣複製**（測試需要變形時只能在複本上以 dotted-path 修改單一鍵，不得自建整份檔）；`duration_s=None` 時取 acceptance 的 `workload.min_continuous_duration_s`；回傳 `SimpleNamespace(acceptance, index, evidence_root, addenda, contract, linux_stats, windows_stats, linux_case_dir, windows_case_dir)`。`fakes_od09.longrun_case(dir, *, platform, started_at, rss_peak_kb, completed_scale=1.0)` 以 P2 Task 15 `LongrunFixture` 建健康候選 report／case_dir（completed 預設等於基線 `completed_total`），回傳物件的 `inputs(*, mode, acceptance_path, baseline)` 產生 `gate.GateInputs`；測試檔頭 `ROOT = Path(__file__).resolve().parents[1]`，並 `import yaml`、`from realhw.acceptance import gate`；`fakes_od09.rerun_baseline_gate(env, *, platform)` 以凍結後的 acceptance 對 `env.<platform>_case_dir` 執行 `gate.evaluate(mode="baseline")` 並寫出 `release-gate.json`，回傳其路徑。）

| 測試名 | 斷言 |
|---|---|
| `test_od09_env_baselines_satisfied` | `evidence_index.item_status` 對兩個基線項為 `SATISFIED`（替身自我檢查，本 Task 內轉綠） |
| `test_baseline_stats_from_lr_mixed` | 合成 48h、間隔 300 s 的 snapshots／events → `duration_s == workload.min_continuous_duration_s`、`hourly_done["COM0|agent:rhw0"]["hours"] == 48` |
| `test_spliced_baseline_rejected` | 中間缺 1 小時快照 → `validate_baseline` 含 `"snapshot gap"` 字樣 |
| `test_short_baseline_rejected` | 比 `min_continuous_duration_s` 少 1 小時 → 違規 `"duration"` |
| `test_thresholds_read_from_workload` | 把替身 acceptance 的 `min_continuous_duration_s` 改為 3600 → 1.5h 基線不再因時長違規（證明沒有硬寫 172800） |
| `test_rust_baseline_rejected` | `daemon_implementation == "rust"` → 違規 |
| `test_load_stop_rejected` | 某來源某完整小時 done=0 → 違規 |
| `test_per_platform_fields_match_p2` | `dict(od09_freeze.PER_PLATFORM_FIELDS)` 的每個 key 的欄位集合 == repo 實際 P2 `longrun_acceptance.yaml` 中 `thresholds.<key>.per_platform.linux` 與 `.windows` 的鍵集合，且 == `tests/test_stage3_consumes.py` 的 `P2_DEFERRED_FIELDS`（P2 改名時本測試先紅） |
| `test_freeze_writes_only_deferred_and_bumps_revision` | freeze 後 `thresholds` 中三個已凍結項與其他頂層鍵逐值不變；三延後項 `status == "frozen"`、`decision_id == "OD-09-F1"`、`thresholds.resources.per_platform.linux.rss_peak_kb == prop["thresholds"]["resources"]["per_platform"]["linux"]["rss_peak_kb"]`（windows 同）；`"rss_peak_kb" not in thresholds.resources`、`"tolerance" not in thresholds.comparison_uncertainty`、頂層與 `thresholds` 無 `deferred` 鍵；`revision` 改變；`gate.load_acceptance` 驗證通過 |
| `test_freeze_records_overrides` | `overrides={"resources.per_platform.linux.rss_peak_kb": 60000}` → `thresholds.resources.per_platform.linux.rss_peak_kb == 60000`、windows 值不變、`overrides` 原樣記錄、addenda 該列含 `resources.per_platform.linux.rss_peak_kb` |
| `test_freeze_rejects_unknown_override_path` | `overrides={"resources.rss_budget.linux": 1}` 或 `{"comparison_uncertainty.per_platform.linux.tolerance": 1}` → `HARNESS_OD09_BASELINE_INVALID`、acceptance 檔 bytes 不變、addenda 無新列 |
| `test_freeze_rejects_field_set_mismatch` | 在 acceptance 複本的 `thresholds.resources.per_platform.windows` 刪除 `rss_slope_kb_per_h`（模擬 P2 改名）→ `HARNESS_OD09_BASELINE_INVALID`、`detail` 含 `resources.per_platform.windows.rss_slope_kb_per_h`；proposal 刪除一欄並重算 sha → 同上；兩者 acceptance 檔 bytes 不變 |
| `test_freeze_requires_satisfied_baselines` | windows 基線 entry 缺 → `HARNESS_OD09_BASELINE_INVALID` |
| `test_refreeze_refused` | 已 frozen 再 freeze → `HARNESS_OD09_TOO_LATE` |
| `test_tampered_proposal_rejected` | 修改 proposal 數值而不更新 sha → `HARNESS_OD09_BASELINE_INVALID`；proposal 的 `acceptance_revision` 與現行不同 → 同上 |
| `test_check_frozen_before` | 未凍結 → 三條 `not frozen`；凍結於 t，run 於 t+1s → `[]`；run 於 t-1s → 三條 `frozen after run start` |
| `test_check_frozen_before_rejects_null_values` | 先以 `freeze` 正常凍結（`frozen_at` 早於 run），再在複本上把三項的 `per_platform.windows` 各欄改回 null → 結果含 `"comparison_uncertainty.per_platform.windows.max_reruns missing"` 等每個 null 欄位、以及 `"acceptance schema invalid: …"`，不得回 `[]`（不得只看 `status` 宣告已凍結） |
| `test_addenda_append_only` | 兩次（不同 acceptance 檔）freeze → addenda 檔前段原文不變、新增兩列 |

`tests/test_longrun_gate_frozen.py`（`pytestmark = [core_portable, memory_only]`；**acceptance 一律為 repo 實際 P2 檔的複本經本 Task `freeze` 寫出的結果**，不得手寫凍結後的 acceptance；report／case_dir 以 `fakes_od09.longrun_case`（P2 Task 15 gate 測試替身）建立；本檔驗證「freeze 寫出的欄位＝未修改的正式 gate 讀取的欄位」）：

| 測試名 | 斷言 |
|---|---|
| `test_deferred_still_blocked` | 未凍結（實際 P2 檔原樣）→ 三項 check 為 `BLOCKED`、`release_gate_result == "BLOCKED"`（P2 原行為不變） |
| `test_frozen_resources_fail_over_budget` | freeze 後 linux candidate RSS 峰值 = `thresholds.resources.per_platform.linux.rss_peak_kb + 1` → `resources` FAIL、`release_gate_result == "FAIL"`；windows candidate 以 `per_platform.windows` 判定（以 override 讓兩平台值不同，同一峰值 linux PASS、windows FAIL） |
| `test_frozen_min_load_insufficient` | freeze 後 candidate 的 `COM0\|agent:rhw0` 全程 completed = `per_platform.linux.min_completed_per_board_source["COM0\|agent:rhw0"] - 1` → `min_effective_load` `INSUFFICIENT_EVIDENCE`、`detail` 含該鍵；某鍵相鄰 completed 間隔 3601 s → 同上 |
| `test_frozen_tolerance_band_insufficient` | freeze 後錯誤率 = baseline + 0.001（落在 `error_rate_abs_tolerance` 0.002 內）→ `error_rate_relative` `INSUFFICIENT_EVIDENCE`；baseline + 0.003 → FAIL；throughput 為基線 94%（落在 `[0.95 − throughput_ratio_tolerance, 0.95)`）→ `throughput_relative` `INSUFFICIENT_EVIDENCE` |
| `test_baseline_mode_not_applicable` | baseline 模式、三項已凍結 → 三項 `NOT_APPLICABLE_BASELINE`、`baseline_result` 不受影響 |
| `test_all_frozen_candidate_pass` | freeze 後、候選 `started_at` 晚於 `frozen_at`、基線 gate 以新 revision 重跑、全部符合 → 三延後項 check 皆 `PASS`（非 `BLOCKED`）、`release_gate_result == "PASS"` |

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_od09.py tests/test_longrun_gate_frozen.py` → Expected: FAIL（`ImportError: tools.stage3.od09_freeze`；`test_real_p2_acceptance_roundtrip_propose_freeze_check_gate` 與 `test_longrun_gate_frozen.py` 因 freeze 未實作而紅）。
- [ ] **Step 3：實作**（haiku）依規則與公式表；百分位與時間分桶只用 stdlib；YAML 以 PyYAML；**不修改 `gate.py`**（只改 schema 的凍結紀錄必填欄位）；寫入路徑只用 `thresholds.<key>.per_platform.<os>.<field>`；並重跑 P2 的 `tests/test_longrun_gate.py`、`tests/test_longrun_acceptance_file.py`；登錄兩個測試檔。若整合測試紅在 gate 讀值（而非 freeze 寫值），停止並回報 P2 缺陷，不在本模組繞過。
- [ ] **Step 4：Run** 同上＋P2 兩個 longrun 測試檔＋`tests/test_stage3_consumes.py` → Expected: PASS。【BLOCKED-OWNER BO-P9-12】實際 `od09 stats → propose → freeze → 重跑基線 gate → check` 於兩平台基線完成後由擁有者執行（程序見 Task 17 的 `od09-deferred-freeze.md`）。
- [ ] **Step 5：commit** `feat(stage3): 新增 OD-09 延後項凍結程序、時序守門與凍結輸出對正式 gate 的整合驗證`（files 含 `tests/fakes_od09.py`、`realhw/acceptance/longrun_acceptance.schema.json`；不含 `gate.py`）。

---

## Task 16：Python daemon 退役切換（packaging）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p9-16-python-daemon-retirement.md`
**BLOCKED-OWNER:** BO-P9-13（翻轉 `retired:true` 的裁決）、BO-P9-17（Windows 原生 build 的 Stage 3 bundle 與自架 runner 上的發佈 job）。

退役範圍：產品 release artifact 不再包含或啟動 Python daemon（無 `py/` runtime、無 `daemon.entry`／`runtime`／`package`、無 `python_cli_delegate`、CLI 不再 fallback 到 `-m sw_core.daemon`、release workflow 不再發佈 wheel 與 PyInstaller exe）。**不刪除** repo 中的 `sw_core/`（A/B 基線重現仍需要；刪除需另一次擁有者裁決），不影響 `realhw/`、`regression/`、`reliability/`、`func-test/`、`tools/uart_stdio_bridge/`、`tests/contract_blackbox/`、`tests/acceptance/`、`tools/stage3/` 與既有 pytest。

**受測物即發佈物**：Stage 3 bundle 在核准前就以最終形狀建置（manifest 只帶靜態標記 `retirement: {"python_daemon": "removed"}`，沒有任何核准或 hash 欄位），以它設定 Stage 3 target、跑完全部 Stage 3 證據；核准只寫進 `packaging/python_daemon_retirement.json`；發佈時以 `retirement verify --mode release --bundle-manifest` 要求所發佈 bundle 的 manifest sha256 等於該平台已記錄的 target，並直接上傳受測 bundle 本身，**不重新建置**（重建會改變 `created_at` 等位元組，使證據失效）。

**Files:**
- Create: `tools/stage3/retirement.py`
- Create: `tests/fakes_retirement.py`（`retirement_repo(root, *, stage3_result)`：建含證據索引、兩平台 Stage 3 target、`evidence_root` 與 keep 清單目錄的假 repo；`stage3_result ∈ {"PASS","BLOCKED","FAIL"}`）
- Modify: `scripts/rust/mkbundle.sh`（新增 `--stage <s>` 與 `--plan-only`；呼叫 `python3 -m tools.stage3 retirement bundle-plan`）
- Modify: `contracts/release_manifest.schema.json`（新增選填 `retirement`：`{"python_daemon": const "removed"}`；`stage=="3"` 時必填，且 `components.daemon.implementation=="rust"`、無 `python_cli_delegate`、daemon 無 `entry`／`runtime`／`package`；有 `retirement` 時 `stage` 必須為 `"3"`）
- Modify: `crates/sw-proto/src/release_manifest.rs`（`python_cli_delegate_entry` 改為 `Option<Vec<String>>`：只有帶退役標記的 manifest 可以缺；新增 `ManifestError::RetirementInconsistent(&'static str)`）、P3 委派路徑（`crates/sw-cli/src/delegate.rs`，`None` 時沿用既有 `INTERNAL_ERROR` 路徑，不新增錯誤碼；Stage 3 CLI 已全原生，正常不會走到）、`crates/sw-cli/src/commands/daemon_cmd.rs`（P4 `locate_daemon_argv`）、`crates/sw-daemon/src/launcher.rs`
- Modify: `.github/workflows/release.yml`
- Modify: `docs/rust-refactor/plans/rust-architecture.md` §10.1（加入 `retirement` 欄位、Stage 2 起 Rust daemon 的形狀 `components.daemon{implementation:"rust",version,git_revision,launcher:{path,sha256}}`、`mkbundle.sh` 實際旗標 `--dev --out --target --profile --delegate-sentinel --print-dir --version-override --stage --plan-only`）
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_retirement.py: {class: core_portable, resources: [memory_only]}`、`test_stage3_retirement_posix.py: {class: posix_only, resources: [memory_only]}`）
- Test: `tests/test_stage3_retirement.py`、`tests/test_stage3_retirement_posix.py`（呼叫 `bash scripts/rust/mkbundle.sh`）、`crates/sw-proto/tests/release_manifest_retirement.rs`、`crates/sw-cli` 內 `daemon_cmd.rs` 的 `#[cfg(test)] mod retired_tests`（`locate_daemon_argv` 為 crate 內函式）、`crates/sw-daemon/tests/launcher_retired.rs`

**Interfaces — Consumes:** Task 14 `stage_status`／`load_index`；Task 3 `runtime_digest`；P3／P4 `ReleaseManifest::parse`、`parse_manifest`；P4 `locate_daemon_argv(ctx)` 與 `launcher.rs`。
**Interfaces — Produces（Python）：**

```python
RETIREMENT_PATH: Path      # <repo>/packaging/python_daemon_retirement.json
MANIFEST_MARKER: Mapping[str, str] = {"python_daemon": "removed"}
KEEP_PYTHON_TEST_TOOLS: tuple[str, ...] = ("realhw", "regression", "reliability", "func-test", "tools/uart_stdio_bridge",
                                           "tests/contract_blackbox", "tests/acceptance", "tools/stage3", "sw_core")
def load_retirement(path: Path = RETIREMENT_PATH) -> dict
def verify(retirement: dict, *, repo_root: Path, mode: str, evidence_root: Path | None = None,
           bundle_manifests: Mapping[str, Path] | None = None) -> list[str]     # mode: "repo" | "release"
def bundle_plan(stage: str, retirement: dict, *, dev: bool) -> dict
    # 鍵：stage、retirement_applies、publishable、candidate、manifest_retirement；retirement_applies 為 true 時另有
    # include_python_runtime、include_python_cli_delegate、daemon_implementation
def github_output(retirement: dict) -> str   # "retired=true\n" 或 "retired=false\n"
```

`verify` 規則（違規字串前綴固定為 `"retirement:"`）：
- `retired is False` → 其餘欄位必須為 null（`evidence_index` 除外；`stage3_target_artifact_ids` 兩值為 null），回 `[]`。
- `retired is True`，`mode=="repo"`：`evidence_index` 存在且 sha256 等於 `evidence_index_sha256`；`approved_by`／`approved_at`／`decision_ref`／`effective_bundle_version` 非 null；`stage3_target_artifact_ids.{linux,windows}` 皆非 null 且分別等於索引 `stage_targets["3"][platform].artifact_id`；`KEEP_PYTHON_TEST_TOOLS` 全部存在。
- `retired is True`，`mode=="release"`：上述全部，加上 `evidence_root` 必填（缺 → `"retirement: evidence root required"`）且 `stage_status(index,"3", evidence_root=…, cases=…)["result"] == "PASS"`（否則 `"retirement: stage 3 not PASS: <result>"`）；`bundle_manifests` 必須恰含 `linux`、`windows`，每個檔案的 sha256 等於 `stage3_target_artifact_ids[platform]`（`"retirement: bundle manifest mismatch: <platform>"`），且 manifest 帶 `MANIFEST_MARKER`、`bundle_version == effective_bundle_version`。

`bundle_plan` 規則（P4 對 Stage 1A～2.4 bundle 內容的既有決定不由本 Task 改變；`publishable`、`candidate` 只出現在 plan 與 mkbundle stdout，不寫進 manifest，因此不影響受測 bundle 的位元組）：

| stage | retired | dev | 結果 |
|---|---|---|---|
| `1A`～`2.4` | false | 任意 | `{"stage":s,"retirement_applies":false,"publishable":not dev,"candidate":false,"manifest_retirement":null}`；mkbundle 依 P4 既有行為組裝 |
| `3` | false | 任意 | `{"stage":"3","retirement_applies":true,"publishable":false,"candidate":true,"include_python_runtime":false,"include_python_cli_delegate":false,"daemon_implementation":"rust","manifest_retirement":{"python_daemon":"removed"}}`（核准前即可建置受測候選；不可發佈） |
| `3` | true | false | 同上但 `"publishable":true,"candidate":false` |
| `3` | true | true | 同上但 `"publishable":false,"candidate":false` |
| `1A`～`2.4` | true | false | `HarnessError("HARNESS_RETIREMENT_INCONSISTENT")`（退役後不得再發佈非 Stage 3 的正式 bundle） |
| `1A`～`2.4` | true | true | `{"stage":s,"retirement_applies":false,"publishable":false,"candidate":false,"manifest_retirement":null}`（僅供測試） |

`HARNESS_RETIREMENT_NOT_APPROVED` 只用在發佈路徑：`retirement verify --mode release` 在 `retired is False` 時回此碼（exit 2），release workflow 的 Stage 3 發佈 job 因此不會在未核准時執行。

**Interfaces — Produces（Rust）：**

```rust
// crates/sw-proto/src/release_manifest.rs（在 P3／P4 既有 ReleaseManifest 上修改）
pub struct ReleaseManifest { /* … */ pub python_cli_delegate_entry: Option<Vec<String>>, /* … */ }
pub struct Retirement { pub python_daemon: String }          // 只接受 "removed"
pub enum ManifestError { InvalidJson(String), Schema(String), RetirementInconsistent(&'static str) }
impl ReleaseManifest {
    pub fn retirement(&self) -> Option<&Retirement>;
    pub fn python_daemon_retired(&self) -> bool;             // retirement.python_daemon == "removed"
}
pub fn validate_retirement(m: &ReleaseManifest) -> Result<(), ManifestError>;
// 規則：retired ⇒ stage == "3" && components.daemon.implementation == "rust" && components.python_cli_delegate 不存在
//       && daemon 無 entry/runtime/package；stage == "3" ⇒ retired；python_cli_delegate_entry 為 None ⇒ retired（否則 Schema，維持 P3 行為）
// ReleaseManifest::parse 與 parse_manifest 皆在成功解析後呼叫 validate_retirement

// crates/sw-cli/src/commands/daemon_cmd.rs（P4 DF-05）
pub fn locate_daemon_argv(ctx: &Ctx) -> Result<Vec<String>, DaemonLocateError>;
// 退役 manifest：第 3 步（components.daemon.runtime.path ＋ -I -m sw_core.daemon）一律跳過——即使被竄改而帶 runtime；
// 第 1、2 步都不可用時沿用既有 DAEMON_BINARY_NOT_FOUND（不新增錯誤碼）

// crates/sw-daemon/src/launcher.rs
pub enum LaunchPlan { RustNative, PythonChild { argv: Vec<String> } }
pub enum LaunchError { PythonDaemonRetired, ManifestInvalid(String) }
pub fn resolve_launch(manifest: &ReleaseManifest) -> Result<LaunchPlan, LaunchError>;
// python_daemon_retired() 為 true 而 implementation == "python"（被竄改的 manifest）→ PythonDaemonRetired；
// main 將其輸出 stderr "serialwrapd: INTERNAL_ERROR: python daemon retired by release manifest" 並 exit 2（沿用既有 INTERNAL_ERROR，不新增碼）
```

`sw-cli`／`sw-daemon` 中不得新增任何平台 cfg（arch §2）。

**`mkbundle.sh` 變更**：新增 `--stage <s>`（未給時維持 P3／P4 既有 stage 值）與 `--plan-only`；解析參數後執行 `plan_json=$(python3 -m tools.stage3 retirement bundle-plan --stage "$STAGE" ${DEV:+--dev})`，非 0 即 `exit 2`；`retirement_applies` 為 true 時以 `python3 -c` 讀取欄位：不組 `py/`、不寫 `daemon.entry`／`runtime`／`package` 與 `python_cli_delegate`、daemon 只寫 `launcher`、在 manifest 寫入 `"retirement": {"python_daemon": "removed"}`；為 false 時維持既有流程；`--dev` 時輸出目錄名加 `-dev` 後綴（Stage 3 候選不加，因為它就是要受測並在核准後原樣發佈的 bundle）；stdout 最後一行印 `{"bundle_dir":…,"publishable":…,"candidate":…}`；`--plan-only` 時印出 `plan_json` 後 `exit 0`，不 build。

**`release.yml` 變更**：
- 新增 job `retirement-gate`（ubuntu，`needs: project-policy`）：checkout 後 `python3 -m tools.stage3 retirement github-output >> "$GITHUB_OUTPUT"`（`outputs.retired`）；`retired=true` 時再跑 `python3 -m tools.stage3 retirement verify --mode repo`。
- 既有 `publish` job：測試 steps（`pytest`）與「Create GitHub Release」不加條件；「Build wheel」與「Upload wheel to release」**整條鏈**加 `if: needs.retirement-gate.outputs.retired != 'true'`（job `needs` 加入 `retirement-gate`）。
- 既有 `publish-windows-exe` **整個 job** 加 `if: needs.retirement-gate.outputs.retired != 'true'`（避免只跳過 PyInstaller 建置、卻仍執行「Smoke test exes」與「Upload exes」去找不存在的 `dist/*.exe`）。
- 新增 `publish-stage3-bundle-linux`（`runs-on: [self-hosted, linux, serialwrap-release]`）與 `publish-stage3-bundle-windows`（`runs-on: [self-hosted, windows, serialwrap-release]`），皆 `needs: [retirement-gate, publish]`、`if: needs.retirement-gate.outputs.retired == 'true'`：從 runner 上的 `SW_STAGE3_BUNDLE_DIR`（擁有者保存的受測 bundle）與 `SW_EVIDENCE_ROOT` 執行 `python3 -m tools.stage3 retirement verify --mode release --evidence-root "$SW_EVIDENCE_ROOT" --bundle-manifest linux=… --bundle-manifest windows=…`，通過後把該目錄打包（`tar`／`Compress-Archive`）、附 `SHA256SUMS`，`gh release upload`；這兩個 job **不得**呼叫 `mkbundle.sh` 或 `cargo build`（不重建受測物）。

**Steps:**

- [ ] **Step 1：寫失敗測試**（haiku）

`tests/test_stage3_retirement.py`（`pytestmark = [core_portable, memory_only]`）關鍵測試：

```python
def test_stage3_candidate_buildable_before_approval():
    plan = retirement.bundle_plan("3", retirement.load_retirement(), dev=False)
    assert plan["candidate"] is True and plan["publishable"] is False
    assert plan["manifest_retirement"] == {"python_daemon": "removed"}
    assert plan["include_python_runtime"] is False and plan["daemon_implementation"] == "rust"


def test_retired_true_requires_stage3_evidence(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="BLOCKED")
    idx = repo / "docs/rust-refactor/evidence/index.json"
    targets = json.loads(idx.read_text(encoding="utf-8"))["stage_targets"]["3"]
    r = dict(retirement.load_retirement(repo / "packaging" / "python_daemon_retirement.json"), retired=True,
             approved_by="owner", approved_at=fakes.ts(30, 1), decision_ref="OD-14", effective_bundle_version="1.0.0",
             stage3_target_artifact_ids={p: targets[p]["artifact_id"] for p in ("linux", "windows")},
             evidence_index_sha256=fakes.sha256_file(idx))
    v = retirement.verify(r, repo_root=repo, mode="release", evidence_root=repo / "evidence",
                          bundle_manifests={p: repo / "bundles" / p / "release-manifest.json" for p in ("linux", "windows")})
    assert any(x.startswith("retirement: stage 3 not PASS") for x in v)


def test_release_requires_tested_bundle_bytes(tmp_path):
    repo = fakes_retirement.retirement_repo(tmp_path, stage3_result="PASS")
    r = fakes_retirement.approved(repo)
    rebuilt = repo / "bundles" / "linux" / "release-manifest.json"
    doc = json.loads(rebuilt.read_text(encoding="utf-8"))
    doc["created_at"] = "2027-01-01T00:00:00+00:00"            # 模擬「核准後重建」
    rebuilt.write_text(json.dumps(doc), encoding="utf-8")
    v = retirement.verify(r, repo_root=repo, mode="release", evidence_root=repo / "evidence",
                          bundle_manifests={p: repo / "bundles" / p / "release-manifest.json" for p in ("linux", "windows")})
    assert "retirement: bundle manifest mismatch: linux" in v


def test_release_workflow_gates_python_artifact_chains():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    assert "retirement-gate" in jobs
    off = "needs.retirement-gate.outputs.retired != 'true'"
    on = "needs.retirement-gate.outputs.retired == 'true'"
    assert off in jobs["publish-windows-exe"].get("if", "")
    for step in jobs["publish"]["steps"]:
        run = step.get("run", "")
        if re.search(r"build --wheel|\.whl", run):
            assert off in step.get("if", ""), step.get("name")
        if re.search(r"pytest|policy_check|gh release create", run):
            assert "retirement-gate" not in step.get("if", ""), step.get("name")
    for name in ("publish-stage3-bundle-linux", "publish-stage3-bundle-windows"):
        job = jobs[name]
        assert on in job["if"]
        runs = " ".join(st.get("run", "") for st in job["steps"])
        assert "retirement verify --mode release" in runs and "--bundle-manifest" in runs
        assert "mkbundle.sh" not in runs and "cargo build" not in runs
```

（`fakes_retirement.approved(repo)` 回傳依索引 target 填好核准欄位的 retirement dict。）

| 測試名 | 斷言 |
|---|---|
| `test_repo_default_verify_clean` | 真實 repo `verify(load_retirement(), repo_root=ROOT, mode="repo") == []` |
| `test_bundle_plan_table[...]` | 依上方 `bundle_plan` 規則表逐列參數化 |
| `test_release_mode_requires_evidence_root` | `evidence_root=None` → 含 `"retirement: evidence root required"` |
| `test_release_mode_requires_both_platforms` | `bundle_manifests` 只給 linux → 違規 |
| `test_target_ids_must_match_index` | `stage3_target_artifact_ids.windows` 與索引不同 → 違規 |
| `test_not_retired_release_mode_not_approved` | `retired=False` 時 CLI `retirement verify --mode release` → exit 2、`HARNESS_RETIREMENT_NOT_APPROVED` |
| `test_keep_list_missing_detected` | retirement_repo 刪 `realhw/` → 違規含 `"retirement: keep path missing: realhw"` |
| `test_index_sha_mismatch` | `evidence_index_sha256` 錯 → 違規含 `"retirement: evidence index sha256 mismatch"` |
| `test_github_output` | `github_output({"retired": False}) == "retired=false\n"` |
| `test_schema_stage3_requires_marker` | 以 `contracts/release_manifest.schema.json` 驗證 `make_bundle(stage="3", retirement_marker=False)` → `ValidationError`；`retirement_marker=True` → 通過；`stage="2.4", retirement_marker=True` → `ValidationError` |
| `test_schema_marker_forbids_python_delegate` | 帶標記的 manifest 加 `python_cli_delegate` → `ValidationError` |
| `test_manifest_has_no_approval_fields` | schema 的 `retirement` 只允許 `python_daemon` 一鍵（`additionalProperties:false`） |
| `test_pytest_suite_does_not_depend_on_wheel` | `tests/test_stage3_*.py` 與 `realhw`／`regression` 的 import 不含 `importlib.metadata.version("serialwrap")` 以外的 wheel 依賴（AST 掃描無 `pkg_resources`、無 `importlib.metadata.distribution("serialwrap")`） |

`tests/test_stage3_retirement_posix.py`（`pytestmark = [posix_only, memory_only]`）：`test_mkbundle_plan_only_stage3_candidate`（`bash scripts/rust/mkbundle.sh --target x86_64-unknown-linux-gnu --stage 3 --plan-only` → rc 0，stdout JSON `candidate is True`、`publishable is False`）、`test_mkbundle_plan_only_stage2_after_retire_refused`（以暫存 retirement 檔 `retired:true`、`--stage 2.4` 非 dev → rc 2 且輸出含 `HARNESS_RETIREMENT_INCONSISTENT`）、`test_mkbundle_accepts_existing_flags`（`--dev --out <tmp> --plan-only` 仍可解析，未破壞 P3／P4 旗標）。

Rust 測試：

```rust
// crates/sw-proto/tests/release_manifest_retirement.rs
use sw_proto::release_manifest::{validate_retirement, ManifestError, ReleaseManifest};

fn load(name: &str) -> Result<ReleaseManifest, ManifestError> {
    let text = std::fs::read_to_string(format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"))).unwrap();
    ReleaseManifest::parse(&text)
}

#[test]
fn retired_manifest_requires_rust_daemon() {
    assert!(matches!(load("stage3_retired_python_daemon.json"), Err(ManifestError::RetirementInconsistent(_))));
}

#[test]
fn stage3_without_marker_is_invalid() {
    assert!(matches!(load("stage3_not_retired.json"), Err(ManifestError::RetirementInconsistent(_))));
}

#[test]
fn retired_rust_only_manifest_is_valid_without_delegate() {
    let m = load("stage3_retired_rust.json").unwrap();
    assert!(validate_retirement(&m).is_ok());
    assert!(m.python_daemon_retired());
    assert!(m.python_cli_delegate_entry.is_none());
}

#[test]
fn unretired_manifest_still_requires_delegate_entry() {
    assert!(matches!(load("stage1b_missing_delegate.json"), Err(ManifestError::Schema(_))));   // P3 既有行為不變
}
```

```rust
// crates/sw-cli/src/commands/daemon_cmd.rs 內 #[cfg(test)] mod retired_tests（以 P4 的 TestEnv／sw-pal fakes 建 ctx）
#[test]
fn retired_manifest_never_falls_back_to_python_runtime() {
    let env = TestEnv::with_manifest(fixtures::stage3_retired_but_runtime_injected()).without_launcher().without_sibling();
    assert!(matches!(locate_daemon_argv(&env.ctx()), Err(DaemonLocateError::BinaryNotFound)));
}

#[test]
fn unretired_keeps_df05_order() {
    let env = TestEnv::with_manifest(fixtures::stage1b_python_daemon()).without_launcher().without_sibling();
    let argv = locate_daemon_argv(&env.ctx()).unwrap();
    assert_eq!(&argv[1..], ["-I", "-m", "sw_core.daemon"]);
}
```

```rust
// crates/sw-daemon/tests/launcher_retired.rs
#[test]
fn tampered_retired_manifest_refuses_python_child() {
    let m = fixtures::stage3_retired_but_python_impl();   // 繞過 validate_retirement 直接建構
    assert!(matches!(resolve_launch(&m), Err(LaunchError::PythonDaemonRetired)));
}

#[test]
fn retired_rust_manifest_runs_native() {
    assert!(matches!(resolve_launch(&fixtures::stage3_retired_rust()), Ok(LaunchPlan::RustNative)));
}
```

fixtures 檔：`crates/sw-proto/tests/fixtures/stage3_retired_python_daemon.json`、`stage3_not_retired.json`、`stage3_retired_rust.json`、`stage1b_missing_delegate.json`（依 arch §10.1 形狀；Rust daemon 只有 `launcher`）。

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_retirement.py tests/test_stage3_retirement_posix.py` 與 `cargo test -p sw-proto --test release_manifest_retirement -p sw-cli -p sw-daemon` → Expected: FAIL（`ImportError: tools.stage3.retirement`；Rust `unresolved import validate_retirement`、`RetirementInconsistent` 不存在）。
- [ ] **Step 3：實作**（luna）依規則；Rust 部分只改 `sw-proto`／`sw-cli`／`sw-daemon` 的業務邏輯，不得新增平台 cfg；修改後跑 `python3 scripts/rust/check_cfg_boundary.py`；同步更新 arch §10.1；登錄兩個測試檔。
- [ ] **Step 4：Run** `python3 -m pytest -q tests/test_stage3_retirement.py tests/test_stage3_retirement_posix.py`、`cargo test --workspace`、`python3 scripts/rust/check_cfg_boundary.py`、`scripts/rust/win-test.sh --package sw-cli --package sw-daemon`（WSL interop，Windows 原生執行）→ Expected: PASS。【BLOCKED-OWNER BO-P9-17】正式 Stage 3 Windows bundle 須在擁有者 Windows 自架 runner 原生 build；本環境的交叉編譯結果不得當作 release artifact。
- [ ] **Step 5：commit** `feat(packaging): 新增 Python daemon 退役切換並保留 Python 測試工具`（files 含 `tools/stage3/retirement.py`、`tests/fakes_retirement.py`、`scripts/rust/mkbundle.sh`、`contracts/release_manifest.schema.json`、三個 crate 的變更與測試、`.github/workflows/release.yml`、`docs/rust-refactor/plans/rust-architecture.md`、兩個測試檔、fragment）。

---

## Task 17：`python3 -m tools.stage3` 入口與擁有者程序文件

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p9-17-stage3-cli-procedures.md`
**BLOCKED-OWNER:** 文件中標示的所有實機／時間步驟（BO-P9-01～18）；本 Task 只交付可執行程序與文件。

**Files:**
- Create: `tools/stage3/__main__.py`
- Create: `docs/rust-refactor/procedures/stage3-runbook.md`、`docs/rust-refactor/procedures/sequential-ab.md`、`docs/rust-refactor/procedures/canary.md`、`docs/rust-refactor/procedures/rollback-drill.md`、`docs/rust-refactor/procedures/od09-deferred-freeze.md`、`docs/rust-refactor/procedures/python-daemon-retirement.md`、`docs/rust-refactor/procedures/evidence-index.md`
- Marker（P0 T14 檔內 `pytestmark`，非 YAML 登錄）：（`test_stage3_cli.py`、`test_stage3_procedures.py`：皆 `{class: core_portable, resources: [memory_only]}`）
- Test: `tests/test_stage3_cli.py`、`tests/test_stage3_procedures.py`

**Interfaces — Produces：** `main(argv: Sequence[str] | None = None) -> int`；`build_parser() -> argparse.ArgumentParser`。子命令（全部輸出 compact JSON 到 stdout；exit：0＝PASS／成功，1＝FAIL／INCOMPLETE／BLOCKED，2＝參數錯誤或 `HarnessError`（stdout `{"ok":false,"reason_code":…,"detail":…}`），3＝`INFRA_ERROR`／`BASELINE_INVALID`／`ABORTED`）：

| 子命令 | 參數 | 呼叫 |
|---|---|---|
| `ab verify-manifest` | `--manifest PATH --repo-root PATH` | `load_manifest`＋`verify_frozen` |
| `ab run` | `--manifest PATH --out DIR --repo-root PATH` | `run_ab` |
| `canary policy freeze` | `--policy PATH --decision-id ID --frozen-by TEXT --evidence-owner linux=NAME --evidence-owner windows=NAME --addenda PATH` | `canary_policy.freeze_policy` |
| `canary collect` | `--cli PATH --ledger-dir DIR --policy PATH --platform {linux,windows} [--once]` | `collect_once`／`collect_loop`（draft policy → exit 2） |
| `canary note` | `--ledger-dir DIR --kind {planned_restart,planned_downtime} --from ISO --to ISO --reason TEXT --author TEXT --platform P` | `write_note` |
| `canary incident open` | `--ledger-dir DIR --kind K --platform P --artifact-id ID --summary TEXT [--evidence REF ...]` | `open_manual` |
| `canary incident close` | `--ledger-dir DIR --policy PATH --id ID --resolution R --note TEXT --closed-by TEXT [--issue-ref REF]` | `close` |
| `canary incident list` | `--ledger LEDGER [...] --policy PATH [--platform P]` | `current_incidents` |
| `canary report` | `--policy PATH --addenda PATH --ledger LEDGER [...] --platform P --artifact-id ID [--carryover PATH] [--md] [--out PATH]` | `gate`／`report_md`（`--out` 寫 ingest 用 JSON） |
| `canary pack-collector` | `--out PATH` | `pack_collector.pack` |
| `rollback-drill run` | `--cli PATH --platform P --boards COM0,COM1 --expect-current V --expect-previous V --out DIR [--forward-argv JSON] [--corrupt-from BUNDLE] [--start-fail-from BUNDLE] --i-understand-this-restarts-the-daemon` | `run_drill`（缺最後旗標 → exit 2；未給 `--corrupt-from`／`--start-fail-from` → verdict `INCOMPLETE`、exit 1） |
| `rollback-drill make-corrupted` | `--src BUNDLE --dst DIR` | `make_corrupted_bundle` |
| `rollback-drill make-start-fail` | `--src BUNDLE --dst DIR` | `make_start_fail_bundle` |
| `evidence target` | `--index PATH --stage S --platform P --release-manifest PATH --contract-revision REV --tool-revision REV --case-list-sha256 HEX --set-by TEXT` | `set_target` |
| `evidence env-id` | `--env JSON_PATH` | `environment_id` |
| `evidence ingest` | `--index PATH --kind K --item ID --platform P --report PATH --evidence-root DIR --environment JSON_PATH [--release-manifest PATH] [--reuse-of ENTRY_ID]` | `evidence_ingest.ingest`（PASS 的唯一入口） |
| `evidence add` | `--index PATH --entry JSON_PATH` | `add_entry`（PASS／XFAIL 只接受 `manual-procedure` 項目；其餘一律走 `ingest`） |
| `evidence status` | `--index PATH --stage S --evidence-root DIR` | `stage_status`（`--evidence-root` 必填） |
| `evidence report` | `--index PATH --evidence-root DIR` | `render_report` |
| `od09 stats` | `--case-dir DIR --platform P --release-manifest PATH` | `baseline_stats` |
| `od09 propose` | `--linux-stats JSON --windows-stats JSON --out PATH` | `propose` |
| `od09 freeze` | `--acceptance PATH --proposal PATH --decision-id ID --approved-by TEXT [--set <key>.per_platform.<os>.<field>=VALUE ...] --index PATH --evidence-root DIR --addenda PATH --out PATH` | `freeze`（`--out` 寫 ingest 用 JSON） |
| `od09 check` | `--acceptance PATH --run-started-at ISO` | `check_frozen_before` |
| `retirement verify` | `[--mode {repo,release}] [--evidence-root DIR] [--bundle-manifest PLATFORM=PATH ...]` | `verify`（`release` 模式且 `retired:false` → exit 2、`HARNESS_RETIREMENT_NOT_APPROVED`） |
| `retirement bundle-plan` | `--stage S [--dev]` | `bundle_plan` |
| `retirement github-output` | — | `github_output` |

**程序文件內容要求**（每份皆繁中；每個需擁有者硬體或時間的步驟以行首 `【BLOCKED-OWNER BO-P9-xx】` 標示；命令以 ```` ```bash ```` 區塊呈現且必須能被 `build_parser()` 解析）：

| 文件 | 必含段落 |
|---|---|
| `stage3-runbook.md` | 全流程順序：(1) OD-09 延後凍結（BO-P9-12）——**於 Stage 0 出口、任何 Rust 候選長跑（最早為 `S1B-L3-MIXED-48H-WINDOWS`）開始前完成**；一旦索引有任何 `candidate_longrun` entry，`od09 freeze` 即拒絕；凍結後以新 revision 重跑兩平台基線 gate → (2) Canary policy 凍結（`canary policy freeze`，BO-P9-06）→ (3) 以 `mkbundle.sh --stage 3` 建出兩平台 Stage 3 候選 bundle（`retired:false` 即可建；Windows 於自架 runner 原生 build，BO-P9-17），保存受測 bundle 目錄，並以 `evidence target --platform linux|windows` 分別設定 target → (4) Linux L1 契約黑箱與 L1.5 A/B（本環境可跑）→ (5) Windows L1 與 L1.5 A/B（BO-P9-01）→ (6) L2 A/B（BO-P9-02／03）→ (7) release candidate 的 L3 P0＋P1＋remote（BO-P9-18）→ (8) Linux 48h A/B 再 Windows 48h A/B（BO-P9-04／05，依序、約 9 天 bench）→ (9) Canary 兩平台並行累積（BO-P9-07／08）→ (10) 回滾演練含 D7、D8（BO-P9-10／11）→ (11) 事故結案（BO-P9-09）→ (12) 全部以 `evidence ingest` 登錄後 `evidence status --stage 3 --evidence-root …` 為 PASS → (13) 退役裁決（BO-P9-13）：寫 `packaging/python_daemon_retirement.json`（含兩平台 target artifact id）→ (14) release workflow 以 `retirement verify --mode release --bundle-manifest` 核對並**原樣發佈受測 bundle**，不重建；artifact 變更時的證據有效性判定規則（§12.2：不得直接合併不同版本執行量；`runtime_digest` 相同才可 `reuse_of`） |
| `sequential-ab.md` | manifest 欄位說明與 L1.5／L2／L3 範本；三個 suite adapter 的實際呼叫；A→B 釋放驗證失敗的處置（不得手動略過、不得改 `release.timeout_s` 後重跑同一 run_id）；`HARNESS_ARTIFACT_MISMATCH` 的意義；L3 必須 on-demand、明確 endpoint、無外部監管的故障保留；破壞性案例的 `authorization`；Windows com0com 實例的 `test_device_allowlist`；複驗須事前在 manifest 宣告 |
| `canary.md` | 部署範圍：**Windows 只部署在單人使用的測試工作站**（OD-12：整機本機使用者皆可信，多人共用機在完成多使用者授權前不得部署；文件不得宣稱「同使用者隔離」）；Linux：systemd user service 範本（`ExecStart=/usr/bin/python3 -m tools.stage3 canary collect …`）；Windows：`py -3 sw-canary.pyz collect …` 以工作排程器「登入時」觸發的建立步驟；凍結前列出 Canary 期間所有時區／DST 變更（`utc_offset_changes`）；計畫性重啟前必寫 `canary note`；事故分診表（kind → 必查證據 → 允許的 resolution，含 `harvest_integrity` 與自動 `safety_violation`）；每週 `canary report` |
| `rollback-drill.md` | 前置（bench 獨占、兩塊板 READY、無 flash／file transfer）、以 `mkbundle.sh --dev --version-override` 建 start-fail 來源 bundle、D0～D8 說明（跨重啟不以 `bridge_generation` 判定）、`--i-understand-this-restarts-the-daemon`、`INCOMPLETE` 與 `limitations` 的意義、失敗時的人工復原步驟 |
| `od09-deferred-freeze.md` | 時序要求（Stage 0 出口、早於 `S1B-L3-MIXED-48H-WINDOWS`）、`od09 stats → propose → freeze → 重跑基線 gate → check` 命令、`--set` 覆寫的紀錄方式、`owner-decisions-addenda.md` 為附加式紀錄 |
| `python-daemon-retirement.md` | 退役前置（`evidence status --stage 3` PASS、兩平台 Stage 3 target artifact）、受測 bundle 的保存位置、修改 `packaging/python_daemon_retirement.json` 的欄位、`retirement verify --mode release --evidence-root --bundle-manifest`、不重建受測物的理由、保留清單、不刪除 `sw_core/` 的理由 |
| `evidence-index.md` | 出口項目錄與 `evidence_kind`、每個 kind 的 `evidence ingest` 命令、entry 欄位（含 `environment_id` 的 env JSON 欄位、`workload_sha256`、`runtime_digest`）、結果語意（SKIP／INFRA_ERROR／INSUFFICIENT 不等於 PASS）、FAIL 不可被後續 PASS 抵銷、`reuse_of` 條件、以 `satisfying_entries` 把 BC／SC／DF 與錯誤碼由 `IMPLEMENTED` 改為 `VERIFIED` 的擁有者步驟（§0.3） |

**Steps:**

- [ ] **Step 1：寫失敗測試**（agy）

`tests/test_stage3_cli.py`：

| 測試名 | 斷言 |
|---|---|
| `test_help_lists_groups` | `build_parser().format_help()` 含 `ab`、`canary`、`rollback-drill`、`evidence`、`od09`、`retirement` |
| `test_harness_error_exit_2` | `main(["retirement","verify","--mode","release"])`（repo 預設 `retired:false`）回 2，stdout JSON `reason_code == "HARNESS_RETIREMENT_NOT_APPROVED"` |
| `test_stage3_bundle_plan_candidate_exit_0` | `main(["retirement","bundle-plan","--stage","3"])` 回 0，stdout JSON `candidate is True` |
| `test_evidence_status_requires_root` | 缺 `--evidence-root` → 2 |
| `test_canary_policy_freeze_cli` | draft policy＋`canary policy freeze …` → 0，檔案 `status=="frozen"`、addenda 新增一列 |
| `test_drill_requires_ack_flag` | 缺 `--i-understand-this-restarts-the-daemon` → 2 |
| `test_canary_report_exit_codes` | PASS 資料 → 0；INCOMPLETE → 1；draft policy → 1 且 `result == "POLICY_NOT_FROZEN"`；policy 被改 → 1 且 `result == "POLICY_CHANGED"` |
| `test_ab_run_infra_exit_3` | 替身使 A 釋放不明 → 3 |
| `test_output_compact_json` | stdout 不含 `": "` 與 `", "`，且 `ensure_ascii=False`（中文 detail 原樣） |
| `test_od09_check_exit` | 未凍結 → 1；已凍結且早於 run → 0 |
| `test_retirement_github_output` | stdout 恰 `retired=false\n` |

`tests/test_stage3_procedures.py`：

```python
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from tools.stage3.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "docs" / "rust-refactor" / "procedures"
DOCS = ["stage3-runbook.md", "sequential-ab.md", "canary.md", "rollback-drill.md", "od09-deferred-freeze.md",
        "python-daemon-retirement.md", "evidence-index.md"]


@pytest.mark.parametrize("doc", DOCS)
def test_commands_parse(doc):
    text = (PROC / doc).read_text(encoding="utf-8")
    cmds = [ln.strip() for block in re.findall(r"```bash\n(.*?)```", text, re.S) for ln in block.splitlines()
            if ln.strip().startswith("python3 -m tools.stage3 ")]
    assert cmds, doc
    for c in cmds:
        argv = shlex.split(c)[3:]
        build_parser().parse_args(argv)


def test_runbook_marks_every_blocked_owner_item():
    text = (PROC / "stage3-runbook.md").read_text(encoding="utf-8")
    for n in range(1, 19):
        assert f"【BLOCKED-OWNER BO-P9-{n:02d}】" in text


def test_runbook_order():
    text = (PROC / "stage3-runbook.md").read_text(encoding="utf-8")
    order = ["BO-P9-12", "BO-P9-06", "BO-P9-17", "BO-P9-01", "BO-P9-02", "BO-P9-18", "BO-P9-04", "BO-P9-05", "BO-P9-07",
             "BO-P9-10", "BO-P9-09", "BO-P9-13"]
    pos = [text.index(f"【BLOCKED-OWNER {k}】") for k in order]
    assert pos == sorted(pos)


def test_od09_freeze_timed_at_stage0_exit():
    for doc in ("stage3-runbook.md", "od09-deferred-freeze.md"):
        assert "S1B-L3-MIXED-48H-WINDOWS" in (PROC / doc).read_text(encoding="utf-8")


def test_od12_single_user_and_no_isolation_claim():
    assert "單人使用" in (PROC / "canary.md").read_text(encoding="utf-8")
    for doc in DOCS:
        text = (PROC / doc).read_text(encoding="utf-8")
        assert "同使用者隔離" not in text or "不得宣稱「同使用者隔離」" in text, doc
        for ln in text.splitlines():
            if "Defender" in ln:
                assert "關閉" in ln, (doc, ln)   # OD-05：代表機台為第三方 EDR、Defender 關閉，不得把防毒歸因於 Defender
```

另加 `test_no_interactive_status_in_procedures`：七份文件與 `tools/stage3/*.py` 皆不含字串 `interactive-status`（`interactive_status` 只允許出現在說明「不得呼叫」的句子中，以 `不得呼叫` 同行判定）。

- [ ] **Step 2：Run** `python3 -m pytest -q tests/test_stage3_cli.py tests/test_stage3_procedures.py` → Expected: FAIL（`ModuleNotFoundError: tools.stage3.__main__`、文件不存在）。
- [ ] **Step 3：實作**（haiku）`__main__.py` 依子命令表；文件依內容要求撰寫，命令全部可被 parser 解析。
- [ ] **Step 4：Run** 同上，再跑全套 `python3 -m pytest -q tests/`、`cargo test --workspace`、`python3 -m policy_check --repo .` → Expected: PASS（`policy_check` 的 R-18 docs 對齊由 `docs/rust-refactor/procedures/` 滿足）。
- [ ] **Step 5：commit** `feat(stage3): 新增 Stage 3 工具入口與擁有者執行程序`。

---

## 1. 完成判定（本 Phase）

本 Phase 的「完成」只包含：20 個 Task（1～17 與 5B、7B、14B）的程式、契約、文件與測試全部合併，新測試檔全部依 P0 T14 帶合規 `pytestmark`（`tests/test_resource_markers.py` 稽核通過）；全套 pytest、`cargo test --workspace`、`check_cfg_boundary.py`、`policy_check` 通過；Linux L1.5 A/B 已以 func-test bridge 模式實跑一次並保存報告（Stage 3 target 尚未設定時只保存、不登錄；已設定則以 `evidence ingest --kind ab-report` 登錄，結果照實記錄）；Windows 原生測試（不含 `windows_com`）在擁有者 Windows 主機經 interop 執行通過。

**不包含、且不得標 PASS**：§0.2 的 BO-P9-01～18。`evidence status --stage 3 --evidence-root …` 在本 Phase 結束時預期為 `BLOCKED`，PR 描述須附該輸出。

## 2. 模型分工總覽

| Task | 標題 | Test author | Implementer |
|---|---|---|---|
| 1 | 契約檔、schema 與基礎模組 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 2 | 共用基礎測試替身 | haiku-xhigh | agy-gemini-3.8-flash-high |
| 3 | A/B manifest 載入、凍結與案例涵蓋檢查 | haiku-xhigh | agy-gemini-3.8-flash-high |
| 4 | A→B 資源釋放驗證 | haiku-xhigh | codex-gpt-6-luna-max |
| 5 | 實例隔離、artifact 選定與乾淨 DUT 狀態 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 5B | L1.5／L2／L3 suite adapter | haiku-xhigh | codex-gpt-6-luna-max |
| 6 | Sequential A/B 執行器 | haiku-xhigh | codex-gpt-6-luna-max |
| 7B | L1.5 A/B 差分 | codex-gpt-6-luna-max | haiku-xhigh |
| 7 | A/B 判定 | codex-gpt-6-luna-max | haiku-xhigh |
| 8 | Canary WAL 收割與提交計數 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 9 | Canary collector 與 zipapp | haiku-xhigh | codex-gpt-6-luna-max |
| 10 | 干擾／交接分類與活躍日 | haiku-xhigh | codex-gpt-6-luna-max |
| 11 | 事故追蹤 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 12 | Canary policy 凍結、帳本彙總與配額 gate | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| 13 | 實機回滾演練 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 14 | Stage 0～3 出口證據索引 | codex-gpt-6-luna-max | agy-gemini-3.8-flash-high |
| 14B | 證據 ingest | haiku-xhigh | codex-gpt-6-luna-max |
| 15 | OD-09 延後凍結與凍結輸出對正式 gate 的讀寫契約 | codex-gpt-6-luna-max | haiku-xhigh |
| 16 | Python daemon 退役切換 | haiku-xhigh | codex-gpt-6-luna-max |
| 17 | 工具入口與擁有者程序文件 | agy-gemini-3.8-flash-high | haiku-xhigh |

判定器／oracle 類 Task（7、7B、12、14、15）比照 P2（Task 7、8、15）由 `codex-gpt-6-luna-max` 撰寫測試；平台相依實作（Windows ctypes／registry／事件記錄、檔案分享旗標，Task 4、8、9）依 master plan §2 由 luna 實作。

審查：依 master plan §1，本 Phase 完成時做一次 Phase 層級雙審（Claude Sonnet effort max＋Codex `gpt-6.1-sol` reasoning medium）；高風險 Task 4、5B、6、7B、8、10、13、14B、16 另於該 Task commit 前由 Claude Sonnet（effort max）做一次單審。審查者永不為該 Task 的實作者。共用替身（`tests/stage3_fakes.py`）與各 Task 的 `tests/fakes_*.py` 依規格 §10.4 屬測試工具變更，須獨立審閱。
