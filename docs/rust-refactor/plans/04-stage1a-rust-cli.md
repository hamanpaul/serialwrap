# P3：Stage 1A Rust CLI 資料面 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Cargo workspace（五個 crate 骨架），完成 `sw-proto`（常數、錯誤碼、RPC／設定／WAL 型別與 Python 相容語意層）、`sw-pal` client 側（sysenv、路徑、RPC transport client UDS／TCP、平台選擇器、委派用 exec）、`sw-cli` Stage 1A 資料面（全域選項、#108 endpoint 解析、白名單重試 client、`cli_trace`、與 Python 位元組相同的 stdout／stderr／exit code、12 個原生群組），以及對未承接管理群組的固定路徑透明委派；並交付可同時指向 Python 與 Rust artifact 的 `tests/contract_blackbox/`、cfg 邊界檢查與 WSL interop 的 Windows 原生測試執行。

**Architecture:** 依 `rust-architecture.md` §1～§3、§4.1／§4.6／§4.9、§7、§9、§10。`sw-cli` 只依賴 `sw-proto`＋`sw-pal`；平台差異只出現在 `sw-pal`，業務碼只看 `ClientCaps` capability 值。CLI 為單執行緒同步程式，argparse 相容層與 Python 語意層（`pyjson`／`pycompat`／`pyfmt`／`pypath`／`pyre`／`yaml11`）全部自製並以 Python 3.12 產生的 golden 驗證。委派以 release manifest 的絕對 entry `execve`（POSIX）或 CreateProcess＋Job 等待（Windows）。

**Tech Stack:** Rust 1.96.1（edition 2021，`x86_64-unknown-linux-gnu`、`x86_64-pc-windows-gnu`）；依賴照 arch §1.2 以 `=` 釘版（本 phase 用到 `indexmap =2.11.4`、`fancy-regex =0.16.2`、`yaml-rust2 =0.10.4`、`sha2 =0.10.9`、`libc =0.2.186`、`windows-sys =0.61.2`、dev `tempfile =3.23.0`）；Python 3.12.3（Linux golden 基準）、Windows Python 3.12.10（`py -3.12`，已確認含 PyYAML 與 pytest 9.0.3）；pytest；WSL interop。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §3.1、§3.2、§4.1～§4.3、§9.2、§9.5、§11.3、§11.6（Stage 1A 列）、§12.2（Stage 1A）、§15.9；`owner-decisions-2026-10-15.md`（OD-06、OD-08、OD-12、OD-15）；`plans/00-master-plan.md`；`plans/rust-architecture.md`；`plans/spec-reconciliation.md`（CLI-11.xx、CLI-12.xx、DF-06、SC-09、GAP-X1、GAP-G-08、GAP-G-11、RPC-H-03、EVT-D-S15）；盤點 `inventory/01-cli.md`、`03-errors.md`、`07-platform-mgmt.md` §0／§1.8／§4、`09-events-transfer.md` §2／§5／§6、`10-gaps.md` G-04／G-08／G-11／X1。

---

## 0. 範圍、前置與裁決

### 0.1 範圍

| 納入 | 不納入（所屬 Phase） |
|---|---|
| workspace 與五個 crate 骨架；`sw-core`／`sw-daemon` 只有空殼 | daemon 本體、`launcher.rs`（P4／P5） |
| `sw-proto`：`constants`、`error_codes`（生成）、`session_last_errors`（生成）、`rpc_types`、`config_types`（`load_profiles`、`RuntimeConfig` 讀取）、`wal_types`、`pyjson`、`pycompat`、`pyfmt`、`pypath`、`pyre`（parse／translate）、`yaml11`（`resolve`，只讀）、`runtime_paths`、`endpoint`、`version`、`release_manifest` | `yaml11/emit.rs`（config.yaml 寫回，P4）、`pyre/redos.rs`（event schema，P7）、`bench_registry.rs`（P4） |
| `sw-pal` client 側：`SysEnv`、`FsRead`、`Clock`、`RpcConnector`／`RpcStream`、`ProcessCtl::exec_or_wait`、`select_backend`、`fakes` | serial、lock、device、wal_fs、rpc server、console、service_ctl、process_tunnel（P4～P8） |
| `sw-cli` 原生群組：`device`、`session`（含 `console-attach`／`console-detach`）、`alias`、`cmd`、`stream`、`log`、`file`、`wal`、`mcu`、`event`、`profile`、`skill`；`--bench` 解析；`--version`／`--help` | `daemon`、`remote`、`connect`、`benches`、`supervision-mode`、`service`、`setup`、`doctor` 的原生實作（P4） |
| 委派：上列 8 群組經 manifest 固定入口交給 Python | Stage 1B 的安裝佈局切換、rollback、`version_pairing` doctor 檢查（P4） |
| `tests/contract_blackbox/`、`scripts/rust/*`（cfg 邊界、golden 產生、mkbundle `--dev`、win-runner／win-test／win-blackbox、匯入 DLL 與 100 次啟動檢查、A/B 比較器、Stage 1A gate 判定器）、BB-proc 兩檔改讀 A/B 選擇器、`.github/workflows/rust.yml` | `tests/acceptance/cases.yaml` schema 本身（P2；本 phase 只以 `regen_lock.py --add-only` 新增列） |

### 0.2 前置（Consumes from other phases）

| 來源 | 交付物 | 本計畫用途 | 缺少時 |
|---|---|---|---|
| P0 | `contracts/error_codes.yaml`、`contracts/session_last_errors.yaml`（arch §3.2 欄位：`code`、`interfaces`、`retryable`、`reachable`、`status`） | Task 8 生成 `error_codes.rs`／`session_last_errors.rs` | Task 8 停在紅燈，不得自寫 YAML 代替 |
| P1-OC | `02b-stage0-owner-confirmations.md`（OD-14）已合併：`contracts/error_codes.yaml` 新增 `DRAIN_STALLED`、移除頂層 `passthrough:`（`last_error_passthrough` meta 項）、`NOT_READY` 改 `reachable:false`；`session_last_errors.yaml` 的 `promoted_via` 語意改為「出現在回應頂層 `last_error` 欄位」；`behavior_changes.yaml` 新增 SC-34／SC-35、移除 SC-08 | Task 8 的生成器與 `error_codes_sync.rs` 以 P1-OC 後的 YAML 為輸入；生成器**不得**產生任何 passthrough／`LastErrorPassthrough` 變體（SC-34），`promoted_via` 只作資料欄、不影響 `ErrorCode` | P1-OC 未合併前不得開始 Task 8（否則生成物與合併後 YAML 不一致，`--check` 必紅）；停止回報 |
| P0 | `contracts/cli_schema.json` | Task 10 的 `cli_spec.rs` 與 schema 一致性測試 | 一致性測試 `panic!("P0 contracts 缺少")`（不得早退或 SKIP），Task 10 停在紅燈 |
| P0 | T14 的 marker 稽核（`tests/test_resource_markers.py`，10 個 marker 詞彙） | 本 phase 新增的每個 pytest 檔都帶合規 `pytestmark`（§0.5） | 稽核 FAIL 視為本 phase 新增失敗 |
| P1 | DF-06（client 非 JSON 回應 → `INVALID_RESPONSE`）與 SC-09（`resolve_config_path()`：`SERIALWRAP_CONFIG` 原樣勝出（即使不存在）→ `CONFIG_DIR/config.yaml`（存在）→ 系統檔（存在）→ `CONFIG_DIR/config.yaml`，02 T15）的 Python 實作已合併 | 黑箱測試以修正後 Python 為基線 | 兩個黑箱案例對 Python 紅燈；不得改寫 oracle 遷就舊行為 |
| P1 | T18：`contracts/release_manifest.schema.json`（若 P0 已建立則由 P1 核對）、`sw_core/release_manifest.py` 的 `tree_sha256()`／`find_release_manifest()`／`check_version_pairing()`、`contracts/golden/tree_sha256.ndjson` | Task 16 manifest 產生與驗證；委派後 Python `doctor` 的 `version_pairing` | Task 16 的 schema／tree 驗證 FAIL（不得 SKIP），Task 16 停在紅燈 |
| P2 | `func-test/runner.py` 外部模式（`--mode pty|bridge|both`、`--selftest-report`，`cli_client` 經 `resolve_cli_exe()` 讀 `SERIALWRAP_CLI_EXE`）；Linux bridge 自測報告 `~/b-log/rust-refactor/p2/selftest-linux/selftest-report.json`；`tests/acceptance/cases.yaml`／`cases.lock.json`（`sets.cli_1A`）與 `regen_lock.py --add-only` | Task 18 L1.5、`stage1a_gate.py` 的必要案例分母 | Task 18／19 對應步驟列「證據不足」並阻擋出口，不得標 PASS |

`tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py` 讀 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE` 的修改**由本 phase Task 2 負責**（P2 只做 marker 分類，05 計畫只驗證），不再列為外部前置。

### 0.3 委派集合裁決（與任務敘述及規格 §4.2 註腳的差異）

規格 §4.2 註腳要求 1A 委派「第 14～20 項與 `session console-attach/detach`」，且把 `benches` 列為 1A 純 RPC。本計畫改依調和表與 arch §7.4：

- `session console-attach`／`console-detach` 在 Python 只是 RPC（`cli.py:2532-2539`，01 §6.3），沒有本地終端橋接 → **1A 原生承接**（`CLI-11.08`）。
- `benches`、`connect` 不送 RPC，依賴 remote registry 並有 prune 副作用（01 §6.13） → **1A 委派**，1B 原生（`CLI-11.09`）。`--bench` 解析（只讀 `benches.state.json`）1A 原生。
- 委派集合固定為資料：`sw_cli::delegate::DELEGATED_GROUPS = ["daemon","remote","connect","benches","supervision-mode","service","setup","doctor"]`（Task 10 交付，`cli_spec` 與 `run` 共用）。擁有者若要改回規格註腳的分法，只改這個常數與 `test_bb_delegation.py` 的參數表，不改其他程式碼。
- **與 P2 案例分期的對照**：P2 `test_cli_stage_rule`（03 L1963）把含 `console_pty`／`console_telnet`／`human_frontend` 的案例標為 `cli_stage:"1B"`，因此 `cases.lock.json` 的 `sets.cli_1A` 不含 console 類案例。本 phase 原生承接的是 `console-attach`／`console-detach` 的 **RPC 組包**，不含終端橋接；其 Stage 1A 證據由本 phase 黑箱（`test_bb_rpc_params.py` 兩列、`test_bb_daemon_e2e.py::test_console_attach_detach`）提供，並在 `stage1a_gate.py` 的覆蓋表（Task 18）中以 `p3_extra` 列入必要集合；終端橋接類案例仍隨 `cli_1B` 在 P4 出口計算。是否把 console RPC 案例改列 `cli_stage:"1A"` 屬 P2 規則變更，列為跨 phase 待辦（§3 B9），不在本計畫修改。

### 0.4 執行順序

1. **逐 Task 紅→綠**：Task 1～19 依編號逐一執行 Step 1～6（寫測試、確認紅燈、commit 測試、實作、確認綠燈、commit 實作），前一個 Task 的 Step 6 完成且雙審修正後才開始下一個 Task 的 Step 1。不採「先批次寫完全部紅燈」：後段 Task 的紅燈若因前段 Task 的 `todo!` 失敗，失敗原因就不是該 Task 的契約，紅燈沒有鑑別力；各 Task 的 Step 5「整個 crate PASS」也只在逐 Task 執行時成立。
2. Task 1 依本文件所有 Task 的 Interfaces 建立 stub（函式本體 `todo!("P3-T<n>: <fn>")`），讓後續測試能編譯。依賴順序：1 骨架 → 2 harness → 3 `pyjson` → 4 `pycompat`／`pyfmt`／`pypath` → 5 `pyre` → 6 `yaml11`／`config_types` → 7 資料型別 → 8 錯誤碼 → 9 `sw-pal` → 10 argparse＋委派路由 → 11 output／trace → 12 endpoint → 13 client → 14 RPC 指令 → 15 event／profile／skill → 16 委派執行 → 17 Windows interop → 18 A/B 與 gate 判定器 → 19 CI 與 Stage 1A 出口 gate。
3. 每個 Task 實作完成後依 master plan 進行雙審（審查 A：Claude Sonnet max；審查 B：Codex gpt-6.1-sol medium），修正後才進下一個 Task。
4. **oracle 凍結（規格 §10.4、§11.3）**：Step 3 commit 的測試、golden 與 fixture 即為該 Task 的 oracle。Step 4～6 不得修改這些檔案；每個 Task 的 Step 5 另跑 `git diff --name-only <Step 3 commit> HEAD -- <Step 3 commit 的檔案清單>`，輸出必須為空。確需修改時，另開獨立 commit（subject 以 `test(oracle):` 開頭），在 PR 描述「oracle 變更」段落逐條列出理由，並取得非實作者的審查者核可後才可繼續；實作者不得自行核可。golden 產生器輸出改變（例如 Python 版本更新）同樣走此流程。

### 0.5 分支與 commit

- 工作分支：`wt/rust-refactor/p3-stage1a-cli`，PR base 為 `feature/rust-refactor`（CLAUDE.md 分支政策：base 為 feature 分支時來源須為同 slug 的 `wt/<slug>/<subtask>`）。禁止 commit 到 `main`。
- commit 一律 Conventional Commits 繁中 subject，trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。**本文件各 Step 的 `git commit -m "<subject>"` 為簡寫**，實際一律執行 `git commit -m "<subject>" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`；`policy_check` 與審查者以此核對。
- changelog fragment 一律用 `policy_check.changelog.parse_fragment` 可解析的 YAML frontmatter（`type` ∈ `feat|fix|refactor|perf|change|docs|remove|deprecate|security`），例如：
  ```markdown
  ---
  type: feat
  scope: rust-cli
  ---
  新增 Rust Cargo workspace 骨架與平台 cfg 邊界檢查。
  ```
  各 Task 標頭的「Changelog fragment」只寫檔名與正文；`type` 一律 `feat`（Task 2、17、18、19 的測試／工具類為 `change`），`scope` 為 `rust-cli`。不得使用 `### Added` 之類的 Keep a Changelog 標題。
- 每個 commit 前：`cargo test --workspace`（已實作部分）、`python3 -m pytest -q tests/`、`python3 -m policy_check --repo .`。紅燈 commit 允許 Rust 測試失敗，但 `python3 -m pytest -q tests/` 中**非本 phase 新增**的測試不得出現新失敗；本 phase 新增、預期紅燈的 pytest 以 `pytest.mark.xfail(strict=True, reason="P3-T<n> 紅燈")` 標示（本文件的測試範例未逐一寫出此 decorator，紅燈 commit 時一律補上），實作完成的同一 commit 移除標記。
- **紅燈不得是 collection error**：`xfail` 攔不住 import 失敗或語法錯誤。Step 1 必須同時提交可 import 的 stub 模組（Python：函式本體 `raise NotImplementedError("P3-T<n>")`；腳本：存在但 `sys.exit(2)`），讓紅燈發生在執行期 assertion，並以 `xfail(strict=True, raises=(NotImplementedError, AssertionError))` 標示。
- **marker（P0 T14）**：本 phase 新增的每個 pytest 檔都帶 `pytestmark`，平台 marker 恰一個（`core_portable`／`posix_only`／`windows_native`）、資源 marker 至少一個（`memory_only`／`process_rpc`／`posix_pty`…），只用 P0 註冊的詞彙。對照：`tests/test_rust_cfg_boundary.py`、`test_rust_goldens_fresh.py`、`test_rust_error_codes_gen.py`、`test_rust_ab_compare.py`、`test_rust_stage1a_gate.py`、`test_rust_check_win_imports.py` → `core_portable`＋`process_rpc`（皆以 subprocess 執行腳本）；`test_rust_ci_workflow.py`、`test_rust_readme_trust_scope.py` → `core_portable`＋`memory_only`；`test_ab_env_bbproc.py` → `posix_only`＋`memory_only`；`test_rust_workspace_layout.py` → `core_portable`＋`process_rpc`；`test_rust_mkbundle.py`、`test_rust_win_scripts.py` → `posix_only`＋`process_rpc`；`tests/contract_blackbox/**` → 依案例：stub／subprocess 類 `core_portable`＋`process_rpc`，PTY／daemon 類 `posix_only`＋`posix_pty`＋`process_rpc`，Windows 專屬 `windows_native`＋`process_rpc`。黑箱另註冊的非平台 marker 只有 `rust_only`、`needs_py_daemon`、`needs_delegation`。

### 0.6 模型分工總表（master plan §2；測試作者 ≠ 實作者）

| Task | 標題 | 測試作者 | 實作者 |
|---|---|---|---|
| 1 | Cargo workspace 骨架、stub 與 cfg 邊界檢查 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 2 | 黑箱契約 harness（`tests/contract_blackbox/`） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 3 | `sw-proto::pyjson` | haiku-xhigh | agy-gemini-3.8-flash-high |
| 4 | `sw-proto::pycompat`／`pyfmt`／`pypath` | agy-gemini-3.8-flash-high | haiku-xhigh |
| 5 | `sw-proto::pyre`（Python `re` 相容） | haiku-xhigh | codex-gpt-6-luna-max |
| 6 | `sw-proto::yaml11` 與 `config_types` | haiku-xhigh | agy-gemini-3.8-flash-high |
| 7 | `sw-proto` 資料型別（constants、runtime_paths、endpoint、version、rpc_types、wal_types、release_manifest） | agy-gemini-3.8-flash-high | haiku-xhigh |
| 8 | 錯誤碼生成與命名空間 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 9 | `sw-pal` client 側 | haiku-xhigh | codex-gpt-6-luna-max |
| 10 | argparse 相容層與 `cli_spec` | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 11 | `output` 與 `cli_trace` | agy-gemini-3.8-flash-high | haiku-xhigh |
| 12 | endpoint 解析（#108）與 bench state | haiku-xhigh | agy-gemini-3.8-flash-high |
| 13 | RPC client（白名單重試、退避、TIMEOUT 探測） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 14 | 資料面 RPC 指令與 `main` 分派 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 15 | `event`／`profile test`／`skill` | agy-gemini-3.8-flash-high | haiku-xhigh |
| 16 | Stage 1A 透明委派與 `mkbundle.sh --dev` | haiku-xhigh | codex-gpt-6-luna-max |
| 17 | Windows 原生 interop 執行（win-runner／win-test／win-blackbox、匯入 DLL 與 100 次啟動檢查） | haiku-xhigh | codex-gpt-6-luna-max |
| 18 | A/B 比較器、Rust CLI→Python daemon 黑箱與 Stage 1A gate 判定器 | haiku-xhigh | agy-gemini-3.8-flash-high |
| 19 | Rust CI workflow 與 Stage 1A 出口 gate | agy-gemini-3.8-flash-high | haiku-xhigh |

分工理由：Task 13（禁止危險重送的重試邏輯）與 Task 17（Windows 行程、WSLENV、staging、exit code 與平台證據）屬 master plan §2「高風險並行／平台」，實作者為 `codex-gpt-6-luna-max`，測試作者改用不同模型。

### 0.7 相對 `rust-architecture.md` 的偏離（本 phase Task 1 在同一 PR 先修訂 arch 並經審閱）

arch 前言規定分計畫不得牴觸 arch，要牴觸時先改 arch。下列偏離由 Task 1 Step 4 寫回 arch 對應章節（並在 `spec-reconciliation.md` 登記需要登記者），審查者以本表核對。名稱沿用 P3 版本，因 `05-stage1b-rust-cli-mgmt.md` §0.2 已以這些名稱消費（`ClientPlatform`、`FsRead`、`SockTimeout`、`exec_or_wait(&[OsString]) -> PalError`），並由 P4 以 `type ClientPlatform = Platform`、`FsOps: FsRead` 擴充。

| arch 章節 | arch 原文 | P3 版本 | 理由 |
|---|---|---|---|
| §4、§4.1 | `Platform`／`sw_pal::native()`／`FsOps` | client 側子集 `ClientPlatform`／`native_client()`／`FsRead`；P4 擴為 `Platform` 並保留別名 | 1A 只需唯讀與 client 能力 |
| §4.1 `SysEnv` | `home_dir`、`expanduser`、`system_config_dir`… | P3 交付 `var`、`vars`、`current_user_home`、`user_home`、`system_config_dir`、`current_exe`、`realpath`、`terminal_columns`、`stdout_is_terminal`、`errno_name`、`pid`；其餘由 P4 補 | `expanduser` 在 `sw-proto::pypath` 以 `HomeLookup` 實作（golden 可測）；`system_config_dir` 為 SC-09 必需 |
| §4.6 | `connect(ep, Duration)`；`RpcStream: Read+Write`＋`set_timeout`／`last_errno` | `connect(ep, SockTimeout)`；`RpcStream{send_all, recv}`；errno 由 `PalError.os_code` 提供 | Python `settimeout(0)` 非阻塞語意（Q-10）無法以 `Duration` 表達 |
| §4.9 | `exec_or_wait(&[String], …) -> !` | `exec_or_wait(&[OsString], &[(String,String)]) -> PalError` | 委派 argv 須保留非 UTF-8 bytes；失敗必須能回報錯誤 |
| §3.1 `RuntimePaths` | `resolve(env, native) -> RuntimePaths`、`PathBuf` 欄位 | `resolve(env, native, home) -> Result<_, PathsError>`、`String` 欄位、`default_tcp_port: PyInt` | Q-15：非法 port 必須回錯；golden 比對字串 |
| §3.3 | `loads -> Result<_, JsonDecodeError>` | `loads -> Result<_, JsonError>`（`Decode`／`Recursion`） | Python `RecursionError` 不是 `ValueError` |
| §3.7 | `safe_load` 產出 `PyValue` | 產出 `YValue`（含 timestamp、binary、非字串 key） | `PyValue::Dict` 只容字串 key |
| §1.1 `.cargo/config.toml` | 設 `runner`／`linker` | 不設；由 `win-test.sh`／`win-runner.sh` 偵測 WSL 後以 `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_{RUNNER,LINKER}` 注入 | host triple 即 `x86_64-pc-windows-gnu` 的原生 Windows runner 上，`runner` 會套用到原生 `cargo test` |
| §1.1 匯入 DLL allowlist | 無 `api-ms-win-core-*` | 加 `api-ms-win-core-*`（Rust std 的 `WaitOnAddress` 匯入 `api-ms-win-core-synch-l1-2-0.dll`）；明列禁止 `libgcc_s_seh-1.dll`、`libwinpthread-1.dll`、`libstdc++-6.dll` | 2026-10 實測交叉編譯 std exe 的匯入表 |
| §5.10 client 回應累積 | 16 MiB | 256 MiB，並登記 Rust-only 差異（調和表新增一列，ID 由調和表維護者配發） | `wal export --limit 50000` 回應可超過 16 MiB；Python 無上限 |
| §7.2 | 未指定 patch 版本 | argparse 方言釘 **CPython 3.12.3**（`invalid choice` 引號與 `--help` 子命令欄寬在 3.12.x 間不同） | 同一 Rust 引擎只能對齊一個方言 |
| §7.4 | 委派 env「僅加兩個變數」 | 加三個：`SERIALWRAP_DELEGATE_DEPTH`、`SERIALWRAP_DELEGATE_ARGV_SHA`、`SERIALWRAP_RELEASE_MANIFEST=<manifest 絕對路徑>` | Python `python -m sw_core.cli` 下 `find_release_manifest(argv0)` 找不到 bundle manifest |
| §3.3（新增） | — | JSON 巢狀深度上限為 Rust 自訂常數，登記為非契約差異 | Python 上限隨平台與 C 堆疊而變（Linux 3.12.3 約 9998、Windows 3.12.10 約 2998） |
| §4 `PalError` | `PalError{kind,os_code,py_message}` | `PalError{kind,os_code,winerror,strerror,py_message}` | Python `OSError` 的 `winerror`、`strerror` 屬性分開保存（`traits.rs`） |
| §4 `ClientCaps` | arch §4 只有完整的 `Capabilities`，無 client 子集 | `ClientCaps{path_flavor,af_unix,native_backend,default_skill,text_newline,exec_replace}`，值型別為 `PalPathFlavor`、`BackendKind`、`SkillPlatform` | 業務碼只看 capability 值（§2.1）；P4 併入 `Capabilities` |
| §4.4 `EndpointSpec::Unix` | `Unix(PathBuf)` | `Unix(OsString)` | 非 UTF-8 socket 路徑須以原始 bytes 運作（Q-19(a)） |
| §4.6 `RpcConnector` | 無 `resolve_tcp` | 新增 `resolve_tcp(host, port) -> PalResult<()>` | Q-10：TCP `getaddrinfo` 早於 timeout 驗證，需獨立呼叫 |
| §1.3 檔案樹 | sw-cli 無 `lib.rs`、`ctx.rs`；sw-pal 無 `select.rs`、`fakes/{env,rpc,process}.rs` | 補列上述檔案（P3 實際落地 stub） | P3-T1 Files 清單 |
| §2.1 禁止清單 | 未列群組 `use`、OS 名稱比較；`[target.'cfg(` 區段 | 補列 `std::env::consts::OS`／`FAMILY`、群組 `use`、`[package] build`、引號 key 與表形式 build 鍵，`[target.…]` 任何 triple 表 | 審查修正：閘門以語意檢查，文件與閘門須一致 |
| §1.2、Task 1 Interfaces | sw-cli 依賴列為 proto＋pal | sw-cli 另依賴 `indexmap =2.11.4` | argparse `Namespace` 插入序（P3-T10 Produces 直接引用 `IndexMap`）；arch §1.2 已列 sw-cli 為 indexmap 使用者 |

---

## Global Constraints

（摘自 `00-master-plan.md`，只列本 phase 適用者）

- 平台相依 production code 只在 `sw-pal`；其他 crate 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script 間接分支（§3.1）；允許 `#[cfg(test)]` 與 `#[cfg(feature = "test-fakes")]`。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）。
- `--retries` 只對 11 個唯讀方法重試：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`（§4.2）；`session.interactive_status` 絕不重試。
- CLI JSON：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出（CLI stdout 另有 `sort_keys=True`，01 §2.1）；`state.json`／WAL 以 `sort_keys=True`。
- WAL rotate 門檻 `R` 以 Stage 0 擷取的 Python 常數為準，不自行換算 64 MB（§1.1.3）。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12），文件與 help 不得宣稱「同使用者隔離」。
- repo 政策：不得 commit 到 `main`；每個 code PR 附 `changelog.d/<issue>-<slug>.md`；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 本 phase 追加（arch §1.3、§7.4、§9.2）：`sw-cli` 不得連結 `sw-core`；委派**絕不搜尋 PATH**；黑箱測試不得 import `sw_core`（golden 產生器 `scripts/rust/goldens/*` 例外，因為它們是在擷取 Python 基線）；資源相依測試命名 `cap_<resource>__<name>` 並 `#[ignore = "cap:<resource>"]`，ignored 不得計為 PASS。

## Review Focus

1. 委派保真與遞迴：只用 manifest 絕對 entry、不搜 PATH；`SERIALWRAP_DELEGATE_DEPTH`／`SERIALWRAP_DELEGATE_ARGV_SHA` 擋 Rust→Python→Rust 迴圈；stdin、exit code、訊號原樣；Windows Job 必須 `SILENT_BREAKAWAY_OK`，否則 `daemon start` spawn 的 daemon 會在 CLI 結束時被殺。
2. 重試白名單：只有 11 方法 × 3 個 transient 碼重試；退避 0.5×2ⁿ 上限 5 s；`session.interactive_status`、`event.*`、`file.*` 永不重試；TIMEOUT 補探測只對非探測方法、errno 取最後一次主嘗試。
3. stdout／stderr 位元組相容：`sort_keys`、`repr(float)`、`ensure_ascii=False`、全形括號錯誤行、`_run_rpc` 與 `event` 的寫入順序相反、非 TTY stdout 區塊緩衝、Windows 上 `\n`→`\r\n` 轉換。
4. endpoint 解析：`--endpoint` > `--socket`（`None` sentinel，空字串也算明確）> `--bench` > config.yaml（SC-09 探索）> 預設；#108 dangling fallback 只探 unix（win backend 另探 tcp）；CLI 對 config.yaml 唯讀。
5. cfg 邊界：平台差異只在 `sw-pal`；`sw-cli` 只看 `ClientCaps`（`path_flavor`、`af_unix`、`text_newline`、`default_skill`），不得比較 OS 名稱；`check_cfg_boundary.py` 與 `cargo metadata` 依賴方向雙重把關。

---

## 1. Python 基線 quirk 速查（各 Task 以 `Q-xx` 引用）

| ID | 基線行為（Rust 必須逐字重現） | 來源 |
|---|---|---|
| Q-01 | CLI stdout：`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",",":"))` 後接 `"\n"`；遞迴排序；float 用 `repr`；`NaN`／`Infinity` 原樣輸出 | 01 §2.1 |
| Q-02 | stderr 錯誤行：`serialwrap: {ctx + " " if ctx}failed: {code_str}[: {message}][（hint: {hint}）]`；`code_str` 取 truthy 的 `error_code`，否則 truthy 的 `message`（此時不再附 message），否則 `UNKNOWN_ERROR`；值以 `str()` 格式化 | 01 §2.2、`cli.py:144-169` |
| Q-03 | `_run_rpc` 寫入順序：[dangling 通知] → [trace 行] → stdout JSON → [錯誤行] → [版本警告]。`event`：[trace] → stdout → [版本警告] → [錯誤行] | 01 §1.2、§6.15 |
| Q-04 | exit code：一般 RPC 0／2；endpoint 解析錯誤 1；`event` 其他例外 1＋`INTERNAL_ERROR`；`cmd submit` 本地 regex 錯 1（無 stderr）；`profile test` 恆 0；`skill` 0；argparse 錯 2（connect／benches 邊界為 1＋JSON）；`--help`／`--version` 0；stdout 已關閉時 1 或 120（Q-20） | 01 §2.4 |
| Q-05 | **Windows Python 的 stdout／stderr 為 universal newline 模式**：寫入 `"\n"` 會變成 `"\r\n"`（2026-10-15 以 `py -3.12` 3.12.10 實測確認），涵蓋 JSON 行、錯誤行、help、`skill` 內容 | CPython `create_stdio`（`newline=NULL` on Windows） |
| Q-06 | stdout 非 TTY 時為 8192 B 區塊緩衝、程序結束才 flush；TTY 時行緩衝；stderr 每次寫入即輸出。`2>&1` 合流時 stderr 行出現在 stdout JSON 之前 | CPython io 預設 |
| Q-07 | `--socket` 預設 `None`（#120 sentinel），`--socket ""` 也算明確指定；`--endpoint ""` 為 falsy，不採用 | 01 §4.5 |
| Q-08 | `_json_boundary_command_from_argv`：`--socket connect`、`--endpoint=connect` 會被判成 connect 邊界 → stdout `{"error_code":"INVALID_ARGS","ok":false}`、stderr 先 usage＋error 再 `serialwrap: connect failed: INVALID_ARGS`、exit 1 | 01 §0.1（實測） |
| Q-09 | argparse：`allow_abbrev`（唯一前綴）、歧義訊息 `ambiguous option: --v could match --version, --verbose`；全域選項只能在 group 之前；子命令 `--timeout` 的 dest 為 `recover_timeout_s`／`interactive_timeout_s` | 01 §1 |
| Q-10 | `--timeout` 的合法性在建立 socket 時才檢查：`<0`、`NaN`、超出 `_PyTime_t`（>約 9.2e9 s）、`inf` → Python 例外，stdout 空、exit 1；`0` → 非阻塞：tcp `[Errno 115] Operation now in progress`、unix 讀取 `[Errno 11] Resource temporarily unavailable`。endpoint 解析失敗時不會走到這一步；**TCP 的 `getaddrinfo` 也在 timeout 驗證之前**（`create_connection` 先解析位址、再對每個 socket `settimeout`），所以 `--timeout -1 --endpoint tcp://no-such-host.invalid:1` 回 `SOCKET_ERROR`（gaierror）、exit 2，而不是 exit 1；unix 則是 `settimeout` 在 connect 前，`--timeout -1` 直接 exit 1 | `client.py:236-246`（實測） |
| Q-11 | errno 為 `ETIMEDOUT` 的 OSError 屬 `TimeoutError` → `TIMEOUT`（無 message、trace errno 為 null） | `client.py:248` |
| Q-12 | AF_UNIX 路徑過長 → `SOCKET_ERROR`，message `AF_UNIX path too long`，errno null | CPython socketmodule |
| Q-13 | TCP：`socket.create_connection` 依 getaddrinfo 順序逐一嘗試、回報最後一個錯誤；解析失敗為 gaierror（Linux `[Errno -2] Name or service not known`，errno_name null） | CPython socket.py |
| Q-14 | 回應：`recv(65536)` 累積到出現 `\n` 或 EOF；取第一個 `\n` 前內容 `bytes.strip()`；空 → `EMPTY_RESPONSE`；`decode("utf-8","replace")` 後 `json.loads`；非 JSON → `INVALID_RESPONSE`（DF-06）；非 object → `INVALID_RESPONSE`；不檢查 `id` | 01 §3.5 |
| Q-15 | `SERIALWRAP_TCP_PORT` 非 Python `int()` 可解析（含空字串）→ 任何命令（含 `--help`）都 exit 1（GAP-X1）；可解析但超出 65535 時保留大整數，win backend 預設 endpoint 變成 `invalid tcp endpoint: 'tcp://127.0.0.1:99999999'` | 10 §3 X1 |
| Q-16 | POSIX CLI 不讀 `SERIALWRAP_ENDPOINT`；win backend 用 `DEFAULT_ENDPOINT`（以 `tcp://` 開頭時）否則 `tcp://127.0.0.1:{DEFAULT_TCP_PORT}` | 01 §4.3 |
| Q-17 | `SERIALWRAP_RPC_BACKEND`：`posix|unix|termios`→posix、`win|windows|win32`→win、`auto`／未設→平台；其他值在 CLI 端退回平台判斷 | 07 §1.8 |
| Q-18 | config.yaml `socket_path` 為非 str 的 truthy 值（如 `5`）→ `"://" in 5` TypeError → exit 1、stdout 空 | `cli.py:770-778` |
| Q-19 | 非 UTF-8 argv（Python 以 surrogateescape 保留）：(a) 只作為檔案系統路徑使用的值（`--socket`、`--endpoint` 的裸路徑與 `unix://` 路徑、`profile test --profile-dir`／`--sample`、`event add --file`）以原始 bytes 運作；(b) 要寫進 RPC 請求的值（`--selector` 等）在 `json.dumps(...).encode("utf-8")` 恆為 strict → `UnicodeEncodeError` → exit 1、stdout 空、daemon 收 0 筆；(c) 要寫到 stdout 的值（例如 `--bench` 名稱出現在錯誤 message）依 locale：`C`／`POSIX`／`C.UTF-8` 為 surrogateescape，原樣寫出 raw byte、exit code 不變；其他 UTF-8 locale 為 strict，stdout 空、exit 1。stderr 恆為 `backslashreplace`。**Rust 規則**：(a)、(b) 照 Python；(c) Rust 不讀 locale，一律採 strict 語意（exit 1、stdout 空、stderr 一行 `serialwrap: internal error: argument is not valid UTF-8`），在 surrogateescape locale 下與 Python 不同，登記為非契約差異（調和表新增一列）。委派群組的 argv 一律以原始 bytes 交給 Python（Task 10、16） | CPython `create_stdio`／surrogateescape（2026-10 實測） |
| Q-20 | stdout 被關閉（`BrokenPipeError`，Python 與 Rust 都忽略 SIGPIPE）：寫入當下即失敗（輸出 ≥ 8192 B 的直通寫入，例如 `skill`）→ traceback、exit 1；輸出 < 8192 B（幾乎所有 RPC 回應）留在緩衝區、到直譯器結束 flush 才失敗 → `Exception ignored … BrokenPipeError`、**exit 120**。stderr 文字非契約（G5）。SIGINT 預設行為（shell 看到 130）；CLI 不讀 stdin。Rust 照此重現：`run` 期間寫入失敗 → 1；最終 flush 失敗 → 120 | 2026-10 以 3.12.3 實測（盤點 10 G-08 原文「rc=1」為讀碼推論，只涵蓋前者） |
| Q-21 | 回應中的 lone surrogate：Rust 轉 U+FFFD（已登記非契約差異 `RPC-H-03`） | arch §3.3 |
| Q-22 | 檔案 OSError 訊息含檔名 repr：`[Errno 2] No such file or directory: 'x.json'`；socket OSError 不含路徑 | CPython `OSError.__str__` |
| Q-23 | 文字模式讀檔（`event add --file`、bench state）：UTF-8 strict（`UnicodeDecodeError` 訊息照 Python）、universal newlines；`profile test --sample` 用 `errors="replace"` | `cli.py:997`、`1148-1161`、`1880` |
| Q-24 | params 的 None 處理三種並存：`device release --reason` 送 null；`console-attach --label`、`alias assign --profile` falsy 時省略 key；`event list/status/reset/tail` 送 null；`log tail-* --from-seq` 為 None 時省略、`stream tail --from-seq` 預設 0 | 01 §12（CLI-12.08、12.09） |
| Q-25 | `cmd submit` 先做本地 `valid_completion_regex`：失敗 stdout `{"error_code":"INVALID_COMPLETION_REGEX","ok":false}`、exit 1、不寫 stderr、不連 daemon | 01 §6.5（實測） |
| Q-26 | `profile test`：`UNKNOWN_PROFILE`／`SAMPLE_READ_FAILED` exit 0；profile YAML 語法錯誤 → exit 1、stdout 空；只取排序最後一個 YAML 檔的 templates（`EVT-D-C2` 保留） | 01 §6.20、09 §2.6 |
| Q-27 | `skill`：原文輸出、不加換行；`--platform auto` 依實際平台（不看 `SERIALWRAP_RPC_BACKEND`） | 09 §6 |
| Q-28 | `--version` 輸出 `serialwrap <VERSION 去頭尾空白>`；版本警告文字 `serialwrap: client 版本 {cli} 與 daemon 版本 {d} 不一致（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）`；`version` 非非空 str 或 cli 為 `unknown`／相同時不警告 | 01 §1.1、§1.2 |
| Q-29 | trace：`-v`→INFO、`-vv`→DEBUG，否則 `SERIALWRAP_LOG_LEVEL`；只在 logger 有效等級 ≤ INFO 時輸出一行 JSON（key 插入序、不排序）；等級 `0`／`NOTSET` 時有效等級繼承 root（WARNING）→ 不輸出；負整數 → 輸出 | 01 §5、`cli_trace.py` |
| Q-30 | 每次 RPC timeout：`--timeout` 有給就用；否則 `session.recover`／`session.attach`／`session.self_test`／`session.console_attach` 用 45.0，其他 5.0（`file.*` 仍 5.0，`EVT-D-F2` 已知缺口） | 01 §4.6 |
| Q-31 | `retries = int(args.retries or 0)`；`attempts = 1 + max(0, retries)` 只對白名單；`event.*` 不轉送 `--retries` | 01 §3.3 |

---

## 2. Tasks

### Task 1：Cargo workspace 骨架、stub 與 cfg 邊界檢查

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-01-workspace.md`（frontmatter `type: feat`、`scope: rust-cli`；正文：新增 Rust Cargo workspace 骨架與平台 cfg 邊界檢查）
**BLOCKED-OWNER:** 無（本環境可完整產出）。

**Files:**
- Create: `Cargo.toml`、`Cargo.lock`、`rust-toolchain.toml`、`.cargo/config.toml`
- Modify: `docs/rust-refactor/plans/rust-architecture.md`（依 §0.7 逐列修訂 §1.1、§3.1、§3.3、§3.7、§4、§4.1、§4.6、§4.9、§5.10、§7.2、§7.4）、`docs/rust-refactor/plans/spec-reconciliation.md`（登記 §0.7 標「登記」的三項非契約差異與 Q-19(c)）
- Create: `crates/sw-proto/Cargo.toml`、`crates/sw-proto/src/lib.rs` 與 Task 3～8 列出的所有模組檔（stub）
- Create: `crates/sw-pal/Cargo.toml`、`crates/sw-pal/src/{lib.rs,traits.rs,sysenv.rs,clock.rs,fs_ops.rs,rpc_transport.rs,process.rs,select.rs}`、`crates/sw-pal/src/{sysenv,rpc_transport,process,fs_ops,clock}/{posix.rs,windows.rs}`、`crates/sw-pal/src/fakes/{mod.rs,env.rs,clock.rs,fs.rs,rpc.rs,process.rs}`（stub）
- Create: `crates/sw-core/Cargo.toml`、`crates/sw-core/src/lib.rs`（只有 crate doc：`//! Stage 2 承接；P3 為空殼。`）
- Create: `crates/sw-daemon/Cargo.toml`、`crates/sw-daemon/src/main.rs`（`fn main(){ eprintln!("serialwrapd: Stage 1A 尚無 Rust daemon"); std::process::exit(2) }`）
- Create: `crates/sw-cli/Cargo.toml`、`crates/sw-cli/src/{lib.rs,main.rs,ctx.rs,argparse/{mod,parser,help,usage,error}.rs,cli_spec.rs,output.rs,cli_trace.rs,endpoint_resolve.rs,bench_state.rs,client.rs,delegate.rs,assets.rs,commands/{mod,rpc_cmds,event_cmd,profile_cmd,skill_cmd}.rs}`（stub）
- Create: `crates/sw-proto/tests/common/mod.rs`、`crates/sw-cli/tests/common/mod.rs`、`crates/sw-pal/tests/common/mod.rs`（`data_root()`；後續 Task 追加 helper 時列為 Modify）
- Create: `scripts/rust/check_cfg_boundary.py`、`scripts/rust/check_dep_direction.py`
- Create: `tests/test_rust_cfg_boundary.py`、`tests/test_rust_workspace_layout.py`
- Modify: `.gitignore`（加 `/target/`）
- Modify: `.project-policy.yml` 的 `code_paths` 加入 `crates/**`、`contracts/**`、`scripts/rust/**`、`Cargo.toml`、`Cargo.lock`、`rust-toolchain.toml`、`.cargo/**`（若 P0 已加入則略過此步）

**Interfaces:**
- Consumes：無。
- Produces：
  - `Cargo.toml`：`[workspace] members = ["crates/*"]`、`resolver = "2"`；`[workspace.package] version = "<VERSION 檔內容>"`、`edition = "2021"`、`rust-version = "1.96"`；`[workspace.dependencies]` 照 arch §1.2 全部 `=` 釘版；`[profile.release]` 照 arch §1.1。
  - `rust-toolchain.toml`：`channel = "1.96.1"`、`targets = ["x86_64-unknown-linux-gnu","x86_64-pc-windows-gnu"]`。
  - `.cargo/config.toml`：**不設** `runner` 與 `linker`（§0.7；原生 Windows runner 的 host triple 就是 `x86_64-pc-windows-gnu`，`[target.<triple>].runner` 會套用到不帶 `--target` 的原生 `cargo test`）。只放與平台無關的設定（例如 `[term]`）；若無設定可省略此檔。WSL 交叉執行改由 `scripts/rust/win-test.sh`／`win-runner.sh` 偵測 WSL（`WSL_DISTRO_NAME` 或 `/proc/sys/fs/binfmt_misc/WSLInterop`）後以 `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER=scripts/rust/win-runner.sh`、`CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc` 環境變數注入（兩支腳本由 Task 17 交付）。
  - crate 依賴：`sw-proto`（indexmap、fancy-regex、yaml-rust2、sha2；dev：`tempfile`）；`sw-pal`（`[target.'cfg(unix)'.dependencies] libc`、`[target.'cfg(windows)'.dependencies] windows-sys`，`[features] test-fakes = []`；dev：`tempfile`）；`sw-cli`（`sw-proto`、`sw-pal`、`indexmap`（P3-T10 的 `Namespace` 直接引用 `indexmap::IndexMap`，§0.7）；dev：`sw-pal` 加 `features=["test-fakes"]`、`tempfile`；`[lib] name="sw_cli"`、`[[bin]] name="serialwrap" path="src/main.rs"`）；`sw-core`（`sw-proto`、`sw-pal`）；`sw-daemon`（`[[bin]] name="serialwrapd"`，依賴 `sw-core`、`sw-proto`、`sw-pal`）。
  - `scripts/rust/check_cfg_boundary.py [--root DIR]`：stdout 每行 `<relpath>:<line>: <rule>: <text>`；exit 0（乾淨）／1（違規）。
  - `scripts/rust/check_dep_direction.py [--root DIR]`：執行 `cargo metadata --format-version 1 --filter-platform <triple>`（兩個 triple 各一次），違規列出 `<crate> -> <dep>: <rule>`，exit 0／1。

規則（arch §2.1，本 phase 從嚴擴大到 `tests/`、`benches/`、`examples/`）：

| 規則 ID | 掃描範圍（排除 `crates/sw-pal/`） | 命中條件 |
|---|---|---|
| `cfg-predicate` | `crates/*/{src,tests,benches,examples}/**/*.rs` | `#[cfg(`、`#![cfg(`、`#[cfg_attr(`、`cfg!(` 的謂詞不是由 `test`、`feature = "test-fakes"` 與 `all`／`any`／`not` 組成 |
| `os-module` | 同上 | `std::os::unix`、`std::os::windows`（含群組 `use` 寫法） |
| `os-name` | 同上 | `std::env::consts::OS`、`FAMILY`（執行期 OS 名稱比較，arch §2.1；審查修正新增） |
| `ffi-crate` | 同上 | `libc::`、`windows_sys::` |
| `build-script` | `crates/*/build.rs` 存在；`crates/*/Cargo.toml`（tomllib 解析）含 `[package] build`、任何 `build-dependencies`（頂層或 target 表內） | 任一 |
| `target-section` | `crates/*/Cargo.toml`（排除 sw-pal） | 任何 `[target.…]` 區段（含 triple 表；審查修正加嚴） |
| `dep-direction` | `cargo metadata` | `sw-cli` 依賴 `sw-core`；`sw-pal` 依賴 `sw-proto`；`sw-proto` 依賴任何 workspace crate；`sw-pal` 以外直接依賴 `libc`／`windows-sys` |
| `test-data-path` | `crates/*/tests/**/*.rs`（含 sw-pal） | `CARGO_MANIFEST_DIR` 出現在 `tests/common/mod.rs` 以外（測試資料一律經 `data_root()`，Step 4 第 7 點） |

- [ ] **Step 1：寫失敗測試**

`tests/test_rust_cfg_boundary.py`：

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "check_cfg_boundary.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True, text=True, check=False,
    )


def _crate(tmp: Path, name: str, rel: str, body: str) -> None:
    f = tmp / "crates" / name / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    toml = tmp / "crates" / name / "Cargo.toml"
    if not toml.exists():
        toml.write_text(f'[package]\nname = "{name}"\nversion = "0.0.0"\n', encoding="utf-8")


@pytest.mark.skipif(not (ROOT / "crates").is_dir(), reason="crates/ 尚未建立")
def test_repo_clean():
    r = _run(ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    "snippet, rule",
    [
        ("#[cfg(unix)]\nfn a() {}\n", "cfg-predicate"),
        ("#[cfg(windows)]\nfn a() {}\n", "cfg-predicate"),
        ('#[cfg(target_os = "linux")]\nfn a() {}\n', "cfg-predicate"),
        ('#![cfg(target_family = "unix")]\n', "cfg-predicate"),
        ("#[cfg_attr(unix, allow(dead_code))]\nfn a() {}\n", "cfg-predicate"),
        ("fn a() { if cfg!(windows) {} }\n", "cfg-predicate"),
        ("#[cfg(all(test, unix))]\nfn a() {}\n", "cfg-predicate"),
        ("#[cfg(debug_assertions)]\nfn a() {}\n", "cfg-predicate"),
        ('#[cfg(feature = "other")]\nfn a() {}\n', "cfg-predicate"),
        ("use std::os::unix::ffi::OsStrExt;\n", "os-module"),
        ("fn a() { unsafe { libc::getpid(); } }\n", "ffi-crate"),
        ("use windows_sys::Win32::Foundation::HANDLE;\n", "ffi-crate"),
    ],
)
@pytest.mark.parametrize("subdir", ["src", "tests"])
def test_detects_forbidden(tmp_path, snippet, rule, subdir):
    _crate(tmp_path, "sw-cli", f"{subdir}/bad.rs", snippet)
    r = _run(tmp_path)
    assert r.returncode == 1
    assert f"crates/sw-cli/{subdir}/bad.rs:" in r.stdout
    assert f": {rule}: " in r.stdout


@pytest.mark.parametrize(
    "snippet",
    [
        "#[cfg(test)]\nmod t {}\n",
        '#[cfg(feature = "test-fakes")]\npub mod fakes {}\n',
        '#[cfg(all(test, feature = "test-fakes"))]\nmod t {}\n',
        "#[cfg(not(test))]\nfn a() {}\n",
    ],
)
def test_allows_test_and_fakes(tmp_path, snippet):
    _crate(tmp_path, "sw-cli", "src/ok.rs", snippet)
    assert _run(tmp_path).returncode == 0


def test_pal_exempt(tmp_path):
    _crate(tmp_path, "sw-pal", "src/sysenv/posix.rs", "#[cfg(unix)]\nuse libc::getpid;\n")
    assert _run(tmp_path).returncode == 0


def test_build_script_and_target_section(tmp_path):
    _crate(tmp_path, "sw-proto", "build.rs", "fn main() {}\n")
    (tmp_path / "crates" / "sw-proto" / "Cargo.toml").write_text(
        "[package]\nname = \"sw-proto\"\nversion = \"0.0.0\"\n[target.'cfg(unix)'.dependencies]\nlibc = \"0.2\"\n",
        encoding="utf-8",
    )
    r = _run(tmp_path)
    assert r.returncode == 1
    assert ": build-script: " in r.stdout and ": target-section: " in r.stdout
```

`tests/test_rust_workspace_layout.py`：

```python
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

CRATES = ["sw-proto", "sw-pal", "sw-core", "sw-daemon", "sw-cli"]


@pytest.fixture(autouse=True)
def _cargo_available():
    # workspace 不存在時不 SKIP：紅燈必須是真的失敗。只有 cargo 缺席可 SKIP，
    # 且 Rust CI（Task 19）設 SW_REQUIRE_CARGO=1，此時 cargo 缺席也是 FAIL。
    if shutil.which("cargo") is None:
        if os.environ.get("SW_REQUIRE_CARGO") == "1":
            pytest.fail("SW_REQUIRE_CARGO=1 但找不到 cargo（證據不足，非 PASS）")
        pytest.skip("cargo 不存在（證據不足，非 PASS）")


def test_members_exist():
    for c in CRATES:
        assert (ROOT / "crates" / c / "Cargo.toml").is_file(), c


def test_dep_direction():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/rust/check_dep_direction.py"), "--root", str(ROOT)],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_workspace_version_matches_VERSION():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    text = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    assert f'version = "{version}"' in text


def test_toolchain_pinned():
    t = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    assert 'channel = "1.96.1"' in t and "x86_64-pc-windows-gnu" in t


def test_cargo_config_has_no_runner_or_linker():
    cfg = ROOT / ".cargo" / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    assert "runner" not in text and "linker" not in text   # §0.7：由 win-test.sh／win-runner.sh 以 env 注入
```

（`tests/test_rust_cfg_boundary.py` 模組層加 `pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]`。兩個 target 的 `cargo check --workspace --all-targets` 不放進 pytest（避免預設套件編譯兩個 target），改在 Step 5 與 Task 19 的 Rust CI job 執行。）

Step 1 同時提交 stub `scripts/rust/check_cfg_boundary.py` 與 `scripts/rust/check_dep_direction.py`（本體 `sys.exit(2)`），兩檔測試函式加 `xfail(strict=True)`（§0.5）。

- [ ] **Step 2：確認紅燈**

Run：`python3 -m pytest -q --runxfail tests/test_rust_cfg_boundary.py tests/test_rust_workspace_layout.py`
Expected：`test_detects_forbidden` 等因 stub 回傳 2 而 FAIL；`test_rust_workspace_layout.py` 因尚無 `Cargo.toml`／`crates/` 而 FAIL（不 SKIP）。不加 `--runxfail` 時兩檔為 xfail，`python3 -m pytest -q tests/` 無新失敗。

- [ ] **Step 3：commit 測試**

`git add tests/test_rust_cfg_boundary.py tests/test_rust_workspace_layout.py scripts/rust/check_cfg_boundary.py scripts/rust/check_dep_direction.py && git commit -m "test(rust): 新增 cfg 邊界與 workspace 佈局測試（紅燈）"`

- [ ] **Step 4：實作**

1. 依「Interfaces」建立 workspace 與五個 crate。`Cargo.lock` 以 `cargo generate-lockfile` 產生後 check-in。
2. 依 Task 3～16 的 Rust Interfaces「Produces」逐一建立 stub：型別完整宣告；函式本體 `todo!("P3-T<n>: <name>")`；trait 先只宣告方法；`fakes` 型別先放空結構。`sw-cli/src/main.rs`：
   ```rust
   fn main() {
       let p = sw_pal::native_client();
       let code = sw_cli::run_process(std::env::args_os().collect(), &p);
       std::process::exit(code);
   }
   ```
3. `check_cfg_boundary.py`：純 stdlib。先剝除 `//` 行註解、`/* */` 區塊註解與字串常值再比對；cfg 謂詞以括號配對取出完整內容，遞迴拆 `all(...)`／`any(...)`／`not(...)`，葉節點只能是 `test` 或 `feature = "test-fakes"`。
4. `check_dep_direction.py`：解析 `cargo metadata` 的 `resolve.nodes`，只看 workspace 成員的**直接**依賴（`deps[].name`），依上表判定。
5. `.gitignore` 加 `/target/`；`.project-policy.yml` 加 code_paths。
6. 依 §0.7 修訂 `rust-architecture.md` 與 `spec-reconciliation.md`（同一 commit，PR 描述列出逐條 diff 供審查）。
7. `crates/{sw-proto,sw-pal,sw-cli}/tests/common/mod.rs` 都提供 `pub fn data_root() -> PathBuf`：先讀**執行期** env `SW_TEST_DATA_DIR`（存在時直接用），否則用 `env!("CARGO_MANIFEST_DIR")/../..`。所有讀 repo 資料（`contracts/**`、`tests/contract_blackbox/data/**`、`sw_core/assets/skill/**`、`VERSION`）的 Rust 測試一律經 `data_root()` 組路徑，不得直接寫 `env!("CARGO_MANIFEST_DIR")`（Windows interop 執行時編譯期 WSL 路徑讀不到，Task 17 staging 會設 `SW_TEST_DATA_DIR`）。`check_cfg_boundary.py` 另加規則 `test-data-path`：`crates/*/tests/**/*.rs` 中 `CARGO_MANIFEST_DIR` 只允許出現在 `tests/common/mod.rs`。

- [ ] **Step 5：確認綠燈**

Run：`python3 -m pytest -q tests/test_rust_cfg_boundary.py tests/test_rust_workspace_layout.py && cargo check --workspace --all-targets --target x86_64-unknown-linux-gnu && CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc cargo check --workspace --all-targets --target x86_64-pc-windows-gnu`
Expected：全部 PASS；`cargo check` 對兩個 target 皆成功（stub 的 `todo!` 不影響編譯）。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(rust): 建立 Cargo workspace 骨架、P3 stub 與 cfg 邊界檢查"`（含 changelog fragment）。

---

### Task 2：黑箱契約 harness（`tests/contract_blackbox/`）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p3-02-contract-blackbox.md`（frontmatter `type: change`、`scope: rust-cli`；正文：新增可指向 Python 或 Rust artifact 的 CLI 黑箱契約測試框架，BB-proc 套件改讀受測 artifact 選擇器）
**BLOCKED-OWNER:** 無。

**Files:**
- Create: `tests/ab_env.py`
- Modify: `tests/conftest.py`（頂層 `import ab_env` 必須在任何 pop 之前；pop 清單加 `SERIALWRAP_CLI_EXE`、`SERIALWRAP_DAEMON_EXE`、`SERIALWRAP_AB_LABEL`、`SERIALWRAP_DELEGATE_DEPTH`、`SERIALWRAP_DELEGATE_ARGV_SHA`）
- Modify: `tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py`（BB-proc 套件改讀 A/B 選擇器，見 Step 4 第 8 點；本 phase 為唯一 owner）
- Create: `tests/contract_blackbox/conftest.py`、`bb_harness.py`、`bb_stubd.py`、`bb_faketarget.py`、`bb_daemon.py`、`bin/py-serialwrapd`（可執行）、`README.md`
- Test: `tests/contract_blackbox/test_bb_no_product_import.py`、`tests/contract_blackbox/test_bb_harness_selftest.py`、`tests/test_ab_env_bbproc.py`

**Interfaces:**
- Consumes：repo 根 `serialwrap`、`serialwrap.py`、`serialwrapd.py`（Python 基線 artifact）。
- Produces（Python，全部 stdlib-only，不 import `sw_core`）：

```python
# tests/ab_env.py
AB_KEYS: tuple[str, ...] = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL")
AB_ENV: dict[str, str]  # import 當下從 os.environ 擷取；之後 root conftest 才 pop

# tests/contract_blackbox/bb_harness.py
@dataclass(frozen=True)
class CliResult:
    rc: int
    stdout: bytes
    stderr: bytes
def repo_root() -> Path
def ab_env() -> dict[str, str]              # 有 tests/ab_env.py 可 import 時取 AB_ENV；以 --confcutdir 獨立執行時
                                            # bb_harness 先把 <repo>/tests 插入 sys.path 再 import，仍失敗才退回 os.environ
def ab_label() -> str                       # ab_env() 的 SERIALWRAP_AB_LABEL，預設 "python-baseline"
def is_rust() -> bool                       # ab_label().startswith("rust")；另見 conftest 的身分交叉檢查
def cli_prefix() -> list[str]               # SERIALWRAP_CLI_EXE；預設 python_baseline_prefix()；以 .py 結尾時前置 sys.executable
def python_baseline_prefix() -> list[str]   # Python 基線入口：POSIX [<repo>/serialwrap]；Windows [sys.executable, <repo>/serialwrap.py]
                                            # 所有「與 Python 基線比較」的測試一律用此函式，不得直接執行 <repo>/serialwrap（POSIX shebang launcher）
def python_baseline_interpreter() -> list[str]  # 基線所用直譯器：POSIX 為 <repo>/serialwrap shebang（#!/usr/bin/env python3）在 env PATH 下解析到的 python3；Windows [sys.executable]
def daemon_prefix() -> list[str]            # SERIALWRAP_DAEMON_EXE；預設 <repo>/tests/contract_blackbox/bin/py-serialwrapd（Windows：[sys.executable, <repo>/serialwrapd.py]）
def isolated_env(tmp: Path, **overrides: str | None) -> dict[str, str]
def run_cli(args: Sequence[str], *, env: Mapping[str, str], stdin: bytes | None = None,
            timeout_s: float = 60.0, merge_stderr: bool = False, cwd: Path | None = None) -> CliResult
def stdout_lines(r: CliResult) -> list[str]   # 依平台 newline 切行（Windows "\r\n"）
NL: bytes                                    # b"\r\n" on Windows（Q-05），否則 b"\n"

# tests/contract_blackbox/bb_stubd.py（可獨立執行：python3 bb_stubd.py --script s.json --workdir D [--tcp]）
class StubDaemon:
    def __init__(self, workdir: Path, script: Mapping[str, Any], *, transport: str = "unix") -> None
    def start(self, timeout_s: float = 10.0) -> None
    @property
    def endpoint(self) -> str              # unix：絕對路徑；tcp：tcp://127.0.0.1:<port>
    def requests(self) -> list[bytes]      # 每筆收到的原始請求行（含結尾 \n）
    def stop(self) -> None
# script 格式：{"methods": {"<method>": [<step>, ...]}, "default": [<step>]}
# step：{"reply": <obj>} | {"reply_raw": "<text>"} | {"close": true} | {"delay_s": <float>, "reply": <obj>}
#       | {"partial_then_close": "<text>"} | {"split_reply": ["<chunk1>", "<chunk2>"], "gap_s": <float>}
# 每個 method 依序消耗 step，最後一個 step 重複使用；請求行原樣寫到 <workdir>/requests.log（base64 每行一筆）

# tests/contract_blackbox/bb_faketarget.py
class FakeTarget:
    def __init__(self, workdir: Path, *, prompt: str = "root@fake:~# ", user: str = "root", password: str = "pw") -> None
    def start(self, by_id_dir: Path, name: str = "usb-FAKE_TARGET_0-if00") -> Path   # 建 PTY，於 by_id_dir 建 symlink
    def rx_log(self) -> bytes                                                     # DUT 收到的全部 bytes
    def stop(self) -> None
# 行為：開機送 "login: "；收到 user+"\r" 後送 "Password: "；收到 password+"\r" 後送 prompt；
#       收到行 L：回顯 L＋"\r\n"，L 為 "echo X" 時再送 X＋"\r\n"，最後送 prompt；收到 "\x03" 送 "^C\r\n"+prompt

# tests/contract_blackbox/bb_daemon.py
class PyDaemon:
    def __init__(self, env: Mapping[str, str], *, profile_dir: Path, socket: str, lock: Path) -> None
    def start(self, timeout_s: float = 15.0) -> None   # 以 daemon_prefix() 啟動，輪詢 socket 存在＋health.ping（以 bb_stubd 內的最小 NDJSON client 送，不經產品）
    def stop(self, timeout_s: float = 5.0) -> None     # 送 daemon.stop，逾時 SIGTERM→SIGKILL，確認行程與 socket 消失
    @property
    def pids(self) -> list[int]                        # 啟動的 daemon pid 及其子行程（/proc/<pid>/task/*/children 遞迴），start 後記錄
# 每個 PyDaemon 在 start 後把 {"pid","children","socket","lock","run_dir"} 追加到 $SW_AB_PIDLOG（未設則不寫），
# 供 ab_blackbox.sh 在 A→B 切換前逐一核對釋放（Task 18）。
```

`isolated_env(tmp)` 內容（與 `tests/conftest.py` 的 9＋5 規則一致）：先**刪除外層 env 中所有 `SERIALWRAP_` 開頭的變數**（涵蓋 P1 新增的 `SERIALWRAP_RELEASE_MANIFEST`、`SERIALWRAP_TEST_DEVICE_ALLOWLIST`、`SERIALWRAP_SOCKET_GROUP` 與日後新增者），再設 `SERIALWRAP_STATE_DIR=<tmp>/state`、`_RUN_DIR=<tmp>/run`、`_WAL_DIR=<tmp>/wal`、`_CONFIG_DIR=<tmp>/config`、`_LOG_DIR=<tmp>/blog`、`_EVENTS_DIR=<tmp>/events.d`、`_EVENTS_RUNTIME_DIR=<tmp>/events-rt`、`_BY_ID_DIR=<tmp>/by-id`、`_BY_PATH_DIR=<tmp>/by-path`；另設 `HOME=<tmp>/home`、`XDG_CONFIG_HOME=<tmp>/xdg-config`、`XDG_STATE_HOME=<tmp>/xdg-state`、刪除 `XDG_RUNTIME_DIR`、`COLUMNS=80`、`LANG=C.UTF-8`、`LC_ALL=C.UTF-8`、`PYTHONDONTWRITEBYTECODE=1`；`overrides` 值為 `None` 時刪除該 key。

`conftest.py`：
- `pytest_configure` 註冊**非平台** markers：`rust_only`（`is_rust()` 為假時 skip，原因 `rust-only contract`）、`needs_py_daemon`、`needs_delegation`（Rust 輪需要 Task 16 委派；Task 14 以 `-m "not needs_delegation"` 排除）。平台與資源 marker 一律用 P0 T14 詞彙（`posix_only`、`windows_native`、`core_portable`、`posix_pty`、`process_rpc`），不另造 `windows_only`、`cap_posix_pty`。
- fixtures：`env`（`isolated_env(tmp_path)`）、`stubd`（factory：`stubd(script, transport=...)`，teardown 自動 stop）、`py_daemon`、`faketarget`。
- `pytest_sessionfinish`：只有在 `config.getoption("confcutdir")` 解析為本目錄（獨立執行）時才斷言 `"sw_core" not in sys.modules`；完整 `pytest tests/` 時由 `test_bb_no_product_import.py` 的 AST 掃描把關（root conftest 的 autouse fixture 會 import `sw_core`，這是預期的）。
- 獨立執行命令：`python3 -m pytest -q tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider`。
- **L1 報告 plugin**（R5 審查後續）：`pytest_configure` 另以 `importlib.util.spec_from_file_location` 依路徑載入 P2 Task 13 的 `tests/resource_report_plugin.py`（`../resource_report_plugin.py`；不經 `tests` 套件 import，避免帶入 root conftest 與 `sw_core`），`has_plugin("sw_resource_report")` 為假才 register。獨立執行時 root conftest 不載入，沒有這一步 `SW_RESOURCE_REPORT` 就不會寫出，P3／P4 gate 的黑箱 L1 列永遠缺檔。測試 `test_bb_standalone_writes_l1_report`（`process_rpc`）：以上方獨立執行命令加 `-k bb_no_product_import` 與 `SW_RESOURCE_REPORT=<tmp>/r.json` 跑子程序 → 報告存在、`format=="l1-results.v1"`、`finished is True`，且 sessionfinish 的 `"sw_core" not in sys.modules` 斷言仍通過。
- **系統 config 防護**用 `@pytest.fixture(scope="session", autouse=True)` 的 `_no_system_config`（**不用** `pytest_sessionstart`：子目錄 conftest 的 `pytest_sessionstart` 在 `pytest tests/` 完整執行時不會被呼叫）：若系統設定檔（POSIX `/etc/serialwrap/config.yaml`、Windows `%ProgramData%\serialwrap\config.yaml`）存在，`pytest.fail("基礎設施錯誤：系統 config 會穿透隔離（SC-09），證據不足")`，避免受測 CLI 依系統 config 連到正式 daemon。（P1 `resolve_config_path` 尚無系統目錄的測試覆寫入口，列為跨 phase 待辦 §3 B9。）
- **受測身分交叉檢查**（session autouse fixture `_artifact_identity`）：`is_rust()` 為真時，`cli_prefix()[-1]` 指向的檔案開頭必須是 ELF（`\x7fELF`）或 PE（`MZ`）原生執行檔，且若同目錄上層有 `release-manifest.json`，其 `components.cli.implementation == "rust"`；`is_rust()` 為假時，`cli_prefix()[-1]` 必須是 `#!` 腳本或 `.py`。不符即 `pytest.fail("A/B 標籤與受測 artifact 不符")`，避免標籤打錯讓 `rust_only` 案例錯跳。

- [ ] **Step 1：寫失敗測試**

`tests/contract_blackbox/test_bb_no_product_import.py`：

```python
from __future__ import annotations

import ast
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORBIDDEN = ("sw_core", "serialwrap", "serialwrapd")


def test_no_product_import_anywhere_in_blackbox():
    offenders = []
    for py in sorted(HERE.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in FORBIDDEN:
                    offenders.append(f"{py.relative_to(HERE)}:{node.lineno}:{n}")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
                offenders.append(f"{py.relative_to(HERE)}:{node.lineno}:__import__")
    assert offenders == []
```

`tests/contract_blackbox/test_bb_harness_selftest.py`：

```python
from __future__ import annotations

import json

import pytest

from bb_harness import NL, run_cli


def test_stubd_records_wire_and_cli_prints_sorted(env, stubd):
    s = stubd({"methods": {"session.list": [{"reply": {"version": "0.0.0-test", "sessions": [], "ok": True, "b": {"z": 1, "a": 2.0}}}]}})
    r = run_cli(["--socket", s.endpoint, "session", "list"], env=env)
    assert r.rc == 0
    assert r.stdout == b'{"b":{"a":2.0,"z":1},"ok":true,"sessions":[],"version":"0.0.0-test"}' + NL
    assert s.requests() == [b'{"id":1,"method":"session.list","params":{}}\n']


def test_stubd_close_gives_empty_response(env, stubd):
    s = stubd({"default": [{"close": True}]})
    r = run_cli(["--socket", s.endpoint, "wal", "current-seq"], env=env)
    assert r.rc == 2
    assert json.loads(r.stdout) == {"ok": False, "error_code": "EMPTY_RESPONSE"}


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_faketarget_login_flow(tmp_path, faketarget):
    import os, time
    link = faketarget.start(tmp_path / "by-id")
    assert link.is_symlink()
    fd = os.open(os.path.realpath(link), os.O_RDWR | os.O_NOCTTY)
    def read_until(token: bytes, timeout: float = 3.0) -> bytes:
        buf, end = b"", time.monotonic() + timeout
        while token not in buf and time.monotonic() < end:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
        assert token in buf, buf
        return buf
    os.set_blocking(fd, False)
    read_until(b"login: ")
    os.write(fd, b"root\r"); read_until(b"Password: ")
    os.write(fd, b"pw\r"); read_until(b"root@fake:~# ")
    os.write(fd, b"echo hi\r"); out = read_until(b"hi\r\nroot@fake:~# ")
    assert b"echo hi\r\n" in out
    os.write(fd, b"\x03"); read_until(b"^C\r\nroot@fake:~# ")
    os.close(fd)
    assert faketarget.rx_log() == b"root\rpw\recho hi\r\x03"


def test_isolated_env_strips_all_serialwrap_vars(env, monkeypatch, tmp_path):
    from bb_harness import isolated_env
    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", "/x")
    monkeypatch.setenv("SERIALWRAP_SOMETHING_NEW", "1")
    e = isolated_env(tmp_path)
    assert not [k for k in e if k.startswith("SERIALWRAP_") and not k.endswith("_DIR")]
    for k in ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SERIALWRAP_DELEGATE_DEPTH"):
        assert k not in env
    assert env["COLUMNS"] == "80"
```

（`test_bb_harness_selftest.py` 模組層 `pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]`，`test_faketarget_login_flow` 以函式層 `posix_only`＋`posix_pty` 覆寫平台——因同檔混合平台，模組層改只放資源 marker、各函式自帶平台 marker，遵守 P0 T14 規則 2。）

`tests/test_ab_env_bbproc.py`（`posix_only`、`memory_only`）：

```python
import importlib.util, sys
from pathlib import Path
import pytest
import ab_env

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("mod", ["test_multiagent_e2e", "test_human_agent_coexist"])
def test_bbproc_honors_cli_and_daemon_exe(monkeypatch, mod):
    monkeypatch.setattr(ab_env, "AB_ENV", {"SERIALWRAP_CLI_EXE": "/fake/cli", "SERIALWRAP_DAEMON_EXE": "/fake/d"})
    spec = importlib.util.spec_from_file_location(f"_probe_{mod}", ROOT / "tests" / f"{mod}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.SERIALWRAP == "/fake/cli"
    assert m.SERIALWRAPD_ARGV == ["/fake/d"]

@pytest.mark.parametrize("mod", ["test_multiagent_e2e", "test_human_agent_coexist"])
def test_bbproc_defaults_to_python_baseline(monkeypatch, mod):
    monkeypatch.setattr(ab_env, "AB_ENV", {})
    spec = importlib.util.spec_from_file_location(f"_probe2_{mod}", ROOT / "tests" / f"{mod}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.SERIALWRAP == str(ROOT / "serialwrap")
```

Step 1 同時提交可 import 的 stub：`bb_harness.py`、`bb_stubd.py`、`bb_faketarget.py`、`bb_daemon.py`（公開名稱齊全，函式本體 `raise NotImplementedError("P3-T2")`，常數 `NL` 依平台給值），自測函式加 `xfail(strict=True, raises=(NotImplementedError, AssertionError))`。

- [ ] **Step 2：確認紅燈**

Run：`python3 -m pytest -q --runxfail tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider && python3 -m pytest -q --runxfail tests/test_ab_env_bbproc.py`
Expected：自測 FAIL 於 `NotImplementedError: P3-T2`（執行期，非 collection error）；`test_ab_env_bbproc.py` FAIL 於 `AttributeError: … 'SERIALWRAPD_ARGV'`／`assert '<repo>/serialwrap' == '/fake/cli'`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(blackbox): 新增黑箱 harness 自測與不得 import 產品碼檢查（紅燈）"`

- [ ] **Step 4：實作**

1. `tests/ab_env.py`：模組層 `AB_ENV = {k: os.environ[k] for k in AB_KEYS if k in os.environ}`；不 import 任何 repo 模組。
2. `tests/conftest.py`：在既有 `import liveguard` 之前加 `import ab_env  # noqa: F401  黑箱 A/B 選擇器須在 pop 前擷取`，pop 清單擴充五個 key。
3. `bb_stubd.py`：`socketserver.ThreadingMixIn` + `UnixStreamServer`／`TCPServer`；每連線逐行讀（以 `\n` 切），依 script 回覆；`reply` 以 `json.dumps(obj, ensure_ascii=False, separators=(",",":")) + "\n"` 寫回；`ready` 判定為 `<workdir>/ready` 檔案出現（內含 endpoint）。stdlib-only，Windows 上只支援 `tcp`。
4. `bb_faketarget.py`：`pty.openpty()`＋獨立 thread 狀態機；`posix_only`。
5. `bb_daemon.py`：`subprocess.Popen(daemon_prefix() + ["--profile-dir", ..., "--socket", ..., "--lock", ...], env=env, stdout=DEVNULL, stderr=<workdir>/daemon.stderr)`。
6. `bin/py-serialwrapd`：
   ```sh
   #!/bin/sh
   here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
   exec python3 "$here/../../../serialwrapd.py" "$@"
   ```
7. `README.md`：說明三個 env、獨立執行命令、`rust_only` 語意、Windows 執行入口（Task 17）。
8. `tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py`：模組常數改為
   ```python
   from ab_env import AB_ENV
   SERIALWRAP = AB_ENV.get("SERIALWRAP_CLI_EXE") or str(ROOT_DIR / "serialwrap")
   SERIALWRAPD_ARGV = [AB_ENV["SERIALWRAP_DAEMON_EXE"]] if AB_ENV.get("SERIALWRAP_DAEMON_EXE") else [sys.executable, str(ROOT_DIR / "serialwrapd.py")]
   ```
   原本以 `SERIALWRAPD` 組 daemon 命令列的地方改用 `SERIALWRAPD_ARGV`；不改任何斷言。（root conftest 已 pop `SERIALWRAP_CLI_EXE`，所以必須讀 `ab_env.AB_ENV`，不能讀 `os.environ`。）

- [ ] **Step 5：確認綠燈**

Run：`python3 -m pytest -q tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider && python3 -m pytest -q tests/test_ab_env_bbproc.py tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py && python3 -m pytest -q tests/`
Expected：黑箱自測全部 PASS（對 Python 基線）；BB-proc 兩檔對 Python 基線結果與修改前相同；完整 suite 無新失敗（含 P0 marker 稽核）。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(blackbox): 實作黑箱 harness、stub daemon、PTY 假 DUT 與 A/B env 擷取"`

---

### Task 3：`sw-proto::pyjson`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p3-03-pyjson.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-proto/src/pyjson.rs`
- Create: `scripts/rust/goldens/gen_pyjson.py`、`scripts/rust/goldens/_tsv.py`、`contracts/golden/pyjson.tsv`
- Test: `crates/sw-proto/tests/common/mod.rs`、`crates/sw-proto/tests/pyjson_golden.rs`、`tests/test_rust_goldens_fresh.py`

**Interfaces:**
- Consumes：Task 1 stub。
- Produces（arch §3.3，細化如下）：

```rust
pub enum PyValue { Null, Bool(bool), Int(PyInt), Float(f64), Str(String), List(Vec<PyValue>), Dict(PyDict) }
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PyInt { Small(i128), Big(String /* 正規化十進位，含負號 */) }
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PyDict(pub indexmap::IndexMap<String, PyValue>);
impl PyDict {
    pub fn new() -> Self;
    pub fn insert(&mut self, k: &str, v: PyValue);        // 已存在：改值、位置不變（Python dict 語意）
    pub fn get(&self, k: &str) -> Option<&PyValue>;
    pub fn setdefault(&mut self, k: &str, v: PyValue);
    pub fn update(&mut self, other: &PyDict);
    pub fn get_truthy(&self, k: &str) -> bool;            // bool(d.get(k))
}
pub enum Sep { Compact, Default, Indent(u8) }
pub struct DumpOpts { pub sep: Sep, pub ensure_ascii: bool, pub sort_keys: bool, pub allow_nan: bool }
pub const CLI_STDOUT: DumpOpts;   // Compact, ensure_ascii=false, sort_keys=true,  allow_nan=true
pub const RPC_WIRE: DumpOpts;     // Compact, ensure_ascii=false, sort_keys=false, allow_nan=true
pub const RPC_EARLY: DumpOpts;    // Default, ensure_ascii=false, sort_keys=false
pub const DISK_STABLE: DumpOpts;  // Compact, ensure_ascii=false, sort_keys=true
#[derive(Debug, Clone, PartialEq)]
pub struct JsonDecodeError { pub msg: String, pub pos: usize /* code point */, pub lineno: usize, pub colno: usize }
impl std::fmt::Display for JsonDecodeError {}             // "{msg}: line {lineno} column {colno} (char {pos})"
#[derive(Debug, Clone, PartialEq)]
pub enum JsonError { Decode(JsonDecodeError), Recursion } // Recursion：對應 Python RecursionError（不是 ValueError）
impl std::fmt::Display for JsonError {}                   // Decode → 同上；Recursion → "maximum recursion depth exceeded"
pub fn loads(s: &str) -> Result<PyValue, JsonError>;      // json.loads（strict=True）
pub fn dumps(v: &PyValue, o: &DumpOpts) -> String;         // 不含尾端換行
pub fn repr_float(x: f64) -> String;                       // Python repr(float)：非有限值為 "nan"／"inf"／"-inf"；唯一實作位置，pyfmt::py_repr_float 單向轉呼叫
// dumps 對非有限 float 另走 "NaN"／"Infinity"／"-Infinity"（json.encoder.floatstr），不經 repr_float 的非有限分支
pub const JSON_MAX_DEPTH: usize = 2900;                    // Rust 自訂、非契約（§0.7）；解析器以顯式堆疊實作，不靠遞迴
#[macro_export] macro_rules! pydict { ($($k:expr => $v:expr),* $(,)?) => { $crate::pyjson::PyDict::from_pairs(vec![$(($k.to_string(), $crate::pyjson::PyValue::from($v))),*]) } }
impl PyDict { pub fn from_pairs(v: Vec<(String, PyValue)>) -> Self; }
// From<T> for PyValue：bool、&str、String、i64、f64、PyInt、PyDict、Vec<PyValue>、Option<T>（None → Null）
```

行為依 arch §3.3 規則 1～6。補充：`msg` 文字逐字照 CPython `json.decoder`（`Expecting value`、`Expecting property name enclosed in double quotes`、`Expecting ',' delimiter`、`Expecting ':' delimiter`、`Unterminated string starting at`、`Invalid control character at`、`Invalid \\escape`、`Invalid \\uXXXX escape`、`Extra data`、`Unexpected UTF-8 BOM (decode using utf-8-sig)`）；`pos` 以 code point 計；lone surrogate 轉 U+FFFD（`RPC-H-03`，Q-21）。巢狀深度上限 `JSON_MAX_DEPTH = 2900` 為 Rust 自訂常數、**非契約**（§0.7 登記）：Python 的 `RecursionError` 門檻取決於 C 堆疊限制（2026-10 實測 Linux 3.12.3 在 n≈9998、Windows 3.12.10 在 n≈2998），無法由 golden 決定，所以 golden 只收落在兩平台、兩實作都明確同側的深度：≤ 2500 必須成功，≥ 12000 必須 `RECURSION`；介於兩者之間不做 A/B 比對。

Golden TSV 格式（`scripts/rust/goldens/_tsv.py` 共用）：每行 `<case_id>\t<op>\t<hex(input UTF-8)>\t<hex(expected UTF-8)>`；用 hex 而非 JSON，避免 golden 讀取反過來依賴受測的 `pyjson`（相對 arch §9.1 的 `.ndjson` 命名為細化）。

| op | input | expected |
|---|---|---|
| `dumps:cli_stdout`／`dumps:rpc_wire`／`dumps:rpc_early`／`dumps:disk_stable` | JSON 文字 | `json.dumps(json.loads(input), **profile)` |
| `loads_error` | JSON 文字 | `str(JSONDecodeError)`；`RecursionError` 時為 `RECURSION` |
| `float_repr` | 16 位 hex 的 IEEE-754 bits | `repr(x)` |
| `int_roundtrip` | 十進位字串 | `json.dumps(int(s))` |

- [ ] **Step 1：寫失敗測試**

`scripts/rust/goldens/gen_pyjson.py`（重點片段）：

```python
PROFILES = {
    "cli_stdout": dict(ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    "rpc_wire": dict(ensure_ascii=False, separators=(",", ":")),
    "rpc_early": dict(ensure_ascii=False),
    "disk_stable": dict(ensure_ascii=False, sort_keys=True, separators=(",", ":")),
}
CORPUS = [
    '{}', '[]', '{"b":1,"a":2}', '{"a":1,"a":2}', '{"k":"\\u00e9\\ud83d\\ude00"}', '"\\u2028/\\u007f"',
    '[1e16,1e-05,0.0001,-0.0,5.0,31.5,1e400,-1e400]', 'NaN', '[Infinity,-Infinity]',
    '123456789012345678901234567890123456789', '-0', '{"x":[true,false,null]}',
    '"\\b\\f\\n\\r\\t\\"\\\\"', '"\\u0001"', ' \t\n{"a" : 1}\r\n ',
    '"\x01"', '{"a":1,}', '[1 2]', '{"a" 1}', "{'a':1}", '"\\x"', '"\\u12"', '\ufeff{}', '{"a":1} x', '',
]
def main(out):
    rows = []
    for i, text in enumerate(CORPUS + fuzz(seed=20261015, n=3000)):
        try:
            v = json.loads(text)
        except json.JSONDecodeError as e:
            rows.append((f"j{i}", "loads_error", text, str(e))); continue
        except RecursionError:
            rows.append((f"j{i}", "loads_error", text, "RECURSION")); continue
        if has_lone_surrogate(v):
            continue                              # RPC-H-03：非契約差異，另由 Rust 單元測試驗 U+FFFD
        for name, kw in PROFILES.items():
            rows.append((f"j{i}", f"dumps:{name}", text, json.dumps(v, **kw)))
    rng = random.Random(20261015)
    for i in range(10000):
        bits = rng.getrandbits(64)
        x = struct.unpack("<d", struct.pack("<Q", bits))[0]
        rows.append((f"f{i}", "float_repr", f"{bits:016x}", repr(x)))
    for depth in (100, 500, 1000, 2000, 2500):            # 兩平台都必須成功
        text = "[" * depth + "]" * depth
        v = json.loads(text)                                # 失敗即產生器 raise，不寫入 golden
        rows.append((f"d{depth}", "dumps:rpc_wire", text, json.dumps(v, **PROFILES["rpc_wire"])))
    for depth in (12000, 20000):                            # 兩平台都必須 RecursionError
        try:
            json.loads("[" * depth + "]" * depth)
            raise SystemExit(f"depth {depth} 未觸發 RecursionError，golden 前提不成立")
        except RecursionError:
            rows.append((f"d{depth}", "loads_error", "[" * depth + "]" * depth, "RECURSION"))
    write_tsv(out, rows)
```

`_tsv.py`：

```python
def write_tsv(path: str | Path, rows: Iterable[tuple[str, str, str, str]]) -> None:
    # 每列 f"{id}\t{op}\t{text.encode('utf-8','surrogatepass').hex()}\t{expected.encode('utf-8','surrogatepass').hex()}\n"；
    # 依原順序寫出（不排序）、UTF-8、LF；case_id 不得重複（重複即 raise ValueError）；檔頭不寫註解行
def read_tsv(path) -> list[tuple[str, str, str, str]]   # 供產生器自測
```

```python
def fuzz(seed: int, n: int) -> list[str]:
    # rng = random.Random(seed)；每筆以 gen_value(rng, depth=0) 遞迴產生：depth≥6 時只出 scalar；
    # scalar 依權重抽 null/true/false/int(±2**70 內)/float(rng.uniform, 含 1e±308 邊界)/str(長度 0–8，code point 自
    # [0x20–0x7e, 0x80–0x7ff, 0x800–0xd7ff, 0xe000–0xffff, 0x10000–0x10ffff, 控制字元 0x00–0x1f] 抽)；
    # 容器 list/dict 長度 0–4；序列化用 json.dumps(v, ensure_ascii=rng.random()<0.5)，再以 10% 機率對結果
    # 隨機刪除、插入或替換一個字元（替換字元自 ' ,:[]{}"\\x0' 抽）。回傳 n 筆字串，順序固定。
def has_lone_surrogate(v) -> bool   # 遞迴檢查 str 是否含 U+D800–U+DFFF
```

（golden 中 `float_repr` 的非有限列期望為 `nan`／`inf`／`-inf`，與 `dumps` 的 `NaN`／`Infinity` 分開驗證。）

`crates/sw-proto/tests/common/mod.rs`（Task 1 已建立 `data_root()`，本 Task 追加）：

```rust
pub struct Row { pub id: String, pub op: String, pub input: String, pub expected: String }
fn unhex(s: &str) -> Vec<u8> { (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect() }
pub fn golden(name: &str) -> Vec<Row> {
    let p = data_root().join("contracts/golden").join(name).to_string_lossy().into_owned();
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{p}: {e}")).lines().map(|l| {
        let f: Vec<&str> = l.split('\t').collect();
        Row { id: f[0].into(), op: f[1].into(),
              input: String::from_utf8(unhex(f[2])).unwrap(), expected: String::from_utf8(unhex(f[3])).unwrap() }
    }).collect()
}
```

`crates/sw-proto/tests/pyjson_golden.rs`：

```rust
mod common;
use sw_proto::pyjson::{self, CLI_STDOUT, DISK_STABLE, RPC_EARLY, RPC_WIRE};

#[test]
fn golden_cases() {
    let mut fails = Vec::new();
    for r in common::golden("pyjson.tsv") {
        let got = match r.op.as_str() {
            "loads_error" => match pyjson::loads(&r.input) {
                Err(pyjson::JsonError::Decode(e)) => e.to_string(),
                Err(pyjson::JsonError::Recursion) => "RECURSION".into(),
                Ok(_) => "OK".into(),
            },
            "float_repr" => pyjson::repr_float(f64::from_bits(u64::from_str_radix(&r.input, 16).unwrap())),
            op => {
                let o = match op { "dumps:cli_stdout" => &CLI_STDOUT, "dumps:rpc_wire" => &RPC_WIRE,
                                   "dumps:rpc_early" => &RPC_EARLY, "dumps:disk_stable" => &DISK_STABLE, _ => panic!("{op}") };
                pyjson::dumps(&pyjson::loads(&r.input).expect(&r.id), o)
            }
        };
        if got != r.expected { fails.push(format!("{} {}: got {:?} want {:?}", r.id, r.op, got, r.expected)); }
    }
    assert!(fails.is_empty(), "{} 筆不符，前 20 筆：\n{}", fails.len(), fails[..fails.len().min(20)].join("\n"));
}

#[test]
fn lone_surrogate_becomes_replacement() {
    let v = pyjson::loads(r#""\ud800x""#).unwrap();
    assert_eq!(pyjson::dumps(&v, &RPC_WIRE), "\"\u{FFFD}x\"");
}

#[test]
fn duplicate_key_keeps_first_position_last_value() {
    let v = pyjson::loads(r#"{"a":1,"b":2,"a":3}"#).unwrap();
    assert_eq!(pyjson::dumps(&v, &RPC_WIRE), r#"{"a":3,"b":2}"#);
}

#[test]
fn rpc_early_exact_bytes() {
    let d = sw_proto::pydict! { "ok" => false, "error_code" => "INVALID_JSON" };
    assert_eq!(pyjson::dumps(&pyjson::PyValue::Dict(d), &RPC_EARLY), r#"{"ok": false, "error_code": "INVALID_JSON"}"#);
}

#[test]
fn non_finite_repr_vs_dumps() {
    assert_eq!(pyjson::repr_float(f64::NAN), "nan");
    assert_eq!(pyjson::repr_float(f64::NEG_INFINITY), "-inf");
    assert_eq!(pyjson::dumps(&pyjson::PyValue::Float(f64::INFINITY), &RPC_WIRE), "Infinity");
}

#[test]
fn depth_limit_is_iterative_and_bounded() {
    let deep = "[".repeat(100_000) + &"]".repeat(100_000);
    assert_eq!(pyjson::loads(&deep), Err(pyjson::JsonError::Recursion));   // 不得 stack overflow（Windows 主執行緒 1 MiB 堆疊）
}
```

`tests/test_rust_goldens_fresh.py`（之後每個 golden Task 在 `GENERATORS` 追加一列）：

```python
GENERATORS = [("gen_pyjson.py", "pyjson.tsv")]

@pytest.mark.skipif(sys.version_info[:3] != (3, 12, 3), reason="golden 以 CPython 3.12.3 為基準（CI 釘 3.12.3；其他版本為證據不足，非 PASS）")
@pytest.mark.parametrize("script, out", GENERATORS)
def test_golden_is_fresh(tmp_path, script, out):
    target = tmp_path / out
    subprocess.run([sys.executable, str(ROOT / "scripts/rust/goldens" / script), "--out", str(target)], check=True)
    assert target.read_bytes() == (ROOT / "contracts/golden" / out).read_bytes()
```

- [ ] **Step 2：確認紅燈**

Run：`python3 scripts/rust/goldens/gen_pyjson.py --out contracts/golden/pyjson.tsv && cargo test -p sw-proto --test pyjson_golden`
Expected：4 個測試 FAIL，訊息含 `not yet implemented: P3-T3`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-proto): 新增 pyjson Python golden 與相容性測試（紅燈）"`

- [ ] **Step 4：實作**

1. 解析器：手寫遞迴下降，逐字照 CPython `json/decoder.py`＋`scanner.py` 的錯誤點與訊息；`pos` 換算成 code point；行列依 `doc.count('\n', 0, pos) + 1` 與 `pos - doc.rindex('\n', 0, pos)` 規則。
2. 數字：符合 `-?(0|[1-9]\d*)` 且無 `.`／`e` → `PyInt`（超出 i128 存 `Big`）；其餘以 Rust `str::parse::<f64>()`（正確捨入）並把溢位轉 `±inf`。
3. 序列化：字串跳脫依 arch §3.3 規則 2；float 以 `pyjson::repr_float`（Task 4 的 `pyfmt::py_repr_float` 轉呼叫它）；`sort_keys` 以 `String` Ord；`Default` 分隔 `", "`／`": "`。
4. `pydict!` macro 依插入序建 `PyDict`。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-proto --test pyjson_golden && python3 -m pytest -q tests/test_rust_goldens_fresh.py`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-proto): 實作 pyjson 解析與 Python json.dumps 等價序列化"`

---

### Task 4：`sw-proto::pycompat`／`pyfmt`／`pypath`

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-04-pycompat.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-proto/src/pycompat.rs`、`crates/sw-proto/src/pyfmt.rs`、`crates/sw-proto/src/pypath.rs`
- Create: `scripts/rust/goldens/gen_pycompat.py`、`contracts/golden/pycompat.tsv`、`contracts/golden/pypath.posix.tsv`、`contracts/golden/pypath.windows.tsv`
- Test: `crates/sw-proto/tests/pycompat_golden.rs`、`tests/test_rust_goldens_fresh.py`（追加）；Modify: `crates/sw-proto/tests/common/mod.rs`（追加 helper）

**Interfaces:**
- Consumes：Task 3 `PyValue`、`PyInt`、`PyDict`。
- Produces：

```rust
// pycompat.rs
pub fn py_int_parse(s: &str) -> Option<PyInt>;        // int(str)：strip、正負號、底線、Unicode 十進位數字；拒絕 "3.5"、"0x10"
pub fn py_float_parse(s: &str) -> Option<f64>;        // float(str)：nan/inf/infinity 不分大小寫、空白、底線
pub fn py_is_space(c: char) -> bool;                   // str.isspace
pub fn py_strip(s: &str) -> &str;                      // str.strip()
pub fn py_bytes_strip(b: &[u8]) -> &[u8];              // bytes.strip()：b" \t\n\r\x0b\x0c"
pub fn py_isdigit(s: &str) -> bool;                    // str.isdigit（含上標數字）
pub fn splitlines(s: &str) -> Vec<&str>;               // str.splitlines()
pub fn universal_newlines(s: &str) -> String;          // "\r\n"、"\r" → "\n"
pub fn decode_utf8_replace(b: &[u8]) -> String;        // errors="replace"（maximal subpart 規則）
#[derive(Debug, Clone, PartialEq)]
pub struct UnicodeDecodeError { pub start: usize, pub end: usize, pub byte: u8, pub reason: &'static str }
impl std::fmt::Display for UnicodeDecodeError {}      // "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
                                                       // 多 byte："... can't decode bytes in position 3-4: invalid continuation byte"
pub fn decode_utf8_strict(b: &[u8]) -> Result<String, UnicodeDecodeError>;
pub fn py_len_chars(s: &str) -> usize;
pub fn char_index_to_byte(s: &str, ci: usize) -> usize;
pub fn byte_index_to_char(s: &str, bi: usize) -> usize;
pub fn py_truthy(v: &PyValue) -> bool;
#[derive(Debug, Clone, PartialEq)]
pub struct PyOsError { pub errno: Option<i64>, pub winerror: Option<i64>, pub strerror: Option<String>,
                       pub filename: Option<String>, pub plain: Option<String> }
impl std::fmt::Display for PyOsError {}
// 規則：plain 有值 → plain；winerror 有值 → "[WinError {w}] {strerror}"；否則 errno 有值 → "[Errno {e}] {strerror}"；
//       filename 有值 → 再接 ": " + py_repr_str(filename)
pub fn sha256_hex(data: &[u8]) -> String;

// runtime_paths.rs 的 EnvSnapshot 由本 Task 先實作（pypath::expanduser 需要）；RuntimePaths 其餘於 Task 7
#[derive(Clone, Debug, Default)]
pub struct EnvSnapshot { vars: std::collections::BTreeMap<String, String> }
impl EnvSnapshot {
    pub fn from_pairs<I: IntoIterator<Item = (String, String)>>(it: I) -> Self;
    pub fn get(&self, k: &str) -> Option<&str>;
}

// pyfmt.rs
pub fn py_repr_float(x: f64) -> String;               // 單向轉呼叫 pyjson::repr_float（實作只在 pyjson）
pub fn py_repr_str(s: &str) -> String;                // repr(str)：引號選擇、\n \r \t \\ \xNN \uNNNN \UNNNNNNNN、isprintable 規則
pub fn py_str(v: &PyValue) -> String;                 // str()：None→"None"、True→"True"、float→repr、str 原樣、list/dict→repr
pub fn py_repr(v: &PyValue) -> String;                // repr()；dict 為 {'k': v}、list 為 [a, b]

// pypath.rs
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathFlavor { Posix, Windows }
pub trait HomeLookup {
    fn current_user_home(&self) -> Option<String>;     // POSIX pwd.getpwuid(os.getuid()).pw_dir
    fn user_home(&self, user: &str) -> Option<String>; // POSIX pwd.getpwnam(user).pw_dir
}
pub fn join(f: PathFlavor, a: &str, rest: &[&str]) -> String;   // posixpath.join／ntpath.join
pub fn normpath(f: PathFlavor, p: &str) -> String;
pub fn isabs(f: PathFlavor, p: &str) -> bool;
pub fn dirname(f: PathFlavor, p: &str) -> String;
pub fn basename(f: PathFlavor, p: &str) -> String;
pub fn splitext(f: PathFlavor, p: &str) -> (String, String);
pub fn expanduser(f: PathFlavor, p: &str, env: &crate::runtime_paths::EnvSnapshot, home: &dyn HomeLookup) -> String;
// Posix：HOME（存在即用，空字串也用）→ current_user_home；"~user" → user_home，查無則原樣；結果去掉尾端 "/"（根目錄除外）
// Windows：USERPROFILE → HOMEDRIVE+HOMEPATH；"~user" 以目前 home 的上層目錄加 user 名
```

Golden（`gen_pycompat.py`；POSIX 用本機 python3，Windows 檔用 `py -3.12` 經 interop 產生，產生命令寫在檔頭註解）：

| op | 範例輸入 | 對照 |
|---|---|---|
| `int` | `" 5"`、`"+5"`、`"1_000"`、`"٤٢"`、`"3.5"`、`"0x10"`、`"_1"`、`"1__0"`、`""`、`"9"*50` | `str(int(s))` 或 `ERR` |
| `float` | `"1e3"`、`" inf "`、`"-Infinity"`、`"nan"`、`"1_0.5"`、`"0x1p3"`、`"1e400"`、`""`、`"."` | `repr(float(s))` 或 `ERR` |
| `repr_str` | 2000 個隨機 code point 字串＋`"it's"`、`'say "x"'`、`"both'\""`、`"\x7f"`、`"\u200b"`、`"é"`、`"\U0001f600"` | `repr(s)` |
| `splitlines` | 含 `\x1c-\x1e`、`\x85`、`\u2028`、`\r\n`、尾端換行 | `json.dumps(s.splitlines(), ensure_ascii=False)`（與 Rust 側 `RPC_EARLY` 同為 `ensure_ascii=False`、預設分隔） |
| `strip` | Unicode 空白組合 | `s.strip()` |
| `decode_replace`／`decode_strict` | 300 個隨機 byte 序列＋過長編碼、surrogate 編碼、截斷 | `b.decode("utf-8","replace")`／`str(UnicodeDecodeError)` |
| `oserror` | `(errno, strerror, filename)` 組合、`winerror` 組合 | `str(OSError(...))` |
| `py_str`／`py_repr` | JSON 值 | `str(v)`／`repr(v)` |
| `pypath.<fn>` | 每個函式 200 組路徑（含 `//`、`.`、`..`、磁碟代號、UNC、`~`、`~bob`） | `posixpath.*`／`ntpath.*`；expanduser 以固定 env（`HOME=/golden/home`、`USERPROFILE=C:\Users\golden`）與 fake pwd 表（目前使用者 → `/golden/pw-home`、`bob` → `/golden/bob-home`，與下方 `FakeHome` 相同） |

- [ ] **Step 1：寫失敗測試**

`crates/sw-proto/tests/pycompat_golden.rs`：

```rust
mod common;
use sw_proto::{pycompat as pc, pyfmt, pypath::{self, PathFlavor}};

struct FakeHome;
impl pypath::HomeLookup for FakeHome {
    fn current_user_home(&self) -> Option<String> { Some("/golden/pw-home".into()) }
    fn user_home(&self, u: &str) -> Option<String> { (u == "bob").then(|| "/golden/bob-home".into()) }
}

fn check(file: &str, f: impl Fn(&str, &str) -> String) {
    let fails: Vec<String> = common::golden(file).into_iter().filter_map(|r| {
        let got = f(&r.op, &r.input);
        (got != r.expected).then(|| format!("{} {} {:?}: got {:?} want {:?}", r.id, r.op, r.input, got, r.expected))
    }).collect();
    assert!(fails.is_empty(), "{} 筆不符：\n{}", fails.len(), fails[..fails.len().min(20)].join("\n"));
}

#[test]
fn pycompat_golden() {
    check("pycompat.tsv", |op, s| match op {
        "int" => pc::py_int_parse(s).map(|i| sw_proto::pyjson::dumps(&i.into(), &sw_proto::pyjson::RPC_WIRE)).unwrap_or("ERR".into()),
        "float" => pc::py_float_parse(s).map(pyfmt::py_repr_float).unwrap_or("ERR".into()),
        "repr_str" => pyfmt::py_repr_str(s),
        "strip" => pc::py_strip(s).to_string(),
        "splitlines" => sw_proto::pyjson::dumps(&pc::splitlines(s).into_iter().map(|x| x.into()).collect::<Vec<_>>().into(),
                                                &sw_proto::pyjson::RPC_EARLY),
        "decode_replace" => pc::decode_utf8_replace(&common::unhex_pub(s)),
        "decode_strict" => match pc::decode_utf8_strict(&common::unhex_pub(s)) { Ok(t) => t, Err(e) => e.to_string() },
        "py_str" => pyfmt::py_str(&sw_proto::pyjson::loads(s).unwrap()),
        "py_repr" => pyfmt::py_repr(&sw_proto::pyjson::loads(s).unwrap()),
        "oserror" => common::oserror_from_case(s).to_string(),
        other => panic!("unknown op {other}"),
    });
}

#[test]
fn pypath_posix_golden() {
    let env = sw_proto::runtime_paths::EnvSnapshot::from_pairs([("HOME".to_string(), "/golden/home".to_string())]);
    check("pypath.posix.tsv", |op, s| common::pypath_dispatch(PathFlavor::Posix, op, s, &env, &FakeHome));
}

#[test]
fn pypath_windows_golden() {
    let env = sw_proto::runtime_paths::EnvSnapshot::from_pairs([("USERPROFILE".to_string(), r"C:\Users\golden".to_string())]);
    check("pypath.windows.tsv", |op, s| common::pypath_dispatch(PathFlavor::Windows, op, s, &env, &FakeHome));
}
```

（`common` 追加 `unhex_pub`、`oserror_from_case`（case 以 `errno|winerror|strerror|filename|plain` 五欄 `\x1f` 分隔）、`pypath_dispatch`。）

```rust
#[test]
fn py_repr_float_forwards_to_pyjson() {
    for x in [1.5, 1e16, -0.0, f64::NAN, f64::INFINITY] { assert_eq!(pyfmt::py_repr_float(x), sw_proto::pyjson::repr_float(x)); }
}
```

- [ ] **Step 2：確認紅燈**

Run：`python3 scripts/rust/goldens/gen_pycompat.py --out-dir contracts/golden && cargo test -p sw-proto --test pycompat_golden`
Expected：FAIL，`not yet implemented: P3-T4`。Windows 檔：`cd /mnt/c && /mnt/c/Windows/py.exe -3.12 <wslpath -w gen_pycompat.py> --windows-only --out-dir <wslpath -w contracts/golden>`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-proto): 新增 pycompat／pyfmt／pypath golden（紅燈）"`

- [ ] **Step 4：實作**

照 CPython 3.12 `Objects/longobject.c`（`int()` 字串規則）、`Python/pystrtod.c`、`Objects/unicodeobject.c`（`isspace`、`isprintable`、`splitlines`、`repr`）、`Lib/posixpath.py`、`Lib/ntpath.py` 逐條移植；Unicode 分類表以 Rust `char` 方法加 Python 特例（`\x1c-\x1f` 屬空白、`\x85`）實作，差異以 golden 驗證；`sha256_hex` 用 `sha2`。`py_repr_float` 只轉呼叫 `pyjson::repr_float`（實作固定在 `pyjson`，不得反向呼叫，避免循環）。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-proto`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-proto): 實作 Python 內建轉型、repr、路徑與 OSError 字串語意"`

---

### Task 5：`sw-proto::pyre`（Python `re` 相容）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p3-05-pyre.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-proto/src/pyre/{mod.rs,parse.rs,translate.rs}`
- Create: `scripts/rust/goldens/gen_pyre.py`、`scripts/rust/goldens/pyre_corpus.txt`、`contracts/golden/pyre.tsv`
- Test: `crates/sw-proto/tests/pyre_golden.rs`、`tests/test_rust_goldens_fresh.py`（追加）；Modify: `crates/sw-proto/tests/common/mod.rs`（`residual_accepts`）

**Interfaces:**
- Consumes：Task 4 `py_len_chars`、`char_index_to_byte`、`byte_index_to_char`；`fancy-regex`。
- Produces（arch §3.6，P3 只做 `parse`＋`translate`）：

```rust
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PyFlags(pub u32);
impl PyFlags { pub const T: u32 = 1; pub const I: u32 = 2; pub const L: u32 = 4; pub const M: u32 = 8; pub const S: u32 = 16; pub const U: u32 = 32; pub const X: u32 = 64; pub const A: u32 = 256; }
#[derive(Debug, Clone, PartialEq)]
pub struct PyReError { pub msg: String, pub pos: Option<usize>, pub line_col: Option<(usize, usize)>, pub value_error: bool }
impl std::fmt::Display for PyReError {}   // pattern 含 "\n"：msg + " (line L, column C)"；否則 msg + " at position N"；pos 為 None：msg
#[derive(Debug, Clone, PartialEq)]
pub enum CompileError { Re(PyReError), Recursion, Overflow(String) /* Python OverflowError，例如 a{99999999999} */, Untranslatable(String) }
pub struct PyRegex { /* fancy_regex::Regex + 原 pattern */ }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PyMatch { pub start: usize, pub end: usize }   // code point 索引
pub fn compile(pat: &str, flags: PyFlags) -> Result<PyRegex, CompileError>;
impl PyRegex {
    pub fn search(&self, hay: &str) -> Option<PyMatch>;
    pub fn search_from(&self, hay: &str, pos: usize) -> Option<PyMatch>;
}
pub const BACKTRACK_LIMIT: usize = 1_000_000;
pub const PARSE_DEPTH_LIMIT: usize;     // gen_pyre.py 以二分搜尋量得 Python 開始 RecursionError 的最小深度 T（記入 golden 檔頭與 constants.json）；Rust 取 T
pub enum CompletionCheck { Valid, Invalid, Crash(String) /* Python 未攔的例外（OverflowError）→ 呼叫端 exit 1、stdout 空 */ }
pub fn completion_regex_check(v: Option<&crate::pyjson::PyValue>) -> CompletionCheck;
pub fn valid_completion_regex(v: Option<&crate::pyjson::PyValue>) -> bool;   // = matches!(completion_regex_check(v), Valid)
pub fn completion_regex_check_strict(v: Option<&crate::pyjson::PyValue>) -> CompletionCheck; // daemon 使用；Untranslatable → Invalid
pub fn valid_completion_regex_strict(v: Option<&crate::pyjson::PyValue>) -> bool; // 嚴格入口的布林包裝
// command_completion.py:11-23：None → Valid；非 Str 或空 → Invalid；UTF-8 長度 > COMPLETION_REGEX_MAX_BYTES(1024) → Invalid；
// compile 的 Re／Recursion → Invalid（Python 攔 re.error／RecursionError）；Overflow → Crash（Python 只攔 re.error、UnicodeEncodeError、
// RecursionError，OverflowError 會變 traceback；KEEP：exit 1、stdout 空）；
// Untranslatable → Valid（Python 端可接受、Rust 轉譯不出時**不在 CLI 本地拒絕**，交由 daemon 自行驗證；不 fail-closed，避免拒絕 Python 接受的輸入）；
// 可轉譯且 search("") 有結果 → Invalid
```

`Untranslatable → Valid` 僅適用 CLI 的相容入口；Rust daemon 與其他做授權／排隊前檢查的路徑必須使用 strict 入口並 fail-closed。此雙 API 裁決消除 arch §3.6 原先未區分 CLI 與 daemon 呼叫端的衝突。

超過 `BACKTRACK_LIMIT` 視為未匹配（arch §3.6）；`Untranslatable` 的構造清單寫入 `contracts/golden/pyre.residuals.txt`（殘餘差異，EVT-D-S15）。殘餘檔格式（每行一筆，`\t` 分隔）：`<pattern 的 hex>\t<構造名>\t<核可者>\t<核可紀錄（PR 留言連結或日期＋審查者）>`；**不以 case id 豁免**。比對規則：只有在 Rust `compile(pattern)` 實際回 `Untranslatable(<同一構造名>)` 時，該 pattern 的 `compile` 列（Python `ok`）與連帶的 `search`／`completion_valid` 列才可視為已登記差異；Rust 結果是其他任何值（含 `ok` 但 search 位置不同、`err:` 文字不同）一律 FAIL。另有測試檢查殘餘檔：無重複列、每列都被至少一個 golden pattern 用到（未使用即過期 → FAIL）、核可欄非空、總筆數 ≤ 20、不含 `sw_core/assets/profiles/*.yaml` 與 `UBOOT_FALLBACK_PROMPTS` 的任何 regex。殘餘檔由**審查者**（非實作者）於 Step 5 後寫入核可欄；實作者只能提出候選。

- [ ] **Step 1：寫失敗測試**

`pyre_corpus.txt`（每行一個 pattern，`\t` 後為 haystack 清單，以 `\x1f` 分隔）至少涵蓋：本 repo 出貨 profile 的全部 regex（`sw_core/assets/profiles/default.yaml`、`UBOOT_FALLBACK_PROMPTS`）、`(?m)^root@prplOS:.*# `、`(?mi)^login:\\s*$`（雙反斜線，SES-16.6-04 保留）、lookahead／lookbehind、`(?P<n>…)(?P=n)`、條件群組 `(?(1)a|b)`、possessive `a*+`、atomic `(?>a)`、`(?x)` 註解、`\Z`、`$` 與尾端 `\n`、`\s` 對 `\x1c`、`\b` 對 Unicode、字元類 `[\w-]`、非法：`(`、`)`、`[`、`a**`、`(?<=a+)`、`(?P<1>x)`、`\8`、`(?z)`、`a{2,1}`、`(?i` 、行內 flag 不在開頭 `a(?i)b`；巢狀深度：產生器以二分搜尋找出 `re.compile("("*n + ")"*n)` 開始 `RecursionError` 的最小 n＝T（在 `valid_completion_regex` 相同的呼叫深度量測），golden 只收 n ∈ {50, 100, T−20}（必須成功）與 {T+20, 2T}（必須 recursion），避開呼叫端堆疊差異造成的邊界抖動；長度邊界 1024／1025 bytes 的 completion regex；可匹配空字串的 `^`、`a*`、`(?=x)?`；`OverflowError` 類：`a{99999999999}`、`a{4294967295}`、`a{1,99999999999}`（op `compile` 期望 `overflow`）。

`gen_pyre.py` 輸出 op：`compile`（`ok`／`err:<str(re.error)>`／`recursion`／`overflow`）、`search`（`none` 或 `<start>,<end>`）、`completion_valid`（`1`／`0`／`crash`，以 `sw_core.command_completion.valid_completion_regex` 擷取，未攔例外記 `crash`）。

`crates/sw-proto/tests/pyre_golden.rs`：

```rust
mod common;
use sw_proto::pyre::{self, CompileError, PyFlags};

#[test]
fn golden_compile_search_completion() {
    let mut fails = Vec::new();
    for r in common::golden("pyre.tsv") {
        let (pat, hay) = r.input.split_once('\u{1f}').unwrap_or((&r.input, ""));
        let got = match r.op.as_str() {
            "compile" => match pyre::compile(pat, PyFlags::default()) {
                Ok(_) => "ok".to_string(),
                Err(CompileError::Re(e)) => format!("err:{e}"),
                Err(CompileError::Recursion) => "recursion".into(),
                Err(CompileError::Overflow(_)) => "overflow".into(),
                Err(CompileError::Untranslatable(_)) => "untranslatable".into(),
            },
            "search" => match pyre::compile(pat, PyFlags::default()) {
                Ok(rx) => rx.search(hay).map(|m| format!("{},{}", m.start, m.end)).unwrap_or("none".into()),
                Err(_) => "compile-failed".into(),
            },
            "completion_valid" => {
                let v = sw_proto::pyjson::PyValue::Str(pat.to_string());
                match pyre::completion_regex_check(Some(&v)) {
                    pyre::CompletionCheck::Valid => "1".into(),
                    pyre::CompletionCheck::Invalid => "0".into(),
                    pyre::CompletionCheck::Crash(_) => "crash".into(),
                }
            }
            o => panic!("{o}"),
        };
        if got != r.expected && !common::residual_accepts(pat) {   // 只在 Rust compile(pat) 回 Untranslatable(登記構造名) 時為真
            fails.push(format!("{} {} {:?}: got {got:?} want {:?}", r.id, r.op, pat, r.expected));
        }
    }
    assert!(fails.is_empty(), "{} 筆不符：\n{}", fails.len(), fails[..fails.len().min(30)].join("\n"));
}

#[test]
fn completion_none_is_valid_and_empty_invalid() {
    assert!(pyre::valid_completion_regex(None));
    assert!(!pyre::valid_completion_regex(Some(&"".into())));
    assert!(!pyre::valid_completion_regex(Some(&sw_proto::pyjson::PyValue::Int(sw_proto::pyjson::PyInt::Small(1)))));
}

#[test]
fn search_reports_code_point_offsets() {
    let rx = pyre::compile("é+x", PyFlags::default()).unwrap();
    assert_eq!(rx.search("aééx").map(|m| (m.start, m.end)), Some((1, 4)));
}
```

`common::residual_accepts(pat)` 讀 `contracts/golden/pyre.residuals.txt`，並實際呼叫 `pyre::compile(pat)` 確認回 `Untranslatable(<登記構造名>)` 才回 true。同檔另有 `residuals_file_hygiene` 測試（無重複、無過期、核可欄非空、≤ 20 筆、不含出貨 profile regex）。紅燈階段此檔為空。

- [ ] **Step 2：確認紅燈**

Run：`python3 scripts/rust/goldens/gen_pyre.py --corpus scripts/rust/goldens/pyre_corpus.txt --out contracts/golden/pyre.tsv && cargo test -p sw-proto --test pyre_golden`
Expected：FAIL，`not yet implemented: P3-T5`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-proto): 新增 Python re 相容 golden 語料（紅燈）"`

- [ ] **Step 4：實作**

1. `parse.rs`：逐條移植 CPython 3.12 `Lib/re/_parser.py`（`Tokenizer`、`_parse_sub`、`_parse`、`_escape`、`_class_escape`、`_parse_flags`、`fix_flags`），錯誤訊息與 `pos` 完全照原碼；遞迴深度以顯式計數器在 `PARSE_DEPTH_LIMIT` 回 `CompileError::Recursion`。
2. `translate.rs`：AST → fancy-regex 語法，規則照 arch §3.6；所有字面字元以 `\x{..}` 輸出避免方言差異；`IGNORECASE` 非 ASCII 的大小寫折疊照 Python `sre` 的 `_sre.unicode_tolower`／特例表；無法表達者回 `Untranslatable(<構造名>)`。
3. 搜尋：`fancy_regex::RegexBuilder::backtrack_limit(BACKTRACK_LIMIT)`；byte offset 轉 code point；`BacktrackLimitExceeded` → `None`。
4. 殘餘候選：執行 golden 後，只把「Rust `untranslatable`、Python `ok`」的 pattern 列為候選（附構造名），寫在 PR 描述；由審查者逐條決定是否寫入 `pyre.residuals.txt` 並填核可欄（§0.4 oracle 凍結：實作者不得自行寫入）。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-proto --test pyre_golden`
Expected：PASS；`pyre.residuals.txt` 只含 `Untranslatable` 類，且不含任何出貨 profile 的 regex。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-proto): 實作 Python re 語法接受集合、錯誤訊息與 fancy-regex 轉譯"`

---

### Task 6：`sw-proto::yaml11` 與 `config_types`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p3-06-yaml11-config.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-proto/src/yaml11/{mod.rs,resolve.rs}`、`crates/sw-proto/src/config_types.rs`
- Create: `scripts/rust/goldens/gen_yaml11.py`、`scripts/rust/goldens/gen_profiles.py`、`contracts/golden/yaml11.tsv`、`contracts/golden/load_profiles.tsv`、`contracts/golden/profiles_fixtures/<case>/*.yaml`（至少 25 組）
- Test: `crates/sw-proto/tests/yaml11_golden.rs`、`crates/sw-proto/tests/config_types_golden.rs`；Modify: `crates/sw-proto/tests/common/mod.rs`（`load_result_json`、`config_error_kind`）

**Interfaces:**
- Consumes：Task 3、Task 4（`py_str`、`pypath`、`PyOsError`、`UnicodeDecodeError`）；`yaml-rust2` 低階事件 API。
- Produces：

```rust
// yaml11/mod.rs
#[derive(Debug, Clone, PartialEq)]
pub enum YValue { Null, Bool(bool), Int(PyInt), Float(f64), Str(String), Timestamp { py_str: String }, Binary(Vec<u8>),
                  Seq(Vec<YValue>), Map(YMap) }
#[derive(Debug, Clone, Default, PartialEq)]
pub struct YMap(pub Vec<(YValue, YValue)>);   // 插入序；重複 key（Python 相等：1 == 1.0 == True）→ 值覆寫、位置保留
impl YMap { pub fn get_str(&self, k: &str) -> Option<&YValue>; pub fn contains_str(&self, k: &str) -> bool; }
#[derive(Debug, Clone, PartialEq)]
pub struct YamlError { pub message: String }
pub fn safe_load(text: &str) -> Result<YValue, YamlError>;   // 只取第一份文件；空文件 → Null；多文件 → YamlError（PyYAML "expected a single document"）
pub fn py_str_of(v: &YValue) -> String;                      // str(v) 語意（Timestamp 用 py_str）
pub fn py_truthy_of(v: &YValue) -> bool;
pub fn to_tagged_json(v: &YValue) -> crate::pyjson::PyValue; // golden 比對用的型別標記編碼（下表，產生器 tag() 與此逐一對應）

// config_types.rs（09 §2 逐條）
pub struct UartProfile { pub baud: PyInt, pub data_bits: PyInt, pub parity: String, pub stop_bits: PyInt, pub flow_control: String, pub xonxoff: bool }
pub struct ProfileTemplate { pub profile_name: String, pub platform: String, pub prompt_regex: String, pub login_regex: String,
    pub password_regex: String, pub post_login_cmd: String, pub ready_probe: String, pub username: Option<String>,
    pub user_env: Option<String>, pub pass_env: Option<String>, pub env_file: Option<String>, pub console_unlock_env: Option<String>,
    pub console_unlock_idle_s: f64, pub timeout_s: f64, pub quiet_window_s: f64, pub hard_timeout_s: f64, pub log_dir: Option<String>,
    pub uart: UartProfile, pub bootloader_prompts: Vec<String>, pub max_console_line_chars: Option<PyInt> }
impl ProfileTemplate { pub fn command_capable(&self) -> bool; }
pub struct SessionProfile { pub com: String, pub act_no: PyInt, pub alias: String, pub device_by_id: String,
    pub template: ProfileTemplate /* 已套用 _merge_session（config.py:243）逐 target 覆寫後的值，不是原 template */ }
// golden 比對時 common::load_result_json 依 Python dataclass SessionProfile（config.py:51）的欄位順序攤平：
// profile_name、com、act_no、alias、device_by_id、platform、prompt_regex…（template 欄位逐一展開），與 dataclasses.asdict 同形
pub struct LoadResult { pub profiles: Vec<SessionProfile>, pub templates: Vec<ProfileTemplate>, pub max_sessions: PyInt }
#[derive(Debug)]
pub enum ConfigError { Yaml(YamlError), Value(String), Os(crate::pycompat::PyOsError), Decode(crate::pycompat::UnicodeDecodeError) }
pub trait ProfileFs {
    fn flavor(&self) -> crate::pypath::PathFlavor;
    fn is_dir(&self, p: &str) -> bool;
    fn list_dir(&self, p: &str) -> Result<Vec<String>, crate::pycompat::PyOsError>;  // os.listdir（未排序）
    fn read_bytes(&self, p: &str) -> Result<Vec<u8>, crate::pycompat::PyOsError>;
    fn expanduser(&self, p: &str) -> String;
}
pub fn load_profiles(profile_dir: &str, fs: &dyn ProfileFs) -> Result<LoadResult, ConfigError>;
pub struct RuntimeConfig { pub data: YMap }
impl RuntimeConfig {
    pub fn from_text(text: Option<&str>) -> Result<RuntimeConfig, ConfigError>;  // None＝檔案不存在 → 空；非 Map → 空；YAML 錯 → Err
    pub fn mode(&self) -> Option<&YValue>;            // data.get("supervision_mode")
    pub fn socket_path(&self) -> Option<&YValue>;     // data.get("socket_path")（不驗型別，Q-18）
}
```

`yaml11` 只做讀取：implicit resolver 逐條照 PyYAML `resolver.py`（bool、int、float、null、timestamp、merge `<<`、value `=`）；quoted scalar 一律 str；支援 `!!str`、`!!int`、`!!float`、`!!bool`、`!!null`、`!!binary`、`!!timestamp`、`!!seq`、`!!map`、`!!set`、`!!omap`；其他 tag → `YamlError`（PyYAML `ConstructorError`）。`ProfileTemplate.prompt_regex` 等欄位的 `str(...)` 轉型以 `py_str_of` 計算（timestamp 欄位走 `py_str`）。`bootloader_prompts` 只收 `YValue::Str` 元素（Python `isinstance(x, str)`）。

- [ ] **Step 1：寫失敗測試**

`gen_yaml11.py`：語料至少 400 筆，涵蓋 `yes/No/ON/off/y/n`（YAML 1.1：`y`／`n` 在 PyYAML **不是** bool）、`0x1A`、`0o17`、`017`、`0b101`、`1_000`、`190:20:30`（六十進位）、`.inf`、`-.Inf`、`.NaN`、`~`、`null`、空值、`2001-12-14`、`2001-12-14t21:59:43.10-05:00`、`'quoted'`、`"esc\tx"`、block scalar `|`／`>`、anchor／alias、merge key、重複 key、`1: a` 與 `true: b` 同 map、多文件、tab 縮排錯誤、出貨 `default.yaml` 全文。expected 為 `json.dumps(tag(yaml.safe_load(t)), ensure_ascii=False, separators=(",",":"))`，錯誤時 `{"t":"error"}`（錯誤訊息非契約，G5）。`tag()` 與 `to_tagged_json` 的編碼（雙邊逐字相同，key 順序即下列順序）：

| Python 值／YValue | 編碼 |
|---|---|
| `None`／`Null` | `{"t":"null"}` |
| `bool`／`Bool` | `{"t":"bool","v":true}` |
| `int`／`Int` | `{"t":"int","v":"<十進位字串，含負號>"}` |
| `float`／`Float` | `{"t":"float","v":"<repr(x)>"}`（`nan`、`inf`、`-inf`、`-0.0` 照 repr） |
| `str`／`Str` | `{"t":"str","v":"<原字串>"}` |
| `datetime.date`／`datetime.datetime`／`Timestamp` | `{"t":"ts","v":"<str(x)>"}` |
| `bytes`／`Binary` | `{"t":"bin","v":"<hex>"}` |
| `list`／`Seq` | `{"t":"seq","v":[<元素編碼>…]}` |
| `set`（`!!set`） | `{"t":"set","v":[<元素編碼>…]}`（依插入序；Python 端以 `yaml` 事件序重建，不用 `set` 迭代序） |
| `dict`／`Map` | `{"t":"map","v":[[<key 編碼>,<value 編碼>]…]}`（插入序；重複 key 依 Python dict 語意：位置保留、值覆寫） |
| 錯誤 | `{"t":"error"}` |

（`!!omap` 在 PyYAML 產生 list of tuple → `seq` 內每個元素為 `{"t":"seq","v":[k,v]}`。）

`gen_profiles.py`：對每個 `profiles_fixtures/<case>/` 以 `HOME=/golden/home` 執行 `sw_core.config.load_profiles(<abs dir>)`，結果 `dataclasses.asdict` 後把 fixture 絕對路徑替換為 `${FIXTURE}`，再 `json.dumps(sort_keys=True)`；例外記錄型別名（`ValueError:<msg>`、`YAMLError`、`OSError`）。fixture 至少涵蓋 09 §2.2～§2.6 每一條：`_as_int` 的 bool／float／字串、`_as_max_console_line_chars` raise、`_as_unlock_idle` raise、`user_env` 三種別名、`profiles` dict 與 list 形、無 templates 時以頂層當 template、passthrough 排最後、`default_name` 不在 map、`targets` 為 None／非 list、`device_by_id` 空跳過、`act_no` 推 `COM{n-1}`、`defaults.max_sessions` 後檔覆蓋、`all_templates` 只留最後一檔（EVT-D-C2）、`log_dir` 相對路徑與 `~`、`.yml` 副檔名、非 yaml 檔忽略、目錄不存在。

`crates/sw-proto/tests/config_types_golden.rs`：

```rust
mod common;
use sw_proto::config_types::{load_profiles, ProfileFs, RuntimeConfig};
use sw_proto::{pycompat::PyOsError, pypath::PathFlavor};

struct DirFs;
impl ProfileFs for DirFs {
    fn flavor(&self) -> PathFlavor { PathFlavor::Posix }
    fn is_dir(&self, p: &str) -> bool { std::path::Path::new(p).is_dir() }
    fn list_dir(&self, p: &str) -> Result<Vec<String>, PyOsError> {
        Ok(std::fs::read_dir(p).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect())
    }
    fn read_bytes(&self, p: &str) -> Result<Vec<u8>, PyOsError> { Ok(std::fs::read(p).unwrap()) }
    fn expanduser(&self, p: &str) -> String { p.replacen('~', "/golden/home", 1) }
}

#[test]
fn load_profiles_golden() {
    let base = common::data_root().join("contracts/golden/profiles_fixtures");
    let base = std::fs::canonicalize(base).unwrap().to_string_lossy().into_owned();
    let mut fails = Vec::new();
    for r in common::golden("load_profiles.tsv") {
        let dir = format!("{base}/{}", r.input);
        let got = match load_profiles(&dir, &DirFs) {
            Ok(res) => common::load_result_json(&res).replace(&dir, "${FIXTURE}"),
            Err(e) => common::config_error_kind(&e),
        };
        if got != r.expected { fails.push(format!("{}: got {got}\nwant {}", r.input, r.expected)); }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n\n"));
}

#[test]
fn runtime_config_semantics() {
    assert!(RuntimeConfig::from_text(None).unwrap().mode().is_none());
    assert!(RuntimeConfig::from_text(Some("- a\n")).unwrap().socket_path().is_none());       // 非 dict → {}
    assert!(RuntimeConfig::from_text(Some("a: [\n")).is_err());                               // YAML 錯 → Err（_safe_runtime_config 會吞）
    let rc = RuntimeConfig::from_text(Some("socket_path: 5\nsupervision_mode: on-demand\n")).unwrap();
    assert!(matches!(rc.socket_path(), Some(sw_proto::yaml11::YValue::Int(_))));              // Q-18：不驗型別
}
```

`yaml11_golden.rs` 以 `to_tagged_json` 經 `pyjson::dumps(..., RPC_WIRE)` 比對 `yaml11.tsv`；`{"t":"error"}` 列只比對是否錯誤。

- [ ] **Step 2：確認紅燈**

Run：`python3 scripts/rust/goldens/gen_yaml11.py --out contracts/golden/yaml11.tsv && python3 scripts/rust/goldens/gen_profiles.py --out contracts/golden/load_profiles.tsv && cargo test -p sw-proto --test yaml11_golden --test config_types_golden`
Expected：FAIL，`not yet implemented: P3-T6`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-proto): 新增 PyYAML safe_load 與 load_profiles golden（紅燈）"`

- [ ] **Step 4：實作**

1. `resolve.rs`：以 `yaml_rust2::parser::Parser` 事件流（`Scalar(value, TScalarStyle, anchor, tag)`）建樹；plain scalar 依 PyYAML implicit resolver 的 regex 與首字元表判型；timestamp 依 `construct_yaml_timestamp` 規則產出 Python `str(date)`／`str(datetime)`（含 `+HH:MM` 時區與微秒補零）。
2. `config_types.rs`：09 §2.2～§2.6 的 helper 與流程逐條移植；`sorted(os.listdir(...))` 以 `String` Ord（code point）排序；YAML 讀取 `decode_utf8_strict`（PyYAML 對 str 讀檔：`open(path, encoding="utf-8")` 的錯誤以 `ConfigError::Decode`）。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-proto --test yaml11_golden --test config_types_golden`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-proto): 實作 YAML 1.1 SafeLoader 子集與 profile 載入語意"`

---

### Task 7：`sw-proto` 資料型別（constants、runtime_paths、endpoint、version、rpc_types、wal_types、release_manifest）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-07-proto-types.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-proto/src/{constants.rs,runtime_paths.rs,endpoint.rs,version.rs,rpc_types.rs,wal_types.rs,release_manifest.rs}`
- Create: `scripts/rust/goldens/gen_constants.py`、`gen_runtime_paths.py`、`gen_endpoint.py`、`gen_wal_line.py`；`contracts/golden/constants.json`、`runtime_paths.posix.tsv`、`runtime_paths.windows.tsv`、`endpoint.tsv`、`wal_line.tsv`
- Test: `crates/sw-proto/tests/{constants_sync.rs,runtime_paths_golden.rs,endpoint_golden.rs,rpc_types.rs,wal_types_golden.rs,release_manifest.rs}`；Modify: `crates/sw-proto/tests/common/mod.rs`（`env_pairs`、`paths_json`）

**Interfaces:**
- Consumes：Task 3、4、6。P1 T18 的 `contracts/release_manifest.schema.json`（`release_manifest.rs` 以 schema 的 `required` 與型別驗 fixture；缺檔 → `panic!("P1 contracts 缺少")`，不得早退）。
- Produces：

```rust
// constants.rs（數值照 arch §3.1；本 phase 用到的）
pub const DEFAULT_TCP_PORT_RAW_DEFAULT: &str = "48700";
pub const LOOPBACK_TCP_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];
pub const RETRYABLE_READONLY_METHODS: [&str; 11] = ["health.ping","health.status","session.list","session.get_state",
    "session.activity","session.console_list","session.log_status","device.list","alias.list","mcu.patterns","mcu.status"];
pub const RETRY_BACKOFF_BASE_S: f64 = 0.5;
pub const RETRY_BACKOFF_MAX_S: f64 = 5.0;
pub const TRANSIENT_ERROR_CODES: [&str; 3] = ["TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE"];
pub const PROBE_TIMEOUT_S: f64 = 1.0;
pub const PROBE_DEADLINE_S: f64 = 2.5;
pub const PROBE_METHODS: [&str; 2] = ["health.ping", "health.status"];
pub const RECV_CHUNK: usize = 65536;
pub const CLIENT_RESPONSE_MAX_BYTES: usize = 256 * 1024 * 1024;    // Rust-only 上限（§0.7：arch §5.10 由 16 MiB 修訂並登記；Python 無上限）
pub const DEFAULT_RPC_TIMEOUT_S: f64 = 5.0;
pub const LONG_RPC_TIMEOUT_FLOOR_S: f64 = 45.0;
pub const LONG_RPC_METHODS: [&str; 4] = ["session.recover","session.attach","session.self_test","session.console_attach"];
pub const ENDPOINT_PROBE_TIMEOUT_S: f64 = 0.2;
pub const SYSTEM_SOCKET: &str = "/run/serialwrap/serialwrapd.sock";
pub const COMPLETION_REGEX_MAX_BYTES: usize = 1024;
pub const CMD_WARN_BYTES: i64 = 4096;
pub const CMD_REJECT_BYTES: i64 = 16384;
pub const FILE_DEFAULT_CHUNK_SIZE: i64 = 512;
pub const FILE_ACK_MODES: [&str; 3] = ["auto", "echo", "none"];
pub const DELEGATE_DEPTH_ENV: &str = "SERIALWRAP_DELEGATE_DEPTH";
pub const DELEGATE_ARGV_SHA_ENV: &str = "SERIALWRAP_DELEGATE_ARGV_SHA";
pub const MAX_NUMERIC_LOG_LEVEL_DIGITS: usize = 4300;

// runtime_paths.rs（EnvSnapshot 已於 Task 4 實作）
#[derive(Clone, Copy, Debug)]
pub struct NativeTraits { pub default_endpoint_is_tcp: bool, pub path_flavor: crate::pypath::PathFlavor }
#[derive(Clone, Debug, PartialEq)]
pub struct RuntimePaths {
    pub config_dir: String, pub state_dir: String, pub run_dir: String, pub data_dir: String,
    pub lock_path: String, pub socket_path: String, pub default_tcp_port: PyInt, pub default_endpoint: String,
    pub profile_dir: String, pub wal_dir: String, pub log_dir: String, pub config_yaml: String,
}
#[derive(Clone, Debug, PartialEq)]
pub enum PathsError { InvalidTcpPort { raw: String } }   // Q-15
impl RuntimePaths {
    pub fn resolve(env: &EnvSnapshot, native: NativeTraits, home: &dyn crate::pypath::HomeLookup) -> Result<RuntimePaths, PathsError>;
}
pub fn bench_state_path(env: &EnvSnapshot, f: crate::pypath::PathFlavor, home: &dyn crate::pypath::HomeLookup) -> String; // cli.py:1115-1122（呼叫時讀 env）
pub fn resolve_config_path(env: &EnvSnapshot, paths: &RuntimePaths, system_config_dir: &str, f: crate::pypath::PathFlavor,
                           home: &dyn crate::pypath::HomeLookup, exists: &dyn Fn(&str) -> bool) -> String;
// SC-09，逐字對齊 P1 constants.resolve_config_path(for_write=False)（02 T15）：
// $SERIALWRAP_CONFIG strip 後非空 → expanduser 後**原樣回傳，即使檔案不存在**（呼叫端讀不到 → 空 config，不再往下找）；
// 否則 CONFIG_DIR/config.yaml 存在 → 它；否則 <system_config_dir>/config.yaml 存在 → 它；否則 CONFIG_DIR/config.yaml

// endpoint.rs（client.py:76-114 ＋ urllib.parse.urlsplit 3.12.3）
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Endpoint { Unix(String), Tcp { host: String, port: u16 } }
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EndpointError(pub String);
pub fn parse_endpoint(s: &str) -> Result<Endpoint, EndpointError>;
pub fn is_loopback_tcp(s: &str) -> bool;

// version.rs
pub fn version() -> &'static str;          // include_str!("../../../VERSION").trim()
pub fn version_line() -> String;           // "serialwrap " + version()

// rpc_types.rs
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Method { HealthPing, HealthStatus, DaemonStop, DaemonHandoffStop /* SC-11，R1-F1；P4 update／rollback 專用 */, DeviceList, DeviceRelease, DeviceAttach, SessionList, SessionGetState,
    SessionClear, SessionBind, SessionPin, SessionUnpin, SessionAttach, SessionSelfTest, SessionActivity, SessionRecover,
    SessionConsoleAttach, SessionConsoleDetach, SessionConsoleList, SessionInteractiveOpen, SessionInteractiveSend,
    SessionInteractiveStatus, SessionInteractiveClose, SessionLogStart, SessionLogStop, SessionLogStatus, AliasList, AliasSet,
    AliasAssign, AliasUnassign, CommandSubmit, CommandGet, CommandResultTail, CommandCancel, ResultTail, LogTailRaw, LogTailText,
    FilePush, FilePull, WalRange, WalReset, WalCurrentSeq, McuPatterns, McuStatus, EventRuleSet, EventRuleDelete, EventRuleList,
    EventRuleGet, EventComEnable, EventComDisable, EventComStatus, EventReset, EventReload, EventTail, Unknown(String) }
impl Method {
    pub fn as_str(&self) -> &str;
    pub fn parse(s: &str) -> Method;
    pub fn known() -> &'static [&'static str];       // 55 個，順序同 01 §8 再加 session.get_state、daemon.handoff_stop（SC-11）
    pub fn is_retryable_readonly(&self) -> bool;     // RETRYABLE_READONLY_METHODS
    pub fn uses_long_timeout(&self) -> bool;         // LONG_RPC_METHODS
}

// wal_types.rs（arch §3.8；P3 只做資料結構與序列化）
pub struct WalRecord { pub seq: u64, pub mono_ts_ns: u64, pub wall_ts: String, pub com: Option<String>, pub dir: String,
    pub source: String, pub cmd_id: Option<String>, pub len: u64, pub crc32_hex: String, pub payload_b64: String,
    pub loss_flag: bool, pub meta: PyDict, pub rotation_failed: Option<bool> }
impl WalRecord { pub fn to_line(&self) -> String; }   // DISK_STABLE；rotation_failed 為 None 時不輸出該 key
pub fn wal_recreated_meta(reason: &str, previous_identity: Option<&str>, range: Option<(u64, u64)>) -> PyDict;

// release_manifest.rs（arch §10.1）
#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseManifest { pub schema_version: i64, pub bundle_version: String, pub stage: String, pub target: String,
    pub git_revision: String, pub python_cli_delegate_entry: Vec<String>, pub raw: PyDict }
#[derive(Clone, Debug, PartialEq)]
pub struct CurrentRelease { pub bundle_version: String, pub dir: String }
#[derive(Clone, Debug, PartialEq)]
pub enum ManifestError { InvalidJson(String), Schema(String) }
pub fn parse_manifest(text: &str) -> Result<ReleaseManifest, ManifestError>;
// Schema 錯誤：schema_version != 1；components.python_cli_delegate.entry 缺、非 list、空、含非字串
pub fn parse_current_release(text: &str) -> Result<CurrentRelease, ManifestError>;
```

`RuntimePaths::resolve` 規則照 07 §0 與 01 §4.1：`_env_path`（strip 後空視為未設；非空值不 strip；`expanduser`）、`_xdg`（空字串視為未設，不 expanduser）、`RUN_DIR` 三段 fallback、`DEFAULT_TCP_PORT = int(env.get("SERIALWRAP_TCP_PORT", "48700"))`（**`os.environ.get` 預設值語意：env 存在但為空字串時也拿空字串去 `int()` → 錯**，Q-15）、`DEFAULT_ENDPOINT`（`default_endpoint_is_tcp` 時 `$SERIALWRAP_ENDPOINT or "tcp://127.0.0.1:{port}"`，否則 `$SERIALWRAP_ENDPOINT or SOCKET_PATH`）、`PROFILE_DIR`。

- [ ] **Step 1：寫失敗測試**

`gen_constants.py`：以 `ast` 從 `sw_core/constants.py`、`client.py`、`cli.py`、`arbiter.py`、`command_completion.py`、`file_transfer.py` 抽出上列名稱的實際值寫入 `constants.json`（例：`{"client.RETRYABLE_READONLY_METHODS": [...sorted], "cli.LONG_RPC_TIMEOUT_FLOOR_S": 45.0, ...}`）。

`gen_runtime_paths.py`：env 矩陣至少 40 組（每個 `SERIALWRAP_*_DIR` 的 未設／空字串／空白／`~/x`／絕對；`XDG_*` 的 未設／空字串；`SERIALWRAP_TCP_PORT` 的 未設／`""`／`" 49000 "`／`"1_0"`／`"abc"`／`"99999999"`／`"٤٨٧٠٠"`；`SERIALWRAP_ENDPOINT` 有無），每組以 `subprocess.run([sys.executable, "-I", "-c", PROBE], env=...)` 執行（PROBE 先 `sys.path.insert(0, repo)` 再 `import sw_core.constants as c`），輸出 JSON 欄位或 `ERR:ValueError`。Windows 檔以 `py -3.12` 執行同一腳本。

`crates/sw-proto/tests/runtime_paths_golden.rs`：

```rust
mod common;
use sw_proto::{pypath::PathFlavor, runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths, PathsError}};

struct H;
impl sw_proto::pypath::HomeLookup for H {
    fn current_user_home(&self) -> Option<String> { Some("/golden/pw-home".into()) }
    fn user_home(&self, _: &str) -> Option<String> { None }
}

fn run(file: &str, native: NativeTraits) {
    let mut fails = Vec::new();
    for r in common::golden(file) {
        let env = EnvSnapshot::from_pairs(common::env_pairs(&r.input));
        let got = match RuntimePaths::resolve(&env, native, &H) {
            Ok(p) => common::paths_json(&p),
            Err(PathsError::InvalidTcpPort { .. }) => "ERR:ValueError".into(),
        };
        if got != r.expected { fails.push(format!("{}: {}\n got  {got}\n want {}", r.id, r.input, r.expected)); }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}

#[test] fn posix() { run("runtime_paths.posix.tsv", NativeTraits { default_endpoint_is_tcp: false, path_flavor: PathFlavor::Posix }) }
#[test] fn windows() { run("runtime_paths.windows.tsv", NativeTraits { default_endpoint_is_tcp: true, path_flavor: PathFlavor::Windows }) }
```

`endpoint_golden.rs`：`endpoint.tsv` 由 `gen_endpoint.py` 呼叫 `sw_core.client._parse_endpoint` 產生（`ok:unix:<path>`、`ok:tcp:<host>:<port>`、`err:<str(ValueError)>`），語料至少 120 筆：`tcp://127.0.0.1:7777`、`tcp://LOCALHOST:1`（hostname 轉小寫）、`tcp://[::1]:5`、`tcp://[::1`（`Invalid IPv6 URL`）、`tcp://h:1/`、`tcp://h:`、`tcp://h`、`tcp://:5`、`tcp://h:65536`、`tcp://h:0`、`tcp://h:-1`、`tcp://h:１`（全形數字）、`tcp://u@h:5`、`tcp://h:5?x`、`tcp://h:5#f`、`TCP://h:5`、` tcp://h:5`（前置空白）、`tcp://h:5\t`、`unix:///a/b`、`unix://host/a`、`unix:relative`、`unix:///`、`unix://`、`foo://x`、`/abs`、`rel/sock`、`""`、`a://b://c`。

`rpc_types.rs`：

```rust
use sw_proto::rpc_types::Method;
#[test] fn known_has_55_unique() {
    let k = Method::known();
    assert_eq!(k.len(), 55);                                   // 54 基線＋daemon.handoff_stop（SC-11，01 x-pending SC-11-handoff）
    let set: std::collections::BTreeSet<_> = k.iter().collect();
    assert_eq!(set.len(), 55);
    assert!(!Method::DaemonHandoffStop.is_retryable_readonly() && !Method::DaemonHandoffStop.uses_long_timeout());
    for m in k { assert_eq!(Method::parse(m).as_str(), *m); }
    assert_eq!(Method::parse("x.y"), Method::Unknown("x.y".into()));
}
// 集合比較：known() 依 01 §8 順序，常數依 health→session→device 順序，兩者不必同序
fn set<'a>(it: impl IntoIterator<Item = &'a str>) -> std::collections::BTreeSet<&'a str> { it.into_iter().collect() }
#[test] fn retry_whitelist_is_exactly_11() {
    let r = set(Method::known().iter().filter(|m| Method::parse(m).is_retryable_readonly()).copied());
    assert_eq!(r, set(sw_proto::constants::RETRYABLE_READONLY_METHODS));
    assert_eq!(r.len(), 11);
    assert_eq!(set(sw_proto::constants::RETRYABLE_READONLY_METHODS).len(), sw_proto::constants::RETRYABLE_READONLY_METHODS.len()); // 常數無重複
    assert!(!Method::SessionInteractiveStatus.is_retryable_readonly());
}
#[test] fn long_timeout_is_exactly_4() {
    let l = set(Method::known().iter().filter(|m| Method::parse(m).uses_long_timeout()).copied());
    assert_eq!(l, set(sw_proto::constants::LONG_RPC_METHODS));
    assert_eq!(l.len(), 4);
}
#[test] fn known_equals_p0_rpc_schema_methods() {
    // 權威集合：P0 T8 contracts/rpc_schema.json 的方法清單＝x-methods 鍵 ∪ x-pending[*].adds_method（P1 T08 併入前後皆為 55；缺檔 → panic，不早退）
    assert_eq!(set(Method::known().iter().copied()), common::rpc_schema_methods().iter().map(String::as_str).collect());
}
```

`rpc_types.rs` 開頭加 `mod common;`。`constants_sync.rs` 逐項比對 `constants.json`；`wal_types_golden.rs` 比對 `gen_wal_line.py` 以 `sw_core.util.dumps_stable` 產生的 50 筆 WAL 行；`release_manifest.rs` 以 6 組 fixture（正常、缺 entry、entry 空 list、entry 含數字、schema_version=2、非 JSON）驗 `parse_manifest`，並驗 `version()` 等於 `env!("CARGO_PKG_VERSION")`。

- [ ] **Step 2：確認紅燈**

Run：產生 golden 後 `cargo test -p sw-proto --test constants_sync --test runtime_paths_golden --test endpoint_golden --test rpc_types --test wal_types_golden --test release_manifest`
Expected：FAIL，`not yet implemented: P3-T7`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-proto): 新增常數、路徑、endpoint、RPC 方法、WAL 行與 manifest 測試（紅燈）"`

- [ ] **Step 4：實作**

照 Interfaces 逐項實作。`parse_endpoint` 以手寫 `urlsplit` 子集移植 3.12.3 `Lib/urllib/parse.py`：`lstrip(_WHATWG_C0_CONTROL_OR_SPACE)`、移除 `\t\r\n`、scheme 合法字元判定與小寫化、`_splitnetloc`、`hostname`（小寫、去 IPv6 括號、`_check_bracketed_host`）、`port`（`isdigit() and isascii()` 否則 `Port could not be cast to integer value as '…'`；範圍 `Port out of range 0-65535`）；`invalid tcp endpoint:` 等外層訊息用 `py_repr_str`。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-proto`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-proto): 實作常數、執行期路徑、endpoint 解析、RPC 方法表、WAL 行與 manifest 解析"`

---

### Task 8：錯誤碼生成與命名空間

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-08-error-codes.md`
**BLOCKED-OWNER:** 無（但依賴 P0 的 YAML；缺少時停在紅燈）。

**Files:**
- Create: `scripts/rust/gen_error_codes.py`
- Modify: `crates/sw-proto/src/error_codes.rs`（生成物，檔頭 `// @generated by scripts/rust/gen_error_codes.py — 勿手改`）、`crates/sw-proto/src/session_last_errors.rs`（生成物）
- Test: `crates/sw-proto/tests/error_codes_sync.rs`、`tests/test_rust_error_codes_gen.py`、`crates/sw-cli/tests/emitted_codes.rs`

**Interfaces:**
- Consumes：P0 `contracts/error_codes.yaml`、`contracts/session_last_errors.yaml`；Task 6 `yaml11::safe_load`。
- Produces（arch §3.2）：

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ErrorCode { /* YAML 每個 code 一個 variant，名稱為 UPPER_SNAKE → UpperCamel */ }
pub enum Interface { Rpc, RpcFrame, CliClient, CliLocal, Rec, Proc }
pub enum Retryable { Yes, No, Conditional, FixArgs }
pub enum Status { Existing, Approved, Implemented, Verified }
pub struct ErrorMeta { pub code: &'static str, pub interfaces: &'static [Interface], pub retryable: Retryable, pub reachable: bool, pub status: Status }
impl ErrorCode {
    pub const META: &'static [ErrorMeta];
    pub fn as_str(self) -> &'static str;
    pub fn parse(s: &str) -> Option<ErrorCode>;
    pub fn meta(self) -> &'static ErrorMeta;
}
// session_last_errors.rs
pub enum SessionLastError { /* 具名值 */, ReprobeFailed(String), AttachFailed(String), BridgeDown(String) }
impl SessionLastError { pub fn as_string(&self) -> String; pub fn parse(s: &str) -> Option<SessionLastError>; }
```

`scripts/rust/gen_error_codes.py [--check]`：讀兩個 YAML，輸出兩個 `.rs`（variant 依 YAML 順序）；`--check` 時比對現有檔，不同則 exit 1 並印 diff。YAML 欄位名若與 arch §3.2 不同，以 P0 schema 為準調整生成器映射，不改 Rust 型別形狀。

- [ ] **Step 1：寫失敗測試**

`crates/sw-proto/tests/error_codes_sync.rs`：

```rust
mod common;
use sw_proto::error_codes::ErrorCode;
use sw_proto::yaml11::{safe_load, YValue};

fn yaml(name: &str) -> YValue {
    let p = common::data_root().join("contracts").join(name);
    safe_load(&std::fs::read_to_string(p).expect("P0 contracts 缺少")).unwrap()
}

#[test]
fn every_yaml_code_has_variant_and_meta_in_order() {
    let codes = common_codes(&yaml("error_codes.yaml"));          // Vec<String>，依 YAML 順序
    let meta: Vec<&str> = ErrorCode::META.iter().map(|m| m.code).collect();
    assert_eq!(meta, codes.iter().map(String::as_str).collect::<Vec<_>>());
    for c in &codes { assert_eq!(ErrorCode::parse(c).map(|e| e.as_str()), Some(c.as_str())); }
}

#[test]
fn credentials_unresolved_is_not_an_rpc_error_code_variant() {
    assert!(ErrorCode::parse("CREDENTIALS_UNRESOLVED").is_none());
    assert!(sw_proto::session_last_errors::SessionLastError::parse("CREDENTIALS_UNRESOLVED").is_some());
}

#[test]
fn cli_codes_used_by_p3_exist_with_cli_interface() {
    use sw_proto::error_codes::Interface::*;
    for c in ["INVALID_ENDPOINT","SOCKET_ERROR","TIMEOUT","EMPTY_RESPONSE","INVALID_RESPONSE","INVALID_BENCH_STATE",
              "BENCH_STATE_IO_ERROR","BENCH_ENDPOINT_NOT_REMEMBERED","INTERNAL_ERROR","INVALID_ARGS",
              "INVALID_COMPLETION_REGEX","UNKNOWN_PROFILE","SAMPLE_READ_FAILED","UNKNOWN_EVENT_CMD"] {
        let m = ErrorCode::parse(c).unwrap_or_else(|| panic!("{c} 不在 error_codes.yaml")).meta();
        assert!(m.interfaces.iter().any(|i| matches!(i, CliClient | CliLocal)), "{c}");
    }
}
```

（`common_codes` 為同檔內 helper，從 YAML 頂層 `codes` 序列取 `code` 欄位。）

`crates/sw-cli/tests/emitted_codes.rs`（型別強制＋掃描雙重把關，OD-06：會輸出的碼必須能由常數傳遞路徑抽出）：
1. 型別：`sw-cli` 組錯誤回應只有一個入口 `output::error_resp(code: ErrorCode, message: Option<&str>) -> PyDict`；`print_internal_error`、`print_endpoint_resolution_error`、`mirror_err` 的錯誤碼參數型別都是 `ErrorCode`，不收 `&str`。
2. 掃描 `crates/sw-cli/src/**/*.rs`（剝除註解後），任何符合 `"[A-Z][A-Z0-9_]{3,}"` 的字串常值即失敗——不論是否已在 YAML（已登記的碼必須寫 `ErrorCode::X`；**未登記的新碼**同樣會被抓到）。允許清單只有 env 名稱前綴 `SERIALWRAP_`、`SW_` 與 `UNKNOWN_ERROR`（Q-02 的格式化預設字）。
3. 抽出 `ErrorCode::([A-Za-z]+)` 的全部使用處，轉回 UPPER_SNAKE，輸出到 `target/emitted_codes.sw-cli.txt`，供 P0 的 OD-06 通用規則設定（「如何抽出會輸出的 code」）引用；本測試另斷言集合中每個碼在 `sw_core/assets/skill/SKILL.md` 或 `contracts/error_codes.yaml` 的 `doc`／說明欄有對應說明（以 P0 設定的文件位置為準）。
4. 負向自測（同檔，以暫存目錄假 src 樹呼叫同一掃描函式）：`"NEW_UNKNOWN_CODE"` 常值 → FAIL；`const C: &str = "SOCKET_ERROR";` 常數轉傳 → FAIL；`ErrorCode::SocketError` 使用但文件缺說明 → FAIL。

`tests/test_rust_error_codes_gen.py`：`python3 scripts/rust/gen_error_codes.py --check` exit 0；P0 YAML 不存在時 **FAIL**（訊息 `P0 contracts 未交付，證據不足`），不 SKIP。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-proto --test error_codes_sync && cargo test -p sw-cli --test emitted_codes`
Expected：FAIL（stub `META` 為空、`parse` 為 `todo!`）。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-proto): 新增錯誤碼與 last_error 命名空間同步測試（紅燈）"`

- [ ] **Step 4：實作**

撰寫 `gen_error_codes.py`（stdlib＋PyYAML），執行後 check-in 兩個生成檔；計數只來自 YAML，不寫任何常數 78。

- [ ] **Step 5：確認綠燈**

Run：`python3 scripts/rust/gen_error_codes.py --check && cargo test -p sw-proto --test error_codes_sync && cargo test -p sw-cli --test emitted_codes`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-proto): 由 contracts 生成 error_code 與 session last_error 列舉"`

---

### Task 9：`sw-pal` client 側

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p3-09-pal-client.md`
**BLOCKED-OWNER:**
- OD-08（已裁決，不再提供替代選項）：動到 PAL 的 PR 必須取得原生 Windows COM handle 證據；WSL interop 報告與記憶體 fake 都不得充數。本 phase 的證據案例固定為 Task 17 的 `win-blackbox.ps1 -ComPair COMa:COMb`（見該 Task「Windows COM 證據」）：在擁有者 Windows 自架 runner 上，以 Spike 1 結果決定的 com0com DUT-side pair 或實體 USB-UART 直連 Windows（不經 usbipd），Python daemon 以原生 serial backend 開 COMa、P2 `uart-stdio-bridge run --endpoint win-serial --com COMb` 接 DUT 模擬器，Rust CLI 執行 attach→READY、`cmd submit`、`device release`／`device attach` 並驗 `windows_com` 案例；另附同機 `uart-stdio-bridge selftest --endpoint win-serial --com-pair COMa:COMb` 的 PASS 報告（P2 Task 8）。證據放 `target/stage1a-evidence/<run_id>/windows-com/`。**OD-15 起資源已備妥**：Spike 1 基本資格已於擁有者 host 通過（pair `CNCA0=COM20`（serialwrap／Python daemon 端，即 COMa）／`CNCB0=COM21`（bridge 端，即 COMb）），本項改為可執行 gate：以 `SW_TEST_COM_PAIR=COM20:COM21`、`win-blackbox.ps1 -ComPair COM20:COM21` 執行，Python daemon 一律為 OD-11 隔離實例（三個隔離目錄明確設定）＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`，由 orchestrator 經 WSL interop 代跑，報告附 Windows build、HVCI 狀態與 pair 對應。擁有者 host 不可用時才記「證據不足」、PR 不得合併；若要改以其他證據替代，須另行變更 OD-08 裁決。
- 擁有者 Windows 自架 runner 上的原生 build／link（arch §1.1：cross check 不取代原生 build）。
- `cap_interactive_desktop__exec_or_wait_ctrl_c`：Windows 互動 console 的 Ctrl-C 轉送與 exit code `0xC000013A` 驗證，需要互動桌面。

**Files:**
- Modify: `crates/sw-pal/src/{lib.rs,traits.rs,sysenv.rs,clock.rs,fs_ops.rs,rpc_transport.rs,process.rs,select.rs}` 與各 `posix.rs`／`windows.rs`；`crates/sw-pal/src/fakes/*`
- Create: `scripts/rust/goldens/gen_oserror.py`、`contracts/golden/oserror.linux.tsv`、`contracts/golden/oserror.windows.tsv`、`contracts/golden/errno_names.linux.tsv`、`contracts/golden/errno_names.windows.tsv`
- Test: `crates/sw-pal/tests/{select.rs,rpc_client_conformance.rs,oserror_golden.rs,exec_delegate.rs,fs_read.rs,sysenv.rs}`

**Interfaces:**
- Consumes：無（`sw-pal` 不依賴 `sw-proto`）。
- Produces：

```rust
// lib.rs
pub struct ClientPlatform { pub env: Arc<dyn SysEnv>, pub fs: Arc<dyn FsRead>, pub clock: Arc<dyn Clock>,
                            pub rpc: Arc<dyn RpcConnector>, pub process: Arc<dyn ProcessCtl>, pub caps: ClientCaps }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum PalPathFlavor { Posix, Windows }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum BackendKind { Posix, Win }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum SkillPlatform { Linux, Windows }
#[derive(Clone, Copy, Debug)]
pub struct ClientCaps { pub path_flavor: PalPathFlavor, pub af_unix: bool, pub native_backend: BackendKind,
                        pub default_skill: SkillPlatform, pub text_newline: &'static str /* "\n" | "\r\n"（Q-05） */,
                        pub exec_replace: bool /* POSIX execve；Windows 等待子行程 */ }
pub fn native_client() -> ClientPlatform;

// traits.rs
pub type PalResult<T> = Result<T, PalError>;
#[derive(Debug, Clone, PartialEq)]
pub struct PalError { pub kind: PalErrorKind, pub os_code: Option<i32>, pub winerror: Option<i32>,
                      pub strerror: String, pub py_message: String /* Python str(OSError)，不含檔名 */ }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PalErrorKind { NotFound, PermissionDenied, AccessDenied, Busy, WouldBlock, TimedOut, Cancelled, Interrupted,
                        InvalidInput, Unsupported, BrokenPipe, ConnectionRefused, AddrInUse, Eof, Io }
pub trait SysEnv: Send + Sync {
    fn var(&self, k: &str) -> Option<String>;
    fn vars(&self) -> Vec<(String, String)>;              // 以 std::env::vars_os() 實作再 lossy 轉換；不得用 std::env::vars()（遇非 UTF-8 會 panic）
    fn system_config_dir(&self) -> String;               // SC-09：POSIX "/etc/serialwrap"；Windows "%ProgramData%\serialwrap"（ProgramData 缺時 "C:\ProgramData\serialwrap"）；CLI 不得自行判斷平台
    fn current_user_home(&self) -> Option<String>;
    fn user_home(&self, user: &str) -> Option<String>;
    fn current_exe(&self) -> PalResult<PathBuf>;
    fn realpath(&self, p: &Path) -> PalResult<PathBuf>;
    fn terminal_columns(&self) -> Option<u16>;            // 查 stdout fd；不讀 COLUMNS
    fn stdout_is_terminal(&self) -> bool;
    fn errno_name(&self, code: i32) -> Option<&'static str>;  // = Python errno.errorcode.get(code)
    fn pid(&self) -> u32;
}
pub trait FsRead: Send + Sync {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>>;        // open()+read；Windows 錯誤以 CRT errno 風格（_dosmaperr＋CRT strerror）
    fn exists(&self, p: &Path) -> bool;                     // os.path.exists
    fn is_dir(&self, p: &Path) -> bool;                     // os.path.isdir
    fn list_dir(&self, p: &Path) -> PalResult<Vec<String>>; // os.listdir；Windows 錯誤為 WinError 風格
}
pub trait Clock: Send + Sync { fn mono_ns(&self) -> u64; fn sleep(&self, d: Duration); }
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EndpointSpec { Unix(OsString), Tcp { host: String, port: u16 } }
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum SockTimeout { Blocking(Duration), NonBlocking }
pub trait RpcConnector: Send + Sync {
    fn af_unix_available(&self) -> bool;
    fn connect(&self, ep: &EndpointSpec, t: SockTimeout) -> PalResult<Box<dyn RpcStream>>;
    /// 只做 getaddrinfo（Q-10：TCP 位址解析早於 timeout 驗證）；失敗的 PalError 與 connect 內的 gaierror 相同
    fn resolve_tcp(&self, host: &str, port: u16) -> PalResult<()>;
}
pub trait RpcStream: Send {
    fn send_all(&mut self, data: &[u8]) -> PalResult<()>;   // sendall；Blocking(d) 為整體上限
    fn recv(&mut self, buf: &mut [u8]) -> PalResult<usize>; // 每次呼叫各自套 timeout；0 = EOF
}
pub trait ProcessCtl: Send + Sync {
    /// 成功時不返回：POSIX execve 取代行程；Windows 等子行程結束後 std::process::exit(child_code)。只在失敗時返回錯誤。
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError;
}

// select.rs（platform_backends._select）
pub fn select_backend(raw: Option<&str>, native: BackendKind) -> Result<BackendKind, String /* "unsupported backend: 'x'" */>;

// fakes（feature "test-fakes"）
pub struct FakeEnv { pub vars: Mutex<BTreeMap<String, String>>, pub exe: PathBuf, pub columns: Option<u16>, pub tty: bool,
                     pub system_config_dir: String /* 預設 "/etc/serialwrap"；測試可改指暫存目錄 */ }
pub struct FakeClock { pub now_ns: AtomicU64, pub sleeps: Mutex<Vec<Duration>> }   // sleep 推進 now_ns 並記錄
impl FakeClock { pub fn advance(&self, d: Duration); pub fn sleeps_s(&self) -> Vec<f64>; }
pub struct MemFs { pub files: Mutex<BTreeMap<PathBuf, Result<Vec<u8>, PalError>>>, pub dirs: Mutex<BTreeSet<PathBuf>> }
#[derive(Clone, Debug)]
pub enum StreamStep { Reply(Vec<u8>) /* 一次 recv 回傳整段 */, Chunk(Vec<u8>), Eof, RecvErr(PalError), Delay(Duration) /* 推進 FakeClock */ }
#[derive(Clone, Debug)]
pub enum FakeConn { Refuse(PalError), Timeout /* connect 回 kind=TimedOut */, Stream(Vec<StreamStep>) }
impl FakeConn {
    pub fn refuse_errno(code: i32, py_message: &str) -> FakeConn;   // kind 依 code 映射（111→ConnectionRefused、110→TimedOut…）
    pub fn stream(steps: Vec<StreamStep>) -> FakeConn;
}
pub struct ScriptedConnector { pub script: Mutex<VecDeque<FakeConn>>, pub sent: Mutex<Vec<Vec<u8>>>,
                               pub timeouts: Mutex<Vec<SockTimeout>>, pub endpoints: Mutex<Vec<EndpointSpec>>,
                               pub clock: Arc<FakeClock>, pub af_unix: bool }
impl ScriptedConnector { pub fn sent(&self) -> Vec<Vec<u8>>; pub fn connect_count(&self) -> usize; pub fn timeouts_s(&self) -> Vec<f64>;
                         pub fn resolve_count(&self) -> usize; pub fn fail_resolve(&self, host: &str, e: PalError); }
// FakeConn::gai_error(code, py_message) -> PalError：kind=Io、os_code=Some(code)、py_message 原樣；resolve_tcp 預設成功
pub fn non_utf8_os_string(raw: &[u8]) -> OsString;   // POSIX：OsStr::from_bytes；Windows：每個 byte b 映射為未配對 surrogate 0xDC00|b（測試專用）
// timeouts_s：Blocking(d) → d 秒；NonBlocking → 0.0
pub struct RecordingProcess { pub calls: Mutex<Vec<(Vec<OsString>, Vec<(String, String)>)>> }  // exec_or_wait 記錄後回傳 kind=Unsupported
pub fn fake_platform(script: Vec<FakeConn>) -> (ClientPlatform, Arc<ScriptedConnector>, Arc<FakeClock>);
// caps：Posix、af_unix=true、text_newline="\n"；env 為空的 FakeEnv；fs 為空的 MemFs；process 為 RecordingProcess
```

原生實作要點（`posix.rs`／`windows.rs`）：
- `rpc_transport/posix.rs`：Unix：`socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC)`；`sun_path` 長度超限 → `PalError{kind: InvalidInput, os_code: None, py_message: "AF_UNIX path too long"}`（Q-12）；`Blocking(d)`：非阻塞 connect＋`poll` 等待 d，逾時 → `TimedOut`；`NonBlocking`：connect 直接回報 `EINPROGRESS`／`EAGAIN`，`recv` 回 `EAGAIN`（Q-10）。TCP：`getaddrinfo(host, port, AF_UNSPEC, SOCK_STREAM)`，失敗 → `py_message = "[Errno {eai}] {gai_strerror}"`、`os_code = Some(eai)`；逐一嘗試，回報最後一個錯誤（Q-13）；`errno == ETIMEDOUT` → `kind = TimedOut`（Q-11）。`py_message = format!("[Errno {e}] {strerror(e)}")`。
- `rpc_transport/windows.rs`：`af_unix_available() = false`；TCP 用 Winsock `getaddrinfo`＋`WSAConnect` 非阻塞＋`select`；錯誤 `py_message = "[WinError {w}] {FormatMessageW 去尾端空白與 '.'}"`、`os_code = Some(w)`（WSA 碼即 Python `errno`）；gaierror `[Errno 11001] getaddrinfo failed`；`WSAETIMEDOUT` → `TimedOut`。
- `process/posix.rs`：`execve(argv[0], argv, environ + env_add)`；失敗回 `PalError`。
- `process/windows.rs`：`CreateProcessW(CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, bInheritHandles=TRUE)`，命令列以 MSVC 規則 quote；建立 Job：`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`（孫行程不入 Job，避免 Python `daemon start` 背景 spawn 的 daemon 在 CLI 結束時被殺）；`AssignProcessToJobObject` → `SetConsoleCtrlHandler(NULL, TRUE)`（在建立子行程**之後**設定，避免旗標被子行程繼承）→ `ResumeThread` → `WaitForSingleObject(INFINITE)` → `GetExitCodeProcess` → `std::process::exit(code as i32)`。
- `sysenv`：`errno_name` 表由 `errno_names.<os>.tsv` golden 驗證（Python `errno.errorcode` 的完整映射，含同值別名的勝出名稱）。`text_newline`：POSIX `"\n"`，Windows `"\r\n"`。`default_skill`：POSIX `Linux`，Windows `Windows`。

- [ ] **Step 1：寫失敗測試**

`gen_oserror.py`（Linux 以 python3、Windows 以 `py -3.12` 各跑一次）在暫存目錄實際執行下列操作並記錄 `str(exc)`、`exc.errno`、`errno.errorcode.get(exc.errno)`、例外類別：

| case id | 操作 |
|---|---|
| `unix_enoent` | connect `<tmp>/nope.sock` |
| `unix_refused` | connect 到已 bind 但未 listen 的 socket 檔 |
| `unix_not_socket` | connect 到一般檔案 |
| `unix_eacces` | connect 到權限 000 目錄內的 socket |
| `unix_too_long` | connect 到長度 200 的路徑 |
| `unix_len_107`／`unix_len_108` | connect 到 `sun_path` 長度恰 107、108 bytes 的不存在路徑（Linux：107 → `ENOENT`，108 → `AF_UNIX path too long`） |
| `unix_empty` | connect `""` |
| `tcp_refused` | `create_connection(("127.0.0.1", <closed port>), 1.0)` |
| `tcp_localhost_refused` | `create_connection(("localhost", <closed port>), 1.0)` |
| `tcp_gai` | `create_connection(("no-such-host.invalid", 1), 1.0)` |
| `tcp_timeout0` | `create_connection(("127.0.0.1", <closed port>), timeout=0)` |
| `unix_recv_nonblock` | `settimeout(0)` 後對不回應的 listener `recv` |
| `file_enoent`／`file_isdir`／`file_eacces` | `open(path, "r", encoding="utf-8")` |
| `listdir_enoent` | `os.listdir(path)` |

`crates/sw-pal/tests/rpc_client_conformance.rs`（重點）：

```rust
use std::{ffi::OsString, time::Duration};
use sw_pal::traits::{EndpointSpec, PalErrorKind, SockTimeout};

fn pal() -> sw_pal::ClientPlatform { sw_pal::native_client() }

#[test]
fn unix_enoent_matches_python_message() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("nope.sock");
    let e = pal().rpc.connect(&EndpointSpec::Unix(p.into_os_string()), SockTimeout::Blocking(Duration::from_secs(1))).err().unwrap();
    let g = golden_case("unix_enoent");
    assert_eq!((e.py_message.as_str(), e.os_code), (g.message.as_str(), g.errno));
}

#[test]
fn unix_path_too_long_has_no_errno() {
    let p = OsString::from(format!("/tmp/{}", "x".repeat(200)));
    let e = pal().rpc.connect(&EndpointSpec::Unix(p), SockTimeout::Blocking(Duration::from_secs(1))).err().unwrap();
    assert_eq!(e.py_message, "AF_UNIX path too long");
    assert_eq!(e.os_code, None);
}

#[test]
fn recv_times_out_per_call() {
    let (ep, _keep) = silent_listener();                 // accept 後不回應
    let mut s = pal().rpc.connect(&ep, SockTimeout::Blocking(Duration::from_millis(200))).unwrap();
    s.send_all(b"{}\n").unwrap();
    let t0 = std::time::Instant::now();
    let e = s.recv(&mut [0u8; 16]).err().unwrap();
    assert_eq!(e.kind, PalErrorKind::TimedOut);
    assert!(t0.elapsed() >= Duration::from_millis(180) && t0.elapsed() < Duration::from_secs(2));
}

#[test]
fn tcp_refused_and_gai_and_timeout0_match_python() {
    for id in ["tcp_refused", "tcp_localhost_refused", "tcp_gai", "tcp_timeout0"] {
        let g = golden_case(id);
        let e = pal().rpc.connect(&g.endpoint(), g.timeout()).err().unwrap_or_else(|| panic!("{id} 應失敗"));
        assert_eq!((e.py_message.as_str(), e.os_code), (g.message.as_str(), g.errno), "{id}");
    }
}
```

（`golden_case`、`silent_listener` 在同檔以 `#[cfg(unix)]`／`#[cfg(windows)]` 選擇對應 golden 檔與 listener 型別——sw-pal 允許平台 cfg；golden 檔一律經 `common::data_root()` 讀取。另加 `unix_len_107_vs_108`：107 bytes 回 `ENOENT` 的 `py_message`、108 bytes 回 `AF_UNIX path too long` 且 `os_code == None`，兩者都對照 golden；`resolve_tcp_gai_matches_python`：`resolve_tcp("no-such-host.invalid", 1)` 的錯誤等於 `tcp_gai` golden。）

`crates/sw-pal/tests/exec_delegate.rs`：

```rust
// 父測試以自身測試執行檔重新啟動子測試，子測試呼叫 exec_or_wait，父測試斷言 exit code 與 env。
#[test]
fn exec_or_wait_propagates_exit_code_and_env() {
    if std::env::var_os("SW_PAL_EXEC_CHILD").is_some() {
        let argv = exit_with_env_argv();   // POSIX ["/bin/sh","-c","[ \"$SW_X\" = ok ] && exit 37 || exit 1"]；Windows ["cmd.exe","/d","/c","if \"%SW_X%\"==\"ok\" (exit 37) else (exit 1)"]
        let e = sw_pal::native_client().process.exec_or_wait(&argv, &[("SW_X".into(), "ok".into())]);
        panic!("exec_or_wait 返回錯誤：{e:?}");
    }
    let st = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "exec_or_wait_propagates_exit_code_and_env", "--nocapture"])
        .env("SW_PAL_EXEC_CHILD", "1").status().unwrap();
    assert_eq!(st.code(), Some(37));
}

#[test]
fn exec_or_wait_reports_missing_program() {
    let e = sw_pal::native_client().process.exec_or_wait(&["/definitely/not/here".into()], &[]);
    assert_eq!(e.kind, sw_pal::traits::PalErrorKind::NotFound);
}

#[test]
#[ignore = "cap:interactive_desktop"]
fn cap_interactive_desktop__exec_or_wait_ctrl_c() {
    // 非 Windows 互動 console 時明確失敗（證據不足），不得空跑成 PASS
    let probe = sw_pal::native_client();
    assert!(probe.caps.path_flavor == sw_pal::PalPathFlavor::Windows && interactive_console_attached(),
            "cap:interactive_desktop 需要 Windows 互動 console（證據不足，非 PASS）");
    if std::env::var_os("SW_PAL_CTRLC_CHILD").is_some() {
        // 子：委派到一個會等待 30 s 的程式；Ctrl-C 應終止孫行程並由 exec_or_wait 照傳 exit code
        let e = probe.process.exec_or_wait(&["cmd.exe".into(), "/d".into(), "/c".into(), "ping -n 30 127.0.0.1 >NUL".into()], &[]);
        panic!("exec_or_wait 返回錯誤：{e:?}");
    }
    for event in [CTRL_C_EVENT, CTRL_BREAK_EVENT] {
        let mut child = spawn_self_in_new_console_group("cap_interactive_desktop__exec_or_wait_ctrl_c", "SW_PAL_CTRLC_CHILD");
        std::thread::sleep(std::time::Duration::from_secs(2));
        send_console_ctrl(event, child.id());                 // GenerateConsoleCtrlEvent(event, <child 的 process group>)
        let st = wait_with_timeout(&mut child, std::time::Duration::from_secs(10)).expect("10 s 內未結束");
        assert_eq!(st.code().map(|c| c as u32), Some(0xC000_013A), "event={event}");   // STATUS_CONTROL_C_EXIT 原樣照傳
    }
}
```

（`interactive_console_attached`、`spawn_self_in_new_console_group`（`CREATE_NEW_PROCESS_GROUP`）、`send_console_ctrl`、`wait_with_timeout`、`CTRL_*_EVENT` 為同檔 `#[cfg(windows)]` helper；sw-pal 允許平台 cfg。）

`select.rs`：

| 輸入 `raw` | native | 期望 |
|---|---|---|
| `None` | Posix | `Ok(Posix)` |
| `Some("auto")` | Win | `Ok(Win)` |
| `Some("POSIX")`、`Some("unix")`、`Some("termios")` | Win | `Ok(Posix)` |
| `Some("Win32")`、`Some("windows")`、`Some("win")` | Posix | `Ok(Win)` |
| `Some("")` | Posix | `Ok(Posix)`（`backend or os.environ.get(...) or "auto"`：空字串 falsy） |
| `Some("bsd")` | Posix | `Err("unsupported backend: 'bsd'")` |

`oserror_golden.rs`：`file_*`、`listdir_enoent` 以 `FsRead` 重現並比對 `strerror`／`os_code`（檔名部分由 `sw-proto::PyOsError` 組裝，於 Task 12 驗）；`errno_names` 逐筆比對 `SysEnv::errno_name`。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-pal --features test-fakes`
Expected：FAIL，`not yet implemented: P3-T9`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-pal): 新增 client 側 RPC transport、OSError 訊息與 exec 委派測試（紅燈）"`

- [ ] **Step 4：實作**

依「原生實作要點」與 Interfaces 完成；`fakes` 完整實作（後續 Task 12、13 依賴）。所有 `unsafe` 區塊附 `// SAFETY:` 註解。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-pal --features test-fakes`；Windows 交叉執行（`scripts/rust/win-test.sh --package sw-pal`，runner 與 linker 由該腳本以 env 注入，§0.7）在 Task 17 交付腳本後補跑，Task 9 階段記錄為「待補」，不標 PASS
Expected：Linux PASS；Windows 交叉執行於 Task 17 Step 5 補跑並 PASS（`cap_*` 為 ignored，列入報告「證據不足」）。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-pal): 實作 sysenv、檔案讀取、UDS／TCP RPC client 與委派 exec"`

---

### Task 10：argparse 相容層、`cli_spec` 與委派路由

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p3-10-argparse.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-cli/src/argparse/{mod.rs,parser.rs,help.rs,usage.rs,error.rs}`、`crates/sw-cli/src/cli_spec.rs`、`crates/sw-cli/src/delegate.rs`（只含 `DELEGATED_GROUPS`、`Route`、`classify`；其餘委派執行留給 Task 16）
- Create: `scripts/rust/capture_cli_help.py`、`scripts/rust/goldens/gen_argparse_synth.py`、`contracts/cli_help/linux/*.json`、`contracts/cli_help/windows/*.json`、`contracts/cli_help/_meta.json`、`contracts/golden/argparse_synth.tsv`
- Test: `crates/sw-cli/tests/argparse_golden.rs`、`crates/sw-cli/tests/argparse_synth.rs`、`crates/sw-cli/tests/cli_spec_schema.rs`、`crates/sw-cli/tests/delegate_route.rs`（`classify_table`）、`tests/contract_blackbox/test_bb_cli_help.py`

**Interfaces:**
- Consumes：Task 4（`py_int_parse`、`py_float_parse`、`py_repr_str`、`py_len_chars`）；Task 7（`version_line`）；P0 `contracts/cli_schema.json`（一致性測試）。
- Produces：

```rust
// argparse/mod.rs
pub enum Nargs { One, ZeroOrMore }
pub enum ActionKind { Store, StoreTrue, Count, Append, Version, Help }
pub enum Conv { Str, Int, Float }
#[derive(Clone, Debug, PartialEq)]
pub enum ArgValue { None, Str(String), Int(sw_proto::pyjson::PyInt), Float(f64), Bool(bool), Count(i64), List(Vec<String>) }
pub struct ActionSpec { pub option_strings: Vec<&'static str>, pub dest: &'static str, pub kind: ActionKind, pub nargs: Nargs,
    pub conv: Conv, pub default: ArgValue, pub choices: Option<Vec<&'static str>>, pub required: bool,
    pub help: Option<String> /* %(default)s 已展開 */, pub metavar: Option<&'static str>, pub version: Option<String> }
pub struct MutexSpec { pub required: bool, pub dests: Vec<&'static str> }
pub struct SubparserEntry { pub name: &'static str, pub help: Option<&'static str>, pub target: SubTarget }
pub enum SubTarget { Native(Box<ParserSpec>), Delegated }
pub struct SubparsersSpec { pub dest: &'static str, pub required: bool, pub title: Option<&'static str>,
                            pub metavar: Option<&'static str>, pub entries: Vec<SubparserEntry> }
pub struct ParserSpec { pub prog: String, pub description: Option<&'static str>, pub epilog: Option<&'static str>,
    pub raw_text: bool, pub add_help: bool, pub allow_abbrev: bool, pub actions: Vec<ActionSpec>,
    pub mutex: Vec<MutexSpec>, pub sub: Option<SubparsersSpec> }
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Namespace { pub vals: indexmap::IndexMap<String, ArgValue> }
impl Namespace {
    pub fn get(&self, dest: &str) -> &ArgValue;
    pub fn str(&self, dest: &str) -> Option<&str>;
    pub fn path(&self) -> Vec<&str>;          // ["session","list"]：cmd → <group>_cmd 的值
}
pub struct ExitRequest { pub code: i32, pub stdout: String, pub stderr: String }
pub enum ParseResult { Ok(Namespace), Exit(ExitRequest), Delegated }
pub fn parse_args(spec: &ParserSpec, argv: &[String], width: usize) -> ParseResult;
pub fn format_help(spec: &ParserSpec, width: usize) -> String;
pub fn format_usage(spec: &ParserSpec, width: usize) -> String;
pub fn help_width(columns_env: Option<&str>, terminal: Option<u16>) -> usize;
// shutil.get_terminal_size：int(COLUMNS) > 0 才採，否則 terminal，否則 80；再減 2（HelpFormatter）

// cli_spec.rs
pub struct SpecDefaults { pub version_line: String, pub profile_dir: String }
pub const GROUP_ORDER: [&str; 20] = ["daemon","device","session","alias","cmd","stream","log","file","wal","mcu",
    "remote","connect","benches","event","supervision-mode","service","setup","doctor","profile","skill"];
pub fn root_spec(d: &SpecDefaults) -> ParserSpec;   // 20 群組；DELEGATED_GROUPS 為 SubTarget::Delegated

// delegate.rs（本 Task 只交付路由；Task 14 的 run 第 2 步就要呼叫 classify，所以不能等到 Task 16）
pub const DELEGATED_GROUPS: [&str; 8] = ["daemon","remote","connect","benches","supervision-mode","service","setup","doctor"];
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route { Native, Delegate { group: &'static str } }
pub fn classify(argv_tail: &[std::ffi::OsString]) -> Route;
// 在任何有損 UTF-8 轉換之前執行：逐 token 以 to_str() 比對選項與群組名；非 UTF-8 token 不可能等於任何選項或群組名，
// 視為一般 positional（若它是第一個非選項 token → Native，由原生路徑依 Q-19 處理）。規則見 Task 16「classify」段（原文保留在該處）。
```

`crates/sw-cli/tests/delegate_route.rs`（本 Task 建立；Task 16 追加 `recursion_rules`）：`classify_table` 照 Task 16 段落所列的表（`v(s)` 改為產生 `Vec<OsString>`），另加 `non_utf8_tokens`：`[b"--socket", b"\xff", b"daemon"]` → `Delegate{daemon}`；`[b"\xff", b"daemon"]` → `Native`；`[b"setup", b"--user", b"\xff"]` → `Delegate{setup}`（非 UTF-8 token 由 `sw_pal::fakes::non_utf8_os_string(&[0xff])` 建立：fakes 位於 sw-pal，可用 `std::os::*`（POSIX 原始 byte、Windows 未配對 surrogate）；sw-cli 測試不得直接用 `std::os::*`）。

`cli_spec.rs` 以宣告式表格逐字轉寫 `cli.py:1908-2452` 中 12 個原生群組的所有 `add_parser`／`add_argument`（help、description、epilog、dest、type、default、choices、required、metavar、mutually exclusive）；委派群組只轉寫 root 層的 `help`。`root_spec` 的 `--version` 字串為 `d.version_line`；`profile test --profile-dir` 的 default 與 help 用 `d.profile_dir`（Q-09、01 §6.20）。

argparse 引擎逐條移植 CPython 3.12 `Lib/argparse.py`：`_parse_known_args`（option string 索引、`_parse_optional` 的前綴／`=`／單破折號規則、`_get_option_tuples` 縮寫比對與歧義錯誤、負數啟發式 `_negative_number_matcher`／`_has_negative_number_optionals`、`--`、`consume_optional`、`consume_positionals` 的 nargs pattern、required 與 mutually exclusive 檢查、`_get_values`、`_check_value`）、`_SubParsersAction.__call__`（未知子命令錯誤 `argument <group>: invalid choice: …`、extras 累積後由 root 報 `unrecognized arguments`）、`HelpFormatter`／`RawTextHelpFormatter`（`max_help_position=24`、寬度、`options:` 標題、group 標題、usage 斷行演算法、code point 計長）、`error()` 輸出 `usage: …\n{prog}: error: {msg}\n` 與 exit 2、`print_help` 到 stdout、version action 到 stdout。訊息模板（`invalid %(type)s value: %(value)r`、`expected one argument`、`the following arguments are required: %s`、`one of the arguments %s is required`、`not allowed with argument %s`、`ignored explicit argument %r`、`unrecognized arguments: %s`、`ambiguous option: %(option)s could match %(matches)s`）逐字照 3.12。

- [ ] **Step 1：寫失敗測試**

**argparse 方言固定為 CPython 3.12.3**（§0.7、arch §7.2 修訂）：3.12.x 之間 `invalid choice` 的引號（3.12.3 `(choose from 'auto', 'echo', 'none')`、3.12.10 `(choose from auto, echo, none)`）與 `--help` 子命令欄寬不同，同一 Rust 引擎只能對齊一種。`_meta.json` 記 `python_version: "3.12.3"`；擷取腳本開頭斷言 `sys.version_info[:3] == (3, 12, 3)`，不符即 exit 2。

`scripts/rust/capture_cli_help.py`：以 subprocess 執行 Python CLI（`python_baseline_prefix()` 等價入口：POSIX `<repo>/serialwrap`，且該 launcher 所用直譯器必須是 3.12.3），固定 env：`COLUMNS∈{80,120}`、`SERIALWRAP_CONFIG_DIR=/golden/config`、`SERIALWRAP_STATE_DIR=/golden/state`、`SERIALWRAP_RUN_DIR=/golden/run`、`HOME=/golden/home`、`LANG=C.UTF-8`，其餘 `SERIALWRAP_*` 全刪。每個情境寫一個 `<id>.json`：`{"argv":[...],"columns":80,"rc":0,"stdout":"…","stderr":"…","native":true,"post_parse_effects":false}`；`native` 依 argv 的群組是否屬 `DELEGATED_GROUPS` 決定；`post_parse_effects` 為 true 表示該 argv 解析成功後會繼續連線（例如 `--sock /x session list`），Rust argparse 單元測試對這類情境只期望 `(0,"","")`，黑箱 help 測試略過（連線行為由其他案例覆蓋）。`_meta.json` 記錄 `python_version`（必須為 `3.12.3`）與產生時的 git revision。Windows 子目錄以 **Windows 上的 CPython 3.12.3**（Task 17 的 `-Python3123` 參數：python.org 3.12.3 embeddable zip 解壓到 `%LOCALAPPDATA%\sw-rust-test\py-3.12.3\`，檔案 sha256 釘在 `scripts/rust/win-python-3.12.3.sha256`）執行 `serialwrap.py` 產生（只取 `--help`、`profile test --help` 與 10 個錯誤情境，因路徑 default 與換行不同）；Windows 的 `py -3.12`（3.12.10）**不得**用來產生 argparse golden。取不到 3.12.3 時 Windows 子目錄不產生，Windows cli_help 列「證據不足」（§3 B8）。CI（Task 19）的 Linux Python 也釘 `3.12.3`。

情境清單（必含，其餘依 parser 樹自動展開）：
- `--help`；20 個群組各自 `--help`；12 個原生群組所有子命令 `--help`（各 2 種 COLUMNS）。
- 錯誤：`[]`、`["bogus"]`、`["session"]`、`["session","bogus"]`、`["--retries","x","session","list"]`、`["--timeout","abc","session","list"]`、`["--timeout"]`、`["--v","session","list"]`、`["--sock","/x","session","list"]`（縮寫成功→後續 SOCKET_ERROR，此情境只錄 rc 與 stdout）、`["session","list","extra"]`、`["file","push","--selector","C","--local","a","--remote","b","--ack-mode","bad"]`、`["skill","--platform","bsd"]`、`["event","reset"]`、`["event","reset","--rule-id","a","--selector","b"]`、`["event","tail","-n","x"]`、`["event","tail","-n5","--since=7","--help"]`、`["cmd","submit"]`、`["cmd","submit","--selector"]`、`["session","recover","--selector","A","--timeout","x"]`、`["--socket","connect"]`、`["--endpoint=connect"]`、`["--","session","list"]`、`["session","list","--","x"]`、`["-vv"]`、`["--version"]`、`["--version","session"]`、`["-h","session"]`、`["session","-h","list"]`、`["profile","test","--profile","p"]`、`["alias","assign","--by-id","b"]`、`["stream","tail","--from-seq","-1"]`、`["log","tail-raw","--from-seq","1e3"]`、`["--retries","-1","session","list"]`（解析成功）。

`crates/sw-cli/tests/argparse_golden.rs`：

```rust
mod common;
use sw_cli::argparse::{parse_args, ParseResult};
use sw_cli::cli_spec::{root_spec, SpecDefaults};

#[test]
fn native_paths_match_python_3_12_golden() {
    let dir = common::data_root().join("contracts/cli_help/linux");
    let spec = root_spec(&SpecDefaults {
        version_line: sw_proto::version::version_line(),
        profile_dir: "/golden/config/profiles".into(),
    });
    let mut fails = Vec::new();
    for ent in std::fs::read_dir(&dir).unwrap() {
        let path = ent.unwrap().path();
        if path.file_name().unwrap() == "_meta.json" { continue; }
        let case = sw_proto::pyjson::loads(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let c = Case::from(&case);
        if !c.native { continue; }
        let (rc, out, err) = match parse_args(&spec, &c.argv, c.columns - 2) {
            ParseResult::Exit(x) => (x.code, x.stdout, x.stderr),
            ParseResult::Ok(_) => (0, String::new(), String::new()),
            ParseResult::Delegated => (-1, String::new(), String::new()),
        };
        if c.parse_only_expected() != (rc, out.as_str(), err.as_str()) {
            fails.push(format!("{}:\n--- got rc={rc}\n{out}{err}\n--- want rc={}\n{}{}", path.display(), c.rc, c.stdout, c.stderr));
        }
    }
    assert!(fails.is_empty(), "{} 筆不符\n{}", fails.len(), fails.join("\n"));
}

#[test]
fn help_width_rules() {
    use sw_cli::argparse::help_width;
    assert_eq!(help_width(Some("120"), Some(50)), 118);
    assert_eq!(help_width(Some("0"), Some(50)), 48);
    assert_eq!(help_width(Some("x"), None), 78);
    assert_eq!(help_width(None, None), 78);
}
```

（`Case::parse_only_expected`：golden 的 rc／stdout／stderr；對於「解析成功後才失敗」的情境（例如 `--sock` 縮寫），期望為 `(0,"","")`。）

`argparse_synth.rs`：`gen_argparse_synth.py` 以 Python argparse 建 12 個合成 parser（涵蓋 `nargs='*'`、`append`、`count`、必填 mutex、`choices`、`type=int` 負數、`allow_abbrev=False`、長選項前綴衝突、`-xVALUE`、`-x=VALUE`、合併旗標 `-vv`），各跑 10 組 argv；Rust 以同樣宣告建 `ParserSpec`（表格寫在測試檔）比對。

`tests/contract_blackbox/test_bb_cli_help.py`：

```python
import json
from pathlib import Path

import pytest

from bb_harness import NL, is_rust, python_baseline_interpreter, repo_root, run_cli

GOLDEN = repo_root() / "contracts" / "cli_help" / ("windows" if NL == b"\r\n" else "linux")
META = json.loads((repo_root() / "contracts" / "cli_help" / "_meta.json").read_text(encoding="utf-8"))
CASES = sorted(p for p in GOLDEN.glob("*.json") if p.name != "_meta.json")
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


@pytest.fixture(scope="module", autouse=True)
def _dialect_guard():
    # Python 基線輪：受測直譯器必須是 golden 的方言；不符 → FAIL（證據不足），不得 SKIP
    if not is_rust():
        import subprocess
        v = subprocess.run([*python_baseline_interpreter(), "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"],
                           capture_output=True, text=True, check=True).stdout.strip()
        assert v == META["python_version"], f"Python 基線 {v} ≠ golden 方言 {META['python_version']}（證據不足）"


def _params():
    for p in CASES:
        c = json.loads(p.read_text(encoding="utf-8"))
        marks = [] if c["native"] else [pytest.mark.needs_delegation]
        yield pytest.param(p, id=p.stem, marks=marks)


def _env(env, columns):
    e = dict(env)
    e.update(COLUMNS=str(columns), SERIALWRAP_CONFIG_DIR="/golden/config", SERIALWRAP_STATE_DIR="/golden/state",
             SERIALWRAP_RUN_DIR="/golden/run", HOME="/golden/home")
    return e


@pytest.mark.parametrize("case", list(_params()))
def test_help_usage_error_bytes(env, case):
    c = json.loads(case.read_text(encoding="utf-8"))
    if c.get("post_parse_effects"):
        pytest.skip("解析後連線行為由其他黑箱案例覆蓋")
    r = run_cli(c["argv"], env=_env(env, c["columns"]))
    nl = NL.decode()
    assert (r.rc, r.stdout.decode(), r.stderr.decode()) == (c["rc"], c["stdout"].replace("\n", nl), c["stderr"].replace("\n", nl))
```

- [ ] **Step 2：確認紅燈**

`crates/sw-cli/tests/cli_spec_schema.rs`（Step 1 就寫，不等實作）：讀 `common::data_root()/contracts/cli_schema.json`，缺檔 `panic!("P0 contracts 缺少")`；逐一比對 `root_spec` 的群組、子命令、旗標、dest、default、choices、required 與 schema 一致。

Run：`python3 scripts/rust/capture_cli_help.py --out contracts/cli_help/linux && cargo test -p sw-cli --test argparse_golden --test argparse_synth --test cli_spec_schema --test delegate_route`
Expected：FAIL，`not yet implemented: P3-T10`；`test_bb_cli_help.py` 對 Python 基線 PASS（`python3 -m pytest -q tests/contract_blackbox -k cli_help --confcutdir=tests/contract_blackbox`，含 `needs_delegation` 列，因 Python 基線不經委派）。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 擷取 Python 3.12 argparse help／usage／錯誤 golden（紅燈）"`

- [ ] **Step 4：實作**

依 Interfaces 完成引擎、`cli_spec.rs` 與 `delegate::classify`。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-cli --test argparse_golden --test argparse_synth --test cli_spec_schema --test delegate_route`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作 argparse 3.12.3 相容層、20 群組命令列規格與委派路由"`

---

### Task 11：`output` 與 `cli_trace`

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-11-output-trace.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-cli/src/{output.rs,cli_trace.rs,ctx.rs}`
- Create: `scripts/rust/goldens/gen_cli_trace.py`、`contracts/golden/cli_trace.tsv`
- Test: `crates/sw-cli/tests/{output_format.rs,cli_trace.rs}`、`tests/contract_blackbox/{test_bb_output_contract.py,test_bb_trace.py}`

**Interfaces:**
- Consumes：Task 3、4、7、9（`ClientCaps.text_newline`、`stdout_is_terminal`）。
- Produces：

```rust
// ctx.rs
pub struct TextIo<'a> { out: &'a mut dyn std::io::Write, err: &'a mut dyn std::io::Write, newline: &'static str }
impl<'a> TextIo<'a> {
    pub fn new(out: &'a mut dyn std::io::Write, err: &'a mut dyn std::io::Write, newline: &'static str) -> Self;
    pub fn out_str(&mut self, s: &str) -> std::io::Result<()>;   // "\n" → newline（Q-05）
    pub fn err_str(&mut self, s: &str) -> std::io::Result<()>;   // 寫入後立即 flush（Q-06）
}
pub struct CliCtx<'a> { pub p: &'a sw_pal::ClientPlatform, pub env: sw_proto::runtime_paths::EnvSnapshot,
                        pub paths: sw_proto::runtime_paths::RuntimePaths, pub io: TextIo<'a>, pub verbose: i64 }
pub fn stdout_writer(is_terminal: bool) -> Box<dyn std::io::Write>;  // 非 TTY：BufWriter 8192；TTY：LineWriter

// output.rs
pub fn print_json(io: &mut TextIo, v: &sw_proto::pyjson::PyDict) -> std::io::Result<()>;     // Q-01
pub fn format_err_line(resp: &sw_proto::pyjson::PyDict, context: Option<&str>) -> String;   // Q-02（不含換行）
pub fn mirror_err(io: &mut TextIo, resp: &sw_proto::pyjson::PyDict, context: Option<&str>) -> std::io::Result<()>;
pub fn warn_version_mismatch(io: &mut TextIo, resp: &sw_proto::pyjson::PyDict, cli_version: &str) -> std::io::Result<()>; // Q-28
pub fn print_internal_error(io: &mut TextIo, context: &str, message: &str) -> i32;          // stdout＋mirror，回傳 1

// cli_trace.rs
pub const LEVEL_DEBUG: i64 = 10; pub const LEVEL_INFO: i64 = 20; pub const LEVEL_WARNING: i64 = 30;
pub fn parse_log_level(raw: Option<&str>) -> Option<sw_proto::pyjson::PyInt>;     // cli_trace.py:19-45
pub fn resolve_cli_trace_level(verbose: i64, env_level: Option<&str>) -> sw_proto::pyjson::PyInt;
pub fn trace_enabled(level: &sw_proto::pyjson::PyInt) -> bool;   // level==0 → 繼承 WARNING；否則 level <= 20
pub fn describe_trace_endpoint(ep: &str) -> (&'static str, String);   // cli_trace.py:104-114
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TraceMeta { pub elapsed_ms: i64, pub retry_count: i64, pub errno: Option<i64>, pub errno_name: Option<String> }
pub fn format_trace_line(endpoint: &str, source: &str, method: &str, timeout_s: f64,
                         resp: &sw_proto::pyjson::PyDict, meta: &TraceMeta) -> String;  // RPC_WIRE 序列化、key 順序固定、不含換行
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-cli/tests/output_format.rs` 的 `format_err_line` 表（`resp` 以 JSON 表示）：

| test | resp | context | 期望 |
|---|---|---|---|
| `code_and_message` | `{"ok":false,"error_code":"SOCKET_ERROR","message":"[Errno 2] No such file or directory"}` | `session.list` | `serialwrap: session.list failed: SOCKET_ERROR: [Errno 2] No such file or directory` |
| `message_only` | `{"ok":false,"message":"m"}` | 無 | `serialwrap: failed: m` |
| `nothing` | `{"ok":false}` | 無 | `serialwrap: failed: UNKNOWN_ERROR` |
| `hint_fullwidth` | `{"ok":false,"error_code":"NEEDS_SUDO","hint":"h"}` | `service start` | `serialwrap: service start failed: NEEDS_SUDO（hint: h）` |
| `empty_strings_skipped` | `{"ok":false,"error_code":"X","message":"","hint":""}` | `c` | `serialwrap: c failed: X` |
| `falsy_code_uses_message` | `{"ok":false,"error_code":0,"message":"m"}` | `c` | `serialwrap: c failed: m` |
| `int_code_str` | `{"ok":false,"error_code":5}` | `c` | `serialwrap: c failed: 5` |
| `list_message_repr` | `{"ok":false,"error_code":"E","message":["a",1.0]}` | `c` | `serialwrap: c failed: E: ['a', 1.0]` |
| `bool_hint` | `{"ok":false,"error_code":"E","hint":true}` | `c` | `serialwrap: c failed: E（hint: True）` |

`print_json` 表：

| test | 值 | newline | 期望 bytes |
|---|---|---|---|
| `sorted_nested` | `{"b":1,"a":{"d":1.0,"c":"é"}}` | `\n` | `{"a":{"c":"é","d":1.0},"b":1}\n` |
| `crlf` | `{"ok":true}` | `\r\n` | `{"ok":true}\r\n` |
| `nan_inf` | `{"x":NaN,"y":-Infinity}` | `\n` | `{"x":NaN,"y":-Infinity}\n` |
| `embedded_newline_escaped` | `{"m":"a\nb"}` | `\r\n` | `{"m":"a\\nb"}\r\n`（JSON 內的換行已跳脫，不受轉換影響） |

`warn_version_mismatch` 表：

| resp.version | cli | 期望 stderr |
|---|---|---|
| `"9.9.9"` | `0.3.2` | `serialwrap: client 版本 0.3.2 與 daemon 版本 9.9.9 不一致（建議統一安裝來源或重啟 daemon 對齊版本；serialwrap doctor 可查其他安裝）\n` |
| `"0.3.2"` | `0.3.2` | 空 |
| `" "`、`5`、缺 | `0.3.2` | 空 |
| `"9.9.9"` | `unknown` | 空 |

`crates/sw-cli/tests/cli_trace.rs`：`cli_trace.tsv` 由 `gen_cli_trace.py` 呼叫 `sw_core.cli_trace` 產生：`parse_log_level` 60 組（`None`、`""`、`"  "`、`"info"`、`" Warn "`、`"fatal"`、`"NOTSET"`、`"0"`、`"-5"`、`"--5"`、`"²"`、`"1"*4300`、`"1"*4301`、`"20"`、`"21"`、`"abc"`）、`resolve_cli_trace_level`（verbose 0／1／2／3 × env）、`isEnabledFor(INFO)`（實際以 `configure_cli_trace_logger` 後查詢，涵蓋 0 與負數）、`describe_trace_endpoint` 30 組（unix 路徑、`tcp://127.0.0.1:1`、`tcp://LOCALHOST:5`、`tcp://[::1]:5`、非 loopback、無法解析、非 UTF-8 以 surrogateescape 的字串）。

```rust
#[test]
fn trace_line_exact() {
    let resp = sw_proto::pydict! { "ok" => false, "error_code" => "SOCKET_ERROR", "message" => "x" };
    let meta = sw_cli::cli_trace::TraceMeta { elapsed_ms: 500, retry_count: 1, errno: Some(2), errno_name: Some("ENOENT".into()) };
    assert_eq!(
        sw_cli::cli_trace::format_trace_line("/nonexistent/x.sock", "--socket", "session.list", 5.0, &resp, &meta),
        r#"{"endpoint_transport":"unix","endpoint_id":"534a413975f6a6393e9cc37ef545b4492fedd7cab94f4359d7949e0b54f82ec2","endpoint_source":"--socket","method":"session.list","elapsed_ms":500,"error_code":"SOCKET_ERROR","errno":2,"errno_name":"ENOENT","retry_count":1,"timeout_s":5.0}"#
    );
}
```

`test_bb_output_contract.py`（stubd）：

| test | 情境 | 斷言 |
|---|---|---|
| `test_rpc_fail_order_separated` | stub 回 `{"ok":false,"error_code":"SESSION_NOT_FOUND","message":"m","version":"9.9.9"}`，`session clear --selector A` | rc 2；stdout 一行 JSON（key 排序）；stderr 兩行，第 1 行 `serialwrap: session.clear failed: SESSION_NOT_FOUND: m`、第 2 行版本警告 |
| `test_event_fail_order` | 同 stub，`event rm r1` | stderr 第 1 行版本警告、第 2 行 `serialwrap: event.rule_delete failed: SESSION_NOT_FOUND: m` |
| `test_merged_stream_order_matches_python` | 同 stub，`merge_stderr=True` | Python 與 Rust 合流 bytes 一致：stderr 兩行在前、stdout JSON 在後（Q-06） |
| `test_ok_no_stderr` | stub 回 `{"ok":true,"version":"<CLI 版本>"}` | stderr 空 |
| `test_broken_pipe_large_write_exit_1` | stdout 為讀端已關閉的 pipe，執行 `skill`（輸出 ≥ 8192 B） | rc 1（Q-20） |
| `test_broken_pipe_small_output_exit_120` | 同上，stub 回 `{"ok":true,"seq":1}`，執行 `--socket <stub> wal current-seq`（輸出 < 8192 B） | rc 120（Q-20：直譯器結束時 flush 失敗） |

`test_bb_trace.py`：`-v --socket /nonexistent/x.sock session list` 的 stderr 第 1 行 JSON（`elapsed_ms` 正規化為 0 後比對）、`-vv`、`SERIALWRAP_LOG_LEVEL=INFO`／`0`／`-5`／`NOTSET`／`garbage`（是否出現 trace 行與 Python 相同）、`--retries 1` 的 `retry_count` 為 1。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-cli --test output_format --test cli_trace`
Expected：FAIL，`not yet implemented: P3-T11`。黑箱兩檔對 Python PASS。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 新增 stdout JSON、stderr 錯誤行、版本警告與 trace 契約測試（紅燈）"`

- [ ] **Step 4：實作**

`format_err_line` 以 `pyfmt::py_str` 格式化 code／message／hint；`py_truthy` 判斷；全形括號 `（hint: …）`。`format_trace_line` 以 `pydict!` 依固定 key 序建構、`timeout_s` 為 `PyValue::Float`、`errno_name` 在 `errno` 為 None 時一律 null。`trace_enabled`：`Big` 正數 → false、`Big` 負數 → true、`Small(0)` → false（繼承 WARNING）、其餘 `<= 20`。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-cli --test output_format --test cli_trace`
Expected：PASS（黑箱對 Rust 的驗證在 Task 14 完成後一併執行）。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作 Python 相容的 stdout／stderr 輸出、版本警告與 RPC trace"`

---

### Task 12：endpoint 解析（#108）與 bench state

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p3-12-endpoint-resolve.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-cli/src/{endpoint_resolve.rs,bench_state.rs}`
- Test: `crates/sw-cli/tests/endpoint_resolve.rs`、`tests/contract_blackbox/test_bb_endpoint.py`

**Interfaces:**
- Consumes：Task 6（`RuntimeConfig`）、Task 7（`RuntimePaths`、`parse_endpoint`、`resolve_config_path`、`bench_state_path`、`SYSTEM_SOCKET`）、Task 9（`RpcConnector`、`FsRead`、`select_backend`、`SysEnv::system_config_dir`）、Task 11（`TextIo`、`print_json`、`mirror_err`）、Task 8（`ErrorCode`）。
- Produces：

```rust
// endpoint_resolve.rs（cli.py:658-835）
#[derive(Clone, Debug, PartialEq)]
pub struct ResolvedEndpoint { pub endpoint: String, pub source: &'static str }
// source ∈ {"--endpoint","--socket","--bench","config.yaml","default","config.yaml -> canonical fallback"}
#[derive(Clone, Debug, PartialEq)]
pub struct EndpointResolutionError { pub code: sw_proto::error_codes::ErrorCode, pub message: String }
#[derive(Clone, Debug, PartialEq)]
pub enum ResolveFailure { Resolution(EndpointResolutionError), Crash(String) }   // Crash：Python traceback 路徑（Q-18），exit 1
pub struct EndpointArgs<'a> { pub endpoint: Option<&'a str>, pub socket: Option<&'a str>, pub bench: Option<&'a str> }
pub fn rpc_backend_is_win(ctx: &CliCtx) -> bool;              // Q-17
pub fn local_default_endpoint(ctx: &CliCtx) -> String;        // Q-16
pub fn endpoint_alive(ctx: &CliCtx, ep: &str) -> bool;        // 01 §4.4，探測 0.2 s
pub fn load_runtime_config(ctx: &CliCtx) -> Result<sw_proto::config_types::RuntimeConfig, sw_proto::config_types::ConfigError>;
// SC-09：path = resolve_config_path(env, paths, p.env.system_config_dir(), flavor, home, &|x| p.fs.exists(x))；
// 讀 path：不存在 → 空 RuntimeConfig（SERIALWRAP_CONFIG 指向不存在的檔時也是空，不再往使用者檔或系統檔找）
pub fn safe_runtime_config(ctx: &CliCtx) -> Option<sw_proto::config_types::RuntimeConfig>;
pub fn resolve_endpoint_info(ctx: &mut CliCtx, a: &EndpointArgs) -> Result<ResolvedEndpoint, ResolveFailure>;
pub fn print_endpoint_resolution_error(ctx: &mut CliCtx, e: &EndpointResolutionError, context: &str) -> i32;   // 回傳 1

// bench_state.rs（cli.py:1115-1188；1A 只讀，不加鎖）
#[derive(Clone, Debug, PartialEq)]
pub enum BenchStateError { Value(String), Os(String) }
pub fn load_bench_state_doc(ctx: &CliCtx, path: &str) -> Result<sw_proto::pyjson::PyDict /* bench map */, BenchStateError>;
pub fn remembered_bench_endpoint(benches: &sw_proto::pyjson::PyDict, code: &str) -> Result<Option<String>, String>;
```

讀檔錯誤組裝：`Os` 的訊息為 `PyOsError{errno, strerror, filename: Some(path)}` 的字串（Q-22）；UTF-8 錯誤與 JSON 錯誤歸 `Value`（Python `UnicodeDecodeError`、`JSONDecodeError` 皆為 `ValueError`），文字照 Python（Q-23）。

- [ ] **Step 1：寫失敗測試**

`crates/sw-cli/tests/endpoint_resolve.rs`（以 `sw_pal::fakes` 組 `ClientPlatform`；`alive` 以 `ScriptedConnector` 決定）：

| test | 輸入 / 檔案 / 可連性 | 期望 |
|---|---|---|
| `endpoint_wins` | `--endpoint tcp://h:1 --socket /s` | `Ok(("tcp://h:1","--endpoint"))` |
| `empty_endpoint_falls_through` | `--endpoint ""`、無 socket、無 config | `Ok((<SOCKET_PATH>,"default"))` |
| `explicit_empty_socket` | `--socket ""` | `Ok(("","--socket"))`（Q-07） |
| `socket_equal_default_is_still_explicit` | `--socket <SOCKET_PATH>`，config 有 `socket_path: /cfg.sock` | `Ok((<SOCKET_PATH>,"--socket"))` |
| `bench_ok` | `--bench lab`，state `{"lab":"tcp://127.0.0.1:7001"}` | `Ok(("tcp://127.0.0.1:7001","--bench"))` |
| `bench_nested_format` | state `{"benches":{"lab":" tcp://localhost:7001 "}}` | `Ok(("tcp://localhost:7001","--bench"))` |
| `bench_missing_file` | state 不存在 | `Resolution(BENCH_ENDPOINT_NOT_REMEMBERED, "bench \"lab\" 尚未記住 endpoint；請先執行 serialwrap connect lab")` |
| `bench_not_object` | state `[1]` | `Resolution(INVALID_BENCH_STATE, "benches.state.json 必須是 object")` |
| `bench_benches_not_object` | state `{"benches":[1]}` | `Resolution(INVALID_BENCH_STATE, "benches.state.json 的 benches 欄位必須是 object")` |
| `bench_bad_json` | state `{` | `Resolution(INVALID_BENCH_STATE, "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)")` |
| `bench_non_loopback` | state `{"lab":"tcp://10.0.0.1:7001"}` | `Resolution(INVALID_BENCH_STATE, "bench \"lab\" 的 endpoint 記憶損壞；請重新執行 serialwrap connect lab")` |
| `bench_unix_value` | state `{"lab":"/x.sock"}` | 同上訊息 |
| `bench_state_is_dir` | state 路徑為目錄 | `Resolution(BENCH_STATE_IO_ERROR, "[Errno 21] Is a directory: '<path>'")` |
| `bench_bad_utf8` | state bytes `ff` | `Resolution(INVALID_BENCH_STATE, "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte")` |
| `config_alive` | config `socket_path: /cfg.sock`，可連 | `Ok(("/cfg.sock","config.yaml"))` |
| `config_dangling_fallback` | `/cfg.sock` 不可連，`<SOCKET_PATH>` 可連，mode 缺 | `Ok((<SOCKET_PATH>,"config.yaml -> canonical fallback"))`；stderr `serialwrap: config.yaml socket_path '/cfg.sock' 不可連，依 supervision_mode=on-demand 改用 '<SOCKET_PATH>'；如需修正請更新 config.yaml 或重跑 serialwrap setup。\n` |
| `config_dangling_system` | 同上但 `supervision_mode: systemd-system`，`/run/serialwrap/serialwrapd.sock` 可連 | canonical 為 `SYSTEM_SOCKET` |
| `config_dangling_canonical_dead` | 兩者皆不可連 | `Ok(("/cfg.sock","config.yaml"))`、stderr 空 |
| `config_dangling_same_value` | `socket_path` 等於 `<SOCKET_PATH>` 且不可連 | `Ok((<SOCKET_PATH>,"config.yaml"))` |
| `config_tcp_not_probed_posix` | `socket_path: tcp://10.1.1.1:5` | `Ok(("tcp://10.1.1.1:5","config.yaml"))`，connector 無任何呼叫 |
| `config_broken_yaml` | config `a: [` | `Ok((<SOCKET_PATH>,"default"))` |
| `config_int_socket_path` | `socket_path: 5` | `Crash(_)`（Q-18） |
| `config_zero_socket_path` | `socket_path: 0` | `Ok((<SOCKET_PATH>,"default"))`（falsy） |
| `sc09_explicit_config_env` | `SERIALWRAP_CONFIG=<tmp>/x.yaml`（`socket_path: /x.sock`，可連） | `Ok(("/x.sock","config.yaml"))` |
| `sc09_explicit_missing_is_empty` | `SERIALWRAP_CONFIG=<tmp>/missing.yaml`（不存在）；使用者 config 與系統 config 都有 `socket_path: /other.sock` | `Ok((<SOCKET_PATH>,"default"))`（explicit 無條件勝出，不存在視為空 config） |
| `sc09_explicit_blank_ignored` | `SERIALWRAP_CONFIG="  "`、使用者 config 有 `socket_path: /u.sock`（可連） | `Ok(("/u.sock","config.yaml"))` |
| `sc09_system_fallback` | 使用者 config 不存在、`FakeEnv.system_config_dir` 指向的目錄有 `config.yaml`（`socket_path: /sys.sock`，可連） | `Ok(("/sys.sock","config.yaml"))`；系統目錄只由 `SysEnv::system_config_dir()` 取得 |
| `sc09_user_beats_system` | 使用者與系統 config 都存在 | 採使用者 config |
| `win_backend_default_tcp` | `SERIALWRAP_RPC_BACKEND=win`，native Posix | `local_default_endpoint == "tcp://127.0.0.1:48700"` |
| `win_backend_custom_port` | 另設 `SERIALWRAP_TCP_PORT=49001` | `"tcp://127.0.0.1:49001"` |
| `posix_ignores_serialwrap_endpoint` | `SERIALWRAP_ENDPOINT=tcp://x:1`，posix | 預設仍為 `<SOCKET_PATH>` |
| `bogus_backend_uses_platform` | `SERIALWRAP_RPC_BACKEND=bsd`，native Posix | `rpc_backend_is_win == false` |
| `win_backend_probes_tcp` | win backend，config `socket_path: tcp://127.0.0.1:1`（不可連），canonical 可連 | fallback 生效 |
| `no_af_unix_unix_dead` | `caps.af_unix=false`，config `/cfg.sock` | `endpoint_alive("/cfg.sock") == false` |
| `empty_endpoint_alive_true` | `endpoint_alive("")` | `true` |

`print_endpoint_resolution_error`：stdout `{"error_code":"BENCH_ENDPOINT_NOT_REMEMBERED","message":"…","ok":false}`、stderr `serialwrap: session.list failed: BENCH_ENDPOINT_NOT_REMEMBERED: …`、回傳 1。

`test_bb_endpoint.py`（stubd 與真實 socket 檔）：`--bench`（三種錯誤與成功）、dangling fallback（config 指向不存在的 sock、stubd 在 `<run>/serialwrapd.sock`）、`--endpoint tcp://h:1/`、`foo://x`、`tcp://[::1`、`--socket ""`、`SERIALWRAP_TCP_PORT=""` → rc 1 stdout 空（Q-15）、`SERIALWRAP_TCP_PORT=abc skill` → rc 1、`SERIALWRAP_CONFIG` 指定檔、`SERIALWRAP_CONFIG` 指向不存在檔時不讀使用者 config（P1 SC-09 前置）。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-cli --test endpoint_resolve`
Expected：FAIL，`not yet implemented: P3-T12`。黑箱對 Python PASS（SC-09 兩案例在 P1 合併前為紅燈，依 §0.2 處理）。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 新增 endpoint 解析優先序、dangling fallback 與 bench state 測試（紅燈）"`

- [ ] **Step 4：實作**

照 `cli.py:658-835`、`1113-1188` 逐行移植；`_safe_runtime_config` 吞錯；`cfg_sock` 型別：`YValue::Str` 正常；falsy（`Null`、`Bool(false)`、`Int(0)`、`Str("")`、空容器）→ 視為無；其他 truthy 非 str → `Crash`。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-cli --test endpoint_resolve`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作 endpoint 解析、#108 dangling fallback 與 --bench 讀取"`

---

### Task 13：RPC client（白名單重試、退避、TIMEOUT 探測）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p3-13-rpc-client.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-cli/src/client.rs`
- Test: `crates/sw-cli/tests/client_retry.rs`、`tests/contract_blackbox/test_bb_client.py`

**Interfaces:**
- Consumes：Task 3、7（常數、`parse_endpoint`、`Method`）、Task 9（`RpcConnector`、`Clock`、`errno_name`）、Task 11（`TraceMeta`）。
- Produces：

```rust
#[derive(Clone, Debug, PartialEq)]
pub enum ClientCrash { InvalidTimeout(String), EncodeError(String) }   // Python ValueError／OverflowError／UnicodeEncodeError → exit 1
pub struct RpcOutcome { pub resp: sw_proto::pyjson::PyDict, pub meta: TraceMeta }
pub fn sock_timeout(timeout_s: f64) -> Result<sw_pal::traits::SockTimeout, ClientCrash>;
// NaN／<0 → InvalidTimeout；>9_223_372_036.854775807 或 inf → InvalidTimeout；0 → NonBlocking；其他 → Blocking
// 呼叫時機照 Q-10：unix 在 connect 前；tcp 在 p.rpc.resolve_tcp() 成功之後（resolve 失敗 → SOCKET_ERROR，不 crash）
pub fn encode_request(req_id: i64, method: &str, params: &sw_proto::pyjson::PyValue) -> Vec<u8>;
// RPC_WIRE：{"id":<req_id>,"method":<method>,"params":<params>} + "\n"
pub fn rpc_call_once(p: &sw_pal::ClientPlatform, endpoint: &str, method: &str, params: &sw_proto::pyjson::PyValue,
                     req_id: i64, timeout_s: f64) -> Result<(sw_proto::pyjson::PyDict, Option<i64> /* errno */), ClientCrash>;
pub fn probe_daemon_after_timeout(p: &sw_pal::ClientPlatform, endpoint: &str) -> sw_proto::pyjson::PyDict;
pub fn rpc_call(p: &sw_pal::ClientPlatform, endpoint: &str, method: &str, params: &sw_proto::pyjson::PyValue,
                req_id: i64, timeout_s: f64, retries: &sw_proto::pyjson::PyInt) -> Result<RpcOutcome, ClientCrash>;
#[doc(hidden)]   // 只供測試以小上限驗 response cap；產品路徑一律用 CLIENT_RESPONSE_MAX_BYTES
pub fn rpc_call_once_capped(p: &sw_pal::ClientPlatform, endpoint: &str, method: &str, params: &sw_proto::pyjson::PyValue,
                            req_id: i64, timeout_s: f64, max_bytes: usize) -> Result<(sw_proto::pyjson::PyDict, Option<i64>), ClientCrash>;
```

行為照 `client.py:120-278` 與 Q-10～Q-14、Q-31：
- `rpc_call_once`：`parse_endpoint` 失敗 → `{"ok":false,"error_code":"INVALID_ENDPOINT","message":<str>}`（**在 `sock_timeout` 之前**，Q-10）；unix 且 `!af_unix_available()` → `{"ok":false,"error_code":"SOCKET_ERROR","message":"本平台無 AF_UNIX，無法連接 unix endpoint '<repr>'（請改用 tcp://127.0.0.1:<port>，#131）"}`（以 `py_repr_str`）；connect `TimedOut` → `{"ok":false,"error_code":"TIMEOUT"}`；其他 → `SOCKET_ERROR`＋`message=py_message`、errno=os_code；送出後逐次 `recv(65536)`，累積超過 `CLIENT_RESPONSE_MAX_BYTES` 仍無換行 → `INVALID_RESPONSE`；回應規則 Q-14。
- `rpc_call`：attempts、退避序列 0.5、1、2、4、5、5…（`Clock::sleep`）；只在 `TRANSIENT_ERROR_CODES` 時重試；最終 `TIMEOUT` 且 method 不在 `PROBE_METHODS` → `resp.update(probe)`；`TraceMeta.elapsed_ms = max(0, int((mono_end - mono_start) ms))`、`retry_count` 照 Python 規則、`errno` 為最後一次主嘗試、`errno_name = env.errno_name(errno)`。
- `probe_daemon_after_timeout`：deadline 2.5 s；`health.ping`（id 0、timeout 1.0）→ `daemon_reachable`；不可達或過 deadline 直接回；`health.status` ok 時取 `commands`、`sessions` 中型別為 `Int`（Python `isinstance(v, int)`：**bool 也是 int**，`True` 會被收）的值。

- [ ] **Step 1：寫失敗測試**

`crates/sw-cli/tests/client_retry.rs`（全部以 `sw_pal::fakes::{FakeClock, ScriptedConnector}`）：

```rust
use sw_cli::client::{rpc_call, ClientCrash};
use sw_pal::fakes::{fake_platform, FakeConn, StreamStep::*};
use sw_proto::pyjson::{PyInt, PyValue};

fn empty() -> PyValue { PyValue::Dict(Default::default()) }

#[test]
fn whitelist_method_retries_socket_error_with_backoff() {
    let (p, conn, clock) = fake_platform(vec![
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::refuse_errno(111, "[Errno 111] Connection refused"),
        FakeConn::stream(vec![Reply(br#"{"ok":true,"sessions":[]}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "session.list", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    assert_eq!(sw_proto::pyjson::dumps(&out.resp.clone().into(), &sw_proto::pyjson::RPC_WIRE), r#"{"ok":true,"sessions":[]}"#);
    assert_eq!(clock.sleeps_s(), vec![0.5, 1.0]);
    assert_eq!(out.meta.retry_count, 2);
    assert_eq!(out.meta.errno, None);
    assert_eq!(conn.sent(), vec![b"{\"id\":1,\"method\":\"session.list\",\"params\":{}}\n".to_vec()]);
    assert_eq!(conn.connect_count(), 3);
}

#[test]
fn non_whitelist_never_retries() {
    // Method::known() 扣掉 11 個白名單＝44 個方法（含 daemon.handoff_stop）× 三種 transient 失敗；每種都只能 connect 一次
    let wl = sw_proto::constants::RETRYABLE_READONLY_METHODS;
    let methods: Vec<&str> = sw_proto::rpc_types::Method::known().iter().copied().filter(|m| !wl.contains(m)).collect();
    assert_eq!(methods.len(), 44);
    for m in methods {
        for (kind, conn_step) in [("refuse", FakeConn::refuse_errno(111, "[Errno 111] Connection refused")),
                                  ("timeout", FakeConn::Timeout),
                                  ("eof", FakeConn::stream(vec![Eof]))] {
            // TIMEOUT 會觸發探測（health.ping 被拒後即停），所以腳本多給一條探測連線
            let (p, conn, clock) = fake_platform(vec![conn_step.clone(), FakeConn::refuse_errno(111, "x")]);
            let _ = rpc_call(&p, "/s.sock", m, &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
            let want_connects = if kind == "timeout" { 2 } else { 1 };   // 主呼叫 1 次＋（只在 TIMEOUT 時）ping 1 次
            assert_eq!(conn.connect_count(), want_connects, "{m} {kind}");
            assert!(conn.sent().iter().all(|b| !b.starts_with(br#"{"id":0"#) || b.starts_with(br#"{"id":0,"method":"health.ping""#)),
                    "{m} {kind}: 探測以外不得有額外請求");
            assert!(conn.sent().iter().filter(|b| b.starts_with(br#"{"id":1,"#)).count() <= 1, "{m} {kind}");
            assert!(clock.sleeps_s().is_empty(), "{m} {kind}");
        }
    }
}

#[test]
fn probe_only_sends_health_methods() {
    let (p, conn, _c) = fake_platform(vec![FakeConn::Timeout,
        FakeConn::stream(vec![Reply(br#"{"ok":true}"#.to_vec())]), FakeConn::stream(vec![Reply(br#"{"ok":true}"#.to_vec())])]);
    let _ = rpc_call(&p, "/s.sock", "command.submit", &empty(), 1, 5.0, &PyInt::Small(3)).unwrap();
    let sent = conn.sent();
    assert!(sent.iter().all(|b| b.starts_with(br#"{"id":0,"method":"health.ping""#) || b.starts_with(br#"{"id":0,"method":"health.status""#)));
}

#[test]
fn backoff_caps_at_5s() {
    let (p, _c, clock) = fake_platform(vec![FakeConn::Timeout; 7]);
    let out = rpc_call(&p, "/s.sock", "health.status", &empty(), 1, 5.0, &PyInt::Small(6)).unwrap();
    assert_eq!(clock.sleeps_s(), vec![0.5, 1.0, 2.0, 4.0, 5.0, 5.0]);
    assert_eq!(out.meta.retry_count, 6);
    assert!(out.resp.get("daemon_reachable").is_none());     // 探測方法自身不補探測
}

#[test]
fn timeout_enrich_reachable_busy_int_only() {
    let (p, conn, _clock) = fake_platform(vec![
        FakeConn::Timeout,
        FakeConn::stream(vec![Reply(br#"{"ok":true,"pong":true}"#.to_vec())]),
        FakeConn::stream(vec![Reply(br#"{"ok":true,"commands":3,"sessions":"x"}"#.to_vec())]),
    ]);
    let out = rpc_call(&p, "/s.sock", "device.list", &empty(), 1, 5.0, &PyInt::Small(0)).unwrap();
    assert_eq!(out.resp.get("daemon_reachable"), Some(&PyValue::Bool(true)));
    assert_eq!(sw_proto::pyjson::dumps(out.resp.get("daemon_busy").unwrap(), &sw_proto::pyjson::RPC_WIRE), r#"{"commands":3}"#);
    let sent = conn.sent();
    assert!(sent[0].starts_with(br#"{"id":0,"method":"health.ping","params":{}}"#));
    assert!(sent[1].starts_with(br#"{"id":0,"method":"health.status","params":{}}"#));
    assert_eq!(conn.timeouts_s(), vec![5.0, 1.0, 1.0]);
}

#[test]
fn invalid_timeout_crashes_only_after_endpoint_parse() {
    let (p, _c, _k) = fake_platform(vec![]);
    let bad = rpc_call(&p, "tcp://h:1/", "device.list", &empty(), 1, -1.0, &PyInt::Small(0)).unwrap();
    assert_eq!(bad.resp.get("error_code"), Some(&"INVALID_ENDPOINT".into()));
    for t in [-1.0, f64::NAN, f64::INFINITY, 1e10] {
        assert!(matches!(rpc_call(&p, "/s.sock", "device.list", &empty(), 1, t, &PyInt::Small(0)), Err(ClientCrash::InvalidTimeout(_))), "{t}");
    }
}

#[test]
fn tcp_resolution_precedes_timeout_validation() {
    // Q-10：create_connection 先 getaddrinfo；解析失敗 → SOCKET_ERROR（gaierror），不 crash
    let (p, conn, _k) = fake_platform(vec![]);
    conn.fail_resolve("no-such-host.invalid", sw_pal::fakes::FakeConn::gai_error(-2, "[Errno -2] Name or service not known"));
    let out = rpc_call(&p, "tcp://no-such-host.invalid:1", "device.list", &empty(), 1, -1.0, &PyInt::Small(0)).unwrap();
    assert_eq!(out.resp.get("error_code"), Some(&"SOCKET_ERROR".into()));
    assert!(matches!(rpc_call(&p, "tcp://127.0.0.1:1", "device.list", &empty(), 1, -1.0, &PyInt::Small(0)), Err(ClientCrash::InvalidTimeout(_))));
    assert_eq!(conn.connect_count(), 0);
}
```

其餘案例（同檔，表格化）：

| test | 腳本 | 期望 |
|---|---|---|
| `negative_retries_one_attempt` | retries=-2，SOCKET_ERROR | connect 1 次 |
| `big_retries_saturates` | retries=`Big("99999999999999999999")`，第 3 次成功 | 成功、sleeps `[0.5,1.0]` |
| `non_transient_no_retry` | 回 `{"ok":false,"error_code":"SESSION_NOT_FOUND"}` | 不重試 |
| `invalid_response_not_transient` | 回 `[1]` | `INVALID_RESPONSE`，不重試 |
| `non_json_is_invalid_response` | 回 `garbage` | `INVALID_RESPONSE`（DF-06） |
| `empty_response` | 立即 EOF | `EMPTY_RESPONSE`（白名單會重試） |
| `whitespace_line_empty` | `"  \r\n"` | `EMPTY_RESPONSE` |
| `split_chunks_and_trailing` | `{"ok":` ＋ `true}\nJUNK` 分兩次 recv | `{"ok":true}` |
| `strip_vertical_tab` | `"\x0b{\"ok\":true}\x0c\n"` | `{"ok":true}` |
| `utf8_replace` | `{"m":"\xff"}\n` | `m` 為 `"\u{FFFD}"` |
| `no_id_check` | `{"id":99,"ok":true}` | 原樣回傳（含 `id`） |
| `etimedout_is_timeout` | connect 失敗 `os_code=110, kind=TimedOut` | `TIMEOUT`、errno None |
| `recv_timeout` | recv `TimedOut` | `{"ok":false,"error_code":"TIMEOUT"}`（無 message） |
| `probe_unreachable` | 主呼叫 TIMEOUT、ping refuse | `daemon_reachable:false`、無 `daemon_busy` |
| `probe_deadline` | ping 成功但 FakeClock 已推進 2.6 s | 不送 `health.status` |
| `probe_bool_counts` | status `{"ok":true,"commands":true}` | `daemon_busy={"commands":true}` |
| `errno_from_last_main_attempt` | 兩次 refuse（111 後 2），第三次 TIMEOUT 再探測 | `errno=None`（最後一次為 TIMEOUT） |
| `response_cap` | `rpc_call_once_capped(max_bytes=1024)`，回 2 KiB 無換行 | `INVALID_RESPONSE` |
| `large_response_ok` | 正式路徑回 20 MiB 的單行 JSON（`wal export --limit 50000` 規模） | 正常解析，`ok:true` |
| `no_af_unix_message` | `af_unix=false`，`/x.sock` | `SOCKET_ERROR`、message `本平台無 AF_UNIX，無法連接 unix endpoint '/x.sock'（請改用 tcp://127.0.0.1:<port>，#131）` |
| `elapsed_ms` | 兩次 sleep＝1.5 s | `elapsed_ms == 1500` |

`test_bb_client.py`（stubd 實測，對 Python 與 Rust 同斷言）：`test_side_effect_methods_single_send`——`--retries 3` 搭配 stub `{"close":true}`（請求已送達、回應遺失），對 `cmd submit --selector A --cmd ls`、`session interactive-send --interactive-id i --data d`、`device release --selector A`、`device attach --selector A`、`file push --selector A --local l --remote r`、`wal reset`、`session recover --selector A`、`cmd cancel --cmd-id c`、`cmd result-tail --cmd-id c` 各斷言 `len(stub.requests()) == 1`（不含探測方法）且 rc 2；`--retries 2 session list` 兩次 `close` 後成功（stub 收到 3 筆、rc 0、耗時 ≥1.4 s）；`--retries 2 session interactive-status --interactive-id i`（stub 收到 1 筆）；`--retries 2 event list`（1 筆）；`--timeout 1 device list` 對 `delay_s: 3` → rc 2、`daemon_reachable` 存在；`--timeout 0 --endpoint tcp://127.0.0.1:<closed> mcu status` → `[Errno 115] Operation now in progress`；`--timeout -1` → rc 1、stdout 空；回 `not json` → `INVALID_RESPONSE` rc 2（P1 DF-06 前置）。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-cli --test client_retry`
Expected：FAIL，`not yet implemented: P3-T13`。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 新增 RPC client 白名單重試、退避與 TIMEOUT 探測測試（紅燈）"`

- [ ] **Step 4：實作**

照 Interfaces 與 `client.py` 逐行移植；`retries` 以 `PyInt` 處理，`Big` 正數等同 `i64::MAX` attempts 上限（迴圈遇成功或非 transient 即停）。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-cli --test client_retry`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作 RPC client、唯讀白名單重試與 TIMEOUT 後探測"`

---

### Task 14：資料面 RPC 指令與 `main` 分派

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-14-rpc-commands.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-cli/src/{lib.rs,main.rs,commands/mod.rs,commands/rpc_cmds.rs}`
- Test: `crates/sw-cli/tests/rpc_params.rs`、`crates/sw-cli/tests/run_process.rs`、`tests/contract_blackbox/{test_bb_rpc_params.py,test_bb_exit_codes.py}`、`tests/contract_blackbox/data/rpc_params.json`（下表 55 列；`rpc_params.rs` 的 `ns(&str)` 以空白切詞、`ns_argv(&[String])` 直接使用切好的 argv）

**Interfaces:**
- Consumes：Task 10～13。
- Produces：

```rust
// lib.rs
pub fn run(argv: Vec<std::ffi::OsString>, p: &sw_pal::ClientPlatform,
           out: &mut dyn std::io::Write, err: &mut dyn std::io::Write) -> i32;
pub fn run_process(argv: Vec<std::ffi::OsString>, p: &sw_pal::ClientPlatform) -> i32;
// run_process：建立 stdout_writer(caps)／stderr、以 std::panic::catch_unwind 包住 run、最後 flush。
//   run 期間 stdout 寫入失敗（EPIPE 等）→ 1；run 正常回傳後最終 flush 失敗 → 120（Q-20，對齊 Python 直譯器結束時 flush 失敗）；
//   panic → stderr 一行 `serialwrap: internal error: <panic 訊息>`、stdout 不再寫、回傳 1（對齊 Python 未攔例外 exit 1；
//   不得以 Rust 預設 exit 101 外洩，exit code 契約只有 0／1／2／120 與委派子行程的原值）
pub fn json_boundary_command(argv: &[String]) -> Option<&'static str>;   // cli.py:2457-2473 逐字（Q-08）
#[doc(hidden)]   // 測試入口：以閉包取代 run，驗 panic → 1、寫入失敗 → 1、最終 flush 失敗 → 120
pub fn run_process_with(f: &dyn Fn(&mut dyn std::io::Write, &mut dyn std::io::Write) -> i32,
                        out: Box<dyn std::io::Write>, err: Box<dyn std::io::Write>) -> i32;

// commands/mod.rs
pub fn dispatch(ctx: &mut CliCtx, ns: &Namespace) -> i32;
// commands/rpc_cmds.rs
pub fn request_for(ns: &Namespace) -> Option<(&'static str /* method */, sw_proto::pyjson::PyValue /* params */)>;
pub fn effective_timeout_s(ns: &Namespace, method: &str) -> f64;     // Q-30
pub fn run_rpc(ctx: &mut CliCtx, ns: &Namespace, method: &str, params: sw_proto::pyjson::PyValue) -> i32;
```

`run` 流程（arch §7.1）：
1. 以 `p.env.vars()` 建 `EnvSnapshot`；`RuntimePaths::resolve` 失敗（Q-15）→ stderr `serialwrap: invalid SERIALWRAP_TCP_PORT: '<raw>'`、回傳 1（在委派判斷之前）。
2. `delegate::classify(&argv_os[1..])`（Task 10）**在任何 UTF-8 轉換之前**對 `OsString` 執行；為 `Delegate` → `delegate::run_delegate`（Task 16），argv 以原始 `OsString` 原樣交給 Python（委派群組可接受非 UTF-8 路徑參數）。
3. 原生路徑才把 argv 轉 `String`（Q-19）：非 UTF-8 token 以 `to_string_lossy` 轉入 `parse_args` 供解析（只影響 usage／錯誤文字的極端情境），同時保留原始 `Vec<OsString>`；解析後依 dest 分類——路徑型 dest（`socket`、`endpoint` 的裸路徑／`unix://` 路徑、`profile_dir`、`sample`、`file`）取原始 `OsString` 交給 PAL；其他 dest（會進 RPC 請求或 stdout）若原始值非 UTF-8，`Namespace` 內保留 lossy 字串並標記 `tainted`，**在 Python 會失敗的同一時點才失敗**：(b) 進 RPC 請求者——endpoint 解析與 connect 照常進行（connect 失敗就照 Python 回 `SOCKET_ERROR`、exit 2），connect 成功後、`send_all` 之前以 `ClientCrash::EncodeError` 結束（stderr 一行 `serialwrap: internal error: argument is not valid UTF-8`、stdout 不寫、exit 1、daemon 收到連線但 0 個請求行）；(c) 要寫到 stdout 者——在 `print_json` 時以同一訊息結束（exit 1、stdout 不寫）。路徑型值若也需要出現在 stdout（例如 `profile_dir` 回顯、`SAMPLE_READ_FAILED` 的檔名 repr）：repr 形式照 Python surrogateescape 的 `'\udcff'` 跳脫輸出（ASCII，可比對），非 repr 的原文回顯走 Q-19 (c) 的 strict 規則。
4. `parse_args(root_spec, argv, help_width(...))`：`Exit` 時寫 stdout／stderr；若 `code == 2` 且 `json_boundary_command(argv)` 有值 → 再印 `{"error_code":"INVALID_ARGS","ok":false}`、mirror（context 為該命令）、回傳 1；否則回傳 `code`。
5. `dispatch`：依 `ns.path()` 平面 match；未命中 → `{"ok":false,"error_code":"INVALID_ARGS"}`、`mirror_err(None)`、回傳 2。

`run_rpc`（`cli.py:932-956`）：`resolve_endpoint_info` → `rpc_call(…, timeout=effective_timeout_s, retries=int(args.retries or 0))` → trace（`trace_enabled` 時）→ `print_json` → 非 ok 時 `format_err_line(context=method)` → `warn_version_mismatch` → 0／2。`ClientCrash` 與 `ResolveFailure::Crash` → stderr 一行 `serialwrap: internal error: <msg>`、stdout 不寫、回傳 1。`cmd submit` 的 `completion_regex_check` 為 `Crash`（OverflowError，Task 5）→ 同上 exit 1、stdout 空、不連 daemon；為 `Invalid` → Q-25。

`request_for` 的完整對照（key 順序即 wire 順序；`params` 未列者為 `{}`）：

| argv（selector 等以 `A`、`c` 代入） | method | params（wire 精確 bytes） |
|---|---|---|
| `device list` | `device.list` | `{}` |
| `device release --selector A` | `device.release` | `{"selector":"A","source":"cli","reason":null}` |
| `device release --selector A --source x --reason r` | `device.release` | `{"selector":"A","source":"x","reason":"r"}` |
| `device attach --selector A` | `device.attach` | `{"selector":"A","force":false}` |
| `device attach --selector A --force` | `device.attach` | `{"selector":"A","force":true}` |
| `session list` | `session.list` | `{}` |
| `session clear --selector A` | `session.clear` | `{"selector":"A"}` |
| `session bind --selector A --device-by-id /dev/x` | `session.bind` | `{"selector":"A","device_by_id":"/dev/x"}` |
| `session pin --selector A --profile p` | `session.pin` | `{"selector":"A","profile":"p"}` |
| `session unpin --selector A` | `session.unpin` | `{"selector":"A"}` |
| `session attach --selector A` | `session.attach` | `{"selector":"A"}` |
| `session self-test --selector A` | `session.self_test` | `{"selector":"A","timeout_s":2.0,"strict_human_lock":false}` |
| `session self-test --selector A --probe-timeout 3 --strict-human-lock` | `session.self_test` | `{"selector":"A","timeout_s":3.0,"strict_human_lock":true}` |
| `session activity --selector A` | `session.activity` | `{"selector":"A"}` |
| `session recover --selector A` | `session.recover` | `{"selector":"A","timeout_s":2.0,"force":false}` |
| `session recover --selector A --timeout 7 --force` | `session.recover` | `{"selector":"A","timeout_s":7.0,"force":true}` |
| `session console-attach --selector A` | `session.console_attach` | `{"selector":"A"}` |
| `session console-attach --selector A --label m` | `session.console_attach` | `{"selector":"A","label":"m"}` |
| `session console-attach --selector A --label ""` | `session.console_attach` | `{"selector":"A"}` |
| `session console-detach --selector A --client-id c` | `session.console_detach` | `{"selector":"A","client_id":"c"}` |
| `session console-list --selector A` | `session.console_list` | `{"selector":"A"}` |
| `session interactive-open --selector A` | `session.interactive_open` | `{"selector":"A","owner":"agent","timeout_s":60.0,"command":"","allow_attached":false}` |
| `session interactive-open --selector A --owner o --timeout 5 --command top --allow-attached` | `session.interactive_open` | `{"selector":"A","owner":"o","timeout_s":5.0,"command":"top","allow_attached":true}` |
| `session interactive-send --interactive-id i --data d` | `session.interactive_send` | `{"interactive_id":"i","data":"d","encoding":"plain"}` |
| `session interactive-status --interactive-id i` | `session.interactive_status` | `{"interactive_id":"i","screen_chars":2048}` |
| `session interactive-close --interactive-id i` | `session.interactive_close` | `{"interactive_id":"i"}` |
| `session log-start --selector A` | `session.log_start` | `{"selector":"A"}` |
| `session log-stop --selector A` | `session.log_stop` | `{"selector":"A"}` |
| `session log-status --selector A` | `session.log_status` | `{"selector":"A"}` |
| `alias list` | `alias.list` | `{}` |
| `alias set --session-id s --alias a` | `alias.set` | `{"session_id":"s","alias":"a"}` |
| `alias assign --by-id b --alias a` | `alias.assign` | `{"by_id":"b","alias":"a"}` |
| `alias assign --by-id b --alias a --profile p` | `alias.assign` | `{"by_id":"b","alias":"a","profile":"p"}` |
| `alias unassign --alias a` | `alias.unassign` | `{"alias":"a"}` |
| `cmd submit --selector A` | `command.submit` | `{"selector":"A","cmd":"","source":"agent","mode":"line","priority":10,"timeout_s":10.0}` |
| `cmd submit --selector A --cmd ls --source s --mode whatever --priority 3 --cmd-timeout 1.5 --expected-duration 9 --completion-regex DONE$` | `command.submit` | `{"selector":"A","cmd":"ls","source":"s","mode":"whatever","priority":3,"timeout_s":1.5,"expected_duration_s":9.0,"completion_regex":"DONE$"}` |
| `cmd submit --selector A --cmd-timeout inf --priority 99999999999999999999999` | `command.submit` | `{"selector":"A","cmd":"","source":"agent","mode":"line","priority":99999999999999999999999,"timeout_s":Infinity}` |
| `cmd status --cmd-id c` | `command.get` | `{"cmd_id":"c"}` |
| `cmd result-tail --cmd-id c` | `command.result_tail` | `{"cmd_id":"c","from_chunk":0,"limit":200}` |
| `cmd cancel --cmd-id c` | `command.cancel` | `{"cmd_id":"c"}` |
| `stream tail` | `result.tail` | `{"from_seq":0,"limit":200}` |
| `stream tail --com COM1` | `result.tail` | `{"from_seq":0,"limit":200,"selector":"COM1"}` |
| `stream tail --selector A --com B --from-seq 5 --limit 1` | `result.tail` | `{"from_seq":5,"limit":1,"selector":"A"}` |
| `log tail-raw` | `log.tail_raw` | `{"limit":200}` |
| `log tail-raw --from-seq 0` | `log.tail_raw` | `{"limit":200,"from_seq":0}` |
| `log tail-text --com COM2 --limit 5` | `log.tail_text` | `{"limit":5,"selector":"COM2"}` |
| `file push --selector A --local l --remote r` | `file.push` | `{"selector":"A","local_path":"l","remote_path":"r","chunk_size":512,"ack_mode":"auto","source":"agent"}` |
| `file push --selector A --local l --remote r --chunk-timeout 3 --ack-mode none --chunk-size 64` | `file.push` | `{"selector":"A","local_path":"l","remote_path":"r","chunk_size":64,"ack_mode":"none","source":"agent","chunk_timeout_s":3.0}` |
| `file pull --selector A --remote r` | `file.pull` | `{"selector":"A","remote_path":"r","ack_mode":"auto","source":"agent"}` |
| `file pull --selector A --remote r --local l --chunk-timeout 2` | `file.pull` | `{"selector":"A","remote_path":"r","ack_mode":"auto","source":"agent","local_path":"l","chunk_timeout_s":2.0}` |
| `wal export` | `wal.range` | `{"from_seq":0,"to_seq":0,"limit":1000}` |
| `wal reset` | `wal.reset` | `{}` |
| `wal current-seq` | `wal.current_seq` | `{}` |
| `mcu patterns` | `mcu.patterns` | `{}` |
| `mcu status` | `mcu.status` | `{}` |

`effective_timeout_s` 表：`session attach`／`recover`／`self-test`／`console-attach` 為 45.0；其餘 5.0；`--timeout 2` 時一律 2.0。

- [ ] **Step 1：寫失敗測試**

`crates/sw-cli/tests/rpc_params.rs`：

```rust
mod common;
use sw_cli::{argparse::{parse_args, ParseResult}, cli_spec::{root_spec, SpecDefaults}, commands::rpc_cmds::{request_for, effective_timeout_s}};

// 表格的唯一來源：tests/contract_blackbox/data/rpc_params.json（上表 55 列，格式 [{"argv":[...],"method":"…","params":"<wire JSON>"}]），
// Rust 單元測試與黑箱測試共用同一檔，避免兩份表漂移。
fn table() -> Vec<(Vec<String>, String, String)> {
    let p = common::data_root().join("tests/contract_blackbox/data/rpc_params.json");
    let v = sw_proto::pyjson::loads(&std::fs::read_to_string(p).unwrap()).unwrap();
    rows_from(&v)   // 同檔 helper：轉成 (argv, method, params)
}

fn ns(argv: &str) -> sw_cli::argparse::Namespace {
    let spec = root_spec(&SpecDefaults { version_line: "serialwrap t".into(), profile_dir: "/p".into() });
    match parse_args(&spec, &shell_words(argv), 78) { ParseResult::Ok(n) => n, _ => panic!("{argv}") }
}

#[test]
fn params_wire_exact() {
    let rows = table();
    assert_eq!(rows.len(), 55);
    for (argv, method, params) in rows {
        let (m, p) = request_for(&ns_argv(&argv)).unwrap_or_else(|| panic!("{argv:?}"));
        assert_eq!((m, sw_proto::pyjson::dumps(&p, &sw_proto::pyjson::RPC_WIRE)), (method.as_str(), params), "{argv:?}");
    }
}

#[test]
fn long_timeout_floor() {
    for (argv, want) in [("session attach --selector A", 45.0), ("session recover --selector A", 45.0),
                         ("session self-test --selector A", 45.0), ("session console-attach --selector A", 45.0),
                         ("file push --selector A --local l --remote r", 5.0), ("--timeout 2 session attach --selector A", 2.0)] {
        let n = ns(argv);
        let (m, _) = request_for(&n).unwrap();
        assert_eq!(effective_timeout_s(&n, m), want, "{argv}");
    }
}

#[test]
fn json_boundary_quirks() {
    use sw_cli::json_boundary_command as jb;
    let v = |s: &str| shell_words(s);
    assert_eq!(jb(&v("--socket connect")), Some("connect"));
    assert_eq!(jb(&v("--endpoint=connect")), Some("connect"));
    assert_eq!(jb(&v("-vv benches")), Some("benches"));
    assert_eq!(jb(&v("--timeout 3 session list")), None);
    assert_eq!(jb(&v("--bench=x connect")), Some("connect"));
}
```

`test_bb_rpc_params.py`：以同一張表 parametrize（表存成 `tests/contract_blackbox/data/rpc_params.json`），每列以 stubd 回 `{"ok":true,"version":"<cli version>"}`，斷言 `stubd.requests()[0] == b'{"id":1,"method":"<m>","params":<p>}\n'`、rc 0、stdout `{"ok":true,"version":"…"}`＋NL；另以 `-v` 斷言 trace 的 `timeout_s`。

`test_bb_exit_codes.py`：

| test | argv / stub | rc | stdout | stderr |
|---|---|---|---|---|
| `test_rpc_ok` | stub ok | 0 | JSON | 空 |
| `test_rpc_fail` | stub `{"ok":false,"error_code":"X"}` | 2 | JSON | `serialwrap: <m> failed: X` |
| `test_resolution_error` | `--bench nope session list` | 1 | `{"error_code":"BENCH_ENDPOINT_NOT_REMEMBERED",…}` | 鏡像 |
| `test_completion_regex_local` | `cmd submit --selector A --completion-regex ^` | 1 | `{"error_code":"INVALID_COMPLETION_REGEX","ok":false}` | 空；stub 收到 0 筆 |
| `test_completion_regex_too_long` | `--completion-regex` 1025 bytes | 1 | 同上 | 空 |
| `test_argparse_error` | `session` | 2 | 空 | usage＋error |
| `test_socket_connect_boundary` | `--socket connect` | 1 | `{"error_code":"INVALID_ARGS","ok":false}` | usage、error、`serialwrap: connect failed: INVALID_ARGS` |
| `test_help_version` | `--help`、`--version` | 0 | 文字 | 空 |
| `test_tcp_port_invalid` | env `SERIALWRAP_TCP_PORT=""` + `skill` | 1 | 空 | 非空 |
| `test_completion_regex_overflow` | `cmd submit --selector A --completion-regex 'a{99999999999}'` | 1 | 空 | 非空；stub 收到 0 筆 |
| `test_tcp_gai_before_timeout` | `--timeout -1 --endpoint tcp://no-such-host.invalid:1 device list` | 2 | `SOCKET_ERROR` JSON | 鏡像（Q-10） |
| `test_unix_bad_timeout` | `--timeout -1 --socket <stub> device list` | 1 | 空 | 非空；stub 收到 0 筆 |
| `test_non_utf8_socket_path`（`posix_only`） | `--socket $'<tmp>/\xff.sock' session list`（該路徑不存在） | 2 | `SOCKET_ERROR` JSON（message 不含路徑，Q-22） | 鏡像 |
| `test_non_utf8_endpoint_path`（`posix_only`） | `--endpoint $'<tmp>/\xff.sock' session list` | 2 | 同上 | 鏡像 |
| `test_non_utf8_selector_request`（`posix_only`） | `--socket <stub> session clear --selector $'\xff'` | 1 | 空 | 非空；stub 收到 0 筆請求行 |
| `test_non_utf8_selector_dead_socket`（`posix_only`） | `--socket <tmp>/nope.sock session clear --selector $'\xff'` | 2 | `SOCKET_ERROR` JSON | 鏡像（connect 失敗早於編碼失敗） |
| `test_non_utf8_bench_echo`（`posix_only`、`rust_only`） | `--bench $'\xff' session list`（Rust strict 規則，Q-19 (c) 已登記差異） | 1 | 空 | `serialwrap: internal error: argument is not valid UTF-8` |
| `test_non_utf8_bench_echo_strict_locale`（`posix_only`） | 同上但 env `PYTHONIOENCODING=utf-8:strict`（Python 端改為 strict；Rust 忽略此變數） | 1 | 空 | 非空 |

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-cli --test rpc_params`
Expected：FAIL，`not yet implemented: P3-T14`。黑箱對 Python PASS。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 新增資料面 RPC params、timeout 與 exit code 契約測試（紅燈）"`

- [ ] **Step 4：實作**

`request_for` 寫成一個平面 `match ns.path().as_slice()`，每支照 `cli.py:2491-2673` 依序 `pydict!` 插入；None／falsy 處理照 Q-24。`cmd submit` 先 `pyre::valid_completion_regex`（Q-25）。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-cli && cargo build -p sw-cli && SERIALWRAP_CLI_EXE=$PWD/target/debug/serialwrap SERIALWRAP_AB_LABEL=rust-1a python3 -m pytest -q tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider -k "rpc_params or exit_codes or output_contract or trace or endpoint or client or cli_help" -m "not needs_delegation"`
Expected：PASS。`needs_delegation` 列（委派群組的 `--help` 等）在 Task 16 Step 5 對 Rust 執行；此處被排除的筆數記入 Task 14 審查紀錄。`crates/sw-cli/tests/run_process.rs` 另驗：`run_process_with(&|_, _| panic!("boom"), …)` → 回傳 1、stderr 一行；`out` 為寫入即失敗的 writer → 1；`out` 為 flush 才失敗的 writer → 120。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作資料面 RPC 子命令、main 分派與 JSON 邊界"`

---

### Task 15：`event`／`profile test`／`skill`

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-15-event-profile-skill.md`
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-cli/src/commands/{event_cmd.rs,profile_cmd.rs,skill_cmd.rs}`、`crates/sw-cli/src/assets.rs`
- Test: `crates/sw-cli/tests/{event_cmd.rs,profile_cmd.rs,skill_cmd.rs}`、`tests/contract_blackbox/{test_bb_event.py,test_bb_profile.py,test_bb_skill.py}`、fixture `tests/contract_blackbox/data/profiles/`

**Interfaces:**
- Consumes：Task 5、6、11～14。
- Produces：

```rust
// event_cmd.rs（cli.py:965-1078）
pub const EVENT_CMD_METHOD: [(&str, &str); 10] = [("add","event.rule_set"),("rm","event.rule_delete"),("list","event.rule_list"),
    ("show","event.rule_get"),("enable","event.com_enable"),("disable","event.com_disable"),("status","event.com_status"),
    ("reset","event.reset"),("reload","event.reload"),("tail","event.tail")];
pub fn event_params(ctx: &CliCtx, ns: &Namespace) -> Result<sw_proto::pyjson::PyValue, String /* INTERNAL_ERROR message */>;
pub fn dispatch_event(ctx: &mut CliCtx, ns: &Namespace) -> i32;
// profile_cmd.rs（cli.py:1804-1905）
pub fn profile_test_check(pattern: &str, sample: &str) -> sw_proto::pyjson::PyDict;
pub fn last_nonblank_line(text: &str) -> Option<&str>;
pub fn bootloader_check(pattern: &str, last: Option<&str>) -> sw_proto::pyjson::PyDict;
pub fn run_profile_test(ctx: &mut CliCtx, ns: &Namespace) -> i32;
// assets.rs（唯一定義處；include_str! 的相對路徑以 crates/sw-cli/src/ 為基準，放到 commands/ 下會差一層）
pub const SKILL_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL.md");
pub const SKILL_WINDOWS_MD: &str = include_str!("../../../sw_core/assets/skill/SKILL_WINDOWS.md");
// skill_cmd.rs（只 use crate::assets::{SKILL_MD, SKILL_WINDOWS_MD}）
pub fn run_skill(ctx: &mut CliCtx, ns: &Namespace) -> i32;
```

`dispatch_event` 順序：`resolve_endpoint_info`（失敗 → exit 1，context 為真實 method）→ `add` 時讀 `--file`（`FsRead::read` → `decode_utf8_strict` → `universal_newlines` → `pyjson::loads`，任何錯誤 → `print_internal_error(context=method, message)`、exit 1）→ `rpc_call(retries=0)` → trace → `print_json` → `warn_version_mismatch` → `mirror_err(context=method)` → 0／2（Q-03、Q-31）。params：`list` → `{"selector":v|null,"owner":v|null}`；`status` → `{"selector":v|null}`；`reset` → `{"rule_id":v|null,"selector":v|null}`；`tail` → `{"rule_id","selector","n","since_ts"}`。

`profile test`：`load_profiles(profile_dir)`（`ConfigError` → stderr 一行、exit 1，Q-26）→ 找第一個 `profile_name` 相符的 template → 無則 `UNKNOWN_PROFILE`（`available` 為名稱集合排序）→ 讀 sample（`decode_utf8_replace`；錯誤 → `SAMPLE_READ_FAILED`，message 含檔名 repr）→ checks；pyre `CompileError::Re(e)` → `error` 欄位為 `e.to_string()`；`Recursion`、`Overflow` → exit 1、stdout 空（Python 只攔 `re.error`，`RecursionError`／`OverflowError` 為 traceback）；`Untranslatable` → `error` 欄位 `untranslatable: <構造>`（已登記殘餘差異 EVT-D-S15）。`matched_line` 依 code point 索引照 `cli.py:1820-1825`。sample 讀檔使用 universal newlines（Python 文字模式預設），再交給 regex。

`skill`：`--platform auto` → `caps.default_skill`；輸出經 `TextIo::out_str`（Windows 轉 CRLF，Q-05、Q-27），不加換行。

- [ ] **Step 1：寫失敗測試**

`crates/sw-cli/tests/profile_cmd.rs`：

```rust
use sw_cli::commands::profile_cmd::{bootloader_check, last_nonblank_line, profile_test_check};

fn j(d: &sw_proto::pyjson::PyDict) -> String { sw_proto::pyjson::dumps(&d.clone().into(), &sw_proto::pyjson::CLI_STDOUT) }

#[test]
fn matched_line_is_full_line() {
    assert_eq!(j(&profile_test_check("ogin:", "boot\nfoo login: \nbar")),
               r#"{"error":null,"matched":true,"matched_line":"foo login: ","pattern":"ogin:"}"#);
}
#[test]
fn empty_pattern_not_matched() {
    assert_eq!(j(&profile_test_check("", "x")), r#"{"error":null,"matched":false,"matched_line":null,"pattern":""}"#);
}
#[test]
fn invalid_regex_reports_python_message() {
    assert_eq!(j(&profile_test_check("(", "x")),
               r#"{"error":"missing ), unterminated subpattern at position 0","matched":false,"matched_line":null,"pattern":"("}"#);
}
#[test]
fn bootloader_uses_last_nonblank_line() {
    assert_eq!(last_nonblank_line("a\n=> \n  \n"), Some("=> "));
    assert_eq!(j(&bootloader_check("^=> $", Some("=> "))), r#"{"error":null,"matched":true,"matched_line":"=> ","pattern":"^=> $"}"#);
    assert_eq!(j(&bootloader_check("^=> $", None)), r#"{"error":null,"matched":false,"matched_line":null,"pattern":"^=> $"}"#);
}
```

`test_bb_profile.py`（fixture `data/profiles/a.yaml`、`b.yaml` 與 sample 檔）：

| test | argv | 斷言 |
|---|---|---|
| `test_success` | `profile test --profile op3-template --sample s.txt --profile-dir D` | rc 0；stdout 與 Python 位元組相同 |
| `test_unknown_profile_available_sorted` | `--profile nope` | rc 0；`error_code` UNKNOWN_PROFILE；`available` 只含最後一個 YAML 檔的 template（Q-26） |
| `test_sample_missing` | `--sample nope.txt` | rc 0；`{"error_code":"SAMPLE_READ_FAILED","message":"[Errno 2] No such file or directory: 'nope.txt'","ok":false}` |
| `test_default_profile_dir_from_env` | 不帶 `--profile-dir`，`SERIALWRAP_CONFIG_DIR=<tmp>/config` | `profile_dir` 為 `<tmp>/config/profiles` |
| `test_yaml_syntax_error_exit_1` | 壞 YAML | rc 1；stdout 空 |
| `test_crlf_sample` | sample 內容 `login:\r\n` | 與 Python 相同 |

`test_bb_event.py`：上述 params 全部列（stubd 斷言 wire）；`add --file` 含 `{"b":1,"a":[1.5,NaN],"z":"é"}` → wire 保留 key 順序與 `NaN`；`add --file` 內容為 `[1,2]` → 照送（CLI-12.04）；`--file missing.json` → rc 1、stdout `{"error_code":"INTERNAL_ERROR","message":"[Errno 2] No such file or directory: 'missing.json'","ok":false}`、stderr `serialwrap: event.rule_set failed: INTERNAL_ERROR: [Errno 2] No such file or directory: 'missing.json'`；壞 JSON（CRLF 檔，錯誤位置依 universal newline 計）；`--retries 3 event list` 對 close → stub 收 1 筆；`--bench nope event list` → rc 1、context `event.rule_list`。

`test_bb_skill.py`：`skill`、`skill --platform linux`、`skill --platform windows` 的 stdout 與 Python 位元組相同；POSIX 下 `skill` 等於 `SKILL.md` 原檔 bytes；Windows 下等於原檔 bytes 把 `\n` 換成 `\r\n`。

`crates/sw-cli/tests/skill_cmd.rs`：`assets::SKILL_MD.as_bytes() == std::fs::read(common::data_root().join("sw_core/assets/skill/SKILL.md"))`（兩檔皆驗），確保內嵌與原檔一致；`run_skill` 以 fake caps `default_skill=Windows` 時輸出 `SKILL_WINDOWS_MD`。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-cli --test event_cmd --test profile_cmd --test skill_cmd`
Expected：FAIL，`not yet implemented: P3-T15`。黑箱對 Python PASS。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 新增 event、profile test 與 skill 契約測試（紅燈）"`

- [ ] **Step 4：實作**

照 Interfaces 逐行移植 `cli.py:965-1112`、`1804-1905`。

- [ ] **Step 5：確認綠燈**

Run：`cargo test -p sw-cli && SERIALWRAP_CLI_EXE=$PWD/target/debug/serialwrap SERIALWRAP_AB_LABEL=rust-1a python3 -m pytest -q tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider -k "event or profile or skill"`
Expected：PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作 event 分派、離線 profile test 與內嵌 skill 輸出"`

---

### Task 16：Stage 1A 透明委派與 `mkbundle.sh --dev`

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p3-16-delegation.md`
**BLOCKED-OWNER:**
- Windows 互動 console 下委派的 Ctrl-C／Ctrl-Break 行為（`cap_interactive_desktop`），需擁有者桌面環境實測。
- 測試團隊 Windows 工作站上以正式安裝佈局（`%LOCALAPPDATA%\Programs\serialwrap\`）執行委派，屬 Stage 1B 安裝流程，本 Task 只驗 dev bundle。

**Files:**
- Modify: `crates/sw-cli/src/delegate.rs`
- Create: `scripts/rust/mkbundle.sh`
- Test: `crates/sw-cli/tests/delegate_route.rs`、`tests/contract_blackbox/test_bb_delegation.py`、`tests/contract_blackbox/data/delegate_probe.py`、`tests/test_rust_mkbundle.py`

**Interfaces:**
- Consumes：Task 7（`parse_manifest`、`parse_current_release`）、Task 9（`ProcessCtl::exec_or_wait`、`SysEnv::current_exe`／`realpath`、`FsRead`）、Task 10（`DELEGATED_GROUPS`、`Route`、`classify`）、Task 11（`print_internal_error`）；P1 T18 的 `tree_sha256` 演算法與 `contracts/golden/tree_sha256.ndjson`、`contracts/release_manifest.schema.json`。
- Produces：

```rust
#[derive(Clone, Debug, PartialEq)]
pub enum DelegateError { Unavailable(String), Recursion }
pub fn argv_sha256(argv_tail: &[std::ffi::OsString]) -> String;
// sha256(各元素 OsStr::as_encoded_bytes() 以 "\0" 連接)；兩平台同一寫法、不用 std::os::*（cfg 邊界）。
// 此值只在 Rust 實例之間比對（遞迴偵測），任何確定性編碼都夠用，不需跨平台相等
pub fn check_recursion(depth_raw: Option<&str>, sha_raw: Option<&str>, my_sha: &str) -> Result<u32 /* incoming depth */, DelegateError>;
pub struct DelegateTarget { pub argv: Vec<std::ffi::OsString>, pub manifest_path: std::path::PathBuf }
pub fn resolve_target(p: &sw_pal::ClientPlatform, argv_tail: &[std::ffi::OsString]) -> Result<DelegateTarget, DelegateError>;
pub fn run_delegate(ctx: &mut CliCtx, argv_tail: &[std::ffi::OsString], group: &str) -> i32;   // 只在錯誤時返回 1
```

規則（arch §7.4 細化）：
- `classify`（Task 10 實作，規則定義於此）：輸入為 `OsString`，只掃 root 層。值型選項（含唯一前綴縮寫）：`--socket`、`--endpoint`、`--bench`、`--timeout`、`--retries`，`--opt=v` 吃 1 個 token、否則吃 2 個；其他以 `-` 開頭的 token 吃 1 個（`-v`、`-vv`、`--verbose`、`-h`、`--version`、歧義前綴、未知選項）；`--` 之後的第一個 token 視為 group；第一個非選項 token 為 group，屬 `DELEGATED_GROUPS` → `Delegate`，否則 `Native`。這樣凡是含委派 group 的 argv（包括 root 層參數錯誤），一律交給 Python 產生完全相同的輸出。
- manifest 定位（兩平台同一演算法，無 cfg）：`exe = current_exe()`；若 `<exe 所在目錄>/current-release.json` 存在 → `manifest = <exe 所在目錄>/../<dir>/release-manifest.json`；否則 `manifest = dirname(dirname(realpath(exe)))/release-manifest.json`。
- entry：`components.python_cli_delegate.entry`；`entry[0]` 為相對路徑時以 manifest 所在目錄為基準以**字串 join** 轉絕對（**不得** `canonicalize`／`realpath`：venv 的 `python3` 是 symlink，解析成 `/usr/bin/python3.12` 後 `-I -m sw_core.cli` 會 `ModuleNotFoundError`）；轉換後必須是存在的檔案；其餘元素原樣。最終 argv = entry ＋ 使用者 `argv[1:]`（`OsString` 原樣）。Windows `CreateProcessW` 一律以該絕對路徑作 `lpApplicationName`（不依命令列首段做 PATH 搜尋）。Windows dev bundle（Task 17 建立）的 entry 為 `["py\\python.exe","-I","-B","-m","sw_core.cli"]`：`py\\` 是 CPython **3.12.3** embeddable（與 arch §10.1 同佈局；不用 venv 的 `py\\Scripts\\python.exe`，因 Windows `py -3.12` 為 3.12.10，argparse 方言不同）。
- 遞迴：`depth_raw` 為 None → 0；非 ASCII 十進位 → `Recursion`；`depth >= 2` → `Recursion`；`sha_raw == my_sha` → `Recursion`；否則子行程 env 加三個變數（§0.7）：`SERIALWRAP_DELEGATE_DEPTH=<depth+1>`、`SERIALWRAP_DELEGATE_ARGV_SHA=<my_sha>`、`SERIALWRAP_RELEASE_MANIFEST=<manifest 絕對路徑>`（讓 Python `find_release_manifest()` 在 `python -m sw_core.cli` 下仍找到 bundle manifest，委派後的 `doctor` 才會做 `version_pairing`）。
- 委派只執行一次：`exec_or_wait` 失敗（`exec failed`）時直接報錯，不重試、不改走 PATH、不換 entry。
- 錯誤輸出：stdout `{"error_code":"INTERNAL_ERROR","message":"delegation target unavailable: <reason>","ok":false}`（`reason` 為 `release manifest not found: <path>`、`invalid release manifest: <detail>`、`delegate entry not found: <path>`、`exec failed: <py_message>` 之一）或 message `delegation recursion detected`；stderr `serialwrap: <group> failed: INTERNAL_ERROR: <message>`；exit 1。
- 委派前不寫任何 bytes；不加逾時；不重試。

`scripts/rust/mkbundle.sh --dev --out <dir> [--target <triple>] [--profile debug|release]`：
1. `cargo build -p sw-cli [--release] --target <triple>`。
2. `V=$(tr -d ' \n' < VERSION)`；建 `<out>/versions/$V/bin/`，複製 `serialwrap`。
3. `python3 -m venv --system-site-packages <out>/versions/$V/py`；在其 `site-packages` 寫 `serialwrap_repo.pth`（內容為 repo 絕對路徑），使 `-I -m sw_core.cli` 可 import（`-I` 停用 user site，PyYAML 由系統 site 提供，2026-10-15 已確認 `/usr/lib/python3/dist-packages/yaml`）。
4. `release-manifest.json`（`DISK_STABLE` 的 Indent(2) 變體、sort_keys、尾端換行）：`schema_version:1`、`bundle_version:$V`、`stage:"1A"`、`created_at`、`target`、`git_revision`（`git rev-parse HEAD`）、`contract_revision`（`git rev-parse HEAD:contracts`）、`components.cli{implementation:"rust",version,git_revision,path:"bin/serialwrap",sha256}`、`components.python_cli_delegate{entry:["py/bin/python3","-I","-B","-m","sw_core.cli"],tree_sha256,runtime_python_version}`、`components.daemon{implementation:"python",version,git_revision,entry:["py/bin/python3","-I","-B","<repo>/serialwrapd.py"],launcher:{path,sha256},runtime:{path:"py/bin/python3",python_version,sha256},package:{name:"serialwrap",version:$V,wheel_sha256:null,tree_sha256}}`、`pairing{rule:"exact",cli_version:$V,daemon_version:$V}`。dev bundle 沒有 Rust launcher 與 wheel：`launcher` 指向 `<repo>/serialwrapd.py` 並記其 sha256、`wheel_sha256` 為 `null`，schema 須允許並在 `description` 註明「dev bundle」；若 P1 schema 不允許，向 P1 提修正而不是省略欄位。`runtime.sha256` 為 `realpath(py/bin/python3)` 的 sha256。`python_version` 由 `py/bin/python3 -c 'import platform;print(platform.python_version())'` 取得，並必須等於 `contracts/cli_help/_meta.json` 的 `python_version`（3.12.3），不符即 exit 1。
   兩個 `tree_sha256` 都**直接呼叫 P1 的實作**：`py/bin/python3 -I -c 'import sw_core.release_manifest as m; print(m.package_tree_sha256())'`（`sw_core` 子樹全部檔案、排除 `__pycache__` 與 `*.pyc`、relpath 以 `sw_core/` 起頭、位元組序排序），不得另寫 `sw_core/**/*.py` 版本；mkbundle 自測先以 `contracts/golden/tree_sha256.ndjson` 驗 P1 函式。
5. 建 `<out>/bin/serialwrap` → `../versions/$V/bin/serialwrap` 的 symlink（驗 realpath 定位）。
6. 以 `jsonschema` 依 `contracts/release_manifest.schema.json` 驗證 manifest，失敗 exit 1；schema 缺檔 exit 1（訊息 `P1 contracts 缺少`），不得跳過。

- [ ] **Step 1：寫失敗測試**

`crates/sw-cli/tests/delegate_route.rs`（`classify_table` 由 Task 10 建立，表格保留於此處作為唯一定義；本 Task 追加 `recursion_rules` 與 `argv_sha_stable`）：

```rust
use sw_cli::delegate::{argv_sha256, check_recursion, classify, DelegateError, Route};

fn v(s: &str) -> Vec<std::ffi::OsString> { s.split_whitespace().map(std::ffi::OsString::from).collect() }

#[test]
fn classify_table() {   // Task 10
    let d = |g| Route::Delegate { group: g };
    let cases: &[(&str, Route)] = &[
        ("", Route::Native), ("session list", Route::Native), ("session console-attach --selector A", Route::Native),
        ("daemon status", d("daemon")), ("--socket daemon session list", Route::Native),
        ("--sock x service status", d("service")), ("--socket=x remote", d("remote")),
        ("-vv setup", d("setup")), ("-- doctor", d("doctor")), ("-h", Route::Native),
        ("--version daemon", d("daemon")), ("--timeout -1 connect x", d("connect")), ("--v benches", d("benches")),
        ("--bench", Route::Native), ("--unknown supervision-mode", d("supervision-mode")),
        ("--retries=2 connect", d("connect")), ("--socket connect", Route::Native), ("--endpoint=connect", Route::Native),
        ("--ret 1 daemon start", d("daemon")), ("profile test --profile p", Route::Native),
    ];
    for (argv, want) in cases { assert_eq!(classify(&v(argv)), *want, "{argv:?}"); }
}

#[test]
fn recursion_rules() {
    assert_eq!(check_recursion(None, None, "s"), Ok(0));
    assert_eq!(check_recursion(Some("1"), Some("other"), "s"), Ok(1));
    assert_eq!(check_recursion(Some("2"), None, "s"), Err(DelegateError::Recursion));
    assert_eq!(check_recursion(Some("0"), Some("s"), "s"), Err(DelegateError::Recursion));
    assert_eq!(check_recursion(Some("x"), None, "s"), Err(DelegateError::Recursion));
    assert_eq!(check_recursion(Some("１"), None, "s"), Err(DelegateError::Recursion));
}

#[test]
fn argv_sha_stable() {
    let a = argv_sha256(&v("setup --user x"));
    assert_eq!(a.len(), 64);
    assert_eq!(a, argv_sha256(&v("setup --user x")));
    assert_ne!(a, argv_sha256(&v("setup --user y")));
    assert_ne!(argv_sha256(&v("a b")), argv_sha256(&v("ab")));   // 分隔符生效
}
```

`tests/contract_blackbox/data/delegate_probe.py`（stdlib；作為 manifest entry 時印出接收到的一切）：

```python
import json, os, sys
if os.environ.get("PROBE_COUNTER"):                     # 每次被執行就追加一行，用來驗「委派恰執行一次」
    with open(os.environ["PROBE_COUNTER"], "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
data = sys.stdin.buffer.read() if os.environ.get("PROBE_READ_STDIN") == "1" else b""
sys.stdout.write(json.dumps({"argv": sys.argv[1:], "depth": os.environ.get("SERIALWRAP_DELEGATE_DEPTH"),
                             "sha": os.environ.get("SERIALWRAP_DELEGATE_ARGV_SHA"), "stdin": data.decode("utf-8", "replace"),
                             "manifest": os.environ.get("SERIALWRAP_RELEASE_MANIFEST"),
                             "pid": os.getpid()}, ensure_ascii=False) + "\n")
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
```

`tests/contract_blackbox/test_bb_delegation.py`：

```python
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from bb_harness import cli_prefix, is_rust, python_baseline_prefix, repo_root, run_cli

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc, pytest.mark.needs_delegation]

# A：兩種 artifact 都必須產生與 Python 完全相同的 rc／stdout／stderr
SAME_AS_PYTHON = [
    ["supervision-mode"],
    ["connect"],
    ["benches"],
    ["remote", "status"],
    ["setup", "--help"],
    ["daemon", "--help"],
    ["--timeout", "abc", "service", "status"],
    ["-h", "daemon"],
]


@pytest.mark.parametrize("argv", SAME_AS_PYTHON, ids=lambda a: " ".join(a))
def test_delegated_output_identical_to_python(env, argv):
    py = subprocess.run([*python_baseline_prefix(), *argv], env=env, capture_output=True, check=False)
    r = run_cli(argv, env=env)
    assert (r.rc, r.stdout, r.stderr) == (py.returncode, py.stdout, py.stderr)


def test_daemon_status_via_stub(env, stubd):
    s = stubd({"methods": {"health.status": [{"reply": {"ok": True, "pid": 1, "version": "x"}}]}})
    py = subprocess.run([*python_baseline_prefix(), "--socket", s.endpoint, "daemon", "status"],
                        env=env, capture_output=True, check=False)
    r = run_cli(["--socket", s.endpoint, "daemon", "status"], env=env)
    assert (r.rc, r.stdout) == (py.returncode, py.stdout)


# B：Rust-only 委派機制
@pytest.mark.rust_only
def test_missing_manifest(tmp_path, env):
    exe = tmp_path / "serialwrap"
    exe.write_bytes(open(cli_prefix()[-1], "rb").read()); exe.chmod(0o755)
    r = subprocess.run([str(exe), "doctor"], env=env, capture_output=True, check=False)
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["error_code"] == "INTERNAL_ERROR"
    assert out["message"].startswith("delegation target unavailable: release manifest not found: ")
    assert r.stderr.decode().startswith("serialwrap: doctor failed: INTERNAL_ERROR: delegation target unavailable")


@pytest.mark.rust_only
@pytest.mark.parametrize("exit_code", [0, 37, 1])
def test_probe_receives_argv_env_stdin_and_exit_code(probe_bundle, env, tmp_path, exit_code):
    counter = tmp_path / "count"
    e = dict(env, PROBE_READ_STDIN="1", PROBE_EXIT=str(exit_code), PROBE_COUNTER=str(counter))
    r = run_cli(["--socket", "/x y", "setup", "--user", "é", "--", "-z"], env=e, stdin=b"hello")
    assert r.rc == exit_code
    got = json.loads(r.stdout)
    assert got["argv"] == ["--socket", "/x y", "setup", "--user", "é", "--", "-z"]
    assert got["depth"] == "1" and len(got["sha"]) == 64 and got["stdin"] == "hello"
    assert got["manifest"] == str(probe_bundle.manifest)
    assert len(counter.read_text().splitlines()) == 1        # 委派恰執行一次（非零 exit 也不重試）


@pytest.mark.rust_only
def test_exec_failure_runs_nothing_and_does_not_fall_back(broken_entry_bundle, env, tmp_path):
    # entry[0] 存在但不可執行（chmod 000）→ exec failed；不得改走 PATH 或重試
    marker = tmp_path / "marker"
    fake = tmp_path / "fakebin"; fake.mkdir()
    (fake / "python3").write_text(f"#!/bin/sh\ntouch {marker}\n"); (fake / "python3").chmod(0o755)
    r = run_cli(["doctor"], env=dict(env, PATH=f"{fake}:{env['PATH']}"))
    assert r.rc == 1 and json.loads(r.stdout)["message"].startswith("delegation target unavailable: exec failed: ")
    assert not marker.exists()


@pytest.mark.rust_only
def test_recursion_detected_when_entry_points_to_self(self_loop_bundle, env):
    r = run_cli(["doctor"], env=env, timeout_s=20)
    assert r.rc == 1
    assert json.loads(r.stdout)["message"] == "delegation recursion detected"


@pytest.mark.rust_only
def test_never_searches_path(probe_bundle, tmp_path, env):
    fake = tmp_path / "fakebin"; fake.mkdir()
    marker = tmp_path / "marker"
    for name in ("python3", "python", "serialwrap"):
        p = fake / name
        p.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n"); p.chmod(0o755)
    r = run_cli(["doctor"], env=dict(env, PATH=f"{fake}:{env['PATH']}"))
    assert r.rc == 0 and not marker.exists()


@pytest.mark.rust_only
@pytest.mark.posix_only
def test_exec_replaces_process_and_sigterm_reaches_child(probe_bundle, env):
    e = dict(env, PROBE_READ_STDIN="1")
    p = subprocess.Popen([*cli_prefix(), "doctor"], env=e, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    time.sleep(0.5)
    cmdline = open(f"/proc/{p.pid}/cmdline", "rb").read().split(b"\0")
    assert any(b"delegate_probe.py" in a for a in cmdline)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=5) == -signal.SIGTERM


@pytest.mark.rust_only
def test_no_output_before_delegation(probe_bundle, env):
    r = run_cli(["-v", "--socket", "/nope", "daemon", "status"], env=dict(env, SERIALWRAP_LOG_LEVEL="DEBUG"))
    assert r.stderr == b""          # probe 不寫 stderr；Rust 端在委派前不得輸出 trace 或任何字元
```

`conftest.py` 另加 fixtures：`probe_bundle`（以 `mkbundle.sh --dev` 建 bundle 後把 manifest 的 `python_cli_delegate.entry` 改成 `["py/bin/python3","-I","<repo>/tests/contract_blackbox/data/delegate_probe.py"]`，並把 `SERIALWRAP_CLI_EXE` 指向 bundle 的 `bin/serialwrap`；屬性 `.manifest` 為 manifest 絕對路徑）、`self_loop_bundle`（entry 改成 `["bin/serialwrap"]`）、`broken_entry_bundle`（entry[0] 指向 bundle 內 `chmod 000` 的檔案）。

`tests/test_rust_mkbundle.py`（`posix_only`、`process_rpc`）：建 bundle 後斷言 manifest 通過 `contracts/release_manifest.schema.json`、`bin/serialwrap` 為 symlink、`versions/<V>/py/bin/python3 -I -m sw_core.cli --version` 輸出 `serialwrap <V>`、`components.cli.sha256` 等於實際檔案 sha256、兩個 `tree_sha256` 等於 `sw_core.release_manifest.package_tree_sha256()`（P1）、`components.daemon.{git_revision,launcher.sha256,runtime.sha256,package.tree_sha256}` 皆非空、`runtime.python_version == "3.12.3"`；以 bundle 執行委派的 `doctor` 時輸出含 `version_pairing` 項目（P1 T18 在偵測到 manifest 時才附加）。cargo 不存在時依 `SW_REQUIRE_CARGO` 規則（Task 1）SKIP 或 FAIL。

- [ ] **Step 2：確認紅燈**

Run：`cargo test -p sw-cli --test delegate_route && python3 -m pytest -q tests/test_rust_mkbundle.py`
Expected：Rust FAIL（`todo!`）；mkbundle 測試 FAIL（腳本不存在）。`test_bb_delegation.py` 對 Python：A 組 PASS、B 組 SKIP（rust_only）。

- [ ] **Step 3：commit 測試**

`git commit -m "test(sw-cli): 新增 Stage 1A 委派路由、遞迴防護與保真測試（紅燈）"`

- [ ] **Step 4：實作**

照規則完成 `delegate.rs` 與 `mkbundle.sh`；`run` 在委派前不得建立任何 stdout 緩衝寫入（Task 14 的流程第 3 步早於任何輸出）。

- [ ] **Step 5：確認綠燈**

Run：
```bash
cargo test -p sw-cli --test delegate_route
scripts/rust/mkbundle.sh --dev --out target/dev-bundle
SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1a \
  python3 -m pytest -q tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider -m needs_delegation
python3 -m pytest -q tests/test_rust_mkbundle.py
```
Expected：全部 PASS（`-m needs_delegation` 涵蓋 `test_bb_delegation.py` 全部與 Task 14 排除的 `test_bb_cli_help.py` 委派列）。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(sw-cli): 實作 manifest 固定入口的透明委派與遞迴防護"`

---

### Task 17：Windows 原生 interop 執行（win-runner／win-test／win-blackbox、匯入 DLL 與 100 次啟動檢查）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p3-17-windows-interop.md`（frontmatter `type: change`、`scope: rust-cli`）
**BLOCKED-OWNER:**
- 正式 gate 的 Windows 原生 build／link 必須在擁有者 Windows 自架 runner（`stable-x86_64-pc-windows-gnu` host toolchain）執行；本 Task 的 interop 報告只算開發迴圈證據。
- 匯入 DLL 檢查：本 Task 交付 `check_win_imports.py` 並對 interop 產物執行（開發證據）；正式結果以自架 runner 原生產物為準。
- Windows COM 證據（OD-08，見下方「Windows COM 證據」）：OD-15 起使用擁有者 host 已通過 Spike 1 基本資格的 com0com pair `COM20:COM21`（可執行 gate）。
- 端點防毒（第三方企業 EDR）在 100 次連續 CLI 執行期間無新增攔截的查證，需擁有者提供 EDR 紀錄；與「不新增解壓執行檔或目錄」（規格 §1.2，本 Task 以快照差分自行驗證）分開記錄、各自判定。
- Windows CPython 3.12.3 embeddable（§3 B8）：需可下載 python.org 檔案或由擁有者放到 `%LOCALAPPDATA%\sw-rust-test\py-3.12.3\`；缺少時 Windows cli_help 與委派 A/B 判「證據不足」。

**Files:**
- Create: `scripts/rust/win-runner.sh`、`scripts/rust/win-test.sh`、`scripts/rust/win-blackbox.ps1`、`scripts/rust/win-blackbox.sh`、`scripts/rust/check_win_imports.py`、`scripts/rust/win-startup-100.ps1`、`scripts/rust/win-python-3.12.3.sha256`
- Create: `scripts/rust/cargo_l1_results.py`（R5-F3：把 cargo libtest 的逐案結果轉成 P2 Task 13 `l1-results.v1`；Linux 原生與 `win-test.sh` 共用）、`tests/test_rust_cargo_l1_results.py`、`tests/fixtures/libtest_sample/`（`--list` 與執行輸出樣本）
- Modify: `tests/ab_env.py`（`AB_KEYS` 加 `SW_BASELINE_PYTHON`）、`tests/contract_blackbox/bb_harness.py`（Windows `python_baseline_prefix()`／`python_baseline_interpreter()` 在 `SW_BASELINE_PYTHON` 有值時改用它）
- Test: `tests/test_rust_win_scripts.py`、`tests/test_rust_check_win_imports.py`

**Interfaces:**
- Consumes：Task 1（`data_root()` 與 §0.7 的 runner／linker env 注入規則）、Task 2 harness、Task 16 dev bundle 結構。
- Produces：
  - `scripts/rust/win-runner.sh <exe> [args…]`：照 arch §9.4 七步驟；exit code 原樣；`SW_WIN_KEEP=1` 保留 staging；`SW_WIN_TIMEOUT` 預設 900；`SW_WIN_EXTRA_FILES`（以 `:` 分隔）一併複製。**測試資料 staging**：在 staging 建 `data\`，複製 `contracts\golden\`、`contracts\cli_help\`、`contracts\{error_codes,session_last_errors}.yaml`、`contracts\cli_schema.json`、`contracts\rpc_schema.json`、`contracts\release_manifest.schema.json`、`tests\contract_blackbox\data\`、`sw_core\assets\skill\`、`sw_core\assets\profiles\`、`VERSION`，並設 `SW_TEST_DATA_DIR=<staging>\data`（WSLENV 以 `/p` 轉換路徑）。
  - `scripts/rust/win-test.sh [--caps r1,r2] [--package P]… [-- cargo test args]`：照 arch §9.4；偵測 WSL 後 export `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER=<repo>/scripts/rust/win-runner.sh`、`CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc`（不在 WSL 時不設，原生 Windows runner 直接 `cargo test`）；先跑 `check_cfg_boundary.py`；報告 `target/win-test-report/<run_id>.json`：`{"run_id","exes":[{"exe","caps","passed","failed","ignored","exit"}],"overall":"pass"|"fail","evidence_insufficient":[<cap>…],"l1_results":"<相對路徑>"}`；`l1_results` 指向同目錄以 `cargo_l1_results.py` 產生的 `l1-results.v1`（`platform:"windows"`），計數欄只作摘要，**出口判定一律以 `l1_results` 對凍結 nodeid 清單判定（P2 `l1_judge`），不得以 `passed>0`／`failed==0` 判定**（R5-F3）。
  - `scripts/rust/cargo_l1_results.py [--platform linux|windows] [--runner <win-runner.sh>] [--list-only] --out <json> -- <cargo test args…>`（R5-F3）：`cargo test --no-run --message-format=json <args>` 取每個 test executable 與其 `target.name`（crate 取 `package_id` 的套件名）；逐一執行 `<exe> --list --format terse`（加 `--ignored` 再列一次以標記 ignored 測試）得 `collected`，nodeid＝`cargo:<crate>::<target.name>::<test name>`；再執行 `<exe>`（只用 stable 旗標，不用 `-Z unstable-options --format json`）並解析 libtest 行 `test <name> ... ok|FAILED|ignored`，產生 `results`（`ok`→`passed`、`FAILED`→`failed`、`ignored`→`skipped`＋`skip_kind:"capability"`（`cap_<res>__` 前綴）或 `"other"`；`--caps` 指定的 `cap_<res>__*` 以 `--ignored` 執行後依實際結果記錄）、`exit_code`＝所有 exe exit 的最大值（任一 exe 被 timeout 殺掉記 124）、`finished`＝全部 exe 都跑完；同一 nodeid 跨 exe 重複時原樣保留兩列（由 `l1_judge` 判 `duplicate`）；`--list-only` 只輸出 `collected`（供 P2 `regen_lock.py --l1-add-only --collect`）。exe 編譯失敗 → `exit_code` 非 0、`finished:false`，不得產生空 `results` 的「成功」報告。
  - `scripts/rust/check_win_imports.py <exe>…`：以 `x86_64-w64-mingw32-objdump -p` 取 `DLL Name:`，allowlist（不分大小寫）：`KERNEL32.dll`、`ADVAPI32.dll`、`WS2_32.dll`、`ntdll.dll`、`bcryptprimitives.dll`、`bcrypt.dll`、`USERENV.dll`、`msvcrt.dll`、`api-ms-win-crt-*.dll`、`api-ms-win-core-*.dll`；明確禁止 `libgcc_s_seh-1.dll`、`libwinpthread-1.dll`、`libstdc++-6.dll`；輸出 JSON `{"exe","imports":[…],"unexpected":[…],"forbidden":[…],"ok"}`，任一 `ok:false` exit 1。
  - `scripts/rust/win-startup-100.ps1 -Exe <win path> -OutDir <win path>`：執行前對 `%TEMP%`、exe 所在目錄、`%LOCALAPPDATA%` 做快照（遞迴列出路徑、大小、`LastWriteTimeUtc`，排除本腳本輸出目錄）；連續 100 次 `& $Exe --version`，逐次記錄開始／結束時間戳、exit code、stdout sha256；執行後再快照並差分，新增的 `*.exe`、`*.dll` 或目錄列入 `new_artifacts`；輸出 `startup-100.json`（`{"runs":[…],"exe_sha256","new_artifacts":[…],"ok"}`；`ok` 只在 100 次 exit 0、輸出一致且 `new_artifacts` 為空時為 true）。EDR 紀錄比對另列 B4，不由本腳本判定。
  - `scripts/rust/win-blackbox.ps1 -RustExe <win path> -RepoSnapshot <win path> -OutDir <win path> -Python3123 <dir> [-ComPair COMa:COMb] [-Label rust-1a-win]`：在 `%LOCALAPPDATA%\Temp\sw-rust-test\<run_id>\` 建 staging（repo 子集：`serialwrap.py`、`serialwrapd.py`、`sw_core\`、`tests\contract_blackbox\`、`tests\ab_env.py`、`tests\resource_report.py`、`tests\resource_report_plugin.py`、`realhw\deployed_content.py`（L1 報告 plugin 與其依賴，R5 審查後續）、`VERSION`、`contracts\`）；`-Python3123` 指向 CPython 3.12.3 embeddable（sha256 與 `win-python-3.12.3.sha256` 相符，否則中止），改寫其 `python312._pth` 加入 staging repo 與 `py -3.12` 的 site-packages（PyYAML 為 3.12 ABI，跨 3.12.x 相容）；建 Windows dev bundle（`bin\serialwrap.exe`、`bin\current-release.json`＝`{"bundle_version":"<V>","dir":"versions\\<V>"}`、`versions\<V>\release-manifest.json`、`versions\<V>\py\`＝上述 3.12.3 embeddable 的複本，entry `["py\\python.exe","-I","-B","-m","sw_core.cli"]`）；依序執行 A（`SERIALWRAP_CLI_EXE=<staging>\serialwrap.py`、`SW_BASELINE_PYTHON=<3.12.3>\python.exe`、label `python-win`）與 B（`SERIALWRAP_CLI_EXE=<bundle>\bin\serialwrap.exe`、label `rust-1a-win`）的 `py -3.12 -m pytest tests\contract_blackbox -m "core_portable or windows_native" --junitxml=<a|b>.xml`（A／B 的 selector argv 逐字等於凍結 `s1a_blackbox` Windows selector，只有 JUnit 輸出路徑不同；不帶 `--confcutdir`、`-p no:cacheprovider` 或其他額外 pytest 旗標；pytest 本身可在 3.12.10 執行，受測 Python 基線與委派目標固定 3.12.3）；A 結束後依 Task 18 的釋放規則確認 throwaway daemon 已結束才跑 B；B 輪另帶 `SW_RESOURCE_REPORT=<staging>\l1\windows\blackbox\resource-report.json`。本命令調整理由：lock selector 為權威，R5-F3。B 輪產生的 junit、`l1\` 與 `-ComPair` 證據複製到 `-OutDir`（`<OutDir>\l1\windows\blackbox\`）。缺 `-Python3123` 時照跑其餘案例，但在輸出加 `"dialect_runtime_missing": true`，比較器把 `test_bb_cli_help`／`test_bb_delegation` 判「證據不足」。
  - `scripts/rust/win-blackbox.sh [--no-compare]`：WSL 端包裝：`cargo build -p sw-cli --release --target x86_64-pc-windows-gnu`（linker 以 env 注入）→ `check_win_imports.py` → 呼叫 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w scripts/rust/win-blackbox.ps1)" …` → `win-startup-100.ps1` → 未帶 `--no-compare` 時以 `scripts/rust/ab_compare.py`（Task 18）比較兩個 junit，輸出 `target/win-blackbox/<run_id>/report.json`。Task 17 階段 `ab_compare.py` 尚未交付，只能以 `--no-compare` 執行；完整比較在 Task 19 出口 gate。

**Windows COM 證據（OD-08；擁有者自架 runner）**：`win-blackbox.ps1 -ComPair COMa:COMb` 額外執行：(1) `uart-stdio-bridge selftest --endpoint win-serial --com-pair COMa:COMb --out <OutDir>\com-selftest`（P2 Task 8），必須 `verdict: PASS`；(2) 以 Python daemon（`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COMa`，OD-11 隔離實例）開 COMa、`uart-stdio-bridge run --endpoint win-serial --com COMb` 接 DUT 模擬器，Rust CLI 依序執行 `session list`（輪詢至 `READY`）、`cmd submit --cmd "echo hi"`＋`cmd status`、`device release`、`device attach`、再次輪詢 `READY`，並跑 P1 的 `windows_com` 案例（`SW_TEST_COM_PAIR=COMa:COMb py -3.12 -m pytest -m windows_com tests/`）；證據（命令列、stdout、daemon log、bridge `wire.ndjson`、COM 名稱與 transport_mode）寫入 `<OutDir>\windows-com\`。未帶 `-ComPair` 時輸出 `"windows_com": "BLOCKED-OWNER"`，不得標 PASS。

- [ ] **Step 1：寫失敗測試**

`tests/test_rust_win_scripts.py`：

```python
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIN_EXE = ROOT / "target" / "x86_64-pc-windows-gnu" / "debug" / "serialwrap.exe"
pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc,
              pytest.mark.skipif(shutil.which("cmd.exe") is None or shutil.which("cargo") is None,
                                 reason="需要 WSL interop 與 cargo（證據不足，非 PASS）")]
XENV = {"CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER": "x86_64-w64-mingw32-gcc"}


@pytest.fixture(scope="module")
def win_exe():
    subprocess.run(["cargo", "build", "-p", "sw-cli", "--target", "x86_64-pc-windows-gnu"], cwd=ROOT, check=True,
                   env={**os.environ, **XENV})
    return WIN_EXE


def _runner(*args, env=None):
    return subprocess.run([str(ROOT / "scripts/rust/win-runner.sh"), *map(str, args)],
                          capture_output=True, env={**os.environ, **(env or {})}, check=False)


def test_runner_version_uses_crlf(win_exe):
    r = _runner(win_exe, "--version")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert r.returncode == 0
    assert r.stdout == f"serialwrap {version}\r\n".encode()     # Q-05


def test_runner_propagates_nonzero_exit(win_exe):
    r = _runner(win_exe, "session")                              # argparse 錯 → 2
    assert r.returncode == 2
    assert b"\r\n" in r.stderr and b"usage: serialwrap session" in r.stderr


def test_runner_forwards_serialwrap_env(win_exe):
    r = _runner(win_exe, "--endpoint", "tcp://127.0.0.1:1", "session", "list", env={"SERIALWRAP_LOG_LEVEL": "INFO"})
    assert r.returncode == 2
    first = r.stderr.split(b"\r\n")[0]
    assert json.loads(first)["endpoint_id"] == "127.0.0.1:1"


def test_runner_cleans_or_keeps_staging(win_exe):
    keep = _runner(win_exe, "--version", env={"SW_WIN_KEEP": "1", "SW_WIN_PRINT_STAGING": "1"})
    staging = Path(keep.stderr.decode().strip().splitlines()[-1])
    assert staging.is_dir()
    shutil.rmtree(staging)
    gone = _runner(win_exe, "--version", env={"SW_WIN_PRINT_STAGING": "1"})
    assert not Path(gone.stderr.decode().strip().splitlines()[-1]).exists()


def test_win_test_report_schema():
    r = subprocess.run([str(ROOT / "scripts/rust/win-test.sh"), "--package", "sw-proto"], cwd=ROOT,
                       capture_output=True, text=True, check=False)
    report = sorted((ROOT / "target/win-test-report").glob("*.json"))[-1]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert set(data) >= {"run_id", "exes", "overall", "evidence_insufficient"}
    assert all(set(e) >= {"exe", "caps", "passed", "failed", "ignored", "exit"} for e in data["exes"])
    assert (r.returncode == 0) == (data["overall"] == "pass")
    assert data["exes"] and sum(e["passed"] for e in data["exes"]) > 0   # 空集合不得算 pass
    l1 = json.loads((report.parent / data["l1_results"]).read_text(encoding="utf-8"))   # R5-F3：逐案結果，計數只是摘要
    assert l1["format"] == "l1-results.v1" and l1["platform"] == "windows" and l1["finished"] is True
    assert {r["nodeid"] for r in l1["results"]} == set(l1["collected"]) - set(l1["deselected"])
    assert all(n.startswith("cargo:sw-proto::") for n in l1["collected"])
    assert sum(r["outcome"] == "passed" for r in l1["results"]) == sum(e["passed"] for e in data["exes"])


def test_runner_stages_fixture_dir(win_exe, tmp_path):
    # golden 測試在 Windows 執行時必須讀得到資料：runner 要建 data\ 並設 SW_TEST_DATA_DIR
    r = subprocess.run([str(ROOT / "scripts/rust/win-test.sh"), "--package", "sw-proto", "--", "--test", "pyjson_golden"],
                       cwd=ROOT, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    keep = _runner(win_exe, "--version", env={"SW_WIN_KEEP": "1", "SW_WIN_PRINT_STAGING": "1"})
    staging = Path(keep.stderr.decode().strip().splitlines()[-1])
    for rel in ("data/contracts/golden/pyjson.tsv", "data/contracts/cli_help/_meta.json", "data/VERSION",
                "data/sw_core/assets/skill/SKILL.md", "data/tests/contract_blackbox/data/rpc_params.json"):
        assert (staging / rel).is_file(), rel
    shutil.rmtree(staging)


def test_win_test_injects_runner_under_wsl(tmp_path):
    out = subprocess.run(["bash", "-c", f"WSL_DISTRO_NAME= SW_WIN_TEST_PRINT_ENV=1 {ROOT}/scripts/rust/win-test.sh --dry-run"],
                         capture_output=True, text=True, check=False).stdout
    assert "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUNNER=" in out    # 本機為 WSL（binfmt_misc/WSLInterop 存在）
```

`tests/test_rust_check_win_imports.py`（`core_portable`、`process_rpc`；以假 `objdump` 輸出測判定邏輯，不需 Windows）：`api-ms-win-core-synch-l1-2-0.dll`＋`KERNEL32.dll` → ok；含 `libgcc_s_seh-1.dll` → `forbidden`、exit 1；含 `foo.dll` → `unexpected`、exit 1；`objdump` 不存在 → exit 2（證據不足，不得 ok）。

`tests/test_rust_cargo_l1_results.py`（`core_portable`、`memory_only`；以 `tests/fixtures/libtest_sample/` 的假 `cargo --message-format=json`、`--list` 與執行輸出驅動，不需真 cargo；R5-F3，failing-first）：

| 測試名 | 斷言 |
|---|---|
| `test_collected_from_list_not_from_run` | 樣本 exe `--list` 列 `a`、`b`、`cap_posix_pty__c`（後者在 `--ignored --list` 中），執行輸出只有 `test a ... ok` → `collected` 三筆 `cargo:sw-proto::pyjson_golden::{a,b,cap_posix_pty__c}`；`results` 只有 `a`；以 P2 `l1_judge.judge(out, [a, b], platform="linux")` → `INSUFFICIENT`、`not_run == [<b>]` |
| `test_ignored_cap_is_skipped_not_pass` | `test cap_posix_pty__c ... ignored` → 該列 `outcome == "skipped"`、`skip_kind == "capability"`；必要集合含 `<c>` → `l1_judge` 為 `INSUFFICIENT`（`skipped_required`） |
| `test_failed_and_exit_code_recorded` | 一支 exe 輸出 `test b ... FAILED` 且 exit 101 → `<b>` 為 `failed`、報告 `exit_code == 101`、`finished is True`；`l1_judge` → `FAIL` |
| `test_build_failure_is_not_success` | 假 `cargo --no-run` exit 101（編譯失敗）→ 報告 `finished is False`、`exit_code == 101`、`results == []`；`l1_judge` 對任何非空必要集合 → `INSUFFICIENT` |
| `test_timeout_marks_unfinished` | 假 runner 對第二支 exe 回 124 → `exit_code == 124`、`finished is False` |
| `test_duplicate_nodeid_preserved` | 兩支 exe 皆列出同 crate／target／名稱（樣本刻意重複）→ `collected` 中該 nodeid 出現兩次；`l1_judge` → `INSUFFICIENT`、`duplicates` 含之 |
| `test_list_only_feeds_regen_lock` | `--list-only` 輸出只含 `collected`，以 P2 `regen_lock.py --l1-add-only --set t --platform linux --collect <輸出>` 於暫存 lock 成功登記三筆 |

（`SW_WIN_PRINT_STAGING=1` 為 runner 的除錯旗標：最後在 stderr 印 staging 的 Linux 路徑。）

- [ ] **Step 2：確認紅燈**

Run：`python3 -m pytest -q tests/test_rust_win_scripts.py tests/test_rust_cargo_l1_results.py`
Expected：FAIL（腳本不存在）。

- [ ] **Step 3：commit 測試**

`git commit -m "test(rust): 新增 WSL interop Windows 執行腳本測試（紅燈）"`

- [ ] **Step 4：實作**

依 Interfaces 撰寫全部腳本；`win-runner.sh` 的 `WSLENV` 照 arch §9.4（`SW_TEST_CAPS:SW_TEST_COM_PAIR:SW_TEST_SEED:RUST_BACKTRACE:RUST_TEST_THREADS:SERIALWRAP_*/u`，`TMP`／`TEMP`／`SW_TEST_DATA_DIR` 以 `/p` 轉換）；WSLENV 不支援萬用字元，腳本須在執行前列舉目前 env 中所有 `SERIALWRAP_` 開頭的變數名稱逐一附加。`win-test.sh` 支援 `--dry-run`（只印將注入的 env 與 cargo 命令）。

- [ ] **Step 5：確認綠燈**

Run：
```bash
python3 -m pytest -q tests/test_rust_win_scripts.py tests/test_rust_check_win_imports.py tests/test_rust_cargo_l1_results.py
scripts/rust/win-test.sh --package sw-proto --package sw-pal --package sw-cli     # 含 Task 9 待補的 sw-pal Windows 交叉執行
scripts/rust/win-blackbox.sh --no-compare
```
Expected：pytest PASS；`win-test` 報告 `overall: pass`，`evidence_insufficient` 列出 `windows_com`、`interactive_desktop`（`cap_*` 為 ignored，不計 PASS）；`check_win_imports.py` 對 interop 產物 `ok:true`；`startup-100.json` 的 `ok:true`；`win-blackbox --no-compare` 產出 A、B 兩份 junit（A/B 判定在 Task 19）。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(rust): 新增 WSL interop 的 Windows 原生測試與黑箱 A/B 執行腳本"`

---

### Task 18：A/B 比較器、Rust CLI→Python daemon 黑箱與 Stage 1A gate 判定器

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p3-18-stage1a-gate.md`（frontmatter `type: change`、`scope: rust-cli`）
**BLOCKED-OWNER:**
- L2：以 Rust CLI（bundle `bin/serialwrap` 作為 TestPilot `serialwrap_exe`）執行資料面相關的 `regression/` 案例（live bench、bench 互斥協定）。
- L3：以 Rust CLI 執行 `realhw` P0 與相關 P1。
- 擁有者 Windows 自架 runner 的原生 build＋`win-blackbox` 重跑（取代 Task 17 interop 報告作為正式 gate）。
- （OD-15 起移出 BLOCKED-OWNER）OD-08 Windows COM 證據改為擁有者 host `COM20:COM21` 的可執行 gate（Task 9、Task 17「Windows COM 證據」；不得替代）；擁有者 host 不可用時記「證據不足」。
- 以上未取得前，Stage 1A 出口狀態只能記為 `IMPLEMENTED`，不得記為 `VERIFIED`（規格 §0.3）。

**Files:**
- Create: `scripts/rust/ab_compare.py`、`scripts/rust/ab_blackbox.sh`、`scripts/rust/stage1a_gate.py`、`tests/contract_blackbox/test_bb_daemon_e2e.py`、`tests/contract_blackbox/data/e2e_profiles/shell.yaml`、`tests/contract_blackbox/data/coverage_1a.json`、`tests/contract_blackbox/data/applicability.json`
- Modify: `tests/acceptance/cases.yaml`、`tests/acceptance/cases.lock.json`（以 P2 `regen_lock.py --add-only` 登記本 phase 新增的黑箱 case_id，`cli_stage:"1A"`；不得刪改既有列）
- Modify: `tests/acceptance/l1_required.lock.json`（只以 P2 `regen_lock.py --l1-add-only` 登記 `s1a_blackbox`、`s1a_delegation`、`s1a_cargo` 的 linux／windows 集合；R5-F3）
- Modify: `README.md`（English 與繁體中文兩段同步新增「Rust CLI（Stage 1A）建置與 A/B 執行」小節）
- Test: `tests/test_rust_ab_compare.py`、`tests/test_rust_stage1a_gate.py`、`tests/test_rust_readme_trust_scope.py`

**Interfaces:**
- Consumes：Task 2～17 全部；P2 的 `func-test/runner.py` 外部模式、`cases.lock.json` 的 `sets.cli_1A` 與 `regen_lock.py`；P1 manifest。
- Produces：
  - `tests/contract_blackbox/data/applicability.json`（**事前登記、與測試同一個 Step 3 commit 凍結**）：`{"<reason>": {"applies_to": "posix"|"windows"|"rust"|"python", "note": "…"}}`，只有登記過的原因可讓雙邊 SKIP 列為 `not-applicable`。初版只有三種：`posix_only`（Windows 上）、`windows_native`（POSIX 上）、`rust-only contract`（Python 輪）。
  - `scripts/rust/ab_compare.py --a <junit> --b <junit> --a-label L --b-label L --out <json> --identity-a <json> --identity-b <json> --applicability <json> [--required <case_id 清單檔>]`：

    | A | B | 分類 | 影響 |
    |---|---|---|---|
    | pass | pass | `ok` | — |
    | pass | fail／error | `regression` | FAIL |
    | fail／error | 任意 | `baseline-fail` | FAIL（測試或基線有誤，屬基礎設施錯誤） |
    | pass | skip（非 `rust_only`） | `evidence-insufficient` | FAIL |
    | skip（`rust-only contract`） | pass | `rust-only-ok` | — |
    | skip（`rust-only contract`） | fail | `regression` | FAIL |
    | skip | skip（同一原因，且原因已登記於 `applicability.json` 並適用於本平台） | `not-applicable` | 列出，不計入 |
    | skip | skip（原因未登記、或原因不同、或登記但不適用本平台，例如 Linux 上缺 `posix_pty`） | `evidence-insufficient` | FAIL |
    | xfail（`<skipped type="pytest.xfail">`） | 任意 | `xfail-unapproved` | FAIL（黑箱套件不允許 xfail；既有哨兵不在此套件） |
    | 任意 | xfail／xpass | `xfail-unapproved` | FAIL |
    | 只出現在一邊 | — | `case-set-mismatch` | FAIL |
    | `--required` 列出但兩邊都缺 | — | `required-missing` | FAIL |

    另外三種整體判定直接 `result: fail`：任一 junit 為空或 testcase 數為 0（`empty-report`）、同一份 junit 內 testcase id 重複（`duplicate-id`）、junit 解析失敗（`unparseable`）。
    輸出 envelope：`{"a_label","b_label","identity":{"a":{…},"b":{…}},"counts":{…},"cases":[{"id","class","a","b","reason"}],"result":"pass"|"fail","errors":[…]}`；exit 0 只在 `result == "pass"`。
    `identity` 每邊必填欄位（缺欄位、空字串或檔案不存在即 `result: fail`，`errors` 列出）：`cli_path`、`cli_sha256`（比較器自行重算並與傳入值比對）、`cli_implementation`（rust／python）、`daemon_version`、`daemon_git_revision`、`daemon_tree_sha256`（Rust 輪取自 manifest `components.daemon.package.tree_sha256`；Python 輪以 P1 `package_tree_sha256()` 計算）、`daemon_runtime_sha256`、`contract_revision`、`case_list_sha256`（該邊 junit 中 testcase id 排序後的 sha256）、`tool_revision`（本 repo `git rev-parse HEAD`＋`tests/contract_blackbox` 的 tree sha）、`platform`、`config_digest`（`isolated_env` 產生的 config 目錄內容 sha256）、`dut_identity`（stub／FakeTarget／`windows-com:<COMa>` 等）、`evidence_files`（路徑＋sha256 清單）。另斷言 A、B 的 `cli_sha256` 不同、`cli_implementation` 分別為 python／rust、`daemon_tree_sha256` 相同、`case_list_sha256` 相同（不同時已由 `case-set-mismatch` 呈現）。
  - `scripts/rust/ab_blackbox.sh [--profile debug|release]`：`mkbundle.sh --dev` → 以 `SW_AB_PIDLOG=<run>/a.pids` 執行 A（Python 基線）→ **釋放確認**（規格 §15.9.1 第 3 步；任一項不明即停止、不啟動 B）：`a.pids` 中每個 pid 與其子行程都已結束（`/proc/<pid>` 不存在或為 zombie 已回收，等待上限 10 s）；每個 socket 路徑不存在；每個 lock 檔可被 `flock -n` 取得；掃 `/proc/*/fd` 無任何行程持有 A 的 run／state 目錄內的檔案或 PTY；結果寫 `<run>/release-check.json` → B（`SERIALWRAP_CLI_EXE=<bundle>/bin/serialwrap`、`SERIALWRAP_AB_LABEL=rust-1a`，同樣記 `b.pids`）→ 產生兩邊 identity JSON → `ab_compare.py --required <stage1a_gate.py 輸出的必要清單>` → `target/ab-report/<run_id>.json`。`--l1-out <dir>`（R5 審查後續）：B 輪的 pytest 命令固定為 `s1a_blackbox` linux selector（`tests/contract_blackbox -m "core_portable or posix_only"`），帶 `SW_RESOURCE_REPORT=<dir>/linux/blackbox/resource-report.json` 與 `--junitxml=<dir>/linux/blackbox/junit.xml`；同一份報告也是 `s1a_delegation`（`test_bb_delegation.py` 子集，selector.argv 相同）的判定輸入，不另跑。
  - `scripts/rust/stage1a_gate.py --ab-report <json> [--win-report <json>] [--l1-results <dir>] [--l15-report [linux=\|windows=]<func-test-report.json>]… [--spike3-evidence <dir>] [--pytest-junit-report <xml>] [--windows-com <dir>] --out <json>`（S1X 修訂：補列 `--spike3-evidence`、`--pytest-junit-report` 與平台前綴；`--l15-report` 可重複。理由：win-wal-av-* 為 spike3 產物而非 func-test 報告，gate 原本無法讀取。）：
    1. 分母：讀 `tests/acceptance/cases.lock.json` 的 `sets.cli_1A` 與 `tests/contract_blackbox/data/coverage_1a.json`（`p3_extra` 必要案例，含 console RPC 案例，§0.3）；兩者聯集即 Stage 1A 必要集合。lock 檔缺失或 `sets.cli_1A` 不存在 → `evidence-insufficient`（不得以空集合通過）。
    2. 逐 case 對帳：L1／黑箱層必須在 ab-report 中為 `ok` 或 `rust-only-ok`；L1.5 層必須在 func-test 報告（以 Rust CLI 執行）為 PASS 且 `gate_eligible`；L2／L3 層記 `BLOCKED-OWNER`（OD-15：`evidence_layer == "L1.5-docker"` 的報告只計 L1.5，掛到 L2／L3 case 時判 `evidence-insufficient`）；缺漏任一必要證據 → `evidence-insufficient`。
    3. `coverage_1a.json` 另列「原生命令 → 證據」：Task 14 的 55 列、`event` 10 子命令、`profile test`、`skill` 每一列至少對應一個 stub 黑箱 case，且資料面命令（下表 E2E 欄）至少一個 Rust CLI→Python daemon case 或一個 P2 L1.5 case；某列找不到真實 daemon 路徑證據（例如 `file push`／`file pull`：FakeTarget 不執行 shell，若 P2 也無對應 L1.5 case）→ 該列 `evidence-insufficient`，阻擋出口，不得以 stub 結果代替。
    4. 安全不變量（規格 §11.3）對照表，每列必須有指定 case 且 PASS：

       | 不變量 | 1A 承接面 | 證據 case |
       |---|---|---|
       | 被拒請求外部 TX 為零 | CLI 本地拒絕 | `test_bb_daemon_e2e.py::test_local_rejections_zero_tx`、`test_bb_exit_codes.py::test_completion_regex_local`／`_overflow`、`test_unix_bad_timeout` |
       | 有副作用的命令不得自動重送 | client 重試白名單 | `client_retry.rs::non_whitelist_never_retries`、`test_bb_client.py::test_side_effect_methods_single_send` |
       | 委派只執行一次、不經 PATH | 委派 | `test_bb_delegation.py::test_probe_receives_argv_env_stdin_and_exit_code`、`test_exec_failure_runs_nothing_and_does_not_fall_back`、`test_never_searches_path` |
       | 不得交錯不同來源 bytes；Recovery／Release／Reboot／Lease 不回放；舊 worker 交接前不重開 | daemon 仍為 Python；Rust CLI 只組包 | P2 L1.5 情境（`func-test/runner.py --mode bridge --scenarios`，以 Rust CLI 執行）中 `cases.yaml` 標註這些不變量的 case_id；`test_bb_daemon_e2e.py::test_recover_release_attach_no_replay` |
       | 證據不足不得轉 PASS | gate 本身 | `test_rust_ab_compare.py`、`test_rust_stage1a_gate.py` |

    5. `--windows-com` 缺席、目錄無 `windows-com.json` 或驗證不符 → `windows_com: evidence-insufficient`（整體 FAIL）；明示 `windows_com: FAIL` → `FAIL`（S1X 修訂，理由：OD-15 將 Windows COM 改為可執行 gate（§3 B1），工具與文字須一致）；`--win-report` 缺失 → `windows_l1: evidence-insufficient`。
    5b. **L1 完整性（R5-F3）**：新增必填 `--l1-results <dir>`，內含 `linux/blackbox/`、`windows/blackbox/`、`linux/cargo/`、`windows/cargo/` 各一份 `l1-results.v1`（pytest 黑箱為 B 輪〔Rust CLI〕以 `SW_RESOURCE_REPORT` 產生的報告＋同目錄 junit：linux 由 `ab_blackbox.sh --l1-out`、windows 由 `win-blackbox.ps1` B 輪產生；cargo 為 `cargo_l1_results.py` 輸出，Windows 取 `win-test` 報告的 `l1_results`）；以 P2 `l1_judge.judge(…, selector=<lock 的 selector>)` 對 `l1_required.lock.json` 判定 6 列：`s1a_blackbox`、`s1a_delegation`（皆取 `<platform>/blackbox/`）、`s1a_cargo`（取 `<platform>/cargo/`）× linux／windows（R5 審查後續：原 `s1a_delegation` 混 pytest 與 cargo 兩種 runner 卻只收一份報告，改為只含 pytest 黑箱子集，委派的 cargo 測試由 `s1a_cargo` 承擔；`delegation/` 目錄不再存在），結果寫入輸出的 `l1:[{"set_id","platform","verdict","reasons","missing","skipped_required","duplicates"}]`。任一 `FAIL` → 該列 `FAIL`；`INSUFFICIENT`／`INVALID`／檔案缺失／set 不存在 → `evidence-insufficient`（`overall` 因而 `FAIL`）。`ab_compare` 的 `ok` 只證明 A、B 一致，**不**證明必要黑箱案例都執行了——兩邊都只跑一個案例也會 `ok`，故不得以 ab-report 或 `win-test` 的 `passed>0` 代替本檢查。三個 set 由 P3 以 P2 `regen_lock.py --l1-add-only` 凍結（selector：`s1a_blackbox`＝argv `tests/contract_blackbox -m <markexpr>`，linux `"core_portable or posix_only"`、windows `"core_portable or windows_native"`；`s1a_delegation`＝同一 argv、`paths:["tests/contract_blackbox/test_bb_delegation.py"]`；`s1a_cargo`＝linux argv `--workspace --all-features`、windows argv `--package sw-proto --package sw-pal --package sw-cli`（與 Step 5 的 `win-test.sh` 相同；Windows 不跑 workspace 其他 crate，該差異由 selector 明載），皆為非 `cap_*` 測試，含 `crates/sw-cli` 的 `delegate_route`／`exec_delegate`），凍結時點依 P2 Task 13：各 Task 在自己的紅燈 commit 登記新增 nodeid（Task 2 起的黑箱測試、Task 16 的委派測試、各 crate 測試），Task 19 只登記 gate 自身測試（R5 審查後續）。
    5c. **S1X 會計規則（gate 側；judge 規則不變）**：(i) 擁有者機台相依列——名稱為 `cap_*` 者與 `scripts/rust/owner_gated.py` 登記的 `windows_switch_running_exe_rename`——於 cargo 報告為 skipped 時，無論是否仍在 `l1_required.lock.json` 內，一律列為 `BLOCKED-OWNER`（輸出 `owner_gated` 清單），整體至多 `IMPLEMENTED`；鎖內者由 judge 的 INSUFFICIENT 轉為 `BLOCKED-OWNER`，且僅限「只由擁有者列造成」之情形（`owner_gated.insufficiency_owner_only`）。(ii) 登記列在 selector 涵蓋的 crate 內、本平台會編譯，卻於 results 與 collected 皆缺席 → `evidence-insufficient`（FAIL）。(iii) `--pytest-junit-report` 必須帶 provenance properties（由 `scripts/rust/junit_provenance.py` 寫入）：`HEAD`＝repo HEAD、`SW_REQUIRE_CARGO`＝`1`、`argv`（JSON 陣列）涵蓋 `tests/` 且不含 `-k`／`-m`／`-x`／`--deselect`／`--ignore`／`--maxfail` 等縮小範圍參數、`git_clean`＝`"1"`（工作樹乾淨，含未追蹤檔、排除 `target/`；F4 補列）、`effective`（JSON 物件；`keyword`／`markexpr`／`deselect`／`ignore`／`ignore_glob`／`collectonly`／`lf`／`ff`／`maxfail` 任一有值即拒收；F4 補列）；任一不符即整份拒收（errors → FAIL）。(iv) `--spike3-evidence <dir>`：`win-wal-av-rotate`、`win-wal-av-unlink`、`win-wal-av-reader-handle`、`win-wal-f12-progress` 的 L1.5 列不由 func-test 報告判定，改讀 `<dir>/<case_id>.json`（`scripts/stage0/spike3_win_wal.py` 產物），依 `scripts/stage0/spike3_evidence.schema.json` 驗證、`case_id` 相符、`artifact.git_head`＝HEAD；verdict `PASS` 且 EDR 產品與狀態已知 → PASS；EDR 未知 → `BLOCKED-OWNER`（不得 PASS）；`FAIL` → FAIL；其餘（缺檔、無法解析、schema 不符、HEAD 不符、`INSUFFICIENT_EVIDENCE`）→ `evidence-insufficient`。（理由：spike3 證據依 schema 與 EDR 主控台資料判定，原 gate 無讀取路徑；EDR 未知依 §13、§11.3 不得放行。）
    5c-iv. **L15 會計（S1X 修訂，提議待 PENDING-OD-S1X-03 範圍裁決後定稿）**：`func-test/runner.py` 於每列輸出結構化 `not_applicable_steps`（step 索引、action、platforms、host）與 `na_only_gap`（其餘條件皆成立、唯一缺口為 n/a 段）。`_l15_case_status`：排除 `na_only_gap` 列後，任一列 FAIL（或未知 verdict）→ FAIL；任一列 INSUFFICIENT_EVIDENCE／SKIP → evidence-insufficient（不得記為 FAIL）；其餘皆 PASS＋gate_eligible 且至少一列 → PASS。`cases.lock.json` 的 platforms 不變（judge 規則與鎖不變）。
    6. 輸出 `{"required":[…],"cases":[{"id","layer","status","evidence"}],"invariants":[…],"coverage":[…],"owner_gated":[…],"overall":"IMPLEMENTED"|"VERIFIED"|"FAIL"}`：任一 `FAIL`／`evidence-insufficient` → `FAIL`；只剩 `BLOCKED-OWNER` → `IMPLEMENTED`；全部 PASS（含 L2／L3 與 Windows COM）才 `VERIFIED`。exit 0 只在非 `FAIL`。

`test_bb_daemon_e2e.py`（`posix_only`、`posix_pty`、`process_rpc`、`needs_py_daemon`；daemon 一律為 Python 基線；對 A 與 B 驗同一組**絕對契約**，不做位元組差分，因 `cmd_id`、時間戳不同。每個 test 以 function-scope fixture 建立自己的 daemon＋FakeTarget 與所需資料，單獨執行與任意順序皆成立）：

| test | 步驟 | 斷言 |
|---|---|---|
| `test_session_reaches_ready` | FakeTarget＋shell profile（`user_env`／`pass_env` 由 daemon env 提供）啟動 daemon；`session list` 輪詢 ≤20 s | `sessions[0].state == "READY"`、`com == "COM0"`、rc 0 |
| `test_cmd_submit_status` | `cmd submit --selector COM0 --cmd "echo hi"` → `cmd status --cmd-id <id>` 輪詢 | submit `ok:true` 含 `cmd_id`；status 回應 `response["command"]["status"]` 最終為 `done`、`response["command"]["stdout"]` 含 `hi`（`command.get` 回 `{"ok":true,"command":{…}}`，`arbiter.py:247-252`） |
| `test_cmd_background_result_tail` | `cmd submit --selector COM0 --cmd "echo bg" --mode background` → 輪詢 `cmd status` 至終態 → `cmd result-tail --cmd-id <id>` | result-tail `ok:true`，chunks 內容含 `bg` |
| `test_log_and_wal` | 本 test 先 `cmd submit --cmd "echo hi"` 並等 `done`，再 `log tail-text --selector COM0`、`wal current-seq`、`wal export --limit 5`、`stream tail --selector COM0` | `lines` 含 `hi`；`seq > 0`；`records` 為 list；stream tail `ok:true` |
| `test_console_attach_detach` | `session console-attach --selector COM0 --label t` → `session console-list --selector COM0` → `session console-detach --selector COM0 --client-id <id>` | 回應含 `client_id`、`vtty`（POSIX）；list 含該 client；detach ok |
| `test_interactive_lease_roundtrip` | `session interactive-open --selector COM0 --allow-attached` → `interactive-send --interactive-id <id> --data "echo il\r"` → `interactive-status --interactive-id <id>` → `interactive-close --interactive-id <id>` | 各步 ok；status 畫面含 `il`；close 後 `cmd submit` 可再執行 |
| `test_recover_release_attach_no_replay` | 先送一筆 `echo before`；`session recover --selector COM0` → 輪詢 READY；`device release --selector COM0` → `device attach --selector COM0` → 輪詢 READY；記錄前後 `FakeTarget.rx_log()` | 每步 ok；release／attach／recover 之後 `rx_log()` 中 `echo before` 只出現一次（不回放）；release 期間 `session list` 狀態為 `RELEASED` |
| `test_alias_roundtrip` | `alias set --session-id <sid> --alias lab0` → `alias list` | list 含 `lab0` |
| `test_event_rule_roundtrip` | 本 test 寫 `rule.json` → `event add --file rule.json` → `event list` → `event rm <id>` | 全部 ok |
| `test_device_mcu_health` | `device list`、`mcu patterns`、`mcu status` | ok；`mcu status.flashing == false` |
| `test_local_rejections_zero_tx` | 記錄 `rx_log()` 與 `wal current-seq`；依序執行 `cmd submit --selector COM0 --completion-regex ^`、`--completion-regex <1025 bytes>`、`--bench nope session list`、`--endpoint foo://x session list`、`--timeout -1 cmd submit --selector COM0 --cmd x` | 各 rc 照 Q-04／Q-10／Q-25；之後 `rx_log()` 位元組不變、`wal current-seq` 不變 |
| `test_no_version_warning` | 以上任一命令 | stderr 不含 `不一致`（bundle 內 CLI 與 daemon 版本相同） |

- [ ] **Step 1：寫失敗測試**

`tests/test_rust_ab_compare.py`（`core_portable`、`process_rpc`）：

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CMP = ROOT / "scripts" / "rust" / "ab_compare.py"
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

INNER = {"pass": "", "fail": "<failure message='x'/>", "error": "<error message='x'/>",
         "skip": "<skipped message='cap_missing:posix_pty'/>",
         "skip_rust_only": "<skipped message='rust-only contract'/>",
         "skip_posix_only": "<skipped message='posix_only'/>",
         "xfail": "<skipped type='pytest.xfail' message='x'/>"}


def junit(path: Path, cases: list[tuple[str, str]]) -> Path:
    body = []
    for cid, st in cases:
        cls, name = cid.rsplit("::", 1)
        body.append(f'<testcase classname="{cls}" name="{name}">{INNER[st]}</testcase>')
    path.write_text(f'<testsuites><testsuite name="bb">{"".join(body)}</testsuite></testsuites>', encoding="utf-8")
    return path


def identity(tmp_path: Path, side: str, **over) -> Path:
    cli = tmp_path / f"cli-{side}"; cli.write_bytes(side.encode())
    base = {"cli_path": str(cli), "cli_sha256": __import__("hashlib").sha256(side.encode()).hexdigest(),
            "cli_implementation": "python" if side == "a" else "rust", "daemon_version": "0.3.2",
            "daemon_git_revision": "a" * 40, "daemon_tree_sha256": "t" * 64, "daemon_runtime_sha256": "r" * 64,
            "contract_revision": "c" * 40, "case_list_sha256": "", "tool_revision": "x", "platform": "linux",
            "config_digest": "d" * 64, "dut_identity": "stub", "evidence_files": []}
    base.update(over)
    p = tmp_path / f"id-{side}.json"; p.write_text(json.dumps(base), encoding="utf-8")
    return p


def run(tmp_path, a, b, platform="linux", id_a=None, id_b=None):
    appl = tmp_path / "appl.json"
    appl.write_text(json.dumps({"posix_only": {"applies_to": "windows"}, "rust-only contract": {"applies_to": "python"}}))
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(CMP), "--a", str(junit(tmp_path / "a.xml", a)), "--b", str(junit(tmp_path / "b.xml", b)),
                        "--a-label", "python", "--b-label", "rust-1a", "--out", str(out), "--applicability", str(appl),
                        "--identity-a", str(id_a or identity(tmp_path, "a", platform=platform)),
                        "--identity-b", str(id_b or identity(tmp_path, "b", platform=platform))],
                       capture_output=True, text=True, check=False)
    return r.returncode, json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize("a, b, cls, rc", [
    ("pass", "pass", "ok", 0),
    ("pass", "fail", "regression", 1),
    ("pass", "error", "regression", 1),
    ("fail", "pass", "baseline-fail", 1),
    ("pass", "skip", "evidence-insufficient", 1),
    ("skip_rust_only", "pass", "rust-only-ok", 0),
    ("skip_rust_only", "fail", "regression", 1),
    ("skip", "skip", "evidence-insufficient", 1),            # 未登記原因（缺 posix_pty）不得當 not-applicable
    ("xfail", "pass", "xfail-unapproved", 1),
    ("pass", "xfail", "xfail-unapproved", 1),
])
def test_classification(tmp_path, a, b, cls, rc):
    code, rep = run(tmp_path, [("t.mod::c1", a)], [("t.mod::c1", b)])
    assert code == rc
    assert rep["cases"][0]["class"] == cls
    assert rep["result"] == ("pass" if rc == 0 else "fail")


def test_registered_skip_is_not_applicable_only_on_matching_platform(tmp_path):
    code, rep = run(tmp_path, [("t::c", "skip_posix_only")], [("t::c", "skip_posix_only")], platform="windows")
    assert (code, rep["cases"][0]["class"]) == (0, "not-applicable")
    code, rep = run(tmp_path, [("t::c", "skip_posix_only")], [("t::c", "skip_posix_only")], platform="linux")
    assert (code, rep["cases"][0]["class"]) == (1, "evidence-insufficient")


def test_case_set_mismatch(tmp_path):
    code, rep = run(tmp_path, [("t::a", "pass")], [("t::b", "pass")])
    assert code == 1
    assert {c["class"] for c in rep["cases"]} == {"case-set-mismatch"}


@pytest.mark.parametrize("a", [[], [("t::a", "pass"), ("t::a", "pass")]], ids=["empty", "duplicate"])
def test_empty_or_duplicate_report_fails(tmp_path, a):
    code, rep = run(tmp_path, a, [("t::a", "pass")])
    assert code == 1 and rep["result"] == "fail" and rep["errors"]


@pytest.mark.parametrize("field", ["cli_sha256", "daemon_tree_sha256", "contract_revision", "dut_identity"])
def test_identity_required_fields(tmp_path, field):
    code, rep = run(tmp_path, [("t::a", "pass")], [("t::a", "pass")], id_b=identity(tmp_path, "b", **{field: ""}))
    assert code == 1 and any(field in e for e in rep["errors"])


def test_identity_cli_sha_recomputed_and_sides_differ(tmp_path):
    code, rep = run(tmp_path, [("t::a", "pass")], [("t::a", "pass")], id_b=identity(tmp_path, "b", cli_sha256="0" * 64))
    assert code == 1
    same = identity(tmp_path, "b", cli_implementation="python")
    code, _ = run(tmp_path, [("t::a", "pass")], [("t::a", "pass")], id_b=same)
    assert code == 1
```

`tests/test_rust_stage1a_gate.py`（`core_portable`、`process_rpc`；以暫存的假 lock／報告驗判定）：`cli_1A` 集合中有一筆 ab-report 缺 → `FAIL`；lock 檔缺 → `FAIL`（不得空集合通過）；只缺 L2／L3 → `IMPLEMENTED`；`coverage_1a.json` 中某資料面命令只有 stub 證據 → `FAIL`；安全不變量表某列 case 不存在 → `FAIL`；`--windows-com` 缺 → `windows_com: evidence-insufficient` 且 `overall` 為 `FAIL`（S1X 修訂，理由：OD-15 將 Windows COM 改為可執行 gate（§3 B1），工具與文字須一致）。另加 R5-F3 failing-first 案例：`test_l1_single_passing_case_is_insufficient`（`s1a_blackbox` 凍結三個 nodeid，`linux/blackbox` 報告只有一個 `passed`、ab-report 兩邊同樣只有該案例且 `result: pass` → `l1` 該列 `INSUFFICIENT`、`missing` 兩筆、`overall == "FAIL"`）、`test_l1_required_skip_is_insufficient`（三案皆在、其一 `skipped`（`skip_kind: "other"`）→ `overall == "FAIL"`，`skipped_required` 含之）、`test_l1_runner_exit_nonzero_is_insufficient`（全部 `passed` 但 `exit_code: 1`→ `FAIL`）、`test_l1_results_missing_is_insufficient`（缺 `windows/cargo/` → `evidence-insufficient`、`overall == "FAIL"`）、`test_l1_full_sets_pass`（四份報告〔兩平台 × blackbox／cargo〕覆蓋三個 set 且 `exit_code: 0`、`finished: true`、`argv` 等於凍結 selector → `l1` 六列全為 `PASS`，其餘條件同 `IMPLEMENTED` 情境時 `overall == "IMPLEMENTED"`）、`test_l1_cargo_argv_not_matching_selector_invalid`（R5 審查後續：`linux/cargo` 報告的 cargo 參數為 `--workspace`、凍結 selector 為 `--workspace --all-features` → 該列 `evidence-insufficient`、`reasons` 含 `selector mismatch`、`overall == "FAIL"`）、`test_l1_delegation_judged_from_blackbox_report`（`s1a_delegation` 由 `<platform>/blackbox/` 報告判定；缺 cargo 報告時只有 `s1a_cargo` 列 `evidence-insufficient`，`s1a_delegation` 仍可 `PASS`）。

`tests/test_rust_readme_trust_scope.py`（`core_portable`、`memory_only`）：`README.md`、`sw_core/assets/skill/*.md` 與 `contracts/cli_help/**/*.json` 的 stdout 文字不得含「同使用者隔離」「same-user isolation」（OD-12）。

`test_bb_daemon_e2e.py` 依上表撰寫（使用 Task 2 的 `py_daemon`、`faketarget` fixtures）；`coverage_1a.json` 與 `applicability.json` 一併寫好。三者與 `cases.yaml` 的新增列在 Step 3 同一 commit 凍結（§0.4）。

- [ ] **Step 2：確認紅燈**

Run：`python3 -m pytest -q --runxfail tests/test_rust_ab_compare.py tests/test_rust_stage1a_gate.py`
Expected：FAIL（`ab_compare.py`／`stage1a_gate.py` 為 stub）。`test_bb_daemon_e2e.py` 對 Python 基線 PASS；`test_rust_readme_trust_scope.py` PASS。

- [ ] **Step 3：commit 測試**

`git commit -m "test(rust): 新增 A/B 比較器、Stage 1A gate 判定與 Rust CLI→Python daemon 黑箱案例（紅燈）"`

- [ ] **Step 4：實作**

1. `ab_compare.py`（stdlib：`xml.etree.ElementTree`、`hashlib`、`json`）。
2. `ab_blackbox.sh`（釋放確認照 Interfaces，不以 `pgrep -f` 代替）。
3. `stage1a_gate.py`。
4. `regen_lock.py --add-only` 登記新增 case_id（只新增，不刪改）。
5. README 兩段同步更新（R-18）：建置指令、`mkbundle.sh --dev`、`ab_blackbox.sh`、`win-blackbox.sh`、委派集合與「Stage 1A 不構成生產檢查點」聲明；不得出現「同使用者隔離」字樣（OD-12）。

- [ ] **Step 5：確認綠燈**

Run：`python3 -m pytest -q tests/test_rust_ab_compare.py tests/test_rust_stage1a_gate.py tests/test_rust_readme_trust_scope.py && scripts/rust/ab_blackbox.sh --profile debug`
Expected：pytest PASS；`ab-report` 的 `result: pass`、`release-check.json` 全部項目通過。

- [ ] **Step 6：commit 實作**

`git commit -m "feat(rust): 新增 A/B 比較器、Stage 1A gate 判定器與 Rust CLI→Python daemon 黑箱"`

---

### Task 19：Rust CI workflow 與 Stage 1A 出口 gate

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p3-19-rust-ci.md`（frontmatter `type: change`、`scope: ci`）
**BLOCKED-OWNER:** 擁有者 Windows 自架 runner 的 job（`runs-on: [self-hosted, windows, serialwrap-com]`）需由擁有者註冊 runner；未註冊前該 job 不得被標為 required 以外的「略過即通過」，PR 描述列「證據不足」。

**Files:**
- Create: `.github/workflows/rust.yml`
- Modify: `.github/workflows/tests.yml`（`python-version: "3.12"` 改釘 `"3.12.3"`，§0.7 argparse 方言）
- Test: `tests/test_rust_ci_workflow.py`

**Interfaces:**
- Produces：`.github/workflows/rust.yml`，每個 PR 觸發（規格 §11.5「每個 PR」列）：
  - job `rust-linux`（`ubuntu-latest`）：安裝 toolchain `1.96.1`（含 `x86_64-pc-windows-gnu` target 與 `gcc-mingw-w64-x86-64`）、Python `3.12.3`＋PyYAML＋pytest＋jsonschema；`python3 scripts/rust/check_cfg_boundary.py`、`python3 scripts/rust/check_dep_direction.py`；`cargo check --workspace --all-targets` 兩個 target（Windows target 以 `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` 注入）；`cargo test --workspace --all-features`；`python3 scripts/rust/gen_error_codes.py --check`；`SW_REQUIRE_CARGO=1 python3 -m pytest -q tests/test_rust_*.py`；`scripts/rust/mkbundle.sh --dev --out target/dev-bundle`；`SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1a python3 -m pytest -q tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider`；Linux L1.5 核心 smoke：`SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap python3 func-test/runner.py --mode pty --case sm-01-attach-to-ready`。
  - job `rust-windows-hosted`（`windows-latest`）：toolchain `stable-x86_64-pc-windows-gnu` 釘 `1.96.1`；`cargo test --workspace`（portable L1；`cap_*` 為 ignored，job summary 列為「證據不足」）；`python scripts/rust/check_win_imports.py` 對原生產物（需 mingw objdump；runner 映像無則安裝 `mingw`）。此 job 只證明免驅動 Windows L1，**不得**計為 `windows_com`（OD-08）。
  - job `rust-windows-com`（`[self-hosted, windows, serialwrap-com]`）：原生 build、`win-blackbox.ps1 -ComPair $env:SW_TEST_COM_PAIR -Python3123 $env:SW_PY3123`、`win-startup-100.ps1`；上傳 `windows-com\` 證據。只在 PR 帶 label `run-windows-com` 或排程時執行（規格 §11.5：自架 bench 不得直接執行未核准的不受信任 PR 程式碼）。
  - 全部 job 都不以 `continue-on-error` 放行。

- [ ] **Step 1：寫失敗測試**

`tests/test_rust_ci_workflow.py`（`core_portable`、`memory_only`；以 PyYAML 解析 workflow）：`rust.yml` 存在且 `on` 含 `pull_request`；`rust-linux` 的步驟依序含上列命令字串（逐項 `in` 斷言）、toolchain `1.96.1`、Python `3.12.3`；`rust-windows-hosted` 的 `runs-on` 為 `windows-latest` 且步驟含 `cargo test --workspace`；`rust-windows-com` 的 `runs-on` 含 `self-hosted` 且 `if:` 條件含 `run-windows-com`；任何 job／step 都沒有 `continue-on-error: true`；`tests.yml` 的 `python-version` 為 `"3.12.3"`。

- [ ] **Step 2：確認紅燈**

Run：`python3 -m pytest -q --runxfail tests/test_rust_ci_workflow.py`
Expected：FAIL（`rust.yml` 不存在、`tests.yml` 仍為 `"3.12"`）。

- [ ] **Step 3：commit 測試**

`git commit -m "test(ci): 新增 Rust CI workflow 結構測試（紅燈）"`

- [ ] **Step 4：實作**

依 Interfaces 撰寫 `rust.yml`、修改 `tests.yml`。

- [ ] **Step 5：Stage 1A 出口 gate（全部在本環境執行並保存證據到 `target/stage1a-evidence/<run_id>/`，不 commit）**

```bash
python3 -m pytest -q tests/test_rust_ci_workflow.py
python3 scripts/rust/check_cfg_boundary.py && python3 scripts/rust/check_dep_direction.py
cargo test --workspace --all-features
mkdir -p target/stage1a-evidence/<run_id>
scripts/rust/mkbundle.sh --dev --profile release --out target/dev-bundle   # F4：本 Step 之後使用 target/dev-bundle，須於最終 HEAD 建置（與 ab_blackbox 的 release 一致）
PYTHONPATH=scripts/rust SW_REQUIRE_CARGO=1 python3 -m pytest -q tests/ -p junit_provenance --junitxml=target/stage1a-evidence/<run_id>/pytest-full.xml  # 完整測試 JUnit 供 gate 對帳 test_rust_* 安全不變量案例；`-p junit_provenance` 寫入 HEAD／SW_REQUIRE_CARGO／argv properties（S1X：缺一或 argv 未涵蓋 tests/ 即 gate 拒收；理由：§11.4 證據須對應受測 revision）
scripts/rust/ab_blackbox.sh --profile release --l1-out target/stage1a-evidence/<run_id>/l1   # A/B 黑箱（含 delegation、daemon e2e）→ target/ab-report/<run_id>.json；B 輪 l1-results → l1/linux/blackbox/
python3 scripts/rust/cargo_l1_results.py --platform linux --out target/stage1a-evidence/<run_id>/l1/linux/cargo/resource-report.json -- --workspace --all-features   # R5-F3：s1a_cargo linux（argv 須等於凍結 selector）
SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap python3 -m pytest -q tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py   # BB-proc（Task 2 已改讀選擇器）
SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap python3 func-test/runner.py --mode bridge \
  --selftest-report ~/b-log/rust-refactor/p2/selftest-linux/selftest-report.json \
  --report-dir target/stage1a-evidence/<run_id>/func-test                                          # L1.5 外部 bridge 模式（S1X 修訂，提議待 PENDING-OD-S1X-03 裁決：原 `--mode both`）。理由：pty 模式依 P2 設計恆不 gate_eligible；lock/judge 為權威（plan 03 §I10；`_l15_case_status` 要求 gate_eligible，故 pty 列不得作為 gate 證據，`_l15_case_status` 不變）。
SERIALWRAP_CLI_EXE=$PWD/target/dev-bundle/bin/serialwrap python3 func-test/runner.py --mode bridge --scenarios \
  --selftest-report ~/b-log/rust-refactor/p2/selftest-linux/selftest-report.json \
  --report-dir target/stage1a-evidence/<run_id>/func-test-scenarios                                # L1.5 安全情境
# L1.5 Windows producer（擁有者 host，OD-15 COM20:COM21；BLOCKED-OWNER 直到執行，Linux 結果不得替代）：
py -3.12 func-test/runner.py --mode bridge --scenarios --daemon-com COM20 --bridge-com COM21 --transport-mode virtual_com0com \
  --selftest-report <Windows selftest-report.json> --report-dir target/stage1a-evidence/<run_id>/func-test-windows   # S1X：gate 的 `--l15-report windows=` 來源
# docker Rust-CLI L1.5：BLOCKED-PREREQ（非 PASS；S1X 修訂，提議待 PENDING-OD-S1X-03 裁決，不得以 run-l15.sh rust-1a 執行）。理由：`scripts/docker/run-l15.sh` 的 suite 僅接受 pytest-physical-uart／func-test-bridge／realhw-p0p1／regression-nondestructive（第 18 與 31 行），`--bundle` 模式要求 CLI 與 daemon 皆 `implementation=rust`（第 71–72 行）；Stage 1 bundle 的 daemon 為 Python，Rust daemon 需 Slice 2.x。範圍歸屬（是否屬 Stage 1）待 OD-S1X-03 裁決。
scripts/rust/win-test.sh --package sw-proto --package sw-pal --package sw-cli
mkdir -p target/stage1a-evidence/<run_id>/l1/windows/cargo && cp "target/win-test-report/$(jq -r .l1_results target/win-test-report/<run_id>.json)" target/stage1a-evidence/<run_id>/l1/windows/cargo/resource-report.json   # windows cargo l1-results
SW_TEST_COM_PAIR=COM20:COM21 scripts/rust/win-blackbox.sh          # F4：必須設 SW_TEST_COM_PAIR，否則不產生 windows-com.json（gate fail closed）。含 ab_compare、check_win_imports、startup-100；B 輪 l1-results → target/win-blackbox/<run_id>/l1/windows/blackbox/
cp -r target/win-blackbox/<run_id>/l1/windows/blackbox target/stage1a-evidence/<run_id>/l1/windows/
# spike3 win-wal-av-* 與 win-wal-f12-progress（擁有者 Windows host，OD-08；EDR 產品與狀態由擁有者主控台查得，未知即不得 PASS）：
py -3.12 scripts/stage0/spike3_win_wal.py --case <win-wal-av-rotate|win-wal-av-unlink|win-wal-av-reader-handle|win-wal-f12-progress> \
  --evidence-dir target/stage1a-evidence/<run_id>/spike3 --edr-product '<主控台查得>' --edr-status '<狀態>' --edr-interceptions <N|unknown>
# S1X 修訂：補列 `--windows-com target/win-blackbox/<run_id>`（Windows COM 為可執行 gate；缺席或不合格即 evidence-insufficient → FAIL，不是 BLOCKED-OWNER）。理由：OD-15 將 Windows COM 改為可執行 gate（§3 B1），工具與文字須一致。
python3 scripts/rust/stage1a_gate.py --ab-report target/ab-report/<run_id>.json \
  --win-report target/win-blackbox/<run_id>/report.json \
  --windows-com target/win-blackbox/<run_id> \
  --l1-results target/stage1a-evidence/<run_id>/l1 \
  --l15-report linux=target/stage1a-evidence/<run_id>/func-test/func-test-report.json \
  --l15-report linux=target/stage1a-evidence/<run_id>/func-test-scenarios/func-test-report.json \
  --l15-report windows=target/stage1a-evidence/<run_id>/func-test-windows/func-test-report.json \
  --spike3-evidence target/stage1a-evidence/<run_id>/spike3 \
  --pytest-junit-report target/stage1a-evidence/<run_id>/pytest-full.xml \
  --out target/stage1a-evidence/<run_id>/stage1a-gate.json
python3 -m policy_check --repo .
```

Expected：每一行 exit 0；`ab-report` 與 `win-blackbox` 報告 `result: pass`；`target/stage1a-evidence/<run_id>/l1/` 由上列命令寫入（`ab_blackbox.sh --l1-out` 與 `win-blackbox.sh` 的 B 輪報告＋junit、`cargo_l1_results.py … -- --workspace --all-features` 與 `win-test.sh` 報告的 `l1_results`），`stage1a-gate.json` 的 `l1` 六列皆 `PASS`（R5-F3；R5 審查後續補上產生指令並拆分 delegation）；`stage1a-gate.json` 的 `overall` 為 `IMPLEMENTED`（L2／L3、自架 runner 原生 build 仍為 BLOCKED-OWNER；Windows COM 須為 `PASS`，不再列為 BLOCKED-OWNER，缺席即 FAIL。S1X 修訂，理由：OD-15 將 Windows COM 改為可執行 gate（§3 B1），工具與文字須一致）且無任何 `evidence-insufficient`；GitHub 上 `rust-linux`、`rust-windows-hosted` 兩個 job 綠燈（PR 描述附 run 連結）。P2 前置缺少時對應行記為「證據不足」並在 PR 描述列出，`stage1a_gate.py` 會判 `FAIL`，不得標 PASS。

- [ ] **Step 6：commit 實作**

`git commit -m "ci(rust): 新增 Rust 雙平台 CI 與 Stage 1A 出口 gate"`

PR 描述需列：§11.6 Stage 1A 列的每項證據與狀態（本環境 PASS／BLOCKED-OWNER／證據不足，直接取自 `stage1a-gate.json`）、「oracle 變更」段落（§0.4，無則寫「無」）、回歸 case 評估（CLAUDE.md #155：本 phase 為 CLI 重寫，pytest＋黑箱已覆蓋；實機項目列入 BLOCKED-OWNER）、`Closes #<issue>`（若有對應 issue）或 `policy-exempt:issue-link`。

---

## 3. BLOCKED-OWNER 總表（本環境無法產出，交付程序與腳本，證據待擁有者執行）

| # | 項目 | 所屬 Task | 本計畫交付 | 需要的擁有者動作 |
|---|---|---|---|---|
| B1 | OD-08（已裁決）：動到 PAL 的 PR 與 Stage 1A 出口需原生 Windows COM handle 證據，不得以 interop 報告或 fake 替代 | 9、17、18、19 | `win-blackbox.ps1 -ComPair COMa:COMb` 的固定案例（bridge selftest＋Rust CLI→Python daemon 於 COMa 的 attach／cmd／release／attach＋P1 `windows_com` 案例）、`rust-windows-com` CI job | OD-15：Spike 1 基本資格已通過，pair 為 `COM20:COM21`（擁有者 host）；註冊自架 runner（`SW_TEST_COM_PAIR=COM20:COM21`，OD-11 隔離實例＋allowlist `COM20`）並執行，屬可執行 gate；擁有者 host 不可用時記證據不足，未完成前 PR 不得合併 |
| B2 | Windows 自架 runner 原生 build／link 與匯入 DLL 檢查 | 9、17、18、19 | `win-test.sh`、`win-blackbox.ps1`、`check_win_imports.py` 可直接在 runner 上執行 | 於自架 runner 執行並回傳報告 |
| B3 | Windows 互動 console 的 Ctrl-C／Ctrl-Break 委派行為（`cap_interactive_desktop__exec_or_wait_ctrl_c`，非互動環境執行時明確失敗） | 9、16 | ignored 測試（含實際斷言） | 於桌面 session 執行 `cargo test -p sw-pal -- --ignored cap_interactive_desktop__` |
| B4 | 端點防毒（第三方企業 EDR）在 100 次連續 CLI 執行無新增攔截 | 17 | `startup-100.json`（時間戳、exit code、exe sha256；「不新增解壓執行檔或目錄」由快照差分自行判定，與 EDR 分開） | 提供同時段 EDR 紀錄比對；權限不足時記「證據不足」 |
| B5 | L2：Rust CLI 執行資料面 `regression/` 案例 | 18 | bundle 與 testbed 設定說明 | 排程 live bench |
| B6 | L3：Rust CLI 執行 `realhw` P0＋相關 P1 | 18 | 同上 | 排程 bench（依 OD-10，與其他長跑互斥） |
| B7 | 測試團隊工作站正式安裝佈局下的委派（屬 1B 安裝流程） | 16 | dev bundle 驗證 | P4 交付後一併驗 |
| B8 | Windows 上的 CPython 3.12.3 embeddable（argparse 方言基準，§0.7） | 10、16、17 | `win-python-3.12.3.sha256`、`win-blackbox.ps1 -Python3123` | 允許下載 python.org 3.12.3 embeddable 或手動放到 `%LOCALAPPDATA%\sw-rust-test\py-3.12.3\`；缺少時 Windows cli_help／委派 A/B 為「證據不足」 |
| B9 | 跨 phase 待辦（本計畫不修改他 phase 檔案）：(a) P2 `test_cli_stage_rule` 是否把 console RPC 案例改列 `cli_stage:"1A"`；(b) P1 `resolve_config_path` 提供系統設定目錄的測試覆寫入口；(c) 盤點 10 G-08 的「rc=1」措辭更正為 1／120 兩種（Q-20）；(d) `05-stage1b` 的 changelog fragment `type: added` 不是合法 type | 0.3、2、11 | 本計畫已以 `p3_extra`、session fixture 防護、Q-20 實測值處理 | 由各 phase owner 排入 |
| B10 | S1X：擁有者機台相依 cargo 列（`cap_*`、`windows_switch_running_exe_rename`）未執行 | 3、9、16、18 | `scripts/rust/owner_gated.py` 登記；gate 與 `check_p4_cases` 列為 BLOCKED-OWNER、不因不在鎖內而消失 | 擁有者 Windows／Linux 主機執行對應 `--ignored` 測試（B2、B3、05 B-02） |
| B11 | S1X：Windows golden 範圍（本計畫 Task 10 文字為 12 情境；實作擷取 14 情境，含 2 boundary）已由 OD-16 (2) 裁決（RESOLVED）：`s1a_blackbox.windows` 之 test_help_usage_error_bytes 分母 431→14，即 `contracts/cli_help/windows` 全部情境 | 10、16 | 鎖依 OD-16 (2) 以 `regen_lock --l1-add-only --allow-removal` 縮減（逐名見 owner-decisions-addenda.md CHG-S1X-OD16-RM-05）；boundary 案例為 oracle 測試與鎖定集合所需 | 已裁決（OD-16 (2)）；見 owner-decisions-s1x-g-list.md「OD-16 套用紀錄」 |

本 phase 沒有 48 小時長跑或 >72 小時監管驗證需求（規格 §11.6：長跑屬 Stage 1B 出口）；CLI 效能指標（暖快取 p50 ≤2 ms／≤5 ms，以 `daemon status` 量測）因 1A 中 `daemon` 群組為委派，延至 P4 量測。

## 4. Stage 1A 出口核對（規格 §11.6）

| 規格要求 | 本計畫證據 | 狀態上限（本環境） |
|---|---|---|
| 已承接命令全部通過 Rust CLI→Python daemon 黑箱 | Task 18 `ab_blackbox.sh` 報告（含 `test_bb_daemon_e2e.py`），由 `stage1a_gate.py` 依 `coverage_1a.json` 逐命令對帳 | `IMPLEMENTED`＋本機 PASS |
| 已承接命令通過 L1.5 | Task 19 Step 5 的 `func-test/runner.py --mode bridge`（linux 與 `--scenarios` 兩份）＋ Windows producer（S1X 修訂，pty 列不 gate_eligible）；docker Rust-CLI L1.5 為 BLOCKED-PREREQ（非 PASS） | 依 P2 前置；缺即 FAIL |
| 未承接管理命令的透明委派有測試證據 | `test_bb_delegation.py`、`delegate_route.rs`、`exec_delegate.rs` | `IMPLEMENTED`＋本機 PASS |
| L2 以 Rust CLI 執行資料面相關案例 | B5 | BLOCKED-OWNER |
| L3 以 Rust CLI 執行 P0 與相關 P1 | B6 | BLOCKED-OWNER |
| 雙平台 L1 | `cargo test --workspace`（Linux）、`win-test.sh`（Windows interop）、CI `rust-windows-hosted`；正式以 B2 | Linux PASS；Windows 開發證據＋hosted L1 |
| 每 PR CI（規格 §11.5） | Task 19 `rust.yml`（`rust-linux`、`rust-windows-hosted`；`rust-windows-com` 依 B1） | 兩個 hosted job PASS；COM job 於擁有者 host 自架 runner 執行（OD-15 可執行 gate） |
| Windows 原生 COM 證據（OD-08） | B1 | 擁有者 host `COM20:COM21` 可執行 gate（OD-15）；host 不可用時證據不足 |
| 必要案例集合對帳（規格 §11.3） | Task 18 `stage1a_gate.py`（`cases.lock.json` `sets.cli_1A` ∪ `coverage_1a.json` `p3_extra`；安全不變量對照表） | `IMPLEMENTED`；任何 `evidence-insufficient` 即 FAIL |
