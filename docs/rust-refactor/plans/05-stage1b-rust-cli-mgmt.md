# P4 Stage 1B：Rust CLI 管理面與終端類承接 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Rust `serialwrap`（`sw-cli`）原生承接 Stage 1A 仍委派給 Python 的 8 個群組（`daemon`、`service`、`supervision-mode`、`remote`、`connect`、`benches`、`setup`、`doctor`）與終端類操作（`session console-attach/console-detach` 及 `serialwrap-minicom` 橋接），並交付 Windows `windows-task` 監管、`%LOCALAPPDATA%\Programs\serialwrap\versions\<v>\`＋`bin\` 安裝佈局、§12.1 更新／`setup --rollback`、release manifest 與版本配對 gate（§9.5），以及 Agent 權限模型文件（§10.3）。完成後委派集合為空，20 個頂層群組全部原生。

**Architecture:** 平台能力全部經 `sw-pal` trait（`ServiceCtl`、`ProcessTunnel`、`ProcessCtl`、`ProcScan`、`FsOps`、`SysEnv`、與 P5 共用的 `device_source::windows_model::RegistryView`）取得；`sw-cli` 只依賴 `sw-proto`＋`sw-pal`，以 capability 值分流，不得出現 OS 名稱比較或平台 `cfg`。systemd 與 Windows 監管的「命令產生」寫成不帶 `cfg` 的共用結構，Linux L1 就能以 `FakeSysEnv` 驗證 Windows 命令序列；真正的系統呼叫只在 `sw-pal/src/<name>/{posix,windows}.rs`。Python 基線（P1 修正後）就是契約：雙邊項目（SC-10、SC-12、SC-20、SC-21、SC-25（Python 僅在 bundle 內，P1 T18）、DF-05、DF-07、DF-10～DF-13、DF-16）以 P1 凍結的 Python 輸出為 oracle 做 A/B，本計畫不得另訂形狀；Rust-only 項目（SC-18、SC-22～SC-24、SC-27 的 Windows 面）的契約文字由本計畫定義並寫入 `contracts/`，且只能使用規格 §9.2 既有錯誤碼（見 §0.3）。另交付 P3 明列為 P4 範圍的 `sw-daemon` launcher、`yaml11/emit.rs`、`bench_registry.rs` 與自含式 dev bundle（Task 17）。

**Tech Stack:** Rust 1.96.1（edition 2021，`x86_64-unknown-linux-gnu`、`x86_64-pc-windows-gnu`）、`windows-sys =0.61.2`（arch §1.2 已列 feature 之外，只新增 `Win32_Globalization`（`MultiByteToWideChar`）與 `Win32_System_Diagnostics_Debug`（`FormatMessageW`），兩者皆在 KERNEL32，由 Task 1 一併修訂 arch §1.2 清單；匯入 DLL 限 arch §1.1 白名單，不得引入 SHELL32／SECUR32；`windows-sys` 只在 sw-pal，sw-daemon／sw-cli 不得直接依賴，arch L148）、`libc =0.2.186`、`sha2 =0.10.9`（arch §1.2 列為 sw-proto 依賴；sw-cli 一律經 `sw_proto::digest` 包裝取用，不直接依賴）、`tempfile =3.23.0`（dev）；Python 3.12＋pytest（黑箱、golden 擷取）；WSL interop（`scripts/rust/win-runner.sh`、`win-test.sh`）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §4.2、§4.3、§6.1、§6.2、§9.4、§9.5、§10.3、§11.6（Stage 1B 列）、§12.1、§13（Spike 2、Spike 6）；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-03、OD-05、OD-08、OD-10、OD-12、OD-13；`docs/rust-refactor/plans/rust-architecture.md` §4.1、§4.8、§4.9、§7.3～§7.7、§10；`docs/rust-refactor/plans/spec-reconciliation.md` SC-10、SC-12、SC-17、SC-18、SC-20～SC-25、SC-27、DF-05、DF-07、DF-10～DF-13、DF-16、PLT-O-10～PLT-O-15、CLI-12.10、GAP-G-05、GAP-G-06；盤點 `inventory/07-platform-mgmt.md`（§0、§3～§12）、`inventory/01-cli.md`（§2、§4、§6.1、§6.11～§6.19、§7）、`inventory/04-wal-state.md`（§2.9、§3、§4）、`inventory/10-gaps.md`（G-04～G-06）。

---

## Global Constraints

（摘自 `00-master-plan.md`，僅列本 Phase 相關者；違反任一條即退回。）

- 平台相依 production code 只在 `sw-pal`；`sw-proto`／`sw-cli` 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script；允許 `#[cfg(test)]` 與 `#[cfg(feature = "test-fakes")]`。每個 Task 的 PASS 步驟都要跑 `python3 scripts/rust/check_cfg_boundary.py`。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間。**本 Phase 不新增任何錯誤碼**（規格 §3.1「不能由實作者臨時新增錯誤碼」、§9.2）：所有新失敗路徑只用 §9.2 既有碼（`TIMEOUT`、`SESSION_BUSY`、`FLASHING_BUSY`、`DEVICE_STILL_HELD`、`CHECKSUM_VERIFY_FAILED`、`INVALID_ARGS`、`INTERNAL_ERROR`、`DAEMON_EXITED`、`DAEMON_NOT_READY` 等），以 `phase`／`reason`／`detail` 欄位區分情境；既有碼若需擴充 `scope.interfaces`（例如加 `cli_local`）只改其 meta 並同步 SKILL 說明。錯誤碼計數由資料產生，不得硬編碼 78（§9.2、OD-06）。
- `--retries` 只對 11 個唯讀方法重試：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`；本 Phase 的固定逾時呼叫（daemon start 探測 0.5／1.0、daemon stop 2.0、setup 探測 0.5、doctor 0.5、remote ping 2.0）一律不讀 `--timeout`／`--retries`（01 §4.6）。
- CLI stdout JSON：`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`＋`\n` 等價（`pyjson` `CLI_STDOUT` profile）；`benches.state.json`、remote registry 用 `DISK_STABLE`；`config.yaml` 用 `yaml11::emit::safe_dump(allow_unicode=true, sort_keys=true)`。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12），文件不得宣稱「同使用者隔離」；`remote` 經 ssh 暴露時信任範圍延伸至通過該 ssh 驗證者。
- Windows 監管預設 `windows-task`，`ExecutionTimeLimit=PT0S`（OD-03、§6.1）；`windows-service` 依 OD-12 不得在多人共用機器部署，本 Phase 只產生 `pending_sudo` 指令，不自動安裝。
- 所有 reconcile、更新、回滾均不得讓舊監管項與新監管項同時拉起不同版本（§6.1）；舊 daemon 須確認結束（ping 不通且行程消失，上限 10 s，SC-12）才啟動新者。任何停止監管 daemon 的路徑（含 `windows-task`）一律走共用 `graceful_stop`：先停用拉起能力 → RPC `daemon.stop`（2.0 s，觸發 SC-11 有序關機）→ 等待結束（≤10 s）→ 逾時才硬終止或回 `TIMEOUT` 且不切換（§12.1 步驟 3）。**update／rollback 例外**（R1-F1，§12.1 步驟 2～3）：第 3 步改送 `daemon.handoff_stop`，由 daemon 在共同 admission 邊界內原子完成 busy 判定、凍結所有新操作與發 token 後才停止；交接被拒或無法確認時不送 `daemon.stop`、不硬停、不切換。CLI 端 `session.list` 預檢不能關閉「預檢後新操作進入」窗口，SC-11 shutdown drain 會中止（非等待完成）進行中的傳輸與 flash。**回退與中斷復原同屬此例外**（R2-F6）：步驟 6 啟動的新版 daemon 在驗收前已對外服務、可能已受理 `file.push`／flash，故步驟 7 回退與 `recover_journal` 停止「目前在跑的 daemon（可能為新版）」也只經 `daemon.handoff_stop`（`reason:"revert"`）；忙碌、不支援或結果不明時保留 journal、不送 `daemon.stop`、不硬停、不切換回 Previous，待下一次 `setup` 在 daemon 閒置後完成回退。
- Stage 1 版本單位是「Rust CLI＋Python daemon runtime／程式碼＋設定資產＋監管定義」組合（§12.1）；`setup --rollback` 不得只切換 CLI；不得改寫 `--version` 或 status 字串湊配對（§9.5）。
- CLI 不搜尋 `PATH` 尋找另一份 serialwrap 或 Python（§4.3）；daemon 定位只依 DF-05 順序。
- 本環境不得對真實主機做破壞性變更：測試一律以 `HOME`、`XDG_*`、`SERIALWRAP_*` 隔離目錄與 PATH stub（`systemctl`、`sudo`、`loginctl`、`ssh`、`autossh`、`schtasks`、`sc.exe`、`powershell.exe`）執行；真實 `systemctl --user serialwrap`、`/etc/systemd`、`/etc/wsl.conf`、Windows Task Scheduler 的註冊只在 owner 授權的 `cap_supervisor_runtime__*` 測試執行（預設 ignored）。
- repo 政策：不得 commit 到 `main`；本 Phase 分支 `wt/rust-refactor/p4-<task-slug>`，PR base `feature/rust-refactor`；每個 code PR 附 `changelog.d/<issue>-rust-p4-<slug>.md`（direct child；`<issue>` 為 Rust 重構追蹤 issue 編號，查無 issue 時省略前綴；frontmatter 含 `type`／`scope`／`issue`，`type` 只能是 `policy_check.changelog.TYPE_TO_SECTION` 的鍵：`feat`／`fix`／`refactor`／`perf`／`change`／`docs`／`remove`／`deprecate`／`security`）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- `.project-policy.yml` 的 `code_paths` 必須含 `crates/**`、`scripts/rust/**`、`contracts/**`（P3 加入；Task 1 驗證，缺則補上），否則 R-09 不觸發 fragment 檢查。

## Review Focus

1. **two-reader 窗口**：reconcile／update／rollback／`daemon restart` 在舊 daemon 未確認結束（ping 不通且行程身分全數消失，≤10 s）前絕不啟動新監管項；Windows `windows-task` 先 `/Change /DISABLE`、再 RPC `daemon.stop`、等待 launcher＋Python 子行程全數結束，逾時才 `/End`，避免 RestartOnFailure 在等待期間拉起舊版，也避免略過 SC-11 有序關機（Task 3、7、10、12）。
2. **委派殘留造成假綠**：每個群組改原生後必須從 `DELEGATED_GROUPS` 移除；黑箱 bundle 的 `python_cli_delegate.entry` 指向 sentinel（被呼叫即寫 marker 並 exit 97），任何漏網委派都會讓測試失敗（Task 1、14）。
3. **Windows 程序語意**：存活判定絕不用等同 `os.kill(pid,0)` 的終止呼叫；`remote close` 先核對根 PID 的 CreationTime 等於 `TunnelProc.start_ticks`，再以 Toolhelp32 parent 鏈＋CreationTime 收集整棵樹終止；ProcScan 必須辨識 `serialwrapd.exe` 並把 launcher 與其 Python 子行程去重，否則每個 Stage 1 daemon 都被漏掃或誤判為多開（Task 4、5）。
4. **SC-18 首次需求啟動只在「請求從未送出」時重送**：僅 connect 階段 PAL kind 為 `ConnectionRefused`／`NotFound`、endpoint 來源為預設本機 endpoint、且方法不是 `health.ping`／`health.status`／`daemon.stop` 時才在跨行程鎖內 auto-start 並重送一次；send／recv 階段失敗、`TIMEOUT`、`EMPTY_RESPONSE`、`PermissionDenied`、明確 `--endpoint/--socket/--bench` 一律不重送（Task 7）。
5. **安裝切換的原子性與可回復**：整筆 update／rollback 持有安裝根目錄跨行程鎖；`*.new`→`*.old.<ts>`→正式名的每一步失敗都要能回到完整 Previous（CLI、launcher、Python runtime、設定、監管定義與其**已註冊狀態**一起），`current-release.json` 只在切換全部成功後改寫、`install-journal.json` 記錄進度供中斷復原——journal 在**任何停用／停止前**就持久保存原始入口、`current-release.json`、config、監管（mode、endpoint、enabled、running、已註冊內容）與 daemon 快照，備份拆成 `backup_started`→`backup_complete`（manifest sha256 入 journal）→`entries_switching`，完整備份提交前不得切換，中斷復原只用 journal 內的原始值，復原失敗時 journal 位元組不變保留且不進行本次 op（R1-F5，Task 12 逐點中斷注入測試）；安全檢查無法確認時一律拒絕（fail-closed）；update／rollback 的交接安全檢查（Flash、FileTransfer、活躍寫入、lease、recovering、身分與探測）**不受 `--force` 影響**（§12.1 步驟 2，Task 12）；停止前的最終交接判定只能是 daemon 原子 `daemon.handoff_stop`（busy 判定＋凍結新操作＋token 同一臨界區），審查須確認 update／rollback 路徑（含步驟 7 回退與 `recover_journal` 的復原變體，R2-F6）不存在任何一般 `daemon.stop` 或「預檢通過即停止」的分支（R1-F1）；版本配對、health、WAL、狀態相容或 smoke 任一失敗即自動回退並保留失敗證據（Task 11、12）。

---

## 0. 執行順序與共用約定

### 0.1 兩段式執行

1. **TDD 階段（紅燈）**：依序完成 Task 1 全部步驟（骨架讓測試可編譯；骨架自身的 parser／help 擴充也先寫失敗測試），再依 Task 17、2→16、18 的順序只做每個 Task 的「步驟 1（寫失敗測試）」與「步驟 2（執行並確認 FAIL）」。全部紅燈原因符合各 Task 的預期（`not implemented: P4-Tn` panic、斷言不符或 sentinel exit 97，**不得**是編譯錯誤或 fixture 錯誤）後，一次 commit：
   `test(rust-cli): 新增 Stage 1B 管理面失敗測試`（trailer 照 Global Constraints）。
   - 紅燈階段新增的 **pytest** 一律加 `@pytest.mark.xfail(strict=True, reason="P4-T<n> 紅燈")`（與 P3 §0.5 相同），使 `python3 -m pytest -q tests/` 在紅燈 commit 與後續各 Task commit 都不出現新失敗；該 Task 實作完成的同一 commit 移除 marker（`strict=True` 保證漏移除會變 XPASS→FAIL）。Rust 紅燈測試只在 `cargo test -p <crate> --test <file>` 層級預期失敗，不進 `pytest tests/`。
   - 預期「對既有實作直接 PASS」的測試（例如 Task 2 若 P3 已有部分 `SysEnv` 方法、Task 15 對 P1 已改過的文件）不得當紅燈證據：必須附一次**突變檢查**（暫時改壞受測實作或文件的一行 → 確認該測試轉紅 → 還原），在 PR 描述記錄突變內容與結果。
   - 紅燈 commit 前執行 §0.4 的必要案例凍結（`python3 scripts/rust/check_p4_cases.py --freeze`），把案例集合 sha256 與 commit hash 寫入 `tests/acceptance/p4_required.lock.json`；之後只能增加案例，不得刪除或改為非必要（§11.3）。
2. **實作階段（綠燈）**：依下表相依順序做每個 Task 的步驟 3～5。每個 Task 完成後送雙審（審查 A：Claude Sonnet effort max；審查 B：Codex `gpt-6.1-sol` reasoning medium），審查者不得為該 Task 實作者；修正後才 commit。

| 實作順序 | Task | 依賴 |
|---|---|---|
| 1 | Task 17（launcher、yaml emit、bench_registry、自含 dev bundle） | Task 1 |
| 2 | Task 2、3、4、5（PAL，可平行） | Task 1 |
| 3 | Task 6、11（可平行） | Task 2、3、5、17 |
| 4 | Task 7 | Task 3、4、6、11、17 |
| 5 | Task 8 | Task 2、5 |
| 6 | Task 9 | Task 8、17 |
| 7 | Task 10 | Task 3、4、5、6、7、11 |
| 8 | Task 12 | Task 4、7、10、11 |
| 9 | Task 13 | Task 2、4、10、11 |
| 10 | Task 14 | Task 7、10 |
| 11 | Task 18（windows-task Spike 2 程序與證據 schema） | Task 3、10、12 |
| 12 | Task 15 | Task 6～14 |
| 13 | Task 16 | 全部 |

### 0.2 本 Phase 消費的 P0～P3 產出（Consumes，全 Phase 共用）

以下**逐字取自 P3（`04-stage1a-rust-cli.md`）的 Produces**（行號為該檔），本 Phase 不得假設 P3 沒交付的方法存在；P3 未提供而本 Phase 需要的介面，一律在 Task 1 的「P3→P4 遷移」步驟或對應 Task 的 Produces 明列並實作。

```rust
// sw-proto（P3 Task 3～8）
pub enum PyValue { .. }  pub struct PyDict(..);  pub fn pyjson::loads(..);  pub fn pyjson::dumps(.., &DumpOpts);   // CLI_STDOUT／DISK_STABLE
pub fn yaml11::safe_load(text: &str) -> Result<YValue, YamlError>;                // 只讀（P3 L22）；emit 由本 Phase Task 17 新增
pub struct RuntimeConfig { pub data: YMap }                                         // P3 L1102
impl RuntimeConfig { pub fn from_text(text: Option<&str>) -> Result<RuntimeConfig, ConfigError>;
                     pub fn mode(&self) -> Option<&YValue>; pub fn socket_path(&self) -> Option<&YValue>; }
pub struct ReleaseManifest { pub schema_version: i64, pub bundle_version: String, pub stage: String, pub target: String,
                             pub git_revision: String, pub python_cli_delegate_entry: Vec<String>, pub raw: PyDict }   // P3 L1288
pub struct CurrentRelease { pub bundle_version: String, pub dir: String }
pub fn parse_manifest(text: &str) -> Result<ReleaseManifest, ManifestError>;      // 沒有 locate；定位由本 Phase Task 11 新增
pub fn parse_current_release(text: &str) -> Result<CurrentRelease, ManifestError>;
pub struct RuntimePaths { .. }  pub struct EnvSnapshot { .. }                       // runtime_paths（P3 Task 7）
pub fn endpoint::parse_endpoint(..);  pub mod constants;  pub mod error_codes;     // 生成碼表（P3 Task 8）
// sw-pal（P3 Task 9；P3 L1511-1590）
pub struct ClientPlatform { pub env: Arc<dyn SysEnv>, pub fs: Arc<dyn FsRead>, pub clock: Arc<dyn Clock>,
                            pub rpc: Arc<dyn RpcConnector>, pub process: Arc<dyn ProcessCtl>, pub caps: ClientCaps }
pub struct ClientCaps { pub path_flavor: PalPathFlavor, pub af_unix: bool, pub native_backend: BackendKind,
                        pub default_skill: SkillPlatform, pub text_newline: &'static str, pub exec_replace: bool }
pub fn native_client() -> ClientPlatform;
pub trait SysEnv { fn var; fn vars; fn current_user_home; fn user_home; fn current_exe; fn realpath;
                   fn terminal_columns; fn stdout_is_terminal; fn errno_name; fn pid; }      // 只有這 10 個方法
pub trait FsRead { fn read; fn exists; fn is_dir; fn list_dir; }                              // 唯讀
pub trait Clock { fn mono_ns(&self) -> u64; fn sleep(&self, d: Duration); }
pub trait RpcConnector { fn af_unix_available; fn connect(&self, ep: &EndpointSpec, t: SockTimeout) -> PalResult<Box<dyn RpcStream>>; }
pub trait ProcessCtl { fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError; }
pub struct PalError { pub kind: PalErrorKind, pub os_code, pub winerror, pub strerror, pub py_message }
pub mod fakes { FakeEnv, FakeClock { now_ns, sleeps }, MemFs, ScriptedConnector, FakeConn, StreamStep, RecordingProcess, fake_platform(..) }
// sw-cli（P3 Task 11～16）
pub struct TextIo<'a> { .. }  impl TextIo { pub fn new(..); pub fn out_str(..); pub fn err_str(..); }   // P3 L1917
pub struct CliCtx<'a> { pub p: &'a ClientPlatform, pub env: EnvSnapshot, pub paths: RuntimePaths, pub io: TextIo<'a>, pub verbose: i64 }
pub fn output::{print_json, format_err_line, mirror_err, warn_version_mismatch, print_internal_error};
pub fn client::rpc_call(p: &ClientPlatform, endpoint: &str, method: &str, params: &PyValue, req_id: i64, timeout_s: f64,
                        retries: &PyInt) -> Result<RpcOutcome, ClientCrash>;            // P3 L2164；rpc_call_once 回 (PyDict, Option<errno>)
pub struct endpoint_resolve::ResolvedEndpoint { pub endpoint: String, pub source: &'static str }
pub fn endpoint_resolve::{rpc_backend_is_win, local_default_endpoint, endpoint_alive, load_runtime_config, safe_runtime_config,
                          resolve_endpoint_info, print_endpoint_resolution_error};
pub fn bench_state::{load_bench_state_doc, remembered_bench_endpoint};                   // 1A 只讀、不加鎖
pub const delegate::DELEGATED_GROUPS: [&str; 8];  pub fn delegate::{classify, run_delegate, resolve_target, check_recursion};   // P3 L2621
pub fn run(argv: Vec<OsString>, p: &ClientPlatform, out: &mut dyn Write, err: &mut dyn Write) -> i32;   // P3 L2315
```

P3 **沒有**交付、但本 Phase 需要者（全部由本 Phase 建立，誰負責見括號）：`SysEnv::{run, run_raw, which, which_all, has_systemd, is_wsl, user_in_group, user_identity, local_appdata}`（Task 2）、可寫 `FsOps`（Task 5，`FsRead` 的超集）、`ServiceCtl`／`ProcScan`／`ProcessTunnel`（Task 3～5）、`ProcessCtl::{spawn_daemon, poll, call_foreground}`（Task 4）、`yaml11::emit`、`bench_registry`、`sw-daemon` launcher（Task 17）、`RuntimeConfig::{set_mode, set_socket, to_yaml}`＋`endpoint_resolve::{config_write_path, save_runtime_config}`（Task 6）、release manifest 定位（Task 11）。

**P3→P4 遷移（Task 1 步驟 3 執行，不改 P3 測試的行為）**：
- `sw_pal::Platform`＝`ClientPlatform` 的超集：`{ env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock, rpc, process: Arc<dyn ProcessCtl>, procs: Arc<dyn ProcScan>, tunnel: Arc<dyn ProcessTunnel>, service: Arc<dyn ServiceCtl>, registry: Arc<dyn RegistryView + Sync>, caps: Capabilities }`；`Capabilities`＝`ClientCaps` 全欄位＋`on_demand_autostart: bool`、`console_kind: ConsoleKind`。`pub type ClientPlatform = Platform;`、`pub type ClientCaps = Capabilities;`、`pub fn native_client() -> Platform { native() }` 保留舊名；`FsOps: FsRead` 為 supertrait，P3 呼叫端不需修改。
- `CliCtx` 保留原名與欄位（`p`、`env`、`paths`、`io: TextIo`、`verbose`），另加 `pub cli_version: &'static str`；本計畫所有 `Ctx`／`ctx.pf`／`ctx.io` 指的就是 `CliCtx`／`ctx.p`／`ctx.io`（不另建 `ctx.rs`）。
- `rpc_call` 沿用 P3 簽章；本計畫虛擬碼中的 `rpc_call(ctx, ep, method, params, timeout, retries)` 是對它的薄包裝 `client::call(ctx, ep, method, &PyValue, timeout_s, &PyInt) -> RpcOutcome`（req_id 由包裝依 P3 規則產生，`ClientCrash` 照 P3 轉 exit 1）。
- `RuntimeConfig` 沿用 P3 的 `from_text`／`mode()->Option<&YValue>`；本計畫寫 `cfg.mode()` 時一律指「`mode()` 為 `YValue::Str(s)` 則 `Some(s)`，其他型別照 P3 Q-18 規則」的輔助 `mode_str(&cfg) -> Option<String>`。
- `DELEGATED_GROUPS` 維持 `pub const DELEGATED_GROUPS: [&str; N]`（P3 型別）；每移除一個群組就改 `N`，Task 14 結束時為 `pub const DELEGATED_GROUPS: [&str; 0] = [];`。

### 0.3 本 Phase 登記的契約項

| 項目 | 檔案 | 內容 |
|---|---|---|
| 錯誤碼 | `contracts/error_codes.yaml` | **不新增碼**。本 Phase 用到的既有碼若 `scope.interfaces` 未含 `cli_local`（例如 `TIMEOUT`、`SESSION_BUSY`、`DEVICE_STILL_HELD`、`CHECKSUM_VERIFY_FAILED`），只補 interface 與對應 SKILL 說明（OD-06 受管區段重生）；`phase` 值集合（`restart_wait_old_exit`、`reconcile_wait_old_exit`、`service_restart_wait_stop`、`install_lock`、`update_verify`、`rollback_verify`、`update_safety_check`、`update_confirm_released`、`update_start_and_verify`）寫入各碼的 `meaning` 附註 |
| 行為變更狀態 | `contracts/behavior_changes.yaml` | SC-18、SC-20～SC-25、SC-27、DF-05、DF-07、DF-10～DF-13、DF-16、BC-05 在對應 Task 完成時 `status: IMPLEMENTED`（P0 `CHANGE_STATUS` 大寫值）；`case_ids` **不改**（P0 已凍結，例如 `sc18-windows-ondemand-autostart`、`sc23-setup-rollback`、`bc05-three-topologies`）；`VERIFIED` 只在 BLOCKED-OWNER 證據補齊後由擁有者改 |
| 測試↔case 綁定 | `tests/acceptance/p4_case_map.yaml`（新增，Task 1） | 每個 P0 case_id 與 P2 `cases.lock.json` `sets.cli_1B` 中 `implemented_by: 05-stage1b-rust-cli-mgmt.md` 的 case_id → 承接測試節點清單（Rust：`<crate>::<test file>::<fn>`；pytest：node id 並加 `@pytest.mark.case("<case_id>")`）、平台、能力、artifact、證據檔；schema 見 §0.4 |
| CLI schema | `contracts/cli_schema.json` | Rust-only：`supervision-mode [set MODE] [--force] [--with-sudo]`、`setup --windows-task/--windows-service/--rollback`、env `SERIALWRAP_INSTALL_ROOT`；雙邊：`daemon restart`、`remote stop`（以 P1 Python parser 擷取） |
| CLI help golden | `contracts/cli_help/rust_only/*.txt` | 由 `scripts/rust/capture_cli_help.py --rust-only` 以 Python 3.12 argparse（`sw_core.cli.build_parser()`＋本計畫的擴充 `add_argument`）產生，COLUMNS=80 與 120 |
| manifest schema | `contracts/release_manifest.schema.json` | 與 P1 T18 欄位一致（`components.daemon.{launcher,runtime,package}`）；新增 optional `components.daemon.state_schema`（string）、`components.supervisor.task_xml_template_sha256`（string）；不改既有必填欄位 |

### 0.4 必要案例集合與 §11.3 不變量對照

`tests/acceptance/p4_case_map.yaml`（Task 1 建立、紅燈 commit 前凍結）每筆：`case_id`、`invariants`（P2 `invariant_ids` 子集）、`layer`、`platforms`、`capabilities`、`artifact`（`rust-cli-bundle`／`sw-pal-test-bin`）、`tests`（節點清單）、`evidence`（`target/p4-evidence/<case_id>.json`，含 commit hash、artifact sha256、結果）、`necessity`、`skip_registered`（只允許事先登記的能力不適用，例如 `rust_only` 在 Python 輪、`windows_native` 在 Linux）。`scripts/rust/check_p4_cases.py`：`--freeze` 寫 `p4_required.lock.json`（案例集合、各案例 sha256、凍結 commit、`error_codes_at_freeze`＝當下 `contracts/error_codes.yaml` 全部碼——Task 16 以此證明本 Phase 未新增碼）；`--check` 讀凍結集合，任一 case 缺證據、證據 FAIL、證據 commit 不是受測 HEAD、或出現未登記 SKIP → exit 1（「證據不足不得轉為 PASS」）。

| §11.3 不變量 | 本 Phase 承接面 | 承接 Task／測試 | 證據層 |
|---|---|---|---|
| 被拒請求外部 TX 為零（`zero_tx_on_reject`） | `FLASHING_BUSY`／`SESSION_BUSY` 拒絕更新、`DAEMON_ALREADY_RUNNING_ELSEWHERE` 拒絕 spawn、SC-18 不重送 | Task 7 `autostart_*`、Task 12 `update_*_busy*`、`update_file_push_after_last_probe`、`update_flash_races_prepare`；黑箱 `test_autostart_l15_tx_once`、`test_update_busy_l15_zero_tx`、`test_update_file_push_after_last_probe_l15`、`test_update_push_after_freeze_zero_tx_l15`（Task 16） | L1 fake 計數＋L1.5 bridge `wire.ndjson`（DUT 端 TX bytes：拒絕案例 0、SC-18 重送案例恰一次 `session.list` 對應的 0 bytes 且命令類恰一次） |
| 不同來源 bytes 不得交錯（`no_forbidden_interleave`） | 本 Phase 不改 daemon I/O；CLI 只傳遞 console-attach | Task 14 `test_minicom_wrapper_*`、Task 16 `tests/test_human_agent_coexist.py`（Rust CLI） | L1.5 |
| 切換不得回放失效輸入（`no_stale_context_replay`） | daemon restart／update／rollback 後 console 重連 | Task 16 `test_restart_no_stale_console_replay`（restart 前 console 未送出的行在新 daemon 不出現於 DUT RX） | L1.5 |
| 舊 worker 未交接不得重開（`no_reopen_before_handoff`） | `wait_daemon_gone`／`confirm_released`／`graceful_stop` | Task 7 `restart_*`、Task 10 `reconcile_aborts_*`、Task 12 `update_*_aborts*`、`update_rpc_down_child_holds_com_blocks` | L1＋L1.5（新 daemon 開 tty 時舊 daemon 已無 fd） |
| 有副作用命令不得自動重送（`no_auto_resend`） | SC-18 | Task 7 `no_autostart_or_resend_after_send_phase_failure`、`autostart_not_for_timeout`、`autostart_permission_denied_no_resend` | L1＋L1.5 |
| 證據不足不得轉 PASS（`insufficient_evidence_not_pass`） | 案例完成度 gate | `check_p4_cases.py --check`（Task 16） | gate |

`rust_only` 案例在 Python 輪的 SKIP 於 `p4_case_map.yaml` 以 `skip_registered: {python: "rust-only 契約（behavior_changes 已登記）"}` 事先登記；Rust 輪對 `necessity: required` 的案例 SKIP 數必須為 0。

### 0.5 跨計畫介面通知（本 Phase 改動既有文件的範圍）

- `rust-architecture.md`：Task 1 修訂 §1.2（`windows-sys` 新增 `Win32_Globalization`、`Win32_System_Diagnostics_Debug` feature）、§4（`Platform`／`Capabilities` 新欄位）、§4.1（`SysEnv`／`FsOps` 新方法：`run`、`run_raw`、`which*`、`user_identity`、`local_appdata`、`symlink_file`、`copy_tree`、`remove_tree`、`file_lock_exclusive`）、§4.8（`ServiceCtl` 實際簽章、`ServiceResult`、`SupervisorDefinition` 欄位、`render_task_xml(&TaskXmlSpec)`）、§4.9（`ProcScan::{pid_alive, instance_tree}`、Windows 掃描 `serialwrapd.exe`／`python*.exe`）、§7.7（Windows expose readiness 改為輪詢，見 Task 8），型別全部由 `sw_pal::traits` re-export（P8 先例：`09-slice24-transfer-flash.md` L186）。
- P5（`06-slice21-daemon-core.md` Task 5）：`device_source/windows_model.rs` 的 `RegistryView`、`read_bt_ports`、`exclude_bluetooth` 改由本 Phase Task 2 先建立（簽章逐字照 P5 L983-986，另加 `Sync` bound），P5 改為沿用並補 `WindowsDeviceSource`；`crates/sw-daemon/src/launcher.rs` 由本 Phase Task 17 建立，P5 只增加 `components.daemon.implementation` 判斷。
- P8（`09-slice24-transfer-flash.md` L1743-1772）：`render_task_xml(&TaskXmlSpec)`、`WindowsTaskCtl::new(env, fs, clock)`、`ServiceResult.ok` 為本 Phase 定案名稱；SC-24-S（`windows-task` stop／restart 先送 `daemon.stop`）本 Phase 已在 Stage 1 實作（`graceful_stop`），P8 Task 13 改為沿用並補 Rust daemon 側驗證。
- P1：BC-05 Windows expose readiness（`make_ssh_check -> lambda: True` 後立即判定）有與 Task 8 相同的競態，本 Phase 以 Rust 修正並在 PR 描述提出 P1 T14 同步修正請求；`is_serialwrapd_argv` 第 4 規則（`-m sw_core.daemon`）同樣請 P1 同步 `sw_core/multi_open.py`，未同步前「Python CLI 偵測 `-m` 形式 daemon」列為 A/B 已登記差異。

---

## Task 1：介面骨架、P3→P4 遷移、測試替身、黑箱 stub、案例綁定與 golden 擷取工具

**Test author model:** agy-gemini-3.8-flash-high（替身、stub、`TestEnv` 與其自測）
**Implementer model:** haiku-xhigh（骨架簽章、`unimplemented!` 主體、遷移 alias）
**Changelog fragment:** `changelog.d/<issue>-rust-p4-scaffold.md`（`type: refactor`，`scope: rust-cli`）

**Files:**
- Create: `crates/sw-pal/src/service_ctl.rs`、`crates/sw-pal/src/service_ctl/{systemd.rs,windows_task.rs,windows_service.rs,posix.rs,windows.rs}`
- Create: `crates/sw-pal/src/process_tunnel/{mod.rs,posix.rs,windows.rs}`、`crates/sw-pal/src/proc_scan/{mod.rs,posix.rs,windows.rs}`、`crates/sw-pal/src/device_source/{mod.rs,windows_model.rs,windows_registry.rs}`（只放 `RegistryView` 與其原生實作；`WindowsDeviceSource` 留給 P5）
- Modify: `crates/sw-pal/src/{lib.rs,traits.rs,sysenv.rs,process.rs}`、Create: `crates/sw-pal/src/fs_ops.rs`（`FsOps: FsRead`）
- Create／擴充: `crates/sw-pal/src/fakes/{sysenv.rs,service.rs,tunnel.rs,proc.rs,process.rs,rpc.rs,registry.rs,fs.rs,platform.rs}`（建立在 P3 `FakeEnv`／`FakeClock`／`MemFs`／`ScriptedConnector` 之上）
- Create: `crates/sw-cli/src/commands/{daemon_cmd.rs,service_cmd.rs,supervision_cmd.rs,remote_cmd.rs,connect_cmd.rs,benches_cmd.rs}`、`crates/sw-cli/src/{doctor_cmd.rs,setup_cmd.rs,remote_tunnel.rs,install_layout.rs,rollback.rs,state_migrate.rs,pairing.rs,wslconf.rs,supervisor_defs.rs,graceful_stop.rs}`
- Modify: `crates/sw-cli/src/{lib.rs,ctx.rs,cli_spec.rs,client.rs,assets.rs}`（`assets.rs` 為 P3 04 Task 1 已建的 stub，不重建；`ctx.rs` 為 P3 既有檔，只加 `cli_version` 欄位；`client.rs` 加 `call` 薄包裝）
- Create: `crates/sw-cli/tests/support/mod.rs`、`crates/sw-cli/tests/support_selftest.rs`、`crates/sw-cli/tests/parser_mgmt.rs`
- Create: `tests/contract_blackbox/mgmt_fixtures.py`、`tests/contract_blackbox/stubs/bin/{systemctl,sudo,loginctl,ssh,autossh,schtasks,sc.exe,powershell.exe,minicom}`、`tests/contract_blackbox/stubs/stubtool.py`、`tests/contract_blackbox/delegate_sentinel.py`、`tests/contract_blackbox/test_mgmt_stubs.py`
- Create: `tests/acceptance/p4_case_map.yaml`、`tests/acceptance/p4_case_map.schema.json`、`scripts/rust/check_p4_cases.py`、`tests/test_rust_p4_case_map.py`
- Modify: `scripts/rust/mkbundle.sh`（新增 `--delegate-sentinel`、`--print-dir`）、`scripts/rust/capture_cli_help.py`（新增 `--rust-only`）、`tests/conftest.py`（`SERIALWRAP_INSTALL_ROOT` 加入隔離覆寫；`SW_STUB_LOG`、`SW_STUB_SCRIPT`、`SW_DELEGATE_MARKER`、`SERIALWRAP_RELEASE_MANIFEST` 加入 pop 清單；**不** pop 受測實作選擇器——P3 `tests/ab_env.py` 已在 pop 前擷取 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_AB_LABEL`）、`.project-policy.yml`（若 `code_paths` 缺 `crates/**`、`scripts/rust/**`、`contracts/**`）
- Modify: `docs/rust-refactor/plans/rust-architecture.md`（§0.5 所列章節；只改介面描述，不改決策）

**Interfaces:**
- Consumes：§0.2 全部（以 P3 實際名稱）。
- Produces（骨架；主體一律 `unimplemented!("P4-T<n>")`，`<n>` 為負責實作的 Task 編號）：

```rust
// sw-pal/src/traits.rs（全部型別在此 re-export：pub use crate::service_ctl::*; pub use crate::proc_scan::*; …）
pub trait SysEnv: Send + Sync {
    /* P3 既有 10 個方法不變 */
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput>;              // Task 2
    fn run_raw(&self, argv: &[String], env: Option<&[(OsString, OsString)]>, timeout: Option<Duration>) -> PalResult<CmdOutputRaw>;
    fn which(&self, name: &str) -> Option<PathBuf>;  fn which_all(&self, name: &str) -> Vec<PathBuf>;
    fn has_systemd(&self) -> bool;  fn is_wsl(&self) -> bool;  fn user_in_group(&self, g: &str) -> bool;
    fn user_identity(&self) -> String;     // POSIX: $USER > getpwuid(geteuid).pw_name；Windows: OpenProcessToken→GetTokenInformation(TokenUser)→LookupAccountSidW（advapi32）組 "DOMAIN\\name"
    fn local_appdata(&self) -> Option<PathBuf>;   // Windows: SHGetKnownFolderPath 不可用（SHELL32）→ 讀 %LOCALAPPDATA%，缺時 <USERPROFILE>\AppData\Local；POSIX: None
}
pub struct CmdOutput { pub rc: i32, pub stdout: String, pub stderr: String }
pub struct CmdOutputRaw { pub rc: i32, pub stdout: Vec<u8>, pub stderr: Vec<u8> }
pub trait FsOps: FsRead {
    fn atomic_write(&self, p: &Path, data: &[u8], mode: Option<u32>) -> PalResult<()>;  fn write_file(&self, p: &Path, data: &[u8]) -> PalResult<()>;
    fn mkdirs(&self, p: &Path) -> PalResult<()>;  fn replace(&self, from: &Path, to: &Path) -> PalResult<()>;  fn remove_file(&self, p: &Path) -> PalResult<()>;
    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()>;  fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()>;
    fn remove_tree(&self, p: &Path) -> PalResult<()>;  fn read_link(&self, p: &Path) -> PalResult<Option<PathBuf>>;  fn set_mode(&self, p: &Path, mode: u32) -> PalResult<()>;
    fn sync_dir(&self, p: &Path) -> PalResult<()>;  fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()>;
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind>;
    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<FileLockGuard>;
}
pub enum LinkKind { Symlink, Copied }
pub struct Capabilities { /* P3 ClientCaps 全欄位 */ pub on_demand_autostart: bool /* SC-18：僅 Windows native true */, pub console_kind: ConsoleKind }
pub struct Platform { pub env, pub fs: Arc<dyn FsOps>, pub clock, pub rpc, pub process: Arc<dyn ProcessCtl>, pub procs: Arc<dyn ProcScan>,
                      pub tunnel: Arc<dyn ProcessTunnel>, pub service: Arc<dyn ServiceCtl>, pub registry: Arc<dyn RegistryView + Sync>, pub caps: Capabilities }
pub type ClientPlatform = Platform;  pub type ClientCaps = Capabilities;  pub fn native() -> Platform;  pub fn native_client() -> Platform;
pub trait ProcessCtl: Send + Sync {
    fn exec_or_wait(&self, argv: &[OsString], env_add: &[(String, String)]) -> PalError;   // P3 既有
    fn spawn_daemon(&self, argv: &[String], env: &[(OsString, OsString)], detached: bool) -> PalResult<ChildHandle>;   // Task 4
    fn poll(&self, c: &ChildHandle) -> Option<i32>;  fn call_foreground(&self, argv: &[String], env: &[(OsString, OsString)]) -> PalResult<i32>;
}

// sw-pal/src/device_source/windows_model.rs（簽章逐字照 P5 Task 5，見 §0.5）
pub trait RegistryView: Send { fn enum_values(&self, key: &str) -> PalResult<Vec<(String, String)>>; fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>>;
                               fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>>; }
pub fn read_bt_ports(view: &dyn RegistryView) -> HashSet<String>;
pub fn exclude_bluetooth(serialcomm: &[(String, String)], bt: &HashSet<String>, exclude: &HashSet<String>) -> Vec<String>; // 07 §1.6
pub const SERIALCOMM_KEY: &str = "HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM";

// sw-pal/src/service_ctl.rs
pub enum SupervisionMode { OnDemand, SystemdUser, SystemdSystem, WindowsTask, WindowsService, Other(String) }
impl SupervisionMode { pub fn parse(s: &str) -> Self; pub fn as_str(&self) -> &str; pub fn is_supervised(&self) -> bool; }
pub enum ServiceAction { Start, Stop, Restart, Status }
impl ServiceAction { pub fn parse(s: &str) -> Option<Self>; pub fn as_str(&self) -> &'static str; }
pub struct ServiceResult { pub ok: bool, pub mode: String, pub action: String, pub rc: Option<i32>, pub stdout: Option<String>,
                           pub stderr: Option<String>, pub error_code: Option<&'static str>, pub hint: Option<String>, pub phase: Option<&'static str> }
pub struct SupervisorDefinition { pub mode: SupervisionMode, pub name: String, pub exec: Vec<String>, pub working_dir: Option<PathBuf>,
                                  pub run_user: Option<String>, pub env: Vec<(String, String)>, pub definition_path: Option<PathBuf> }
pub struct CmdRecord { pub argv: Vec<String>, pub rc: i32 }
pub enum TaskQuery { Running, NotRunning(String /* Ready|Disabled|Queued|Unknown */), NotFound, QueryFailed(String) }
pub trait ServiceCtl: Send + Sync {
    fn supported_modes(&self) -> Vec<SupervisionMode>;
    fn action(&self, mode: &SupervisionMode, action: ServiceAction, with_sudo: bool) -> ServiceResult;          // name="serialwrap"
    fn action_named(&self, mode: &SupervisionMode, name: &str, action: ServiceAction, with_sudo: bool) -> ServiceResult;
    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord>;
    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>>;                                       // 不執行，供 pending_sudo
    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord>;
    fn set_enabled(&self, mode: &SupervisionMode, enabled: bool, with_sudo: bool) -> Vec<CmdRecord>;
    fn query(&self, mode: &SupervisionMode) -> TaskQuery;                                                         // windows-task／windows-service；systemd 以 is-active
    fn hard_stop(&self, mode: &SupervisionMode, with_sudo: bool) -> ServiceResult;                                // windows-task `/End`；systemd `stop`
}
pub struct TaskXmlSpec { pub name: String, pub user_id: String, pub command: String, pub arguments: String, pub working_dir: String }
pub fn render_task_xml(spec: &TaskXmlSpec) -> String;
pub fn encode_utf16le_bom(s: &str) -> Vec<u8>;
pub fn shell_join_windows(args: &[String]) -> String;           // 純函式，MSVCRT／CommandLineToArgvW 規則的反向
pub fn split_windows_cmdline(s: &str) -> Vec<String>;           // 純函式自寫（不呼叫 SHELL32 CommandLineToArgvW），與 shell_join_windows 互逆
pub struct SystemdCtl { env: Arc<dyn SysEnv>, clock: Arc<dyn Clock> }           // 無 cfg：只產生並執行 argv
pub struct WindowsTaskCtl { env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock> }
pub struct WindowsServiceCtl { env: Arc<dyn SysEnv>, clock: Arc<dyn Clock> }
pub struct CompositeServiceCtl { modes: Vec<SupervisionMode>, systemd: SystemdCtl, task: WindowsTaskCtl, service: WindowsServiceCtl }

// sw-pal/src/proc_scan/mod.rs
pub struct DaemonProc { pub pid: u32, pub socket: Option<String>, pub tree: Vec<ProcIdentity> /* launcher＋子行程（Windows）；POSIX 只含自身 */ }
pub struct ProcIdentity { pub pid: u32, pub start_ticks: Option<u64> }
pub struct WinProcInfo { pub pid: u32, pub parent_pid: u32, pub image: String, pub argv: Vec<String>, pub creation_time: u64 }
pub enum ArgvFlavor { Posix, Windows }
pub fn dedup_launcher_children(procs: &[WinProcInfo]) -> Vec<DaemonProc>;
pub fn is_serialwrapd_argv(parts: &[String], flavor: ArgvFlavor) -> bool;   // multi_open.py:27-62 三規則＋第 4 規則（Task 4）
pub fn extract_socket_arg(parts: &[String]) -> Option<String>;              // multi_open.py:65-83
pub trait ProcScan: Send + Sync {
    fn serialwrapd_instances(&self) -> PalResult<Vec<DaemonProc>>;
    fn serialwrapd_tty_holders(&self, paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus);
    fn pid_alive(&self, pid: u32) -> bool;
    fn identity_alive(&self, id: &ProcIdentity) -> bool;   // pid 存活且 start_ticks 相符（None 時只看 pid）
}
pub struct PosixProcScan { proc_root: PathBuf }  impl PosixProcScan { pub fn with_root(p: &Path) -> Self; }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum HolderScanStatus { Ok, Permission, Unknown, Unsupported }   // arch §4.2 名稱；定義在 traits.rs，P5 06 的 `HolderReport.status`／`UartExclusive` 沿用同一型別（跨計畫審查：原稿用未定義的 `HolderStatus`）

// sw-pal/src/process_tunnel/mod.rs（arch §4.9 原樣）
pub struct TunnelProc { pub pid: u32, pub group: ProcGroupRef, pub start_ticks: Option<u64> }
pub enum ProcGroupRef { Pgid(i32), WinTree }
pub trait ProcessTunnel: Send + Sync { fn spawn_detached(..); fn alive(..); fn terminate(..); fn stderr_tail(..); fn supports_control_master(&self) -> bool; }

// sw-cli 各模組的對外函式簽章見 Task 6～17 的 Interfaces；Task 1 只放空殼。
```

測試替身（feature `test-fakes`；未設定的 argv：`run`／`run_raw` 回 `(0,"","")`，等同 Python FakeEffects）：

```rust
// sw-pal/src/fakes/*.rs
pub struct FakeSysEnv; impl FakeSysEnv { pub fn builder() -> FakeSysEnvBuilder; pub fn calls(&self) -> Vec<Vec<String>>; pub fn call_envs(&self) -> Vec<Option<Vec<(OsString,OsString)>>>; pub fn timeouts(&self) -> Vec<Option<Duration>>; }
impl FakeSysEnvBuilder {
    pub fn var(self, k: &str, v: &str) -> Self;  pub fn home(self, p: &Path) -> Self;  pub fn systemd(self, b: bool) -> Self;
    pub fn wsl(self, b: bool) -> Self;  pub fn in_group(self, g: &str) -> Self;  pub fn which(self, n: &str, p: &str) -> Self;
    pub fn which_all(self, n: &str, ps: &[&str]) -> Self;  pub fn command(self, argv: &[&str], rc: i32, out: &str, err: &str) -> Self;
    pub fn command_raw(self, argv: &[&str], rc: i32, out: &[u8], err: &[u8]) -> Self;  pub fn command_seq(self, argv: &[&str], seq: &[(i32, &str, &str)]) -> Self;
    pub fn command_err(self, argv: &[&str], kind: PalErrorKind, py_message: &str) -> Self;
    pub fn user_identity(self, s: &str) -> Self;  pub fn local_appdata(self, p: &Path) -> Self;  pub fn current_exe(self, p: &Path) -> Self;
    pub fn build(self) -> Arc<FakeSysEnv>;
}
impl FakeClock { pub fn new() -> Arc<Self>; pub fn elapsed(&self) -> Duration; }   // 擴充 P3 FakeClock：elapsed＝所有 sleep 與 advance 的總和
pub struct FakeProcScan; impl FakeProcScan { pub fn new(v: Vec<DaemonProc>) -> Arc<Self>; pub fn script(self: &Arc<Self>, seq: Vec<Vec<DaemonProc>>);
                                             pub fn fail(self: &Arc<Self>); pub fn alive_script(&self, pid: u32, seq: Vec<bool>); pub fn holders(&self, v: Vec<u32>, st: HolderScanStatus); }
pub struct FakeProcessCtl; impl FakeProcessCtl { pub fn new() -> Arc<Self>; pub fn spawns(&self) -> Vec<(Vec<String>, Vec<(OsString,OsString)>, bool)>;
                                                 pub fn poll_script(&self, seq: Vec<Option<i32>>); pub fn foreground_rc(&self, rc: i32); pub fn spawn_fail(&self, kind: PalErrorKind); }
pub struct FakeTunnel; impl FakeTunnel { pub fn new(supports_cm: bool) -> Arc<Self>; pub fn spawned(&self) -> Vec<(Vec<String>, PathBuf)>;
                                         pub fn set_alive(&self, pid: u32, alive: bool); pub fn terminated(&self) -> Vec<u32>; pub fn set_tail(&self, s: &str); pub fn terminate_fails(&self, pid: u32); }
pub struct FakeServiceCtl; impl FakeServiceCtl { pub fn new(modes: Vec<SupervisionMode>) -> Arc<Self>; pub fn actions(&self) -> Vec<(String, String, bool)>;   // (mode, action|"set_enabled:<b>"|"install"|"hard_stop", with_sudo)
                                                 pub fn respond(&self, r: ServiceResult); pub fn query_script(&self, seq: Vec<TaskQuery>); }
pub struct FakeRpcConnector; impl FakeRpcConnector { pub fn new() -> Arc<Self>;                 // 包 P3 ScriptedConnector；預設所有 endpoint ConnectionRefused
    pub fn respond(&self, ep: &str, method: &str, line: &str); pub fn respond_seq(&self, ep: &str, method: &str, lines: &[&str]);
    pub fn refuse_with(&self, ep: &str, kind: PalErrorKind); pub fn fail_after_send(&self, ep: &str, method: &str); pub fn recv_timeout(&self, ep: &str, method: &str);
    pub fn requests(&self) -> Vec<(String, String, String)>; }      // (endpoint, method, params JSON)
pub struct FakeRegistry; impl FakeRegistry { pub fn new(serialcomm: Vec<(&str,&str)>, bt: &[&str]) -> Arc<Self>; pub fn fail(&self); }
pub fn real_fs() -> Arc<dyn FsOps>;      // 真實檔案系統（tempdir 內使用）
pub struct FaultFs; impl FaultFs { pub fn wrap(inner: Arc<dyn FsOps>) -> Arc<Self>; pub fn fail_replace_when_src_ends_with(&self, suffix: &str); pub fn fail_copy_tree(&self); }
pub fn platform() -> FakePlatformBuilder;   // .env() .procs() .process() .tunnel() .service() .rpc() .registry() .fs(Arc<dyn FsOps>) .clock(Arc<FakeClock>) .caps(Capabilities) .build() -> Platform
pub fn posix_caps() -> Capabilities;  pub fn windows_caps() -> Capabilities;   // windows_caps: console_kind=TcpTelnet, native_backend=Win, path_flavor=Windows, on_demand_autostart=true, text_newline="\r\n"
```

`crates/sw-cli/tests/support/mod.rs`（`TestEnv`；所有 builder 回 `Self`、所有元件預設為 fake，未設定的 fake 呼叫即 panic `fake component not configured: <name>`，只有 `rpc` 預設為「全部 refused」、`procs` 預設為空清單）：

| API | 語意 |
|---|---|
| `TestEnv::posix()`／`TestEnv::windows()` | tmp `HOME`、隔離 `RuntimePaths`（`SERIALWRAP_*` 指向 tmp 子目錄）、`posix_caps()`／`windows_caps()`、`FakeClock`、`real_fs()` 根於 tmp |
| `with_config(&str)`、`config_path()`、`home()`、`root() -> InstallRoot`、`ctx() -> CliCtx`、`bundle_dir()`、`TestEnv::default_socket() -> String`（POSIX 隔離 `SOCKET_PATH`）、`TestEnv::win_endpoint() -> &'static str`（`tcp://127.0.0.1:48700`） | 寫 `CONFIG_DIR/config.yaml`；取得路徑與 ctx |
| `with_var(k, v)`、`with_systemd(b)`、`with_wsl(b)`、`with_which_missing(name)`、`without_group(g)`、`all_required_ok()` | 轉給 `FakeSysEnvBuilder`；`all_required_ok` 設定 doctor 非 advisory 檢查全部通過的最小環境 |
| `with_procs(Vec<DaemonProc>)` | `FakeProcScan::new` |
| `with_manifest_launcher()`、`with_manifest_runtime_ok(pyver)` | 在 `bundle_dir()` 建 Task 17 形狀的 manifest 與 `bin/serialwrapd`（或 runtime），`current_exe` 指向 `bundle_dir()/bin/serialwrap`；`runtime_ok` 另腳本化 runtime 的 `-c` 版本查詢 |
| `with_rpc_ok(ep, method, line)`、`with_rpc_after_spawn(ep, &[methods])`、`with_rpc_refused_then_ok(ep, method, line)`、`with_rpc_fail_after_send(ep, method)`、`with_rpc_recv_timeout(ep, method)`、`with_rpc_refused_kind(ep, kind)` | `FakeRpcConnector`；`after_spawn`＝第一次 `spawn_daemon` 之後才回 ok |
| `with_daemon_alive_then_gone(ep, pid, n)`、`with_daemon_alive_forever(ep, pid)` | ping 在第 n 次輪詢（含）起 refused，且 `FakeProcScan::alive_script(pid)` 同步轉 false；`forever` 兩者永遠存活 |
| `with_registry_state(port, PyDict)`、`with_benches_yaml(&str)`、`with_bench_state(&str)`、`with_tunnel_ready()`、`bench_state_path()` | remote／bench 檔案與 `FakeTunnel` |
| `with_install_root()`、`with_current(v, prev: Option<&str>)`、`with_installed(v)`、`with_bundle(v)`、`make_version_dir(&root, v, files) -> PathBuf`、`version_bytes(v, rel)`、`backup_dir_of(&json) -> PathBuf`、`write_file(p, bytes)` | 安裝佈局建構（`with_bundle` 用 Task 17 manifest 形狀並計算真 sha256） |
| `with_fs_fault_replace_suffix(s)` | 以 `FaultFs` 包住 fs |
| `with_new_daemon_reporting_version(v)` | 新 spawn 的 daemon `health.status.version` 回 v |
| `with_crash_at(point: &str)`、`clear_crash()`、`journal_json() -> Option<serde_json::Value>`、`journal_bytes() -> Option<Vec<u8>>`、`config_bytes() -> Vec<u8>`、`recover() -> Option<sw_cli::rollback::RecoveryReport>`、常數 `CRASH_POINTS: &[&str]` | Task 12 中斷注入（R1-F5）：`Ctx` 的 `#[cfg(feature="test-fakes")]` `FaultPlan` 在指名的 crash point（`"<journal_step>:<before_effect|after_effect>"` 或步驟內點如 `"backup_started:after_config_copy"`、`"entries_switching:after_first_rename"`）讓 `sw_cli::run` 立即回 `(137,"","")`，不執行任何後續副作用或 Drop 以外的清理（等同行程被殺；lock guard 的 Drop 視同 OS 釋放）；fake service／rpc／procs／fs 狀態保留供下一次 `run`。`recover()` 直接呼叫 `recover_journal`（持 `install_lock`）。`CRASH_POINTS` 列舉 Task 12 每個 `JournalStep` 的 `before_effect`／`after_effect` 與上列步驟內點，Task 12 實作新增 crash point 時必須同步補進此常數（`crash_points_cover_every_journal_step` 測試核對） |
| `with_supervisor_state(mode, enabled, running)`、`with_idle_daemon(ep, pid)`、`with_spawn_exits(rc)`、`service().query_now() -> {enabled: Option<bool>, running: bool}`、`rpc_now(ep, method) -> serde_json::Value` | Task 12 中斷注入用的**有狀態**監管／daemon 模型：`with_supervisor_state` 寫 config 的 `supervision_mode` 並讓 `FakeServiceCtl` 依 `set_enabled`／`start`／`stop`／`install` 動作維護 enabled／running（`query_script` 由此狀態推導，`TaskQuery::NotRunning("Disabled")`＝enabled false）；`with_idle_daemon` 讓 endpoint 的 `health.ping`／`mcu.status`／`session.list` 回閒置 daemon（`flashing:false`、單一 COM0 READY 無 busy）、`ProcScan` 回該 pid，收到 `daemon.handoff_stop` 時回 `{"ok":true,"stopping":true,"handoff_token":<32 hex>}`（R1-F1；update／rollback 的唯一停止 RPC）、收到 `daemon.stop`（僅使用者明確停止；update／rollback 含步驟 7 回退與 journal 復原一律不送，R2-F6）時直接接受，兩者之後皆依 `with_daemon_alive_then_gone` 語意結束、之後 `start`／spawn 再復活；`with_new_daemon_handoff_script(&[&str])`（R2-F6）只作用於**由 update／rollback 步驟 6（`start_and_verify`）啟動的 daemon 實例**：自設定起它收到的第 k 個 `daemon.handoff_stop` 回 `lines[k]`（例如 `SESSION_BUSY`＋`ops:["file.push"]`、`FLASHING_BUSY`＋`ops:["flash"]`、`METHOD_NOT_FOUND`，或特殊字串 `"<recv_timeout>"` 模擬 recv 逾時），非 grant 回應時該實例**不結束**、ping 持續 ok、`ProcScan` 仍回其 pid；lines 用盡後回閒置 grant 並依上述語意結束（模擬「新版已受理的傳輸／燒錄完成後才閒置」）；`with_spawn_exits(rc)` 讓下一次 spawn 的 daemon 立即以 rc 結束；`service().actions()` 的動作字串為 `ServiceAction::as_str()`（`start`、`stop`、`set_enabled:<b>`、`install`…）；`query_now`／`rpc_now` 讀當下模型狀態 |
| `run(&[&str]) -> (i32, String, String)`、`run_from_bundle(&[&str])` | 以 `sw_cli::run` 執行；`run_from_bundle` 先把 `current_exe` 指到 `bundle_dir()/bin/serialwrap` |
| `calls()`、`process()`、`rpc()`、`tunnel()`、`service()`、`clock()` | 取替身 |
| `event_order() -> EventOrder`；`EventOrder::index_of(&str) -> usize`、`EventOrder::index_of_nth(&str, n: usize) -> usize`（第 n 次出現，0 起算；R2-F6）；`rpc_params(&t, method)`／`rpc_params_nth(&t, method, n)`（該方法第一次／第 n 次請求的 params） | 合併 `rpc:<method>`、`run:<argv join ' '>`、`spawn:<argv0>`、`service:<action>` 事件，依 `FakeClock` 單調序號（同時刻依發生序）；`rpc:health.ping#gone` 指「第一次回 refused 的 ping」；找不到即 panic |
| 自由函式 `json(&str)`、`s`、`i`、`b`、`dict`、`list`、`argv_list`、`sha256_hex`、`manifest_fixture(v)`、`alive_state(role, pid)`、`alive_state_with_identity(role, pid, id)`、常數 `B1_YAML` | 斷言輔助 |

`tests/contract_blackbox/mgmt_fixtures.py`（以 `pytest_plugins` 載入；**沿用** P3 `tests/contract_blackbox/{bb_harness,bb_stubd,bb_faketarget,bb_daemon}.py` 與其 fixtures `env`、`stubd`、`py_daemon`、`faketarget`、marker `rust_only`）只新增：`stub_path`（PATH 前置 stubs/bin，並保留 `/usr/bin:/bin`）、`stub_log` 讀取器、`no_delegation`（測後斷言 `SW_DELEGATE_MARKER` 不存在）、`mgmt_bundle`（`mkbundle.sh --dev --delegate-sentinel --print-dir` 產物，提供 `.cli`、`.root`、`.materialized_bin`）、`run_wrapper(path, args, env) -> CompletedProcess`（以 `bash` 執行物化 wrapper，逾時 30 s）、`live_daemon`（以 P3 `PyDaemon`＋`FakeTarget` 在隔離 env 啟動真 Python daemon，提供 `.endpoint` 與 `.console_list(com)`——後者以 `bb_stubd` 內的最小 NDJSON client 送 `session.console_list`，不經產品）、`pytest.mark.case(id)` 註冊。受測實作判定一律用 `bb_harness.is_rust()`（`SERIALWRAP_AB_LABEL` 以 `rust` 開頭，P3 L384-395）；Rust 輪的命令列為 `SERIALWRAP_CLI_EXE=<bundle>/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest …`。

黑箱 stub：`tests/contract_blackbox/stubs/bin/<name>` 皆為 `#!/usr/bin/env python3` 薄殼，呼叫 `stubtool.py <name> "$@"`：把 `{"tool":name,"argv":[...],"cwd":...}` 以一行 JSON append 到 `$SW_STUB_LOG`；依 `$SW_STUB_SCRIPT`（JSON：`{"<name> <argv join ' '>": {"rc":0,"stdout":"","stderr":""}}`，key 支援結尾 `*` 前綴比對）回應；未命中回 rc 0、空輸出。`minicom` stub 另把收到的 `-D` 路徑寫進 log 並在 `SW_STUB_MINICOM_HOLD_S` 秒後退出。`delegate_sentinel.py`：寫 `$SW_DELEGATE_MARKER`（預設 `<bundle>/DELEGATED`）後 `sys.exit(97)`。

- [ ] **步驟 1：寫失敗測試**

`tests/contract_blackbox/test_mgmt_stubs.py`：

```python
import json, os, subprocess, sys
from pathlib import Path

STUBS = Path(__file__).parent / "stubs" / "bin"

def _run_stub(name, args, tmp_path, script=None):
    log = tmp_path / "stub.ndjson"
    env = dict(os.environ, SW_STUB_LOG=str(log))
    if script is not None:
        env["SW_STUB_SCRIPT"] = json.dumps(script, ensure_ascii=False)
    p = subprocess.run([str(STUBS / name), *args], capture_output=True, text=True, env=env)
    rows = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return p, rows

def test_stub_logs_argv_and_defaults_to_rc0(tmp_path):
    p, rows = _run_stub("systemctl", ["--user", "start", "serialwrap"], tmp_path)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    assert rows == [{"tool": "systemctl", "argv": ["--user", "start", "serialwrap"], "cwd": os.getcwd()}]

def test_stub_scripted_response_with_prefix(tmp_path):
    script = {"schtasks /Query*": {"rc": 1, "stdout": "", "stderr": "錯誤: 系統找不到指定的檔案。"}}
    p, _ = _run_stub("schtasks", ["/Query", "/TN", "serialwrap"], tmp_path, script)
    assert p.returncode == 1 and p.stderr == "錯誤: 系統找不到指定的檔案。"

def test_delegate_sentinel_exits_97_and_writes_marker(tmp_path):
    marker = tmp_path / "DELEGATED"
    p = subprocess.run([sys.executable, str(Path(__file__).parent / "delegate_sentinel.py"), "doctor"],
                       env=dict(os.environ, SW_DELEGATE_MARKER=str(marker)))
    assert p.returncode == 97 and marker.exists()
```

（三個 pytest 均加 `@pytest.mark.xfail(strict=True, reason="P4-T1 紅燈")`，步驟 3 完成時移除。）

`crates/sw-pal/tests/fakes_selftest.rs`（只驗替身與可編譯介面，**不**對骨架主體寫 `should_panic`——那類測試會在後續 Task 實作後永久變紅）：

```rust
use sw_pal::fakes::*;
#[test]
fn fake_sysenv_records_calls_and_scripted_rc() {
    let env = FakeSysEnv::builder().command(&["systemctl", "--user", "is-active", "serialwrap"], 3, "inactive\n", "").build();
    let out = env.run(&["systemctl".into(), "--user".into(), "is-active".into(), "serialwrap".into()], None).unwrap();
    assert_eq!((out.rc, out.stdout.as_str()), (3, "inactive\n"));
    let out2 = env.run(&["loginctl".into(), "enable-linger".into(), "alice".into()], None).unwrap();
    assert_eq!(out2.rc, 0);
    assert_eq!(env.calls().len(), 2);
}
#[test]
fn fake_platform_unconfigured_component_panics_not_real_os() {
    let pf = platform().build();
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| pf.tunnel.supports_control_master()));
    assert!(r.is_err());
}
#[test]
fn p3_alias_still_compiles() {
    let _: fn() -> sw_pal::ClientPlatform = sw_pal::native_client;
}
```

`crates/sw-cli/tests/support_selftest.rs`：`event_order_merges_by_clock_then_sequence`、`daemon_alive_then_gone_flips_on_nth_poll`（n=3：前 2 次 ping ok 且 pid 存活，第 3 次起皆失敗）、`unconfigured_fake_panics_with_name`。

`crates/sw-cli/tests/parser_mgmt.rs`（Task 1 新增的 parser 擴充先寫測試）：`supervision_mode_help_matches_rust_only_golden`（`--help` 與 `contracts/cli_help/rust_only/supervision-mode.{80,120}.txt` 逐位元組相同）、`setup_help_matches_rust_only_golden`、`setup_rollback_and_user_mutually_exclusive`（`setup --user --rollback` → exit 2、argparse 互斥訊息逐字）、`supervision_mode_words_nargs_star`（`supervision-mode set systemd-user --force` 解析出 `words=["set","systemd-user"]`、`force=true`）。

`tests/test_rust_p4_case_map.py`：`p4_case_map.yaml` 通過 schema；每個 P0 `behavior_changes.yaml` 中 phases 含 P4 的 change 其 `case_ids` 全部出現在 map；`cases.lock.json` `sets.cli_1B` 中 `implemented_by` 為本檔者全部出現；每筆 `tests` 非空且節點存在（Rust 以 `cargo test -- --list` 比對、pytest 以 `--collect-only`）。

- [ ] **步驟 2：執行並確認 FAIL**

```bash
python3 -m pytest -q tests/contract_blackbox/test_mgmt_stubs.py tests/test_rust_p4_case_map.py   # 預期：xfail（stub／map 尚未建立）
cargo test -p sw-pal --features test-fakes --test fakes_selftest      # 預期：編譯失敗 unresolved import `sw_pal::fakes::FakeSysEnv`
cargo test -p sw-cli --test parser_mgmt --test support_selftest       # 預期：help golden 不存在／斷言失敗
```

- [ ] **步驟 3：實作**

1. **P3→P4 遷移**（§0.2）：`Platform`／`Capabilities` 超集、舊名 alias、`FsOps: FsRead`、`CliCtx.cli_version`、`client::call` 包裝、`mode_str`；`cargo test --workspace` 中 P3 既有測試必須全部維持原結果。
2. 依 Produces 建立所有檔案與簽章；主體 `unimplemented!("P4-T<n>")`。`sw-pal/src/lib.rs` 的 `native()` 組 `CompositeServiceCtl`（POSIX：`modes=[OnDemand, SystemdUser, SystemdSystem]`；Windows：`[OnDemand, WindowsTask, WindowsService]`），這是唯一出現 `cfg` 的位置之一。
3. 替身全部實作完成（非 `unimplemented!`），因為其他 Task 的紅燈要靠它們。`FakePlatformBuilder` 未指定的元件用「呼叫即 panic `fake component not configured: <name>`」的實作，避免測試意外走到真實 OS。
4. `cli_spec.rs`：8 個群組的完整宣告式 parser（01 §6.1、§6.11～§6.19 逐字），加上 Rust-only 擴充：`supervision-mode` 新增 positional `words`（`nargs='*'`，metavar `set MODE`，help `不帶參數：印出目前監管模式；set MODE：切換並執行 reconcile`）、`--force`（help `flash 進行中仍強制切換`）、`--with-sudo`（dest `with_sudo`，help `允許代跑需要 root／系統管理員權限的指令`）；`setup` 的互斥群組追加 `--windows-task`（dest `windows_task`，help `Windows：以工作排程器登入自啟監管（Windows 預設）`）、`--windows-service`（dest `windows_service`，help `Windows：以 Windows 服務監管（OD-12：只列出待執行指令）`）、`--rollback`（help `回滾到上一個完整發佈組合（Previous）`）。`daemon restart`、`remote` 的 `stop` 字詞照 P1 Python parser。
5. `scripts/rust/capture_cli_help.py --rust-only`：`import sw_core.cli`，取 `build_parser()`，以 `_SubParsersAction.choices` 找到 `supervision-mode`、`setup` 子 parser 並以上述相同 `add_argument` 呼叫擴充，輸出 `contracts/cli_help/rust_only/{supervision-mode,setup}.{80,120}.txt`。
6. `scripts/rust/mkbundle.sh`：`--delegate-sentinel` 時 manifest `components.python_cli_delegate.entry` 改為 `["py/bin/python3","-I","<repo>/tests/contract_blackbox/delegate_sentinel.py"]`（Windows 用 `py\\python.exe`）；`--print-dir` 只把 bundle 絕對路徑印到 stdout。
7. `tests/conftest.py`：`os.environ["SERIALWRAP_INSTALL_ROOT"] = _iso("install")`；pop 清單加 `SW_STUB_LOG`、`SW_STUB_SCRIPT`、`SW_DELEGATE_MARKER`、`SERIALWRAP_RELEASE_MANIFEST`。新增 `test_ab_label_survives_root_conftest`（以 `SERIALWRAP_AB_LABEL=rust-1b` 啟動子 pytest 時 `bb_harness.is_rust()` 為真）。
8. `p4_case_map.yaml`、schema、`check_p4_cases.py`（`--freeze`／`--check`，§0.4）。
9. `rust-architecture.md` 依 §0.5 修訂介面描述。

- [ ] **步驟 4：執行並確認 PASS**

```bash
python3 -m pytest -q tests/contract_blackbox/test_mgmt_stubs.py tests/test_rust_p4_case_map.py
cargo test -p sw-pal --features test-fakes --test fakes_selftest
cargo test -p sw-cli --test parser_mgmt --test support_selftest
cargo test --workspace && cargo build --workspace --target x86_64-pc-windows-gnu
python3 scripts/rust/check_cfg_boundary.py && python3 -m pytest -q tests/ && python3 -m policy_check --repo .
```

- [ ] **步驟 5：commit**：`refactor(rust-cli): 建立 Stage 1B 管理面介面骨架、P3 遷移與測試替身`

**BLOCKED-OWNER：** 無。

---

## Task 2：PAL `SysEnv` 管理面方法與 Windows 登錄讀取（`RegistryView`）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max（PAL／平台碼，master plan §2「實作 B」）
**Changelog fragment:** `changelog.d/<issue>-rust-p4-pal-sysenv.md`（`type: feat`，`scope: sw-pal`）

**Files:**
- Modify: `crates/sw-pal/src/sysenv.rs`、`crates/sw-pal/src/sysenv/{posix.rs,windows.rs}`
- Modify: `crates/sw-pal/src/device_source/{windows_model.rs,windows_registry.rs}`
- Create: `crates/sw-pal/tests/sysenv_mgmt.rs`、`crates/sw-pal/tests/registry_view.rs`、`crates/sw-pal/tests/golden/run_decode.ndjson`（由 `scripts/rust/gen_run_decode_golden.py` 以 Python 3.12 `subprocess.run(text=True)` 產生）、`scripts/rust/gen_run_decode_golden.py`

**Interfaces:**
- Consumes：P3 `SysEnv` 既有 10 個方法。
- Produces：`SysEnv::{has_systemd, is_wsl, user_in_group, which, which_all, run, run_raw, user_identity, local_appdata}`（P3 全部未提供，本 Task 實作）、`RegistryView` 原生實作（`WinRegistryView`；POSIX `NullRegistryView` 一律 `Err(Unsupported)`）、`read_bt_ports`、`exclude_bluetooth`。

行為（逐字照 07 §4.2、§1.6；Python 基線 `sw_core/sysenv.py`）：
- `has_systemd`：`/run/systemd/system` 為目錄；任何錯誤 → false。Windows：恆 false。
- `is_wsl`：`uname().release` 小寫含 `microsoft`；Windows：恆 false。
- `user_in_group(g)`：Windows 恆 false；POSIX `getgrnam(g).gr_gid ∈ getgroups() ∪ {getegid()}`。
- `which(name)`：`shutil.which` 語意（PATH 逐目錄，POSIX 需 `X_OK`；Windows 依 `PATHEXT`，`name` 已含副檔名時不加）。
- `which_all(name)`：07 §4.2 演算法（`PATHEXT` 預設 `.COM;.EXE;.BAT;.CMD`、略過空目錄字串、依序去重、例外 → 空）。
- `run`：等同 `subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=…)`（`sysenv.py:81-87`）：
  - 回 `(rc, stdout, stderr)`；逾時 → `(-1,"","TIMEOUT")`；執行檔不存在 → `Err(PalError{kind:NotFound, py_message:"[Errno 2] No such file or directory: '<argv0>'"})`（POSIX）／`"[WinError 2] 系統找不到指定的檔案。"`（Windows，FormatMessageW 系統語系）。
  - 解碼＝Python text mode：encoding 為 locale preferred encoding（POSIX 隔離環境下為 UTF-8；Windows 為 `GetACP()` 代碼頁，經 `MultiByteToWideChar(MB_ERR_INVALID_CHARS)`），**strict**；解碼失敗 → `Err(PalError{kind:Io, py_message:<Python UnicodeDecodeError 的 str()>})`，呼叫端照 Python 該處的例外路徑處理（不得以 replacement 字元繼續）。
  - universal newlines：`\r\n`→`\n`、單獨 `\r`→`\n`（stdout 與 stderr 皆然）。
- `run_raw`：回原始 bytes，不解碼、不換行轉換；`env=Some(..)` 時以該 env（`OsString`）**取代**子行程環境。
- `user_identity`：POSIX `$USER` 非空優先，否則 `getpwuid(geteuid()).pw_name`；Windows `OpenProcessToken(TOKEN_QUERY)`→`GetTokenInformation(TokenUser)`→`LookupAccountSidW` 組 `"<ReferencedDomainName>\\<Name>"`（全在 ADVAPI32，符合 arch §1.1 白名單）；失敗時退回 `%USERDOMAIN%\%USERNAME%`。
- `local_appdata`：Windows `%LOCALAPPDATA%`（非空）否則 `%USERPROFILE%\AppData\Local`；POSIX `None`。
- `WinRegistryView`（Windows）：`enum_values("HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM")` 以 `RegOpenKeyExW`＋`RegEnumValueW` 依序回 `(value-name, REG_SZ 資料)`；key 不存在 → `Ok(vec![])`；`enum_subkeys`／`query_string` 同 P5 L983 語意。
- `read_bt_ports(view)`：遞迴 `HKLM\SYSTEM\CurrentControlSet\Enum\BTHENUM`，讀每個 `<sub>\Device Parameters` 的 `PortName`（`REG_SZ` 才收），吞掉所有錯誤。
- `exclude_bluetooth`：`com ∈ bt || com ∈ exclude || device_path.to_lowercase().contains("bthmodem")` → 剔除；其餘依輸入順序收 `com`。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-pal/tests/registry_view.rs` 與 `sysenv_mgmt.rs`）

```rust
use std::collections::HashSet;
use sw_pal::device_source::windows_model::exclude_bluetooth;
#[test]
fn exclude_bluetooth_matches_python_rules_and_keeps_order() {
    let sc = vec![("\\Device\\Serial0".to_string(), "COM3".to_string()),
                  ("\\Device\\BthModem0".to_string(), "COM4".to_string()),
                  ("\\Device\\VCP0".to_string(), "COM10".to_string()),
                  ("\\Device\\VCP1".to_string(), "COM7".to_string())];
    let bt: HashSet<String> = ["COM7".to_string()].into();
    let ex: HashSet<String> = ["COM10".to_string()].into();
    assert_eq!(exclude_bluetooth(&sc, &bt, &ex), vec!["COM3".to_string()]);
}
```

表列案例（`sysenv_mgmt.rs`，POSIX 案例以 `tempfile` 建 PATH 目錄；Windows 案例名稱以 `cap_` 前綴之外的一般測試在 `win-test.sh` 原生執行）：

| 測試名 | 斷言 |
|---|---|
| `which_all_dedups_in_path_order` | PATH=`a:b:a`，a、b 各有可執行 `serialwrap` → `[a/serialwrap, b/serialwrap]` |
| `which_all_skips_non_executable_posix` | b/serialwrap mode 0644 → 只回 a |
| `which_all_empty_path_returns_empty` | PATH="" → `[]` |
| `which_all_pathext_windows`（原生 Windows） | PATH 目錄含 `serialwrap.exe`、`PATHEXT=.EXE;.CMD` → `[<dir>\\serialwrap.exe]`；`which_all("serialwrap.exe")` 不重複加副檔名 |
| `run_timeout_returns_minus1_timeout` | `run(["sleep","5"], Some(0.2s))` → `(-1,"","TIMEOUT")` |
| `run_missing_exe_is_notfound_with_py_message` | `run(["no-such-exe-x"])` → `Err`，`kind==NotFound`，`py_message=="[Errno 2] No such file or directory: 'no-such-exe-x'"` |
| `run_universal_newlines_matches_python_golden` | `printf 'a\r\nb\rc\n'` → stdout 與 `golden/run_decode.ndjson` 的 Python 結果相同（`"a\nb\nc\n"`） |
| `run_invalid_utf8_is_decode_error_like_python` | `printf '\xff'` → `Err(kind=Io)`，`py_message` 與 golden 中 Python `UnicodeDecodeError` 字串相同 |
| `run_windows_acp_decode_golden`（原生 Windows） | `cmd /c echo` 輸出 ACP 中文 → 與 Windows Python 3.12 `text=True` 結果相同 |
| `run_raw_replaces_env` | `run_raw(["env"], Some(&[("A","1")]))` stdout 只含 `A=1` 這一行（加上 OS 強制變數除外：POSIX 無） |
| `run_raw_preserves_nul_bytes_and_crlf` | `run_raw(["printf","a\\0b\\r\\n"])` stdout == `b"a\0b\r\n"` |
| `user_identity_posix_prefers_user_env` | `USER=alice` → `"alice"` |
| `user_identity_windows_domain_backslash_name`（原生 Windows） | 結果含 `\` 且等於 `whoami` 輸出（不分大小寫） |
| `has_systemd_false_when_dir_absent`（以 `SysEnvPosix::with_root(tmp)` 測試建構子） | tmp 無 `run/systemd/system` → false；建立後 → true |
| `registry_view_posix_unsupported` | POSIX `NullRegistryView.enum_values(SERIALCOMM_KEY)` → `Err(kind=Unsupported)`；`read_bt_ports` 空 |
| `registry_view_windows_serialcomm_readable`（原生 Windows） | `enum_values(SERIALCOMM_KEY)` 回 `Ok`（可為空），每筆 COM 名以 `COM` 開頭 |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-pal --features test-fakes --test registry_view --test sysenv_mgmt` → `not implemented: P4-T2`。
- [ ] **步驟 3：實作**：依「行為」逐條實作；Windows registry 只在 `device_source/windows_registry.rs`、token／帳號查詢只在 `sysenv/windows.rs`。`SysEnvPosix::with_root(root)` 為 `#[cfg(feature="test-fakes")]` 測試建構子，正式路徑 root=`/`。完成後 `objdump -p` 確認 Windows 測試二進位匯入 DLL 仍在 arch §1.1 白名單內。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-pal --features test-fakes --test registry_view --test sysenv_mgmt
scripts/rust/win-test.sh --package sw-pal -- sysenv_mgmt registry_view
python3 scripts/rust/check_cfg_boundary.py
```

- [ ] **步驟 5：commit**：`feat(sw-pal): 補齊管理面 SysEnv 與 Windows SERIALCOMM 讀取`

**BLOCKED-OWNER：**
- Windows 實機 SERIALCOMM 含藍牙與 USB-UART 混合列舉的實測（需 OD-05 機台接上實體 USB-UART 與藍牙 COM；本環境 interop 只能驗證空／既有登錄內容）。
- OD-08：本 Task 動到 PAL，PR 必附原生 Windows COM handle 證據（com0com DUT-side pair 或實體 USB-UART 直連 Windows）；本環境無法產出，PR 標示證據待補。

---

## Task 3：PAL `ServiceCtl`：systemd、`windows-task`（Task XML／schtasks）、`windows-service`（sc.exe）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-service-ctl.md`（`type: feat`，`scope: sw-pal`）

**Files:**
- Modify: `crates/sw-pal/src/service_ctl.rs`、`crates/sw-pal/src/service_ctl/{systemd.rs,windows_task.rs,windows_service.rs,posix.rs,windows.rs}`
- Create: `crates/sw-pal/tests/service_ctl.rs`、`crates/sw-pal/tests/golden/serialwrap.task.xml`

**Interfaces:**
- Consumes：`SysEnv::run`、`SysEnv::user_identity`、`FsOps::atomic_write`、`Clock::sleep`、`Clock::mono_ns`。
- Produces：Task 1 的 `ServiceCtl` 系列全部實作。

行為：
- **systemd-user／systemd-system 的 `action`**：逐字照 07 §5.2（`systemctl --user <a> serialwrap`；system 特權 action 無 `with_sudo` → `NEEDS_SUDO`，hint `需 root：請執行 \`sudo systemctl <a> serialwrap\`（或加 --with-sudo 讓本指令代跑）`；status 不加 sudo）。`ServiceResult` 成功形狀 `{ok:rc==0, mode, action, rc, stdout, stderr}`。
- **on-demand／未知模式**：`NO_SYSTEMD`，hint `目前為 on-demand 監管模式（無 systemd 服務）。使用 \`serialwrap daemon start/stop\`。`（Windows 同字串，KEEP）。
- **systemd 的 `query`**：`systemctl [--user] is-active serialwrap`：stdout trim ∈ {`active`,`activating`,`deactivating`,`reloading`} → `Running`；∈ {`inactive`,`failed`} → `NotRunning`；rc 4（unit 不存在）→ `NotFound`；其他（含執行失敗、逾時 5 s）→ `QueryFailed`。`hard_stop` = `action(Stop)`。systemd 的 `action(Stop|Restart)` 行為不變（雙邊 A/B，Python 以 `systemctl` 送 SIGTERM，daemon 端 SC-11 有序關機）。
- **windows-task**（Rust-only，SC-24）：
  - start `["schtasks","/Run","/TN",name]`；status `["schtasks","/Query","/TN",name,"/FO","LIST","/V"]`；`hard_stop`＝`["schtasks","/End","/TN",name]`（只在 `graceful_stop` 逾時後由 sw-cli 呼叫）。**`action(Stop)` 與 `action(Restart)` 在 PAL 層不組合**——安全停止需要 RPC `daemon.stop` 與行程身分確認，屬 sw-cli `graceful_stop`（Task 7）；PAL 收到這兩個 action 回 `{ok:false, error_code:"INTERNAL_ERROR", hint:"windows-task 的 stop／restart 必須經 graceful_stop 組合"}` 且不執行任何指令（sw-cli 永不走到此分支，測試守住）。不需要 `with_sudo`。
  - `install(def)`：`user_id=env.user_identity()`；`render_task_xml(TaskXmlSpec{name, user_id, command:def.exec[0], arguments: shell_join_windows(def.exec[1..]), working_dir})` → `encode_utf16le_bom` → `fs.atomic_write(def.definition_path)` → 執行 `["schtasks","/Create","/XML",<definition_path>,"/TN",name,"/F"]`，回 `[CmdRecord]`。
  - `set_enabled`：`["schtasks","/Change","/TN",name,"/ENABLE"|"/DISABLE"]`。`uninstall`：`["schtasks","/Delete","/TN",name,"/F"]`。
  - `query`：執行 `["powershell.exe","-NoProfile","-NonInteractive","-Command","try { (Get-ScheduledTask -TaskName '<name>' -ErrorAction Stop).State.ToString() } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NotFound' } else { exit 3 } }"]`（逾時 5 s）。stdout trim：`Running`／`Queued` → `Running`；`Ready`／`Disabled`／`Unknown` → `NotRunning(<值>)`；`NotFound`（由 `ObjectNotFound` 類別判定，不比對語系訊息）→ `NotFound`；rc≠0、空輸出、其他字串、逾時、執行檔不存在 → `QueryFailed(<rc 與 stderr 摘要>)`。**`QueryFailed` 永不視為已停止**。
- **windows-service**（Rust-only，SC-24；OD-12 限制）：start `["sc.exe","start",name]`、status `["sc.exe","query",name]`、`hard_stop`＝`["sc.exe","stop",name]`；`action(Stop|Restart)` 同 windows-task 交由 `graceful_stop`。start 與 `hard_stop` 無 `with_sudo` → `{ok:false,error_code:"NEEDS_SUDO",hint:"需系統管理員權限：請在系統管理員 PowerShell 執行 \`sc.exe <a> serialwrap\`（或加 --with-sudo 讓本指令代跑）"}`（restart 的 hint 中 `<a>` 寫成 `stop serialwrap\` 與 \`sc.exe start`，即 `` `sc.exe stop serialwrap` 與 `sc.exe start serialwrap` ``）。`query` 以 `sc.exe query` stdout 的 `STATE\s*:\s*(\d+)`：1 → `NotRunning("STOPPED")`、其他數字 → `Running`；rc 1060（服務不存在）→ `NotFound`；其他 → `QueryFailed`。`install_plan` 回：
  1. `["sc.exe","create",name,"binPath=","\"<exec0>\" <args>","start=","auto","obj=","NT AUTHORITY\\LocalService"]`
  2. `["sc.exe","failure",name,"reset=","86400","actions=","restart/60000/restart/60000/restart/60000"]`
  3. `["sc.exe","start",name]`
  `install` 在本 Phase **永不執行**上述指令，回空 `Vec`（setup 把 `install_plan` 放進 `pending_sudo`，見 Task 10）。
- `supported_modes`：組裝時決定（Task 1）。不支援的 mode 呼叫 `action` → 與 on-demand 相同的 `NO_SYSTEMD` 形狀。
- `SupervisionMode::parse`：`on-demand|systemd-user|systemd-system|windows-task|windows-service`，其他 → `Other(s)`；`is_supervised` 對前述 4 個監管模式為 true。

`render_task_xml` 逐字輸出（行尾 `\r\n`，兩空白縮排，最後一行 `</Task>\r\n`；`{…}` 以 XML 跳脫 `& < > " '` 後代入）：

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>serialwrap UART broker daemon (windows-task)</Description>
    <URI>\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_id}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

- [ ] **步驟 1：寫失敗測試**（`crates/sw-pal/tests/service_ctl.rs`）

```rust
use sw_pal::fakes::*;
use sw_pal::service_ctl::*;
use std::time::Duration;

fn sm(s: &str) -> SupervisionMode { SupervisionMode::parse(s) }

#[test]
fn systemd_system_privileged_without_sudo_returns_needs_sudo_and_runs_nothing() {
    let env = FakeSysEnv::builder().build();
    let ctl = SystemdCtl::new(env.clone(), FakeClock::new());
    let r = ctl.action(&sm("systemd-system"), ServiceAction::Restart, false);
    assert!(!r.ok);
    assert_eq!(r.error_code, Some("NEEDS_SUDO"));
    assert_eq!(r.hint.as_deref(), Some("需 root：請執行 `sudo systemctl restart serialwrap`（或加 --with-sudo 讓本指令代跑）"));
    assert!(env.calls().is_empty());
}

#[test]
fn systemd_system_status_never_uses_sudo() {
    let env = FakeSysEnv::builder().command(&["systemctl", "status", "serialwrap"], 3, "inactive", "").build();
    let r = SystemdCtl::new(env.clone(), FakeClock::new()).action(&sm("systemd-system"), ServiceAction::Status, true);
    assert_eq!(env.calls(), vec![vec!["systemctl".to_string(), "status".into(), "serialwrap".into()]]);
    assert_eq!((r.ok, r.rc), (false, Some(3)));
}

#[test]
fn task_xml_is_byte_exact_golden() {
    let xml = render_task_xml(&TaskXmlSpec {
        name: "serialwrap".into(), user_id: "HOST\\alice".into(),
        command: "C:\\Users\\alice\\AppData\\Local\\Programs\\serialwrap\\bin\\serialwrapd.exe".into(),
        arguments: "--socket tcp://127.0.0.1:48700".into(),
        working_dir: "C:\\Users\\alice\\AppData\\Local\\Programs\\serialwrap\\bin".into() });
    assert_eq!(xml, include_str!("golden/serialwrap.task.xml"));
    assert!(xml.contains("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"));
    let bytes = encode_utf16le_bom(&xml);
    assert_eq!(&bytes[..2], &[0xFF, 0xFE]);
}

#[test]
fn windows_task_install_writes_xml_then_creates_task() {
    let tmp = tempfile::tempdir().unwrap();
    let env = FakeSysEnv::builder().user_identity("HOST\\alice").build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    let def_path = tmp.path().join("serialwrap.task.xml");
    let recs = ctl.install(&SupervisorDefinition { mode: sm("windows-task"), name: "serialwrap".into(),
        exec: vec!["C:\\p\\bin\\serialwrapd.exe".into(), "--socket".into(), "tcp://127.0.0.1:48700".into()],
        working_dir: Some("C:\\p\\bin".into()), run_user: None, env: vec![], definition_path: Some(def_path.clone()) }, false);
    assert_eq!(recs.iter().map(|r| r.argv.clone()).collect::<Vec<_>>(),
               vec![vec!["schtasks".to_string(), "/Create".into(), "/XML".into(), def_path.display().to_string(), "/TN".into(), "serialwrap".into(), "/F".into()]]);
    let raw = std::fs::read(&def_path).unwrap();
    assert_eq!(&raw[..2], &[0xFF, 0xFE]);
}

#[test]
fn windows_task_stop_and_restart_are_not_composed_in_pal() {
    let env = FakeSysEnv::builder().build();
    let ctl = WindowsTaskCtl::new(env.clone(), real_fs(), FakeClock::new());
    for a in [ServiceAction::Stop, ServiceAction::Restart] {
        let r = ctl.action(&sm("windows-task"), a, false);
        assert_eq!((r.ok, r.error_code), (false, Some("INTERNAL_ERROR")));
    }
    assert!(env.calls().is_empty(), "PAL 不得自行 /End 或 /Run");
}

const PS_QUERY: &str = "try { (Get-ScheduledTask -TaskName 'serialwrap' -ErrorAction Stop).State.ToString() } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { 'NotFound' } else { exit 3 } }";

#[test]
fn windows_task_query_failure_is_never_stopped() {
    for (rc, out, err) in [(3, "", "Access is denied."), (0, "", ""), (0, "Garbage\r\n", "")] {
        let env = FakeSysEnv::builder().command(&["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_QUERY], rc, out, err).build();
        let q = WindowsTaskCtl::new(env, real_fs(), FakeClock::new()).query(&sm("windows-task"));
        assert!(matches!(q, TaskQuery::QueryFailed(_)), "rc={rc} out={out:?} → {q:?}");
    }
}
```

表列案例（同檔）：

| 測試名 | 斷言 |
|---|---|
| `systemd_user_start_runs_user_scope` | calls == `[["systemctl","--user","start","serialwrap"]]`；`ok == (rc==0)` |
| `systemd_system_start_with_sudo_prefixes_sudo` | calls == `[["sudo","systemctl","start","serialwrap"]]` |
| `on_demand_returns_no_systemd_shape` | `error_code==Some("NO_SYSTEMD")`、hint 逐字、`rc==None`、calls 空 |
| `unknown_mode_foo_returns_no_systemd` | `mode=="foo"`、`NO_SYSTEMD` |
| `windows_task_start_status_hard_stop_argv` | `Start`、`Status`、`hard_stop` 的 argv 逐字如「行為」 |
| `windows_task_set_enabled_false` | `["schtasks","/Change","/TN","serialwrap","/DISABLE"]` |
| `windows_task_query_ready_is_not_running` | powershell 回 `Ready\r\n` → `NotRunning("Ready")`；`Queued\r\n` → `Running` |
| `windows_task_query_object_not_found` | powershell 回 `NotFound\r\n`、rc 0 → `NotFound` |
| `systemd_query_maps_is_active` | `active`→Running、`inactive`／`failed`→NotRunning、rc 4→NotFound、執行檔不存在→QueryFailed |
| `windows_service_start_without_sudo_needs_sudo` | `NEEDS_SUDO`、hint 逐字、calls 空 |
| `windows_service_install_never_executes` | `install(..)` 回空、calls 空；`install_plan` 三條 argv 逐字 |
| `windows_service_query_parses_state_code` | `sc.exe query` stdout 含 `        STATE              : 1  STOPPED` → `NotRunning`；`: 4  RUNNING` → `Running`；rc 1060 → `NotFound`；rc 5 → `QueryFailed` |
| `shell_join_split_windows_roundtrip_golden` | `contracts/golden/windows_cmdline.ndjson`（由 Windows Python `subprocess.list2cmdline` 產生 join 端、Windows 原生 `CommandLineToArgvW` 於 golden 產生腳本中計算 split 端）逐筆相同；含空白、`"`、尾端 `\`、空字串參數 |
| `task_xml_escapes_ampersand_in_path` | command `C:\\A&B\\serialwrapd.exe` → `<Command>C:\A&amp;B\serialwrapd.exe</Command>` |
| `supervision_mode_parse_roundtrip` | 5 個合法值 `as_str()` 往返；`"Systemd-User"` → `Other` |
| `cap_supervisor_runtime__windows_task_roundtrip`（`#[ignore = "cap:supervisor_runtime"]`，原生 Windows） | 以 name=`serialwrap-captest-<pid>`、exec=`cmd.exe /c exit 0` 註冊→`/Run`→輪詢 `query` 直到 `NotRunning`（≤10 s）→`uninstall`→`query`==`NotFound`；查回 XML 含 `PT0S` |

`crates/sw-pal/tests/golden/serialwrap.task.xml`：以上方模板代入 `task_xml_is_byte_exact_golden` 的值、CRLF 行尾的檔案（`.gitattributes` 對此檔設 `-text`）。

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-pal --features test-fakes --test service_ctl` → `not implemented: P4-T3`。
- [ ] **步驟 3：實作**

```rust
impl ServiceCtl for SystemdCtl {
    fn action_named(&self, mode, name, action, with_sudo) -> ServiceResult {
        match mode {
            SupervisionMode::SystemdUser => self.run_result(mode, action, vec!["systemctl","--user",action.as_str(),name]),
            SupervisionMode::SystemdSystem => {
                let privileged = !matches!(action, ServiceAction::Status);
                if privileged && !with_sudo { return needs_sudo_systemd(mode, action, name); }
                let mut argv = vec!["systemctl", action.as_str(), name];
                if privileged { argv.insert(0, "sudo"); }
                self.run_result(mode, action, argv)
            }
            _ => no_systemd(mode, action),
        }
    }
    fn query(&self, mode) -> TaskQuery { /* systemctl [--user] is-active serialwrap，對應見「行為」 */ }
}
// CompositeServiceCtl::action_named：mode 不在 self.modes → no_systemd；否則依 mode 轉給 systemd/task/service。
```

`shell_join_windows`／`split_windows_cmdline`：純函式自寫（`list2cmdline` 規則與 MSVCRT／`CommandLineToArgvW` 解析規則：含空白、Tab 或 `"` 的參數以 `"` 包住，`"` 前的 `\` 加倍、結尾 `\` 加倍），放無 `cfg` 的共用檔；**不**呼叫 SHELL32 的 `CommandLineToArgvW`（arch §1.1 DLL 白名單），原生 API 只出現在 golden 產生腳本。原生 `posix.rs`／`windows.rs` 只負責 `native()` 組裝；所有 argv 產生在無 `cfg` 的共用檔。

- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-pal --features test-fakes --test service_ctl
scripts/rust/win-test.sh --package sw-pal -- service_ctl
python3 scripts/rust/check_cfg_boundary.py
```

- [ ] **步驟 5：commit**：`feat(sw-pal): 實作 systemd 與 windows-task／windows-service 監管控制`

**BLOCKED-OWNER：**
- Spike 2：在 OD-05 代表機台以正式 artifact 註冊 `windows-task`，驗證登入觸發、唯一實例（`IgnoreNew`）、失敗自動重啟、`PT0S` 與實際連續運行 >72 小時（需互動登入／登出與重開機）；可執行程序、腳本與證據 schema 由 Task 18 交付。
- OD-08：本 Task 動到 PAL，PR 必附原生 Windows COM handle 證據（本環境無法產出，PR 標示證據待補）。
- `cap_supervisor_runtime__windows_task_roundtrip` 在擁有者 Windows 主機執行（會建立並刪除排程工作，需擁有者同意）。
- `windows-service` 的帳號、Session 0、ACL 資格（OD-12；僅在擁有者核准採用時）。

---

## Task 4：PAL `ProcessCtl`（spawn／poll／foreground）與 `ProcScan`（POSIX `/proc`、Windows Toolhelp32）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-process-scan.md`（`type: feat`，`scope: sw-pal`）

**Files:**
- Modify: `crates/sw-pal/src/process.rs`、`crates/sw-pal/src/process/{posix.rs,windows.rs}`
- Modify: `crates/sw-pal/src/proc_scan/{mod.rs,posix.rs,windows.rs}`
- Create: `crates/sw-pal/tests/proc_scan.rs`、`crates/sw-pal/tests/process_ctl.rs`

**Interfaces:**
- Consumes：無新依賴。
- Produces：arch §4.9 `ProcessCtl::{spawn_daemon, poll, call_foreground}`（`exec_or_wait` 屬 P3）、`ProcScan::serialwrapd_instances`、`dedup_launcher_children`、`is_serialwrapd_argv`、`extract_socket_arg`、`PosixProcScan::with_root`。另加 `ProcScan::pid_alive(pid)`（POSIX：讀 `/proc/<pid>/stat` 第 3 欄狀態，檔案不存在或狀態為 `Z`／`X` → false，否則 true——避免 Docker 等 PID 1 不回收環境的 zombie 被當成存活；Windows：`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`＋`GetExitCodeProcess==STILL_ACTIVE`；**不得**送任何訊號）與 `ProcScan::identity_alive(&ProcIdentity)`（pid 存活且目前 start_ticks（POSIX `/proc/<pid>/stat` 第 22 欄、Windows CreationTime）與記錄值相等）。

行為：
- `spawn_daemon(argv, env, detached=true)`：POSIX `setsid`、stdout／stderr → `/dev/null`、**stdin 繼承**（CLI-12.10 KEEP）；Windows `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`，stdout／stderr → `NUL`，stdin 繼承。env 以給定清單**取代**。
- `poll`：`waitpid(WNOHANG)`／`GetExitCodeProcess`。`call_foreground`：等同 `subprocess.call`，回子行程 rc（POSIX 被訊號終止 → `-signo`，與 Python 相同）。
- `serialwrapd_instances`：
  - POSIX：`listdir(proc_root)` 數字項（失敗 → `Err(ScanFailure)`），讀 `cmdline`，`\0` 切、去空、`decode_utf8_replace`；`is_serialwrapd_argv(parts, ArgvFlavor::Posix)` 三規則（basename(argv0)==`serialwrapd`；任一 part 以 `serialwrapd.py` 結尾；argv[1:] 中含 `/` 且 basename==`serialwrapd`）＋**第 4 規則**：存在連續兩個 token `-m`、`sw_core.daemon`（DF-05 第 3 層 fallback 啟動的 daemon，否則 `find_conflicting_daemon`／doctor `single_daemon`／預檢都看不到它；P1 同步請求見 §0.5）；`extract_socket_arg`（`--socket X` 或 `--socket=X`）；依 pid 升冪；`tree` 只含自身 `ProcIdentity{pid, start_ticks}`。
  - Windows（SC-17）：`CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS)` 列出全部行程；對 image 名（不分大小寫）為 `serialwrapd.exe` 或 `python*.exe` 的行程以 `NtQueryInformationProcess(ProcessCommandLineInformation=60)` 取命令列、以自寫 `split_windows_cmdline`（Task 1，不用 SHELL32）切開；`creation_time` 取 `GetProcessTimes`。`is_serialwrapd_argv(parts, ArgvFlavor::Windows)`：路徑分隔同時接受 `\` 與 `/`，basename 比較**不分大小寫**且先去掉結尾 `.exe`（`C:\p\bin\SerialwrapD.EXE --socket …` 命中規則 1；`serialwrapd.py` 後綴比對亦不分大小寫），第 4 規則同 POSIX。最後 `dedup_launcher_children`：若某符合者的 `parent_pid` 指向另一個 image 為 `serialwrapd.exe` 且同樣符合的行程，且子行程 `creation_time >= 父 creation_time`，則捨棄子行程、保留 launcher（socket 取 launcher 的 `--socket`，launcher 沒有時取子行程的），並把子行程的 `ProcIdentity` 併入 launcher 的 `tree`。命令列讀取失敗時：image 為 `serialwrapd.exe` 者仍列入（`socket: None`，保守視為可能衝突）；`python*.exe` 者略過。
  - `serialwrapd_tty_holders`：POSIX 照 07 §3.2；Windows 回 `(空, Unsupported)`（Slice 2.1 前不擴充）。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-pal/tests/proc_scan.rs`）

```rust
use sw_pal::proc_scan::*;
fn w(pid: u32, ppid: u32, image: &str, argv: &[&str], t: u64) -> WinProcInfo {
    WinProcInfo { pid, parent_pid: ppid, image: image.into(), argv: argv.iter().map(|s| s.to_string()).collect(), creation_time: t }
}
#[test]
fn windows_launcher_and_python_child_count_as_one_daemon() {
    let procs = vec![
        w(100, 4, "serialwrapd.exe", &["C:\\p\\bin\\serialwrapd.exe", "--socket", "tcp://127.0.0.1:48700"], 10),
        w(101, 100, "python.exe", &["C:\\p\\versions\\0.4.0\\py\\python.exe", "-I", "C:\\p\\versions\\0.4.0\\py\\serialwrapd.py", "--socket", "tcp://127.0.0.1:48700"], 11),
        w(200, 4, "python.exe", &["python.exe", "-c", "print(1)"], 12),
    ];
    let d = dedup_launcher_children(&procs);
    assert_eq!(d.len(), 1);
    assert_eq!((d[0].pid, d[0].socket.as_deref()), (100, Some("tcp://127.0.0.1:48700")));
}
#[test]
fn reused_parent_pid_does_not_swallow_child() {
    // 子行程比「同 pid 的父」更早建立 → 父 pid 已被重用，不得去重
    let procs = vec![
        w(100, 4, "serialwrapd.exe", &["serialwrapd.exe", "--socket", "tcp://127.0.0.1:48701"], 50),
        w(101, 100, "python.exe", &["python.exe", "-I", "serialwrapd.py"], 20),
    ];
    assert_eq!(dedup_launcher_children(&procs).len(), 2);
}
#[test]
fn posix_scan_reads_fake_proc_root_sorted_by_pid() {
    let tmp = tempfile::tempdir().unwrap();
    for (pid, cmd) in [("300", "/usr/bin/python3\0/opt/sw/serialwrapd.py\0--socket=/run/a.sock\0"),
                       ("20", "serialwrapd\0--socket\0/run/b.sock\0"),
                       ("21", "bash\0-c\0echo\0"), ("self", "x\0")] {
        std::fs::create_dir_all(tmp.path().join(pid)).unwrap();
        std::fs::write(tmp.path().join(pid).join("cmdline"), cmd).unwrap();
    }
    let got = PosixProcScan::with_root(tmp.path()).serialwrapd_instances().unwrap();
    assert_eq!(got.iter().map(|d| (d.pid, d.socket.clone())).collect::<Vec<_>>(),
               vec![(20, Some("/run/b.sock".into())), (300, Some("/run/a.sock".into()))]);
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `is_serialwrapd_rule1_basename` | `["/x/serialwrapd"]` → true；`["/x/serialwrapd2"]` → false |
| `is_serialwrapd_rule2_py_suffix_anywhere` | `["python3","a/serialwrapd.py"]` → true |
| `is_serialwrapd_rule3_requires_slash` | `["env","serialwrapd"]` → false；`["env","/opt/serialwrapd"]` → true |
| `is_serialwrapd_rule4_module_form` | `["/v/py/bin/python3","-I","-m","sw_core.daemon","--socket","/s"]` → true；`["python3","-m","sw_core.cli"]` → false；`["python3","-c","-m sw_core.daemon"]` → false（非連續 token） |
| `is_serialwrapd_windows_exe_case_insensitive` | Windows flavor：`["C:\\p\\bin\\serialwrapd.exe","--socket","tcp://127.0.0.1:48700"]` → true；`["C:\\P\\BIN\\SerialwrapD.EXE"]` → true；`["C:\\x\\serialwrapd2.exe"]` → false；`["python.exe","-c","print(1)"]` → false；POSIX flavor 對 `serialwrapd.exe` → false（POSIX 規則不變） |
| `windows_launcher_only_counts_one` | 只有 launcher（無 Python 子行程）→ 1 筆、socket 取自 launcher |
| `windows_unreadable_launcher_cmdline_kept_as_unknown_socket` | `serialwrapd.exe` 命令列讀取失敗 → 1 筆、`socket: None` |
| `extract_socket_equals_form_and_trailing_flag` | `["serialwrapd","--socket"]` → None；`["serialwrapd","--socket=x"]` → `Some("x")` |
| `posix_scan_missing_root_is_scan_failure` | root 不存在 → `Err(ScanFailure)` |
| `pid_alive_self_true_and_reaped_false`（POSIX 真實） | 自身 pid true；spawn `true` 並 wait 後 false |
| `pid_alive_false_for_zombie` | fake proc root 中 `<pid>/stat` 狀態欄為 `Z` → false；`X` → false；`S` → true |
| `identity_alive_detects_pid_reuse` | `ProcIdentity{pid:self, start_ticks:Some(1)}` → false |
| `spawn_daemon_detached_new_session`（POSIX 真實） | 子行程 `getsid(pid)==pid`；stdout 寫入不出現在測試輸出 |
| `call_foreground_returns_exit_code` | `["sh","-c","exit 3"]` → 3 |
| `windows_scan_finds_self_spawned_fake_daemon`（原生 Windows，`win-test.sh`） | spawn 一個複製成 `serialwrapd.exe` 名稱的測試 helper（`--socket tcp://127.0.0.1:1`）→ 掃描結果含其 pid 與 socket；helper 結束後消失 |
| `windows_pid_alive_never_terminates_target`（原生 Windows） | 對執行中 helper 呼叫 `pid_alive` 100 次後 helper 仍存活 |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-pal --features test-fakes --test proc_scan --test process_ctl` → `not implemented: P4-T4`。
- [ ] **步驟 3：實作**：依「行為」。Windows 命令列讀取用 `windows_sys::Wdk::System::Threading::NtQueryInformationProcess`（feature `Wdk_System_Threading` 已在 arch 清單），緩衝區以回傳長度重配一次；失敗略過該行程不中止掃描。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-pal --features test-fakes --test proc_scan --test process_ctl
scripts/rust/win-test.sh --package sw-pal -- proc_scan process_ctl
```

- [ ] **步驟 5：commit**：`feat(sw-pal): 實作 daemon spawn 與 serialwrapd 行程掃描（含 Windows launcher 去重）`

**BLOCKED-OWNER：**
- 以正式 Windows artifact（原生 build 的 launcher＋bundled Python）在 OD-05 機台實測 Stage 1 daemon 只被計為 1 個實例；受 EDR 限制時 `NtQueryInformationProcess` 是否可讀命令列需實機確認。
- OD-08：本 Task 動到 PAL，PR 必附原生 Windows COM handle 證據（本環境無法產出，PR 標示證據待補）。

---

## Task 5：PAL `ProcessTunnel`（BC-05）與 `FsOps` 跨行程鎖／連結

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-process-tunnel.md`（`type: feat`，`scope: sw-pal`）

**Files:**
- Modify: `crates/sw-pal/src/process_tunnel/{mod.rs,posix.rs,windows.rs}`、`crates/sw-pal/src/fs_ops.rs`、`crates/sw-pal/src/fs_ops/{posix.rs,windows.rs}`
- Create: `crates/sw-pal/tests/process_tunnel.rs`、`crates/sw-pal/tests/fs_lock.rs`

**Interfaces:**
- Consumes：`ProcScan::pid_alive`（Task 4）。
- Produces：arch §4.9 `ProcessTunnel` 全部；`FsOps` 寫入面全部（`atomic_write`、`write_file`、`mkdirs`、`replace`、`remove_file`、`copy_preserving`、`copy_tree`、`remove_tree`、`read_link`、`set_mode`、`sync_dir`、`file_lock_exclusive`、`symlink_or_copy_dir`、`symlink_file`；Task 1 Produces 簽章）。

行為：
- `spawn_detached(argv, log)`：建立 log 父目錄；log 以 append 開啟；stdin=null、stdout／stderr=log；POSIX `setsid`、`close_fds`，回 `TunnelProc{pid, group:Pgid(getpgid(pid)), start_ticks: /proc/<pid>/stat 第 22 欄}`；Windows `CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW`、`bInheritHandles` 只繼承 log handle（`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`），`group: WinTree`，`start_ticks`＝CreationTime FILETIME。不使用任何 pipe。argv[0] 為裸名時依 PATH 解析（等同 Popen）。
- `alive(p)`：POSIX `kill(pid,0)` 成功且（`start_ticks` 為 None 或目前 ticks 為 None 或相等）；Windows `OpenProcess`＋`GetExitCodeProcess==STILL_ACTIVE`＋CreationTime 相等。
- `terminate(p, grace=5s)`：POSIX 照 07 §8.4 `_terminate_pgid`（SIGTERM → 每 0.1 s reap／`killpg(pgid,0)` → 逾時 SIGKILL＋reap；吞 ESRCH／EPERM）。Windows：**先核對根身分**——`OpenProcess(p.pid)` 取 CreationTime，與 `p.start_ticks` 不相等（或 `start_ticks` 為 None 而無法核對）→ 視為原行程已結束、不送任何終止（回 `Ok`，與 POSIX 吞 ESRCH 對稱），防止 PID 重用誤殺；相等才以 Toolhelp32 快照一次，收集以 `p.pid` 為根、CreationTime 不早於父者的整棵子樹，由葉到根 `TerminateProcess(1)`，每 0.1 s 檢查至 `grace` 用盡。不送 `GenerateConsoleCtrlEvent`：子行程以 `CREATE_NO_WINDOW` 建立在獨立 console，CLI 不與其共用 console，`CTRL_BREAK_EVENT` 送不到，只會白等 grace。ssh 的 `-R`／`-L` forward 於 TCP 連線中斷時由 relay 端釋放（BLOCKED-OWNER 實測）。
- `stderr_tail(log, max=500)`：每次重開 log 讀末 `max` bytes，`decode_utf8_replace`。
- `supports_control_master`：POSIX true；Windows false。
- `file_lock_exclusive(p, blocking)`：POSIX `open(O_RDWR|O_CREAT,0600)`＋`flock(LOCK_EX[|LOCK_NB])`；Windows `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK[|FAIL_IMMEDIATELY], 0, 1, 0)`；guard drop 時解鎖並關閉。非阻塞取不到 → `Err(kind=WouldBlock)`。
- `symlink_or_copy_dir(link, target)`（DF-16）：先移除既有 link（symlink → unlink；真目錄 → 遞迴刪；其他 → unlink），再 `symlink`；Windows `CreateSymbolicLinkW(..., SYMBOLIC_LINK_FLAG_DIRECTORY|ALLOW_UNPRIVILEGED_CREATE)` 失敗（`ERROR_PRIVILEGE_NOT_HELD`）→ 遞迴複製並回 `Copied`。
- `symlink_file(link, target)`：POSIX `symlink`；Windows → `Err(Unsupported)`。
- `copy_tree(from, to)`：遞迴複製（檔案 `copy_preserving`、保留相對結構；`to` 已存在 → `Err(AlreadyExists 等價的 InvalidInput)`）；`remove_tree(p)`：遞迴刪除，不跟隨 symlink（symlink 本身 unlink）；Windows 對唯讀屬性先清除再刪。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-pal/tests/process_tunnel.rs`）

```rust
use std::time::{Duration, Instant};
#[test]
fn posix_terminate_kills_whole_group_including_grandchild() {
    let tmp = tempfile::tempdir().unwrap();
    let t = sw_pal::native().tunnel;
    let p = t.spawn_detached(&["sh".into(), "-c".into(), "sleep 300 & sleep 300; wait".into()], &tmp.path().join("t.log")).unwrap();
    std::thread::sleep(Duration::from_millis(200));
    let start = Instant::now();
    t.terminate(&p, Duration::from_secs(5)).unwrap();
    assert!(start.elapsed() < Duration::from_secs(6));
    assert!(!t.alive(&p));
    let sw_pal::process_tunnel::ProcGroupRef::Pgid(g) = p.group else { panic!("posix 必為 Pgid") };
    assert_eq!(unsafe { libc::killpg(g, 0) }, -1, "群組內不得殘留行程");
}
#[test]
fn stderr_tail_returns_last_500_bytes_decoded() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("x.log");
    std::fs::write(&log, [b"a".repeat(600), "錯誤".as_bytes().to_vec()].concat()).unwrap();
    let tail = sw_pal::native().tunnel.stderr_tail(&log, 500);
    assert!(tail.ends_with("錯誤") && tail.len() <= 500);
}
```

（上例中 `libc` 僅 sw-pal 測試使用，`sw-pal` 已是唯一允許 `libc` 的 crate；此測試檔加 `#![cfg(unix)]`，屬 sw-pal 內允許的平台 cfg。）

表列案例：

| 測試名 | 斷言 |
|---|---|
| `spawn_detached_no_pipes_stdout_to_log` | 子行程 `echo hi` 後 log 內容 `hi\n`；子行程 fd 0 指向 `/dev/null`（POSIX 讀 `/proc/<pid>/fd/0`） |
| `alive_false_after_pid_reuse_ticks_mismatch` | 以假 `start_ticks=1` 建 `TunnelProc{pid:self}` → false |
| `supports_control_master_by_platform` | POSIX true；原生 Windows false |
| `windows_terminate_tree_kills_child_and_grandchild`（原生 Windows） | `cmd /c "start /b ping -n 300 127.0.0.1 & ping -n 300 127.0.0.1"` → terminate 後 Toolhelp32 查無任何子孫 |
| `windows_alive_never_terminates`（原生 Windows） | 對存活子行程呼叫 `alive` 50 次後仍存活 |
| `file_lock_nonblocking_contention_wouldblock`（`fs_lock.rs`） | 同 path 第二個非阻塞 lock → `Err(WouldBlock)`；第一個 drop 後成功 |
| `file_lock_cross_process`（POSIX 與原生 Windows） | 子行程持鎖 1 s，父行程非阻塞 → WouldBlock；阻塞版在子行程釋放後 1.5 s 內取得 |
| `symlink_or_copy_dir_replaces_real_dir` | link 原為含檔真目錄 → 回 `Symlink`，舊內容消失 |
| `symlink_or_copy_dir_windows_fallback_copies`（原生 Windows、非開發者模式） | 回 `Copied`，目標檔案皆在 |
| `windows_terminate_root_pid_reused_not_signalled`（原生 Windows） | 以真實 helper 的 pid 搭配錯誤 `start_ticks` 建 `TunnelProc` → `terminate` 回 Ok、helper 仍存活 |
| `windows_terminate_without_ctrl_break_finishes_fast`（原生 Windows） | 對存活 `ping -n 300` 呼叫 `terminate(grace=5s)` → 1 s 內結束 |
| `copy_tree_and_remove_tree` | 巢狀目錄複製後內容與 mode 相同；`remove_tree` 不跟隨指向外部的 symlink（外部檔仍在） |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-pal --features test-fakes --test process_tunnel --test fs_lock` → `not implemented: P4-T5`。
- [ ] **步驟 3：實作**：依「行為」。Windows 樹收集：快照一次，建 `parent→children` 表，BFS 時檢查 `child.creation_time >= parent.creation_time` 防 pid 重用。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-pal --features test-fakes --test process_tunnel --test fs_lock
scripts/rust/win-test.sh --package sw-pal -- process_tunnel fs_lock
```

- [ ] **步驟 5：commit**：`feat(sw-pal): 實作無 pipe 的 ProcessTunnel 與跨行程檔案鎖`

**BLOCKED-OWNER：**
- Win32-OpenSSH `ssh.exe` 在擁有者 Windows 主機被 `TerminateProcess` 後 relay 端 `-R`／`-L` forward 的實際釋放時間（需真實 relay）。
- OD-08：本 Task 動到 PAL，PR 必附原生 Windows COM handle 證據（本環境無法產出，PR 標示證據待補）。

---

## Task 6：`config.yaml` 寫入、`service`、`supervision-mode` 查詢（DF-07）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/<issue>-rust-p4-service-cmd.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-proto/src/config_types.rs`（`RuntimeConfig::{set_mode, set_socket, to_yaml}`）、`crates/sw-cli/src/endpoint_resolve.rs`（`config_write_path`、`save_runtime_config`）
- Modify: `crates/sw-cli/src/commands/{service_cmd.rs,supervision_cmd.rs}`、`crates/sw-cli/src/delegate.rs`（移除 `service`、`supervision-mode`）
- Create: `crates/sw-cli/tests/mgmt_service.rs`、`crates/sw-proto/tests/runtime_config_write.rs`、`tests/contract_blackbox/test_cli_mgmt_service.py`

**Interfaces:**
- Consumes：`ServiceCtl::action`（Task 3）、P3 `RuntimeConfig::from_text`／`endpoint_resolve::load_runtime_config`、`yaml11::emit::safe_dump`（Task 17）。
- Produces：

```rust
impl RuntimeConfig {
    pub fn set_mode(&mut self, mode: &str, socket_path: Option<&str>);   // 07 §5.1：socket_path Some 才覆寫
    pub fn set_socket(&mut self, endpoint: &str);
    pub fn to_yaml(&self) -> String;                                     // safe_dump(allow_unicode=true, sort_keys=true)
}
pub fn sw_cli::endpoint_resolve::config_write_path(ctx: &Ctx) -> PathBuf;   // SC-09 寫入目標：SERIALWRAP_CONFIG（strip 非空）> CONFIG_DIR/config.yaml；永不為系統唯讀 fallback
pub fn sw_cli::endpoint_resolve::save_runtime_config(ctx: &Ctx, cfg: &RuntimeConfig) -> Result<(), ConfigError>;  // mkdir parents → to_yaml → 直接覆寫（非原子，KEEP）
pub fn sw_cli::commands::service_cmd::run_service(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub fn sw_cli::commands::supervision_cmd::run_supervision_mode(ns: &Namespace, ctx: &mut Ctx) -> i32;  // 無 words：查詢；words[0]=="set" 交給 Task 10 的 run_supervision_set
pub fn sw_cli::commands::supervision_cmd::load_config_or_report(ctx: &mut Ctx, kind: ConfigErrorReport) -> Result<RuntimeConfig, i32>; // DF-07
pub enum ConfigErrorReport { PlainStderr /* supervision-mode */, JsonInternalError /* service/setup/daemon */ }
```

行為：
- `service <action> [--with-sudo]`：`mode = config.mode() or "on-demand"`；`ServiceCtl::action` → 轉 PyDict（`ok, mode, action` 必有；`rc, stdout, stderr` 或 `error_code, hint`）→ `print_json`；`mirror_err(context="service <action>")`；exit `0 if ok else 2`。
- `supervision-mode`（無 words）：stdout `"{mode or 'on-demand'}\n"`（純文字，不驗證值），exit 0。
- DF-07 壞 YAML：`supervision-mode` → stderr 一行 `serialwrap: config.yaml 解析失敗: <path>: <yaml 錯誤摘要>`、exit 1、stdout 空；`service` → stdout `{"error_code":"INTERNAL_ERROR","message":"config.yaml 解析失敗: <path>: <摘要>","ok":false}`、stderr `serialwrap: service <action> failed: INTERNAL_ERROR: config.yaml 解析失敗: …`、exit 1。P1 DF-07 只凍結「stderr 恰一行」與 JSON 形狀，未凍結摘要文字；本計畫定為 `<摘要>`＝`YamlError` 的 PyYAML 等價 `problem` 加 ` (line <l>, column <c>)`（1-based），換行一律以空白取代以維持單行。A/B 比較器只比 code、exit、stderr 行數與前綴 `config.yaml 解析失敗: <path>`（摘要屬非契約部分，G1），此定義登記於 `contracts/behavior_changes.yaml` DF-07 附註。
- `service stop|restart` 在 `windows-task`／`windows-service` 模式由 **Task 7** 接上 `graceful_stop`（本 Task 先讓這兩個組合回 PAL 的 `INTERNAL_ERROR`，測試在 Task 7）：輸出 `{ok, mode, action, rc, stdout, stderr, graceful:<是否未用 hard_stop>}`；逾時 `{ok:false, mode, action, error_code:"TIMEOUT", phase:"service_stop_wait_stop"|"service_restart_wait_stop", hint}`、exit 2。systemd 模式不變（雙邊）。
- 寫入位置（SC-09）：讀取可能來自系統唯讀 fallback（POSIX `/etc/serialwrap/config.yaml`、Windows `%ProgramData%\serialwrap\config.yaml`），但 `save_runtime_config` 一律寫 `config_write_path`；寫入前的「舊內容」以 `config_write_path` 現有檔為準（不存在則以讀到的設定為起點，與 Python `_save_runtime_config` 相同）。
- `to_yaml` 輸出與 PyYAML `safe_dump(allow_unicode=True)` 位元組相同（golden 由 Task 17 的 `scripts/rust/goldens/gen_yaml_golden.py` 產生；本 Task 在 Task 17 語料之外新增 6 個 config 形狀案例：空 dict、只有 mode、mode＋tcp socket、含 `windows: {exclude_coms: [COM3, COM10]}`、含中文值、含需引號的字串 `"on"`）。

- [ ] **步驟 1：寫失敗測試**

`crates/sw-cli/tests/mgmt_service.rs`：

```rust
mod support; use support::*;
#[test]
fn service_on_demand_prints_no_systemd_and_mirrors_stderr() {
    let t = TestEnv::posix();                         // tmp HOME + 隔離 RuntimePaths + FakeSysEnv
    let (rc, out, err) = t.run(&["service", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(out, "{\"action\":\"start\",\"error_code\":\"NO_SYSTEMD\",\"hint\":\"目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。\",\"mode\":\"on-demand\",\"ok\":false}\n");
    assert_eq!(err, "serialwrap: service start failed: NO_SYSTEMD（hint: 目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。）\n");
}
#[test]
fn supervision_mode_prints_plain_text_unvalidated() {
    let t = TestEnv::posix().with_config("supervision_mode: foo\n");
    assert_eq!(t.run(&["supervision-mode"]), (0, "foo\n".to_string(), String::new()));
}
#[test]
fn supervision_mode_bad_yaml_is_one_stderr_line_exit1() {
    let t = TestEnv::posix().with_config("supervision_mode: [unterminated\n");
    let (rc, out, err) = t.run(&["supervision-mode"]);
    assert_eq!((rc, out.as_str()), (1, ""));
    assert!(err.starts_with(&format!("serialwrap: config.yaml 解析失敗: {}", t.config_path().display())));
    assert_eq!(err.matches('\n').count(), 1);
}
```

（`support::TestEnv` 由 Task 1 的 `crates/sw-cli/tests/support/mod.rs` 提供：`posix()`、`windows()`、`with_config(&str)`、`with_fake_*`、`run(&[&str]) -> (i32, String, String)`、`config_path()`。）

表列案例：

| 測試名（Rust／黑箱同名） | 斷言 |
|---|---|
| `service_systemd_user_status_passthrough` | config `systemd-user`；FakeSysEnv `systemctl --user status serialwrap` → rc 3 → stdout 含 `"rc":3`、exit 2、stderr 前綴 `serialwrap: service status failed: ` |
| `service_systemd_system_start_needs_sudo` | exit 2、`error_code=="NEEDS_SUDO"`、無 calls |
| `service_windows_task_start_runs_schtasks`（`TestEnv::windows()`） | calls == `[["schtasks","/Run","/TN","serialwrap"]]`、rc 0 → exit 0 |
| `service_bad_yaml_json_internal_error` | stdout `error_code=="INTERNAL_ERROR"`、exit 1 |
| `service_invalid_action_argparse_exit2` | `service reload` → exit 2、stderr 含 `argument action: invalid choice: 'reload' (choose from 'start', 'stop', 'restart', 'status')` |
| `runtime_config_to_yaml_matches_pyyaml_golden`（sw-proto） | 6 案例逐位元組等於 golden |
| `config_save_never_writes_system_fallback` | 只有 `/etc` 等價的系統 fallback 檔（隔離根下）存在、`supervision-mode set on-demand` 後 → 寫入 `CONFIG_DIR/config.yaml`，系統檔位元組不變；設 `SERIALWRAP_CONFIG=<p>` → 寫 `<p>` |
| `runtime_config_set_mode_keeps_other_keys` | 原有 `windows:` 保留、`socket_path` 在 `None` 時不動 |
| `test_service_ab_on_demand`（黑箱，Python 與 Rust 都跑） | 同上 NO_SYSTEMD 逐位元組；`no_delegation` |

- [ ] **步驟 2：執行並確認 FAIL**

```bash
cargo test -p sw-cli --test mgmt_service ; cargo test -p sw-proto --test runtime_config_write   # 預期 not implemented: P4-T6
SW_BUNDLE=$(scripts/rust/mkbundle.sh --dev --delegate-sentinel --print-dir)
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_service.py  # 預期 rc 97（仍委派）
```

- [ ] **步驟 3：實作**：照「行為」；`delegate.rs` 的 `DELEGATED_GROUPS` 移除 `service`、`supervision-mode`；`ServiceResult → PyDict` 轉換放 `service_cmd.rs::result_to_pydict`（鍵順序不影響 stdout，因 `CLI_STDOUT` sort_keys）。
- [ ] **步驟 4：執行並確認 PASS**：步驟 2 的三組命令全部 PASS；另跑 `python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_service.py`（Python 基線）。
- [ ] **步驟 5：commit**：`feat(rust-cli): 原生承接 service 與 supervision-mode 查詢`

**BLOCKED-OWNER：** 無。

---

## Task 7：`daemon start/stop/status/restart`、`graceful_stop`、多開預檢與 Windows 首次需求啟動（SC-10、SC-17、SC-18、SC-20、DF-05）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-daemon-cmd.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/commands/daemon_cmd.rs`、`crates/sw-cli/src/graceful_stop.rs`、`crates/sw-cli/src/commands/service_cmd.rs`（windows-task／windows-service 的 stop／restart 接 `graceful_stop`）、`crates/sw-cli/src/commands/rpc_cmds.rs`（SC-18 掛點）、`crates/sw-cli/src/client.rs`（新增 `call_with_failure`）、`crates/sw-cli/src/delegate.rs`（移除 `daemon`）
- Create: `crates/sw-cli/tests/mgmt_daemon.rs`、`crates/sw-cli/tests/mgmt_graceful_stop.rs`、`tests/contract_blackbox/test_cli_mgmt_daemon.py`

**Interfaces:**
- Consumes：`ProcessCtl`、`ProcScan`（Task 4）、`ServiceCtl`（Task 3）、`FsOps::file_lock_exclusive`（Task 5）、`load_config_or_report`（Task 6）、`install_layout::locate_manifest`（Task 11）、P3 `endpoint_resolve::*`、`client::call`。
- Produces：

```rust
pub fn run_daemon_start(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub fn run_daemon_stop(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub fn run_daemon_status(ns: &Namespace, ctx: &mut Ctx) -> i32;     // health.status，**不**經 SC-18
pub fn run_daemon_restart(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub enum DaemonLocateError { NotFound }
pub fn locate_daemon_argv(ctx: &Ctx) -> Result<Vec<String>, DaemonLocateError>;                 // DF-05
pub fn normalize_endpoint_for_compare(s: &str, cwd: &Path, flavor: PathFlavor) -> String;       // SC-10
pub fn find_conflicting_daemon<'a>(target: &str, daemons: &'a [DaemonProc], cwd: &Path, flavor: PathFlavor) -> Option<&'a DaemonProc>;
pub fn resolve_daemon_start_env_files(ctx: &Ctx, profile_dir: &str) -> Vec<String>;
pub fn load_daemon_env(ctx: &Ctx, files: &[String]) -> Result<(Vec<(OsString, OsString)>, Vec<String>), EnvFileSourceError>;
pub struct DaemonIdentity { pub pid: Option<u32>, pub tree: Vec<ProcIdentity>, pub known: bool }
pub fn capture_daemon_identity(ctx: &Ctx, endpoint: &str) -> DaemonIdentity;
// health.status(1.0).pid ＋ ProcScan 中 socket 正規化後等於 endpoint 的實例（含 Windows launcher＋子行程 tree）；
// known＝至少取得一個 ProcIdentity（ProcScan 失敗或查無 → false）
pub fn wait_daemon_gone(ctx: &Ctx, endpoint: &str, id: &DaemonIdentity, timeout: Duration) -> bool; // SC-12（Task 10、12 共用）
pub struct StopReport { pub graceful: bool, pub ran: Vec<Vec<String>>, pub rc: Option<i32>, pub stdout: String, pub stderr: String }
pub enum StopFailure { Timeout { phase: &'static str, id: DaemonIdentity }, Service(ServiceResult) }
pub fn graceful_stop(ctx: &mut Ctx, mode: &SupervisionMode, endpoint: &str, with_sudo: bool, keep_disabled: bool,
                     phase: &'static str) -> Result<StopReport, StopFailure>;   // ＝ graceful_stop_with(.., StopRpc::Plain)
pub enum StopRpc { Plain, Handoff { reason: &'static str /* "update"|"rollback"|"revert" */ } }   // R1-F1；Handoff 只由 Task 12 使用（"revert"：步驟 7 回退與 recover_journal 停止目前在跑的 daemon，R2-F6）
pub fn graceful_stop_with(ctx: &mut Ctx, mode: &SupervisionMode, endpoint: &str, with_sudo: bool, keep_disabled: bool,
                          phase: &'static str, rpc: StopRpc) -> Result<StopReport, StopFailure>;
// StopFailure 另加 Handoff(HandoffError)（Task 12 型別）：交接被拒或無法確認，未硬停；StopReport 另加 handoff_token: Option<String>
pub enum FailPhase { Connect, Send, Recv }
pub struct RpcFailure { pub phase: FailPhase, pub pal_kind: Option<PalErrorKind>, pub maybe_sent: bool }
pub fn client::call_with_failure(ctx: &Ctx, ep: &str, method: &str, params: &PyValue, timeout_s: f64, retries: &PyInt) -> (RpcOutcome, Option<RpcFailure>);
// 由 P3 rpc_call_once 的內部分支直接產生型別化 metadata，不得解析 message；maybe_sent＝send_all 已開始呼叫
pub enum AutostartDecision { NotApplicable, Started { pid: u32 }, AlreadyUp, Failed(PyDict) }
pub fn maybe_autostart_on_demand(ctx: &mut Ctx, method: &str, resolved: &ResolvedEndpoint, failure: Option<&RpcFailure>, resp: &PyDict) -> AutostartDecision; // SC-18
```

行為（`daemon start`，逐字照 01 §7 與 07 §5.3，以下列出與 Python 基線的差異與 Rust 細節）：
1. `--endpoint` 檢查、`--endpoint`／`--socket` 不一致檢查：逐字照 01 §7 步驟 2、3（exit 2，stderr context `daemon start`）。
2. 監管 gate：`mode_str(config)` 解析為 `is_supervised()`（含 `windows-task`／`windows-service`，SC-24）或未知字串（KEEP：非 on-demand 一律 route）→ `ServiceCtl::action(Start)` 加 `_routed_to:"service start"`，exit 0／2。壞 YAML → DF-07 JSON（exit 1）。
3. 冪等探測：`resolve_local_endpoint_info`（忽略 `--bench`）→ `health.ping`（0.5 s，帶 trace，`_DAEMON_START_PROBE_TRACE` 等價）ok → `{"already_running":true,"ok":true,"socket":<ep>}` exit 0。
4. spawn 目標 `sock`：`--socket`（含空字串）> `--endpoint` > `local_default_endpoint`；trace source 依序 `--socket|--endpoint|default`。
5. 多開預檢（POSIX 與 Windows 皆做；Windows 為 SC-17 Rust-only）：無 `--force-spawn` 時 `ProcScan::serialwrapd_instances()`（`ScanFailure` → 視為無衝突）；`find_conflicting_daemon` 以 `normalize_endpoint_for_compare` 比較（`unix://` 前綴去除；非 `tcp://` 者 `abspath(cwd)`＋`normpath`（Python `posixpath`／`ntpath` 規則，不解 symlink），`PathFlavor::Windows` 再 `normcase`（小寫、`/`→`\`）；`tcp://` 字串原樣）；`socket=None` 也算衝突。命中 → 01 §7 步驟 7 的 JSON（含 `message`、`hint`、`existing_socket`、`existing_pid`、`target_socket`），exit 2，**不鏡像 stderr**（KEEP）。
6. daemon argv（DF-05，P1 T18 凍結順序）：
   1. manifest（`install_layout::locate_manifest`，與 P1 `find_release_manifest` 同序：`SERIALWRAP_RELEASE_MANIFEST` > `realpath(current_exe)` 上兩層）存在且 `components.daemon.launcher.path`（相對 manifest 目錄）存在 → `[<abs launcher>]`；
   2. 否則 `dirname(realpath(current_exe))/serialwrapd<suffix>` 存在（suffix：`PathFlavor::Windows` 為 `.exe`）→ 該路徑；
   3. 否則 manifest 存在且 `components.daemon.runtime.path` 存在 → `[<abs runtime>, "-I", "-m", "sw_core.daemon"]`（`-I` 為 bundled runtime 隔離 user site，與 P3 delegate entry 相同；Python 以自身 `sys.executable` 執行不需要。Task 4 第 4 規則以連續 `-m sw_core.daemon` 辨識，不受 `-I` 影響）；
   4. 否則 `{"error_code":"DAEMON_BINARY_NOT_FOUND","message":"找不到 serialwrapd（release manifest 的 launcher、CLI 同層 serialwrapd、manifest 的 Python runtime 皆不存在）；請確認發行包完整","ok":false}`、exit 2、stderr 鏡像（context `daemon start`）。P1 未凍結此文字（Python 現行文字仍提及 PATH），A/B 比較 code、exit 與 stderr 前綴，message 屬非契約部分（G1），文字登記於 `behavior_changes.yaml` DF-05 附註。
   完整 cmd：`[*argv, "--profile-dir", pd, "--socket", sock, "--lock", lock]`。
7. env 檔：`SERIALWRAP_DAEMON_ENV_FILE` 有設 → strip 非空 `[v]` 否則 `[]`；未設 → `["~/OPI.env", "<profile_dir>/OPI.env"]`。逐檔 expanduser＋strip、去重、不存在略過。非 win backend：`run_raw(["bash","-lc","set -a && source \"$1\" >/dev/null && env -0","serialwrap",<path>], env=Some(累積 env))`，rc≠0 → `ENV_FILE_SOURCE_FAILED`（message＝stderr 以 UTF-8 replace 解碼或 `failed to source <path>`）；成功則以 `\0` 切出的 `KEY=VALUE` **以原始 bytes 轉 `OsString`**（POSIX `OsStringExt::from_vec`，經 sw-pal 輔助 `os_string_from_bytes`；等價 Python `surrogateescape` 的位元組保真，`cli.py:218-219`）**整個取代** env，非 UTF-8 值不得被改寫。win backend：`_parse_env_file_simple` 規則（`KEY=VALUE`、`export KEY=VALUE`、`#` 註解、空行、去成對引號、不展開），讀檔 OSError → `EnvFileSourceError("無法讀取 env 檔：<py_message>")`；結果 `update`。失敗 JSON 帶 `env_file`、`message`、（非空時）`env_files`，exit 2。
8. `--foreground` → `call_foreground(cmd, env)`，回其 rc，無 JSON。
9. 背景：`spawn_daemon(cmd, env, detached=true)`；輪詢 `attempts = 50 if rpc_backend_is_win else 15`，每次先 `clock.sleep(0.2s)`：`poll` 有值 → `DAEMON_EXITED{pid, returncode}` exit 2；`health.ping`(0.5) ok → `health.status`(1.0) → `{"ok":true,"pid","socket":sock[,"env_files"][,"warnings"]}` exit 0；逾時 → `DAEMON_NOT_READY{pid}` exit 2，不 kill（KEEP）。

`daemon stop`：逐字照 01 §7.6；route 條件改為 `mode.is_supervised() && !explicit_target`。systemd 模式 route 到 `service stop`（雙邊，不變）；`windows-task`／`windows-service`（SC-24，Rust-only）route 到 `graceful_stop(keep_disabled=false, phase:"service_stop_wait_stop")`，輸出同 `service stop`（Task 6）加 `_routed_to:"service stop"`。

`daemon status`：`client::call(resolved, "health.status", {}, timeout 依全域 --timeout 或 5.0, retries 依 --retries)`，與 1A `_run_rpc` 相同輸出與版本警告；**不**觸發 SC-18（查詢狀態不得有啟動 daemon 的副作用）。

`daemon restart`（SC-20，雙邊；輸出形狀逐字照 P1 L2081，不另訂）：
- systemd 模式（user／system）→ 與 `serialwrap service restart` 的輸出與 exit code **原樣相同**（不另加欄位；以 P1 實作 A/B 為準）。
- `windows-task`／`windows-service`（Rust-only）→ `graceful_stop(keep_disabled=true, phase:"service_restart_wait_stop")` → `set_enabled(true)` → `ServiceCtl::action(Start)`；輸出 `{ok, mode, action:"restart", rc, stdout, stderr, graceful}`，逾時 `{"ok":false,"error_code":"TIMEOUT","phase":"service_restart_wait_stop",...}` exit 2，且**不** `/Run`、維持 disabled 由 hint 指示（`hint:"舊 daemon 未在 10 秒內結束；工作已停用以免 RestartOnFailure 拉起，確認後執行 serialwrap service start"`）。
- on-demand：解析本機 endpoint（同 start）；`stopped = ping(0.5) 成功`；成功時 `id = capture_daemon_identity` → RPC `daemon.stop`(2.0) → `wait_daemon_gone(endpoint, &id, 10s)`；逾時 → `{"ok":false,"error_code":"TIMEOUT","phase":"restart_wait_old_exit"}`、**exit 1**；之後（含 `stopped=false`）以同 endpoint 作為 `--socket` 執行 start 流程，輸出 `{"ok":<start.ok>,"restarted":true,"start":<start 回應>,"stopped":<bool>}`（sort_keys），exit 依 start 結果（0／2）。

`wait_daemon_gone`：每 0.2 s：`health.ping`(0.5) 失敗 **且** `id.tree` 中每個 `ProcIdentity` 皆 `!ProcScan::identity_alive`（`tree` 為空時只看 ping——與 P1 SC-12「`old_pid` 取不到為 None → 只看 ping」相同）→ true；以 `Clock` 計時到 `timeout` → false。

`graceful_stop`（§12.1 步驟 3；Rust-only 路徑與 update／rollback 共用）：
1. `id = capture_daemon_identity(endpoint)`；
2. supervised：`set_enabled(false)`（windows-task `/Change /DISABLE`；systemd 不呼叫——systemd 的 `service stop` 維持雙邊行為，只有 update／rollback 呼叫端另行 disable）；
3. `StopRpc::Plain`：RPC `daemon.stop`（2.0 s，觸發 SC-11 有序關機；連不上不算失敗）。`StopRpc::Handoff`（update／rollback，Task 12；R1-F1）：改送 `daemon.handoff_stop`（`request_handoff_stop`），**不送** `daemon.stop`；`Err(HandoffError)` → 立即回 `Err(StopFailure::Handoff)`，不做第 4、5 步（不等待、不 `hard_stop`），`keep_disabled` 由呼叫端還原；`Ok(grant)` → 記 `handoff_token` 後續第 4 步。`daemon stop`／`service stop|restart`／`daemon restart`／reconcile 一律 `Plain`（使用者明確要求停止，SC-11 drain 中止進行中操作屬預期，非交接）；Task 12 的**每一個**停止點——步驟 3 停舊版、步驟 7 回退停新版、`recover_journal` 復原變體停目前在跑的 daemon——一律 `Handoff`，Task 12 不得呼叫 `graceful_stop`（`Plain`）（R2-F6：新版 daemon 自步驟 6 起已對外服務，回退的停止與正向更新同樣可能撞上已受理的傳輸／燒錄）；
4. `wait_daemon_gone(endpoint, &id, 10s)`；成功 → 第 6 步；
5. 逾時 → `hard_stop`（windows-task `/End`、windows-service `sc.exe stop`、systemd `stop`、on-demand 無）後再 `wait_daemon_gone(.., 5s)`；仍未結束 → `Err(Timeout{phase, id})`；
6. `keep_disabled==false` 時 `set_enabled(true)`（`service stop` 只停不改登入自啟設定；`/End`／`daemon.stop` 以 exit 0 結束不觸發 RestartOnFailure）；`graceful = 未執行 hard_stop`。

SC-18（Rust-only，`caps.on_demand_autostart==true` 才啟用）：在 `rpc_cmds` 與 event dispatch 的 RPC 呼叫後，若同時滿足：
- (a) `mode_str(config)` 為 None 或 `on-demand`；
- (b) `resolved.source=="default"`，或 `resolved.source=="config.yaml"` 且 `normalize_endpoint_for_compare(endpoint)==normalize(local_default_endpoint)`；
- (c) `failure.phase==Connect`、`failure.maybe_sent==false`、resp `error_code=="SOCKET_ERROR"` 且 `failure.pal_kind ∈ {ConnectionRefused, NotFound}`（`PermissionDenied`、`TimedOut`、其他 kind 一律不觸發）；
- (d) `method ∉ {"health.ping","health.status","daemon.stop"}`；

則：
1. 取 `FsOps::file_lock_exclusive(<RUN_DIR>/autostart.lock, blocking=true)`（等待上限 15 s，以 Clock 計時；逾時視為失敗 `TIMEOUT`）；
2. 持鎖後先重探 `health.ping`(0.5)：ok → `AlreadyUp`（不 spawn、不印啟動訊息）；
3. 否則以 start 流程（無參數、輸出不印）啟動；回 `DAEMON_EXITED`／`DAEMON_ALREADY_RUNNING_ELSEWHERE`／`DAEMON_NOT_READY` 或 spawn 錯誤時，再探一次 `health.ping`(0.5)，健康即視為 `AlreadyUp`（輸家被實例鎖擋下但 daemon 已由贏家啟動）；
4. 釋放鎖。`Started` → stderr `serialwrap: on-demand：預設 endpoint <ep> 沒有 daemon，已自動啟動（pid=<pid>）\n`；`Started`／`AlreadyUp` → **重送一次**原請求（同 params、同 timeout、retries 0），以第二次結果照常輸出。`Failed` → 原回應照常輸出，另加 stderr `serialwrap: on-demand 自動啟動失敗: <error_code>\n`。其他任何情況不重送。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-cli/tests/mgmt_daemon.rs`）

```rust
mod support; use support::*;
use sw_pal::proc_scan::DaemonProc;

#[test]
fn start_refuses_other_daemon_on_different_socket_without_stderr_mirror() {
    let t = TestEnv::posix().with_procs(vec![DaemonProc { pid: 4242, socket: Some("/run/other.sock".into()), tree: vec![] }]);
    let (rc, out, err) = t.run(&["--socket", "/tmp/x/serialwrapd.sock", "daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "error_code"), "DAEMON_ALREADY_RUNNING_ELSEWHERE");
    assert_eq!(i(&j, "existing_pid"), 4242);
    assert_eq!(s(&j, "target_socket"), "/tmp/x/serialwrapd.sock");
    assert_eq!(s(&j, "message"), "偵測到另一個 serialwrapd（pid=4242）綁在 /run/other.sock，與本次 daemon start 目標 /tmp/x/serialwrapd.sock 不同；為避免同一批裝置被兩個 daemon 同時開啟（two-reader），預設拒絕再 spawn 一個。");
    assert_eq!(s(&j, "hint"), "改用 --socket /run/other.sock 指向現有 daemon；確認後要強制另起才加 --force-spawn");
    assert_eq!(err, "");
    assert!(t.process().spawns().is_empty());
}

#[test]
fn start_treats_normalized_same_socket_as_no_conflict() {
    let t = TestEnv::posix().with_procs(vec![DaemonProc { pid: 7, socket: Some("/tmp/x/serialwrapd.sock".into()), tree: vec![] }])
        .with_manifest_launcher()                    // bundle/bin/serialwrapd 存在
        .with_rpc_after_spawn("/tmp/x/serialwrapd.sock", &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["--socket", "unix:///tmp/x/../x/serialwrapd.sock", "daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(t.process().spawns()[0].0[0], t.bundle_dir().join("bin/serialwrapd").display().to_string());
}

#[test]
fn autostart_only_when_request_never_sent() {
    let t = TestEnv::windows()
        .with_rpc_refused_then_ok("tcp://127.0.0.1:48700", "session.list", "{\"id\":1,\"ok\":true,\"sessions\":[],\"version\":\"0.4.0\"}")
        .with_manifest_launcher();
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 0);
    assert_eq!(out, "{\"id\":1,\"ok\":true,\"sessions\":[],\"version\":\"0.4.0\"}\n");
    assert!(err.starts_with("serialwrap: on-demand：預設 endpoint tcp://127.0.0.1:48700 沒有 daemon，已自動啟動（pid="));
    assert_eq!(t.rpc().requests().iter().filter(|r| r.1 == "session.list").count(), 2);
}

#[test]
fn no_autostart_or_resend_after_send_phase_failure() {
    let t = TestEnv::windows().with_rpc_fail_after_send("tcp://127.0.0.1:48700", "command.submit").with_manifest_launcher();
    let (rc, _, _) = t.run(&["cmd", "submit", "--selector", "COM0", "--cmd", "reboot"]);
    assert_eq!(rc, 2);
    assert!(t.process().spawns().is_empty());
    assert_eq!(t.rpc().requests().iter().filter(|r| r.1 == "command.submit").count(), 1);
}

#[test]
fn restart_on_demand_output_matches_p1_shape() {
    let t = TestEnv::posix().with_daemon_alive_then_gone(&TestEnv::default_socket(), 4321, 2).with_manifest_launcher()
        .with_rpc_after_spawn(&TestEnv::default_socket(), &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert!(b(&j, "restarted") && b(&j, "stopped") && b(&j, "ok"));
    assert!(b(&dict(&j, "start"), "ok"));
    assert!(!j.contains_key("stopped_pid"));
}
```

`crates/sw-cli/tests/mgmt_graceful_stop.rs`：

```rust
mod support; use support::*;
#[test]
fn windows_task_stop_sends_daemon_stop_before_end() {
    let t = TestEnv::windows().with_config("supervision_mode: windows-task\n")
        .with_daemon_alive_then_gone(TestEnv::win_endpoint(), 900, 3);
    let (rc, out, _) = t.run(&["service", "stop"]);
    assert_eq!(rc, 0, "{out}");
    let o = t.event_order();
    assert!(o.index_of("run:schtasks /Change /TN serialwrap /DISABLE") < o.index_of("rpc:daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")), "已正常結束不得硬殺");
    assert!(o.index_of("rpc:health.ping#gone") < o.index_of("run:schtasks /Change /TN serialwrap /ENABLE"));
    assert!(b(&json(&out), "graceful"));
}
#[test]
fn windows_task_restart_aborts_when_daemon_never_exits() {
    let t = TestEnv::windows().with_config("supervision_mode: windows-task\n").with_daemon_alive_forever(TestEnv::win_endpoint(), 900);
    let (rc, out, _) = t.run(&["service", "restart"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("TIMEOUT", "service_restart_wait_stop"));
    assert!(t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")), "逾時後才 /End");
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/Run")));
    assert!(!t.calls().iter().any(|c| c.last().map(String::as_str) == Some("/ENABLE")));
    assert!(t.clock().elapsed() >= std::time::Duration::from_secs(15));
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `start_endpoint_rejected_posix` | `--endpoint tcp://1.2.3.4:1 daemon start` → exit 2、`REMOTE_NOT_SUPPORTED`、message `--endpoint 不支援 daemon start（daemon 只能在本機啟動）`、stderr context `daemon start` |
| `start_endpoint_loopback_allowed_windows` | `TestEnv::windows()`、`--endpoint tcp://127.0.0.1:48701` → 進入 spawn，cmd 含 `--socket tcp://127.0.0.1:48701` |
| `start_endpoint_socket_mismatch_invalid_args` | message `--endpoint 與 --socket 不一致；daemon start 請擇一指定 bind 位址` |
| `start_routes_systemd_user_to_service` | config `systemd-user` → stdout 含 `"_routed_to":"service start"`、calls `[["systemctl","--user","start","serialwrap"]]` |
| `start_routes_windows_task_to_schtasks` | `TestEnv::windows()`、config `windows-task` → calls `[["schtasks","/Run","/TN","serialwrap"]]` |
| `start_unknown_mode_routes_no_systemd` | config `foo` → `NO_SYSTEMD`、`_routed_to` |
| `start_already_running` | ping ok → `{"already_running":true,"ok":true,"socket":…}`、exit 0、無 spawn |
| `start_conflict_unknown_socket_hint` | 衝突 daemon `socket:None` → `existing_socket:null`、message 中顯示 `未知（無法從 /proc 讀出 --socket，可能是舊版 daemon）`、hint 為未知版本 |
| `start_conflict_detects_module_form_daemon` | ProcScan 回 `["…/python3","-I","-m","sw_core.daemon","--socket","/run/o.sock"]` 形式的實例（經 Task 4 規則 4）→ `DAEMON_ALREADY_RUNNING_ELSEWHERE` |
| `start_force_spawn_skips_precheck` | `--force-spawn` → spawn 發生 |
| `start_scan_failure_is_no_conflict` | `FakeProcScan::fail()` → spawn 發生 |
| `start_locate_order_sibling_then_runtime` | 無 launcher、有 sibling → sibling；兩者皆無、有 runtime → `[runtime,"-I","-m","sw_core.daemon"]` |
| `start_locate_honors_release_manifest_env` | `SERIALWRAP_RELEASE_MANIFEST=<m>` 指向另一 bundle → 用其 launcher |
| `start_binary_not_found` | 三者皆無 → `DAEMON_BINARY_NOT_FOUND`、message 逐字如「行為」、exit 2、stderr 鏡像 |
| `start_env_files_default_list` | 未設 env → 嘗試 `~/OPI.env`、`<pd>/OPI.env`；兩者皆不存在 → 無 bash 呼叫、JSON 無 `env_files` |
| `start_env_file_source_failure` | `bash -lc` rc 1 stderr `boom` → `ENV_FILE_SOURCE_FAILED`、`message:"boom"`、`env_file`、`env_files` |
| `start_env_file_non_utf8_value_preserved` | `env -0` 輸出含 `B=\xff\xfe` → spawn env 的 `B` 原始 bytes 為 `\xff\xfe`（非 U+FFFD） |
| `start_env_file_win_simple_parse` | Windows：檔案 `export A="1"\n# c\nB=2` → spawn env 含 `A=1`、`B=2`、未呼叫 bash |
| `start_foreground_returns_child_rc` | `foreground_rc(3)` → exit 3、stdout 空 |
| `start_daemon_exited` | poll 第 1 次 `Some(2)` → `{"error_code":"DAEMON_EXITED","ok":false,"pid":…,"returncode":2}` |
| `start_not_ready_posix_15_attempts` | ping 一直失敗 → 15 次 0.2 s sleep 後 `DAEMON_NOT_READY`；FakeProcessCtl 無 kill 記錄 |
| `start_not_ready_windows_50_attempts` | 同上 50 次 |
| `start_success_includes_warnings_and_env_files` | status 回 `warnings:["no_profiles_loaded"]` → JSON 帶 `warnings` 與 `env_files` |
| `stop_routes_supervised_unless_explicit_target` | config `systemd-user` 且無 `--socket` → `["systemctl","--user","stop","serialwrap"]`；config `windows-task` → `graceful_stop` 序列（`/DISABLE`→`rpc:daemon.stop`→`/ENABLE`）；帶 `--socket x` → 只送 RPC `daemon.stop` |
| `stop_rpc_timeout_fixed_2s_no_retries` | `--retries 3 --timeout 9 daemon stop` → 只送 1 次、timeout 2.0 |
| `stop_bench_unremembered_exit1` | `--bench B9 daemon stop` → exit 1、`BENCH_ENDPOINT_NOT_REMEMBERED` |
| `restart_supervised_systemd_equals_service_restart` | config `systemd-user` → calls `[["systemctl","--user","restart","serialwrap"]]`；stdout／exit 與同環境 `service restart` 逐位元組相同 |
| `restart_on_demand_stop_wait_start` | ping ok → `daemon.stop` → ping refused＋identity 消失 → spawn；`daemon.stop` 早於 spawn |
| `restart_on_demand_timeout_is_p1_shape` | 停止後 ping 持續 ok 10 s（FakeClock）→ stdout `{"error_code":"TIMEOUT","ok":false,"phase":"restart_wait_old_exit"}`、**exit 1**、無 spawn |
| `restart_on_demand_not_running_wraps_start` | ping 失敗 → 不送 `daemon.stop`，直接 spawn；輸出 `stopped:false`、`restarted:true`、`start:{…}` |
| `wait_gone_zombie_counts_as_gone` | ping refused、`pid_alive` 依 Task 4 zombie 規則為 false → true（不等滿 10 s） |
| `wait_gone_launcher_child_still_alive_blocks` | Windows identity tree＝launcher＋python；launcher 已死、python 仍活 → 逾時 false |
| `autostart_disabled_on_posix_caps` | `TestEnv::posix()` 相同情境 → 不 spawn、exit 2 |
| `autostart_not_for_explicit_endpoint` | Windows `--endpoint tcp://127.0.0.1:48700 session list` → 不 spawn |
| `autostart_not_for_daemon_status` | Windows、無 daemon、`daemon status` → 不 spawn、只送 1 次 `health.status`、輸出原 `SOCKET_ERROR` |
| `autostart_not_for_daemon_stop` | Windows `daemon stop`（on-demand）→ 不 spawn |
| `autostart_not_for_timeout` | connect 成功但 recv 逾時 → 不 spawn、不重送 |
| `autostart_permission_denied_no_resend` | connect 失敗 kind `PermissionDenied`（`SOCKET_ERROR`）→ 不 spawn、不重送 |
| `autostart_connect_timeout_no_resend` | connect `TimedOut`（`TIMEOUT`）→ 不 spawn、不重送 |
| `autostart_concurrent_second_cli_reuses_daemon` | 先持有 `autostart.lock` 的「贏家」在釋放前讓 ping 轉 ok → 第二個 CLI 取得鎖後 `AlreadyUp`、無 spawn、重送 1 次、無「已自動啟動」訊息 |
| `autostart_loser_daemon_exited_but_healthy_is_success` | spawn 回 `DAEMON_EXITED`（被實例鎖擋）但再探 ping ok → 重送成功、exit 0、無「自動啟動失敗」訊息 |
| `autostart_failure_reports_stderr` | spawn 後 `DAEMON_EXITED` 且再探仍失敗 → 原 `SOCKET_ERROR` 回應照印，stderr 另一行 `serialwrap: on-demand 自動啟動失敗: DAEMON_EXITED` |
| `test_daemon_start_ab_conflict`（黑箱 A/B） | 以 PATH 外的假 `serialwrapd` 行程（`tests/contract_blackbox/bin/fake-serialwrapd --socket /tmp/other`）製造衝突 → Python／Rust stdout 逐位元組相同 |
| `test_daemon_start_stop_ab_real_daemon`（黑箱 A/B，`process_rpc`） | 隔離目錄以 P3 `py_daemon` 啟動真 daemon → `daemon status` ok → `daemon stop` → ping 失敗；兩實作輸出經 contracts 正規化（pid、started_at）後相同 |
| `test_daemon_restart_ab`（黑箱 A/B，`process_rpc`） | on-demand restart 後 pid 改變；兩實作輸出形狀 `{ok,restarted,start,stopped}` 經正規化後相同 |
| `test_autostart_l15_tx_once`（黑箱，`rust_only`、`windows_native`，§0.4） | 原生 Windows 隔離 install root、無 daemon、bridge＋DUT 模擬器：`cmd submit --cmd "echo X"` → DUT 端 `wire.ndjson` 的 `echo X\r` 恰出現一次 |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_daemon --test mgmt_graceful_stop` → `not implemented: P4-T7`；黑箱 Rust 端 → exit 97（委派 sentinel）。
- [ ] **步驟 3：實作**（關鍵虛擬碼；`ctx` 即 `CliCtx`）

```rust
pub fn run_daemon_start(ns: &Namespace, ctx: &mut Ctx) -> i32 {
    let is_win = endpoint_resolve::rpc_backend_is_win(ctx);
    if let Some(r) = check_endpoint_args(ns, is_win) { return emit_err(ctx, r, "daemon start", 2); }
    let cfg = match load_config_or_report(ctx, ConfigErrorReport::JsonInternalError) { Ok(c) => c, Err(rc) => return rc };
    let mode = SupervisionMode::parse(mode_str(&cfg).as_deref().unwrap_or("on-demand"));
    if mode != SupervisionMode::OnDemand { return route_service(ctx, &mode, ServiceAction::Start, ns.flag("with_sudo"), "service start"); }
    let local = match endpoint_resolve::resolve_local_endpoint_info(ns, ctx) { Ok(r) => r, Err(e) => return print_endpoint_err(ctx, e, "daemon start") };
    if probe_healthy(ctx, &local.endpoint, 0.5) { print_json(&mut ctx.io, &pydict!{"already_running"=>true,"ok"=>true,"socket"=>local.endpoint}); return 0; }
    let (sock, src) = spawn_target(ns, ctx);
    if !ns.flag("force_spawn") {
        if let Ok(ds) = ctx.p.procs.serialwrapd_instances() {
            if let Some(d) = find_conflicting_daemon(&sock, &ds, &cwd(ctx), ctx.p.caps.path_flavor) {
                print_json(&mut ctx.io, &elsewhere_payload(d, &sock)); return 2;          // 不 mirror_err（KEEP）
            }
        }
    }
    let argv = match locate_daemon_argv(ctx) { Ok(a) => a, Err(_) => return emit_err(ctx, binary_not_found(), "daemon start", 2) };
    let cmd = [argv, vec!["--profile-dir".into(), pd, "--socket".into(), sock.clone(), "--lock".into(), lock]].concat();
    let (env, loaded) = match load_daemon_env(ctx, &resolve_daemon_start_env_files(ctx, &pd)) { Ok(v) => v, Err(e) => return emit_err(ctx, e.to_pydict(), "daemon start", 2) };
    if ns.flag("foreground") { return ctx.p.process.call_foreground(&cmd, &env).unwrap_or(1); }
    let child = ctx.p.process.spawn_daemon(&cmd, &env, true)?;
    for _ in 0..(if is_win { 50 } else { 15 }) {
        ctx.p.clock.sleep(Duration::from_millis(200));
        if let Some(rc) = ctx.p.process.poll(&child) { return emit_err(ctx, daemon_exited(child.pid, rc), "daemon start", 2); }
        if ping_traced(ctx, &sock, src, 0.5) { return print_started(ctx, child.pid, &sock, &loaded, status_warnings(ctx, &sock)); }
    }
    emit_err(ctx, pydict!{"error_code"=>"DAEMON_NOT_READY","ok"=>false,"pid"=>child.pid}, "daemon start", 2)
}
```

`elsewhere_payload`、hint、message 字串逐字照 01 §7 步驟 7。移除 `daemon` 群組委派。`contracts/behavior_changes.yaml`：SC-18、SC-20、DF-05 `status: IMPLEMENTED`（`case_ids` 不改）；`tests/acceptance/p4_case_map.yaml` 把 `sc18-windows-ondemand-autostart` 綁到本 Task 的 `autostart_*` 與 `test_autostart_l15_tx_once`、`sc20-daemon-restart` 綁到 `restart_*` 與 `test_daemon_restart_ab`、`df05-daemon-locate-order` 綁到 `start_locate_*`／`start_binary_not_found`。`contracts/error_codes.yaml`：`TIMEOUT` 補 `cli_local` interface 與 phase 附註（§0.3）。

- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-cli --test mgmt_daemon --test mgmt_graceful_stop
scripts/rust/win-test.sh --package sw-cli -- mgmt_daemon mgmt_graceful_stop
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_daemon.py
python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_daemon.py
python3 scripts/rust/check_cfg_boundary.py
```

- [ ] **步驟 5：commit**：`feat(rust-cli): 原生承接 daemon start/stop/status/restart 與多開預檢`

**BLOCKED-OWNER：**
- Windows on-demand 首次需求啟動在 OD-05 機台以正式 artifact（原生 build）實跑，含 EDR 開啟下 `DETACHED_PROCESS` spawn 不被攔截的證據；`test_autostart_l15_tx_once` 的原生 Windows COM 版本依 OD-08（com0com 或實體 USB-UART）。
- `daemon restart` 在實體 UART 上確認舊 daemon 釋放 tty 後新 daemon 才取得（Spike 6 實機部分）。

---

## Task 8：`remote`（`-R`／`-L`／`status`／`close`／`stop` 別名）原生與 BC-05 Windows 路徑（SC-21）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max（fail-closed 隧道、跨行程鎖、BC-05 Windows 程序語意）
**Changelog fragment:** `changelog.d/<issue>-rust-p4-remote.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/remote_tunnel.rs`、`crates/sw-cli/src/commands/remote_cmd.rs`、`crates/sw-cli/src/delegate.rs`（移除 `remote`）
- Create: `crates/sw-cli/tests/mgmt_remote.rs`、`tests/contract_blackbox/test_cli_mgmt_remote.py`
- Modify: `tools/docker/remote_tunnel_test.sh`（新增 `SERIALWRAP_CLI_BUNDLE_DIR`：設定時以唯讀 bind mount 掛入容器 `/opt/sw-bundle`，所有 `docker exec … serialwrap` 改為 `/opt/sw-bundle/bin/serialwrap` 絕對路徑；未設定時行為不變；每個拓樸維持既有逾時與清理）

**Interfaces:**
- Consumes：`ProcessTunnel`、`FsOps::file_lock_exclusive`（Task 5）、`SysEnv::run`（runner）、`SysEnv::which`、`RuntimePaths::remote_run_dir`、`pyjson`（`DISK_STABLE`）、`sha2`。
- Produces：

```rust
pub struct TunnelSpec { pub role: Role, pub ssh_target: String, pub port: u16, pub local: Option<u16>, pub forward_src: Option<String>,
                        pub remote_socket: Option<String>, pub via: Via, pub ssh_opts: Vec<String>, pub ready_timeout: f64 }
pub enum Role { Expose, Connect }  pub enum Via { Ssh, Autossh }
pub struct TunnelError { pub code: &'static str, pub message: Option<String> }
pub fn parse_target(t: &str) -> Result<(String, u16), TunnelError>;
pub fn compute_identity(spec: &TunnelSpec) -> String;
pub fn default_ssh_opts(control_path: &str) -> Vec<String>;
pub fn build_argv(spec: &TunnelSpec, cp: &str, control_master: bool) -> Vec<String>;
pub fn bind_is_loopback_only(ss_out: &str, port: u16) -> bool;
pub struct Registry { pub remote_dir: PathBuf }
impl Registry { pub fn new(run_dir: &Path) -> Self; pub fn lock(&self, fs: &dyn FsOps) -> PalResult<FileLockGuard>; pub fn read(&self, port: i64) -> Option<PyDict>;
                pub fn read_all(&self) -> Vec<PyDict>; pub fn write(&self, st: &PyDict, pid: u32) -> std::io::Result<()>; pub fn remove(&self, port: i64); }
pub fn open_tunnel(ctx: &Ctx, spec: &TunnelSpec, run_dir: &Path) -> Result<PyDict, TunnelError>;
pub fn status(ctx: &Ctx, run_dir: &Path) -> PyDict;
pub fn close(ctx: &Ctx, run_dir: &Path, selector: &str) -> PyDict;
pub fn run_remote(ns: &Namespace, ctx: &mut Ctx) -> i32;
```

行為：
- POSIX：逐字照 07 §8.1～§8.5（`_TARGET_RE`、identity 字串組合含 `None`、`default_ssh_opts` 順序、`_direction_args`、`build_argv`、`ssh -O check`、role probe 第一次失敗即 `REMOTE_BIND_UNVERIFIED`（PLT-O-10 KEEP）、runner `timeout 10.0` → `(124, "probe timeout: …"[:500])`、registry 檔名與 JSON 欄位、`_public`、`remote_hint`、`status` prune 與 orphan、`close` 只在 alive 時終止（PLT-O-11 KEEP）、`close` 冪等 `closed:[]`）。registry 鎖改用 `FsOps::file_lock_exclusive(remote_dir/.registry.lock, blocking=true)`（POSIX 等同 flock）。
- CLI：`words` 空或 `status` → status；`close` 或 `stop`（SC-21 別名）→ `selector = if words.len() > 1 { words[1] } else { "all" }`（等同 `cli.py:1564` 的 `words[1] if len(words) > 1 else "all"`；`remote close ""` 篩出 0 筆回 `closed:[]`，**不得**當成 all）；其他 → open。錯誤 exit 1，訊息逐字照 07 §8.5；多餘 words 忽略（KEEP）。不再有 `REMOTE_NOT_SUPPORTED`（BC-05：Windows 支援）。
- Windows（`ProcessTunnel::supports_control_master()==false`，BC-05）：
  - `build_argv(control_master=false)`：省略 `ControlMaster=auto`、`ControlPath=…`、`ControlPersist=no` 三組 `-o`，其餘相同；`compute_identity` 不變（仍含使用者 `ssh_opts`）。state JSON 照 P1 T14（`02-stage0-python-bc.md` L1949）：`pgid` 寫 `null`、`pid_start_ticks` 寫 CreationTime 整數、`control_path` **仍寫入**（不建立該檔）；讀取端容忍 `pgid:null`（1A 委派期間 Python 寫的 registry 與 Rust 混用）。
  - readiness：`ssh_check` 改為「行程存活」；role probe：connect → 對本地 forward `health.ping`(2.0)；expose＋`--remote-socket` → true；expose＋tcp → `["ssh", *使用者 ssh_opts, "-o","BatchMode=yes", ssh_target, "ss","-ltnH","sport = :<port>"]`（新連線，不用 ControlPath）。Windows 沒有 `ssh -O check` 保證 `-R` forward 已建立，因此 **expose 探測改為輪詢**（BC-05 競態修正，§0.5 已請 P1 同步）：探測 rc≠0 或輸出無 `:<port>` 監聽列 → 「尚未就緒」，每 0.5 s 重探直到 `ready_timeout`，逾時回 `status:"starting"`（與 POSIX deadline 逾時相同形狀，不 terminate）；只有 `ss` rc 0 且列出該 port 但 `bind_is_loopback_only==false`（wildcard 或非 loopback）時才 `REMOTE_BIND_UNVERIFIED` 並 terminate（fail-closed）；connect 的 ping 失敗同樣輪詢至 deadline。POSIX 維持 PLT-O-10「第一次失敗即 `REMOTE_BIND_UNVERIFIED`」。
  - 存活：`ProcessTunnel::alive`；停止：`terminate`（先核對根 CreationTime，再整棵樹）。
  - `forward_src`：daemon endpoint 為 tcp → `127.0.0.1:<port>`（照 `_forward_src_from_endpoint`）。
  - `ssh` 可執行檔解析：`SysEnv::which("ssh")`（Windows 依 PATHEXT 找到 `ssh.exe`），找不到 → `SSH_NOT_FOUND` `PATH 找不到 ssh`。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-cli/tests/mgmt_remote.rs`）

```rust
mod support; use support::*;
use sw_cli::remote_tunnel::*;

fn spec(role: Role) -> TunnelSpec {
    TunnelSpec { role, ssh_target: "u@relay".into(), port: 48800, local: None, forward_src: Some("/run/sw.sock".into()),
                 remote_socket: None, via: Via::Ssh, ssh_opts: vec!["-p2222".into()], ready_timeout: 10.0 }
}

#[test]
fn posix_expose_argv_is_byte_exact() {
    assert_eq!(build_argv(&spec(Role::Expose), "/r/remote/cm-48800", true),
        ["ssh","-N","-T","-o","BatchMode=yes","-o","ExitOnForwardFailure=yes","-o","ControlMaster=auto","-o","ControlPath=/r/remote/cm-48800",
         "-o","ControlPersist=no","-o","ServerAliveInterval=30","-o","ServerAliveCountMax=3","-p2222","-R","127.0.0.1:48800:/run/sw.sock","u@relay"]
        .iter().map(|s| s.to_string()).collect::<Vec<_>>());
}

#[test]
fn windows_argv_drops_control_master_but_identity_unchanged() {
    let a = build_argv(&spec(Role::Expose), "ignored", false);
    assert!(!a.iter().any(|x| x.starts_with("Control")));
    assert_eq!(compute_identity(&spec(Role::Expose)),
               sha256_hex("expose|u@relay|48800|None|/run/sw.sock|None|ssh|-p2222"));
}

#[test]
fn remote_stop_is_alias_of_close() {
    let t = TestEnv::posix().with_registry_state(48800, alive_state("expose", 999));
    let (rc, out, _) = t.run(&["remote", "stop", "48800"]);
    assert_eq!((rc, out.as_str()), (0, "{\"closed\":[48800],\"ok\":true}\n"));
    assert_eq!(t.tunnel().terminated(), vec![999]);
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `parse_target_errors` | `relay` → `INVALID_TARGET` `target 需為 [user@]host:port，取得 'relay'`；`h:0` → `port 超出範圍：0` |
| `connect_argv_default_dest` | `-L 127.0.0.1:<local>:localhost:<port>`；`--remote-socket /s` → `127.0.0.1:<local>:/s` |
| `autossh_prefix` | `via=Autossh` → argv 開頭 `["autossh","-M","0","-N","-T"]` |
| `bind_loopback_only_cases` | `LISTEN 0 128 127.0.0.1:48800 0.0.0.0:*` → true；含 `0.0.0.0:48800` → false；`[::1]:48800` → true；無匹配 → false |
| `open_already_running_same_identity` | 既有 state alive 同 identity → `{"already_running":true,"ok":true,…}`，無 spawn |
| `open_conflict_different_identity` | `TUNNEL_CONFLICT` `port 48800 已有不同 identity 的隧道，請先 remote close 48800`、exit 1 |
| `open_spawn_failed_reports_tail` | 行程 1 tick 後死、tail `Permission denied` → `TUNNEL_SPAWN_FAILED`，message 為 tail，registry 已移除 |
| `open_role_probe_unverified_no_retry`（POSIX，PLT-O-10 KEEP） | `ss` 回空 → `REMOTE_BIND_UNVERIFIED` `無法在 relay 驗證遠端 bind`，`ss` 只呼叫 1 次，tunnel 被 terminate |
| `open_starting_when_deadline_exceeded` | ssh_check 一直 false → status `starting`、exit 0 |
| `open_local_on_expose_invalid_args` | `-R --local 1 h:1` → `INVALID_ARGS` `--local 僅用於 -L（connect）` |
| `open_r_and_l_invalid_args` | `-R -L h:1` → `-R 與 -L 不可同時指定` |
| `status_prunes_dead_and_lists_orphan` | dead state 被移除；`cm-49000` 無 state → `{"alive":true,"control_path":…,"status":"orphan"}` |
| `close_dead_pid_not_signalled` | pid 已死 → 不呼叫 terminate、仍列入 closed |
| `close_terminate_error_marks_error` | terminate Err → state 寫回 `status:"error"`、不列入 closed |
| `registry_write_disk_stable` | state 檔內容 = `DISK_STABLE` 序列化、無尾端換行、檔名 `<port>.json` |
| `windows_role_probe_tcp_uses_new_connection` | `TestEnv::windows()` expose tcp → runner argv == `["ssh","-p2222","-o","BatchMode=yes","u@relay","ss","-ltnH","sport = :48800"]` |
| `windows_connect_ready_via_ping` | ping 本地 forward ok → `active` |
| `windows_state_matches_p1_schema` | 寫入 state 的 `pgid` 為 null、`pid_start_ticks` 為整數、`control_path` 為字串（檔案不存在） |
| `registry_reads_python_written_pgid_null` | 以 P1 Python 形狀（`pgid:null`）的 state 檔 → `status` 正常列出、`close` 正常終止 |
| `windows_expose_probe_before_forward_ready_retries` | FakeTunnel 存活；runner 前 3 次 `ss` 回空、第 4 次回 `LISTEN 0 128 127.0.0.1:48800 0.0.0.0:*` → `active`；`ss` 呼叫 4 次；無 terminate |
| `windows_expose_probe_rc_nonzero_is_not_ready` | runner 一直 rc 255 → deadline 後 `status:"starting"`、exit 0、無 terminate |
| `windows_expose_wildcard_bind_fails_closed` | 第 1 次 `ss` 即回 `0.0.0.0:48800` → `REMOTE_BIND_UNVERIFIED`、terminate 1 次 |
| `remote_close_empty_selector_closes_nothing` | 兩條 alive 隧道、`remote close ""` → `{"closed":[],"ok":true}`、無 terminate；`remote stop ""` 同 |
| `test_remote_ab_status_close_empty`（黑箱 A/B） | 空 run_dir：`remote` → `{"ok":true,"tunnels":[]}`；`remote close` → `{"closed":[],"ok":true}`；Python 與 Rust 相同 |
| `test_remote_ab_open_with_stub_ssh`（黑箱 A/B） | stub `ssh`（`-O check` rc 0；長駐模式 sleep）＋ stub `ss` 回 loopback → 兩實作輸出經 pid 正規化後相同 |
| `test_remote_topologies_docker`（cap `docker`、`process_rpc`；`bc05-three-topologies` 的 Linux 面） | `SERIALWRAP_CLI_BUNDLE_DIR=$SW_BUNDLE bash tools/docker/remote_tunnel_test.sh all` exit 0：`direct`、`nat_host`、`dual_nat`、`agent_pull` 四拓樸加 `gwports` fail-closed 全過；log 中每次 `serialwrap` 呼叫路徑皆為 `/opt/sw-bundle/bin/serialwrap` |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_remote` → `not implemented: P4-T8`；黑箱 Rust → 97。
- [ ] **步驟 3：實作**：依「行為」；`open_tunnel` 的「鎖內讀判定→spawn→寫 spawning」、「鎖外 wait_ready」、「任何錯誤（含 panic，以 `catch_unwind` 包住）→ terminate＋鎖內 remove＋原錯誤回傳」順序逐字照 07 §8.4。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-cli --test mgmt_remote
scripts/rust/win-test.sh --package sw-cli -- mgmt_remote
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_remote.py
SERIALWRAP_CLI_BUNDLE_DIR=$SW_BUNDLE bash tools/docker/remote_tunnel_test.sh all   # 四拓樸＋gwports（需 Docker）
bash tools/docker/remote_tunnel_test.sh all                                        # 未設 bundle：既有 Python 行為不變
```

- [ ] **步驟 5：commit**：`feat(rust-cli): 原生承接 remote 並支援 Windows 隧道（BC-05）`

**BLOCKED-OWNER：** BC-05 Windows 端以 Win32-OpenSSH 對真實 relay／bench 跑既有拓樸、程序生命週期、停止與重啟（`bc05-windows-remote-reverse`、`bc05-windows-remote-local`、`bc05-process-lifecycle-stop-restart`；需擁有者 relay 與 Windows 網路環境）；L3 `realhw` remote 套件以 Rust CLI 執行。

---

## Task 9：`connect`／`benches` 原生（SC-27）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/<issue>-rust-p4-connect-benches.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/commands/{connect_cmd.rs,benches_cmd.rs}`、`crates/sw-cli/src/bench_state.rs`（加寫入與跨行程鎖）、`crates/sw-cli/src/delegate.rs`（移除 `connect`、`benches`）
- Create: `crates/sw-cli/tests/mgmt_connect.rs`、`tests/contract_blackbox/test_cli_mgmt_connect.py`

**Interfaces:**
- Consumes：`remote_tunnel::{open_tunnel, status, close, compute_identity}`（Task 8）、`bench_registry`（Task 17）、P3 `bench_state::{load_bench_state_doc, remembered_bench_endpoint}`、`FsOps::{file_lock_exclusive, atomic_write}`（Task 5）。
- Produces：

```rust
pub fn run_connect(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub fn run_benches(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub fn bench_state::remember(ctx: &Ctx, code: &str, endpoint: &str) -> Result<(), BenchStateError>;   // <path>.lock 跨行程鎖（Windows 亦然，SC-27）
pub fn bench_state::forget(ctx: &Ctx, code: &str) -> Result<(), BenchStateError>;
pub fn benches_cmd::status_tunnels_from(resp: &PyDict) -> Result<Vec<PyDict>, TunnelError>;   // cli.py:1262-1276 的 ok／tunnels 檢查（純函式）
```

行為：逐字照 07 §9.3、§9.4（`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`--close` 的相符檢查 `TUNNEL_CONFLICT`、`TUNNEL_STILL_ACTIVE`、記憶寫入失敗時 rollback 與 `rollback_warning`、`TUNNEL_STATUS_ERROR`、benches 依 code 排序與 `alive` 判定）。寫入：新檔扁平根、既有含 `benches` 物件則寫巢狀；`atomic_write`（mkstemp prefix `benches.state.json.tmp.`、0600、`DISK_STABLE`、無尾端換行；P3 的 `FsOps::atomic_write` 已含 fsync，與 Python 的「無 fsync」差異屬可接受的加強，不影響位元組）。argparse 錯誤且 JSON 邊界命令 → stdout `{"error_code":"INVALID_ARGS","ok":false}`、stderr usage＋error 後再加 `serialwrap: connect failed: INVALID_ARGS`，exit 1（P3 已實作邊界判定，本 Task 驗證原生路徑）。Windows 不再回 `REMOTE_NOT_SUPPORTED`（SC-27）。

- [ ] **步驟 1：寫失敗測試**

```rust
mod support; use support::*;
#[test]
fn connect_open_remembers_flat_endpoint() {
    let t = TestEnv::posix()
        .with_benches_yaml("benches:\n  B1:\n    target: u@relay\n    remote_socket: /run/serialwrap/serialwrapd.sock\n    local_port: 49001\n    ssh_opts: []\n    autossh: false\n")
        .with_tunnel_ready();
    let (rc, _, _) = t.run(&["connect", "B1"]);
    assert_eq!(rc, 0);
    assert_eq!(std::fs::read_to_string(t.bench_state_path()).unwrap(), "{\"B1\":\"tcp://127.0.0.1:49001\"}");
}
#[test]
fn connect_close_refuses_mismatched_identity() {
    let t = TestEnv::posix().with_benches_yaml(B1_YAML).with_bench_state("{\"B1\":\"tcp://127.0.0.1:49001\"}")
        .with_registry_state(49001, alive_state_with_identity("connect", 77, "deadbeef"));
    let (rc, out, _) = t.run(&["connect", "B1", "--close"]);
    assert_eq!(rc, 1);
    assert_eq!(s(&json(&out), "message"), "local_port=49001 已有不相符的 stateful tunnel；僅可關閉相符的 connect tunnel");
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `connect_unknown_code` | `UNKNOWN_BENCH_CODE`、message `unknown bench code: B9`、exit 1 |
| `connect_invalid_config_extra_key` | entry 多 `host:` → `INVALID_BENCH_CONFIG`、message 以 `bench "B1" 包含不支援欄位` 開頭 |
| `connect_target_requires_user_at_host` | `target: relay` → message `bench "B1" 的 target 必須是 user@host 形式` |
| `connect_nested_state_preserved` | 既有 `{"benches":{"B2":…}}` → 寫入後為巢狀且保留 B2 |
| `connect_remember_failure_rolls_back` | 狀態檔目錄不可寫 → `BENCH_STATE_IO_ERROR`、tunnel 被 close、exit 1 |
| `connect_close_still_active` | close 後 status 仍列出該 port（orphan `cm-49001`）→ `TUNNEL_STILL_ACTIVE`、記憶未清除 |
| `connect_close_clears_memory` | close 成功 → 狀態檔不含 B1 |
| `connect_missing_code_json_boundary` | `connect` → stdout `{"error_code":"INVALID_ARGS","ok":false}`、exit 1、stderr 末行 `serialwrap: connect failed: INVALID_ARGS` |
| `benches_sorted_with_alive` | 兩 bench、B2 記憶 port 在 connect tunnel 中 → `[{"alive":false,"code":"B1","endpoint":null},{"alive":true,"code":"B2","endpoint":"tcp://127.0.0.1:49002"}]` |
| `benches_status_tunnels_from_error_shapes`（unit） | `status_tunnels_from`：`{"ok":false}` → `TUNNEL_STATUS_ERROR`／`無法確認 tunnel 狀態`；`{"ok":false,"error_code":"X","message":"m"}` → `X`／`m`；`{"ok":true}`（缺 tunnels）→ `TUNNEL_STATUS_ERROR`／`remote status 回應缺少 tunnels 清單`。Python「status 回非 dict」分支在 Rust 型別上不可達（`status()` 回 `PyDict`），於 PR 描述註記，不另造測試 |
| `windows_connect_supported`（`TestEnv::windows()`） | 不回 `REMOTE_NOT_SUPPORTED`；spawn argv 無 Control 選項 |
| `bench_state_lock_cross_process`（原生 Windows 與 POSIX） | 兩行程同時 remember 不同 code → 兩者皆存在 |
| `test_connect_benches_ab`（黑箱 A/B） | stub ssh：connect→benches→connect --close→benches 四步輸出 Python／Rust 相同（pid 正規化） |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_connect` → `not implemented: P4-T9`。
- [ ] **步驟 3：實作**：照「行為」；移除委派。`contracts/behavior_changes.yaml` SC-27 `status: IMPLEMENTED`（`case_ids` 不改），`p4_case_map.yaml` 把 `sc27-windows-connect-benches` 綁到本 Task 測試。
- [ ] **步驟 4：執行並確認 PASS**：`cargo test -p sw-cli --test mgmt_connect`、`scripts/rust/win-test.sh --package sw-cli -- mgmt_connect`、黑箱兩實作。
- [ ] **步驟 5：commit**：`feat(rust-cli): 原生承接 connect 與 benches（含 Windows）`

**BLOCKED-OWNER：** Windows 測試團隊工作站經真實 bench relay 的 `connect`／`--bench` 端到端（需 bench 與 relay 帳號）。

---

## Task 10：`setup`（資產物化、WSL systemd、reconcile）與 `supervision-mode set`（SC-12、SC-22、SC-24、DF-10～DF-13、DF-16）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-setup.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/{setup_cmd.rs,assets.rs,state_migrate.rs,wslconf.rs,supervisor_defs.rs}`、`crates/sw-cli/src/commands/supervision_cmd.rs`、`crates/sw-cli/src/delegate.rs`（移除 `setup`）
- Create: `crates/sw-cli/tests/mgmt_setup.rs`、`crates/sw-cli/tests/golden/wslconf.ndjson`（由 `scripts/rust/gen_wslconf_golden.py` 以 Python `configparser` 產生）、`scripts/rust/gen_wslconf_golden.py`、`tests/contract_blackbox/test_cli_mgmt_setup.py`

**Interfaces:**
- Consumes：`ServiceCtl`（Task 3）、`ProcScan`（Task 4）、`FsOps::{symlink_or_copy_dir, copy_preserving, set_mode, atomic_write}`（Task 5）、`RuntimeConfig::set_mode`／`save_runtime_config`（Task 6）、`capture_daemon_identity`、`wait_daemon_gone`、`graceful_stop`、`run_daemon_stop` 內部函式（Task 7）、`install_layout::resolve_install_root`（Task 11，用於 Windows Task XML 的穩定入口路徑）。
- Produces：

```rust
pub struct UserDirs { pub config: PathBuf, pub data: PathBuf, pub agents_skill_link: PathBuf, pub legacy_agents_skill_link: PathBuf, pub bin: PathBuf }
pub fn user_dirs(env: &EnvSnapshot, home: &Path) -> UserDirs;                       // 07 §6.1（env 值不 strip、不 expanduser，KEEP）
pub struct Asset { pub rel: &'static str, pub bytes: &'static [u8] }
pub const PROFILE_ASSETS: &[Asset]; pub const SKILL_ASSETS: &[Asset]; pub const TOOL_ASSETS: &[Asset];   // include_bytes!("../../../sw_core/assets/...")
pub const WRAPPER_MAP: &[(&str, &str)];   // [("minicom_router.sh","serialwrap-minicom"),("minicom-broker.sh","serialwrap-minicom-broker"),("minicom-raw.sh","serialwrap-minicom-raw")]
pub struct MaterializeReport { pub profiles: PathBuf, pub skill_link: PathBuf, pub skill_link_kind: LinkKind, pub bin: Option<PathBuf> }
pub fn materialize_assets(ctx: &Ctx, dirs: &UserDirs, force: bool) -> Result<MaterializeReport, SetupError>;
pub fn detect_legacy_install(ctx: &Ctx, home: &Path) -> Option<PyDict>;
pub struct WslInfo { pub wsl: bool, pub already: bool, pub enabled_now: bool, pub needs_restart: bool, pub rc: Option<i32>, pub hint: Option<String>, pub pending: Vec<Vec<String>> }
pub fn ensure_wsl_systemd(ctx: &Ctx, home: &Path, target: &SupervisionMode, with_sudo: bool) -> WslInfo;  // DF-11
pub fn merge_wsl_conf_systemd(existing: &str) -> String;                             // configparser 相容
pub fn render_user_unit(exec_start: &str) -> String;                                 // 07 §5.4 逐字、無尾端換行
pub fn render_system_unit(exec_start: &str, run_user: &str) -> String;
pub struct ReconcileRequest { pub old_mode: SupervisionMode, pub target_mode: SupervisionMode, pub home: PathBuf, pub daemon_running: bool,
                              pub daemon_id: DaemonIdentity, pub old_endpoint: String, pub any_flashing: bool, pub with_sudo: bool, pub force: bool,
                              pub socket_path: Option<String>, pub config_path: PathBuf }
pub enum ReconcileError { FlashingBusy(String), WaitOldExitTimeout { partial: PyDict }, Config(String) }
pub fn reconcile(ctx: &mut Ctx, req: &ReconcileRequest) -> Result<PyDict, ReconcileError>;
pub fn resolve_target_mode(ns: &Namespace, ctx: &Ctx) -> Result<SupervisionMode, PyDict>;
pub fn run_setup(ns: &Namespace, ctx: &mut Ctx) -> i32;
pub fn sw_cli::commands::supervision_cmd::run_supervision_set(ns: &Namespace, ctx: &mut Ctx, mode: &str) -> i32;
pub fn state_migrate::migrate_legacy_state(fs: &dyn FsOps, legacy: &Path, dest: &Path) -> bool;      // 存在且 dest 不存在才 copy2
```

行為：
- **target 解析**：`--user`→`systemd-user`、`--system`→`systemd-system`、`--on-demand`→`on-demand`、`--windows-task`→`windows-task`、`--windows-service`→`windows-service`；無旗標：`supported_modes` 含 `WindowsTask` → `windows-task`（OD-03）；否則 `has_systemd` ? `systemd-user` : `on-demand`。旗標指定的模式不在 `ServiceCtl::supported_modes()`（on-demand 例外，永遠支援）→ stdout `{"error_code":"INVALID_ARGS","message":"監管模式 <m> 不適用於本平台（可用：<以「、」串接的 supported_modes>）","ok":false}`、stderr 鏡像（context `setup`）、exit 2。
- **流程**（照 07 §6.7，差異標註）：
  1. legacy 偵測（07 §6.3 逐字；`state_json` 檢查 `/tmp/serialwrap/state.json`）。
  2. `old = config.mode() or "on-demand"`；壞 YAML → DF-07 JSON exit 1。
  3. 探測（忽略 `--bench`）：`health.ping`(0.5) → `daemon_running`；running 時 `capture_daemon_identity`（`health.status`(1.0).pid＋ProcScan，供 SC-12；P1 同樣以 `daemon status.pid` 取 `old_pid`）；`mcu.status`(0.5).`flashing` → `any_flashing`。例外皆 false。
  4. `any_flashing && !force` → `FLASHING_BUSY`（message `flash 進行中，拒絕 setup（可用 --force 覆寫）`）exit 2，物化前。
  5. `materialize_assets(force)`：profiles 不覆寫（除非 force）；skill 永遠刷新到 `data/skill`，`symlink_or_copy_dir(agents_skill_link, data/skill)`（DF-16），移除 legacy `serialwrap-mcp` link（只在是 symlink 且目標 basename 為 `serialwrap-mcp` 或路徑含 `custom-skills/serialwrap-mcp`）；wrappers 只在 `caps.console_kind==ConsoleKind::Pty` 時寫入 `bin`（copy＋`chmod 0755`，永遠覆寫；DF-13 的 broker 修正已在 Python 資產，Rust 原樣嵌入）。
  6. `ensure_wsl_systemd`（DF-11）：非 WSL → `{wsl:false,…}`；WSL 且有 systemd → already；否則 merge＋staging `home/.local/share/serialwrap/wsl.conf`；**只有** target 為 systemd 模式且 `with_sudo` 才執行 `["sudo","install","-m","0644",staging,"/etc/wsl.conf"]`，`needs_restart = rc==0`；否則把該 argv 放進 `pending`，`needs_restart=false`，setup 繼續 reconcile。`needs_restart` → 07 §6.7 步驟 7 的 JSON exit 0。
  7. effective socket：old==target==on-demand、非 win backend、無 `--socket`／`--endpoint`、既有 config socket 為可解析 tcp → 保留（`preserved_remote_socket`，照 `docs/setup-tcp-endpoint.md`）；target `systemd-system` → `/run/serialwrap/serialwrapd.sock`；target `windows-task`／`windows-service` 或 win backend on-demand → `local_default_endpoint`（tcp，SC-24）；其他 → `SOCKET_PATH`。
  8. `reconcile`，成功輸出 07 §6.7 步驟 10 的 JSON（`pending_sudo` 非空才帶；WSL pending 併入），exit 0。
- **reconcile**（07 §6.6 逐字，差異如下）：
  - 護欄 2 擴充：target 或 old 為 `windows-service` 且 old≠target → `install_plan`／停止指令全部進 `pending_sudo`，`applied:false`、不停舊、不寫 config；`warnings:["windows-service 依 OD-12 須完成多使用者認證／授權後才可部署；本版只列出 pending_sudo 指令，不自動安裝"]`。
  - 停舊：on-demand 且 running → 內部 `daemon.stop`（RPC 2.0；`ran` 記 `["serialwrap","daemon","stop"]`，不經 PATH）；systemd-user → stop＋disable；systemd-system → sudo stop＋disable（或 pending）；windows-task／windows-service → `graceful_stop(keep_disabled=true, phase:"reconcile_wait_old_exit")`（`/Change /DISABLE` → RPC `daemon.stop` → 等待 → 逾時才 `/End`；`ran` 記錄實際執行的 schtasks 與 `["serialwrap","daemon","stop"]`）。
  - **SC-12（雙邊，逐字照 P1 L2080）**：停舊後、起新前呼叫 `wait_daemon_gone(old_endpoint, &daemon_id, 10s)`（old 為 on-demand 且未 running 時略過；`daemon_id.tree` 為空時只看 ping，與 P1「`old_pid` 取不到為 None → 只看 ping」相同）；逾時 → `ReconcileError::WaitOldExitTimeout`，不起新、不寫 config；reconcile 結果為既有欄位（`mode`=old、`transitioned:false`、`applied:false`、`ran`、`pending_sudo`…）加 `"ok":false,"error_code":"TIMEOUT","phase":"reconcile_wait_old_exit"`，setup／`supervision-mode set` 的 CLI 外層形狀與 **exit 1** 以 P1 實作為 oracle（A/B `test_setup_ab_reconcile_timeout`）。
  - state 遷移（DF-10，逐字照 P1 `02-stage0-python-bc.md` L2197）：`systemd-system` 遷移目標為 `/var/lib/serialwrap/state.json`，且 `render_system_unit` 的 unit 文字含 `Environment=SERIALWRAP_STATE_DIR=/var/lib/serialwrap`（daemon 實際讀寫該路徑）；Windows 模式間不遷移（路徑相同）。
  - 起新：systemd-user → 寫 unit（`render_user_unit("%h/.local/bin/serialwrapd")`）＋ daemon-reload／enable／start ＋ `loginctl enable-linger <$USER or $LOGNAME>`；systemd-system → `_system_install_cmds(include_start=True)`，unit 的 `ExecStart` 可執行檔固定為穩定入口 `<InstallRoot.bin>/serialwrapd`（絕對路徑；Python 用 `shutil.which("serialwrapd")`，Rust 依 §4.3 不搜尋 PATH——此差異登記於 `behavior_changes.yaml` DF-05 附註，A/B 比較 unit 時以正規化規則把 ExecStart 可執行檔路徑替換為 `<serialwrapd>`）；windows-task → `ServiceCtl::install(SupervisorDefinition{mode:WindowsTask, name:"serialwrap", exec:[<install_root>\bin\serialwrapd.exe, "--socket", <effective_socket>], working_dir:<install_root>\bin, definition_path:<install_root>\supervisor\serialwrap.task.xml})` 後 `action(Start)`；同模式 windows-task → 只重新 install（不 start），與 systemd-user 同模式「enable 不 start」對稱。
  - `ran`（DF-12）＝實際執行過的 argv（含 `ServiceCtl` 回傳的 `CmdRecord.argv`），依執行順序。
- **`supervision-mode set <mode> [--force] [--with-sudo]`**（SC-22，Rust-only）：words 不是 `[]` 或 `["set", MODE]` → `{"error_code":"INVALID_ARGS","message":"用法：serialwrap supervision-mode [set MODE]","ok":false}` exit 2；MODE 不在 5 個合法值或不受支援 → 同 setup 的 `INVALID_ARGS` 訊息；執行 setup 的步驟 2、3、4（`FLASHING_BUSY` message 改為 `flash 進行中，拒絕切換監管模式（可用 --force 覆寫）`）、7、8（**不**物化資產、**不**做 WSL 處理）；成功 stdout `{"ok":true,"previous_mode":<old>,"setup":<reconcile 結果>[,"pending_sudo":[…]]}` exit 0。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-cli/tests/mgmt_setup.rs`）

```rust
mod support; use support::*;
#[test]
fn on_demand_to_systemd_user_stops_waits_then_starts() {
    let t = TestEnv::posix().with_systemd(true).with_var("USER", "alice")
        .with_daemon_alive_then_gone(TestEnv::default_socket(), 4321, /*polls_until_gone*/ 3);
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 0, "{out}");
    let setup = dict(&json(&out), "setup");
    assert_eq!(s(&setup, "mode"), "systemd-user");
    assert_eq!(argv_list(&setup, "ran"), vec![
        vec!["serialwrap","daemon","stop"], vec!["systemctl","--user","daemon-reload"],
        vec!["systemctl","--user","enable","serialwrap"], vec!["systemctl","--user","start","serialwrap"],
        vec!["loginctl","enable-linger","alice"]]);
    let order = t.event_order();   // RPC 與 run 呼叫的合併時間序
    assert!(order.index_of("rpc:daemon.stop") < order.index_of("rpc:health.ping#gone"));
    assert!(order.index_of("rpc:health.ping#gone") < order.index_of("run:systemctl --user start serialwrap"));
    assert_eq!(std::fs::read_to_string(t.home().join(".config/systemd/user/serialwrap.service")).unwrap(),
               sw_cli::setup_cmd::render_user_unit("%h/.local/bin/serialwrapd"));
}

#[test]
fn reconcile_aborts_when_old_daemon_never_exits() {
    let t = TestEnv::posix().with_systemd(true).with_daemon_alive_forever(TestEnv::default_socket(), 4321);
    let (rc, out, _) = t.run(&["setup", "--user"]);
    assert_eq!(rc, 1);                                   // P1 SC-12 凍結 exit 1
    let j = json(&out);
    let r = if j.contains_key("error_code") { j.clone() } else { dict(&j, "setup") };   // 外層形狀以 P1 oracle 為準（A/B 另驗）
    assert_eq!((s(&r, "error_code"), s(&r, "phase")), ("TIMEOUT", "reconcile_wait_old_exit"));
    assert!(!t.calls().iter().any(|c| c == &["systemctl", "--user", "start", "serialwrap"]));
    assert!(!t.config_path().exists() || !std::fs::read_to_string(t.config_path()).unwrap().contains("systemd-user"));
}

#[test]
fn wsl_without_sudo_never_runs_sudo_install() {
    let t = TestEnv::posix().with_wsl(true).with_systemd(false);
    let (rc, out, _) = t.run(&["setup", "--on-demand"]);
    assert_eq!(rc, 0);
    assert!(!t.calls().iter().any(|c| c.first().map(String::as_str) == Some("sudo")));
    assert!(!json(&out).contains_key("needs_restart"));
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `materialize_profiles_not_overwritten_without_force` | 既有 `profiles/default.yaml` 內容 `x` 保留；`--force` 後等於資產位元組 |
| `materialize_skill_always_refreshed_and_linked` | `data/skill/SKILL.md` sha256 == 資產；`~/.agents/skills/serialwrap` 為 symlink 指向 `data/skill` |
| `materialize_removes_legacy_mcp_link_only_when_matching` | 指向 `.../serialwrap-mcp` 的 symlink 被移除；指向他處者保留 |
| `materialize_wrappers_posix_only` | `TestEnv::posix()`：三個 wrapper 0755 且位元組等於資產；`TestEnv::windows()`：`bin` 不建立 |
| `assets_embedded_sha256_match_repo` | `PROFILE_ASSETS`／`SKILL_ASSETS`／`TOOL_ASSETS` 每個 sha256 等於 `sw_core/assets/<rel>` 檔案 |
| `legacy_detect_shape` | 建 `~/.paul_tools/serialwrap` → dict 鍵 `hint, minicom_symlink, path, serialwrap, state_json` 與 hint 逐字 |
| `wslconf_merge_matches_configparser_golden` | golden 12 案例（空、已有 `[boot]`、壞格式、含註解、大小寫鍵、`key: value`）逐字相同 |
| `wsl_systemd_target_with_sudo_installs` | `--user --with-sudo`、WSL 無 systemd → calls 含 `["sudo","install","-m","0644",<staging>,"/etc/wsl.conf"]`、輸出 `needs_restart:true` |
| `flashing_busy_before_materialize` | mcu.status `flashing:true` → `FLASHING_BUSY`、exit 2、`profiles` 目錄不存在 |
| `same_mode_systemd_user_enables_without_start` | old=target=systemd-user → ran 不含 `start` |
| `system_without_sudo_pending_only` | `--system` 無 sudo → `applied:false`、`mode` 為舊值、`pending_sudo` 6 條（含 start）、config 未寫 |
| `system_with_sudo_unit_content` | staging unit == `render_system_unit("<InstallRoot.bin>/serialwrapd --socket /run/serialwrap/serialwrapd.sock --profile-dir /etc/serialwrap/profiles", <run_user>)`；含 `Environment=SERIALWRAP_STATE_DIR=/var/lib/serialwrap`；PATH 上另放一個 `serialwrapd` 不影響 ExecStart |
| `out_of_system_without_sudo_blocks` | old systemd-system → target user、無 sudo → 不停舊、不起新、pending 含 stop/disable |
| `df10_system_state_migration_target` | old user → system（with sudo）→ `migrate_legacy_state` 目標為 `/var/lib/serialwrap/state.json`（隔離根下）、unit 含對應 `Environment=` 行 |
| `preserve_remote_tcp_socket_on_demand` | config `socket_path: tcp://127.0.0.1:49001`、`--on-demand` → 寫回值不變 |
| `windows_default_target_is_windows_task` | `TestEnv::windows()` 無旗標 → ran 依序 `["schtasks","/Create","/XML",<xml>,"/TN","serialwrap","/F"]`、`["schtasks","/Run","/TN","serialwrap"]`；XML 的 `<Command>` 為 `<install_root>\bin\serialwrapd.exe`、`<Arguments>` 為 `--socket tcp://127.0.0.1:48700` |
| `windows_task_to_on_demand_graceful_stop_order` | `event_order`：`/Change … /DISABLE` < `rpc:daemon.stop` < `rpc:health.ping#gone`；daemon 正常結束時 `ran` 不含 `/End` |
| `windows_task_to_on_demand_end_only_after_timeout` | daemon 不結束 → `/End` 在 10 s 等待之後；再 5 s 仍不結束 → `TIMEOUT`、`phase:"reconcile_wait_old_exit"`、config 未寫 |
| `windows_service_target_pending_only` | `--windows-service` → `applied:false`、pending 為 `install_plan` 三條、warnings 逐字 |
| `user_flag_on_windows_invalid_args` | `TestEnv::windows()` `setup --user` → `INVALID_ARGS`、message `監管模式 systemd-user 不適用於本平台（可用：on-demand、windows-task、windows-service）` |
| `supervision_set_switches_and_outputs_previous` | `supervision-mode set systemd-user` → `previous_mode:"on-demand"`、`setup.mode:"systemd-user"`、未物化資產 |
| `supervision_set_bad_words` | `supervision-mode set` → `INVALID_ARGS` `用法：serialwrap supervision-mode [set MODE]` |
| `supervision_set_flashing_message` | message `flash 進行中，拒絕切換監管模式（可用 --force 覆寫）` |
| `test_setup_ab_on_demand_isolated`（黑箱 A/B） | 隔離 HOME、stub PATH、`setup --on-demand` → Python／Rust stdout 相同（路徑以 tmp 正規化）；物化結果樹相同 |
| `test_setup_ab_user_with_stub_systemctl`（黑箱 A/B） | stub `systemctl`／`loginctl` → 兩實作 `ran` 與 unit 檔相同 |
| `test_setup_ab_reconcile_timeout`（黑箱 A/B，`process_rpc`） | 以會忽略 `daemon.stop` 的 stub daemon（P3 `stubd` 腳本：`daemon.stop` 回 ok 但持續回 ping）讓舊 daemon 不結束 → 兩實作 stdout（正規化路徑）與 exit 1 相同 |
| `test_supervision_set_rust_only`（黑箱，`rust_only`） | 同 Rust 表列 |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_setup` → `not implemented: P4-T10`；黑箱 Rust → 97。
- [ ] **步驟 3：實作**（reconcile 主幹）

```rust
pub fn reconcile(ctx: &mut Ctx, req: &ReconcileRequest) -> Result<PyDict, ReconcileError> {
    if req.any_flashing && !req.force { return Err(ReconcileError::FlashingBusy("flash 進行中，拒絕切換監管模式（可用 force 覆寫）".into())); }
    let mut rec = Recorder::new(ctx);                     // 包住 SysEnv::run 與 ServiceCtl，累積 ran
    let mut pending: Vec<Vec<String>> = vec![];
    if let Some(early) = guard_privileged(ctx, req, &mut pending) { return Ok(early); }   // systemd-system / windows-service 無授權
    if req.old_mode == req.target_mode { same_mode_refresh(&mut rec, req, &mut pending)?; return Ok(done(req, false, rec.ran(), pending)); }
    stop_old(&mut rec, req, &mut pending)?;
    if needs_wait(req) && !wait_daemon_gone(ctx, &req.old_endpoint, &req.daemon_id, Duration::from_secs(10)) {
        return Err(ReconcileError::WaitOldExitTimeout { partial: partial(req, rec.ran(), pending) });   // TIMEOUT＋phase reconcile_wait_old_exit
    }
    migrate_state(ctx, req);                               // DF-10，例外吞掉（KEEP）
    start_new(&mut rec, req, &mut pending)?;
    let mut cfg = endpoint_resolve::load_runtime_config(ctx).map_err(|e| ReconcileError::Config(e.to_string()))?;
    cfg.set_mode(req.target_mode.as_str(), req.socket_path.as_deref());
    endpoint_resolve::save_runtime_config(ctx, &cfg).map_err(|e| ReconcileError::Config(e.to_string()))?;   // 寫 config_write_path（SC-09）
    Ok(done(req, true, rec.ran(), pending))
}
```

`merge_wsl_conf_systemd`：移植 `configparser.ConfigParser`（`optionxform=str`、`DEFAULTSECT`、`=`／`:` 分隔、`;`／`#` 整行註解捨棄、續行、重複 section／option → 解析失敗 → 空 parser），輸出 `[sec]\nkey = value\n` 每 section 後空一行。`contracts/behavior_changes.yaml`：SC-22、SC-24、SC-12、DF-10～DF-13、DF-16 `status: IMPLEMENTED`（`case_ids` 不改），`p4_case_map.yaml` 綁定 `sc22-supervision-mode-set`、`sc24-windows-task-service`、`sc12-reconcile-wait-old-daemon`、`df10-system-scope-state-path`、`df11-wsl-systemd-sudo-gate`、`df12-setup-ran-field`、`df13-minicom-broker-rename`、`df16-windows-skill-copy` 到本 Task 測試。

- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-cli --test mgmt_setup
scripts/rust/win-test.sh --package sw-cli -- mgmt_setup
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_setup.py
python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_setup.py
```

- [ ] **步驟 5：commit**：`feat(rust-cli): 原生承接 setup 與 supervision-mode set（含 windows-task）`

**BLOCKED-OWNER：**
- 真實 `systemd-user`／`systemd-system` reconcile（本機正式 daemon 由 systemd 監管，實跑會替換正式服務；需擁有者排定維護窗口）。
- 真實 WSL `/etc/wsl.conf` 寫入與 `wsl --shutdown` 流程（主機層變更）。
- `windows-task` 端到端 setup 在測試團隊工作站與 OD-05 機台（註冊、登入自啟、on-demand→windows-task 交接時實體 COM 釋放）。

---

## Task 11：安裝佈局、release manifest 驗證與版本配對 gate（§9.5、§12.1、SC-25）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-install-layout.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/{install_layout.rs,pairing.rs}`、`crates/sw-proto/src/release_manifest.rs`（新增 optional 欄位）、`contracts/release_manifest.schema.json`、`scripts/rust/mkbundle.sh`（寫入 `state_schema`、`task_xml_template_sha256`、`SHA256SUMS`）
- Create: `crates/sw-cli/tests/mgmt_layout.rs`、`tests/test_rust_release_manifest_schema.py`

**Interfaces:**
- Consumes：`FsOps::{copy_preserving, copy_tree, remove_tree, replace, symlink_file, atomic_write, sync_dir, file_lock_exclusive}`（Task 5）、`SysEnv::local_appdata`（Task 2）、P3 `SysEnv::{current_exe, realpath, var}`、P3 `parse_manifest`／`parse_current_release`、`sw_proto::digest`（sha2 包裝）、`Clock`、P1 `contracts/golden/tree_sha256.ndjson`。
- Produces：

```rust
pub enum EntryStyle { Symlink, Copy }
pub struct InstallRoot { pub root: PathBuf, pub versions: PathBuf, pub bin: PathBuf, pub backups: PathBuf, pub supervisor: PathBuf,
                         pub current_file: PathBuf, pub entry_style: EntryStyle, pub entry_names: Vec<String> }
pub fn resolve_install_root(ctx: &Ctx) -> InstallRoot;
// root：SERIALWRAP_INSTALL_ROOT（strip 非空，expanduser）> PathFlavor::Windows：%LOCALAPPDATA%\Programs\serialwrap > POSIX：DATA_DIR
// bin：Windows <root>\bin；POSIX ~/.local/bin（SERIALWRAP_INSTALL_ROOT 設定時改 <root>/bin，供隔離測試）
// current_file：Windows <root>\bin\current-release.json；POSIX <root>/current-release.json
// entry_names：Windows ["serialwrap.exe","serialwrapd.exe"]；POSIX ["serialwrap","serialwrapd"]
pub struct ReleaseRef { pub bundle_version: String, pub dir: String }
pub struct CurrentRelease { pub bundle_version: String, pub dir: String, pub previous: Option<ReleaseRef>, pub backup_dir: Option<String>, pub switched_at: String }
pub fn read_current(root: &InstallRoot) -> Result<Option<CurrentRelease>, LayoutError>;
pub fn write_current(ctx: &Ctx, root: &InstallRoot, cur: &CurrentRelease) -> Result<(), LayoutError>;   // DISK_STABLE 的 Indent(2)＋sort_keys＋尾端換行，atomic_write
pub enum ExeLocation { InstalledVersion(String), StableEntry, External(PathBuf) }
pub fn classify_exe(ctx: &Ctx, root: &InstallRoot) -> ExeLocation;
pub fn verify_bundle(ctx: &Ctx, dir: &Path, m: &ReleaseManifest) -> Result<(), Vec<PairingDiff>>;   // 所有 components.*.sha256 與 tree_sha256、SHA256SUMS
pub fn stage_version(ctx: &Ctx, root: &InstallRoot, bundle_dir: &Path, m: &ReleaseManifest) -> Result<PathBuf, LayoutError>;  // 複製到 versions/<v>.staging → verify → rename 成 versions/<v>
pub struct SwitchReport { pub replaced: Vec<(String, Option<String>)>, pub old_files: Vec<PathBuf> }
pub fn switch_entries(ctx: &Ctx, root: &InstallRoot, version_dir: &Path) -> Result<SwitchReport, LayoutError>;
pub fn revert_entries(ctx: &Ctx, root: &InstallRoot, report: &SwitchReport) -> Result<(), LayoutError>;
pub struct PairingDiff { pub field: String, pub expected: String, pub actual: String }
pub fn check_pairing(m: &ReleaseManifest, cli_version: &str, daemon_version: Option<&str>, digests: &[(String, String)]) -> Vec<PairingDiff>;
pub fn tree_sha256(ctx: &Ctx, py_root: &Path) -> std::io::Result<String>;   // 逐字照 P1 T18 `tree_sha256`：遞迴、排除 `__pycache__` 目錄與 `*.pyc`、relpath 以 `/`、依 relpath 位元組序、sha256("".join(f"{rel}\0{sha256}\n"))
pub fn locate_manifest(ctx: &Ctx) -> Result<(PathBuf, ReleaseManifest), LayoutError>;   // P1 `find_release_manifest` 同序：SERIALWRAP_RELEASE_MANIFEST（strip 非空且存在）> realpath(current_exe) 的上兩層 release-manifest.json > Windows 由 current-release.json 的 dir
pub fn pairing_item(m: &ReleaseManifest, manifest_dir: &Path, cli_version: &str, daemon_version: Option<&str>) -> PyDict;
// 逐字照 P1 `check_version_pairing`：{"check":"version_pairing","ok","advisory":false,"detail":"ok"|", ".join(mismatches),"mismatches":[…]}
pub fn install_lock(ctx: &Ctx, root: &InstallRoot) -> Result<FileLockGuard, PyDict>;
// <root>/.install.lock；每 0.2 s 非阻塞嘗試，5 s 內取不到 → {"ok":false,"error_code":"TIMEOUT","phase":"install_lock","message":"另一個 setup／更新／回滾正在進行（<root>/.install.lock）"}
```

行為：
- `stage_version`：`versions/<v>` 已存在且 `verify_bundle` 通過 → 直接回傳（冪等）；存在但驗證失敗 → `LayoutError::ExistingCorrupt`（不覆寫，回報）；否則複製到 `versions/<v>.staging`、驗證、`replace` 成 `versions/<v>`、`sync_dir(versions)`。
- `switch_entries`：
  - `EntryStyle::Copy`（Windows）：對每個 entry：`copy_preserving(<vdir>/bin/<n>, <bin>/<n>.new)` → 若 `<bin>/<n>` 存在：`replace(<bin>/<n>, <bin>/<n>.old.<UTC %Y%m%d-%H%M%S>)`（執行中 exe 允許 rename）→ `replace(<n>.new, <n>)`。任一步失敗：已改名者依 `report` 反向還原、刪 `.new`，回錯。全部成功後才由呼叫端 `write_current`。
  - `EntryStyle::Symlink`（POSIX）：`symlink_file(<bin>/<n>.new, <vdir>/bin/<n>)` → 記錄舊目標（`read_link` 或「一般檔」則先 `replace` 成 `.old.<ts>` 保存）→ `replace(<n>.new, <n>)`（rename 覆蓋 symlink 為原子操作）。
  - 啟動時清理：`<bin>/*.old.*` 中非本次產生者嘗試刪除，失敗忽略（Windows 執行中 exe 刪不掉屬正常）。
- `check_pairing`（update／rollback 內部用，回結構化 diff）：`manifest.pairing.rule=="exact"`；`pairing.cli_version == cli_version == bundle_version`；`daemon_version`（Some 時）`== pairing.daemon_version`；`digests` 每個 `(component_path, actual_sha256)` 與 manifest 宣告相同。`field` 名稱：`cli_version`、`daemon_version`、`bundle_version`、`sha256:<path>`、`tree_sha256:<component>`、`stable_entry:<name>`。
- `pairing_item`（doctor 用，雙邊 SC-25）：逐字照 P1 L2422-2437 的判定與 `mismatches` 順序（`cli_version`、`daemon_version`、`daemon_unreachable`、`cli_sha256`、`launcher_sha256`、`runtime_sha256`、`package_tree_sha256`）；daemon 不可達 → `ok:false`＋`daemon_unreachable`；manifest 欄位路徑 `components.cli.sha256`、`components.daemon.{launcher,runtime}.sha256`、`components.daemon.package.tree_sha256`。
- `install_lock`：整筆 update／rollback／首次安裝持有；持鎖後才 `read_current`，避免兩個 updater 交錯操作 `.new`、備份、入口與 `current-release.json`。
- manifest 位置：`locate_manifest`（上列順序；`SERIALWRAP_RELEASE_MANIFEST` 與 P1 共用，測試隔離時由 conftest pop）。`current-release.json` 新增 `previous`、`backup_dir`、`switched_at` 欄位；P3 的讀取端必須忽略未知鍵（本 Task 加回歸測試）。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-cli/tests/mgmt_layout.rs`）

```rust
mod support; use support::*;
use sw_cli::install_layout::*;
#[test]
fn windows_copy_switch_keeps_old_and_writes_new() {
    let t = TestEnv::windows().with_install_root();
    let root = resolve_install_root(&t.ctx());
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    let v = t.make_version_dir(&root, "0.4.0", &[("bin/serialwrap.exe", b"new-cli"), ("bin/serialwrapd.exe", b"new-d")]);
    let rep = switch_entries(&t.ctx(), &root, &v).unwrap();
    assert_eq!(std::fs::read(root.bin.join("serialwrap.exe")).unwrap(), b"new-cli");
    let old = rep.old_files.iter().find(|p| p.file_name().unwrap().to_string_lossy().starts_with("serialwrap.exe.old.")).unwrap();
    assert_eq!(std::fs::read(old).unwrap(), b"old-cli");
    assert!(!root.bin.join("serialwrap.exe.new").exists());
}
#[test]
fn switch_failure_mid_way_restores_previous_entries() {
    let t = TestEnv::windows().with_install_root().with_fs_fault_replace_suffix("serialwrapd.exe.new");
    let root = resolve_install_root(&t.ctx());
    t.write_file(&root.bin.join("serialwrap.exe"), b"old-cli");
    t.write_file(&root.bin.join("serialwrapd.exe"), b"old-d");
    let v = t.make_version_dir(&root, "0.4.0", &[("bin/serialwrap.exe", b"new-cli"), ("bin/serialwrapd.exe", b"new-d")]);
    assert!(switch_entries(&t.ctx(), &root, &v).is_err());
    assert_eq!(std::fs::read(root.bin.join("serialwrap.exe")).unwrap(), b"old-cli");
    assert_eq!(std::fs::read(root.bin.join("serialwrapd.exe")).unwrap(), b"old-d");
}
#[test]
fn pairing_reports_daemon_version_mismatch() {
    let m = manifest_fixture("0.4.0");
    let d = check_pairing(&m, "0.4.0", Some("0.3.2"), &[]);
    assert_eq!(d.len(), 1);
    assert_eq!((d[0].field.as_str(), d[0].expected.as_str(), d[0].actual.as_str()), ("daemon_version", "0.4.0", "0.3.2"));
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `install_root_env_override` | `SERIALWRAP_INSTALL_ROOT=/x` → root `/x`、bin `/x/bin` |
| `install_root_windows_default` | `local_appdata=C:\U\a\AppData\Local` → root `…\Programs\serialwrap`、current_file `…\bin\current-release.json` |
| `install_root_posix_default_is_data_dir` | 未設 env → root == `RuntimePaths.data_dir`、bin `~/.local/bin` |
| `posix_symlink_switch_atomic` | 切換後 `~/.local/bin/serialwrap` 為 symlink 指向 `versions/0.4.0/bin/serialwrap`；原本一般檔被保存成 `.old.<ts>` |
| `stage_version_idempotent` | 第二次 stage 相同 bundle 不重複複製（mtime 不變） |
| `stage_version_existing_corrupt_refused` | `versions/0.4.0` 內檔案被改 → `ExistingCorrupt`，不覆寫 |
| `verify_bundle_detects_tree_tamper` | 改動 `py/sw_core/cli.py` 一個 byte → diff `tree_sha256:daemon` |
| `tree_sha256_matches_python_reference` | 與 P1 `contracts/golden/tree_sha256.ndjson` 逐筆相同（含 `__pycache__`／`*.pyc` 排除案例） |
| `pairing_item_matches_p1_shape` | daemon 不可達 → `{"advisory":false,"check":"version_pairing","detail":"daemon_unreachable","mismatches":["daemon_unreachable"],"ok":false}`；全相符 → `detail:"ok"`、`mismatches:[]` |
| `install_lock_second_updater_times_out_without_changes` | 子行程持有 `.install.lock` 10 s → 本行程 `install_lock` 於 5 s（FakeClock）後回 `TIMEOUT`／`phase:"install_lock"`；install root 樹（含 mtime）不變 |
| `locate_manifest_env_override_first` | `SERIALWRAP_RELEASE_MANIFEST` 指向存在檔 → 用它；指向不存在檔 → 退回 realpath 規則 |
| `current_release_roundtrip_and_unknown_keys` | 寫入後讀回相等；含未知鍵 `x:1` 的檔案仍可讀（P3 `delegate`／manifest locate 亦通過） |
| `classify_exe_three_cases` | exe 在 `versions/0.4.0/bin` → `InstalledVersion("0.4.0")`；在 `bin` → `StableEntry`；在 tmp bundle → `External(dir)` |
| `pairing_exact_all_ok` | 全相符 → 空 |
| `pairing_daemon_absent_skips_daemon_field` | `daemon_version=None` → 無 `daemon_version` diff |
| `manifest_schema_accepts_new_optional_fields`（pytest） | `jsonschema` 以 `contracts/release_manifest.schema.json` 驗 `mkbundle.sh --dev` 產物通過；去掉 `state_schema` 仍通過 |
| `windows_switch_running_exe_rename`（原生 Windows） | 以子行程執行 `bin\serialwrap.exe` 期間切換 → 成功，舊檔以 `.old.<ts>` 存在 |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_layout` → `not implemented: P4-T11`；pytest 新欄位 schema 測試失敗。
- [ ] **步驟 3：實作**：依「行為」；`mkbundle.sh`（Task 17 已改為自含 bundle）對 `components.daemon.state_schema` 寫 `"python-state-v0"`（Stage 1 Python daemon 的 `state.json` 無版本欄位，以此常數表示 04 §2.2 schema），`components.supervisor.task_xml_template_sha256` 寫 `render_task_xml` 模板（未代入）sha256。SC-25 `status: IMPLEMENTED`（`case_ids` 不改；`sc25-release-manifest-version-pairing` 的 doctor 面測試於 Task 13 補入 `p4_case_map.yaml`）。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-cli --test mgmt_layout
scripts/rust/win-test.sh --package sw-cli -- mgmt_layout
python3 -m pytest -q tests/test_rust_release_manifest_schema.py
```

- [ ] **步驟 5：commit**：`feat(rust-cli): 實作版本目錄、穩定入口切換與版本配對檢查`

**BLOCKED-OWNER：**
- 正式 Windows release artifact 必須在擁有者 Windows 自架 runner 原生 build/link（arch §1.1），並以該 artifact 驗證安裝佈局與 `windows_switch_running_exe_rename`。
- EDR 開啟下 `bin\*.old.*` 改名與新 exe 首次執行不被攔截的證據（§1.2 端點防毒列）。

---

## Task 12：§12.1 首次安裝、更新流程與 `setup --rollback`（SC-23）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/<issue>-rust-p4-update-rollback.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/rollback.rs`、`crates/sw-cli/src/setup_cmd.rs`（`run_setup` 分流：`--rollback`；`ExeLocation::External` 且含 manifest：無 Current → 首次安裝、有 Current → update；任何分支開始前先處理殘留 `install-journal.json`）
- Create: `crates/sw-cli/tests/mgmt_update.rs`、`tests/contract_blackbox/test_cli_mgmt_update.py`

**Interfaces:**
- Consumes：Task 10 `reconcile` 的 `stop_old`／`start_new`、`materialize_assets`、`resolve_target_mode`（改為 `pub(crate)`）、Task 11 全部（含 `install_lock`、`locate_manifest`）、`capture_daemon_identity`／`wait_daemon_gone`／`graceful_stop_with`＋`StopRpc::Handoff`（Task 7）、`ServiceCtl::{set_enabled, query, install, uninstall}`（Task 3）、`ProcScan::serialwrapd_tty_holders`（Task 4）；daemon 端原子交接 RPC `daemon.handoff_stop`（SC-11 交接停止：Python 由 P1 T08 交付、Rust daemon 由 P8 Task 14 交付；不支援的 daemon 回 `METHOD_NOT_FOUND`，本 Task 據此 fail-closed 拒絕）。
- Produces：

```rust
pub enum StepName { Lock, Verify, SafetyCheck, StopSupervisor, ConfirmReleased, BackupAndSwitch, StartAndVerify, RevertToPrevious }
impl StepName { pub fn as_str(&self) -> &'static str; }   // "lock","verify","safety_check","stop_supervisor","confirm_released","backup_and_switch","start_and_verify","revert_to_previous"
pub struct StepRecord { pub step: StepName, pub ok: bool, pub detail: PyDict }
pub struct UpdateReport { pub ok: bool, pub op: &'static str /* "install"|"update"|"rollback" */, pub from_version: Option<String>, pub to_version: String,
                          pub steps: Vec<StepRecord>, pub backup_dir: Option<PathBuf>, pub reverted: bool, pub error_code: Option<&'static str>,
                          pub phase: Option<&'static str>, pub reason: Option<String>, pub message: Option<String>,
                          pub revert_pending: bool /* R2-F6：回退因新版交接被拒／無法確認而暫緩，journal 保留 */,
                          pub revert_error: Option<PyDict> /* {error_code, phase:"update_revert", handoff|handoff_outcome, busy|probe|pids} */ }
pub struct SupervisorSnapshot { pub mode: SupervisionMode, pub endpoint: String, pub enabled: Option<bool>, pub running: bool,
                                pub registration: Option<PathBuf> /* 匯出的 Task XML／unit 檔複本 */ }
pub struct BackupSet { pub dir: PathBuf, pub supervisor: SupervisorSnapshot }
pub fn backup_state(ctx: &Ctx, root: &InstallRoot, from: Option<&str>, to: &str, sup: SupervisorSnapshot) -> Result<BackupSet, LayoutError>;
// 內容：config.yaml（config_write_path）、state.json、benches.state.json、profiles/（遞迴）、監管定義檔（user unit／system unit staging／Task XML）、
//       已註冊監管狀態（mode、endpoint、enabled、running；windows-task 以 `schtasks /Query /TN serialwrap /XML` 匯出實際註冊內容）、
//       穩定入口（entries.json：symlink 目標或複本）
// 位置：<root>/backups/<from or "none">-to-<to>-<UTC %Y%m%d-%H%M%S>/，另寫 manifest.json 列出每個檔的原始路徑與 sha256
pub fn restore_backup(ctx: &mut Ctx, set: &BackupSet, include_state: bool) -> Result<Vec<PathBuf>, LayoutError>;
// 順序：config.yaml → profiles → 監管定義檔 → 重新註冊（windows-task `schtasks /Create /XML <備份 XML> /TN serialwrap /F`；systemd 寫回 unit＋daemon-reload＋依 enabled 執行 enable/disable；
//       備份時無註冊而目前有 → uninstall）→（include_state 時）state.json／benches.state.json
pub struct DaemonSnapshot { pub id: DaemonIdentity, pub attached_paths: Vec<PathBuf>, pub sessions: Vec<PyDict> /* com、state、released */ }
pub fn safety_check(ctx: &Ctx, endpoint: &str) -> Result<Option<DaemonSnapshot>, (&'static str, PyDict)>;   // Ok(None)＝確認沒有 daemon；只是「提早拒絕」的預檢，不是交接授權
// R1-F1：§12.1 步驟 2→3 的交接授權只能由 daemon 在共同 admission 邊界內原子取得（busy 判定＋凍結所有新操作＋發 token 為同一臨界區），
// CLI 端 session.list 預檢與 daemon.stop 之間的窗口無法由 CLI 關閉，故步驟 3 不送一般 daemon.stop。
pub struct HandoffGrant { pub token: String /* 32 個小寫 hex */ }
pub enum HandoffError {
    Busy { code: &'static str /* "FLASHING_BUSY"|"SESSION_BUSY" */, busy: PyList },    // daemon 原子判定為忙碌：未凍結、未停止
    Unconfirmed { code: String, probe: &'static str /* "daemon.handoff_stop" */, outcome: &'static str /* "refused"|"unknown" */ },
}
pub fn request_handoff_stop(ctx: &Ctx, endpoint: &str, reason: &str /* "update"|"rollback"|"revert" */) -> Result<HandoffGrant, HandoffError>;
// reason 為 01 SC-11-handoff 的自由字串參數（`str(params.get("reason") or "update")`，未限定值、busy 判定與凍結語意不因 reason 而異）；"revert" 由步驟 7 與 recover_journal 使用（R2-F6）
// RPC daemon.handoff_stop {"reason":…}（2.0 s、不重試、不觸發 SC-18）：
//   {"ok":true,"stopping":true,"handoff_token":<32 hex>} → Ok；
//   {"ok":false,"error_code":"FLASHING_BUSY"|"SESSION_BUSY","busy":[…]} → Busy；
//   其他 ok:false（含舊 daemon 的 METHOD_NOT_FOUND）／ok:true 但缺 stopping 或 token 格式不符 → Unconfirmed{outcome:"refused"}（INVALID_RESPONSE 或原碼）；
//   connect 失敗 → Unconfirmed{outcome:"refused"}；send／recv 失敗或逾時 → Unconfirmed{outcome:"unknown"}（daemon 可能已凍結並停止，不得硬停）
// R1-F5：journal 必須自帶「回到完整 Previous」所需的全部原始資料，不得依賴行程記憶體或尚未完成的備份。
// journal 的 step 為**內部持久化進度**（比 StepName 細），與輸出報告的 steps[*].step 分開。
pub enum JournalStep { Lock, Verify, SafetyCheck, SnapshotTaken, StopSupervisor, ConfirmReleased,
                       BackupStarted, BackupComplete, EntriesSwitching, StartAndVerify, RevertToPrevious }
// as_str："lock","verify","safety_check","snapshot_taken","stop_supervisor","confirm_released",
//         "backup_started","backup_complete","entries_switching","start_and_verify","revert_to_previous"
pub enum EntrySnapshot { Symlink { name: String, target: String }, Copy { name: String, sha256: String, size: u64 }, Absent { name: String } }
pub struct OriginalSnapshot {
    pub current: Option<CurrentRelease>,          // 切換前 current-release.json 全文（None＝首次安裝）
    pub entries: Vec<EntrySnapshot>,              // 切換前每個穩定入口（symlink 目標或複本 sha256）
    pub config_sha256: Option<String>,            // 切換前 config.yaml 的 sha256（複本在 snapshot_dir/config.yaml）
    pub supervisor: SupervisorSnapshot,           // query() 的 mode、endpoint、enabled、running；registration 指向 snapshot_dir 內的匯出複本
    pub daemon: Option<DaemonSnapshot>,           // safety_check 的結果；Some＝停止前 daemon 健康在跑（還原時須重新啟動）
    pub daemon_start: &'static str,               // "supervised"｜"on_demand_internal"｜"none"：還原 running 時使用的啟動路徑
}
pub struct Journal { pub op: String, pub from: Option<String>, pub to: String, pub step: JournalStep, pub started_at: String,
                     pub original: Option<OriginalSnapshot>,   // step >= SnapshotTaken 時必為 Some
                     pub snapshot_dir: Option<String>,         // <root>/install-journal.snapshot/：registration 匯出、config.yaml 複本、entries.json
                     pub snapshot_sha256: Option<Vec<(String, String)>>, // snapshot_dir 內每檔 sha256（recover 時核對）
                     pub backup_dir: Option<String>,           // step >= BackupStarted 時為 Some（可能為 *.partial）
                     pub backup_manifest_sha256: Option<String> } // 只在 step >= BackupComplete 時為 Some
pub fn write_journal(ctx: &Ctx, root: &InstallRoot, j: &Journal) -> Result<(), LayoutError>;
// <root>/install-journal.json：atomic_write（含檔案 fsync）＋sync_dir(root)；DISK_STABLE 序列化。寫入失敗 → 中止本步、不得進行該步的任何副作用。
pub fn persist_original_snapshot(ctx: &Ctx, root: &InstallRoot, sup: &SupervisorSnapshot, daemon: &Option<DaemonSnapshot>) -> Result<(OriginalSnapshot, PathBuf), LayoutError>;
// 在**任何**停用／停止之前執行：匯出已註冊監管內容、複製 config.yaml、記錄穩定入口與 current-release.json 到 snapshot_dir，每檔 fsync＋sync_dir
pub struct RecoveryReport { pub ok: bool, pub from_step: &'static str, pub backup_used: bool, pub journal_kept: bool,
                            pub restored: PyDict /* current、entries、config、supervisor{enabled,running}、daemon_version */,
                            pub error_code: Option<&'static str>, pub reason: Option<String>, pub message: Option<String>,
                            pub detail: PyDict /* R2-F6：交接被拒時的 handoff／busy 或 probe／handoff_outcome／pids；其他情況為空 */ }
pub fn recover_journal(ctx: &mut Ctx, root: &InstallRoot) -> Option<RecoveryReport>;
pub enum RevertStop { Absent, Stopped { token: String, graceful: bool }, Deferred(HandoffError) }   // R2-F6
pub fn stop_for_revert(ctx: &mut Ctx, mode: &SupervisionMode, endpoint: &str, with_sudo: bool, phase: &'static str) -> Result<RevertStop, StopFailure>;
// 步驟 7 與 recover_journal 復原變體共用、唯一的停止入口：確認不存在 → Absent（零 RPC 停止）；否則只經 graceful_stop_with(StopRpc::Handoff{reason:"revert"})；
// Busy／Unconfirmed → Deferred（未硬停、enabled 已還原為交接前值、journal 未改寫）；交接 ok 後等待逾時才 hard_stop。內部不得呼叫 graceful_stop（Plain）或送 daemon.stop。
// 行程在但 RPC 不通（connect 失敗）→ Deferred(Unconfirmed{code:"DEVICE_STILL_HELD", outcome:"refused"})，detail 帶 pids。
// 無 journal → None。ok:true 才刪 journal 與 snapshot_dir；ok:false → journal 與 snapshot_dir 原樣保留（位元組不變），呼叫端以 rc 2 結束、不進行本次 op。
pub fn run_install(ctx: &mut Ctx, bundle_dir: &Path, ns: &Namespace) -> UpdateReport;
// force_requested：使用者是否帶 `--force`。update／rollback 路徑**只用於回報**（safety_check 步驟 detail 記 `force_ignored:true`），
// 不得傳入 safety_check、stop_supervisor 的 daemon.handoff_stop 交接或任何交接判定（§12.1 步驟 2：不符合交接條件時拒絕更新，無覆寫）。
pub fn run_update(ctx: &mut Ctx, bundle_dir: &Path, target_mode: &SupervisionMode, with_sudo: bool, force_requested: bool) -> UpdateReport;
pub fn run_rollback(ctx: &mut Ctx, with_sudo: bool, force_requested: bool) -> UpdateReport;
```

錯誤碼（**全部為 §9.2 既有碼**，以 `phase`／`reason` 區分，見 §0.3）：

| 情境 | `error_code` | `phase` | `reason`／附加欄位 |
|---|---|---|---|
| 另一個安裝程序持鎖 | `TIMEOUT` | `install_lock` | — |
| bundle manifest 缺、壞、schema 不符 | `INVALID_ARGS` | `update_verify` | `manifest_invalid`、`message` |
| 檔案 sha256／tree 不符 | `CHECKSUM_VERIFY_FAILED` | `update_verify`／`rollback_verify` | `diffs:[{field,expected,actual}]` |
| 新舊 `state_schema` 不同且無核准遷移 | `INVALID_ARGS` | `update_verify`／`rollback_verify` | `state_schema_incompatible`、`current`、`target` |
| 沒有可回滾的 Previous | `INVALID_ARGS` | `rollback_verify` | `rollback_unavailable`；message `沒有可回滾的 Previous 版本（<原因>）` |
| flash 進行中 | `FLASHING_BUSY` | `update_safety_check` | message `flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）` |
| 前景／背景命令、lease、recovering | `SESSION_BUSY` | `update_safety_check` | `busy:[…]`（與 P8 SC-23-G 相同碼） |
| 安全探測 RPC 失敗／逾時／格式異常 | 該 RPC 的碼（`TIMEOUT`、`SOCKET_ERROR`、`INVALID_RESPONSE`…） | `update_safety_check` | `probe:<method>` |
| 步驟 3 daemon 原子交接判定忙碌（預檢後才進入的 `file.push`、flash 等） | daemon 回的 `FLASHING_BUSY`／`SESSION_BUSY` | `update_safety_check` | `busy:[…]`（daemon 回傳，每筆多 `ops`）、`handoff:"refused"`；監管已還原 enabled、未送 `daemon.stop`、未硬停 |
| 步驟 3 交接無法確認（daemon 不支援回 `METHOD_NOT_FOUND`、回應格式不符、連線／逾時） | 原碼或 `INVALID_RESPONSE`／`TIMEOUT`／`SOCKET_ERROR` | `update_safety_check` | `probe:"daemon.handoff_stop"`、`handoff_outcome:"refused"|"unknown"`；監管已還原 enabled、未硬停、不切換 |
| ping 不通但仍有 daemon 行程，或身分無法確認 | `DEVICE_STILL_HELD` | `update_safety_check` | `pids`、`holder_unknown` |
| 舊 daemon 10 s 內未結束 | `TIMEOUT` | `update_confirm_released` | `pids` |
| tty 仍被持有 | `DEVICE_STILL_HELD` | `update_confirm_released` | `pids`、`paths` |
| 新版啟動失敗 | `DAEMON_EXITED`／`DAEMON_NOT_READY`／`TIMEOUT` | `update_start_and_verify` | `reverted`、`revert_error` |
| 新版驗收失敗 | `INTERNAL_ERROR` | `update_start_and_verify` | `pairing_mismatch`／`health_failed`／`wal_unhealthy`／`state_incompatible`／`smoke_failed`、`pairing_diffs`、`reverted` |
| 步驟 5／6 失敗後回退，但新版 daemon 交接被拒（驗收期間已受理 `file.push`／flash 等）或無法確認（`METHOD_NOT_FOUND`、格式不符、逾時、行程在但 RPC 不通）（R2-F6） | 頂層仍為原失敗碼（上兩列）；回退失敗放 `revert_error.error_code`（daemon 回的 `FLASHING_BUSY`／`SESSION_BUSY`，或原碼／`INVALID_RESPONSE`／`TIMEOUT`／`SOCKET_ERROR`／`DEVICE_STILL_HELD`） | 頂層 `update_start_and_verify`；`revert_error.phase:"update_revert"` | `reverted:false`、`revert_pending:true`、`revert_error{error_code,phase,handoff:"refused"\|handoff_outcome:"refused"\|"unknown",busy\|probe:"daemon.handoff_stop"\|pids}`、hint；未送 `daemon.stop`、未硬停、入口／current／config 仍為新版、監管 enabled 還原為快照值、journal 與 `snapshot_dir` 保留（下一次 `setup` 經 `recover_journal` 完成回退） |
| 寫 journal 或原始快照失敗（磁碟滿、權限） | `INTERNAL_ERROR` | `update_journal` | `journal_write_failed`、`journal_step`；該步副作用一律未執行 |
| 中斷復原：journal 無法解析、`original` 缺漏、snapshot 檔 sha256 不符 | `INTERNAL_ERROR` | `install_recover` | `journal_corrupt`／`snapshot_missing`／`snapshot_mismatch`、`journal_step`；journal 保留 |
| 中斷復原：需用備份但 `backup_manifest_sha256` 缺或備份檔驗證不符 | `INTERNAL_ERROR` | `install_recover` | `backup_incomplete`、`journal_step`、`backup_dir`；journal 保留、不啟動任何版本 |
| 中斷復原：舊 daemon／holder 仍在、或還原後 Previous 驗收失敗 | `DEVICE_STILL_HELD`／`TIMEOUT`／`DAEMON_EXITED`／`DAEMON_NOT_READY`／`INTERNAL_ERROR` | `install_recover` | 同步驟 4／6 的附加欄位；journal 保留 |
| 中斷復原（「已開始切換」類）：目前在跑的 daemon（可能為新版）交接被拒或無法確認（R2-F6） | daemon 回的 `FLASHING_BUSY`／`SESSION_BUSY`，或原碼／`INVALID_RESPONSE`／`TIMEOUT`／`SOCKET_ERROR`／`DEVICE_STILL_HELD` | `install_recover` | `handoff:"refused"`＋`busy`，或 `probe:"daemon.handoff_stop"`＋`handoff_outcome`、`journal_step`、hint；`recovered_journal.ok:false`、journal 與 `snapshot_dir` 逐位元組保留、未送 `daemon.stop`、未硬停、不切換回 Previous、不進行本次 op |

行為（update 與 rollback 共用；rollback 的「新版本」＝Previous；首次安裝見文末）：
0. **lock＋journal**（R1-F5：journal 必須自帶回到完整 Previous 的原始資料）：`install_lock`（Task 11）取得後才 `read_current`；每個 `JournalStep` 在其副作用**開始前** `write_journal`（fsync＋`sync_dir`），寫入失敗即中止（`update_journal`，見上表）；成功或完整回退且 Previous 驗收通過後才刪 journal 與 `snapshot_dir`。中斷時 `doctor` 的 `version_pairing` 會顯示不一致，fix 指向 `serialwrap setup`。任何分支（install／update／rollback）開始前若有 `install-journal.json` → `recover_journal`，依**內部** `journal.step` 分三類（輸出 `recovered_journal:<RecoveryReport>`）：
   - **無副作用**（`lock`、`verify`、`safety_check`）：尚未動到監管、daemon、入口或設定（`stage_version` 的 `versions/<v>` 屬未啟用目錄，不需還原）→ 直接刪 journal；`from_step` 照記、`backup_used:false`。
   - **已停用／已停止、尚未切換**（`snapshot_taken`、`stop_supervisor`、`confirm_released`、`backup_started`、`backup_complete`）：先核對 `snapshot_sha256` 與 `original` 完整（缺 → `snapshot_missing`／`snapshot_mismatch`，journal 保留）；再核對穩定入口、`current-release.json`、`config.yaml` 與 `original.entries`／`original.current`／`config_sha256` 相同（不同 → 依「已開始切換」處理，但只在 `backup_manifest_sha256` 存在時，否則 `backup_incomplete`）；**不得**呼叫 `restore_backup`、不得讀 `backup_dir`（`backup_started` 時的 `*.partial` 目錄改名為 `*.incomplete` 保留為證據，`backup_used:false`）。接著還原監管與 daemon 到**原始**值：`set_enabled(original.supervisor.enabled)`（mode 不支援 enabled 時略過）；`original.daemon` 為 Some（停止前 daemon 健康在跑）且目前 ping 不通 → 先確認舊行程樹已全數結束且 POSIX `serialwrapd_tty_holders(original.daemon.attached_paths)` 為空（仍在 → `DEVICE_STILL_HELD`／`install_recover`，不啟動，§12.1「不能強行啟動上一版搶同一 UART」），再依 `original.daemon_start` 重新啟動舊版本（`supervised` → `action(Start)`；`on_demand_internal` → 以舊穩定入口執行內部 start 流程），並以步驟 6 的 1～5 驗收**舊版本**（版本＝`original.current.bundle_version`、快照 session 的 `com` 仍在、RELEASED 仍為 RELEASED）；`original.daemon` 為 None → 不啟動任何 daemon、`running` 保持 false。
   - **已開始切換**（`entries_switching`、`start_and_verify`、`revert_to_previous`）：先以 `backup_manifest_sha256` 驗證 `backup_dir/manifest.json`，再逐檔核對 manifest 內 sha256；任一不符或欄位缺 → `backup_incomplete`，journal 保留、不啟動任何版本（fail-closed）。驗證通過才執行步驟 7 的**復原變體**（見步驟 7；停止目前在跑的 daemon 只經 `stop_for_revert` 交接，忙碌或不明 → `ok:false`、journal 逐位元組保留、不硬停、不切換，R2-F6），其 entries、current、監管 enabled／running 與 daemon 啟動路徑一律取自 `journal.original`，不依賴行程記憶體中的 `SwitchReport`／`SupervisorSnapshot`。
   - 任一類還原失敗 → `RecoveryReport.ok:false`、`journal_kept:true`、journal 與 `snapshot_dir` 位元組不變，`setup` 以 rc 2 結束且**不**進行本次 op（輸出 `{"ok":false,"error_code",…,"phase":"install_recover","recovered_journal":<report>}`）；成功 → 刪 journal 後繼續本次 op。
1. **verify**：update：`parse_manifest(<bundle>/release-manifest.json)`＋`verify_bundle`＋**state_schema 相容性**（Current 與新 manifest 的 `components.daemon.state_schema` 不同 → 拒絕；只有新 manifest 宣告 `components.daemon.state_migration:{from,to,id,approval_ref}` 且 `id` 列於 `contracts/behavior_changes.yaml` 已核准項時才允許——Stage 1 沒有任何核准遷移，故一律拒絕，§12.1「如确需不相容變更，必須先核准遷移／回復方案」）＋`stage_version`；rollback：`read_current` 有 `previous` 且 `versions/<prev>` 存在且 `verify_bundle` 通過且 state_schema 相同，否則依上表（原因字串：`current-release.json 不存在`／`未記錄 previous`／`版本目錄 <dir> 不存在`／`版本目錄驗證失敗`）。
2. **safety_check**（fail-closed；**不受 `--force` 影響**——§12.1 步驟 2「檢查 Flash、FileTransfer、活躍寫入與其他不可安全中止操作；不符合交接條件時拒絕更新」沒有覆寫條款，且步驟 3 起的 daemon 停止（SC-11 shutdown drain）會取消傳輸、停止 flash pump，行程安全退出無法撤銷 DUT 上已發生的部分燒寫／部分寫檔。本步是**提早拒絕的預檢**（不動監管與 daemon），交接授權以步驟 3 的 daemon 原子交接為準。故 busy 判定、身分確認與 RPC 探測在 update／rollback 一律執行；帶 `--force` 時只在本步 detail 記 `force_ignored:true`，結果與未帶時逐欄相同。一般 `setup`（Task 10，非 update 路徑）的 `--force` 覆寫 flashing 為 Python 對等行為，不適用於此）：
   - `health.ping`(0.5) 失敗 → `ProcScan::serialwrapd_instances()` 查 endpoint 對應實例：無 → `Ok(None)`（確認沒有 daemon）；有（daemon 失聯但行程仍在，可能仍持有 COM）或 ProcScan 失敗 → `DEVICE_STILL_HELD`。
   - ping 成功 → `mcu.status`(0.5) 與 `session.list`(1.0) **都必須**回 `ok:true` 且欄位型別正確，任一失敗／逾時／格式異常 → 拒絕（上表）；`capture_daemon_identity` 的 `known==false` → `DEVICE_STILL_HELD`（`holder_unknown:true`）。
   - busy 判定：`mcu.status.flashing` → `FLASHING_BUSY`；任一 session `state ∈ {FLASHING, RECOVERING}` 或 `recovering==true` 或 `foreground_busy==true` 或 `outstanding_commands>0` 或 `interactive_session_id!=null` → `SESSION_BUSY`，detail `busy:[{"com","state","foreground_busy","outstanding_commands","interactive_session_id","recovering"}]`。
3. **stop_supervisor**：**任何停用／停止之前**先 `persist_original_snapshot`——把 `DaemonSnapshot`（identity、各 session `attached_real_path`、`com`／`state`；daemon 不在跑時為 None）、`SupervisorSnapshot`（`query` 的 mode、endpoint、running、enabled，及匯出到 `snapshot_dir` 的已註冊內容：windows-task `schtasks /Query /TN serialwrap /XML`、systemd unit 複本）、穩定入口 `EntrySnapshot`、`current-release.json` 全文、`config.yaml` 複本與 `daemon_start` 寫入 `snapshot_dir`（每檔 fsync＋`sync_dir`），再 `write_journal{step:snapshot_taken, original:Some(..), snapshot_sha256}`；兩者任一失敗 → `update_journal` 拒絕、監管與 daemon 完全未動。之後 `write_journal{step:stop_supervisor}`，windows-task／windows-service 才 `set_enabled(false)`；**停用後由 daemon 原子交接取代 CLI 端重檢**（R1-F1）：daemon 在跑時（步驟 2 非 `Ok(None)`）呼叫 `graceful_stop_with(mode, keep_disabled=true, phase:"update_confirm_released", StopRpc::Handoff{reason:"update"|"rollback"})`，其第 3 步改送 `daemon.handoff_stop`（**不送**一般 `daemon.stop`）。daemon 在共同 admission 邊界（P1 T08／P8 Task 14 的 `AdmissionGate`）內一次完成「busy 判定（與步驟 2 同條件，另含已 admit 但尚未反映到 `session.list` 的 `file.push`／`file.pull`／flash sync-probe／排入中的命令 ticket）→ 閒置才凍結所有新的有副作用操作 → 發 `handoff_token` → 觸發 SC-11 停止」，凍結後才到的 `file.push`、`command.submit`、`interactive_open`、ttyMCU sync-probe 一律零 TX 被拒（RPC 回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`；flash 端點保持沉默、不寫任何 byte 到 MCU）。因此 SC-11 shutdown drain 開始時**不存在**可被它撤銷的傳輸或燒錄——這是「不截斷」的唯一依據；**不得**再宣稱「檢查後才進入的請求由 daemon drain 完成」（SC-11 Phase 1 會 `TransferAbort`／撤銷前景命令並停止 flash pump，drain 是中止而非等待完成）。結果處置（皆不受 `--force` 影響）：`HandoffError::Busy` → `set_enabled(original.supervisor.enabled)`、輸出 daemon 回的碼與 `busy`、`handoff:"refused"`、不硬停、刪 journal；`HandoffError::Unconfirmed` → 同樣還原 enabled、**不** `hard_stop`、不切換、輸出 `probe:"daemon.handoff_stop"`＋`handoff_outcome`（`unknown` 時 hint 指示以 `serialwrap daemon status` 確認後重跑 `setup`）、刪 journal；`Ok(grant)` → `stop_supervisor.detail.handoff_token=grant.token`，之後只等待結束（≤10 s），逾時才 `hard_stop`（此時 daemon 已凍結且無不可中止操作）。步驟 2 判定 daemon 不存在時不送任何停止 RPC。
4. **confirm_released**：`wait_daemon_gone(endpoint, &snapshot.id, 10s)`（ping 不通**且** identity tree——Windows 含 launcher 與 Python 子行程——全數結束；行程結束即由 OS 關閉其 COM handle，Windows holders 依 SC-17 為 unknown，以完整行程樹消失為釋放證據），且 POSIX 上 `serialwrapd_tty_holders(attached_paths)` 為空（Windows 記 `holders_status:"unsupported"`）。失敗依上表，**不**切換；回復：`set_enabled(original.supervisor.enabled)`（若步驟 3 有 disable）；journal 保留於 `confirm_released`，下一次 `setup` 依步驟 0「已停用／已停止」類還原 running；舊 daemon 已結束而 holder 仍在時**不啟動任何版本**（§12.1「不能強行啟動上一版搶同一 UART」），由輸出的 `pids` 指示處理。
5. **backup_and_switch**（報告中仍為單一 `backup_and_switch` 步驟；journal 拆成三個持久化階段，**備份完整提交前不得切換任何東西**）：
   1. `write_journal{step:backup_started, backup_dir:<final 名稱>}` → `backup_state(from=current, to=new, original.supervisor)` 先寫入 `<backup_dir>.partial/`，所有檔案寫完並 fsync 後**最後**寫 `manifest.json`（每檔原始路徑＋sha256）並 fsync，再 `replace(<backup_dir>.partial, <backup_dir>)`＋`sync_dir(backups)`；
   2. `write_journal{step:backup_complete, backup_manifest_sha256:sha256(manifest.json)}`（此後中斷才允許以備份回退）；
   3. `write_journal{step:entries_switching}` 之後才開始 `switch_entries` → `materialize_assets(force=false)`（profiles 保留使用者修改、skill 與 wrappers 以新版內嵌資產刷新，同 Task 10）→ supervised 模式重新 `install` 監管定義（穩定入口路徑不變，Task XML 仍指 `bin\serialwrapd.exe`）→ `write_current{bundle_version:new, dir, previous:Some(old), backup_dir, switched_at}`。rollback 另做：以 `current.backup_dir`（當初從 Previous 切到 Current 時的備份）呼叫 `restore_backup(include_state=false)`——**先**還原 config（mode、endpoint）、**再**依 Previous 的已註冊狀態重新註冊或 reload 監管定義（跨 mode 時：解除 Current mode 的註冊、建立 Previous mode 的註冊）；state.json 保留 Current（schema 已確認相同，含更新後才發生的 released 保護，DF-04）；被覆蓋的 Current 設定與定義已在本步新備份中。
6. **start_and_verify**：依**目標組合的 mode**（update：`target_mode`；rollback：Previous 備份的 mode）啟動——supervised → `set_enabled(true)`（還原為快照中的 enabled 值）＋`action(Start)`；on-demand 且步驟 3 前 daemon 健康 → 以新穩定入口執行內部 start 流程。驗收**全部**通過才算成功（最多等 daemon start 的輪詢時間）：
   1. `[<bin>/serialwrap, "--version"]`（`SysEnv::run`）輸出等於 `serialwrap <new>`；
   2. `health.status` ok 且 `version == pairing.daemon_version`；`check_pairing` 無 diff；
   3. `health.status.wal.healthy == true`（同 doctor `wal_writable` 判準）；
   4. 狀態相容：`session.list` ok；快照中每個 session 的 `com` 仍在；快照時為 RELEASED 者仍為 RELEASED（不得被自動 attach）；
   5. 可操作性 smoke：唯讀 `device.list`、`alias.list` 皆 ok。
   daemon 於步驟 3 前未在跑 → 只驗 1 與 manifest 配對，不 spawn。
7. **revert_to_previous**（步驟 5 或 6 失敗、或 `recover_journal`）：**停止目前在跑的 daemon 只經原子交接**（R2-F6；§12.1 步驟 3 的「安全停止」與步驟 2 的「不符合交接條件時拒絕」同樣適用於回退：步驟 6 的新版 daemon 一啟動就對外服務，驗收期間外部 client 可能已讓它受理 `file.push`／`file.pull`／flash sync-probe／命令，一般 `daemon.stop` 觸發的 SC-11 drain Phase 1 會 `TransferAbort`／停 flash pump，無法撤銷 DUT 上的部分寫檔／燒寫——與 R1-F1 修正的正向競態相同）。`stop_for_revert(ctx, mode, ep, phase)`：
   - **確認不存在**：`health.ping`(0.5) 失敗**且** `ProcScan::serialwrapd_instances()` 查無 endpoint 對應實例（含步驟 6 spawn 的 pid；例如 `DAEMON_EXITED` 或步驟 5 失敗而從未啟動）→ 不送任何停止 RPC，直接進入還原。ProcScan 失敗不算確認不存在。
   - **其他一律**（ping ok，或 ping 不通但行程仍在、或 ProcScan 失敗）→ `graceful_stop_with(mode, keep_disabled=true, phase, StopRpc::Handoff{reason:"revert"})`（`mode`＝啟動該 daemon 的組合：in-process 為步驟 6 的目標 mode；復原變體為目前 config 的 mode），**不送** `daemon.stop`、**不**呼叫 `graceful_stop`（`Plain`）。`Ok(grant)` → detail 記 `revert_handoff_token`，等待結束（≤10 s），逾時才 `hard_stop`（此時已凍結、無不可中止操作）；ping 不通但行程仍在時交接 connect 失敗即為 `Unconfirmed`（無 RPC 通道可原子確認閒置）。
   - **`Err(Handoff(Busy|Unconfirmed))` → 回退暫緩（fail-closed）**：**不** `hard_stop`、**不** `revert_entries`／`restore_backup`／`write_current`、不切換監管註冊（新版維持 Current 且繼續服務其已受理的操作）；監管 enabled 還原為交接前的值（in-process＝步驟 6 設定的 `original.supervisor.enabled`；復原變體＝交接前 `query()` 值）；journal 與 `snapshot_dir` **逐位元組保留**（本子步驟在 `write_journal{step:revert_to_previous}` **之前**執行，故 journal 仍停在 `entries_switching`／`start_and_verify`；交接 ok 後中斷，下一次復原見 daemon 已不存在即直接還原，冪等）。in-process 輸出頂層維持原失敗碼與 `phase:"update_start_and_verify"`，另帶 `reverted:false`、`revert_pending:true`、`revert_error{error_code:<daemon 回碼或 Unconfirmed 碼>, phase:"update_revert", handoff:"refused"＋busy | probe:"daemon.handoff_stop"＋handoff_outcome | pids}`、hint `新版 daemon 仍有進行中的傳輸／燒錄（或無法確認），未中止、尚未回退；完成後再執行 serialwrap setup 以回到 <prev>`（`unknown`／`METHOD_NOT_FOUND` 時另指示以 `serialwrap daemon status` 確認無進行中操作，必要時由使用者明確 `serialwrap daemon stop` 後重跑 `setup`）；`failure.json` 照寫並加 `revert_deferred:true`。
   交接成功（或確認不存在）後：`write_journal{step:revert_to_previous}` → `revert_entries` → `restore_backup(本次備份, include_state=false)`（config → 定義檔 → 重新註冊）→ `write_current(舊值)` → 依舊 `SupervisorSnapshot` 還原 enabled／running → 舊版本以步驟 6 的 1～5 驗收。輸出依上表，detail 帶 `reverted:true|false`、`pairing_diffs`、`revert_error`（若有）；失敗證據（各步驟 detail、新版 daemon 最後 `health.status` 或 spawn 錯誤）保存到 `<backup_dir>/failure.json`。
   **復原變體**（`recover_journal` 的「已開始切換」類；行程記憶體已遺失）：先 `stop_for_revert(phase:"install_recover_wait_stop")`（同上；有 daemon（可能是新版，且可能自上次 updater 中斷後已服務很久）→ 只經 `daemon.handoff_stop{"reason":"revert"}`，**不送** `daemon.stop`；`Busy`／`Unconfirmed` → `RecoveryReport{ok:false, journal_kept:true, error_code:<daemon 回碼或 Unconfirmed 碼>}`＋`handoff:"refused"`＋`busy` 或 `probe:"daemon.handoff_stop"`＋`handoff_outcome`，`phase:"install_recover"`，journal 與 `snapshot_dir` 逐位元組不變、不硬停、不切換、`setup` rc 2 且不進行本次 op；交接 ok 後 `wait_daemon_gone`（10 s，逾時才 `hard_stop`，仍失敗 → `TIMEOUT`／`install_recover`，journal 保留））→ `write_journal{step:revert_to_previous}` → 以 `original.entries` 還原穩定入口（`restore_entries_from_snapshot`：Symlink 重指 `target`；Copy 由 `versions/<original.current.dir>/bin/<n>` 複製並核對 `sha256` 等於快照值，不符 → 失敗；Absent 刪除）→ `restore_backup(backup_dir, include_state=false)` → `write_current(original.current)`（逐欄等於快照）→ 確認 tty holder 已釋放後依 `original.supervisor.enabled`／`original.daemon`／`original.daemon_start` 還原 enabled 與 running → 步驟 6 的 1～5 驗收舊版本。全部通過才刪 journal；`failure.json` 另記 `recovered_from_step`。

**首次安裝**（`ExeLocation::External`＋manifest、`read_current` 為 None）：lock → verify＋`stage_version` → `materialize_assets(force)`（Task 10，含 skill link 與 wrappers）→ `switch_entries` → `resolve_target_mode`（Windows 預設 `windows-task`）→ effective socket（Task 10 步驟 7）→ `reconcile(old = config.mode or on-demand, target)`（寫入 config 的 `supervision_mode`／`socket_path`、註冊並啟動監管）→ `write_current{previous:None}` → 步驟 6 驗收（supervised 時）。失敗 → 移除本次 `bin` 入口與 config 變更（以本次備份還原）並回報。首次安裝同樣在 `materialize_assets` 之前 `persist_original_snapshot`（`original.current:None`、入口多為 `Absent`、`config.yaml` 複本或 `config_sha256:None`、`daemon` 依當下 ping）並寫 `snapshot_taken`，中斷復原依步驟 0：刪除 `Absent` 入口、以 snapshot 的 `config.yaml` 還原（或刪除本次新建者）、解除本次註冊、`original.daemon` 為 None 時不啟動任何 daemon。輸出 `{"ok":true,"install":<report>,"setup":<reconcile 結果>,"legacy","doctor_hint","console_hint"[,"pending_sudo"]}`。

CLI 輸出：成功 `{"ok":true,"update":{"backup_dir","from_version","reverted":false,"steps":[{"ok":true,"step":"lock",…},…],"to_version"},"legacy","doctor_hint","console_hint"[,"pending_sudo"][,"recovered_journal"]}` exit 0（rollback 以 `"rollback"`、首次安裝以 `"install"` 取代 `"update"` 鍵）；失敗 `{"ok":false,"error_code":<code>,"phase":<phase>,"message":…,"update"|"rollback"|"install":<report>}`、stderr 鏡像（context `setup`）、exit 2。`setup` 在 `ExeLocation::External` 但無 manifest（`cargo run` 開發模式）→ 走 Task 10 的一般 setup（不動佈局）。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-cli/tests/mgmt_update.rs`）

```rust
mod support; use support::*;
#[test]
fn rollback_without_previous_is_unavailable() {
    let t = TestEnv::windows().with_install_root().with_current("0.4.0", None);
    let (rc, out, err) = t.run(&["setup", "--rollback"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase")), ("INVALID_ARGS", "rollback_verify"));
    assert_eq!(s(&j, "message"), "沒有可回滾的 Previous 版本（未記錄 previous）");
    assert!(err.starts_with("serialwrap: setup failed: INVALID_ARGS: "));
}
#[test]
fn update_refuses_while_file_transfer_in_progress() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false}]}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    assert_eq!((s(&json(&out), "error_code"), s(&json(&out), "phase")), ("SESSION_BUSY", "update_safety_check"));
    assert!(t.service().actions().is_empty(), "不得停止監管");
}
#[test]
fn update_safety_probe_timeout_refuses() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "session.list");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "phase"), s(&j, "probe")), ("update_safety_check", "session.list"));
    assert!(t.service().actions().is_empty() && t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
}
// R0-F4：§12.1 步驟 2 的交接安全檢查不得被 --force 覆寫（Flash／FileTransfer 中停 daemon 無法撤銷 DUT 已發生的部分燒寫／寫檔）
fn assert_refused_untouched(t: &TestEnv, j: &serde_json::Value, key: &str, code: &str, cur_ver: &str) {
    assert_eq!((s(j, "error_code"), s(j, "phase")), (code, "update_safety_check"));
    let steps = j[key]["steps"].as_array().unwrap();
    assert_eq!(steps.last().unwrap()["step"], "safety_check", "不得進入 stop_supervisor");
    assert_eq!(steps.last().unwrap()["detail"]["force_ignored"], true);
    assert!(steps.last().unwrap()["detail"].get("skipped").is_none(), "busy 判定不得被略過");
    assert!(t.service().actions().is_empty(), "不得 disable／stop／End 監管");
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop" && r.1 != "daemon.handoff_stop"), "不得要求 daemon 停止或交接");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, cur_ver);
    assert_eq!(std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(), t.version_bytes(cur_ver, "bin/serialwrap.exe"));
}
#[test]
fn update_force_does_not_override_flashing() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}");
    let (rc, out, err) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(s(&j, "message"), "flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）");
    assert!(err.starts_with("serialwrap: setup failed: FLASHING_BUSY: "));
    assert_refused_untouched(&t, &j, "update", "FLASHING_BUSY", "0.4.0");
}
#[test]
fn rollback_force_does_not_override_flashing() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_installed("0.4.1").with_current("0.4.1", Some("0.4.0"))
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":true,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}");
    let (rc, out, _) = t.run(&["setup", "--rollback", "--force"]);
    assert_eq!(rc, 2);
    assert_refused_untouched(&t, &json(&out), "rollback", "FLASHING_BUSY", "0.4.1");
}
#[test]
fn update_force_does_not_override_file_transfer() {
    // file.push 進行中：session.list 顯示 foreground_busy:true 且 outstanding_commands>0
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false}]}");
    let (rc, out, _) = t.run_from_bundle(&["setup", "--force"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_refused_untouched(&t, &j, "update", "SESSION_BUSY", "0.4.0");
    assert_eq!(j["busy"][0]["com"], "COM0");
}
#[test]
fn rollback_force_does_not_override_file_transfer() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_installed("0.4.1").with_current("0.4.1", Some("0.4.0"))
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":1,\"interactive_session_id\":null,\"recovering\":false}]}");
    let (rc, out, _) = t.run(&["setup", "--rollback", "--force"]);
    assert_eq!(rc, 2);
    assert_refused_untouched(&t, &json(&out), "rollback", "SESSION_BUSY", "0.4.1");
}
// R1-F1：步驟 2 預檢閒置之後才開始的 file.push／flash，必須由 daemon 原子交接拒絕停止；CLI 不得送一般 daemon.stop、不得硬停
const IDLE_LIST: &str = "{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false}]}";
fn assert_handoff_refused(t: &TestEnv, j: &serde_json::Value, key: &str, code: &str, cur_ver: &str) {
    assert_eq!((s(j, "error_code"), s(j, "phase")), (code, "update_safety_check"));
    assert_eq!(s(j, "handoff"), "refused");
    let steps = j[key]["steps"].as_array().unwrap();
    assert_eq!(steps.last().unwrap()["step"], "stop_supervisor");
    let rpcs: Vec<String> = t.rpc().requests().iter().map(|r| r.1.clone()).collect();
    assert_eq!(rpcs.iter().filter(|m| *m == "daemon.handoff_stop").count(), 1, "交接只問一次、不重試");
    assert!(rpcs.iter().all(|m| m != "daemon.stop"), "不得改送一般 daemon.stop");
    let o = t.event_order();
    assert!(o.index_of("run:schtasks /Change /TN serialwrap /DISABLE") < o.index_of("rpc:daemon.handoff_stop"));
    assert!(o.index_of("rpc:daemon.handoff_stop") < o.index_of("run:schtasks /Change /TN serialwrap /ENABLE"), "拒絕後還原 enabled");
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")), "不得硬停");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, cur_ver);
    assert_eq!(std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(), t.version_bytes(cur_ver, "bin/serialwrap.exe"));
    assert!(!t.root().root.join("install-journal.json").exists());
}
#[test]
fn update_file_push_after_last_probe() {
    // 預檢 session.list／mcu.status 皆閒置；預檢後另一 client 開始 file.push → daemon 交接判定忙碌（ops 含 file.push）
    let t = TestEnv::windows().with_config("supervision_mode: windows-task\n").with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(TestEnv::win_endpoint(), "daemon.handoff_stop", "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}");
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert!(err.starts_with("serialwrap: setup failed: SESSION_BUSY: "));
    assert_handoff_refused(&t, &j, "update", "SESSION_BUSY", "0.4.0");
    assert_eq!(j["busy"][0]["ops"][0], "file.push");
    assert_eq!(rpc_params(&t, "daemon.handoff_stop")["reason"], "update");   // support 新增 rpc_params：該方法第一次請求的 params
}
#[test]
fn update_flash_races_prepare() {
    // 預檢時 flashing:false；flasher 在預檢與交接之間觸發 ttyMCU sync-probe → daemon 回 FLASHING_BUSY（優先於 SESSION_BUSY）
    let t = TestEnv::windows().with_config("supervision_mode: windows-task\n").with_install_root().with_installed("0.4.0").with_installed("0.4.1").with_current("0.4.1", Some("0.4.0"))
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(TestEnv::win_endpoint(), "daemon.handoff_stop", "{\"ok\":false,\"error_code\":\"FLASHING_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"flash\"]}]}");
    let (rc, out, _) = t.run(&["setup", "--rollback", "--force"]);
    assert_eq!(rc, 2, "{out}");
    let j = json(&out);
    assert_handoff_refused(&t, &j, "rollback", "FLASHING_BUSY", "0.4.1");
    assert_eq!(rpc_params(&t, "daemon.handoff_stop")["reason"], "rollback");
}
#[test]
fn update_handoff_unsupported_daemon_refuses_without_stop() {
    // 不支援交接的 daemon（P1 前 Python、P8 Task 14 前 Rust）回 METHOD_NOT_FOUND → fail-closed，不得退回一般 daemon.stop
    let t = TestEnv::windows().with_config("supervision_mode: windows-task\n").with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", "{\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "mcu.status", "{\"flashing\":false,\"ok\":true}")
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", IDLE_LIST)
        .with_rpc_ok(TestEnv::win_endpoint(), "daemon.handoff_stop", "{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}");
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase"), s(&j, "probe"), s(&j, "handoff_outcome")),
               ("METHOD_NOT_FOUND", "update_safety_check", "daemon.handoff_stop", "refused"));
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")));
}
#[test]
fn pairing_failure_after_switch_reverts_to_complete_previous() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_new_daemon_reporting_version("0.4.0");          // 新 daemon 回報舊版本 → 配對失敗
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")), ("INTERNAL_ERROR", "update_start_and_verify", "pairing_mismatch"));
    assert!(b(&dict(&j, "update"), "reverted"));
    let cur = sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap();
    assert_eq!(cur.bundle_version, "0.4.0");
    assert_eq!(std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(), t.version_bytes("0.4.0", "bin/serialwrap.exe"));
    assert!(t.backup_dir_of(&j).join("failure.json").exists());
    assert!(!t.root().root.join("install-journal.json").exists());
}
// R1-F5：journal 必須在任何停用／停止前持久保存原始入口、監管與 daemon 快照；備份完整提交前不得切換
fn acts(t: &TestEnv) -> Vec<String> { t.service().actions().into_iter().map(|a| a.1).collect() }
fn assert_back_to_original(t: &TestEnv, cfg0: &[u8], cur0: &[u8]) {
    assert_eq!(std::fs::read(t.root().current_file).unwrap(), cur0, "current-release.json 必須逐位元組回到原值");
    assert_eq!(std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(), t.version_bytes("0.4.0", "bin/serialwrap.exe"));
    assert_eq!(std::fs::read(t.root().bin.join("serialwrapd.exe")).unwrap(), t.version_bytes("0.4.0", "bin/serialwrapd.exe"));
    assert_eq!(t.config_bytes(), cfg0, "config.yaml 必須回到原值");
    assert!(t.journal_json().is_none() && !t.root().root.join("install-journal.snapshot").exists());
}
#[test]
fn journal_persists_original_snapshot_before_disable() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true /*enabled*/, true /*running*/)
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("stop_supervisor:before_effect");
    let cfg0 = t.config_bytes();
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    assert!(acts(&t).is_empty(), "crash 點之前不得有任何停用");
    let j = t.journal_json().expect("停用前 journal 必須已落盤");
    assert_eq!(j["step"], "stop_supervisor");
    let o = &j["original"];
    assert_eq!(o["current"]["bundle_version"], "0.4.0");
    assert_eq!((&o["supervisor"]["mode"], &o["supervisor"]["enabled"], &o["supervisor"]["running"]),
               (&serde_json::json!("windows-task"), &serde_json::json!(true), &serde_json::json!(true)));
    assert_eq!(o["daemon"]["id"]["pid"], 4242);
    assert_eq!(o["daemon_start"], "supervised");
    assert_eq!(o["entries"][0]["sha256"], sha256_hex(&t.version_bytes("0.4.0", "bin/serialwrap.exe")));
    assert_eq!(o["config_sha256"], sha256_hex(&cfg0));
    assert!(j["backup_dir"].is_null() && j["backup_manifest_sha256"].is_null());
    let sdir = std::path::PathBuf::from(j["snapshot_dir"].as_str().unwrap());
    assert!(sdir.join("registered.task.xml").exists() && sdir.join("config.yaml").exists() && sdir.join("entries.json").exists());
}
#[test]
fn journal_recovery_restarts_original_on_demand_daemon_after_stop() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n")
        .with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("confirm_released:after_effect");            // 舊 daemon 已停止、尚未開始備份
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    assert!(t.rpc().requests().iter().any(|r| r.1 == "daemon.handoff_stop"));   // R1-F1：update 停止只經交接
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    let t = t.clear_crash().with_new_daemon_reporting_version("0.4.0");
    let rec = t.recover().expect("殘留 journal");
    assert!(rec.ok, "{:?}", rec.reason);
    assert_eq!((rec.from_step, rec.backup_used, rec.journal_kept), ("confirm_released", false, false));
    let sp = t.process().spawns();
    assert_eq!(sp.len(), 1, "停止前 daemon 健康在跑 → 必須還原 running");
    assert_eq!(std::path::Path::new(&sp[0].0[0]), t.root().bin.join("serialwrapd.exe"), "以舊穩定入口啟動");
    assert_eq!(rec.restored["daemon_version"], "0.4.0");
    assert!(acts(&t).is_empty(), "on-demand 不得動監管");
    assert_back_to_original(&t, &cfg0, &cur0);
}
#[test]
fn journal_recovery_restores_original_enabled_and_running_windows_task() {
    for (enabled, running) in [(true, true), (true, false), (false, false)] {
        let mut t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
            .with_supervisor_state("windows-task", enabled, running);
        if running { t = t.with_idle_daemon(TestEnv::win_endpoint(), 4242).with_new_daemon_reporting_version("0.4.0"); }
        let t = t.with_crash_at("confirm_released:after_effect");
        assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
        let before = acts(&t).len();
        let t = t.clear_crash();
        let rec = t.recover().unwrap();
        assert!(rec.ok, "enabled={enabled} running={running}: {:?}", rec.reason);
        let after: Vec<String> = acts(&t)[before..].to_vec();
        assert_eq!(after.iter().any(|a| a == "set_enabled:true"), enabled, "enabled 必須回到快照值（原為 disabled 者不得被啟用）");
        assert_eq!(after.iter().filter(|a| a.as_str() == "start").count(), running as usize, "running 必須回到快照值");
        if running { assert!(t.event_order().index_of("service:set_enabled:true") < t.event_order().index_of("service:start")); }
        assert_eq!((&rec.restored["supervisor"]["enabled"], &rec.restored["supervisor"]["running"]),
                   (&serde_json::json!(enabled), &serde_json::json!(running)));
        assert!(t.journal_json().is_none());
    }
}
#[test]
fn journal_recovery_backup_started_never_uses_partial_backup() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true).with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("backup_started:after_config_copy");          // 備份只寫了一半，manifest.json 尚未寫
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let j = t.journal_json().unwrap();
    assert_eq!(j["step"], "backup_started");
    assert!(j["backup_manifest_sha256"].is_null());
    let bdir = std::path::PathBuf::from(j["backup_dir"].as_str().unwrap());
    assert!(!bdir.exists(), "未提交的備份只能在 *.partial");
    let t = t.clear_crash().with_new_daemon_reporting_version("0.4.0");
    let rec = t.recover().unwrap();
    assert!(rec.ok);
    assert_eq!((rec.from_step, rec.backup_used), ("backup_started", false));
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("/Create /XML")), "不得以未完成備份重新註冊");
    assert!(std::path::PathBuf::from(format!("{}.incomplete", bdir.display())).exists(), "partial 備份改名保留為證據");
    assert!(!bdir.exists());
    assert!(acts(&t).iter().any(|a| a == "start"));
    assert_back_to_original(&t, &cfg0, &cur0);
}
#[test]
fn switch_never_starts_before_backup_committed() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true).with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("entries_switching:before_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let j = t.journal_json().unwrap();
    assert_eq!(j["step"], "entries_switching");
    let bdir = std::path::PathBuf::from(j["backup_dir"].as_str().unwrap());
    let m = std::fs::read(bdir.join("manifest.json")).expect("切換前備份必須已提交");
    assert_eq!(j["backup_manifest_sha256"], sha256_hex(&m));
    assert!(!std::path::PathBuf::from(format!("{}.partial", bdir.display())).exists());
    assert_eq!(std::fs::read(t.root().bin.join("serialwrap.exe")).unwrap(), t.version_bytes("0.4.0", "bin/serialwrap.exe"), "entries_switching 前入口不得變動");
}
#[test]
fn journal_recovery_with_unverifiable_backup_keeps_journal() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true).with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("entries_switching:after_first_rename");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let bdir = std::path::PathBuf::from(t.journal_json().unwrap()["backup_dir"].as_str().unwrap().to_owned());
    t.write_file(&bdir.join("config.yaml"), b"tampered\n");          // 備份內容與 manifest 不符
    let (j0, before) = (t.journal_bytes().unwrap(), acts(&t).len());
    let t = t.clear_crash();
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = json(&out);
    assert_eq!((s(&o, "error_code"), s(&o, "phase"), s(&o, "reason")), ("INTERNAL_ERROR", "install_recover", "backup_incomplete"));
    assert_eq!(o["recovered_journal"]["journal_kept"], true);
    assert_eq!(t.journal_bytes().unwrap(), j0, "復原失敗時 journal 必須逐位元組保留");
    assert_eq!(acts(&t).len(), before, "fail-closed：不得啟動任何版本");
    assert!(t.process().spawns().is_empty());
    assert!(t.calls().iter().all(|c| !c.join(" ").contains("/Create /XML")), "不得以未驗證備份還原");
}
#[test]
fn journal_recovery_failure_keeps_journal_and_skips_op() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_config("supervision_mode: on-demand\n").with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("confirm_released:after_effect");
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let j0 = t.journal_bytes().unwrap();
    let t = t.clear_crash().with_spawn_exits(1);                         // 舊版 daemon 重新啟動失敗
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = json(&out);
    assert_eq!((s(&o, "error_code"), s(&o, "phase")), ("DAEMON_EXITED", "install_recover"));
    assert_eq!(o["recovered_journal"]["ok"], false);
    assert_eq!(t.journal_bytes().unwrap(), j0);
    assert!(o.get("update").is_none(), "復原失敗不得繼續本次 update");
    assert_eq!(t.rpc().requests().iter().filter(|r| r.1 == "daemon.handoff_stop").count(), 1, "不得再跑一次 update 的停止流程");
    assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"));
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, "0.4.0");
}
// R2-F6：步驟 6 的新版 daemon 一啟動就對外服務；步驟 7 回退與 recover_journal 停止它時只能經原子交接，
// 新版已受理的 file.push／flash 不得被 SC-11 drain 中止（修正前必 FAIL：舊實作以 graceful_stop 送一般 daemon.stop）
const BUSY_PUSH: &str = "{\"ok\":false,\"error_code\":\"SESSION_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"READY\",\"foreground_busy\":true,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"file.push\"]}]}";
const BUSY_FLASH: &str = "{\"ok\":false,\"error_code\":\"FLASHING_BUSY\",\"busy\":[{\"com\":\"COM0\",\"state\":\"FLASHING\",\"foreground_busy\":false,\"outstanding_commands\":0,\"interactive_session_id\":null,\"recovering\":false,\"ops\":[\"flash\"]}]}";
fn methods(t: &TestEnv) -> Vec<String> { t.rpc().requests().iter().map(|r| r.1.clone()).collect() }
fn handoffs(t: &TestEnv) -> usize { methods(t).iter().filter(|m| *m == "daemon.handoff_stop").count() }
fn assert_revert_deferred_untouched(t: &TestEnv, j0: Option<&[u8]>, new_ver: &str) {
    assert!(methods(t).iter().all(|m| m != "daemon.stop"), "回退／復原不得送一般 daemon.stop");
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")), "交接被拒或不明時不得硬停");
    assert_eq!(sw_cli::install_layout::read_current(&t.root()).unwrap().unwrap().bundle_version, new_ver, "不得切換回 Previous");
    for exe in ["serialwrap.exe", "serialwrapd.exe"] {
        assert_eq!(std::fs::read(t.root().bin.join(exe)).unwrap(), t.version_bytes(new_ver, &format!("bin/{exe}")), "{exe} 不得被還原");
    }
    let st = t.service().query_now();
    assert_eq!((st.enabled, st.running), (Some(true), true), "新版繼續服務其已受理的操作、監管 enabled 還原為交接前值");
    assert!(t.journal_json().is_some() && t.root().root.join("install-journal.snapshot").exists(), "journal 與 snapshot_dir 必須保留");
    if let Some(j0) = j0 { assert_eq!(t.journal_bytes().unwrap(), j0, "journal 必須逐位元組不變"); }
}
#[test]
fn revert_refuses_while_new_daemon_pushing() {
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true).with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_new_daemon_reporting_version("0.4.0")        // 新版驗收 pairing_mismatch → 步驟 7
        .with_new_daemon_handoff_script(&[BUSY_PUSH]);     // 驗收期間外部 client 已讓新版受理 file.push
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    let (rc, out, err) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2, "{out}");
    assert!(err.starts_with("serialwrap: setup failed: INTERNAL_ERROR: "));
    let j = json(&out);
    assert_eq!((s(&j, "error_code"), s(&j, "phase"), s(&j, "reason")), ("INTERNAL_ERROR", "update_start_and_verify", "pairing_mismatch"));
    let u = &j["update"];
    assert_eq!((&u["reverted"], &u["revert_pending"]), (&serde_json::json!(false), &serde_json::json!(true)));
    let re = &u["revert_error"];
    assert_eq!((re["error_code"].as_str(), re["phase"].as_str(), re["handoff"].as_str()), (Some("SESSION_BUSY"), Some("update_revert"), Some("refused")));
    assert_eq!(re["busy"][0]["ops"][0], "file.push");
    assert_eq!(handoffs(&t), 2, "步驟 3 停舊版 1 次＋步驟 7 停新版 1 次，不重試");
    assert_eq!(rpc_params_nth(&t, "daemon.handoff_stop", 1)["reason"], "revert");
    let o = t.event_order();
    assert!(o.index_of("service:start") < o.index_of_nth("rpc:daemon.handoff_stop", 1), "交接對象是步驟 6 啟動的新版");
    assert!(o.index_of_nth("rpc:daemon.handoff_stop", 1) < o.index_of_nth("service:set_enabled:true", 1), "拒絕後還原 enabled");
    assert_revert_deferred_untouched(&t, None, "0.4.1");
    assert_eq!(t.journal_json().unwrap()["step"], "start_and_verify", "交接被拒前不得推進到 revert_to_previous");
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&std::fs::read(t.backup_dir_of(&j).join("failure.json")).unwrap()).unwrap()["revert_deferred"], true);
    // 新版已受理的 push 完成（script 用盡 → 交接回閒置 grant）後，下一次 setup 的 recover_journal 才完成回退
    let rec = t.recover().expect("殘留 journal");
    assert!(rec.ok, "{:?}", rec.reason);
    assert_eq!((rec.from_step, rec.backup_used), ("start_and_verify", true));
    assert_eq!(handoffs(&t), 3);
    assert_eq!(rpc_params_nth(&t, "daemon.handoff_stop", 2)["reason"], "revert");
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert_back_to_original(&t, &cfg0, &cur0);
    assert_eq!(t.rpc_now(TestEnv::win_endpoint(), "health.status")["version"], "0.4.0");
}
#[test]
fn journal_recovery_refuses_while_new_daemon_flashing() {
    // updater 在新版啟動後、驗收完成前被殺；新版持續對外服務並開始 flash
    let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
        .with_supervisor_state("windows-task", true, true).with_idle_daemon(TestEnv::win_endpoint(), 4242)
        .with_crash_at("start_and_verify:after_effect");
    let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
    assert_eq!(t.run_from_bundle(&["setup"]).0, 137);
    let t = t.clear_crash().with_new_daemon_reporting_version("0.4.0")
        .with_new_daemon_handoff_script(&[BUSY_FLASH, BUSY_FLASH]);   // 第一次 recover() 與下一次 setup 時 flash 仍在進行
    let j0 = t.journal_bytes().unwrap();
    assert_eq!(t.journal_json().unwrap()["step"], "start_and_verify");
    let h0 = handoffs(&t);
    let rec = t.recover().unwrap();
    assert!(!rec.ok && rec.journal_kept);
    assert_eq!((rec.from_step, rec.error_code), ("start_and_verify", Some("FLASHING_BUSY")));
    assert_eq!((&rec.detail["handoff"], &rec.detail["busy"][0]["ops"][0]), (&serde_json::json!("refused"), &serde_json::json!("flash")));
    assert_eq!(handoffs(&t), h0 + 1);
    assert_eq!(rpc_params_nth(&t, "daemon.handoff_stop", h0)["reason"], "revert");
    assert_revert_deferred_untouched(&t, Some(&j0), "0.4.1");
    // 經 setup 入口同樣 fail-closed，且不進行本次 op
    let (rc, out, _) = t.run_from_bundle(&["setup"]);
    assert_eq!(rc, 2);
    let o = json(&out);
    assert_eq!((s(&o, "error_code"), s(&o, "phase")), ("FLASHING_BUSY", "install_recover"));
    assert_eq!((&o["recovered_journal"]["ok"], &o["recovered_journal"]["journal_kept"]), (&serde_json::json!(false), &serde_json::json!(true)));
    assert_eq!(o["recovered_journal"]["detail"]["handoff"], "refused");
    assert!(o.get("update").is_none(), "復原未完成不得繼續本次 update");
    assert_revert_deferred_untouched(&t, Some(&j0), "0.4.1");
    // flash 完成（script 用盡）→ 交接 ok → 回到完整 Previous
    let rec = t.recover().unwrap();
    assert!(rec.ok, "{:?}", rec.reason);
    assert!(methods(&t).iter().all(|m| m != "daemon.stop"));
    assert!(!t.calls().iter().any(|c| c.get(1).map(String::as_str) == Some("/End")), "閒置新版正常結束時不得硬停");
    assert_back_to_original(&t, &cfg0, &cur0);
}
#[test]
fn update_interrupt_injection_every_point_recovers_to_previous() {
    for point in CRASH_POINTS {
        let t = TestEnv::windows().with_install_root().with_installed("0.4.0").with_bundle("0.4.1")
            .with_supervisor_state("windows-task", true, true).with_idle_daemon(TestEnv::win_endpoint(), 4242)
            .with_crash_at(point);
        // revert_to_previous 的點只有在新版驗收失敗時才會走到：讓新 daemon 回報舊版本 → pairing_mismatch → 步驟 7
        let t = if point.starts_with("revert_to_previous:") { t.with_new_daemon_reporting_version("0.4.0") } else { t };
        let (cfg0, cur0) = (t.config_bytes(), std::fs::read(t.root().current_file).unwrap());
        assert_eq!(t.run_from_bundle(&["setup"]).0, 137, "{point}");
        let t = t.clear_crash().with_new_daemon_reporting_version("0.4.0");
        match t.recover() {
            None => assert!(point.starts_with("lock:") || point.starts_with("verify:") || point.starts_with("safety_check:"), "{point}: 有副作用的點必須留下 journal"),
            Some(rec) => assert!(rec.ok, "{point}: {:?}", rec.reason),
        }
        assert_back_to_original(&t, &cfg0, &cur0);
        let st = t.service().query_now();                           // FakeServiceCtl 目前的註冊狀態
        assert_eq!((st.enabled, st.running), (Some(true), true), "{point}: 監管必須回到原始 enabled／running");
        assert_eq!(t.rpc_now(TestEnv::win_endpoint(), "health.status")["version"], "0.4.0", "{point}");
        assert!(t.rpc().requests().iter().all(|r| r.1 != "daemon.stop"), "{point}: update、步驟 7 回退與 journal 復原皆不得送一般 daemon.stop（R2-F6）");
    }
}
#[test]
fn crash_points_cover_every_journal_step() {
    for step in ["lock","verify","safety_check","snapshot_taken","stop_supervisor","confirm_released",
                 "backup_started","backup_complete","entries_switching","start_and_verify","revert_to_previous"] {
        for edge in ["before_effect", "after_effect"] {
            assert!(CRASH_POINTS.contains(&format!("{step}:{edge}").as_str()), "{step}:{edge}");
        }
    }
    assert!(CRASH_POINTS.contains(&"backup_started:after_config_copy") && CRASH_POINTS.contains(&"entries_switching:after_first_rename"));
    assert!(CRASH_POINTS.contains(&"start_and_verify:started"), "R2-F6：新版已對外服務、驗收前的中斷點");
}
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `update_happy_path_steps_in_order` | `steps[*].step` == `["lock","verify","safety_check","stop_supervisor","confirm_released","backup_and_switch","start_and_verify"]`、全 ok、current=新版、previous=舊版、journal 已刪 |
| `update_lock_contention_no_side_effects` | 另一行程持 `.install.lock` → `TIMEOUT`／`install_lock`、無任何 RPC、install root 不變 |
| `update_verify_failed_no_side_effects` | bundle 內 exe 被竄改 → `CHECKSUM_VERIFY_FAILED`／`update_verify`、`diffs[0].field` 以 `sha256:` 開頭、無任何 RPC stop、`versions/<v>` 不存在 |
| `update_refuses_incompatible_state_schema` | 新 manifest `state_schema:"python-state-v1"` → `INVALID_ARGS`／`state_schema_incompatible`、無 stop |
| `update_flashing_busy` | `flashing:true` → `FLASHING_BUSY`、message `flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）` |
| `update_busy_background_command` | `foreground_busy:false, outstanding_commands:2` → `SESSION_BUSY` |
| `update_busy_interactive_lease` | `interactive_session_id:"L1"` → `SESSION_BUSY` |
| `update_busy_recovering` | `recovering:true` 或 `state:"RECOVERING"` → `SESSION_BUSY` |
| `update_safety_malformed_session_list_refuses` | `session.list` 回 `{"ok":true}`（缺 sessions）→ `INVALID_RESPONSE`／`probe:"session.list"` |
| `update_busy_appears_after_disable_aborts`（R1-F1 改寫） | `session.list` 閒置、停用後 `daemon.handoff_stop` 回 `SESSION_BUSY`（`ops:["command.submit"]`）→ `SESSION_BUSY`／`update_safety_check`、`handoff:"refused"`、`set_enabled(true)` 被呼叫（快照 enabled 為 true）、RPC 序中無 `daemon.stop`、無 `/End`、journal 與 `snapshot_dir` 已刪；CLI 不得以第二次 `session.list` 代替交接 |
| `update_file_push_after_last_probe`（上方程式碼；R1-F1，修正前必 FAIL） | 預檢全閒置、`daemon.handoff_stop` 回 `SESSION_BUSY`＋`ops:["file.push"]` → 見 `assert_handoff_refused`：steps 止於 `stop_supervisor`、交接 RPC 恰一次且 `reason:"update"`、`/DISABLE` < `rpc:daemon.handoff_stop` < `/ENABLE`、無 `daemon.stop`、無 `/End`、Current 與 `bin` 未變；帶 `--force` 再跑結果逐欄相同 |
| `update_flash_races_prepare`（上方程式碼；R1-F1，修正前必 FAIL） | 預檢 `flashing:false`、`daemon.handoff_stop` 回 `FLASHING_BUSY`＋`ops:["flash"]`（`setup --rollback --force`）→ `FLASHING_BUSY`／`update_safety_check`、`reason:"rollback"`，其餘同上 |
| `update_handoff_unsupported_daemon_refuses_without_stop`（上方程式碼） | `daemon.handoff_stop` 回 `METHOD_NOT_FOUND` → 原碼、`probe:"daemon.handoff_stop"`、`handoff_outcome:"refused"`；無 `daemon.stop`、無 `/End`、不切換 |
| `update_handoff_recv_timeout_no_hard_stop` | `daemon.handoff_stop` recv timeout → `TIMEOUT`／`update_safety_check`、`handoff_outcome:"unknown"`、hint 含 `serialwrap daemon status`；`set_enabled(true)` 被呼叫、無 `/End`、無 `daemon.stop`、不切換 |
| `update_handoff_malformed_grant_refuses` | `daemon.handoff_stop` 回 `{"ok":true,"stopping":true}`（缺 token）或 token 非 32 hex → `INVALID_RESPONSE`、`probe:"daemon.handoff_stop"`、無 `/End`、不切換 |
| `update_never_sends_plain_daemon_stop` | update 與 rollback 的 happy path（含 on-demand 與 windows-task 模式）RPC 序中 `daemon.stop` 出現 0 次、`daemon.handoff_stop` 恰 1 次，`stop_supervisor.detail.handoff_token` 等於 daemon 回傳 token；daemon 不存在（步驟 2 `Ok(None)`）時兩者皆 0 次。R2-F6 擴充：驗收失敗回退路徑（`pairing_mismatch`／`health_failed`／`wal_unhealthy`／`state_incompatible`／`smoke_failed` 各一，update 與 rollback 各跑）`daemon.stop` 仍 0 次、`daemon.handoff_stop` 恰 2 次（第 2 次 `reason:"revert"`，`start_and_verify`／`revert_to_previous` detail 記 `revert_handoff_token`）、`reverted:true`；新版 spawn 即結束（`with_spawn_exits(1)`，`DAEMON_EXITED`）時交接恰 1 次（新版確認不存在，零停止 RPC）且 `reverted:true` |
| `update_rpc_down_child_holds_com_blocks` | ping refused 但 ProcScan 仍回 endpoint 對應實例 → `DEVICE_STILL_HELD`／`update_safety_check`、`pids` 含該 pid、無切換 |
| `update_identity_unknown_blocks` | ping ok、`FakeProcScan::fail()` → `DEVICE_STILL_HELD`、`holder_unknown:true`、無 stop |
| `update_force_does_not_override_flashing`／`rollback_force_does_not_override_flashing`（上方程式碼） | FLASHING＋`--force` → `FLASHING_BUSY`／`update_safety_check`、steps 止於 `safety_check` 且 detail `force_ignored:true`、無 `skipped`；`service().actions()` 為空、無 `daemon.stop` RPC、`current-release.json` 與 `bin` 未變 |
| `update_force_does_not_override_file_transfer`／`rollback_force_does_not_override_file_transfer`（上方程式碼） | `file.push` 進行中（`foreground_busy:true`、`outstanding_commands:1`）＋`--force` → `SESSION_BUSY`，其餘斷言同上 |
| `update_force_does_not_override_interactive_or_recovering` | `interactive_session_id:"L1"`、或 `state:"RECOVERING"`，各＋`--force` → `SESSION_BUSY`、無監管動作、無 `daemon.stop` |
| `update_force_does_not_skip_identity_or_probe` | `--force`＋`FakeProcScan::fail()` → `DEVICE_STILL_HELD`（`holder_unknown:true`）；`--force`＋`session.list` recv timeout → `probe:"session.list"` 拒絕；兩者皆無 stop |
| `update_force_recheck_after_disable_still_aborts` | `--force`＋`session.list` 閒置、`daemon.handoff_stop` 回 `SESSION_BUSY` → `SESSION_BUSY`、`set_enabled(true)` 被呼叫、無 `daemon.stop`、無 `/End`（步驟 3 交接也不受 `--force` 影響；`--force` 不得改送一般 `daemon.stop`） |
| `update_force_idle_same_result_as_without` | 全閒置時帶與不帶 `--force` 的 `steps[*].step` 與最終 `ok` 相同；帶 `--force` 時 `safety_check.detail.force_ignored==true` |
| `update_windows_task_graceful_before_switch` | `event_order`：`/DISABLE` < `rpc:daemon.handoff_stop`（回 ok＋token）< `rpc:health.ping#gone` < 第一個 `bin` 改名；RPC 序無 `daemon.stop`；daemon 正常結束時無 `/End` |
| `update_old_daemon_never_exits_aborts_without_switch` | ping 持續 ok → `TIMEOUT`／`update_confirm_released`、`bin` 未變、`set_enabled(true)` 被呼叫、無 Start；journal 保留 `step:"confirm_released"` 且 `original.daemon` 非 null；下一次 `recover()` 見 daemon 仍在跑 → 不 spawn、`ok:true`、journal 刪除 |
| `update_tty_still_held_posix_aborts` | `TestEnv::posix()`：identity 消失但 `serialwrapd_tty_holders` 仍回該 tty → `DEVICE_STILL_HELD`／`update_confirm_released`、不啟動任何版本 |
| `update_backup_contains_required_files` | backup dir 含 `config.yaml`、`state.json`、`benches.state.json`、`profiles/default.yaml`、`supervisor/serialwrap.task.xml`、`registration.json`（mode、endpoint、enabled、running）、`registered.task.xml`（`schtasks /Query /XML` 匯出）、`entries.json`、`manifest.json`（每檔 sha256） |
| `update_version_ok_but_health_fails_reverts` | 新 daemon 版本相符但 `health.status.ok:false` → `INTERNAL_ERROR`／`health_failed`、`reverted:true` |
| `update_wal_unhealthy_reverts` | `wal.healthy:false` → `wal_unhealthy`、`reverted:true` |
| `update_released_session_auto_attached_reverts` | 快照 COM1 `RELEASED`、新 daemon 回 `READY` → `state_incompatible`、`reverted:true` |
| `update_smoke_failure_reverts` | `device.list` 回 `ok:false` → `smoke_failed` |
| `update_materializes_assets_preserving_profiles` | 既有 `profiles/default.yaml` 被使用者修改 → 更新後保留；`data/skill/SKILL.md` 等於新版資產 |
| `journal_recovery_after_crash_mid_switch` | `with_crash_at("entries_switching:after_first_rename")` 產生真實的半切換 `bin`（`serialwrap.exe` 已換、`serialwrapd.exe` 未換）與已提交備份 → 下一次 `setup` 先依 `journal.original`（非行程記憶體）回復：兩個入口 sha256 等於 `original.entries`、`current-release.json` 逐位元組等於快照、`restore_backup` 使用 journal 的 `backup_dir`、監管 enabled／running 回到快照值；輸出 `recovered_journal{ok:true,from_step:"entries_switching",backup_used:true}`、journal 與 `snapshot_dir` 刪除後才繼續本次 update |
| `journal_persists_original_snapshot_before_disable`（上方程式碼） | crash 於 `stop_supervisor:before_effect` → 無任何監管動作；journal 已落盤且 `original.{current,entries,config_sha256,supervisor{mode,enabled,running},daemon.id.pid,daemon_start}` 全數正確、`snapshot_dir` 含 `registered.task.xml`／`config.yaml`／`entries.json`、`backup_dir` 為 null |
| `journal_recovery_restarts_original_on_demand_daemon_after_stop`（上方程式碼） | on-demand、停止前 daemon 健康；crash 於 `confirm_released:after_effect` → `recover()` 以舊穩定入口 spawn 一次、版本 0.4.0、`backup_used:false`、無監管動作、入口／current／config 逐位元組不變、journal 刪除 |
| `journal_recovery_restores_original_enabled_and_running_windows_task`（上方程式碼） | `(enabled,running)` ∈ {(T,T),(T,F),(F,F)}：復原後 `set_enabled:true` 出現與否等於原 enabled、`start` 次數等於原 running、`set_enabled:true` 早於 `start`、`restored.supervisor` 等於快照 |
| `journal_recovery_backup_started_never_uses_partial_backup`（上方程式碼） | crash 於 `backup_started:after_config_copy` → journal `backup_manifest_sha256` 為 null、正式 `backup_dir` 不存在；復原 `backup_used:false`、無 `/Create /XML`、partial 改名 `*.incomplete`、running 還原、其餘回到原值 |
| `switch_never_starts_before_backup_committed`（上方程式碼） | crash 於 `entries_switching:before_effect` → `backup_dir/manifest.json` 已存在且 sha256 等於 journal 的 `backup_manifest_sha256`、無 `*.partial`、入口未變 |
| `journal_recovery_with_unverifiable_backup_keeps_journal`（上方程式碼） | 半切換後竄改備份 → rc 2、`INTERNAL_ERROR`／`install_recover`／`backup_incomplete`、`recovered_journal.journal_kept:true`、journal 逐位元組不變、無監管動作、無 spawn、無 `/Create /XML` |
| `journal_recovery_failure_keeps_journal_and_skips_op`（上方程式碼） | 復原時舊版 daemon spawn 立即結束 → rc 2、`DAEMON_EXITED`／`install_recover`、`recovered_journal.ok:false`、journal 逐位元組不變、輸出無 `update` 報告、`daemon.handoff_stop` 只有第一次那 1 次且無 `daemon.stop`、current 仍 0.4.0 |
| `update_interrupt_injection_every_point_recovers_to_previous`（上方程式碼） | 對 `CRASH_POINTS` 每個點（每個 `JournalStep` 的 before／after＋步驟內點；`revert_to_previous:*` 以 pairing 失敗驅動）注入中斷後 `recover()`：無副作用點可無 journal、其餘必須 `ok:true`；入口、`current-release.json`、`config.yaml` 逐位元組回到原值，監管 `(enabled,running)==(true,true)`，`health.status.version=="0.4.0"`，journal 與 `snapshot_dir` 均刪除；每個點全程 RPC 序無 `daemon.stop`（R2-F6：`start_and_verify:*`／`revert_to_previous:*` 點的新版停止也只經交接） |
| `crash_points_cover_every_journal_step`（上方程式碼） | `CRASH_POINTS` 涵蓋 11 個 `JournalStep` × {before_effect, after_effect} 與三個步驟內點（含 R2-F6 的 `start_and_verify:started`） |
| `journal_write_failure_aborts_before_effect` | `with_fs_fault_replace_suffix("install-journal.json")`（journal 的 atomic replace 失敗）於 `snapshot_taken` → `INTERNAL_ERROR`／`update_journal`／`journal_write_failed`、`journal_step:"snapshot_taken"`、無 `set_enabled`、無 `daemon.stop`／`daemon.handoff_stop` |
| `rollback_keeps_current_state_same_schema` | 兩版 `state_schema` 相同 → `state.json` 內容維持 Current；`config.yaml` 回到切換前內容 |
| `update_release_then_rollback_keeps_released` | 更新後 COM1 `device release`（state.json 記 RELEASED）→ rollback → state.json 仍為 RELEASED，Previous daemon 啟動後 COM1 未被自動 attach |
| `rollback_refuses_incompatible_state_schema` | Previous manifest `state_schema` 與 Current 不同 → `INVALID_ARGS`／`rollback_verify`／`state_schema_incompatible`、無 stop |
| `rollback_reregisters_previous_task` | Current 期間 Task XML 被改（例如 Arguments 不同）→ rollback 後執行 `schtasks /Create /XML <backup>/registered.task.xml /TN serialwrap /F`，且其順序在 `config.yaml` 還原之後、`/Run` 之前 |
| `rollback_cross_mode_windows_task_to_on_demand` | Previous 為 on-demand、Current 為 windows-task → rollback 後 `schtasks /Delete /TN serialwrap /F` 已執行、config `supervision_mode` 為 `on-demand`、以 on-demand 內部 start 啟動 |
| `rollback_cross_mode_failure_recovers_current` | 上例中 Previous daemon 啟動失敗 → 回到 Current：Task 重新註冊並啟動、config 為 `windows-task` |
| `systemd_rollback_daemon_reload_after_unit_restore` | `TestEnv::posix()` systemd-user：unit 寫回後 `systemctl --user daemon-reload` 早於 `start` |
| `rollback_twice_rolls_forward` | 連續兩次 rollback → 回到原 Current |
| `rollback_not_cli_only` | rollback 後 `bin` 下 CLI 與 launcher、`current-release.json.dir`、監管定義與已註冊狀態全部指向 Previous |
| `update_start_failure_writes_failure_json` | 新 daemon `DAEMON_EXITED` → `failure.json` 含各步驟 detail 與 `returncode` |
| `update_on_demand_not_running_skips_daemon_start` | 原本 daemon 未跑 → start_and_verify 只驗 `--version` 與 manifest，不 spawn |
| `fresh_install_materializes_profiles_and_sets_windows_task` | 空白 install root＋`run_from_bundle(["setup"])` → `install` 報告全 ok；`profiles/default.yaml`、skill link 存在；config `supervision_mode: windows-task`、`socket_path: tcp://127.0.0.1:48700`；calls 含 `/Create /XML` 與 `/Run`；`current-release.json.previous` 為 null |
| `test_fresh_install_blackbox`（黑箱，`rust_only`） | 隔離 HOME＋空 install root＋stub PATH：bundle `setup --on-demand` → `daemon start` → `session list` ok；profiles／config 存在 |
| `test_update_rollback_ab_isolated`（黑箱，`rust_only`，`process_rpc`） | 以 `mkbundle.sh --dev --version-override 0.4.0`／`0.4.1` 產兩個自含 bundle（Task 17；兩者 `tree_sha256` 不同），在隔離 install root：安裝 0.4.0 → 更新 0.4.1 → `daemon status.version=="0.4.1"` → `setup --rollback` → `daemon status.version=="0.4.0"` 且 `--version` 為 `serialwrap 0.4.0`、`version_pairing.ok` |
| `test_update_busy_l15_zero_tx`（黑箱，`rust_only`，`posix_pty`，§0.4） | bridge＋DUT 模擬器上有背景命令進行中 → 更新被拒（`SESSION_BUSY`）；拒絕前後 DUT 端 `wire.ndjson` 無任何新增 TX |
| `test_update_file_push_after_last_probe_l15`（黑箱，`rust_only`，`posix_pty`，`process_rpc`；R1-F1） | 真 Python daemon（Stage 1 bundle）＋bridge：以 `SW_TEST_UPDATE_PAUSE_AFTER=safety_check` 讓 `setup` 在預檢後暫停（只在 `mkbundle.sh --dev` 以 `test-hooks` feature 編入；正式 bundle 不讀此 env），期間另一 client 開始 1 MiB `file push`，再放行 → `SESSION_BUSY`、`handoff:"refused"`；`daemon status.pid` 不變、WAL 無 shutdown 記錄；push 正常完成且 DUT 端 md5 正確；`current-release.json` 未變 |
| `revert_refuses_while_new_daemon_pushing`（上方程式碼；R2-F6，修正前必 FAIL） | 新版驗收 `pairing_mismatch`、步驟 7 對新版的交接回 `SESSION_BUSY`＋`ops:["file.push"]` → rc 2、頂層 `INTERNAL_ERROR`／`update_start_and_verify`／`pairing_mismatch`；`update.reverted:false`、`update.revert_pending:true`、`revert_error{error_code:"SESSION_BUSY",phase:"update_revert",handoff:"refused"}`；交接共 2 次、第 2 次 `reason:"revert"` 且晚於步驟 6 `service:start`、拒絕後 `set_enabled:true`；無 `daemon.stop`、無 `/End`；current 與兩個 `bin` 入口仍為 0.4.1、監管 `(enabled,running)==(true,true)`；journal 停在 `start_and_verify`（未推進到 `revert_to_previous`）、`snapshot_dir` 保留、`failure.json.revert_deferred==true`；之後 `recover()`（交接回 grant）→ `ok:true`、`from_step:"start_and_verify"`、`backup_used:true`、交接第 3 次 `reason:"revert"`、仍無 `daemon.stop`、全部回到原值且 `health.status.version=="0.4.0"` |
| `journal_recovery_refuses_while_new_daemon_flashing`（上方程式碼；R2-F6，修正前必 FAIL） | crash 於 `start_and_verify:after_effect`（新版已啟動並對外服務）後新版開始 flash：`recover()` → `ok:false`、`journal_kept:true`、`error_code:"FLASHING_BUSY"`、`detail.handoff:"refused"`、`detail.busy[0].ops==["flash"]`、交接恰多 1 次且 `reason:"revert"`；journal 逐位元組不變、無 `daemon.stop`、無 `/End`、current／入口仍 0.4.1、監管 `(true,true)`；`setup` 入口 → rc 2、`FLASHING_BUSY`／`install_recover`、`recovered_journal{ok:false,journal_kept:true,detail.handoff:"refused"}`、輸出無 `update`；flash 完成後 `recover()` → `ok:true`、無 `/End`、回到原值 |
| `revert_handoff_unsupported_new_daemon_keeps_journal`（R2-F6） | `setup --rollback`，Previous 為 P8 Task 14 之前的 Rust artifact：步驟 6 啟動後驗收 `smoke_failed`，`with_new_daemon_handoff_script(&["{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\"}"])` → `rollback.reverted:false`、`revert_pending:true`、`revert_error{error_code:"METHOD_NOT_FOUND",phase:"update_revert",probe:"daemon.handoff_stop",handoff_outcome:"refused"}`、hint 含 `serialwrap daemon status` 與 `serialwrap daemon stop`；無 `daemon.stop`（不得退回一般停止）、無 `/End`、journal 保留；之後模擬使用者明確 `serialwrap daemon stop`（Task 7 `Plain`，允許）使 daemon 結束 → `recover()` 判定新版確認不存在、零停止 RPC、`ok:true` 回到 Current |
| `revert_handoff_recv_timeout_no_hard_stop`（R2-F6） | 新版驗收 `wal_unhealthy`，步驟 7 交接 script `["<recv_timeout>"]` → `revert_error{error_code:"TIMEOUT",handoff_outcome:"unknown"}`、`revert_pending:true`；無 `/End`、無 `daemon.stop`、不切換、journal 保留；`recover_journal` 同情境（crash 於 `start_and_verify:after_effect`）→ `TIMEOUT`／`install_recover`、`detail.handoff_outcome:"unknown"`、journal 逐位元組不變 |
| `revert_new_daemon_rpc_down_process_alive_defers`（R2-F6） | 步驟 6 新版 `DAEMON_NOT_READY`（ping 持續 refused、`ProcScan` 仍回其 pid）→ 交接 connect 失敗 → `revert_error{error_code:"DEVICE_STILL_HELD",handoff_outcome:"refused",pids:[<new pid>]}`、`revert_pending:true`；不 `hard_stop`、不 `revert_entries`、journal 保留；`FakeProcScan::fail()` 時同樣暫緩（ProcScan 失敗不算確認不存在） |
| `rollback_revert_refuses_while_previous_daemon_pushing`（R2-F6） | `setup --rollback`：Previous daemon 啟動後驗收 `state_incompatible`、其交接回 `BUSY_PUSH` → 斷言同 `revert_refuses_while_new_daemon_pushing`（鍵為 `rollback`；`current-release.json` 與入口仍為 rollback 目標 0.4.0、尚未回到原 Current 0.4.1）；待 script 用盡 `recover()` 回到 0.4.1 |
| `test_revert_new_daemon_push_not_aborted_l15`（黑箱，`rust_only`，`posix_pty`，`process_rpc`；R2-F6） | 真 Python daemon（兩個 dev bundle）＋bridge：dev bundle 以 `SW_TEST_UPDATE_PAUSE_AFTER=start_and_verify:started`（新版已啟動、驗收前暫停）與 `SW_TEST_UPDATE_FORCE_VERIFY_FAIL=smoke_failed`（兩者皆只在 `test-hooks` feature 編入，正式 bundle 不讀）執行 `setup`；暫停期間另一 client 對新版開始 1 MiB `file push`，再放行 → `setup` exit 2、`revert_pending:true`、`revert_error.error_code=="SESSION_BUSY"`；新版 `daemon status.pid` 不變、其 WAL 無 shutdown 記錄、push 正常完成且 DUT 端 md5 正確；之後再跑 `setup` → 回退完成、`daemon status.version` 為舊版、`version_pairing.ok`。同 feature 另提供 `SW_TEST_UPDATE_CRASH_AT=<CRASH_POINTS 之一>`（以 `FaultPlan` 在該點讓行程以 137 結束，供 09 Task 14 黑箱的 journal 復原競態使用；`start_and_verify:started`（新版已啟動、驗收開始前）為步驟內點，PAUSE 與 CRASH 皆可用，並加入 `CRASH_POINTS`） |
| `test_update_push_after_freeze_zero_tx_l15`（黑箱，同上） | 預檢後放行交接、`daemon.handoff_stop` 已回 ok（daemon 凍結中）時另一 client 送 `file push` → 回 `FLUSHED_BY_SHUTDOWN`、`reason:"handoff_frozen"`；DUT 端 `wire.ndjson` 自凍結起無任何新增 TX、DUT 上不存在部分檔案；更新照常完成 |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_update` → `not implemented: P4-T12`。
- [ ] **步驟 3：實作**（主幹）

```rust
pub fn run_update(ctx: &mut Ctx, bundle: &Path, mode: &SupervisionMode, with_sudo: bool, force_requested: bool) -> UpdateReport {
    let root = resolve_install_root(ctx);
    let _lock = step!(r0, Lock, install_lock(ctx, &root));                                         // 持有到函式結束
    if let Some(rec) = recover_journal(ctx, &root) { if !rec.ok { return UpdateReport::recover_failed(rec); } r0.note_recovered(rec); }
    let mut j = Journal::begin(ctx, &root, "update", read_current_version(ctx, &root), manifest_version(bundle))?; // write_journal(Lock)
    let mut r = UpdateReport::new("update", read_current_version(ctx, &root), manifest_version(bundle));
    j.advance(ctx, &root, JournalStep::Verify)?;                                                    // 每個 advance：atomic_write＋fsync＋sync_dir，失敗即中止
    let vdir = step!(r, Verify, verify_schema_and_stage(ctx, &root, bundle));
    let ep = local_endpoint(ctx);
    j.advance(ctx, &root, JournalStep::SafetyCheck)?;
    let snap = step!(r, SafetyCheck, safety_check(ctx, &ep).note_force_ignored(force_requested)); // 無覆寫：§12.1 步驟 2；force 只記 detail
    let sup0 = query_supervisor(ctx, mode)?;                                                        // 尚未停用：原始 enabled／running
    let (orig, sdir) = persist_original_snapshot(ctx, &root, &sup0, &snap)?;                        // R1-F5：停止前持久化
    j.set_original(orig, sdir).advance(ctx, &root, JournalStep::SnapshotTaken)?;
    j.advance(ctx, &root, JournalStep::StopSupervisor)?;
    step!(r, StopSupervisor, stop_supervisor(ctx, mode, with_sudo, &ep, &snap, &j.original()));     // disable → graceful_stop_with(StopRpc::Handoff)：daemon 原子 busy 判定＋凍結＋token（無 force、不送一般 daemon.stop、Busy／Unconfirmed 不硬停）
    j.advance(ctx, &root, JournalStep::ConfirmReleased)?;
    if let Err(e) = confirm_released(ctx, &ep, &snap) { restore_enabled(ctx, &j.original()); return r.fail(ConfirmReleased, e); } // journal 保留
    let (backup, sw) = step_or_revert!(r, BackupAndSwitch, backup_and_switch(ctx, &root, &vdir, mode, &mut j)); // BackupStarted → BackupComplete → EntriesSwitching
    j.advance(ctx, &root, JournalStep::StartAndVerify)?;
    if let Err(e) = start_and_verify(ctx, mode, &snap, &vdir) { return revert_to_previous(ctx, r, backup, sw, &mut j, e); } // 用 j.original() 還原；先 stop_for_revert（只經 Handoff{reason:"revert"}），Deferred → revert_pending、journal 保留、不硬停（R2-F6）
    clear_journal(ctx, &root); r.ok()                                                               // 刪 journal 與 snapshot_dir
}
```

`contracts/error_codes.yaml`：不新增碼；`SESSION_BUSY`、`DEVICE_STILL_HELD`、`CHECKSUM_VERIFY_FAILED`、`INVALID_ARGS`、`TIMEOUT` 若缺 `cli_local` interface 則補上，`meaning` 附註本 Task 的 `phase` 值；SC-23 `status: IMPLEMENTED`，`tests/acceptance/p4_case_map.yaml` 把 `sc23-setup-rollback` 綁到本 Task 全部測試。

- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-cli --test mgmt_update
scripts/rust/win-test.sh --package sw-cli -- mgmt_update
SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox/test_cli_mgmt_update.py
python3 -m pytest -q tests/ && python3 -m policy_check --repo .
```

- [ ] **步驟 5：commit**：`feat(rust-cli): 實作完整組合首次安裝、更新與 setup --rollback`

**BLOCKED-OWNER：**
- Spike 6 實機回滾：在 L2 bench 與 OD-05 Windows 機台（OD-08：com0com DUT-side pair 或實體 USB-UART 直連 Windows）完成「更新→回滾」並證明真實串口被舊版 daemon 釋放、Previous daemon 重新取得（§12.1 末段），不只比對版本字串。
- §10.3 實際權限下（一般任務 Agent 身分）無法觸發 update／rollback 或替換版本檔的證據（見 Task 15）。

---

## Task 13：`doctor` 全部檢查原生與 `version_pairing`（SC-25）

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/<issue>-rust-p4-doctor.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/doctor_cmd.rs`、`crates/sw-cli/src/delegate.rs`（移除 `doctor`）
- Create: `crates/sw-cli/tests/mgmt_doctor.rs`、`tests/contract_blackbox/test_cli_mgmt_doctor.py`

**Interfaces:**
- Consumes：`SysEnv::{which, which_all, user_in_group, has_systemd, is_wsl, run}`、`ProcScan::serialwrapd_instances`、`RegistryView`（`SERIALCOMM_KEY`）＋`read_bt_ports`＋`exclude_bluetooth`（Task 2）、`endpoint_resolve::{resolve_default_endpoint_with_source, endpoint_alive, local_default_endpoint}`、`client::rpc_call`、`yaml11::safe_load`、`assets::PROFILE_ASSETS`（Task 10）、`install_layout::{locate_manifest, pairing_item}`（Task 11）。
- Produces：

```rust
pub struct Check { pub check: &'static str, pub ok: bool, pub detail: String, pub fix: Option<String>, pub advisory: bool, pub extra: PyDict }
// 一般項目輸出 {advisory, check, detail, fix, ok}；version_pairing 依 P1 輸出 {advisory, check, detail, mismatches, ok}（無 fix），以 fix=None＋extra 表示
pub const DOCTOR_ADVISORY_CHECKS: &[&str];       // 07 §7.1 Linux 集合
pub const DOCTOR_ADVISORY_CHECKS_WIN: &[&str];   // 07 §7.1 Windows 集合
pub fn run_doctor(ctx: &Ctx) -> Vec<Check>;      // 平台清單以 caps.path_flavor 選擇（Windows flavor → Windows 清單）
pub fn run_doctor_cmd(ns: &Namespace, ctx: &mut Ctx) -> i32;   // 恆 0
```

行為：
- 清單與順序：Linux 18 項（07 §7.2）、Windows 12 項；**只有 `locate_manifest` 找到 manifest 時**才在最後附加 `version_pairing`（P1 T18 第 4 點，雙邊相同；`cargo run` 開發模式與純 Python 安裝都不出現此項）。`version_pairing` 非 advisory。各項 ok 判準、detail、fix 字串逐字照 07 §7.3（含 `endpoint_reachable`、`wal_dir`、`wal_writable`、`profile_bootloader_prompts` 的所有分支）。
- `python`／`pyyaml`／`pyserial`：檢查對象改為 release manifest 的 daemon runtime（Rust CLI 本身不是 Python；登記為 SC-25 附註）：`python` → `run([runtime,"-I","-c","import sys;print('%d.%d.%d'%sys.version_info[:3])"], 5s)`，rc 0 → detail 為輸出 strip，ok = `>= 3.10`；`pyyaml` → `run([runtime,"-I","-c","import yaml;print(getattr(yaml,'__version__','已安裝'))"])`，rc≠0 → detail `未安裝`；`pyserial` 同法 `import serial`。找不到 manifest 或 runtime → 三項 ok=false，detail `無法定位 daemon Python runtime（<原因>）`，fix `重新安裝發佈組合（serialwrap setup）`。
- `profile_bootloader_prompts` 的出貨資產改讀內嵌 `PROFILE_ASSETS["default.yaml"]`（位元組與 `sw_core/assets/profiles/default.yaml` 相同，Task 10 已驗）。
- `devices`（Windows）：`registry.enum_values(SERIALCOMM_KEY)` 失敗 → detail `無法讀取 SERIALCOMM`；成功時 `exclude_bluetooth(serialcomm, read_bt_ports(registry), exclude_coms)`；`exclude_coms` 從 config.yaml 的 `windows.exclude_coms` 每次重讀（PLT-O-16 doctor 側行為）。
- `version_pairing`（雙邊 SC-25，逐字照 P1 L2422-2437）：`pairing_item(manifest, manifest_dir, cli_version, daemon_version)`；daemon 版本由 `health.status`(0.5 s，endpoint＝cfg socket 或 local default，與 `wal_dir` 同 seam)，不可達 → `daemon_version=None` → `ok:false`、`mismatches` 含 `daemon_unreachable`；`detail` 為 `"ok"` 或 `", ".join(mismatches)`；manifest 解析失敗 → `ok:false`、`detail="invalid release manifest: <key>"`、`mismatches:[]`。
- 輸出 `{"checks":[…],"ok":overall}`（`overall = all(ok or check in advisory)`），exit 恆 0。

- [ ] **步驟 1：寫失敗測試**（`crates/sw-cli/tests/mgmt_doctor.rs`）

```rust
mod support; use support::*;
#[test]
fn linux_check_order_and_advisory_stamp() {
    let t = TestEnv::posix().with_manifest_runtime_ok("3.12.7");
    let (rc, out, _) = t.run(&["doctor"]);
    assert_eq!(rc, 0);
    let names: Vec<String> = list(&json(&out), "checks").iter().map(|c| s(c.as_dict(), "check").to_string()).collect();
    assert_eq!(names, ["python","pyyaml","serialwrap_on_path","serialwrapd_on_path","other_serialwrap_installs","dialout",
        "serialwrap_minicom_on_path","jq_on_path","minicom_on_path","systemd","supervision_mode","single_daemon","endpoint_reachable",
        "wal_dir","wal_writable","profile_bootloader_prompts","devices","wsl_systemd","version_pairing"]);
    for c in list(&json(&out), "checks") {
        let d = c.as_dict();
        let expect_adv = sw_cli::doctor_cmd::DOCTOR_ADVISORY_CHECKS.contains(&s(d, "check"));
        assert_eq!(b(d, "advisory"), expect_adv, "{}", s(d, "check"));
        if b(d, "ok") && d.contains_key("fix") { assert_eq!(s(d, "fix"), ""); }
        if s(d, "check") == "version_pairing" { assert!(!d.contains_key("fix") && d.contains_key("mismatches")); }
    }
}
#[test]
fn overall_ok_ignores_advisory_failures_only() {
    let t = TestEnv::posix().with_manifest_runtime_ok("3.12.7").all_required_ok().with_which_missing("jq");
    assert!(b(&json(&t.run(&["doctor"]).1), "ok"));
    let t2 = TestEnv::posix().with_manifest_runtime_ok("3.12.7").all_required_ok().without_group("dialout");
    assert!(!b(&json(&t2.run(&["doctor"]).1), "ok"));
}
```

表列案例（每列一個測試；detail／fix 逐字照 07 §7.3）：

| 測試名 | 斷言 |
|---|---|
| `windows_check_order` | 12 項＋`version_pairing`，順序照 07 §7.2 Windows |
| `other_installs_single` | `which_all` 1 筆 → ok、detail `僅偵測到目前這份`、不呼叫 `--version` |
| `other_installs_multi_versions_differ` | 2 筆 `--version`（timeout 2.0）回不同 → ok false、detail `a=serialwrap 0.3.2；b=serialwrap 0.4.0` |
| `single_daemon_multi_open` | ProcScan 2 個 → detail `2 個 serialwrapd 在跑（偵測到多開）` |
| `endpoint_reachable_no_daemons` | detail `未偵測到執行中的 serialwrapd（on-demand 模式正常）；本 client 解析到 <ep>（來源：預設（SOCKET_PATH，受 SERIALWRAP_STATE_DIR/XDG 環境變數影響））` |
| `endpoint_reachable_tcp_always_alive_posix` | config `tcp://127.0.0.1:1`、有 daemon → ok（PLT-O-12 KEEP） |
| `endpoint_reachable_unreachable` | unix socket 不通 → ok false、detail 列出 daemon socket（排序、`、` 串接） |
| `wal_dir_mismatch` | shell `SERIALWRAP_WAL_DIR=/a`、daemon `wal_path=/b/raw.wal.ndjson` → ok false、detail `daemon 實際生效 WAL_DIR=/b，與 shell SERIALWRAP_WAL_DIR=/a 不一致` |
| `wal_writable_unhealthy_reasons` | `wal:{healthy:false,wal_dir_exists:true,wal_dir_writable:false,current_seq:5,wal_file_exists:false,write_failures:2,last_write_error:"E"}` → detail `WAL 目錄 <d> 異常：目錄不可寫；已寫過 5 筆紀錄但現行 WAL 檔不存在；append 失敗 2 次（最後一次：E）` |
| `wal_writable_recreated_suffix` | healthy＋`recreated_count:2` → detail 結尾 `；曾自癒重建 2 次（消失期間的紀錄無法復原）` |
| `bootloader_prompts_drift` | 線上 `brcm-template` 無 `bootloader_prompts` → ok false、detail `線上 <live> 的 brcm-template 缺 bootloader_prompts（出貨資產有）` |
| `bootloader_prompts_not_materialized` | 線上檔不存在 → ok true、detail `線上 profile 未物化（<live>），略過` |
| `devices_linux_counts_dangling` | by-id 目錄 2 個項（1 個 dangling）→ ok、detail `2` |
| `devices_windows_excluded_suffix` | serialcomm 3、kept 1 → detail `1（COM3）；排除 2（藍牙/exclude_coms）` |
| `devices_windows_registry_error` | `FakeRegistry::fail()` → detail `無法讀取 SERIALCOMM` |
| `python_runtime_missing_manifest` | 無 manifest → python／pyyaml ok false、detail `無法定位 daemon Python runtime（找不到 release manifest）`；且清單**不含** `version_pairing` |
| `version_pairing_daemon_down_is_unreachable` | 有 manifest、daemon 未跑 → `{"advisory":false,"check":"version_pairing","detail":"daemon_unreachable","mismatches":["daemon_unreachable"],"ok":false}`、整體 ok false |
| `version_pairing_mismatch_detail` | daemon 回 `0.3.2` → `mismatches==["daemon_version"]`、`detail=="daemon_version"`、ok false |
| `version_pairing_invalid_manifest` | manifest 缺 `pairing` → `detail=="invalid release manifest: pairing"` |
| `doctor_exit_always_zero` | 全部失敗仍 exit 0 |
| `test_doctor_ab_shared_checks`（黑箱 A/B） | 隔離環境下兩實作的前 18（Linux）項中，除 `python`／`pyyaml` 的 detail 外逐欄相同；無 manifest 時兩邊皆無 `version_pairing` |
| `test_doctor_ab_version_pairing_in_bundle`（黑箱 A/B） | 同一自含 bundle 內，Rust doctor 與 bundle 內 Python doctor（`py/bin/python3 -I -m sw_core.cli doctor`，P1 T18）的 `version_pairing` item 逐欄相同（daemon 未跑與跑兩種狀態） |

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-cli --test mgmt_doctor` → `not implemented: P4-T13`。
- [ ] **步驟 3：實作**：每個 check 一個函式 `fn check_<name>(ctx) -> Check`，`run_doctor` 依平台陣列依序呼叫並以 advisory 集合蓋章；字串常數集中在 `doctor_cmd.rs` 頂部，與 07 §7.3 逐列對照註解。`p4_case_map.yaml` 的 `sc25-release-manifest-version-pairing` 補 `version_pairing_*` 與兩個 A/B；`contracts/behavior_changes.yaml` SC-25 附註記錄 python／pyyaml／pyserial 檢查對象改為 daemon runtime。
- [ ] **步驟 4：執行並確認 PASS**：`cargo test -p sw-cli --test mgmt_doctor`、`scripts/rust/win-test.sh --package sw-cli -- mgmt_doctor`、黑箱兩實作。
- [ ] **步驟 5：commit**：`feat(rust-cli): 原生承接 doctor 並新增 version_pairing 檢查`

**BLOCKED-OWNER：** 測試團隊 Windows 工作站上的 `doctor` 全項實跑（實體 COM、EDR、真實 `windows-task` 監管狀態）。

---

## Task 14：終端類承接（console-attach／detach、`serialwrap-minicom` 端到端、Windows Telnet 提示）與委派集合清空

**Test author model:** haiku-xhigh
**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/<issue>-rust-p4-terminal.md`（`type: refactor`，`scope: rust-cli`）

**Files:**
- Modify: `crates/sw-cli/src/delegate.rs`（`pub const DELEGATED_GROUPS: [&str; 0] = [];`；保留機制與遞迴防護供回退）、`crates/sw-cli/src/commands/rpc_cmds.rs`（console-attach 輸出驗證，不改行為）
- Create: `crates/sw-cli/tests/mgmt_terminal.rs`、`tests/contract_blackbox/test_cli_mgmt_terminal.py`、`tests/contract_blackbox/test_cli_no_delegation.py`

**Interfaces:**
- Consumes：P3 的 `session console-attach/console-detach` 原生 RPC 路徑（CLI-11.08）、`assets::TOOL_ASSETS`（Task 10）、Task 7 `daemon start`、Task 6 `supervision-mode`。
- Produces：`pub const DELEGATED_GROUPS: [&str; 0] = [];`（沿用 P3 的陣列型別）；行為不新增（純承接驗證）。

行為：
- `session console-attach --selector X [--label L]`：`session.console_attach {"selector"[, "label"(truthy 才帶)]}`，45 s floor，輸出原樣（含 Windows daemon 回的 `console_endpoint`）；`console-detach --selector X --client-id C`：`{"selector","client_id"}`。
- `serialwrap-minicom`（物化的 `minicom_router.sh`）以 `SERIALWRAP_BIN=<Rust serialwrap>` 執行時，其呼叫的 `supervision-mode`（純文字）、`daemon start`、`session list`、`session attach`、`session console-attach --selector <com> --label minicom:$$`、結束時 `session console-detach --selector <com> --client-id <id>`（`minicom_router.sh` cleanup trap）全部由 Rust 原生處理；trap 在 SIGTERM／EXIT 時仍送 detach。
- 全部 20 個群組與 `--help`／`--version` 皆不委派。

- [ ] **步驟 1：寫失敗測試**

`tests/contract_blackbox/test_cli_no_delegation.py`：

```python
import pytest
GROUP_PROBES = [
    ["daemon", "status"], ["service", "status"], ["supervision-mode"], ["remote", "status"], ["benches"],
    ["connect", "NOPE"], ["setup", "--on-demand"], ["doctor"],
    ["session", "console-attach", "--selector", "COM0"], ["session", "console-detach", "--selector", "COM0", "--client-id", "c1"],
]
@pytest.mark.parametrize("argv", GROUP_PROBES, ids=lambda a: " ".join(a))
@pytest.mark.rust_only
def test_every_mgmt_group_is_native(env, stub_path, no_delegation, argv):
    from bb_harness import run_cli
    r = run_cli(argv, env=env)
    assert r.returncode != 97, r.stderr
```

`tests/contract_blackbox/test_cli_mgmt_terminal.py`（關鍵案例）：

```python
def test_minicom_wrapper_attach_detach_via_rust_cli(mgmt_bundle, live_daemon, stub_path, stub_log, run_wrapper, tmp_path):
    # 全部為 Task 1 mgmt_fixtures；live_daemon 以 P3 PyDaemon＋FakeTarget 組成
    wrapper = mgmt_bundle.materialized_bin / "serialwrap-minicom"
    env = {"SERIALWRAP_BIN": str(mgmt_bundle.cli), "MINICOM_BIN": str(stub_path / "minicom"),
           "MINICOM_AUTO_CAPTURE": "0", "SW_STUB_MINICOM_HOLD_S": "1", "SERIALWRAP_AUTO_START_DAEMON": "0"}
    p = run_wrapper(wrapper, ["COM0"], env)
    assert p.returncode == 0, p.stderr
    rows = stub_log()
    minicom = [r for r in rows if r["tool"] == "minicom"]
    assert len(minicom) == 1 and "-D" in minicom[0]["argv"]
    vtty = minicom[0]["argv"][minicom[0]["argv"].index("-D") + 1]
    assert vtty.startswith("/dev/pts/")
    assert live_daemon.console_list("COM0") == []          # wrapper 結束後已 detach
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `console_attach_label_omitted_when_empty`（Rust，stubd） | `--label ""` → 送出 params 無 `label` 鍵 |
| `console_attach_long_timeout_floor` | 無 `--timeout` → socket timeout 45.0 |
| `console_attach_windows_endpoint_passthrough` | stubd 回 `console_endpoint:"127.0.0.1:50123"` → stdout 原樣含該鍵 |
| `delegated_groups_empty` | `sw_cli::delegate::DELEGATED_GROUPS.is_empty()`（型別 `[&str; 0]`） |
| `delegate_mechanism_still_guards_recursion` | 以測試注入 1 個群組時 `SERIALWRAP_DELEGATE_DEPTH=2` → `delegation recursion detected` |
| `test_minicom_wrapper_sigterm_still_detaches`（黑箱，`posix_pty`） | wrapper 執行中送 SIGTERM → daemon `console_list` 空 |
| `test_minicom_wrapper_auto_start_daemon_on_demand`（黑箱） | `SERIALWRAP_AUTO_START_DAEMON=1`、無 daemon → wrapper 經 Rust `daemon start` 起 daemon 後 attach 成功 |
| `test_every_mgmt_group_is_native`（10 參數） | 皆非 97，`no_delegation` marker 不存在 |

- [ ] **步驟 2：執行並確認 FAIL**：Rust bundle 跑 `test_cli_no_delegation.py` → 在前面 Task 未完成時多數 exit 97；`delegated_groups_empty` 斷言失敗。
- [ ] **步驟 3：實作**：清空 `DELEGATED_GROUPS`；保留 `maybe_delegate` 函式（集合為空時直接回 None），並在 `delegate.rs` 註記「Stage 1B 後委派集合為空；機制保留僅供緊急回退，回退須另登記 behavior change」。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test -p sw-cli --test mgmt_terminal
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox/test_cli_no_delegation.py tests/contract_blackbox/test_cli_mgmt_terminal.py
```

- [ ] **步驟 5：commit**：`refactor(rust-cli): 清空 Stage 1A 委派集合並承接終端類操作`

**BLOCKED-OWNER：** Windows 測試團隊以 TeraTerm／PuTTY 連 `console_endpoint` 的實機人機操作（Telnet console 屬 daemon 能力，Stage 1 仍為 Python daemon；本 Phase 只驗 CLI 傳遞）。

---

## Task 15：Agent 權限模型文件與驗證工具（§10.3）、對外文件同步

**Test author model:** haiku-xhigh
**Implementer model:** agy-gemini-3.8-flash-high（文件為主；`verify_agent_perms.py` 屬安全敏感，審查 A 須專審其檔案副作用：只能觸碰 sentinel 檔，對真實 target 不得 rename／replace／delete）
**Changelog fragment:** `changelog.d/<issue>-rust-p4-agent-permission-docs.md`（`type: docs`，`scope: rust-refactor`）

**Files:**
- Create: `docs/rust-refactor/deploy/agent-permission-model.md`、`docs/rust-refactor/deploy/windows-install-update-rollback.md`、`scripts/rust/verify_agent_perms.py`、`tests/test_rust_agent_perm_verifier.py`、`tests/test_rust_deploy_docs.py`
- Modify: `README.md`（English 與繁體中文兩段同步：Rust bundle 安裝、`windows-task`、`daemon restart`、`supervision-mode set`、`setup --rollback`、首次安裝與更新的錯誤處置；`remote stop` 與 BC-05 Windows remote、OD-12 信任範圍段落 **P1 T14 已改**（`02-stage0-python-bc.md` L1921），本 Task 只核對並補 Rust bundle 相關敘述）、SKILL 受管區段（以 P0 交付的受管區段產生器依 `contracts/error_codes.yaml` 重生，涵蓋本 Phase 擴充 `cli_local` interface 與 `phase` 附註的既有碼，OD-06）
- 只驗證不修改：`sw_core/assets/skill/SKILL_WINDOWS.md`（「本期不支援 remote」段落已由 P1 T14 移除）；若 P1 尚未合併則本 Task 的對應測試列「證據不足」，**不得**在本 Task 代改 P1 範圍

**Interfaces:**
- Consumes：Task 11 的佈局路徑定義、Task 12 的更新流程、`contracts/error_codes.yaml`。
- Produces：

```text
scripts/rust/verify_agent_perms.py --targets <path>... [--json-out <file>]
  對真實 target **只做「開啟後立即關閉」與 access 查詢**，絕不 rename／replace／delete／寫入真實檔；會改變目錄內容的探測一律用同目錄 sentinel：
    write：以附加模式開啟 target（O_APPEND，不寫入任何 byte、不 truncate，立即關閉；開啟成功即判 allowed）；
    rename：POSIX 依父目錄 os.access(W_OK|X_OK) 且（無 sticky bit 或 target 屬於目前身分）判定；
            Windows 以 ctypes CreateFileW(target, DELETE, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_EXISTING) 能否開啟判定（rename 需要 DELETE 權限；開啟即關閉）；
    delete：判定規則同 rename（不實際刪除）；
    replace：= rename 判定 AND 父目錄可建立檔案（以 parent_dir_write 的 sentinel 結果）；
    parent_dir_write：在父目錄建立 .permprobe-<pid>-<rand> sentinel、對 sentinel 依序做 write／rename（→ .permprobe-<pid>-<rand>.r）／replace／delete，全部對 sentinel 執行。
  sentinel 以 try/finally 清除；啟動時先清掉同目錄超過 1 小時的 .permprobe-* 殘留。輸出 JSON：
  {"identity": "<user or DOMAIN\\user>", "platform": "...", "results": [{"target": str, "op": str, "allowed": bool, "error": str|null}], "blocked": bool}
  blocked = 所有 op 皆 allowed=false。exit 0（blocked）／3（有任何 allowed）／2（參數錯誤）。
```

文件 `agent-permission-model.md` 必含章節（以 `tests/test_rust_deploy_docs.py` 檢查標題存在）：`## 1. 身分清單`（一般任務 Agent、安裝／更新者、daemon 執行身分；由部署負責人填表，表格欄位：身分、帳號、取得方式、允許操作）、`## 2. 受保護路徑`（`%LOCALAPPDATA%\Programs\serialwrap\versions\`、`…\bin\`、`…\backups\`、`…\supervisor\` 與其父目錄；Linux `${DATA_DIR}/versions/`、`~/.local/bin/serialwrap{,d}`）、`## 3. Windows ACL 設定`（`icacls` 指令：移除一般 Agent 身分的 `(W)`／`(M)`／`(D)`、保留 `(RX)`）、`## 4. Linux 權限設定`（安裝者擁有、Agent 身分只讀）、`## 5. 同身分部署的限制`（若 Agent 與安裝者同一帳號，只能宣稱流程約束，不得宣告已完成權限阻斷，§10.3 原文）、`## 6. 驗證程序`（`verify_agent_perms.py` 以 Agent 身分執行、預期 `blocked:true`、證據保存格式）、`## 7. 信任範圍`（OD-12：整機本機使用者可信；`remote` 經 ssh 時延伸至通過 ssh 驗證者；不得宣稱同使用者隔離）。

- [ ] **步驟 1：寫失敗測試**（`tests/test_rust_agent_perm_verifier.py`）

```python
import json, os, stat, subprocess, sys
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rust" / "verify_agent_perms.py"

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 會繞過權限位元")
def test_readonly_tree_reports_blocked(tmp_path):
    vdir = tmp_path / "versions" / "0.4.0"
    vdir.mkdir(parents=True)
    exe = vdir / "serialwrap"
    exe.write_bytes(b"x")
    exe.chmod(0o555)
    vdir.chmod(0o555)
    (tmp_path / "versions").chmod(0o555)
    try:
        p = subprocess.run([sys.executable, str(SCRIPT), "--targets", str(exe), str(vdir)], capture_output=True, text=True)
        rep = json.loads(p.stdout)
        assert p.returncode == 0 and rep["blocked"] is True
        assert {r["op"] for r in rep["results"]} >= {"write", "rename", "replace", "parent_dir_write"}
        assert exe.read_bytes() == b"x"
    finally:
        (tmp_path / "versions").chmod(0o755); vdir.chmod(0o755); exe.chmod(0o755)

def test_writable_tree_reports_not_blocked_and_restores(tmp_path):
    f = tmp_path / "serialwrap"
    f.write_bytes(b"orig")
    p = subprocess.run([sys.executable, str(SCRIPT), "--targets", str(f)], capture_output=True, text=True)
    rep = json.loads(p.stdout)
    assert p.returncode == 3 and rep["blocked"] is False
    assert f.read_bytes() == b"orig"
    assert not list(tmp_path.glob("*.permprobe*"))
```

表列案例：

| 測試名 | 斷言 |
|---|---|
| `test_bad_args_exit2` | 無 `--targets` → exit 2 |
| `test_json_out_file_written` | `--json-out` 檔內容與 stdout 相同 |
| `test_deploy_doc_has_required_sections`（`test_rust_deploy_docs.py`） | 7 個章節標題皆存在 |
| `test_deploy_doc_never_claims_same_user_isolation` | 兩份 deploy 文件與 README 不含「同使用者隔離」，除非同一行含「不得宣稱」 |
| `test_readme_bilingual_mentions_new_commands` | English 與繁中段落都含 `daemon restart`、`supervision-mode set`、`setup --rollback`、`windows-task` |
| `test_skill_windows_no_remote_not_supported_claim` | `SKILL_WINDOWS.md` 不含 `本期不支援 serialwrap remote`（P1 已改：本測試初次即 PASS，依 §0.1 附突變檢查證明會轉紅） |
| `test_error_codes_have_skill_entries` | P0 的 OD-06 規則檢查器對 `contracts/error_codes.yaml` 全碼通過；本 Phase 無新增碼（`test_rust_stage1b_registration.py` 另驗） |
| `test_verifier_never_touches_real_target` | 對可寫 target 執行後：target 的 inode、mtime、內容皆不變；目錄中無 `.permprobe*` 殘留 |
| `test_verifier_interrupted_leaves_no_real_change` | 以 `SIGKILL` 在 sentinel 探測中途終止 → target 不變；再次執行會清掉舊 sentinel |

- [ ] **步驟 2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_rust_agent_perm_verifier.py tests/test_rust_deploy_docs.py --runxfail` → 腳本與文件不存在（測試以 `xfail(strict=True, reason="P4-T15 紅燈")` 標示，`--runxfail` 用來確認真的紅；已由 P1 達成的斷言依 §0.1 做突變檢查）。
- [ ] **步驟 3：實作**：撰寫兩份文件（繁體中文）、驗證腳本（stdlib-only，Windows 身分以 `os.environ["USERDOMAIN"]+"\\"+os.environ["USERNAME"]` 取得）、README 雙語段落、重生 SKILL 受管區段；移除本 Task 測試的 xfail marker。
- [ ] **步驟 4：執行並確認 PASS**：`python3 -m pytest -q tests/`、`cargo test -p sw-cli`（skill golden）、`python3 -m policy_check --repo .`（R-16／R-18／R-22）。
- [ ] **步驟 5：commit**：`docs(rust-refactor): 新增 Agent 權限模型與 Windows 安裝更新回滾文件`

**BLOCKED-OWNER：**
- §10.3：部署負責人填寫身分清單（一般任務 Agent、安裝／更新者的實際帳號與有效權限）。
- 在測試團隊 Windows 工作站以一般任務 Agent 實際身分執行 `verify_agent_perms.py`，取得 `blocked:true` 證據；若兩者同帳號，由擁有者決定接受「流程約束」宣告。

---

## Task 16：Stage 1B 整合：全群組黑箱 A/B、L1.5、效能 gate、Windows 原生 L1、契約與案例完成度核對

**Test author model:** agy-gemini-3.8-flash-high
**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/<issue>-rust-p4-stage1b-exit.md`（`type: feat`，`scope: rust-cli`）

**Files:**
- Create: `scripts/rust/bench_cli_roundtrip.py`、`scripts/rust/check_no_onefile_extract.py`、`tests/test_rust_stage1b_registration.py`、`tests/test_rust_bench_tools.py`、`tests/contract_blackbox/test_cli_mgmt_ab_suite.py`、`tests/contract_blackbox/test_cli_mgmt_l15.py`
- Modify: `tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py`（確認讀 `SERIALWRAP_CLI_EXE`，P2／P3 已改則只驗證）
- 不修改 `00-master-plan.md`（該表無狀態欄）；Phase 狀態寫在 PR 描述與 `target/p4-evidence/summary.json`

**Interfaces:**
- Consumes：本 Phase 全部；P2 的 A/B 比較器、`func-test/runner.py --mode bridge`（L1.5 外部 bridge 模式，讀 `SERIALWRAP_CLI_EXE`）、`tests/acceptance/cases.lock.json` 與 §0.4 的 `p4_required.lock.json`；`contracts/` 正規化規則。
- Produces：

```text
scripts/rust/bench_cli_roundtrip.py --cli <exe> --endpoint <ep> [--n 200] [--warmup 20] [--platform linux|windows] [--json-out <f>] [--cli-only-stub]
  固定程序：先跑 warmup 次（不計），再收 n 個「有效樣本」；每個樣本＝以 subprocess 啟動 `<exe> --endpoint <ep> daemon status`
  到子行程 exit 的單調時間（time.perf_counter_ns，計時邊界：Popen 呼叫前 → wait() 返回後），stdout 必須是 ok:true 的 JSON 且 exit 0，
  否則該次記為 failed_samples，不得計入分位數；failed_samples>0 → 結果 ok:false（量測失敗，不輸出 PASS）。
  分位數：對排序後樣本以「nearest-rank」法（rank = ceil(p/100 × n)，1-based）取 p50／p95／p99。
  gate（§1.2）：linux ≤2／3.5／5 ms、windows ≤5／10／15 ms；任一超標 → gate:"FAIL"；非標準量測環境（§1.1.1 manifest 欄位不符）→ gate:"NOT_STANDARD_ENV"（不得標 PASS）。
  --cli-only-stub：改對 P3 `bb_stubd`（立即回覆的 health.status）量測，只作歸因參考，不進 gate。
  輸出 {"cli","n","warmup","platform","p50_ms","p95_ms","p99_ms","samples_ms":[...],"failed_samples","gate","ok","env":{量測 manifest 欄位（§1.1.1）}}。
scripts/rust/check_no_onefile_extract.py --cli <exe> [--runs 100]
  每次執行前把 TMP／TEMP／TMPDIR 指向全新空目錄 D_i，並在 CLI 行程存活期間以檔案系統事件監看（Linux inotify、Windows ReadDirectoryChangesW，皆以 ctypes／stdlib 實作）
  遞迴觀察 D_i 與系統預設暫存根（%TEMP% 或 /tmp）；記錄任何「建立」事件（不限名稱、含建立後立即刪除者），再加執行前後快照比對。
  輸出 {"runs","created":[{"run","path","kind"}],"leftover":[...],"ok":bool}；created 或 leftover 非空 → ok:false。
```

- [ ] **步驟 1：寫失敗測試**

`tests/test_rust_stage1b_registration.py`：

```python
import json, yaml
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
P4_CHANGES = ["SC-18", "SC-20", "SC-21", "SC-22", "SC-23", "SC-24", "SC-25", "SC-27", "DF-05", "DF-07", "DF-10", "DF-11", "DF-12", "DF-13", "DF-16", "BC-05"]

def test_stage1b_behavior_changes_implemented_with_frozen_case_ids():
    bc = {e["id"]: e for e in yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))["changes"]}
    cmap = {c["case_id"]: c for c in yaml.safe_load((ROOT / "tests/acceptance/p4_case_map.yaml").read_text(encoding="utf-8"))["cases"]}
    for cid in P4_CHANGES:
        assert bc[cid]["status"] in ("IMPLEMENTED", "VERIFIED"), cid          # P0 CHANGE_STATUS 大寫
        for case in bc[cid]["case_ids"]:                                        # P0 凍結，不得改名
            assert case in cmap and cmap[case]["tests"], f"{cid}:{case} 無承接測試"

def test_stage1b_adds_no_error_codes():
    codes = {e["code"] for e in yaml.safe_load((ROOT / "contracts/error_codes.yaml").read_text(encoding="utf-8"))["codes"]}
    for forbidden in ["DAEMON_STILL_RUNNING", "ROLLBACK_UNAVAILABLE", "UPDATE_BUSY", "UPDATE_VERIFY_FAILED", "UPDATE_START_FAILED"]:
        assert forbidden not in codes
    base = set(json.loads((ROOT / "tests/acceptance/p4_required.lock.json").read_text(encoding="utf-8"))["error_codes_at_freeze"])
    assert codes == base, "本 Phase 不得新增或刪除錯誤碼（§3.1、§9.2）"

def test_no_delegated_groups_left_in_rust_source():
    src = (ROOT / "crates/sw-cli/src/delegate.rs").read_text(encoding="utf-8")
    assert "pub const DELEGATED_GROUPS: [&str; 0] = [];" in src

def test_p4_required_cases_have_passing_evidence():
    import subprocess, sys
    p = subprocess.run([sys.executable, str(ROOT / "scripts/rust/check_p4_cases.py"), "--check"], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
```

（`test_p4_required_cases_have_passing_evidence` 讀 §0.4 凍結集合；在 BLOCKED-OWNER 證據補齊前，`necessity: required` 且 `layer ∈ {L2, L3, manual}` 的案例以 `evidence_status: BLOCKED-OWNER` 列出並使檢查回 exit 2「證據不足」——本測試對 exit 2 標 `xfail(strict=True, reason="BLOCKED-OWNER 證據待補")`，不得改為 PASS；L1／L1.5 案例缺證據則 exit 1 直接 FAIL。）

`tests/contract_blackbox/test_cli_mgmt_ab_suite.py`：`AB_CASES` **由 `tests/acceptance/p4_required.lock.json` 讀取**（凍結於紅燈 commit，§0.1），不得以 pytest collection 反推；對每個案例以 P2 比較器在「Python CLI＋Python daemon」與「Rust CLI＋Python daemon」兩輪依序執行（先停 A、確認資源釋放、再起 B，新模擬器實例），斷言比較器結果 `equal`；`rust_only` 案例只跑 B 並驗絕對契約。凍結清單中任一案例的測試節點不存在或被 SKIP（未登記）→ FAIL。

`tests/contract_blackbox/test_cli_mgmt_l15.py`（L1.5，P2 bridge＋DUT 模擬器，`posix_pty`；§0.4 不變量）：`test_autostart_l15_tx_once`（POSIX 以 `caps` 覆寫 env 模擬不可行，故本檔只收 Linux 可跑者；Windows 版本在 `win-blackbox.ps1` 原生執行）、`test_update_busy_l15_zero_tx`、`test_update_file_push_after_last_probe_l15`、`test_update_push_after_freeze_zero_tx_l15`（R1-F1，見 Task 12 表）、`test_reject_conflict_l15_zero_tx`（`DAEMON_ALREADY_RUNNING_ELSEWHERE` 時 DUT 無 TX）、`test_restart_no_stale_console_replay`（restart 前 human console 緩衝未送出的行，新 daemon 起來後 DUT RX 中不出現）、`test_restart_no_reopen_before_handoff`（新 daemon 開 tty 時舊 daemon 的 fd 已關：bridge `control.ndjson` 中舊 holder `closed` 早於新 holder `opened`）。

`tests/test_rust_bench_tools.py` 表列案例：

| 測試名 | 斷言 |
|---|---|
| `test_bench_script_outputs_percentiles` | 對 stub CLI（`python -c` 印 `{"ok":true}` 並 exit 0）n=10 → JSON 含 `p50_ms`≤`p95_ms`≤`p99_ms`、`samples_ms` 長度 10、`failed_samples==0` |
| `test_bench_nearest_rank_quantiles` | 注入固定樣本 1..100 ms → p50=50、p95=95、p99=99 |
| `test_bench_failed_runs_not_counted` | stub CLI 交替 exit 1 → `failed_samples>0`、`ok:false`、`gate` 不為 `PASS` |
| `test_bench_gate_thresholds` | 注入 linux 樣本 p95=3.6 ms → `gate:"FAIL"`；windows p50=4.9／p95=9.9／p99=14.9 → `gate:"PASS"`（標準環境欄位齊備時）；欄位不齊 → `NOT_STANDARD_ENV` |
| `test_no_onefile_checker_flags_new_dir` | 假 CLI 每次建立 `_MEItest<i>` → `ok:false`、`leftover` 長度 = runs |
| `test_no_onefile_checker_flags_create_then_delete` | 假 CLI 建立 `x.exe` 後立即刪除再退出 → `ok:false`、`created` 含該路徑 |
| `test_no_onefile_checker_redirects_tmp` | 假 CLI 寫到 `$TMPDIR/anything` → 被偵測（名稱不限 `_MEI*`） |
| `cargo_workspace_tests_windows_native` | `scripts/rust/win-test.sh --package sw-pal --package sw-cli` 報告 `failed==0`（ignored 不計 PASS） |

- [ ] **步驟 2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_rust_stage1b_registration.py tests/test_rust_bench_tools.py --runxfail` → 在各 Task 尚未更新登記時失敗；腳本不存在（紅燈期以 `xfail(strict=True, reason="P4-T16 紅燈")` 標示）。
- [ ] **步驟 3：實作**：兩支腳本（stdlib-only＋ctypes）；A/B 套件與 L1.5 套件；`check_p4_cases.py --check` 整合所有證據寫 `target/p4-evidence/summary.json`（每個 case：結果、證據檔、commit、artifact sha256）。
- [ ] **步驟 4：執行並確認 PASS**

```bash
cargo test --workspace && cargo build --workspace --release
scripts/rust/win-test.sh --package sw-pal --package sw-cli
SW_BUNDLE=$(scripts/rust/mkbundle.sh --dev --delegate-sentinel --print-dir)
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap SERIALWRAP_AB_LABEL=rust-1b python3 -m pytest -q tests/contract_blackbox
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap python3 -m pytest -q tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py
SERIALWRAP_CLI_EXE=$SW_BUNDLE/bin/serialwrap python3 func-test/runner.py --mode bridge --selftest-report <P2 Task 8 selftest 報告> --report-dir target/p4-evidence/l15-linux   # P2 既有 13 case＋11 L1.5 scenario 以 Rust CLI 執行
scripts/rust/win-blackbox.sh                                                                                 # P3 Task 17 入口（staging 需加入本 Phase 的 tests/contract_blackbox 與 Task 17 Windows dev bundle）；含 Windows 版 test_autostart_l15_tx_once
python3 scripts/rust/bench_cli_roundtrip.py --cli $SW_BUNDLE/bin/serialwrap --endpoint <隔離 daemon socket> --platform linux --json-out target/p4-bench-linux-wsl2.json
python3 scripts/rust/bench_cli_roundtrip.py --cli $SW_BUNDLE/bin/serialwrap --endpoint <stubd socket> --cli-only-stub --json-out target/p4-bench-cli-only.json
python3 scripts/rust/check_no_onefile_extract.py --cli $SW_BUNDLE/bin/serialwrap --runs 100
python3 scripts/rust/check_p4_cases.py --check
python3 scripts/rust/check_cfg_boundary.py && python3 -m pytest -q tests/ && python3 -m policy_check --repo .
```

本環境量得的 WSL2 數值只記為「Linux／WSL2 開發參考」（`gate:"NOT_STANDARD_ENV"`），報告中明確標示環境，不填入 §1.2 正式欄位。P3 沿用的 Stage 1A 證據（資料面 A/B、L1.5）只在其 artifact sha256 與本次 bundle 的 `components.cli.sha256` 相同時才可沿用，否則以本次 bundle 重跑。

- [ ] **步驟 5：commit**：`test(rust-cli): 完成 Stage 1B 黑箱 A/B、L1.5 與量測工具`

**BLOCKED-OWNER：**
- §1.2 CLI 暖快取往返 p50／p95／p99 在 §1.1.1 標準量測環境（Linux 原生、Windows OD-05 機台原生 artifact）正式量測（`bench_cli_roundtrip.py` gate 必須為 `PASS`）；Windows 連續 100 次 CLI 不產生解壓檔案或目錄的正式證據；EDR 無新增攔截證據（查詢權限不足時列證據不足）。
- §11.6 Stage 1B L2：以 Rust CLI 作為 pinned `serialwrap_exe` 在 live bench 跑全部適用 TestPilot `serialwrap_regression` 案例，含已授權破壞集。
- §11.6 Stage 1B L3：`realhw` P0＋P1＋remote 以 Rust CLI 執行。
- §11.6／OD-10：Rust CLI＋Python daemon 混合組合 48 小時長跑（至少 Windows 生產目標環境；bench 依序排程，約佔 3 天）——程序與 gate 判定沿用 P2 `longrun_acceptance.yaml`，本 Phase 只把 Rust CLI artifact 填入 manifest。
- Spike 2（>72 h `windows-task` 監管，程序見 Task 18）、Spike 6（混合版本與實機回滾）與 OD-08 原生 Windows COM handle 證據（動到 PAL 的 PR 必附）。
- 正式 release artifact 於 Windows 自架 runner 原生 build/link 與簽署前檢查（`objdump -p` 匯入 DLL 白名單、launcher Subsystem 為 Windows GUI）。

---

## Task 17：`sw-daemon` launcher、`yaml11::emit`、`bench_registry` 與自含式 dev bundle（P3 明列為 P4 範圍者）

**Test author model:** haiku-xhigh
**Implementer model:** codex-gpt-6-luna-max（launcher 的 Windows Job Object 與 PyYAML emitter 移植屬平台／高風險）
**Changelog fragment:** `changelog.d/<issue>-rust-p4-launcher-bundle.md`（`type: feat`，`scope: rust-daemon`）

**Files:**
- Create: `crates/sw-daemon/src/launcher.rs`；Modify: `crates/sw-daemon/src/main.rs`（P3 空殼 → 呼叫 `launcher::run`；檔頭 crate 屬性 `#![windows_subsystem = "windows"]`——這是 crate 屬性不是 `cfg`，非 Windows target 忽略）
- Modify: `crates/sw-pal/src/process.rs`、`crates/sw-pal/src/process/windows.rs`（`exec_or_wait_opts`）
- Create: `crates/sw-proto/src/yaml11/emit.rs`、`crates/sw-proto/src/bench_registry.rs`、`crates/sw-proto/src/digest.rs`（sha2 包裝，供 sw-cli 使用）
- Create: `scripts/rust/goldens/gen_yaml_golden.py`、`crates/sw-proto/tests/golden/yaml_emit.ndjson`、`crates/sw-proto/tests/yaml_emit.rs`、`crates/sw-proto/tests/bench_registry.rs`、`crates/sw-daemon/tests/launcher.rs`
- Modify: `scripts/rust/mkbundle.sh`（自含 `py/`、launcher、`--version-override`）、`crates/sw-cli/src/lib.rs`（`--version` 字串來源）、`tests/test_rust_mkbundle.py`（P3 檔，追加案例）

**Interfaces:**
- Consumes：P3 `ProcessCtl::exec_or_wait`、`parse_manifest`、`yaml11::safe_load`；arch §6.1 launcher 模式、§10.1 manifest 欄位。
- Produces：

```rust
// sw-daemon/src/launcher.rs
pub fn launcher_argv(manifest_dir: &Path, m: &ReleaseManifest, argv_tail: &[OsString]) -> Result<Vec<OsString>, LauncherError>;
// implementation=="python"：entry（相對 manifest 目錄轉絕對）＋argv_tail；entry 尾段為 <abs>/py/serialwrapd.py（multi_open 規則 2）
pub fn run(argv: Vec<OsString>, p: &sw_pal::Platform) -> i32;   // 定位 manifest（current_exe 的上兩層）→ launcher_argv → exec_or_wait_opts
pub enum LauncherError { ManifestNotFound(String), ManifestInvalid(String), UnsupportedImplementation(String) }
// 失敗：stderr 一行 `serialwrapd: <原因>` 後 exit 2（不印 JSON；daemon 非 CLI 介面）
// sw-pal/src/process.rs
pub struct ExecOpts { pub no_window: bool, pub kill_on_close: bool }
fn exec_or_wait_opts(&self, argv: &[OsString], env_add: &[(String, String)], o: ExecOpts) -> PalError;
// POSIX：同 exec_or_wait（execve；opts 忽略）。Windows：CreateProcessW(CREATE_SUSPENDED | (no_window? CREATE_NO_WINDOW))
//   → AssignProcessToJobObject(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE when kill_on_close) → ResumeThread → WaitForSingleObject → exit(child code)
// sw-proto/src/yaml11/emit.rs
pub fn safe_dump(v: &YValue, allow_unicode: bool, sort_keys: bool) -> String;   // PyYAML 6 `safe_dump(default_flow_style=False)` 位元組相同
// 移植 Emitter.analyze_scalar／choose_scalar_style（plain／single／double 判定、需引號的 bool/null/int/float/timestamp 字樣、
// 前後空白、特殊字元、`: `／` #`、indicator 開頭、多行、非可印字元與 allow_unicode）、Representer（dict 排序、list 縮排、None→null、bool→true/false）
// sw-proto/src/bench_registry.rs（07 §9.1 逐字）
pub struct BenchEntry { pub code: String, pub target: String, pub remote_socket: Option<String>, pub remote_port: Option<u16>,
                        pub local_port: u16, pub ssh_opts: Vec<String>, pub autossh: bool }
pub fn load_benches(text: &str) -> Result<BTreeMap<String, BenchEntry>, BenchConfigError>;   // 訊息逐字照 07 §9.1（INVALID_BENCH_CONFIG）
pub fn resolve(benches: &BTreeMap<String, BenchEntry>, code: &str) -> Result<&BenchEntry, BenchConfigError>;   // UNKNOWN_BENCH_CODE `unknown bench code: <code>`
// sw-proto/src/digest.rs
pub fn sha256_hex(data: &[u8]) -> String;  pub struct Sha256Stream; impl Sha256Stream { pub fn new() -> Self; pub fn update(&mut self, b: &[u8]); pub fn hex(self) -> String; }
// sw-cli --version
pub const CLI_VERSION: &str = match option_env!("SW_BUNDLE_VERSION") { Some(v) => v, None => env!("CARGO_PKG_VERSION") };   // 編譯期常數，無 build.rs
```

`mkbundle.sh --dev` 改為**自含**（取代 P3 的 `serialwrap_repo.pth`，避免兩個版本共用 live repo 程式碼而使更新／回滾驗不出差異）：
1. `V=${VERSION_OVERRIDE:-$(tr -d ' \n' < VERSION)}`；`--version-override X` 設 `VERSION_OVERRIDE=X`，並以 `SW_BUNDLE_VERSION=X cargo build -p sw-cli -p sw-daemon` 建置（`option_env!` 於編譯期讀取；CLI 與 daemon 版本由同一建置輸入產生，不是事後改字串，§9.5）。
2. `py/`：`python3 -m venv --system-site-packages <out>/versions/$V/py`（PyYAML 仍取系統 site）；**複製** `sw_core/`（排除 `__pycache__`、`*.pyc`）到 `py/sw_core/`、`serialwrapd.py` 到 `py/serialwrapd.py`、`VERSION` 檔內容寫 `$V`（Python `sw_core` 讀版本的位置）；不寫任何 `.pth`。
3. `bin/serialwrap`、`bin/serialwrapd`（sw-daemon launcher）；Windows：`serialwrap.exe`、`serialwrapd.exe`、`py\python.exe`。
4. manifest `components.daemon`＝`{implementation:"python", version:$V, launcher:{path:"bin/serialwrapd",sha256}, entry:["py/bin/python3","-I","py/serialwrapd.py"], runtime:{path:"py/bin/python3",python_version,sha256}, package:{name:"serialwrap",version:$V,tree_sha256}}`（`tree_sha256` 依 P1 T18 規則對 `py/sw_core` 計算）；`python_cli_delegate.entry` 不變（`-I -m sw_core.cli`，由 `py/` 內複本提供）；同目錄寫 `SHA256SUMS`（每個部署檔 `<sha256>  <relpath>`，依 relpath 排序）。
5. 穩定入口 `<out>/bin/{serialwrap,serialwrapd}` → `../versions/$V/bin/…` 的 symlink（Windows 為複本）。

- [ ] **步驟 1：寫失敗測試**

`crates/sw-proto/tests/yaml_emit.rs`：逐筆讀 `golden/yaml_emit.ndjson`（`{"input": <JSON 值>, "allow_unicode": bool, "out": "<PyYAML 輸出>"}`），斷言 `safe_dump(...) == out`。`gen_yaml_golden.py` 以 Python 3.12＋PyYAML 6 產生 **≥150 筆**語料，至少涵蓋：所有 YAML 1.1 bool／null 字樣（`y`、`Yes`、`on`、`OFF`、`~`、`null`、空字串）、int／float／八進位／十六進位／`1_000`／`.inf`／`.NaN` 字樣、timestamp 字樣、前後空白、`: `、` #`、每個 indicator 開頭（`-?:,[]{}#&*!|>'"%@` 與反引號）、多行字串、Tab、控制字元、非 BMP 字元、CJK（`allow_unicode` true／false 各一）、巢狀 dict／list、空 dict／list、`windows: {exclude_coms: [COM3, COM10]}` 形狀、Windows 路徑（`C:\Users\a`）、`tcp://127.0.0.1:48700`。

`crates/sw-proto/tests/bench_registry.rs`：07 §9.1 每條錯誤訊息一案（多餘欄位、缺欄位、`target` 非 `user@host`、port 範圍、`ssh_opts` 非字串陣列、`autossh` 非 bool）＋`resolve` 未知 code。

`crates/sw-daemon/tests/launcher.rs`：

| 測試名 | 斷言 |
|---|---|
| `launcher_argv_python_entry_absolute_and_tail_preserved` | manifest entry `["py/bin/python3","-I","py/serialwrapd.py"]`、tail `["--socket","/s","--lock","/l"]` → `[<dir>/py/bin/python3,"-I",<dir>/py/serialwrapd.py,"--socket","/s","--lock","/l"]`（含空白與非 ASCII 的 tail 原樣保留） |
| `launcher_argv_matches_multi_open_rule2` | 結果 argv 以 `is_serialwrapd_argv(.., Posix)` 判定為 true |
| `launcher_rejects_rust_implementation_in_stage1` | `implementation:"rust"` → `UnsupportedImplementation`、stderr 一行、exit 2 |
| `launcher_posix_exec_replaces_process`（POSIX 真實） | 以 helper manifest 指向 `sh -c 'echo $$'` → 印出的 pid 等於 launcher 子行程 pid（execve） |
| `launcher_exit_code_passthrough`（POSIX 與原生 Windows） | entry 為 `exit 7` 的 helper → launcher exit 7 |
| `launcher_windows_parent_kill_kills_child`（原生 Windows） | `TerminateProcess(launcher)` 後 1 s 內 Python helper 已結束（Job Object kill-on-close） |
| `launcher_windows_gui_subsystem`（原生 Windows） | `objdump -p serialwrapd.exe` 的 `Subsystem` 為 `Windows GUI`；從 `windows-task` 啟動不產生 console 視窗（以 `GetConsoleWindow()==NULL` 的 helper 回報） |

`tests/test_rust_mkbundle.py` 追加：`test_bundle_py_is_self_contained`（`py/` 下無 `*.pth` 指向 repo；`py/bin/python3 -I -c "import sw_core,os;print(os.path.dirname(sw_core.__file__))"` 位於 bundle 內）、`test_bundle_two_versions_have_distinct_tree_sha256`（`--version-override 0.4.0` 與 `0.4.1`：`--version` 分別為 `serialwrap 0.4.0`／`0.4.1`、bundle 內 Python `serialwrap --version` 相同、兩者 `tree_sha256` 不同、`pairing` 兩欄等於各自版本）、`test_bundle_daemon_components_and_sha256sums`（manifest 含 `launcher`／`runtime`／`package.tree_sha256`；`SHA256SUMS` 每行與實際檔案相符）、`test_bundle_launcher_starts_python_daemon`（隔離 env 下 `bin/serialwrapd --socket <tmp>/s --lock <tmp>/l --profile-dir <tmp>/p` 啟動後 `health.ping` ok、`ProcScan` 視為 1 個實例）。

- [ ] **步驟 2：執行並確認 FAIL**：`cargo test -p sw-proto --test yaml_emit --test bench_registry; cargo test -p sw-daemon --test launcher` → `not implemented: P4-T17`；`python3 -m pytest -q tests/test_rust_mkbundle.py --runxfail` 追加案例失敗。
- [ ] **步驟 3：實作**：依 Produces；`gen_yaml_golden.py` 先產 golden 並 commit；`launcher::run` 不讀 `PATH`、不讀 `SERIALWRAP_*` 以外的設定；`exec_or_wait_opts` 的 Windows 實作只在 `sw-pal/src/process/windows.rs`。
- [ ] **步驟 4：執行並確認 PASS**

```bash
python3 scripts/rust/goldens/gen_yaml_golden.py --check
cargo test -p sw-proto --test yaml_emit --test bench_registry && cargo test -p sw-daemon --test launcher
scripts/rust/win-test.sh --package sw-daemon -- launcher
python3 -m pytest -q tests/test_rust_mkbundle.py
python3 scripts/rust/check_cfg_boundary.py
```

- [ ] **步驟 5：commit**：`feat(rust-daemon): 新增 Stage 1 launcher、YAML 輸出與自含式 dev bundle`

**BLOCKED-OWNER：**
- 正式 Windows bundle 的 bundled Python runtime（非 venv）由擁有者 Windows 自架 runner 產生並原生 build launcher；本環境只驗 dev bundle。
- OD-08：本 Task 動到 PAL（`exec_or_wait_opts`），PR 必附原生 Windows COM handle 證據（本環境無法產出，PR 標示證據待補）。

---

## Task 18：`windows-task` Spike 2 程序、腳本與證據 schema（P2 指派 `win-task-*` 案例）

**Test author model:** haiku-xhigh
**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/<issue>-rust-p4-spike2-procedure.md`（`type: docs`，`scope: rust-refactor`）

**Files:**
- Create: `scripts/rust/win/spike2_windows_task.ps1`、`scripts/rust/win/collect_task_evidence.ps1`、`docs/rust-refactor/deploy/spike2-windows-task.md`、`contracts/evidence/spike2_windows_task.schema.json`、`scripts/rust/judge_spike2.py`、`tests/test_rust_spike2_tools.py`
- Create: `docs/rust-refactor/deploy/windows-task-restart-procedure.md`（P2 `p1-rst-daemon` 的 windows-task 版）、`docs/rust-refactor/deploy/windows-remote-live-e2e.md`（P2 `rm-live-e2e` 的 Windows 版）
- Modify: `tests/acceptance/p4_case_map.yaml`（`win-task-logon-trigger`、`win-task-single-instance`、`win-task-restart-on-failure`、`win-task-72h`、`p1-rst-daemon`（windows-task 面）、`rm-live-e2e`（Windows 面）綁定本 Task 程序與判定器）

**Interfaces:**
- Consumes：Task 3（Task XML、`query`）、Task 10（`setup --windows-task`）、Task 12（安裝佈局）；P2 `cases.yaml` 的 case 定義與 §11.4 envelope。
- Produces：

```text
scripts/rust/win/spike2_windows_task.ps1 -Phase <install|logon|single|restart|longrun-start|longrun-check|teardown> -InstallRoot <path> -OutDir <path>
  install：以正式 artifact 執行 `serialwrap setup --windows-task`，匯出 `schtasks /Query /TN serialwrap /XML`，確認 PT0S、IgnoreNew、RestartOnFailure(PT1M, 999)、LogonTrigger
  logon：記錄登出→登入時間戳與 daemon 首次 health.ping ok 的時間（`Get-ScheduledTaskInfo` LastRunTime、`daemon status.started_at`）
  single：在 Running 狀態下 `schtasks /Run` 兩次 → serialwrapd 實例數（`daemon status.multi_open`＋Toolhelp32）恆為 1
  restart：`Stop-Process -Force` launcher（模擬崩潰）→ 記錄重新拉起時間（期望 ≤ 90 s：PT1M＋啟動時間）
  longrun-start／longrun-check：寫入起始時間戳；check 時要求 now-start ≥ 72 h、期間 `health.ping` 每 5 分鐘取樣皆 ok（由同一 task 內的取樣器寫入 evidence）、`ExecutionTimeLimit` 未觸發
  teardown：只在擁有者同意下 `schtasks /Delete`
  每個 phase 輸出 <OutDir>/<case_id>.json，符合 contracts/evidence/spike2_windows_task.schema.json
scripts/rust/judge_spike2.py --evidence-dir <dir>   # 依 schema 與上列門檻判 PASS／FAIL／INSUFFICIENT_EVIDENCE（§11.4；缺檔或時間戳不連續 → INSUFFICIENT_EVIDENCE，不得判 PASS）
```

evidence schema 必填：`case_id`、`artifact_sha256`（CLI、launcher、runtime）、`machine_id`、`os_build`、`user`、`started_at`／`ended_at`（UTC ISO-8601）、`observations[]`（`ts`、`kind`、`value`）、`task_xml_sha256`、`verdict_inputs`。

- [ ] **步驟 1：寫失敗測試**（`tests/test_rust_spike2_tools.py`）：`judge_spike2.py` 對合成證據的判定——72 h 足夠且取樣連續 → PASS；71.9 h → FAIL；取樣缺 2 小時 → INSUFFICIENT_EVIDENCE；重啟 120 s → FAIL；single 實例數出現 2 → FAIL；schema 缺欄位 → INSUFFICIENT_EVIDENCE。另驗兩份 deploy 文件含「前置條件」「步驟」「預期證據」「判定」「清理」五節，且 `.ps1` 不含 `schtasks /Delete` 以外的破壞性指令（掃描 `Remove-Item -Recurse` 指向 install root 以外路徑者即 FAIL）。測試以 `xfail(strict=True, reason="P4-T18 紅燈")` 標示。
- [ ] **步驟 2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_rust_spike2_tools.py --runxfail` → 腳本不存在。
- [ ] **步驟 3：實作**：依 Produces；`.ps1` 只讀寫 `-OutDir` 與 `-InstallRoot`，不改系統設定以外的任何東西（登出／登入由擁有者手動，腳本只記錄）。
- [ ] **步驟 4：執行並確認 PASS**：`python3 -m pytest -q tests/test_rust_spike2_tools.py`；`pwsh -NoProfile -Command "Invoke-ScriptAnalyzer scripts/rust/win/*.ps1"`（可用時）；`python3 -m policy_check --repo .`。
- [ ] **步驟 5：commit**：`docs(rust-refactor): 新增 windows-task Spike 2 程序、證據 schema 與判定器`

**BLOCKED-OWNER：** 全部 `win-task-*` 實跑（需擁有者 Windows 工作站、互動登入／登出、>72 h 連續運行）；windows-task 版 `p1-rst-daemon` 與 Windows 版 `rm-live-e2e`（需實體板與真實 relay）。本 Task 只交付可執行程序與判定器，證據待擁有者執行（master plan §3）。

---

## 附錄 A：Task × 模型 × 調和表項目對照

| Task | 標題 | 測試作者 | 實作者 | 調和表／規格項目 |
|---|---|---|---|---|
| 1 | 介面骨架、P3→P4 遷移、測試替身、黑箱 stub、案例綁定與 golden 擷取工具 | agy-gemini-3.8-flash-high | haiku-xhigh | §11.3、arch §4 修訂 |
| 2 | PAL `SysEnv` 管理面方法與 Windows 登錄讀取（`RegistryView`） | haiku-xhigh | codex-gpt-6-luna-max | 07 §1.6、§4.2、OD-08 |
| 3 | PAL `ServiceCtl`：systemd、windows-task、windows-service | haiku-xhigh | codex-gpt-6-luna-max | SC-24、OD-03、OD-12 |
| 4 | PAL `ProcessCtl` 與 `ProcScan` | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | SC-17、CLI-12.10 |
| 5 | PAL `ProcessTunnel` 與 `FsOps` 寫入面／跨行程鎖／連結 | haiku-xhigh | codex-gpt-6-luna-max | BC-05、DF-16 |
| 6 | `config.yaml` 寫入、`service`、`supervision-mode` 查詢 | agy-gemini-3.8-flash-high | haiku-xhigh | DF-07、SC-09 |
| 7 | `daemon start/stop/status/restart`、`graceful_stop`、多開預檢、首次需求啟動 | haiku-xhigh | codex-gpt-6-luna-max | SC-10、SC-11、SC-17、SC-18、SC-20、DF-05 |
| 8 | `remote` 原生與 BC-05 Windows 路徑 | haiku-xhigh | codex-gpt-6-luna-max | BC-05、SC-21、PLT-O-10、PLT-O-11 |
| 9 | `connect`／`benches` 原生 | agy-gemini-3.8-flash-high | haiku-xhigh | SC-27 |
| 10 | `setup` 與 `supervision-mode set` | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | SC-12、SC-22、SC-24、DF-10～DF-13、DF-16 |
| 11 | 安裝佈局、manifest 驗證、安裝鎖與版本配對 gate | haiku-xhigh | codex-gpt-6-luna-max | SC-25、§9.5、§12.1 |
| 12 | §12.1 首次安裝、更新流程與 `setup --rollback` | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | SC-23、§12.1、GAP-G-05b |
| 13 | `doctor` 全部檢查與 `version_pairing` | agy-gemini-3.8-flash-high | haiku-xhigh | SC-25、PLT-O-12 |
| 14 | 終端類承接與委派集合清空 | haiku-xhigh | agy-gemini-3.8-flash-high | CLI-11.08、§4.3 |
| 15 | Agent 權限模型文件與驗證工具、文件同步 | haiku-xhigh | agy-gemini-3.8-flash-high（審查 A 專審驗證腳本副作用） | §10.3、OD-06、OD-12 |
| 16 | Stage 1B 整合：A/B、L1.5、效能 gate、Windows 原生 L1、案例完成度 | agy-gemini-3.8-flash-high | haiku-xhigh | §11.3、§11.6、§1.2 |
| 17 | `sw-daemon` launcher、`yaml11::emit`、`bench_registry`、自含式 dev bundle | haiku-xhigh | codex-gpt-6-luna-max | arch §6.1、§10.1、§9.5、OD-13 |
| 18 | `windows-task` Spike 2 程序、腳本與證據 schema | haiku-xhigh | agy-gemini-3.8-flash-high | §13 Spike 2、OD-03、P2 `win-task-*` |

審查者固定：審查 A Claude Sonnet（effort max）、審查 B Codex `gpt-6.1-sol`（reasoning medium）；兩者皆不得為該 Task 實作者。

## 附錄 B：BLOCKED-OWNER 彙總（本 Phase 不得標 PASS）

| # | 項目 | 來源 | 對應 Task |
|---|---|---|---|
| B-01 | Spike 2：`windows-task` 登入觸發、唯一實例、失敗重啟、`PT0S`、實際 >72 h 運行（OD-05 機台、正式 artifact） | §6.1、§13、OD-03 | 3、10、16、18 |
| B-02 | `cap_supervisor_runtime__*` 在擁有者 Windows 主機註冊／刪除測試排程工作 | Global Constraints | 3 |
| B-03 | `windows-service` 帳號／Session 0／ACL 資格（僅在 OD-12 條件成立且核准採用時） | OD-12、§13 Spike 2 | 3、10 |
| B-04 | 真實 `systemd-user`／`systemd-system` reconcile 與 WSL `/etc/wsl.conf`＋`wsl --shutdown`（會替換本機正式服務或修改主機設定） | §6.1 | 10 |
| B-05 | Spike 6：混合版本實機更新／回滾，真實串口釋放與重新取得（OD-08 原生 Windows COM：com0com DUT-side pair 或實體 USB-UART） | §12.1、§13、OD-08 | 7、12 |
| B-06 | BC-05 Windows：Win32-OpenSSH 對真實 relay 的三拓樸、生命週期、停止／重啟；L3 remote | §9.1 BC-05、§11.6 | 5、8、9 |
| B-07 | 正式 Windows release artifact 原生 build/link（自架 runner）與 DLL 匯入檢查 | arch §1.1、ADR-01 | 11、16 |
| B-08 | EDR 開啟下 spawn、改名切換、首次執行不被攔截；100 次 CLI 執行期間無任何暫存檔／目錄建立（含建立後刪除） | §1.2、OD-05 | 7、11、16 |
| B-09 | §1.2 CLI 往返效能在標準量測環境正式量測（Linux 原生、Windows 原生） | §1.1、§1.2 | 16 |
| B-10 | Stage 1B L2：Rust CLI 為 pinned `serialwrap_exe` 跑全部適用案例含已授權破壞集 | §11.6 | 16 |
| B-11 | Stage 1B L3：`realhw` P0＋P1＋remote | §11.6 | 16 |
| B-12 | Rust CLI＋Python daemon 混合組合 48 h 長跑（Windows 生產目標環境，OD-10 依序排程） | §11.6、OD-10 | 16 |
| B-13 | §10.3 身分清單由部署負責人提供，並以一般任務 Agent 實際身分取得 `verify_agent_perms.py` `blocked:true` 證據 | §10.3 | 12、15 |
| B-14 | 測試團隊工作站 `doctor` 全項、`connect`／`--bench` 經真實 relay、TeraTerm／PuTTY 連 `console_endpoint` 實機操作 | §11.6 | 9、13、14 |
| B-15 | Windows 實機 SERIALCOMM（藍牙＋USB-UART 混合）與 `NtQueryInformationProcess` 在 EDR 下可讀性 | SC-17、07 §1.6 | 2、4 |
| B-16 | OD-08：所有動到 PAL 的 PR（Task 2、3、4、5、17）必附原生 Windows COM handle 證據（com0com DUT-side pair 或實體 USB-UART） | OD-08 | 2、3、4、5、17 |
| B-17 | P2 指派本 Phase 的 `win-task-logon-trigger`、`win-task-single-instance`、`win-task-restart-on-failure`、`win-task-72h`、windows-task 版 `p1-rst-daemon`、Windows 版 `rm-live-e2e` 實跑（程序與判定器由 Task 18 交付） | P2 L1855、L1869、L1906-1909、L2493 | 18 |
| B-18 | 正式 Windows bundle 的 bundled Python runtime 與 launcher（GUI subsystem）於自架 runner 原生產生 | arch §1.1、§6.1 | 17 |
