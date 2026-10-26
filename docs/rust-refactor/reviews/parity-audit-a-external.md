# 1:1 對照稽核 A — Python 對外介面 vs Rust 重構

- 稽核日期：2026-10-26（排程例行稽核，唯讀分析；本檔為唯一新增檔案）
- 基準：`origin/feature/rust-refactor` @ `507565f`（main＋Stage 0＋Stage 1 Rust CLI 已合併）。另以唯讀方式比對 `origin/feature/rust-refactor-p5` @ `df66409`（Slice 2.1 進行中）與 `origin/feature/rust-refactor-p6` @ `e5e2c56`（Slice 2.2 進行中）。
- 範圍 A：Python 實作的**對外介面**——CLI 子命令／選項／預設／exit code／stdout-stderr 形狀、RPC 方法／參數／回應／錯誤碼、環境變數、`config.yaml`、`profiles/*.yaml`、`state.json`、WAL／mirror、log 檔名、socket／lock 路徑、XDG／Windows 路徑解析、監管模式、`serialwrapd` 行程介面、安裝器／腳本／資產。
- 不在範圍：內部演算法（session 狀態機、arbiter、drain 等）屬稽核 B。

## 方法（機械式、非抽樣）

1. **CLI**：直接 `import sw_core.cli` 呼叫 `build_parser()`，遞迴走訪 argparse 樹，得 75 個子命令、134 個選項／位置參數（含 7 個全域選項）；逐項與 `contracts/cli_schema.json` 的 `x-groups`／`x-global-options` 比對 flags、dest、default、choices、nargs。結果：**Python 每一個選項都在 schema 中、dest 全數一致**；唯一差異為 store_true 的 nargs=0 表示法（無語意差）與 `remote stop`（Python 以 `words` 位置參數處理，schema 以虛擬子命令表示，屬 SC-21）。Rust 側以 `crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema` 對同一 schema 逐項比對；本次實跑 `cargo test -p sw-cli --test cli_spec_schema --test rpc_params --test emitted_codes`：1＋13＋3 passed。`crates/sw-cli/src/delegate.rs:9` `DELEGATED_GROUPS = []`，即 Stage 1 後全部 20 個群組已由 Rust 原生處理（schema 的 `stage: delegate-1a` 欄為歷史標記）。
2. **RPC**：以 regex 抽出 `SerialwrapService._dispatch` 的全部 `method == "…"` 分支（53）＋`log.tail_text`（與 tail_raw 共用分支）＋`daemon.stop`（server 內建）＝55，與 `contracts/rpc_schema.json` `x-methods`（55）**完全一致**；再逐分支抽 `params.get("…")` 鍵，與 schema `params` 比對，僅 3 個別名（`by_id`、`profile_name`、`command`）未列為頂層鍵，皆已以 `x-aliases` 登記。Slice 歸屬取自 `tests/acceptance/slice21_scope.yaml`（p5 分支）：2.1 served 39、2.2 deferred 3、2.3 deferred 10、2.4 deferred 2＋`daemon.handoff_stop`（P8-T14）。
3. **錯誤碼**：掃描 `sw_core/**/*.py` 全部 `error_code`／`code=` 字面值（74 個）＋大寫碼樣式字串，全部落在 `contracts/error_codes.yaml`（97）、`session_last_errors.yaml`（34）或 `diagnostic_aux.yaml`（`NOT_BOOTLOADER`、`ATTACHED_NOT_READY`、`DEVICE_MISSING` 等 classification；`INVALID_INTERACTIVE_ENCODING` 經 `EXCEPTION` 包裝，見 06 L3340）。CLI 端碼以 CamelCase 比對 `ErrorCode::<Variant>` 於 `crates/sw-cli/src`：全部 35 個 `cli_local`／`cli_client` 碼皆有發出點（`UNKNOWN_BENCH_CODE` 經 `sw_proto::bench_registry`）。
4. **環境變數**：掃描 `os.environ.get`／`environ[...]`／`getenv` 與字串常值，逐一比對 base／p5／p6 三個分支的 `crates/**/*.rs`、`contracts/**`、計畫 04～09。
5. **檔案格式／路徑**：以 `contracts/{state_file,wal_record,wal_meta_events}.schema.json`、`golden/{runtime_paths,endpoint,wal_line,load_profiles}.*` 對照 `sw_core/{constants,config,wal,session_manager,runtime_config}.py`；WAL META 事件以 regex 抽出 Python 發出點並確認皆在 schema 中。
6. 狀態判定：
   - **COVERED**＝凍結契約＋Rust 實作＋測試皆已在 `feature/rust-refactor`（Stage 1 已合併部分）。
   - **PLANNED**＝契約已凍結且 06～09 有明確 Task 承接，但 Rust daemon 端尚未合併（p5／p6 為紅燈 TDD 與部分實作）。
   - **GAP**＝無契約、無計畫 Task、無測試。
   - **DIVERGENT**＝Rust 計畫或程式與 Python 不同；有登記者引用 `behavior_changes.yaml` 的 SC／DF 與 OD，未登記者明確標示。

## 摘要計數

| 區段 | COVERED | PLANNED | GAP | DIVERGENT | 小計 |
|---|---:|---:|---:|---:|---:|
| A1 CLI 子命令 | 75 | 0 | 0 | 0 | 75 |
| A2 CLI 選項／位置參數 | 134 | 0 | 0 | 0 | 134 |
| A3 CLI exit code／輸出形狀 | 28 | 0 | 0 | 0 | 28 |
| A4 RPC 方法 | 0 | 56 | 0 | 0 | 56 |
| A5 錯誤碼 | 28 | 71 | 0 | 0 | 99 |
| A6 環境變數 | 18 | 10 | 5 | 1 | 34 |
| A7 config.yaml 鍵 | 3 | 3 | 0 | 1 | 7 |
| A8 profiles/*.yaml 結構 | 24 | 0 | 0 | 0 | 24 |
| A9 state.json／WAL／mirror／事件與日誌檔 | 3 | 7 | 0 | 0 | 10 |
| A10 socket／lock／路徑與監管模式 | 6 | 6 | 0 | 3 | 15 |
| A11 serialwrapd 行程介面 | 1 | 4 | 0 | 0 | 5 |
| A12 安裝器／腳本／資產／入口 | 6 | 0 | 0 | 3 | 9 |
| A13 Rust-only 新增介面（Python 無對應） | 0 | 0 | 0 | 8 | 8 |
| **合計** | **326** | **157** | **5** | **16** | **504** |

**結論**：對外介面的契約化程度很高——CLI 全面 COVERED，RPC／錯誤碼／檔案格式皆已凍結並由 P5～P8 承接。真正的缺口集中在**少數未被任何契約或計畫收錄的環境變數**（5 項 GAP），另有 1 項**未登記的行為差異**（daemon 端 backend env 強制選擇）。其餘 15 項 DIVERGENT 皆為已登記之 Rust-only 或交付形態變更。

## GAP 表

| # | 項目 | Python file:line | 為何重要 | 建議承接 |
|---:|---|---|---|---|
| G-1 | `SERIALWRAP_SERIAL_BACKEND`（`auto`／`posix`／`termios`／`pyserial`／`windows`／`win32`） | `sw_core/serial_port.py:774-782` | README.md:2492 與 CLAUDE.md 皆把它列為公開覆寫手段；Rust 無任何讀取點、契約或計畫文字，設定後會被**靜默忽略**（使用者以為切了後端，實際沒有）。`docs/rust-refactor/**` 全文無此字串 | P5-T2／T3（serial PAL）：明定 Rust 為編譯期單一原生後端，env 改為「已知值接受並忽略／非原生值 WARN 或 exit 1」；於 `behavior_changes.yaml` 登記一筆 SC，並同步 README 中英兩段（R-18） |
| G-2 | `SERIALWRAP_TEST_SYSTEM_CONFIG_PATH` | `sw_core/constants.py:35`；`tests/conftest.py:93-94` | pytest 以此 env 把系統設定檔 `/etc/serialwrap/config.yaml`（Windows `%ProgramData%`）導向隔離目錄（#120 隔離防線的一部分）。Rust `SysEnv::system_config_dir()` 固定回 `/etc/serialwrap`（`crates/sw-pal/src/sysenv/posix.rs:64`），`endpoint_resolve.rs:122` 與 `runtime_paths.rs:339-357` 都會讀它；p5 `crates/sw-daemon/tests/support/mod.rs:137` 有設此 env 但無消費端。後果：在有系統設定檔的主機上跑 A/B 黑箱時，Rust CLI／daemon 會讀到真實系統設定，與 Python 結果分岔且破壞隔離 | P5-T0c（sw-pal SysEnv）：比照 Python 讀此 env（僅測試用）或在 `tests/conftest.py`／`bb_harness.isolated_env` 改以其他機制隔離；需登記於契約（`x-env` 或 runtime_paths golden 新增一列） |
| G-3 | `SERIALWRAP_TEST_KILL_AFTER_SAVE`（隔離實例專用，`_save_state` 後以 `os._exit(137)` 模擬被殺） | `sw_core/session_manager.py:8126-8140` | `contracts/daemon_logging.yaml:385-392` 已凍結其兩則 log 訊息，`realhw/procedures/stage0-release-kill.md`（Stage 0 交接持久化程序）依賴它；06～09 無任何 Task 承接，Rust daemon 上線後該實機程序無法對 Rust 執行 | P8（09，device release／handoff 持久化）或 P5-T15（StateStore）：新增測試鉤子 Task，沿用 `isolation_reason` 判定與 daemon_logging 訊息 |
| G-4 | `SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE` | `sw_core/session_manager.py:8152-8165` | 同 G-3；`contracts/daemon_logging.yaml:399` 已登記訊息但無 Rust 承接 | 同 G-3 |
| G-5 | `SERIALWRAP_EVENTS_LOG_PATH`（預設 `<EVENTS_RUNTIME_DIR>/events.ndjson`） | `sw_core/constants.py:90-93`；`service.py:285` | P7 的 `EngineDeps.log_path`（08 L2023）沒有解析規則；p5 `sw_proto::runtime_paths::DaemonDirs` 只有 `events_dir`／`events_runtime_dir`（p5 `runtime_paths.rs:394`），runtime_paths golden 亦無此欄。可能導致 Rust 忽略此覆寫，事件日誌落到預設位置 | P7 event store Task（08 L1682 一帶）：`DaemonDirs` 加 `events_log_path`，runtime_paths golden 加欄 |

## DIVERGENT 表

| # | 項目 | Python | Rust | 依據／是否登記 |
|---:|---|---|---|---|
| D-1 | daemon 端 `SERIALWRAP_RPC_BACKEND`／`SERIALWRAP_LOCK_BACKEND`／`SERIALWRAP_DEVICE_BACKEND` 指定非本平台後端 | `sw_core/platform_backends.py:24-76`：可在 POSIX 強制 `win`（daemon 改用 `TcpRpcServer`、`WindowsDeviceSource`；CLAUDE.md 明載「可在 POSIX 機器以 env 強制選 win 後端（用於測試）」） | P5-T19 `check_backend_env`（06 L4114、L4282）：合法但非本 build 原生後端 → `backend '<v>' is not available in this build`、exit 1。另 Rust CLI 仍依 `RPC_BACKEND=win` 改用 `tcp://127.0.0.1:48700`（P3 Q-17），兩端不對稱 | **未登記**：`behavior_changes.yaml` 無對應 SC／DF，亦無 OD。建議登記一筆 SC（或改為接受 `win` 於 POSIX 走 TCP listener），並在 README／CLAUDE.md 標明 Rust daemon 不支援 |
| D-2 | `config.yaml` 新鍵 `windows.serial_console` | 無 | P6（選配 com0com serial console） | SC-29（僅 p6 分支登記）；OD-01、OD-08 |
| D-3 | 監管模式 `windows-task`／`windows-service` | 只有 on-demand／systemd-user／systemd-system | P4 已實作 | SC-24；OD-03、OD-12 |
| D-4 | Windows on-demand 首次需求啟動 | 無 | P4 | SC-18（Rust-only） |
| D-5 | Windows 多開偵測 | `multi_open.py` 掃 `/proc`，Windows 恆失效 | 行程掃描實作 | SC-17（Rust-only） |
| D-6 | 安裝入口 | `install.sh`（pipx／pip --user＋`serialwrap setup`） | bundle＋`serialwrap setup` 安裝佈局（`~/.local/bin` symlink 到 `versions/<v>/bin`；Windows `bin\current-release.json`） | arch §10.2、05 Task 10／17、SC-23、SC-25（OD-13）；無對應 install.sh（已知取代） |
| D-7 | Windows 發佈物 | `scripts/build_windows.ps1`＋`serialwrap.spec`（PyInstaller one-file） | `scripts/rust/mkbundle.sh`＋release manifest；`check_no_onefile_extract.py` | SC-25（OD-13）；`inventory/10-gaps.md` §2.4 |
| D-8 | systemd unit `ExecStart` 可執行檔 | `shutil.which("serialwrapd")` | 固定 `<InstallRoot.bin>/serialwrapd`，不搜尋 PATH | DF-05 附註（05 L1683）；A/B 以正規化比較 |
| D-9 | `supervision-mode set [--with-sudo] [--force]` | 無 | P4 | SC-22（Rust-only） |
| D-10 | `setup --rollback`（七步更新／回滾） | 無 | P4 | SC-23（Rust-only） |
| D-11 | `setup --with-com0com`／`--remove-com0com` | 無 | P4 | SC-36；OD-15、OD-01、OD-08 |
| D-12 | console 出向 ring 1 MiB drop-oldest | 無上限 | P6（p6 `out_ring.rs`） | SC-15（Rust-only） |
| D-13 | Windows event handler（CreateProcessW＋Job、`cmd.exe /d /s /c`、env 白名單另加 6 鍵） | 僅 POSIX 語意 | P7 | SC-26；OD-14 #6 |
| D-14 | `SERIALWRAP_CONSOLE_BACKEND`（auto／pty／tcp） | 無 | P6 | SC-28（僅 p6 分支登記） |
| D-15 | Console 失效上下文與資源上限細化 | — | P6 | SC-31（僅 p6 分支登記）；OD-04 |
| D-16 | Rust-only env：`SERIALWRAP_INSTALL_ROOT`、`SERIALWRAP_DELEGATE_DEPTH`／`_ARGV_SHA`、`SERIALWRAP_TEST_DELEGATE_GROUP` | 無 | `install_layout.rs`、`delegate.rs:111-273`、`lib.rs` | 屬 Stage 1 委派／安裝佈局機制；`delegate.rs` 註解已說明，未逐一列入 `cli_schema.json` `x-env`（建議補登，非阻斷） |

> 註：`behavior_changes.yaml` 中 `applies_to.python` 有值（Python 0.4.0 已同步實作）的 SC／DF／BC（如 SC-09、SC-10、SC-20、SC-21、DF-07、BC-05 等）兩邊行為一致，不計為 DIVERGENT，於附錄以 COVERED／PLANNED 計。

## 其他觀察（不計入狀態）

- **RPC 全數 PLANNED**：55 個方法的契約（params／result／errors／error_order）皆已凍結，但 Rust daemon 尚無任何方法合併進 `feature/rust-refactor`；p5 分支 `crates/sw-daemon/src/service.rs` 僅 75 行骨架，黑箱 `tests/contract_blackbox/slice21/*` 為紅燈 TDD。附錄所列 Task id 為以計畫正文首次出現位置推得的承接 Task，供追蹤參考。
- **`error_detail`／classification 等非錯誤碼**：`SESSION_NOT_READY` 的 `error_detail: NOT_BOOTLOADER`（`session_manager.py:7042`）與 self-test classification 由 `diagnostic_aux.yaml` 承接，P5-T14（06 L3335）已有對應案例。
- **`cli_schema.json` 的 `stage` 欄**：`daemon`／`remote`／`connect` 等群組仍標 `delegate-1a`，但 `DELEGATED_GROUPS` 已為空；建議在 schema 註記為歷史值，避免下一位讀者誤判為仍委派 Python。
- **README 與 CLI 樹**：README 未提及 `alias *`、`cmd cancel`、`daemon restart`、`daemon stop`、`session activity`、`stream tail`（R-18 屬 WARN，不影響本稽核狀態）。

## 附錄：完整清單

### A1 CLI 子命令（75 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `serialwrap daemon` | sw_core/cli.py:2059 | cli_schema.json x-groups[daemon]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 2 | `serialwrap daemon start` | sw_core/cli.py:2066 | cli_schema.json x-groups[daemon]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 3 | `serialwrap daemon stop` | sw_core/cli.py:2088 | cli_schema.json x-groups[daemon]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 4 | `serialwrap daemon restart` | sw_core/cli.py:2096 | cli_schema.json x-groups[daemon]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 5 | `serialwrap daemon status` | sw_core/cli.py:2107 | cli_schema.json x-groups[daemon]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 6 | `serialwrap device` | sw_core/cli.py:2109 | cli_schema.json x-groups[device]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 7 | `serialwrap device list` | sw_core/cli.py:2115 | cli_schema.json x-groups[device]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 8 | `serialwrap device release` | sw_core/cli.py:2116 | cli_schema.json x-groups[device]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 9 | `serialwrap device attach` | sw_core/cli.py:2123 | cli_schema.json x-groups[device]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 10 | `serialwrap session` | sw_core/cli.py:2130 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 11 | `serialwrap session list` | sw_core/cli.py:2136 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 12 | `serialwrap session clear` | sw_core/cli.py:2137 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 13 | `serialwrap session bind` | sw_core/cli.py:2139 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 14 | `serialwrap session pin` | sw_core/cli.py:2142 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 15 | `serialwrap session unpin` | sw_core/cli.py:2145 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 16 | `serialwrap session attach` | sw_core/cli.py:2147 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 17 | `serialwrap session self-test` | sw_core/cli.py:2149 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 18 | `serialwrap session activity` | sw_core/cli.py:2158 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 19 | `serialwrap session recover` | sw_core/cli.py:2160 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 20 | `serialwrap session console-attach` | sw_core/cli.py:2164 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 21 | `serialwrap session console-detach` | sw_core/cli.py:2167 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 22 | `serialwrap session console-list` | sw_core/cli.py:2170 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 23 | `serialwrap session interactive-open` | sw_core/cli.py:2172 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 24 | `serialwrap session interactive-send` | sw_core/cli.py:2184 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 25 | `serialwrap session interactive-status` | sw_core/cli.py:2188 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 26 | `serialwrap session interactive-close` | sw_core/cli.py:2191 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 27 | `serialwrap session log-start` | sw_core/cli.py:2193 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 28 | `serialwrap session log-stop` | sw_core/cli.py:2195 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 29 | `serialwrap session log-status` | sw_core/cli.py:2197 | cli_schema.json x-groups[session]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 30 | `serialwrap alias` | sw_core/cli.py:2200 | cli_schema.json x-groups[alias]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 31 | `serialwrap alias list` | sw_core/cli.py:2206 | cli_schema.json x-groups[alias]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 32 | `serialwrap alias set` | sw_core/cli.py:2207 | cli_schema.json x-groups[alias]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 33 | `serialwrap alias assign` | sw_core/cli.py:2210 | cli_schema.json x-groups[alias]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 34 | `serialwrap alias unassign` | sw_core/cli.py:2214 | cli_schema.json x-groups[alias]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 35 | `serialwrap cmd` | sw_core/cli.py:2217 | cli_schema.json x-groups[cmd]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 36 | `serialwrap cmd submit` | sw_core/cli.py:2223 | cli_schema.json x-groups[cmd]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 37 | `serialwrap cmd status` | sw_core/cli.py:2253 | cli_schema.json x-groups[cmd]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 38 | `serialwrap cmd result-tail` | sw_core/cli.py:2255 | cli_schema.json x-groups[cmd]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 39 | `serialwrap cmd cancel` | sw_core/cli.py:2259 | cli_schema.json x-groups[cmd]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 40 | `serialwrap stream` | sw_core/cli.py:2262 | cli_schema.json x-groups[stream]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 41 | `serialwrap stream tail` | sw_core/cli.py:2268 | cli_schema.json x-groups[stream]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 42 | `serialwrap log` | sw_core/cli.py:2274 | cli_schema.json x-groups[log]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 43 | `serialwrap log tail-raw` | sw_core/cli.py:2280 | cli_schema.json x-groups[log]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 44 | `serialwrap log tail-text` | sw_core/cli.py:2290 | cli_schema.json x-groups[log]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 45 | `serialwrap file` | sw_core/cli.py:2301 | cli_schema.json x-groups[file]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 46 | `serialwrap file push` | sw_core/cli.py:2307 | cli_schema.json x-groups[file]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 47 | `serialwrap file pull` | sw_core/cli.py:2319 | cli_schema.json x-groups[file]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 48 | `serialwrap wal` | sw_core/cli.py:2330 | cli_schema.json x-groups[wal]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 49 | `serialwrap wal export` | sw_core/cli.py:2336 | cli_schema.json x-groups[wal]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 50 | `serialwrap wal reset` | sw_core/cli.py:2340 | cli_schema.json x-groups[wal]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 51 | `serialwrap wal current-seq` | sw_core/cli.py:2341 | cli_schema.json x-groups[wal]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 52 | `serialwrap mcu` | sw_core/cli.py:2343 | cli_schema.json x-groups[mcu]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 53 | `serialwrap mcu patterns` | sw_core/cli.py:2349 | cli_schema.json x-groups[mcu]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 54 | `serialwrap mcu status` | sw_core/cli.py:2350 | cli_schema.json x-groups[mcu]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 55 | `serialwrap remote` | sw_core/cli.py:2352 | cli_schema.json x-groups[remote]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 56 | `serialwrap connect` | sw_core/cli.py:2401 | cli_schema.json x-groups[connect]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 57 | `serialwrap benches` | sw_core/cli.py:2413 | cli_schema.json x-groups[benches]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 58 | `serialwrap event` | sw_core/cli.py:2419 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 59 | `serialwrap event add` | sw_core/cli.py:2426 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 60 | `serialwrap event rm` | sw_core/cli.py:2429 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 61 | `serialwrap event list` | sw_core/cli.py:2432 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 62 | `serialwrap event show` | sw_core/cli.py:2436 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 63 | `serialwrap event enable` | sw_core/cli.py:2439 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 64 | `serialwrap event disable` | sw_core/cli.py:2442 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 65 | `serialwrap event status` | sw_core/cli.py:2445 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 66 | `serialwrap event reset` | sw_core/cli.py:2448 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 67 | `serialwrap event reload` | sw_core/cli.py:2453 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 68 | `serialwrap event tail` | sw_core/cli.py:2455 | cli_schema.json x-groups[event]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 69 | `serialwrap supervision-mode` | sw_core/cli.py:2461 | cli_schema.json x-groups[supervision-mode]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 70 | `serialwrap service` | sw_core/cli.py:2467 | cli_schema.json x-groups[service]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 71 | `serialwrap setup` | sw_core/cli.py:2490 | cli_schema.json x-groups[setup]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 72 | `serialwrap doctor` | sw_core/cli.py:2519 | cli_schema.json x-groups[doctor]（stage=delegate-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 73 | `serialwrap profile` | sw_core/cli.py:2531 | cli_schema.json x-groups[profile]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 74 | `serialwrap profile test` | sw_core/cli.py:2537 | cli_schema.json x-groups[profile]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |
| 75 | `serialwrap skill` | sw_core/cli.py:2557 | cli_schema.json x-groups[skill]（stage=rust-1a）；P3/P4；`crates/sw-cli/tests/cli_spec_schema.rs::cli_spec_matches_schema`、`argparse_golden.rs` | COVERED |

### A2 CLI 選項／位置參數（134 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `(global)` `--version`（dest=version, default='==SUPPRESS=='） | sw_core/cli.py:2027 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 2 | `(global)` `-v/--verbose`（dest=verbose, default=0） | sw_core/cli.py:2028 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 3 | `(global)` `--socket`（dest=socket, default=None） | sw_core/cli.py:2029 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 4 | `(global)` `--endpoint`（dest=endpoint, default=None） | sw_core/cli.py:2030 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 5 | `(global)` `--bench`（dest=bench, default=None） | sw_core/cli.py:2031 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 6 | `(global)` `--timeout`（dest=timeout_s, default=None） | sw_core/cli.py:2037 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 7 | `(global)` `--retries`（dest=retries, default=0） | sw_core/cli.py:2044 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 8 | `daemon start` `--profile-dir`（dest=profile_dir, default='/root/.config/serialwrap/profiles'） | sw_core/cli.py:2067 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 9 | `daemon start` `--lock`（dest=lock, default='/root/.local/state/serialwrap/run/se…） | sw_core/cli.py:2068 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 10 | `daemon start` `--foreground`（dest=foreground, default=False） | sw_core/cli.py:2069 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 11 | `daemon start` `--with-sudo`（dest=with_sudo, default=False） | sw_core/cli.py:2070 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 12 | `daemon start` `--force-spawn`（dest=force_spawn, default=False） | sw_core/cli.py:2077 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 13 | `daemon stop` `--with-sudo`（dest=with_sudo, default=False） | sw_core/cli.py:2089 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 14 | `daemon restart` `--with-sudo`（dest=with_sudo, default=False） | sw_core/cli.py:2100 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 15 | `device release` `--selector`（dest=selector, default=None） | sw_core/cli.py:2120 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 16 | `device release` `--source`（dest=source, default='cli'） | sw_core/cli.py:2121 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 17 | `device release` `--reason`（dest=reason, default=None） | sw_core/cli.py:2122 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 18 | `device attach` `--selector`（dest=selector, default=None） | sw_core/cli.py:2127 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 19 | `device attach` `--force`（dest=force, default=False） | sw_core/cli.py:2128 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 20 | `session clear` `--selector`（dest=selector, default=None） | sw_core/cli.py:2138 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 21 | `session bind` `--selector`（dest=selector, default=None） | sw_core/cli.py:2140 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 22 | `session bind` `--device-by-id`（dest=device_by_id, default=None） | sw_core/cli.py:2141 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 23 | `session pin` `--selector`（dest=selector, default=None） | sw_core/cli.py:2143 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 24 | `session pin` `--profile`（dest=profile, default=None） | sw_core/cli.py:2144 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 25 | `session unpin` `--selector`（dest=selector, default=None） | sw_core/cli.py:2146 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 26 | `session attach` `--selector`（dest=selector, default=None） | sw_core/cli.py:2148 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 27 | `session self-test` `--selector`（dest=selector, default=None） | sw_core/cli.py:2150 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 28 | `session self-test` `--probe-timeout`（dest=probe_timeout_s, default=2.0） | sw_core/cli.py:2151 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 29 | `session self-test` `--strict-human-lock`（dest=strict_human_lock, default=False） | sw_core/cli.py:2152 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 30 | `session activity` `--selector`（dest=selector, default=None） | sw_core/cli.py:2159 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 31 | `session recover` `--selector`（dest=selector, default=None） | sw_core/cli.py:2161 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 32 | `session recover` `--timeout`（dest=recover_timeout_s, default=2.0） | sw_core/cli.py:2162 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 33 | `session recover` `--force`（dest=force, default=False） | sw_core/cli.py:2163 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 34 | `session console-attach` `--selector`（dest=selector, default=None） | sw_core/cli.py:2165 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 35 | `session console-attach` `--label`（dest=label, default=None） | sw_core/cli.py:2166 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 36 | `session console-detach` `--selector`（dest=selector, default=None） | sw_core/cli.py:2168 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 37 | `session console-detach` `--client-id`（dest=client_id, default=None） | sw_core/cli.py:2169 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 38 | `session console-list` `--selector`（dest=selector, default=None） | sw_core/cli.py:2171 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 39 | `session interactive-open` `--selector`（dest=selector, default=None） | sw_core/cli.py:2173 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 40 | `session interactive-open` `--owner`（dest=owner, default='agent'） | sw_core/cli.py:2174 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 41 | `session interactive-open` `--timeout`（dest=interactive_timeout_s, default=60.0） | sw_core/cli.py:2175 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 42 | `session interactive-open` `--command`（dest=command, default=''） | sw_core/cli.py:2176 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 43 | `session interactive-open` `--allow-attached`（dest=allow_attached, default=False） | sw_core/cli.py:2177 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 44 | `session interactive-send` `--interactive-id`（dest=interactive_id, default=None） | sw_core/cli.py:2185 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 45 | `session interactive-send` `--data`（dest=data, default=None） | sw_core/cli.py:2186 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 46 | `session interactive-send` `--encoding`（dest=encoding, default='plain'） | sw_core/cli.py:2187 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 47 | `session interactive-status` `--interactive-id`（dest=interactive_id, default=None） | sw_core/cli.py:2189 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 48 | `session interactive-status` `--screen-chars`（dest=screen_chars, default=2048） | sw_core/cli.py:2190 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 49 | `session interactive-close` `--interactive-id`（dest=interactive_id, default=None） | sw_core/cli.py:2192 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 50 | `session log-start` `--selector`（dest=selector, default=None） | sw_core/cli.py:2194 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 51 | `session log-stop` `--selector`（dest=selector, default=None） | sw_core/cli.py:2196 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 52 | `session log-status` `--selector`（dest=selector, default=None） | sw_core/cli.py:2198 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 53 | `alias set` `--session-id`（dest=session_id, default=None） | sw_core/cli.py:2208 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 54 | `alias set` `--alias`（dest=alias, default=None） | sw_core/cli.py:2209 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 55 | `alias assign` `--by-id`（dest=by_id, default=None） | sw_core/cli.py:2211 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 56 | `alias assign` `--alias`（dest=alias, default=None） | sw_core/cli.py:2212 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 57 | `alias assign` `--profile`（dest=profile, default=None） | sw_core/cli.py:2213 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 58 | `alias unassign` `--alias`（dest=alias, default=None） | sw_core/cli.py:2215 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 59 | `cmd submit` `--selector`（dest=selector, default=None） | sw_core/cli.py:2240 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 60 | `cmd submit` `--cmd`（dest=command_text, default=''） | sw_core/cli.py:2241 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 61 | `cmd submit` `--source`（dest=source, default='agent'） | sw_core/cli.py:2247 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 62 | `cmd submit` `--mode`（dest=mode, default='line'） | sw_core/cli.py:2248 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 63 | `cmd submit` `--priority`（dest=priority, default=10） | sw_core/cli.py:2249 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 64 | `cmd submit` `--cmd-timeout`（dest=cmd_timeout_s, default=10.0） | sw_core/cli.py:2250 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 65 | `cmd submit` `--expected-duration`（dest=expected_duration_s, default=None） | sw_core/cli.py:2251 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 66 | `cmd submit` `--completion-regex`（dest=completion_regex, default=None） | sw_core/cli.py:2252 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 67 | `cmd status` `--cmd-id`（dest=cmd_id, default=None） | sw_core/cli.py:2254 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 68 | `cmd result-tail` `--cmd-id`（dest=cmd_id, default=None） | sw_core/cli.py:2256 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 69 | `cmd result-tail` `--from-chunk`（dest=from_chunk, default=0） | sw_core/cli.py:2257 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 70 | `cmd result-tail` `--limit`（dest=limit, default=200） | sw_core/cli.py:2258 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 71 | `cmd cancel` `--cmd-id`（dest=cmd_id, default=None） | sw_core/cli.py:2260 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 72 | `stream tail` `--selector`（dest=selector, default=None） | sw_core/cli.py:2269 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 73 | `stream tail` `--com`（dest=com, default=None） | sw_core/cli.py:2270 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 74 | `stream tail` `--from-seq`（dest=from_seq, default=0） | sw_core/cli.py:2271 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 75 | `stream tail` `--limit`（dest=limit, default=200） | sw_core/cli.py:2272 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 76 | `log tail-raw` `--selector`（dest=selector, default=None） | sw_core/cli.py:2281 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 77 | `log tail-raw` `--com`（dest=com, default=None） | sw_core/cli.py:2282 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 78 | `log tail-raw` `--from-seq`（dest=from_seq, default=None） | sw_core/cli.py:2283 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 79 | `log tail-raw` `--limit`（dest=limit, default=200） | sw_core/cli.py:2289 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 80 | `log tail-text` `--selector`（dest=selector, default=None） | sw_core/cli.py:2291 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 81 | `log tail-text` `--com`（dest=com, default=None） | sw_core/cli.py:2292 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 82 | `log tail-text` `--from-seq`（dest=from_seq, default=None） | sw_core/cli.py:2293 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 83 | `log tail-text` `--limit`（dest=limit, default=200） | sw_core/cli.py:2299 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 84 | `file push` `--selector`（dest=selector, default=None） | sw_core/cli.py:2308 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 85 | `file push` `--local`（dest=local, default=None） | sw_core/cli.py:2309 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 86 | `file push` `--remote`（dest=remote, default=None） | sw_core/cli.py:2310 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 87 | `file push` `--chunk-size`（dest=chunk_size, default=512） | sw_core/cli.py:2311 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 88 | `file push` `--chunk-timeout`（dest=chunk_timeout_s, default=None） | sw_core/cli.py:2312 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 89 | `file push` `--ack-mode`（dest=ack_mode, default='auto', choices=['auto', 'echo', 'none']） | sw_core/cli.py:2314 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 90 | `file push` `--source`（dest=source, default='agent'） | sw_core/cli.py:2318 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 91 | `file pull` `--selector`（dest=selector, default=None） | sw_core/cli.py:2320 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 92 | `file pull` `--remote`（dest=remote, default=None） | sw_core/cli.py:2321 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 93 | `file pull` `--local`（dest=local, default=None） | sw_core/cli.py:2322 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 94 | `file pull` `--chunk-timeout`（dest=chunk_timeout_s, default=None） | sw_core/cli.py:2323 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 95 | `file pull` `--ack-mode`（dest=ack_mode, default='auto', choices=['auto', 'echo', 'none']） | sw_core/cli.py:2325 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 96 | `file pull` `--source`（dest=source, default='agent'） | sw_core/cli.py:2328 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 97 | `wal export` `--from-seq`（dest=from_seq, default=0） | sw_core/cli.py:2337 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 98 | `wal export` `--to-seq`（dest=to_seq, default=0） | sw_core/cli.py:2338 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 99 | `wal export` `--limit`（dest=limit, default=1000） | sw_core/cli.py:2339 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 100 | `remote` `words`（dest=words, default=None, nargs=*） | sw_core/cli.py:2367 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 101 | `remote` `-R`（dest=reverse, default=False） | sw_core/cli.py:2375 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 102 | `remote` `-L`（dest=forward, default=False） | sw_core/cli.py:2376 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 103 | `remote` `--autossh`（dest=autossh, default=False） | sw_core/cli.py:2377 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 104 | `remote` `--local`（dest=local, default=None） | sw_core/cli.py:2378 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 105 | `remote` `--remote-socket`（dest=remote_socket, default=None） | sw_core/cli.py:2379 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 106 | `remote` `--ready-timeout`（dest=ready_timeout, default=10.0） | sw_core/cli.py:2385 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 107 | `remote` `--ssh-opt`（dest=ssh_opt, default=[]） | sw_core/cli.py:2392 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 108 | `connect` `code`（dest=code, default=None） | sw_core/cli.py:2406 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 109 | `connect` `--close`（dest=close, default=False） | sw_core/cli.py:2407 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 110 | `event add` `--file`（dest=file, default=None） | sw_core/cli.py:2427 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 111 | `event rm` `rule_id`（dest=rule_id, default=None） | sw_core/cli.py:2430 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 112 | `event list` `--selector`（dest=selector, default=None） | sw_core/cli.py:2433 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 113 | `event list` `--owner`（dest=owner, default=None） | sw_core/cli.py:2434 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 114 | `event show` `rule_id`（dest=rule_id, default=None） | sw_core/cli.py:2437 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 115 | `event enable` `--selector`（dest=selector, default=None） | sw_core/cli.py:2440 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 116 | `event disable` `--selector`（dest=selector, default=None） | sw_core/cli.py:2443 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 117 | `event status` `--selector`（dest=selector, default=None） | sw_core/cli.py:2446 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 118 | `event reset` `--rule-id`（dest=rule_id, default=None） | sw_core/cli.py:2450 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 119 | `event reset` `--selector`（dest=selector, default=None） | sw_core/cli.py:2451 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 120 | `event tail` `--rule-id`（dest=rule_id, default=None） | sw_core/cli.py:2456 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 121 | `event tail` `--selector`（dest=selector, default=None） | sw_core/cli.py:2457 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 122 | `event tail` `-n`（dest=n, default=50） | sw_core/cli.py:2458 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 123 | `event tail` `--since`（dest=since, default=None） | sw_core/cli.py:2459 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 124 | `service` `action`（dest=action, default=None, choices=['start', 'stop', 'restart', 'status']） | sw_core/cli.py:2477 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 125 | `service` `--with-sudo`（dest=with_sudo, default=False） | sw_core/cli.py:2482 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 126 | `setup` `--user`（dest=user, default=False） | sw_core/cli.py:2502 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 127 | `setup` `--system`（dest=system, default=False） | sw_core/cli.py:2503 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 128 | `setup` `--on-demand`（dest=on_demand, default=False） | sw_core/cli.py:2504 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 129 | `setup` `--force`（dest=force, default=False） | sw_core/cli.py:2510 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 130 | `setup` `--with-sudo`（dest=with_sudo, default=False） | sw_core/cli.py:2511 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 131 | `profile test` `--profile`（dest=profile, default=None） | sw_core/cli.py:2548 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 132 | `profile test` `--sample`（dest=sample, default=None） | sw_core/cli.py:2549 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 133 | `profile test` `--profile-dir`（dest=profile_dir, default='/root/.config/serialwrap/profiles'） | sw_core/cli.py:2550 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |
| 134 | `skill` `--platform`（dest=platform, default='auto', choices=['auto', 'linux', 'windows']） | sw_core/cli.py:2566 | cli_schema.json；`cli_spec_schema.rs::cli_spec_matches_schema`（dest/default/action 逐項比對）；`rpc_params.rs::params_wire_exact` | COVERED |

### A3 CLI exit code／輸出形狀（28 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | exit code context `_run_rpc`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 2 | exit code context `daemon start`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 3 | exit code context `daemon status`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 4 | exit code context `daemon stop`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 5 | exit code context `daemon restart`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 6 | exit code context `event`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 7 | exit code context `connect`（success=0, failure=1） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 8 | exit code context `benches`（success=0, failure=1） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 9 | exit code context `remote`（success=0, failure=1） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 10 | exit code context `setup`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 11 | exit code context `service <action>`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 12 | exit code context `cmd submit`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 13 | exit code context `profile test`（success=0, failure=0） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 14 | exit code context `doctor`（success=0, failure=0） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 15 | exit code context `argparse-boundary`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 16 | exit code context `fallthrough`（success=0, failure=2） | sw_core/cli.py | cli_schema.json x-exit-codes；`tests/contract_blackbox/test_bb_exit_codes.py`、`test_bb_exit_codes_argv.py`（A/B 對 Rust CLI） | COVERED |
| 17 | exit code context `skill` | sw_core/cli.py | cli_schema.json x-exit-codes-simple；`test_bb_exit_codes.py` | COVERED |
| 18 | exit code context `supervision-mode` | sw_core/cli.py | cli_schema.json x-exit-codes-simple；`test_bb_exit_codes.py` | COVERED |
| 19 | exit code context `--help/--version` | sw_core/cli.py | cli_schema.json x-exit-codes-simple；`test_bb_exit_codes.py` | COVERED |
| 20 | x-output.stdout_json | sw_core/cli.py:150-190（_print／_mirror_err） | cli_schema.json x-output；`crates/sw-cli/tests/output_format.rs`、`test_bb_output_contract.py` | COVERED |
| 21 | x-output.stderr_error_line | sw_core/cli.py:150-190（_print／_mirror_err） | cli_schema.json x-output；`crates/sw-cli/tests/output_format.rs`、`test_bb_output_contract.py` | COVERED |
| 22 | x-output.unknown_error_literal | sw_core/cli.py:150-190（_print／_mirror_err） | cli_schema.json x-output；`crates/sw-cli/tests/output_format.rs`、`test_bb_output_contract.py` | COVERED |
| 23 | x-output.plain_text_commands | sw_core/cli.py:150-190（_print／_mirror_err） | cli_schema.json x-output；`crates/sw-cli/tests/output_format.rs`、`test_bb_output_contract.py` | COVERED |
| 24 | x-retry（client 重試／逾時／版本訊息） | sw_core/client.py、sw_core/cli.py:101-103 | cli_schema.json；`client_retry.rs`、`test_bb_client.py` | COVERED |
| 25 | x-timeouts（client 重試／逾時／版本訊息） | sw_core/client.py、sw_core/cli.py:101-103 | cli_schema.json；`client_retry.rs`、`test_bb_client.py` | COVERED |
| 26 | x-fixed-timeouts（client 重試／逾時／版本訊息） | sw_core/client.py、sw_core/cli.py:101-103 | cli_schema.json；`client_retry.rs`、`test_bb_client.py` | COVERED |
| 27 | x-hybrid-rpc（client 重試／逾時／版本訊息） | sw_core/client.py、sw_core/cli.py:101-103 | cli_schema.json；`client_retry.rs`、`test_bb_client.py` | COVERED |
| 28 | x-version（client 重試／逾時／版本訊息） | sw_core/client.py、sw_core/cli.py:101-103 | cli_schema.json；`client_retry.rs`、`test_bb_client.py` | COVERED |

### A4 RPC 方法（56 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `alias.assign`（params 3、errors 1） | sw_core/service.py:1174 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 2 | `alias.list`（params 0、errors 0） | sw_core/service.py:1166 | rpc_schema.json x-methods；Slice 2.1 → P5-T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 3 | `alias.set`（params 2、errors 1） | sw_core/service.py:1169 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 4 | `alias.unassign`（params 1、errors 1） | sw_core/service.py:1182 | rpc_schema.json x-methods；Slice 2.1 → P5-T15/T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 5 | `command.cancel`（params 1、errors 2） | sw_core/service.py:1233 | rpc_schema.json x-methods；Slice 2.1 → P5-T8；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 6 | `command.get`（params 1、errors 1） | sw_core/service.py:1218 | rpc_schema.json x-methods；Slice 2.1 → P5-T13；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 7 | `command.result_tail`（params 3、errors 2） | sw_core/service.py:1222 | rpc_schema.json x-methods；Slice 2.1 → P5-T14；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 8 | `command.submit`（params 10、errors 12） | sw_core/service.py:1186 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 9 | `daemon.handoff_stop`（params 1、errors 2） | sw_core/service.py:838 | rpc_schema.json x-methods；Slice 2.4 → P8-T14 | PLANNED |
| 10 | `daemon.stop`（params 0、errors 0） | sw_core/rpc_posix.py／daemon.py（server 內建） | rpc_schema.json x-methods；Slice 2.1 → P5-T17/T19；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 11 | `device.attach`（params 2、errors 5） | sw_core/service.py:1032 | rpc_schema.json x-methods；Slice 2.1 → P5-T12；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 12 | `device.list`（params 0、errors 0） | sw_core/service.py:1021 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 13 | `device.release`（params 3、errors 5） | sw_core/service.py:1024 | rpc_schema.json x-methods；Slice 2.1 → P5-T12；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 14 | `event.com_disable`（params 1、errors 0） | sw_core/service.py:1458 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 15 | `event.com_enable`（params 1、errors 0） | sw_core/service.py:1456 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 16 | `event.com_status`（params 1、errors 0） | sw_core/service.py:1460 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 17 | `event.reload`（params 0、errors 0） | sw_core/service.py:1468 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 18 | `event.reset`（params 2、errors 0） | sw_core/service.py:1462 | rpc_schema.json x-methods；Slice 2.3 → P7-T11 | PLANNED |
| 19 | `event.rule_delete`（params 1、errors 0） | sw_core/service.py:1443 | rpc_schema.json x-methods；Slice 2.3 → P7-T11 | PLANNED |
| 20 | `event.rule_get`（params 1、errors 1） | sw_core/service.py:1451 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 21 | `event.rule_list`（params 2、errors 0） | sw_core/service.py:1446 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 22 | `event.rule_set`（params 0、errors 1） | sw_core/service.py:1437 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 23 | `event.tail`（params 4、errors 0） | sw_core/service.py:1470 | rpc_schema.json x-methods；Slice 2.3 → P7-T16 | PLANNED |
| 24 | `file.pull`（params 6、errors 19） | sw_core/service.py:1405 | rpc_schema.json x-methods；Slice 2.4 → P8-T6 | PLANNED |
| 25 | `file.push`（params 7、errors 22） | sw_core/service.py:1367 | rpc_schema.json x-methods；Slice 2.4 → P8-T6 | PLANNED |
| 26 | `health.ping`（params 0、errors 0） | sw_core/service.py:1001 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 27 | `health.status`（params 0、errors 0） | sw_core/service.py:1003 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 28 | `log.tail_raw`（params 4、errors 3） | sw_core/service.py:1262 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 29 | `log.tail_text`（params 4、errors 3） | sw_core/service.py:1262 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 30 | `mcu.patterns`（params 0、errors 0） | sw_core/service.py:1006 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 31 | `mcu.status`（params 0、errors 0） | sw_core/service.py:1011 | rpc_schema.json x-methods；Slice 2.1 → P5-T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 32 | `result.tail`（params 6、errors 2） | sw_core/service.py:1237 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 33 | `session.activity`（params 4、errors 2） | sw_core/service.py:1045 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 34 | `session.attach`（params 4、errors 8） | sw_core/service.py:1109 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 35 | `session.bind`（params 5、errors 5） | sw_core/service.py:1089 | rpc_schema.json x-methods；Slice 2.1 → P5-T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 36 | `session.clear`（params 4、errors 4） | sw_core/service.py:1077 | rpc_schema.json x-methods；Slice 2.1 → P5-T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 37 | `session.console_attach`（params 5、errors 3） | sw_core/service.py:1115 | rpc_schema.json x-methods；Slice 2.2 → P6-T10 | PLANNED |
| 38 | `session.console_detach`（params 5、errors 3） | sw_core/service.py:1122 | rpc_schema.json x-methods；Slice 2.2 → P6-T10 | PLANNED |
| 39 | `session.console_list`（params 4、errors 2） | sw_core/service.py:1129 | rpc_schema.json x-methods；Slice 2.2 → P6-T10 | PLANNED |
| 40 | `session.get_state`（params 1、errors 1） | sw_core/service.py:1041 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 41 | `session.interactive_close`（params 1、errors 3） | sw_core/service.py:859 | rpc_schema.json x-methods；Slice 2.1 → P5-T14；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 42 | `session.interactive_open`（params 8、errors 6） | sw_core/service.py:854 | rpc_schema.json x-methods；Slice 2.1 → P5-T14；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 43 | `session.interactive_send`（params 3、errors 6） | sw_core/service.py:1145 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 44 | `session.interactive_status`（params 2、errors 4） | sw_core/service.py:1153 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 45 | `session.list`（params 0、errors 0） | sw_core/service.py:1038 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 46 | `session.log_start`（params 4、errors 3） | sw_core/service.py:1349 | rpc_schema.json x-methods；Slice 2.1 → P5-T14（P7-T10 golden）；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 47 | `session.log_status`（params 4、errors 2） | sw_core/service.py:1361 | rpc_schema.json x-methods；Slice 2.1 → P5-T14（P7-T10 golden）；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 48 | `session.log_stop`（params 4、errors 3） | sw_core/service.py:1355 | rpc_schema.json x-methods；Slice 2.1 → P5-T14（P7-T10 golden）；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 49 | `session.pin`（params 5、errors 4） | sw_core/service.py:1096 | rpc_schema.json x-methods；Slice 2.1 → P5-T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 50 | `session.recover`（params 6、errors 10） | sw_core/service.py:1069 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 51 | `session.self_test`（params 6、errors 2） | sw_core/service.py:1057 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 52 | `session.unpin`（params 4、errors 3） | sw_core/service.py:1103 | rpc_schema.json x-methods；Slice 2.1 → P5-T16；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 53 | `wal.current_seq`（params 0、errors 0） | sw_core/service.py:1346 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 54 | `wal.range`（params 3、errors 1） | sw_core/service.py:1319 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 55 | `wal.reset`（params 0、errors 0） | sw_core/service.py:1343 | rpc_schema.json x-methods；Slice 2.1 → P5-T18；黑箱 `tests/contract_blackbox/slice21/*`（feature/rust-refactor-p5，紅燈 TDD，未合併） | PLANNED |
| 56 | RPC wire（NDJSON framing、generic errors METHOD_NOT_FOUND/EXCEPTION/INVALID_JSON/INVALID_REQUEST/INVALID_METHOD、version 欄） | sw_core/rpc_posix.py:40-135、sw_core/rpc_win.py、sw_core/service.py:828-870 | rpc_schema.json x-wire／x-generic-errors；P5-T17（framing）、P5-T18（分派器） | PLANNED |

### A5 錯誤碼（99 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `ALIAS_NOT_FOUND`（rpc） | sw_core/session_manager.py:1735 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 2 | `AUTOBOOT_QUIET`（rec,rpc） | sw_core/service.py:671 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8 | PLANNED |
| 3 | `BASE64_DECODE_FAILED`（rpc） | sw_core/file_transfer.py:324 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 4 | `BENCH_ENDPOINT_NOT_REMEMBERED`（cli_local） | sw_core/cli.py:758 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 5 | `BENCH_STATE_IO_ERROR`（cli_local） | sw_core/cli.py:756 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 6 | `CHECKSUM_MISMATCH`（rpc） | sw_core/file_transfer.py:237 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 7 | `CHECKSUM_VERIFY_FAILED`（cli_local,rpc） | sw_core/file_transfer.py:232 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8；CLI 端已承接 | PLANNED |
| 8 | `CMD_CONTAINS_NEWLINE`（rpc） | sw_core/arbiter.py:159 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 9 | `CMD_NOT_CANCELABLE`（rpc） | sw_core/arbiter.py:260 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 10 | `CMD_NOT_FOUND`（rpc） | sw_core/arbiter.py:251 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 11 | `CMD_TOO_LONG`（rpc） | sw_core/arbiter.py:143 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 12 | `COMMAND_ABORTED_IN_FLIGHT`（rec） | （見 error_codes.yaml） | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 13 | `COMMAND_FAILED`（rec） | sw_core/arbiter.py:318 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 14 | `CONSOLE_LINE_LIMIT_TOO_SMALL`（rpc） | sw_core/file_transfer.py:376 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 15 | `CONSOLE_NOT_FOUND`（rpc） | sw_core/session_manager.py:3996 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P6 | PLANNED |
| 16 | `DAEMON_ALREADY_RUNNING`（proc） | sw_core/lock_posix.py:22 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5-T19 | PLANNED |
| 17 | `DAEMON_ALREADY_RUNNING_ELSEWHERE`（cli_local） | sw_core/cli.py:511 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 18 | `DAEMON_BINARY_NOT_FOUND`（cli_local） | sw_core/cli.py:527 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 19 | `DAEMON_EXITED`（cli_local） | sw_core/cli.py:589 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 20 | `DAEMON_NOT_READY`（cli_local） | sw_core/cli.py:618 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 21 | `DEVICE_ALREADY_BOUND`（rpc） | sw_core/session_manager.py:2049 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 22 | `DEVICE_NOT_BOUND`（rpc） | sw_core/session_manager.py:2161 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 23 | `DEVICE_NOT_FOUND`（rpc） | sw_core/session_manager.py:2116 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 24 | `DEVICE_NOT_PRESENT`（rpc） | sw_core/session_manager.py:2901 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 25 | `DEVICE_STILL_HELD`（cli_local,rpc） | sw_core/session_manager.py:2925 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5；CLI 端已承接 | PLANNED |
| 26 | `DRAIN_STALLED`（rpc） | sw_core/session_manager.py:3006 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 27 | `ECHO_ACK_UNSUPPORTED`（rpc） | sw_core/file_transfer.py:109 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 28 | `EMPTY_RESPONSE`（cli_client） | sw_core/client.py:267 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 29 | `ENV_FILE_SOURCE_FAILED`（cli_local） | sw_core/cli.py:551 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 30 | `EXCEPTION`（rpc_frame） | sw_core/rpc_posix.py:59 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P7 | PLANNED |
| 31 | `FLASHING_BUSY`（cli_local,rec,rpc） | sw_core/session_manager.py:1838 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8；CLI 端已承接 | PLANNED |
| 32 | `FLASH_TARGET_IS_CONSOLE`（rpc） | sw_core/flash_endpoint.py:292 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 33 | `FLUSHED_BY_RECOVERY`（rec） | sw_core/arbiter.py:60 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 34 | `FLUSHED_BY_SHUTDOWN`（rec,rpc） | sw_core/service.py:532 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P6, P7, P8 | PLANNED |
| 35 | `FORCE_RECOVER_TIMEOUT`（rpc） | sw_core/session_manager.py:4686 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 36 | `INTERACTIVE_EXPIRED`（rpc） | sw_core/session_manager.py:6425 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 37 | `INTERACTIVE_NOT_FOUND`（rpc） | sw_core/session_manager.py:4218 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 38 | `INTERNAL_ERROR`（cli_local） | sw_core/cli.py:160-164 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 39 | `INVALID_ARGS`（cli_local,rpc） | sw_core/service.py:732 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P6, P8；CLI 端已承接 | PLANNED |
| 40 | `INVALID_BENCH_CONFIG`（cli_local） | sw_core/cli.py:1355 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 41 | `INVALID_BENCH_STATE`（cli_local） | sw_core/cli.py:754 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 42 | `INVALID_COMPLETION_REGEX`（cli_local,rec,rpc） | sw_core/command_completion.py:8 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5；CLI 端已承接 | PLANNED |
| 43 | `INVALID_ENDPOINT`（cli_client,cli_local） | sw_core/client.py:232 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 44 | `INVALID_JSON`（rpc_frame） | sw_core/rpc_posix.py:31 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5-T17 | PLANNED |
| 45 | `INVALID_METHOD`（rpc_frame） | sw_core/rpc_posix.py:47 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5-T17 | PLANNED |
| 46 | `INVALID_REQUEST`（rpc_frame） | sw_core/rpc_posix.py:38 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5-T17 | PLANNED |
| 47 | `INVALID_RESPONSE`（cli_client） | sw_core/client.py:268-273 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 48 | `INVALID_RULE_SCHEMA`（rpc） | sw_core/service.py:1138 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P7 | PLANNED |
| 49 | `INVALID_TARGET`（cli_local） | sw_core/remote_tunnel.py:54 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 50 | `LOCAL_FILE_NOT_FOUND`（rpc） | sw_core/file_transfer.py:103 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 51 | `LOG_OPEN_FAILED`（rpc） | sw_core/session_manager.py:4787 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 52 | `METHOD_NOT_FOUND`（rpc） | sw_core/service.py:1174 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 53 | `MOVE_FAILED`（rpc） | sw_core/file_transfer.py:252 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 54 | `MOVE_TIMEOUT`（rpc） | sw_core/file_transfer.py:250 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 55 | `NEEDS_SUDO`（cli_local） | sw_core/service_ctl.py:42 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 56 | `NOT_READY`（rpc） | sw_core/session_manager.py:2146 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 57 | `NO_ACTIVE_CAPTURE`（rpc） | sw_core/session_manager.py:4810 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 58 | `NO_SYSTEMD`（cli_local） | sw_core/service_ctl.py:55 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 59 | `PROFILE_IS_EXPLICIT`（rpc） | sw_core/session_manager.py:2118 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 60 | `PROFILE_NOT_COMMAND_CAPABLE`（rpc） | sw_core/service.py:628 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8 | PLANNED |
| 61 | `PROMPT_TIMEOUT`（rec,rpc） | sw_core/session_manager.py:3927 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 62 | `PROMPT_TIMEOUT_FORCE_RECOVERED`（rpc） | sw_core/session_manager.py:3919 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 63 | `PROMPT_TIMEOUT_RECOVERED`（rec,rpc） | sw_core/session_manager.py:3897 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 64 | `PULL_PARSE_FAILED`（rpc） | sw_core/file_transfer.py:319 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 65 | `READY_UNCONFIRMED`（rec,rpc） | sw_core/service.py:655 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8 | PLANNED |
| 66 | `REMOTE_BIND_UNVERIFIED`（cli_local） | sw_core/remote_tunnel.py:313 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 67 | `REMOTE_NOT_SUPPORTED`（cli_local） | sw_core/cli.py:436 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 68 | `RULE_NOT_FOUND`（rpc） | sw_core/service.py:1150 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P7 | PLANNED |
| 69 | `SAMPLE_READ_FAILED`（cli_local） | sw_core/cli.py:1885 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 70 | `SEND_FAILED`（rec） | sw_core/arbiter.py:297 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 71 | `SESSION_BUSY`（cli_local,rec,rpc） | sw_core/session_manager.py:1062 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8；CLI 端已承接 | PLANNED |
| 72 | `SESSION_INTERACTIVE_BUSY`（rec,rpc） | sw_core/session_manager.py:3670 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P6, P8 | PLANNED |
| 73 | `SESSION_NOT_FOUND`（rpc） | sw_core/session_manager.py:1720 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8 | PLANNED |
| 74 | `SESSION_NOT_READY`（rec,rpc） | sw_core/service.py:642 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P6, P8 | PLANNED |
| 75 | `SESSION_QUEUE_FULL`（rpc） | sw_core/arbiter.py:202 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 76 | `SESSION_RECOVERING`（rec,rpc） | sw_core/session_manager.py:2145 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5, P8 | PLANNED |
| 77 | `SOCKET_ERROR`（cli_client） | sw_core/client.py:239 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 78 | `SOCKET_UNAVAILABLE`（proc） | sw_core/lock_posix.py:37 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5-T19 | PLANNED |
| 79 | `SSH_NOT_FOUND`（cli_local） | sw_core/remote_tunnel.py:598 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 80 | `STATE_CHANGED`（rpc） | sw_core/session_manager.py:2521 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 81 | `TARGET_DECODER_FAILED`（rpc） | sw_core/file_transfer.py:219 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 82 | `TARGET_DECODER_MISSING`（rpc） | sw_core/file_transfer.py:522 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 83 | `TARGET_ENCODER_FAILED`（rpc） | sw_core/file_transfer.py:318 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 84 | `TARGET_ENCODER_MISSING`（rpc） | sw_core/file_transfer.py:544 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 85 | `TEST_DEVICE_ALLOWLIST_REJECTED`（proc） | sw_core/test_allowlist.py:125 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5-T19 | PLANNED |
| 86 | `TIMEOUT`（cli_client,cli_local） | sw_core/client.py:249 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 87 | `TRANSFER_ECHO_STALL`（rpc） | sw_core/file_transfer.py:198 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 88 | `TRANSFER_PROBE_FAILED`（rpc） | sw_core/file_transfer.py:521 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 89 | `TRANSFER_TIMEOUT`（rpc） | sw_core/file_transfer.py:210 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P8 | PLANNED |
| 90 | `TUNNEL_CONFLICT`（cli_local） | sw_core/remote_tunnel.py:451 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 91 | `TUNNEL_SPAWN_FAILED`（cli_local） | sw_core/remote_tunnel.py:180 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 92 | `TUNNEL_STATUS_ERROR`（cli_local） | sw_core/cli.py:1265 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 93 | `TUNNEL_STILL_ACTIVE`（cli_local） | sw_core/cli.py:1390 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 94 | `UNKNOWN_BENCH_CODE`（cli_local） | sw_core/cli.py:1353 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 95 | `UNKNOWN_EVENT_CMD`（cli_local） | sw_core/cli.py:1064 | error_codes.yaml；`sw_proto::error_codes`；sw-cli 發出（`crates/sw-cli/tests/emitted_codes.rs`、mgmt_*.rs） | COVERED |
| 96 | `UNKNOWN_PROFILE`（cli_local,rpc） | sw_core/session_manager.py:2113 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5；CLI 端已承接 | PLANNED |
| 97 | `WAL_MISSING`（rpc） | sw_core/service.py:545 | error_codes.yaml；`sw_proto::error_codes`（`crates/sw-proto/tests/error_codes_sync.rs`）；daemon 發出端 P5 | PLANNED |
| 98 | session_last_errors.yaml（34 值） | sw_core/session_manager.py、login_fsm.py | contracts；`sw_proto::session_last_errors`；daemon 端 P5-T11/T13、P7 | PLANNED |
| 99 | diagnostic_aux.yaml（warning_codes／error_details／classifications／recommended_actions） | sw_core/session_manager.py、login_fsm.py | contracts；`sw_proto::session_last_errors`；daemon 端 P5-T11/T13、P7 | PLANNED |

### A6 環境變數（34 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `SERIALWRAP_CONFIG_DIR` | sw_core/constants.py:27、cli.py:72、bench_registry.py:45 | cli_schema x-env；`sw_proto::runtime_paths`＋`contracts/golden/runtime_paths.{posix,windows}.tsv`（`runtime_paths_golden.rs`） | COVERED |
| 2 | `SERIALWRAP_STATE_DIR` | sw_core/constants.py:28,39、cli.py:1236 | 同上；daemon 端 P5-T0c `resolve_daemon_dirs` | COVERED |
| 3 | `SERIALWRAP_RUN_DIR` | sw_core/constants.py:46、cli.py:1704-1707 | 同上（含「只設 STATE_DIR → RUN_DIR=STATE_DIR」規則） | COVERED |
| 4 | `SERIALWRAP_CONFIG（SC-09）` | sw_core/constants.py:105 | cli_schema x-env；`runtime_paths.rs:339-357`；`runtime_config_write.rs` | COVERED |
| 5 | `SERIALWRAP_PROFILE_DIR` | sw_core/constants.py:69 | x-env；runtime_paths golden；daemon `--profile-dir` 預設 P5-T19 | COVERED |
| 6 | `SERIALWRAP_ENDPOINT` | sw_core/constants.py:60,64 | x-env；`endpoint_golden.rs`、`endpoint_resolve.rs` | COVERED |
| 7 | `SERIALWRAP_TCP_PORT` | sw_core/constants.py:55 | x-env；runtime_paths golden（default_tcp_port） | COVERED |
| 8 | `SERIALWRAP_RPC_BACKEND（CLI 端）` | sw_core/cli.py:783-796、platform_backends.py:48 | x-env；P3 Q-17；`endpoint_resolve.rs`（win_backend_default_tcp／bogus_backend_uses_platform） | COVERED |
| 9 | `SERIALWRAP_RPC_BACKEND／SERIALWRAP_LOCK_BACKEND／SERIALWRAP_DEVICE_BACKEND（daemon 端）` | sw_core/platform_backends.py:24-76 | P5-T19 `check_backend_env`（06 L4114、L4282）：非本 build 原生後端 → exit 1；Python 可在 POSIX 強制 win 後端 | DIVERGENT |
| 10 | `SERIALWRAP_SERIAL_BACKEND（auto／posix／pyserial）` | sw_core/serial_port.py:774-782；README.md:2492；CLAUDE.md | 無契約、無計畫任務、Rust 無讀取點 | GAP |
| 11 | `SERIALWRAP_BENCHES_FILE` | sw_core/bench_registry.py:50 | x-env；`crates/sw-proto/tests/bench_registry.rs` | COVERED |
| 12 | `SERIALWRAP_LOG_LEVEL` | sw_core/cli_trace.py:53 | x-env；`crates/sw-cli/tests/cli_trace.rs` | COVERED |
| 13 | `SERIALWRAP_DAEMON_ENV_FILE` | sw_core/cli.py:236,352 | x-env；`mgmt_daemon.rs` | COVERED |
| 14 | `SERIALWRAP_DATA_DIR／XDG_DATA_HOME` | sw_core/constants.py:47、setup_cmd.py:53-57 | runtime_paths golden（data_dir）；`mgmt_setup.rs`、`mgmt_layout.rs` | COVERED |
| 15 | `XDG_CONFIG_HOME／XDG_STATE_HOME／XDG_RUNTIME_DIR` | sw_core/constants.py:22-25,259-264 | x-env；runtime_paths golden | COVERED |
| 16 | `ProgramData（Windows 系統設定目錄）` | sw_core/constants.py:31 | `sw-pal/src/sysenv/windows.rs:85-90`；runtime_paths.windows.tsv | COVERED |
| 17 | `SERIALWRAP_TEST_SYSTEM_CONFIG_PATH` | sw_core/constants.py:35；tests/conftest.py:93-94 | 無契約、無計畫；Rust `SysEnv::system_config_dir()` 固定 `/etc/serialwrap`（`sw-pal/src/sysenv/posix.rs:64`），不讀此 env；p5 `sw-daemon/tests/support/mod.rs:137` 有設定但無消費端 | GAP |
| 18 | `SERIALWRAP_BY_ID_DIR／SERIALWRAP_BY_PATH_DIR` | sw_core/constants.py:70-71 | P5-T0c `DaemonDirs{by_id_dir,by_path_dir}`（06 L83、L538 `daemon_dirs_defaults`）；P5-T5 | PLANNED |
| 19 | `SERIALWRAP_WAL_DIR` | sw_core/constants.py:68、doctor_cmd.py:392 | CLI doctor 已承接（`mgmt_doctor.rs`）；daemon WAL P5-T7 | PLANNED |
| 20 | `SERIALWRAP_LOG_DIR` | sw_core/constants.py:72 | runtime_paths golden（log_dir）；daemon capture P5-T14／P7（08 L1420 `log_path_for`） | PLANNED |
| 21 | `SERIALWRAP_EVENTS_DIR／SERIALWRAP_EVENTS_RUNTIME_DIR` | sw_core/constants.py:83-89 | P5-T0c `DaemonDirs{events_dir,events_runtime_dir}`（06 L83）；消費端 P7 event store | PLANNED |
| 22 | `SERIALWRAP_EVENTS_LOG_PATH` | sw_core/constants.py:90-93、service.py:285 | P7 `EngineDeps.log_path`（08 L2023）未定義解析規則；p5 `DaemonDirs` 無此欄；無 golden | GAP |
| 23 | `SERIALWRAP_TTYMCU_PATH` | sw_core/constants.py:66 | P8（09 L30、L1810：`RuntimePaths.ttymcu_path`＋golden 加欄） | PLANNED |
| 24 | `SERIALWRAP_SOCKET_GROUP` | sw_core/rpc_posix.py:114 | P5-T17 `listen_options`（06 L22、L4117；SC-08 撤回／OD-14 #3） | PLANNED |
| 25 | `SERIALWRAP_TEST_DEVICE_ALLOWLIST（SC-19、OD-11）` | sw_core/test_allowlist.py:27,108 | error_codes TEST_DEVICE_ALLOWLIST_REJECTED；P5-T5／T19（06 L4167 `allowlist_rejected_reasons_exact`） | PLANNED |
| 26 | `SERIALWRAP_TEST_KILL_AFTER_SAVE（隔離實例測試鉤子）` | sw_core/session_manager.py:8126-8140 | `contracts/daemon_logging.yaml:385-392` 已登記其 log 訊息；06～09 無任何 Task 承接；`realhw/procedures/stage0-release-kill.md` 依賴 | GAP |
| 27 | `SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE（隔離實例測試鉤子）` | sw_core/session_manager.py:8152-8165 | `contracts/daemon_logging.yaml:399` 已登記；06～09 無 Task 承接 | GAP |
| 28 | `SERIALWRAP_RELEASE_MANIFEST（SC-25）` | sw_core/release_manifest.py:29 | `sw_proto::release_manifest`、`crates/sw-proto/tests/release_manifest.rs`；P4 | COVERED |
| 29 | `SERIALWRAP_EVENT_*（11 鍵：SCHEMA_VERSION、RULE_ID、OWNER、KIND、SELECTOR、MATCHED_AT、MATCHED_TEXT、FIRE_COUNT、WAL_SEQ、LEVEL、SCOPE）` | sw_core/event_engine/dispatcher.py:48-68 | P7（08 L406 `event_payload` golden、L1935 `build_env`、L1994） | PLANNED |
| 30 | handler 繼承白名單 HOME／LANG／USER／PATH | sw_core/event_engine/dispatcher.py:16,52 | P7（08 L1994 `inherited_env_only_whitelisted_present`）；Windows 另加 6 鍵為 SC-26（Rust-only） | PLANNED |
| 31 | `SUDO_USER／USER／LOGNAME（setup linger、sudo 身分）` | sw_core/setup_cmd.py:314,551 | `crates/sw-cli/src/setup_cmd.rs`；`mgmt_setup.rs` | COVERED |
| 32 | `PATH／PATHEXT（which）` | sw_core/sysenv.py:144-147 | `sw-pal/src/sysenv`；`mgmt_doctor.rs` | COVERED |
| 33 | profile `user_env`／`pass_env`／`console_unlock_env` 指名之帳密 env（env_file → os.environ fallback） | sw_core/auth.py:84-144、login_fsm.py:96-113 | P5-T13（最小 login＋帳密）、P7（`CREDENTIALS_UNRESOLVED` 終態） | PLANNED |
| 34 | minicom wrapper env：SERIALWRAP_BIN／SERIALWRAP_SOCKET／SERIALWRAP_AUTO_START_DAEMON／SERIALWRAP_ATTACH_WHEN_NOT_READY／SERIALWRAP_ATTACH_WAIT_TICKS／SERIALWRAP_PREFERRED_COM／MINICOM_* | sw_core/assets/tools/minicom_router.sh:13-31 | wrapper 為原樣 shell 資產，由 Rust setup 物化（05 L1636 `WRAPPER_MAP`、L3096、L3146 黑箱） | COVERED |

### A7 config.yaml 鍵（7 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `supervision_mode` | sw_core/runtime_config.py:19-30 | `sw_proto::config_types::RuntimeConfig`；`mgmt_supervision_switch.rs`、`mgmt_service.rs` | COVERED |
| 2 | `socket_path` | sw_core/runtime_config.py:22-40、daemon 啟動後 set_socket | CLI 讀取 COVERED（`endpoint_resolve.rs`）；daemon 寫入 P5-T19（06 L4084：RPC 綁定後寫 socket_path） | PLANNED |
| 3 | `windows.exclude_coms` | sw_core/device_source.py:20-40 | CLI doctor 已承接（p5 `sw-cli/src/doctor_cmd.rs:1255`）；daemon P5-T5（06 L1414，`RuntimeConfig::windows_exclude_coms`） | PLANNED |
| 4 | `windows.test_device_allowlist（OD-11）` | sw_core/test_allowlist.py:60-75 | P5-T0c accessor、P5-T5／T19 | PLANNED |
| 5 | 壞 YAML／非 dict 內容處理（DF-07） | sw_core/runtime_config.py:13-17、cli.py supervision-mode | DF-07（雙邊）；`mgmt_supervision_switch.rs` | COVERED |
| 6 | 設定探索順序（SERIALWRAP_CONFIG → CONFIG_DIR → 系統 /etc 或 %ProgramData%）（SC-09） | sw_core/constants.py:100-130 | `runtime_paths.rs:339-357`；`runtime_paths_golden.rs` | COVERED |
| 7 | `windows.serial_console`（SC-29，Rust-only；僅在 feature/rust-refactor-p6 登記） | —（Python 無此鍵） | P6；OD-01／OD-08 | DIVERGENT |

### A8 profiles/*.yaml 結構（24 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `defaults` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 2 | `defaults.log_dir` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 3 | `profiles` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 4 | `targets` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 5 | `platform` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 6 | `prompt_regex` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 7 | `login_regex` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 8 | `password_regex` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 9 | `user_env／username_env／login_env／username（別名）` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 10 | `pass_env／password_env／pw_env（別名）` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 11 | `env_file` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 12 | `post_login_cmd` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 13 | `ready_probe` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 14 | `timeout_s` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 15 | `hard_timeout_s` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 16 | `bootloader_prompts` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 17 | `console_unlock_env` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 18 | `console_unlock_idle_s` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 19 | `quiet_window_s` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 20 | `max_console_line_chars` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 21 | `max_sessions` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 22 | `log_dir（per-profile／per-target）` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 23 | `uart.baud／data_bits／parity／stop_bits／flow_control／xonxoff` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |
| 24 | `targets[].com／alias／profile／profile_name／device_by_id／act_no` | sw_core/config.py（`load_profiles`；`.get()` 共 55 處） | `sw_proto::config_types`（P3）；`contracts/golden/load_profiles.tsv`＋`profiles_fixtures`（`config_types_golden.rs`）；daemon 行為消費 P5-T13／P7（login）、P8（max_console_line_chars） | COVERED |

### A9 state.json／WAL／mirror／事件與日誌檔（10 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | state.json 頂層 aliases／bindings／profile_detected／profile_pins／released（DISK_STABLE、0600、mkstemp→fsync→replace） | sw_core/session_manager.py:1270-1400 | `contracts/state_file.schema.json`；P5-T15 StateStore（p5 `sw-core/src/state_store.rs`、`tests/session_manager.rs`）；DF-04 | PLANNED |
| 2 | WAL 記錄 12 欄（cmd_id、com、crc32、dir、len、loss_flag、meta、mono_ts_ns、payload_b64、seq、source、wall_ts）＋rotation_failed | sw_core/wal.py | `contracts/wal_record.schema.json`、`golden/wal_line.tsv`；`sw_proto::wal_types`（`wal_types_golden.rs`）；寫入端 P5-T7（p5 `tests/wal_writer.rs`）；BC-01、DF-03、DF-15、DF-17 | PLANNED |
| 3 | WAL META 事件 7 種（credentials_unresolved、transport_stall_suspected、command_completion、WAL_RECREATED_AFTER_EXTERNAL_UNLINK、WAL_ROTATED_AFTER_DEFERRAL、tx_outcome_unknown、command_aborted_in_flight） | sw_core/session_manager.py:1893,1992,6469,6574；uart_io.py:2240；wal.py:298,570 | `contracts/wal_meta_events.schema.json`；P5-T7／T9／T14、P7（stall／credentials） | PLANNED |
| 4 | raw.mirror.log 鏡像行格式 | sw_core/wal.py | `golden/wal_line.tsv`、`pycompat_s21.tsv` to_printable；P5-T7 | PLANNED |
| 5 | WAL 輪替 64 MiB、歸檔名 `.{%Y%m%d-%H%M%S}`（UTC）、同名不覆蓋 | sw_core/wal.py:200-215,930-940；constants.py:73 | wal_record.schema x-rotate；DF-03；P5-T6／T7 | PLANNED |
| 6 | agent log 檔名 `{log_dir}/{COM}_{%y%m%d-%H%M%S 本地}.log`、log_dir 優先序 | sw_core/session_manager.py:7818-7870 | P5-T14（06 L3356）、P7（08 L1420、L1471 `log_path_uses_local_time`） | PLANNED |
| 7 | events.d/<rule_id>.json（indent=2, sort_keys）、counter 檔、events.ndjson（10 MiB×3 輪替） | sw_core/event_engine/registry.py:62-106、counter.py:54、event_log.py:32；constants.py:94-95 | P7（08 L23 序列化規則、L1682 event store；DF-01／DF-09） | PLANNED |
| 8 | benches.yaml（SERIALWRAP_BENCHES_FILE） | sw_core/bench_registry.py:40-174 | `sw_proto::bench_registry`（`bench_registry.rs` 測試）；P4 | COVERED |
| 9 | benches.state.json（記住的 bench endpoint） | sw_core/cli.py:1235-1350 | `crates/sw-cli/src/bench_state.rs`；`mgmt_connect.rs` | COVERED |
| 10 | remote tunnel 狀態 `<run_dir>/remote/`、ControlPath | sw_core/cli.py:1700-1712、remote_tunnel.py:83-180 | `crates/sw-cli/src/remote_tunnel.rs`；`mgmt_remote.rs`；BC-05 | COVERED |

### A10 socket／lock／路徑與監管模式（15 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | LOCK_PATH `<RUN_DIR>/serialwrapd.lock`、SOCKET_PATH `<RUN_DIR>/serialwrapd.sock` | sw_core/constants.py:51-52 | runtime_paths golden；SC-10（雙邊）；daemon 取鎖 P5-T4 | PLANNED |
| 2 | Windows 預設 endpoint `tcp://127.0.0.1:48700`、loopback 白名單 | sw_core/constants.py:55-62 | endpoint golden；daemon P5-T17 | PLANNED |
| 3 | SYSTEM_SOCKET `/run/serialwrap/serialwrapd.sock`（systemd-system） | sw_core/setup_cmd.py:302、cli.py:896 | `crates/sw-cli/src/setup_cmd.rs:801`；`mgmt_setup.rs` | COVERED |
| 4 | TTYMCU `<RUN_DIR>/dev/ttyMCU` | sw_core/constants.py:66 | P8（09 L1809-1810） | PLANNED |
| 5 | POSIX UDS 權限 0660（不設 0700） | sw_core/rpc_posix.py:100-125 | P5-T17（SC-08 撤回，OD-14 #3） | PLANNED |
| 6 | Windows singleton lock（LockFileEx，SC-10） | sw_core/lock_win.py | SC-10（Python 0.4.0 已同步）；P5-T4 | PLANNED |
| 7 | supervision_mode `on-demand`（預設） | sw_core/cli.py:335,474,646 | `mgmt_daemon.rs`、`mgmt_service.rs` | COVERED |
| 8 | supervision_mode `systemd-user`（unit 渲染、linger） | sw_core/systemd_units.py:15-40、setup_cmd.py:540-560 | `supervisor_defs.rs`；`mgmt_setup.rs` | COVERED |
| 9 | supervision_mode `systemd-system` | sw_core/systemd_units.py:42-80、setup_cmd.py | `mgmt_setup.rs`；DF-10、DF-11 | COVERED |
| 10 | daemon start/stop 在 systemd 模式 route 到 service（#108）、canonical socket fallback | sw_core/cli.py:638-700,856-900 | x-hybrid-rpc；`mgmt_daemon.rs`、`endpoint_resolve.rs` | COVERED |
| 11 | legacy `~/.paul_tools` 偵測（只指引不刪） | sw_core/setup_cmd.py:123-140 | `mgmt_setup.rs:357` | COVERED |
| 12 | multi_open／foreign_holders（#101，/proc 掃描） | sw_core/multi_open.py | CLI doctor／daemon status：P4（doctor）、daemon `health.status` P5-T18；Windows 面 SC-17（Rust-only） | PLANNED |
| 13 | supervision_mode `windows-task`／`windows-service`（SC-24） | —（Python 無） | P4 已實作；OD-03、OD-12 | DIVERGENT |
| 14 | Windows on-demand 首次需求啟動（SC-18） | — | P4；Rust-only | DIVERGENT |
| 15 | Windows 多開偵測以行程掃描實作（SC-17） | sw_core/multi_open.py（Windows 恆失效） | P4；Rust-only | DIVERGENT |

### A11 serialwrapd 行程介面（5 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `serialwrapd --profile-dir`（預設 PROFILE_DIR） | sw_core/daemon.py:75 | P5-T19（06 L4154 usage 逐字） | PLANNED |
| 2 | `serialwrapd --socket`（預設 DEFAULT_ENDPOINT） | sw_core/daemon.py:77 | P5-T19 | PLANNED |
| 3 | `serialwrapd --lock`（預設 LOCK_PATH） | sw_core/daemon.py:78 | P5-T19 | PLANNED |
| 4 | exit code 0／1／2／3 與 stderr `serialwrapd: …` 形狀（lock 衝突、no profiles loaded、allowlist 拒絕、RPC 綁定失敗） | sw_core/daemon.py:230-316 | P5-T19（06 L4084、L4154-4171）；DF-14、SC-11 | PLANNED |
| 5 | Stage 1 過渡：`sw-daemon` launcher 委派 Python daemon | — | P4 已實作（`crates/sw-daemon/src/launcher.rs`、`tests/launcher.rs`） | COVERED |

### A12 安裝器／腳本／資產／入口（9 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `serialwrap`／`serialwrapd` console_scripts 名稱 | pyproject.toml:24-26 | Rust bin 同名（sw-cli／sw-daemon） | COVERED |
| 2 | repo 根 shim `serialwrap`、`serialwrap.py`、`serialwrapd.py` | serialwrap:1-5、serialwrap.py、serialwrapd.py | Rust 以原生 bin 取代；Stage 1 委派入口由 manifest `python_cli_delegate_entry` 指定（`delegate.rs:202`） | COVERED |
| 3 | assets：profiles/default.yaml、profiles/brcm.env.example | sw_core/assets/profiles/ | `crates/sw-cli/src/assets.rs:17-18`（include_bytes）；`mgmt_setup.rs` | COVERED |
| 4 | assets：skill/SKILL.md、SKILL_WINDOWS.md（`serialwrap skill`） | sw_core/assets/skill/ | `skill_cmd.rs` 測試；DF-16 | COVERED |
| 5 | assets：tools/minicom_router.sh、minicom-broker.sh、minicom-raw.sh → serialwrap-minicom{,-broker,-raw} | sw_core/assets/tools/ | P4（05 L1636 WRAPPER_MAP、L3096-3146）；DF-13 | COVERED |
| 6 | `tools/bench-issue.sh`／`tools/bench-enroll.sh`（管理端 shell，產出 benches.yaml） | tools/bench-issue.sh、tools/bench-enroll.sh | 不屬 Python 套件；保留 shell；其輸出 benches.yaml 由 Rust `bench_registry` 讀取（有 golden） | COVERED |
| 7 | `install.sh`（pipx／pip --user ＋ `serialwrap setup`） | install.sh:1-25 | Rust 改為 bundle＋`serialwrap setup` 安裝佈局（arch §10.2、05 Task 10/17、SC-23／SC-25）；無對應 install.sh | DIVERGENT |
| 8 | `scripts/build_windows.ps1`＋`serialwrap.spec`（PyInstaller one-file） | scripts/build_windows.ps1:1-40、serialwrap.spec | `scripts/rust/mkbundle.sh`＋release manifest（SC-25，OD-13）；`check_no_onefile_extract.py` | DIVERGENT |
| 9 | systemd unit ExecStart 可執行檔解析（Python `shutil.which`，Rust 不搜尋 PATH、固定 `<InstallRoot.bin>/serialwrapd`） | sw_core/setup_cmd.py | DF-05 附註（05 L1683） | DIVERGENT |

### A13 Rust-only 新增介面（Python 無對應）（8 項）

| # | 項目 | Python 位置 | Rust 承接（契約／golden／計畫任務／測試） | 狀態 |
|---:|---|---|---|---|
| 1 | `supervision-mode set [--with-sudo] [--force]`（SC-22） | — | cli_schema x-rust-only；P4 | DIVERGENT |
| 2 | `setup --rollback`（SC-23） | — | cli_schema x-rust-only；P4 | DIVERGENT |
| 3 | `setup --with-com0com`／`--remove-com0com`（SC-36） | — | cli_schema x-rust-only；P4；OD-15、OD-01、OD-08 | DIVERGENT |
| 4 | console 出向 ring 1 MiB drop-oldest（SC-15） | — | P6（p6 分支 `out_ring.rs`） | DIVERGENT |
| 5 | Windows event handler（CreateProcessW＋Job、cmd.exe /d /s /c、env 白名單 +6 鍵）（SC-26） | sw_core/event_engine/dispatcher.py（POSIX only） | P7；OD-14 #6 | DIVERGENT |
| 6 | `SERIALWRAP_CONSOLE_BACKEND`（auto／pty／tcp）（SC-28，p6 分支登記） | — | P6 | DIVERGENT |
| 7 | Console 失效上下文與資源上限細化（SC-31，p6 分支登記） | — | P6；OD-04 | DIVERGENT |
| 8 | Rust-only env：SERIALWRAP_INSTALL_ROOT、SERIALWRAP_DELEGATE_DEPTH／_ARGV_SHA、SERIALWRAP_TEST_DELEGATE_GROUP | — | `install_layout.rs`、`delegate.rs:111-273`、`lib.rs` | DIVERGENT |
