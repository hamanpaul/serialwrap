# serialwrap Rust 架構（全 Phase 共用）

> 依據：`docs/rust-refactor/serialwrap-rust-spec-final.md`（下稱「規格」，章節以 §N 表示）、`docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-01～OD-13）、`docs/rust-refactor/plans/00-master-plan.md`、`docs/rust-refactor/inventory/01～10`（Python 基線，HEAD `93553e6`，`VERSION`=`0.3.2`）。
> 規格與程式碼的逐項裁決見同目錄 `spec-reconciliation.md`（下稱「調和表」，項目以 `RPC-H-01`、`SC-04`、`DF-03` 之類 ID 引用）。
> 本文件是 P3～P9 各分計畫的共同基準。分計畫可細化，不得與本文件牴觸；要牴觸時先改本文件並經審閱。

---

## 0. 基本原則

1. **Python 基線就是契約**（規格 §1.3、§5.3 末段）。標為「待基線擷取」的值由 Stage 0 擷取並凍結進 `contracts/`，實作者沒有選擇權。
2. **凡偏離基線，必須有登記**：已核准的 BC-01～BC-05；依定稿規格推導的 `SC-xx`；缺陷或安全修正 `DF-xx`。三者都寫進 `contracts/behavior_changes.yaml`，並且先在 Python（Stage 0）實作，Rust 再承接。調和表標為「Rust-only」的項目例外：它們不參與 A/B 差分，只驗絕對契約。
3. **位元組級相容的範圍**：只限契約要求的面，包括 CLI stdout／stderr／exit code、RPC wire、WAL／mirror、`state.json`、`benches.state.json`、remote registry、events.ndjson、規則檔與 counter 檔、systemd unit 文字、`config.yaml`。日誌文字與 `EXCEPTION.message` 不在此列（§2.6）。
4. **平台差異只能出現在 `sw-pal`**（§3.1）。業務 crate 透過 capability 值與 trait 物件取得平台能力。
5. **有界資源**：沒有無界 channel，也沒有無界 buffer（§5.4）。所有容量列在 §5.10。

---

## 1. Cargo Workspace

### 1.1 版本與工具鏈

| 項目 | 值 |
|---|---|
| edition | `2021`（與 master plan 一致） |
| MSRV | `rust-version = "1.96"`；`rust-toolchain.toml` 釘 `channel = "1.96.1"`、`targets = ["x86_64-unknown-linux-gnu","x86_64-pc-windows-gnu"]` |
| Linux target | `x86_64-unknown-linux-gnu` |
| Windows target | `x86_64-pc-windows-gnu`。開發迴圈在 WSL 以 `x86_64-w64-mingw32-gcc` 交叉連結，再經 interop 執行（§9.4）。**正式 gate 與 release artifact 一律在擁有者 Windows 自架 runner 上原生 build/link**（同一 target triple，`stable-x86_64-pc-windows-gnu` host toolchain），以符合 ADR-01「cross check 不取代原生 build/link」 |
| profile.release | `opt-level=3`、`lto="thin"`、`codegen-units=1`、`panic="unwind"`、`debug=1`、`strip="none"`（符號另存）；RPC 邊界以 `catch_unwind` 對應 Python 的 `EXCEPTION` |
| `.cargo/config.toml` | `[target.x86_64-pc-windows-gnu] linker = "x86_64-w64-mingw32-gcc"`、`runner = "scripts/rust/win-runner.sh"` |
| Windows 連結檢查 | release pipeline 以 `objdump -p` 確認匯入 DLL 只有 `KERNEL32`、`ADVAPI32`、`WS2_32`、`ntdll`、`bcryptprimitives`／`bcrypt`、`USERENV`、`msvcrt`／`api-ms-win-crt-*`；出現 mingw 執行期 DLL 時判定失敗 |

### 1.2 依賴（全部 `=` 釘版本；版本至 2026-10-15 皆已發佈兩週以上）

| crate | 版本 | 使用者 | 理由 |
|---|---|---|---|
| `indexmap` | `=2.11.4` | sw-proto | `PyDict` 需要保留插入序（Python dict 語意）。MSRV 1.63 |
| `fancy-regex` | `=0.16.2`（`default-features=false, features=["std","unicode","perf"]`） | sw-proto | Python `re` 方言的**執行**引擎：支援 lookaround、backreference、atomic group，並以 `backtrack_limit` 限制回溯（ADR-01）。語法**接受與否**由自製 `pyre` 決定（§3.6） |
| `yaml-rust2` | `=0.10.4`（`default-features=false`） | sw-proto | 只用低階事件解析器：它保留 scalar style，plain 與 quoted 才能分辨，再由自製 resolver 重現 PyYAML `SafeLoader` 的 YAML 1.1 解析（§3.7）。serde_yaml 走 YAML 1.2 且丟失 style，不採用 |
| `crc32fast` | `=1.5.0` | sw-proto | IEEE CRC-32（zlib 相同 poly／init／xorout） |
| `sha2` | `=0.10.9` | sw-proto | `cli_trace` 的 `endpoint_id`、remote `identity`、release manifest digest |
| `crossbeam-channel` | `=0.5.15` | sw-pal、sw-core、sw-daemon | 有界 MPMC 與 `select_biased!`：mailbox 的 control lane 優先、deadline receive |
| `libc` | `=0.2.186` | sw-pal（`cfg(unix)`） | poll、flock、`TIOCEXCL`、termios、openpty、`/proc`、`getrandom` 等 POSIX syscall |
| `windows-sys` | `=0.61.2` | sw-pal（`cfg(windows)`） | `CreateFileW`、overlapped I/O、`CancelIoEx`、`LockFileEx`、COMM、Toolhelp32、Job Object、`BCryptGenRandom`、`FILE_SHARE_DELETE`、`GetFileInformationByHandleEx`、Winsock、Console。features：`Win32_Foundation`、`Win32_Storage_FileSystem`、`Win32_System_IO`、`Win32_Devices_Communication`、`Win32_System_Threading`、`Win32_Networking_WinSock`、`Win32_System_Console`、`Win32_System_JobObjects`、`Win32_System_Diagnostics_ToolHelp`、`Win32_Security`、`Win32_Security_Cryptography`、`Win32_System_Registry`、`Win32_System_Time`、`Win32_System_SystemInformation`、`Wdk_System_Threading` |
| dev：`tempfile` | `=3.23.0` | 各 crate 測試 | 隔離目錄 |

**刻意不採用**：

- `serde`／`serde_json`：RPC 參數以 Python 方式強制轉型，例如 `int("5")`、`0 or 10`、`bool("false")`，typed 反序列化反而錯；此外還需要 Python `repr(float)`、任意精度 int 與 `NaN`，所以改用自製 `pyjson`（§3.3）。
- `clap`：做不到 argparse 的 usage、help、錯誤文字與 exit 2 的位元組級相同，所以改寫 argparse 相容層（§7.2）。
- `tokio`：理由見 §1.4。
- `base64`：要逐字重現 `binascii.a2b_base64` 的非 strict 語意（§3.4）。
- `chrono`：只需要 UTC civil 轉換與本地 offset，後者由 PAL 提供。
- `getrandom`：亂數由 `SysEnv::random_bytes` 提供，測試可注入。

### 1.3 目錄與檔案（以規格 §3.2 為基礎；**粗體**＝相對規格新增或搬移，理由見調和表 `WAL-D18`、`WAL-D19`、`GAP-G-11`）

```text
serialwrap/
├── Cargo.toml                      # [workspace] members=["crates/*"], resolver="2", [workspace.package] edition/rust-version
├── rust-toolchain.toml
├── .cargo/config.toml
├── contracts/
│   ├── error_codes.yaml            # RPC/CLI error_code（含 interface、reachable、retryable…）
│   ├── session_last_errors.yaml    # session last_error 獨立命名空間
│   ├── behavior_changes.yaml       # BC-01..05、SC-xx、DF-xx
│   ├── rpc_schema.json
│   ├── cli_schema.json
│   ├── wal_record.schema.json
│   ├── state_file.schema.json
│   ├── **cli_help/**               # argparse help／usage／error golden（Python 3.12, COLUMNS=80 與 120）
│   └── **release_manifest.schema.json**
├── crates/
│   ├── sw-proto/                   # 純資料與 Python 相容語意；無 OS 依賴
│   │   └── src/ lib.rs constants.rs error_codes.rs **session_last_errors.rs** rpc_types.rs config_types.rs wal_types.rs
│   │            **pyjson.rs pycompat.rs pyfmt.rs version.rs release_manifest.rs bench_registry.rs runtime_paths.rs endpoint.rs**
│   │            **pyre/{mod.rs,parse.rs,translate.rs,redos.rs}  yaml11/{mod.rs,resolve.rs,emit.rs}**
│   ├── sw-pal/                     # 唯一允許平台 cfg 的 crate
│   │   └── src/ lib.rs traits.rs sysenv.rs serial.rs device_source.rs device_watcher.rs lock.rs wal_fs.rs
│   │            rpc_transport.rs console_pty.rs console_com0com.rs service_ctl.rs process_tunnel.rs
│   │            **console_tcp.rs proc_scan.rs clock.rs fs_ops.rs process.rs signal.rs fakes/{mod.rs,serial.rs,clock.rs,fs.rs,proc.rs,console.rs}**
│   │            （每個平台相依檔另有 `<name>/posix.rs`、`<name>/windows.rs` 子模組）
│   ├── sw-core/                    # 業務：actor、仲裁、登入、WAL、console、事件
│   │   └── src/ lib.rs session_manager.rs uart_bridge.rs telnet_console.rs arbiter.rs login_fsm.rs wal.rs
│   │            file_transfer.rs flash_endpoint.rs transport_stall.rs alias_registry.rs session_auth.rs
│   │            event_engine/{mod.rs,schema.rs,matcher.rs,line_buffer.rs,counter.rs,registry.rs,dispatcher.rs,event_log.rs}
│   │            **session_core.rs actor.rs mailbox.rs generations.rs tx_writer.rs drain.rs deferred.rs quiet_gate.rs
│   │            lease.rs capture.rs human_console.rs self_test.rs state_store.rs mcu_patterns.rs resources.rs**
│   │            （規格的 bench_registry.rs → sw-proto；state_migrate.rs → sw-cli）
│   ├── sw-daemon/                  # bin `serialwrapd`
│   │   └── src/ main.rs service.rs daemon_main.rs **framing.rs health.rs launcher.rs**
│   └── sw-cli/                     # bin `serialwrap`
│       └── src/ main.rs client.rs remote_tunnel.rs doctor_cmd.rs setup_cmd.rs cli_trace.rs
│                **argparse/{mod.rs,parser.rs,help.rs,usage.rs,error.rs} cli_spec.rs output.rs endpoint_resolve.rs
│                delegate.rs bench_state.rs state_migrate.rs install_layout.rs rollback.rs assets.rs
│                commands/{rpc_cmds.rs,daemon_cmd.rs,service_cmd.rs,supervision_cmd.rs,remote_cmd.rs,connect_cmd.rs,
│                          benches_cmd.rs,event_cmd.rs,profile_cmd.rs,skill_cmd.rs}**
├── scripts/rust/ **win-runner.sh win-test.sh check_cfg_boundary.py mkbundle.sh capture_cli_help.py gen_error_codes.py**
├── tests/contract_blackbox/        # Python pytest 黑箱；不 import sw_core
└── tests/acceptance/               # 案例承接清單與 schema（規格 §3.2）
```

依賴方向是單向的，CI 以 `cargo metadata` 檢查：

```text
sw-proto  ←  sw-core  ←  sw-daemon
   ↑           ↑
sw-pal ────────┘ ←  sw-cli（只依賴 sw-proto + sw-pal；不得連結 sw-core）
```

`sw-pal` 不依賴 `sw-proto`，平台型別在 PAL 內自行定義。`sw-cli` 不連結 `sw-core`，避免 CLI 夾帶 daemon 邏輯並維持啟動成本。CLI 需要的純資料語意（regex 驗證、profile 載入、bench registry）放在 `sw-proto`。

### 1.4 執行緒模型：std thread＋有界 crossbeam channel（不採用 tokio）

| 規格要求 | 為何以 thread 滿足、tokio 不合適 |
|---|---|
| §5.2.3 Windows `CancelIoEx` 只是取消**要求**；確認完成前不得重用 OVERLAPPED／buffer | 每個 handle 由專屬 I/O worker thread 持有 OVERLAPPED 與 buffer，取消後必定 `GetOverlappedResult(bWait=TRUE)` 才釋放。tokio 的 future drop 不保證等待完成；mio 也沒有 COM 埠的 overlapped 支援 |
| §5.2.3 POSIX 可喚醒讀取 | reader 以 `poll([serial_fd, wake_fd])` 等待；wake 走 `eventfd`，非 Linux 用 self-pipe |
| §5.2.4 等待 drain 不得阻塞 SessionActor mailbox | 等待由 PAL worker thread 進行，actor 只收 `WorkerStoppedReceipt` 與 `WorkerDrainTimeout` 事件 |
| §5.3 mailbox 有界，RX flood 不得餓死控制事件 | 每個 actor 有兩條有界 lane，以 `select_biased!` 讓 control 優先（§5.2） |
| 決定性測試 | 狀態機寫成 sans-IO 的 `SessionCore::on_event(ev, now) -> Effects`（§5.1），L1 不需要 thread 或 async runtime |
| CLI 啟動成本 p50 ≤2ms（Linux） | CLI 是單執行緒同步程式，沒有 runtime 初始化 |

---

## 2. 平台條件編譯邊界（規格 §3.1）

### 2.1 規則

在 `sw-pal` 以外的 crate（sw-proto、sw-core、sw-daemon、sw-cli）禁止下列寫法：

- 任何 `#[cfg(...)]`、`#![cfg(...)]`、`#[cfg_attr(...)]`、`cfg!(...)`，其謂詞含 `unix`、`windows`、`target_os`、`target_family`、`target_env`、`target_vendor`、`debug_assertions`。
- `use std::os::unix`、`use std::os::windows`、`libc::`、`windows_sys::`。
- `build.rs`、`[build-dependencies]`。
- Cargo.toml 內的 `[target.'cfg(` 區段。

允許的寫法：`#[cfg(test)]`，以及 `#[cfg(feature = "test-fakes")]`（白名單只有這一個 feature）。

平台選擇只在組裝點（`sw-daemon::daemon_main`、`sw-cli::main`）呼叫 `sw_pal::native()` 取得 `Platform` 物件。業務碼只看 capability 值，例如 `ConsoleKind::Pty | ConsoleKind::TcpTelnet`、`RpcBackendKind`、`HolderScanCapability`，不得出現 OS 名稱比較。

### 2.2 強制方式

- `scripts/rust/check_cfg_boundary.py`（stdlib-only）：以上列 regex 掃描 `crates/*/src/**/*.rs`、`crates/*/Cargo.toml` 與 `crates/*/build.rs`（排除 `crates/sw-pal`）。命中即列出 `file:line`，exit 1。
- 掛載點：
  - `tests/test_rust_cfg_boundary.py`：讓 repo 必跑的 `python3 -m pytest -q tests/` 一併強制；未建立 `crates/` 時 SKIP。
  - CI 的 Rust job 第一步。
  - `scripts/rust/win-test.sh` 執行前。
- 第二道防線：CI 在兩個 target 下分別執行 `cargo metadata --filter-platform`，確認 sw-core、sw-daemon、sw-cli 的依賴樹不含 `libc` 或 `windows-sys` 的直接依賴。

---

## 3. `sw-proto`：常數、命名空間、型別與 Python 相容語意

### 3.1 `constants.rs`（數值全部取自盤點；改值等同改契約）

| 名稱 | 值 | 來源 |
|---|---|---|
| `DEFAULT_TCP_PORT` | `48700`（env `SERIALWRAP_TCP_PORT` 以 Python `int()` 規則解析；空字串或非整數：CLI 與 daemon 皆 exit 1，stderr 一行 `serialwrap: invalid SERIALWRAP_TCP_PORT: ...`，見 `GAP-X1`） | constants.py:47 |
| `LOOPBACK_TCP_HOSTS` | `["127.0.0.1","localhost","::1"]` | :49 |
| `DEFAULT_WAL_ROTATE_BYTES` | `67108864`（條件 `size >= R`，寫入前檢查；WAL 與 mirror 各自判斷） | :65 |
| `EVENTS_LOG_ROTATE_BYTES`／`EVENTS_LOG_BACKUP_COUNT` | `10485760`／`3` | event_log.py |
| `RX_RATE_WINDOW_S`／`RX_FLOOD_BYTES_PER_10S` | `10.0`／`20000` | :91,93 |
| `TRANSPORT_STALL_MIN_RX_AGE_S` | `30.0` | :102 |
| `MAX_RECOVERY_LEASE_S`／`BOOTLOADER_RX_TAIL_BYTES`／`HUMAN_ACTIVE_WINDOW_S`／`HUMAN_PEER_GRACE_S` | `120.0`／`512`／`60.0`／`3.0` | :107-113 |
| `REPROBE_RX_IDLE_S`／`REPROBE_BACKOFF_S`／`REPROBE_MAX_INTERVAL_S`／`REPROBE_MAX_ATTEMPTS` | `3.0`／`2.0`／`15.0`／`10` | :115-121 |
| `BOOT_BANNER_PATTERNS` | `["U-Boot","Hit any key to stop autoboot","CFE version "]`（大小寫敏感 substring） | :125 |
| `BOOT_QUIET_WINDOW_S`／`BOOT_BANNER_TAIL_CHARS` | `180.0`／`256` | :134,149 |
| `READY_RECONFIRM_RETRY_S`／`_MAX_S`／`_MAX_ATTEMPTS` | `5.0`／`300.0`／`5` | :151-163 |
| `UBOOT_FALLBACK_PROMPTS` | `["^=> $","^U-Boot> $","^CFE> $"]` | :182 |
| `BG_CAPTURE_MAX_BYTES`／`BG_CAPTURE_MAX_COUNT` | `4194304`（以字元數計）／`64` | :191,193 |
| `CMD_HISTORY_MAX`／`CMD_PENDING_MAX` | `512`／`256` | :195,197 |
| `DEFERRED_INPUT_MAX_BYTES` | `262144`（BC-02：超過整段清空） | :202 |
| `CMD_WARN_BYTES`／`CMD_REJECT_BYTES` | `4096`／`16384` | arbiter.py:14-15 |
| `COMPLETION_REGEX_MAX_BYTES` | `1024` | command_completion.py:7 |
| `ATTACHED_CONSOLE_LEASE_TIMEOUT_S` | `86400.0` | session_manager.py:65 |
| `MIN_FILE_CHUNK_TIMEOUT_S`／`MIN_FILE_PULL_TIMEOUT_S`／`MIN_FILE_ECHO_TIMEOUT_S` | `5.0`／`30.0`／`5.0` | :69,70,74 |
| `STALE_CONSOLE_GRACE_S`／`RX_WINDOW_MAX_ENTRIES`／`RX_MAX_CHARS` | `2.0`／`4096`／`131072`（字元） | uart_io.py:26,30,152 |
| `SERIAL_READ_CHUNK`／`IO_POLL_S`／`WORKER_JOIN_S`／`CONSOLE_TCP_BACKLOG`／`EAGAIN_BACKOFF_S` | `8192`／`0.2`／`2.0`／`8`／`0.01` | uart_io.py |
| `PYSERIAL_READ_TIMEOUT_S`／`PYSERIAL_WRITE_TIMEOUT_S` | `0.2`／`5.0`（Windows backend 的 `COMMTIMEOUTS` 依此設定） | serial_port.py:227-228 |
| `POSIX_BAUD_MAP` | `{9600,19200,38400,57600,115200,230400,460800,921600}`；未列 baud：configure 用 115200，`set_baud` no-op | serial_port.py:36 |
| flash | `grace=0.3`、首讀 `4096`、pump chunk `4096`／select `0.2`、probe RX buffer `4096`、probe poll `0.02`、holder watch `poll=0.5,max=1800.0`、McuPattern 預設 `baud=115200,timeout_ms=500`、預設 pattern `ti-cc26xx`／`5555`／`00cc`／115200 | 06 §1 |
| Telnet | `IAC=0xFF SE=0xF0 SB=0xFA WILL=0xFB WONT=0xFC DO=0xFD DONT=0xFE`；`TELNET_GREETING = FF FB 01 FF FB 03 FF FD 03 FF FB 00` | telnet_console.py |
| client | `RETRYABLE_READONLY_METHODS`（11 個，§7.5）、`RETRY_BACKOFF_BASE_S=0.5`、`RETRY_BACKOFF_MAX_S=5.0`、transient 錯誤碼 `{TIMEOUT,SOCKET_ERROR,EMPTY_RESPONSE}`、`PROBE_TIMEOUT_S=1.0`、`PROBE_DEADLINE_S=2.5`、`PROBE_METHODS=[health.ping,health.status]`、recv chunk `65536` | client.py |
| CLI | `DEFAULT_RPC_TIMEOUT_S=5.0`、`LONG_RPC_TIMEOUT_FLOOR_S=45.0`、`LONG_RPC_METHODS={session.recover,session.attach,session.self_test,session.console_attach}`；daemon start 輪詢 POSIX 15 次、win backend 50 次，每次 0.2s；ping 0.5／status 1.0；daemon stop 2.0；setup 探測 0.5；doctor 0.5；remote ping 2.0；`LEGACY_DAEMON_ENV_FILE="~/OPI.env"`、`PROFILE_DAEMON_ENV_FILE="OPI.env"` | cli.py |
| lock | 存活探測 `0.2`s；lock 檔 `0o600` | lock_*.py |
| RPC | 請求行上限 `65536`（邊界值由 Stage 0 測試擷取，`RPC-H-02`） | rpc_posix.py |
| file transfer | `DEFAULT_CHUNK_SIZE=512`、`DEFAULT_ECHO_SLICE_SIZE=64`、`DEFAULT_ECHO_TIMEOUT_S=5.0`、`ACK_MODES=[auto,echo,none]`；sentinel 字串逐字照 09 §4.1 | file_transfer.py |
| event | `REGEX_MAX_LEN=512`、`REGEX_MATCH_INPUT_MAX=4096`（字元）、`LINE_BUFFER_MAX=16384`（bytes）、matcher queue `1024`、dispatcher `8`、`STDIO_TAIL_BYTES=4096`、`INHERITED_ENV_KEYS=[PATH,HOME,USER,LANG,LC_ALL]` | event_engine |
| remote | `POLL_INTERVAL_S=0.2`、`ready_timeout=10.0`、runner timeout `10.0`（逾時 rc=124）、terminate `5.0`／step `0.1`、stderr tail `500` | remote_tunnel.py |
| 裝置 | watcher poll `1.0`；Windows BT TTL `5.0`；`max_sessions` 預設 `16` | device_*.py、config.py |
| self-test／recover | probe timeout 預設 `2.0`；force recover `10×1.0s`；CTRL_C／CTRL_D 各等 `min(timeout_s,2.0)` | session_manager.py |
| human console | `timeout_s=30.0`、`priority=100`、`source="human:<client_id>"`、`HUMAN_INTERACTIVE_COMMANDS`（18 個，02 §9） | service.py |
| Drain（新） | `DRAIN_TIMEOUT_MS=3000`（§5.2.5） | 規格 |

`runtime_paths.rs`：`RuntimePaths::resolve(env: &EnvSnapshot, native: NativeTraits) -> RuntimePaths`。逐字重現 constants.py 的 import-time 規則，包含：

- `_env_path`：strip 後為空視為未設；非空值不 strip；`expanduser`。
- `_xdg`：空字串視為未設，不 expanduser。
- `RUN_DIR` 三段 fallback。
- Windows 預設值仍走 `%USERPROFILE%\.config` 等 XDG fallback（`GAP-G-05a`）。

`NativeTraits { default_endpoint_is_tcp: bool, path_flavor: PathFlavor }` 由 PAL 提供，避免業務碼以 OS 名稱分支。新增 `SERIALWRAP_CONFIG` 與系統設定目錄 fallback（`SC-09`）。`bench_state_path` 與 `remote_run_dir` 在呼叫時重讀 env，語意與 Python 等價。

### 3.2 錯誤碼命名空間

- `error_codes.rs`：`pub enum ErrorCode`（每個碼一個 variant）、`as_str()`、`ErrorCode::META: &[ErrorMeta]`。`ErrorMeta` 欄位為 `code`、`interfaces: &[Interface]`、`retryable: Retryable`、`reachable: bool`、`status: Status`。
  - `Interface` 取值：`Rpc`、`RpcFrame`、`CliClient`、`CliLocal`、`Rec`、`Proc`。
  - `Retryable` 取值：`Yes`、`No`、`Conditional`、`FixArgs`。
  - `Status` 取值：`Existing`、`Approved`、`Implemented`、`Verified`。
  - 本檔由 `scripts/rust/gen_error_codes.py` 依 `contracts/error_codes.yaml` 產生並 check-in；不使用 build.rs（§2）。
- 一致性測試 `crates/sw-proto/tests/error_codes_sync.rs`：用 `yaml11` 讀 YAML，逐筆比對 enum 與 META。另有 Python 測試以 AST 加常數傳遞路徑掃描 `sw_core`（03 的方法），確認 Python 可輸出的每個碼都在 YAML。計數由資料產生（OD-06），沒有寫死的 78。
- `session_last_errors.rs`：`pub enum SessionLastError`，包含 31 個具名值（03 §5.2），加上動態前綴 `ReprobeFailed(String)`、`AttachFailed(String)`、`BridgeDown(String)`。`USER_ENV_MISSING` 標記 `reachable:false`。此 enum 與 `ErrorCode` 互不轉換。`session.attach`／`session.recover` 把 last_error 值當 `error_code` 輸出時（`RPC-D-02`，owner 待確認），以 `RpcErrorCode::LastErrorPassthrough(SessionLastError)` 表示，不併入 RPC 碼計數。
- 非錯誤碼也有各自的目錄，同樣由 YAML 生成：`WarningCode`（`CMD_LENGTH_WARNING`）、`ErrorDetail`（`NOT_BOOTLOADER`）、`Classification`（self-test）、`RecommendedAction`、`RecoveryAction`。

### 3.3 `pyjson.rs`：JSON 的值、解析與序列化

```rust
pub enum PyValue { Null, Bool(bool), Int(PyInt), Float(f64), Str(String), List(Vec<PyValue>), Dict(PyDict) }
pub enum PyInt { Small(i128), Big(String /* 正規化十進位 */) }
pub struct PyDict(indexmap::IndexMap<String, PyValue>);   // 插入序；重複 key：值取後者、位置保留首次
pub struct DumpOpts { pub sep: Sep, pub ensure_ascii: bool, pub sort_keys: bool, pub allow_nan: bool }
pub enum Sep { Compact /* "," ":" */, Default /* ", " ": " */, Indent(u8) /* ",\n" + ": " */ }
pub fn loads(s: &str) -> Result<PyValue, JsonDecodeError>;     // json.loads(strict=True)
pub fn dumps(v: &PyValue, o: &DumpOpts) -> String;
macro_rules! pydict { ($($k:expr => $v:expr),* $(,)?) => { ... } }  // 回應依 Python 插入序逐行對照
```

規則：

1. **解析**：接受 `NaN`、`Infinity`、`-Infinity`；字串內出現 raw 控制字元（<0x20）時報錯；不含 `.`／`e` 的數字一律是 int，超過 i128 的存為 `Big`；浮點以 correctly-rounded 解析，`1e400` → `inf`；`\uXXXX` 支援 surrogate pair，lone surrogate 轉成 U+FFFD（Python 會保留，屬已登記的非契約差異 `RPC-H-03`）；巢狀深度上限 `JSON_MAX_DEPTH`，值由 Stage 0 擷取，超過時視同 Python `RecursionError`（連線關閉、不回應）。
2. **字串輸出**：`"`→`\"`、`\\`、`\n` `\r` `\t` `\b` `\f` 用短跳脫；其他 <0x20 → `\u00xx`（小寫 hex）；`/`、DEL、U+2028／2029 不跳脫。`ensure_ascii=true` 時非 ASCII → `\uxxxx`（小寫，BMP 外拆 surrogate pair）。
3. **float 輸出**等於 Python `repr(float)`：
   - 非有限值輸出 `NaN`／`Infinity`／`-Infinity`。
   - 其餘取 `format!("{:e}", x)` 得到最短 round-trip 的 digits 與十進位指數 e。`-4 <= e < 16` 用定點表示，整數值補 `.0`；否則用 `d[.ddd]e±XX`，指數至少兩位、一律帶正負號。
   - 範例：`1e16→1e+16`、`1e-05`、`0.0001`、`-0.0`、`5.0`、`31.5`。
   - golden 測試：pytest 產生 10k 筆隨機 float 的 `repr`，兩邊比對。
4. **int 輸出**：十進位，不含 `+`。
5. **sort_keys** 依 code point 排序，等於 Rust `String` 的 Ord。
6. **四種序列化 profile**（逐一對應盤點）：

| profile | 參數 | 使用處 |
|---|---|---|
| `CLI_STDOUT` | Compact、ensure_ascii=false、sort_keys=true，加 `\n` | CLI `_print` |
| `RPC_WIRE` | Compact、ensure_ascii=false、sort_keys=false、allow_nan，加 `\n` | daemon 回應、client 請求（key 順序 `id`,`method`,`params`） |
| `RPC_EARLY` | Default、無 `id`、無 `version` | `INVALID_JSON`／`INVALID_REQUEST`（逐字 `{"ok": false, "error_code": "INVALID_JSON"}\n`） |
| `DISK_STABLE` | Compact、sort_keys=true、ensure_ascii=false | WAL 行、`state.json`、`benches.state.json`（無尾端換行）、remote registry |

   另外三種：events.ndjson 與 handler stdin 用 Default、不排序、ensure_ascii=false；規則檔用 Indent(2)、sort_keys=true、無尾端換行；counter 檔用 Default、ensure_ascii=true。

### 3.4 `pycompat.rs`／`pyfmt.rs`：Python 內建語意

以下函式各自附 Python 對照 golden 測試；測試時 pytest 以 subprocess 呼叫 python3 產生期望值，存入 `contracts/golden/pycompat.ndjson`。

- `py_int(&PyValue) -> Result<PyInt, PyErr>`：`int()` 的規則。字串會 strip，可帶正負號，允許位數之間的 `_`，接受 Unicode 十進位數字，拒絕 `"3.5"`；`bool` 轉 0／1；float 截斷。
- `py_float(&PyValue)`：`float()` 的規則。接受 `nan`、`inf`、`infinity`（不分大小寫）、空白、`_`。
- `py_truthy(&PyValue)`：`bool()` 的規則。
- `py_str(&PyValue)`：`str()` 的規則。`True`→`"True"`、float 用 repr、dict／list 用 Python repr（排版另有 golden）。
- `or_default(&PyValue, default)`：`x or D`。
- `coerce_rpc_bool`：對應 service.py:72-86。
- `py_round(x, n)`：`format!("{:.n$}")` 後再 parse 回 float。CPython 的 round 走 dtoa 正確捨入，兩者結果相同。
- `iso_utc_now(us) -> String`：`datetime.now(utc).isoformat()`，microsecond 為 0 時省略小數部分。
- `strftime_utc("%Y%m%d-%H%M%S")`、`strftime_local("%y%m%d-%H%M%S", offset)`。
- `uuid4_hex(rng)`、`uuid4_dashed(rng)`。
- `a2b_base64_lenient(&[u8]) -> Result<Vec<u8>, B64Error>`：`binascii.a2b_base64` 非 strict 模式，忽略非字母表字元，padding 錯誤回錯。`b2a_base64`。
- `shlex_quote(&str)`：逐字對應 09 §4.3。
- `to_printable(&[u8])`：對應 util.py:24-33。
- `strip_ansi(&str)`：event／transfer 用的 regex。`clean_text(&str)`：capture／result 用的 regex。兩者是不同函式（09 §3.3）。
- `decode_utf8_replace(&[u8])`：位置與 Python `errors="replace"` 一致，每個非法 maximal subpart 換成一個 U+FFFD。
- `splitlines(&str)`：Python `str.splitlines` 的分隔集合，含 `\x1c-\x1e`、`\x85`、U+2028／2029。
- `py_len_chars(&str)`：以 code point 計數。凡 Python 以 `len(str)` 計量的上限都必須用它，例如 `RX_MAX_CHARS`、`BG_CAPTURE_MAX_BYTES`、echo slice。

### 3.5 `rpc_types.rs`

- `pub enum Method { HealthPing, HealthStatus, DaemonStop, DaemonHandoffStop, …（共 55 個：02 §7 的 54 個＋SC-11 `daemon.handoff_stop`，R1-F1）, Unknown(String) }`，附 `as_str()`／`parse()`。另有 `Method::is_retryable_readonly()`（11 個）與 `Method::uses_long_timeout()`（4 個）。
- 參數存取器負責重現 02 §5.2 的強制轉型表：

  ```rust
  pub struct Params<'a>(&'a PyDict);
  impl Params<'_> {
      fn str_or(&self, k:&str, d:&str) -> Result<String, PyErr>;          // str(params.get(k) or d)
      fn int_or(&self, k:&str, d:i128) -> Result<i128, PyErr>;            // int(params.get(k) or d)
      fn float_or(&self, k:&str, d:f64) -> Result<f64, PyErr>;
      fn truthy(&self, k:&str) -> bool;                                    // bool(params.get(k))
      fn raw(&self, k:&str) -> Option<&PyValue>;
      fn selector_chain(&self, chain: SelectorChain) -> String;            // 依 02 §5.3 的 5 種別名鏈
  }
  ```

  `PyErr` 一律映射為 framing 層的 `EXCEPTION`。
- 回應以 `pydict!` 建構，位置照 Python 插入序，`version` 由 service 包裝層以 `setdefault` 補在尾端。`SessionPublic` 等子結構以 `fn to_public(&self) -> PyDict` 逐欄依 02 §6.1 的順序輸出。條件 key `console_endpoint` 由 capability 決定（`ConsoleKind::TcpTelnet` 或 bridge 有 endpoint 時才輸出）。新增欄位 `drain_stalled`、`drain_timeout_epoch`（`SC-04`）插在 `capture` 之後、`console_endpoint` 之前。

### 3.6 `pyre/`：Python `re` 相容層

- `parse.rs`：移植 CPython 3.12 `re/_parser.py`。產出 `sre` AST 並重現 `re.error` 的接受集合，包括 inline flags 必須位於開頭、`(?P<n>…)`／`(?P=n)`、lookbehind 必須固定寬度、條件群組、possessive 與 atomic（3.11+）。
- `redos.rs`：移植 event schema 的 `_ambiguity_score`／`_has_backref`，並採**無 google-re2 的接受集合**作基準（`EVT-D-S15`）。
- `translate.rs`：把 AST 轉成 fancy-regex 語法並保持語意：
  - 非 MULTILINE 的 `$` → `(?=\n?\z)`。
  - `\Z` → `\z`。
  - `\s` 擴成 Python 集合 `[\t\n\x0b\x0c\r\x1c-\x1f \x85\p{White_Space}]`。
  - `(?x)` 展開處理；字元類交集等語法另行轉寫。
  - 無法轉譯的構造回傳 `Untranslatable`，呼叫端視同不合法，例如 `INVALID_COMPLETION_REGEX`。殘餘差異清單由契約測試列舉並登記。
- API：

  ```rust
  pub fn compile(pat:&str, flags:PyFlags) -> Result<PyRegex, PyReError>;
  impl PyRegex { fn search(&self, hay:&str) -> Option<PyMatch>; fn search_from(&self, hay:&str, pos:usize) -> Option<PyMatch>; }
  ```

  執行時 `backtrack_limit=1_000_000`。超過上限時當作「未匹配」並遞增 `regex_backtrack_exhausted` 診斷計數，只寫 log。
- 用途：`valid_completion_regex`（CLI 本地預檢與 daemon）、profile regex（prompt／login／password／bootloader）、event rule regex、`profile test`。

### 3.7 `yaml11/`：PyYAML `safe_load`／`safe_dump` 相容層

- `resolve.rs`：以 yaml-rust2 事件流（含 `TScalarStyle` 與 tag）配合 PyYAML `SafeLoader` 的 implicit resolver 逐條移植：
  - bool：`yes/Yes/YES/no/…/true/false/on/off`。
  - int：含 `0x`、`0o`、舊式八進位 `0[0-7]+`、`0b`、六十進位、`_`。
  - float：含 `.inf`、`.nan`、六十進位。
  - null：`~`、`null`、空值。
  - timestamp：輸出 `PyValue::Str` 並附 `Timestamp` 標記，只有不吃 timestamp 的欄位會讀到。
  - quoted scalar 一律是 str。`!!str` 等 explicit tag 皆支援。
  - 產出 `PyValue`。
- `emit.rs`：`safe_dump(PyValue::Dict, allow_unicode=true, sort_keys=true)` 的子集輸出，僅限 block mapping／sequence 與 scalar。scalar 樣式選擇移植 PyYAML `Emitter.analyze_scalar` 與 `choose_scalar_style`（plain／single-quoted／double-quoted）。用於 `config.yaml`，golden 對照 PyYAML。
- `config_types.rs`：`load_profiles(dir, fs) -> Result<LoadResult, ConfigError>`，逐字重現 09 §2 的規則，包括 helper 轉型、templates 只取最後一個檔（`EVT-D-C2` 保留）、raise 與靜默的分流。另含 `RuntimeConfig`（`supervision_mode`、`socket_path`、`windows.exclude_coms`，新增 `windows.test_device_allowlist` 見 `SC-19`）與 `UartProfile`。

### 3.8 `wal_types.rs`

`WalRecord { seq:u64, mono_ts_ns:u64, wall_ts:String, com, dir:String, source, cmd_id:Option<String>, len:u64, crc32_hex:[u8;8], payload_b64, loss_flag:bool, meta:PyDict, rotation_failed:Option<bool> }`。

- `to_line()` 用 `DISK_STABLE` 序列化，key 依字母序。
- `crc32` 是 8 位小寫 hex 字串（`WAL-D1`）。
- 新增 META 事件 `WAL_RECREATED_AFTER_EXTERNAL_UNLINK` 的 schema，隨 BC-01 一起凍結：

  ```json
  {"event":"WAL_RECREATED_AFTER_EXTERNAL_UNLINK",
   "reason":"external_unlink"|"replaced"|"dir_missing",
   "previous_identity":str|null,
   "potentially_lost_seq_range":[first,last]|null,
   "range_known":bool}
  ```

  外層紀錄：`dir:"META"`、`source:"system"`、`loss_flag:true`。

---

## 4. `sw-pal`：平台抽象層（完整 trait）

`traits.rs` 定義全部 trait。`sw_pal::native()` 回傳 `Platform`，內含各 trait 的 `Arc<dyn …>`。`fakes/`（feature `test-fakes`）提供記憶體替身給 L1 使用；替身不得冒充原生 gate（§11.2）。

```rust
// ---- 共通 ----
pub type PalResult<T> = Result<T, PalError>;
#[derive(Debug, Clone)]
pub struct PalError { pub kind: PalErrorKind, pub os_code: Option<i32>, pub py_message: String }
// py_message：Python str(OSError) 格式；POSIX "[Errno N] <strerror>"，Windows "[WinError N] <FormatMessageW 系統語系>"
pub enum PalErrorKind { NotFound, PermissionDenied, AccessDenied, Busy, WouldBlock, TimedOut, Cancelled,
                        Interrupted, InvalidInput, Unsupported, BrokenPipe, ConnectionRefused, AddrInUse, Eof, Io }

pub struct Platform {
    pub env: Arc<dyn SysEnv>, pub clock: Arc<dyn Clock>, pub fs: Arc<dyn FsOps>,
    pub serial: Arc<dyn SerialBackend>, pub uart_excl: Arc<dyn UartExclusive>,
    pub devices: Box<dyn Fn(DeviceSelection) -> Box<dyn DeviceSource> + Send + Sync>,
    pub instance_lock: Arc<dyn InstanceLockFactory>, pub wal_fs: Arc<dyn WalFs>,
    pub rpc_connect: Arc<dyn RpcConnector>, pub rpc_listen: Arc<dyn RpcListenerFactory>,
    pub console: Arc<dyn ConsoleTransportFactory>, pub service: Arc<dyn ServiceCtl>,
    pub tunnel: Arc<dyn ProcessTunnel>, pub procs: Arc<dyn ProcScan>, pub process: Arc<dyn ProcessCtl>,
    pub signals: Arc<dyn SignalWait>, pub caps: Capabilities,
}
pub struct Capabilities { pub console_kind: ConsoleKind, pub native_rpc: RpcBackendKind, pub native_lock: LockBackendKind,
    pub native_device: DeviceBackendKind, pub holder_scan: HolderScanCapability, pub flash_endpoint: bool /* /dev/ttyMCU */,
    pub com0com_console: bool, pub path_flavor: PathFlavor, pub exec_replace: bool }
```

### 4.1 `SysEnv`、`Clock`、`FsOps`

```rust
pub trait SysEnv: Send + Sync {
    fn var(&self, k: &str) -> Option<String>;               // 非 UTF-8 以 surrogateescape 等價方式保留（lossy 時記錄）
    fn vars(&self) -> Vec<(String, String)>;
    fn home_dir(&self) -> PathBuf;
    fn expanduser(&self, p: &str) -> String;                // os.path.expanduser 的平台語意
    fn local_appdata(&self) -> Option<PathBuf>;             // Windows %LOCALAPPDATA%；POSIX None
    fn program_data(&self) -> Option<PathBuf>;              // Windows %ProgramData%；POSIX None
    fn system_config_dir(&self) -> PathBuf;                 // /etc/serialwrap ｜ %ProgramData%\serialwrap（SC-09）
    fn pid(&self) -> u32;
    fn is_wsl(&self) -> bool; fn has_systemd(&self) -> bool; fn user_in_group(&self, g: &str) -> bool;
    fn which(&self, name: &str) -> Option<PathBuf>; fn which_all(&self, name: &str) -> Vec<PathBuf>; // sysenv.py 語意（PATHEXT）
    fn terminal_columns(&self) -> Option<u16>;              // argparse：COLUMNS → tty size → None(=80)
    fn random_bytes(&self, buf: &mut [u8]);                 // getrandom(2) ｜ BCryptGenRandom
    fn os_error_message(&self, code: i32) -> String;
    fn current_exe(&self) -> PalResult<PathBuf>;
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput>; // SystemEffects.run；逾時 (-1,"","TIMEOUT")
}
pub struct CmdOutput { pub rc: i32, pub stdout: String, pub stderr: String }

pub trait Clock: Send + Sync {
    fn mono_ns(&self) -> u64;                  // CLOCK_MONOTONIC ｜ QueryPerformanceCounter（WAL mono_ts_ns 用）
    fn wall_unix_us(&self) -> i64;             // UTC 微秒
    fn local_utc_offset_s(&self, unix_s: i64) -> i32; // localtime_r ｜ SystemTimeToTzSpecificLocalTime
    fn sleep(&self, d: Duration);              // FakeClock：推進虛擬時間並喚醒等待者
}
// 決定性測試：SessionCore 只吃 `now: Mono` 參數；runtime loop 才呼叫 Clock。FakeClock 提供 advance(d)。

pub trait FsOps: Send + Sync {
    fn atomic_write(&self, path: &Path, bytes: &[u8], opts: AtomicWriteOpts) -> PalResult<()>;
    // mkstemp(dir,prefix) 權限 0600 → write → flush → fsync → replace（Windows PermissionError 重試 6 次 0.01×2^n）
    // → 目錄 fsync（POSIX；Windows 跳過）→ 清殘留 tmp。對應 state.json／benches.state.json
    fn replace(&self, from: &Path, to: &Path) -> PalResult<()>;
    fn sync_dir(&self, dir: &Path) -> PalResult<()>;       // POSIX fsync(dirfd)；Windows Ok(())，並在 Spike 3 另行實證
    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()>; // shutil.copy2
    fn set_mode(&self, p: &Path, mode: u32) -> PalResult<()>; fn chgrp(&self, p: &Path, group: &str) -> PalResult<()>;
    fn file_lock_exclusive(&self, p: &Path, blocking: bool) -> PalResult<FileLockGuard>; // remote registry／bench state 跨行程鎖
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind>;   // DF-16
}
```

### 4.2 `SerialBackend`、`SerialHandle`、`UartExclusive`

```rust
pub struct SerialConfig { pub baud: u32, pub data_bits: u8, pub parity: Parity, pub stop_bits: u8,
                          pub flow: FlowControl, pub xonxoff: bool /* POSIX backend 忽略（UIO-12.1-e） */ }
pub trait SerialBackend: Send + Sync {
    /// 以獨占方式開啟並完成設定。
    /// POSIX：open(O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC) → flock(LOCK_EX|LOCK_NB) → ioctl(TIOCEXCL)
    ///        → **鎖後 holder 重掃**（規格 §6.2「拒絕已識別的外部衝突」；P1 T10「非合作式外部 holder」凍結規則）：
    ///          TIOCEXCL 不撤銷既有 handle，外部程式（如 minicom）先以普通 open 開埠、不取 flock 時前三步都會成功，
    ///          因此 `probe_holders(path)`（排除自身 pid）`pids` 非空 → ioctl(TIOCNXCL) → close(fd)、回 `Held{pids, holder_unknown:false}`；
    ///          `pids` 為空且 status∈{Unknown, Permission} → 照常開啟並 WARNING（不得當安全證明，`foreign_holders` 標 holder_unknown）
    ///        → termios（06 §10.2 的位元設定）。重掃在 termios 與任何 RX／TX 之前，故被拒時 UART 零 TX、外部工具的 termios 不被改寫。
    ///        此檢查在 backend 內，所有呼叫者（初次／熱插自動 attach、PROBE、`device.attach`，含 `force:true`）都無法略過。
    ///        **TIOCEXCL 成功後的任何失敗返回**（鎖後重掃拒絕、termios、eventfd 建立失敗）一律先 ioctl(TIOCNXCL) 再 close(fd)
    ///        （`ExclGuard`）：TIOCEXCL 是 tty 物件旗標，不隨 fd 關閉清除，只在 tty 物件釋放（最後一個 handle 關閉且對端不存在）時重置；
    ///        PTY master 仍開啟（測試 harness、bridge 受測端）或外部 holder 仍持有時，殘留旗標會讓之後的一般 open 一律 EBUSY（R0-F3 實測）。
    /// Windows：CreateFileW(\\.\COMn, GENERIC_READ|GENERIC_WRITE, dwShareMode=0, OPEN_EXISTING, FILE_FLAG_OVERLAPPED)
    ///          → SetCommState／SetCommTimeouts（ReadIntervalTimeout=MAXDWORD 等，write constant 5000ms）
    fn open_exclusive(&self, dev: &DevicePath, cfg: &SerialConfig) -> Result<Box<dyn SerialHandle>, OpenError>;
}
pub enum OpenError {
    Held(HolderReport),                                   // POSIX EWOULDBLOCK(flock)、open EBUSY(TIOCEXCL)，或鎖後重掃識別到外部 holder
    AccessDenied { os: PalError, holder: HolderReport },  // Windows ERROR_ACCESS_DENIED：不推斷 holder，holder_unknown=true
    NotFound(PalError),
    Other(PalError),
}
pub struct HolderReport { pub pids: Vec<u32>, pub holder_unknown: bool, pub status: HolderScanStatus }
pub enum HolderScanStatus { Ok, Permission, Unknown, Unsupported }

pub trait SerialHandle: Send {
    fn split(self: Box<Self>) -> (Box<dyn SerialReader>, Box<dyn SerialWriter>, Arc<dyn IoWaker>, CloseToken);
    fn set_baud(&self, baud: u32) -> PalResult<()>;
    fn mirror_termios_from(&self, pty: &PtySlaveRef, fallback_baud: Option<u32>) -> PalResult<()>; // Windows：Unsupported → 呼叫端只 set_baud
    fn is_alive(&self) -> bool;
}
pub enum ReadOutcome { Data(usize), Eof, Woken, TimedOut }
pub trait SerialReader: Send {
    /// POSIX：poll([fd, wake_fd], timeout) 後 read；Windows：overlapped ReadFile + WaitForMultipleObjects([io_event, wake_event])。
    /// 被喚醒時 Windows 先 CancelIoEx(h,&ov) 再 GetOverlappedResult(bWait=TRUE)，已完成的 bytes 照實回報，不丟棄。
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome>;
}
pub enum TxOutcome { NotSubmitted, Completed(usize), Unknown }
pub struct TxResult { pub outcome: TxOutcome, pub error: Option<PalError> } // Completed(n<len)+error 表示部分完成後失敗
/// OS 提交的序列化點（規格 §5.2.2，R0-F1）。PAL 每次要把 bytes 交給 OS 前都必須經過 gate：
/// POSIX 一次非阻塞 `write(2)`（fd 為 O_NONBLOCK，立即返回 n 或 EAGAIN）；Windows 一次 overlapped `WriteFile` 發出（立即返回
/// 完成或 ERROR_IO_PENDING）。`submit` 的實作（sw-core `FenceSubmitGate`）在 Fence 鎖內重驗 permit，通過才呼叫 `os_submit`，
/// 並在同一臨界區登記 `in_flight` 後放鎖；被拒時**不呼叫** `os_submit`。`os_submit` 只做提交、不得等待完成；
/// POLLOUT／GetOverlappedResult 等完成等待一律在 gate 之外進行，因此鎖只跨「非阻塞提交」，不跨完成等待。
pub trait SubmitGate { fn submit(&self, os_submit: &mut dyn FnMut() -> SubmitStep) -> Result<SubmitStep, SubmitRevoked>; }
pub enum SubmitStep { Accepted(usize) /* POSIX write(2)=n>0 或 Windows 同步完成 */, Pending /* Windows ERROR_IO_PENDING：已交 OS */,
                      WouldBlock /* EAGAIN：未交 OS */, Failed(PalError) /* 0 byte 交 OS */ }
pub struct SubmitRevoked;                       // 原因由 gate 實作自行保存（PAL 不依賴 sw-core 的 FenceReject）
pub struct AlwaysOpen;                          // impl SubmitGate：直接呼叫 os_submit；只供 PAL conformance 與 PAL 單元測試
pub trait SerialWriter: Send {
    /// 寫一段 payload，直到完成、取消、失敗或被 gate 撤銷。POSIX：每次 write(2) 都經 `gate.submit`，EAGAIN 時於 gate 外
    /// poll(POLLOUT, wake) 後再經 gate 提交下一段（每段重驗，不再無限 sleep 重試）；Windows：overlapped WriteFile 經 `gate.submit`
    /// 發出，於 gate 外等 [ov.hEvent, wake]；cancel 時 CancelIoEx 後 GetOverlappedResult(bWait=TRUE)，n=已傳 bytes。
    /// 回傳值必須能區分「從未提交 OS」「OS 確認 n bytes」「結果不明」（BC-03、§5.1.4）。gate 撤銷：尚未提交任何 bytes →
    /// `NotSubmitted`；已確認 k≥1 bytes 後撤銷 → `Completed(k)`＋`PalError::cancelled()`，未提交的尾段不得再交 OS。
    fn write_gated(&mut self, data: &[u8], cancel: &CancelToken, gate: &dyn SubmitGate) -> TxResult;
    /// = `write_gated(data, cancel, &AlwaysOpen)`。sw-core 禁止呼叫（`tx_writer.rs` 以外亦不得持有 SerialWriter）。
    fn write(&mut self, data: &[u8], cancel: &CancelToken) -> TxResult { self.write_gated(data, cancel, &AlwaysOpen) }
}
pub trait IoWaker: Send + Sync { fn wake(&self); }   // eventfd/self-pipe 寫入 ｜ SetEvent
#[derive(Clone)] pub struct CancelToken { /* Arc<AtomicBool> + Arc<dyn IoWaker> */ }
pub struct CloseToken;
impl CloseToken { pub fn close(self, r: Box<dyn SerialReader>, w: Box<dyn SerialWriter>) -> CloseReceipt; }
// close 只能在 reader 與 writer 都交回後呼叫；型別保證 handle 不會被雙重關閉，也不會在 I/O 未收斂時關閉
// POSIX 關閉序（I/O 收斂後、關閉有效 tty fd 前解除獨占；R0-F3）：ioctl(TIOCNXCL) → flock(LOCK_UN) → close(fd) → close(wake_r／wake_w)。
//   TIOCNXCL 失敗：errno ∈ {EIO, ENXIO, ENODEV}（tty 已 hangup／裝置已拔除，tty 物件隨最後一個 handle 釋放、旗標一併消失）→ excl_cleared=true、不記錯；
//   其他 errno → 仍照序 close（不得因此保留 fd），excl_cleared=false、error=Some(該 errno)，daemon stderr WARNING
//   `serialwrapd: TIOCNXCL failed on <dev>: <py_message>`；之後同一裝置的 open 可能得 `Held{pids:[], holder_unknown:true}`，不得被當成成功交接的證據。
// Windows／fake：CloseHandle 即解除共用限制，excl_cleared 恆等於 closed。
pub struct CloseReceipt { pub closed: bool, pub excl_cleared: bool, pub error: Option<PalError> }

pub trait UartExclusive: Send + Sync {
    /// 只供診斷，不是鎖。POSIX 掃 /proc/*/fd（排除自身 pid；readlink 比對 realpath／原路徑，或比對 st_rdev）；
    /// Windows 回 { pids:[], holder_unknown:true, status:Unsupported }。這只表示診斷能力不足（SC-17），不是獨占失敗；
    /// Windows 獨占保證來自 open_exclusive 的 dwShareMode=0，驗收以外部第二次開啟被拒／release 後可取得的證據判定（P2 Task 15 winexcl，R1-F7）。
    fn probe_holders(&self, real_path: &str) -> HolderReport;
}
```

### 4.3 `DeviceSource`、`DeviceWatcher`

```rust
pub struct DeviceInfo { pub by_id: String, pub real_path: String }
pub struct DeviceSelection { pub by_id_dir: PathBuf, pub by_path_dir: PathBuf, pub exclude_coms: Vec<String>,
                             pub test_allowlist: Option<Vec<String>> /* SC-19（OD-11） */ }
pub trait DeviceSource: Send {
    /// POSIX：依目錄順序，sorted(listdir)，exists 才收，realpath 去重（先掃到者勝）。
    /// Windows：HKLM SERIALCOMM＋BTHENUM PortName（TTL 5s）＋bthmodem 啟發式＋exclude_coms；
    /// test_allowlist 存在時只保留清單內的 by_id。
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>>;   // 保留列舉順序
}
pub struct DeviceWatcher;  // thread "serialwrap-device-watcher"
impl DeviceWatcher {
    pub fn start(src: Box<dyn DeviceSource>, poll: Duration, tx: crossbeam_channel::Sender<WatcherEvent>, clock: Arc<dyn Clock>) -> WatcherHandle;
}
pub enum WatcherEvent { Delta { added: Vec<DeviceInfo>, removed: Vec<DeviceInfo>, snapshot: Vec<(String, DeviceInfo)> }, Tick, ScanFailed(PalError) }
// 差異演算法照 device_watcher.py:52-68（含 changed 的 append 順序）。單輪失敗只記 log，不終止 thread。
// 呼叫端必須先做一次 initial_scan()，完成 rank 後才 start（DF-14）。
```

### 4.4 `InstanceLock`（Tier 1）

```rust
pub enum EndpointSpec { Unix(PathBuf), Tcp { host: String, port: u16 } }
pub trait InstanceLockFactory: Send + Sync {
    /// POSIX：makedirs → open(0600) → flock(EX|NB)（失敗 → AlreadyRunning）→ socket 檔存在時 0.2s 試連：
    ///   連得上 → AlreadyRunning；ENOENT/ECONNREFUSED → unlink stale；其他錯誤 → SocketUnavailable → 寫 "pid\n" + fsync
    /// Windows：先試連 endpoint（0.2s，連得上 → AlreadyRunning）→ open → LockFileEx(EXCLUSIVE|FAIL_IMMEDIATELY, offset 0, len 1)
    /// 路徑比較以正規化形式進行（SC-10）；lock 檔不刪除
    fn acquire(&self, lock_path: &Path, endpoint: &EndpointSpec) -> Result<Box<dyn InstanceLockGuard>, InstanceLockError>;
}
pub enum InstanceLockError { AlreadyRunning, SocketUnavailable, Io(PalError) }  // daemon：stderr "serialwrapd: <CODE>"，exit 2
pub trait InstanceLockGuard: Send { fn release(self: Box<Self>); }
```

### 4.5 `WalFs`

```rust
pub trait WalFs: Send + Sync {
    /// POSIX：open(O_WRONLY|O_APPEND|O_CREAT|O_CLOEXEC, 0o666 & ~umask)
    /// Windows：CreateFileW(FILE_APPEND_DATA|SYNCHRONIZE, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_ALWAYS)
    fn open_append(&self, path: &Path) -> PalResult<Box<dyn WalFile>>;
    fn path_identity(&self, path: &Path) -> PalResult<Option<FileIdentity>>;  // None = 不存在
    fn size(&self, path: &Path) -> PalResult<Option<u64>>;
    fn rename_noreplace_or_suffix(&self, from: &Path, base_to: &Path) -> PalResult<PathBuf>; // DF-03：同名時加 .1 .2 …
    fn sync_dir(&self, dir: &Path) -> PalResult<()>;
    fn ensure_dir(&self, dir: &Path) -> PalResult<()>;
    fn dir_writable(&self, dir: &Path) -> bool;                               // os.access(W_OK|X_OK) 等價
}
pub trait WalFile: Send {
    fn write_all_flush(&mut self, bytes: &[u8]) -> PalResult<()>;  // user space 不留緩衝；每筆一次 write（可能分多次 syscall）
    fn sync(&mut self) -> PalResult<()>;                           // fsync ｜ FlushFileBuffers
    fn identity(&self) -> PalResult<FileIdentity>;                 // (st_dev, st_ino) ｜ (VolumeSerialNumber, FileId128)
    fn unlinked(&self) -> PalResult<bool>;                         // POSIX st_nlink==0 ｜ Windows FileStandardInfo.DeletePending
    fn close(self: Box<Self>) -> PalResult<()>;
}
#[derive(PartialEq, Eq, Clone, Debug)] pub struct FileIdentity(pub u128, pub u128);
```

### 4.6 `RpcTransport`（client 與 server）

```rust
pub trait RpcConnector: Send + Sync {
    fn af_unix_available(&self) -> bool;
    /// connect 逾時 → Err(kind=TimedOut)；其他 → Err(py_message=Python str(OSError))
    fn connect(&self, ep: &EndpointSpec, timeout: Duration) -> PalResult<Box<dyn RpcStream>>;
}
pub trait RpcStream: std::io::Read + std::io::Write + Send {
    fn set_timeout(&mut self, t: Option<Duration>) -> PalResult<()>;
    fn last_errno(&self) -> Option<i32>;       // cli_trace 的 errno/errno_name
}
pub struct ListenOptions { pub mode: u32, pub group: Option<String>, pub ensure_dir_mode: Option<u32> }
// SC-08：預設 mode 0o600、dir 0o700（只在新建時套用）；group 有值時 mode 0o660 並 chgrp，失敗時忽略（與 Python 相同）
pub trait RpcListenerFactory: Send + Sync {
    /// Unix：既有 socket 檔一律 unlink 後再 bind（前置 lock 已探測過存活）
    /// TCP：只允許 LOOPBACK_TCP_HOSTS，否則回錯，訊息逐字照 rpc_win.py
    fn bind(&self, ep: &EndpointSpec, opts: &ListenOptions) -> PalResult<Box<dyn RpcListener>>;
}
pub trait RpcListener: Send {
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<Box<dyn RpcStream>>>;  // 有界等待，供 stop 檢查
    fn close(self: Box<Self>);                                                          // Unix：移除 socket 檔
}
```

### 4.7 `ConsoleTransport`（PTY／TCP listener／選配 com0com）

```rust
pub enum ConsoleKind { Pty, TcpTelnet }
pub trait ConsoleTransportFactory: Send + Sync {
    fn kind(&self) -> ConsoleKind;
    /// POSIX openpty：master 設 O_NONBLOCK；slave raw（iflag=oflag=lflag=0、CREAD|CLOCAL|CS8、VMIN=1、VTIME=0）。
    /// daemon 同時持有 master 與 slave（keepalive）。vtty 路徑為 /dev/pts/N，不建 symlink
    fn create_pty(&self) -> PalResult<PtyPair>;
    /// 127.0.0.1:0 ephemeral、SO_REUSEADDR、backlog 8、nonblocking；accept 後設 TCP_NODELAY
    fn bind_tcp_console(&self) -> PalResult<Box<dyn ConsoleListener>>;
    /// OD-01 選配；不支援時回 Unsupported
    fn open_com0com_console(&self, port: &str) -> PalResult<Box<dyn ConsoleIo>>;
    /// /proc 掃描外部持有的 pts 路徑；Windows 或不可讀時回 None
    fn held_slave_paths(&self) -> Option<HashSet<String>>;
}
pub struct PtyPair { pub master: Box<dyn ConsoleIo>, pub slave_keepalive: KeepAlive, pub slave_path: String, pub slave_ref: PtySlaveRef }
pub trait ConsoleIo: Send {
    fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome>;
    fn try_write(&mut self, data: &[u8]) -> PalResult<usize>;   // 不阻塞；EAGAIN → Ok(0)
    fn waker(&self) -> Arc<dyn IoWaker>;
    fn has_external_peer(&self, held: Option<&HashSet<String>>) -> bool;
    fn close(self: Box<Self>);
}
pub trait ConsoleListener: Send {
    fn endpoint(&self) -> String;   // "127.0.0.1:<port>"
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<(Box<dyn ConsoleIo>, String /* peer "ip:port" */)>>;
}
```

`/dev/ttyMCU`（`caps.flash_endpoint`）使用同一個 `create_pty`，另加 `fn link(&self, slave_path, link_path)`、`tty_setraw(slave)`、`tcgetattr_speed(slave)`，皆屬 POSIX-only capability。

### 4.8 `ServiceCtl`

```rust
pub enum SupervisionMode { OnDemand, SystemdUser, SystemdSystem, WindowsTask, WindowsService, Other(String) }
pub enum ServiceAction { Start, Stop, Restart, Status }
pub struct SupervisorDefinition { pub mode: SupervisionMode, pub exec: Vec<String>, pub run_user: Option<String>, pub env: Vec<(String,String)> }
pub trait ServiceCtl: Send + Sync {
    /// systemd-*：逐字照 service_ctl.py（NEEDS_SUDO／NO_SYSTEMD 的 hint 字串、rc/stdout/stderr）
    /// windows-task：schtasks /Run|/End|/Query /TN serialwrap；windows-service：sc.exe start|stop|query serialwrap
    /// 其他模式：NO_SYSTEMD 形狀
    fn action(&self, mode: &SupervisionMode, action: ServiceAction, with_sudo: bool) -> PyDictLike;
    /// systemd 由 sw-cli 呼叫 render_user_unit／render_system_unit 產生文字（逐字、無尾端換行）。
    /// windows-task 產生 Task XML 後執行 schtasks /Create /XML <file> /TN serialwrap /F，XML 設定：
    ///   <LogonTrigger><UserId>當前使用者</UserId></LogonTrigger>、<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>、
    ///   <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>、
    ///   <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>、<DisallowStartIfOnBatteries>false、<StopIfGoingOnBatteries>false、
    ///   Principal LogonType=InteractiveToken RunLevel=LeastPrivilege；Exec=穩定入口 bin\serialwrapd.exe
    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord>;
    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord>;
    fn wait_stopped(&self, mode: &SupervisionMode, timeout: Duration) -> bool;   // SC-12
}
pub struct CmdRecord { pub argv: Vec<String>, pub rc: i32 }   // DF-12：setup 的 "ran" 填實際執行過的命令
```

### 4.9 `ProcessTunnel`（BC-05）、`ProcessCtl`、`ProcScan`、`SignalWait`

```rust
pub struct TunnelProc { pub pid: u32, pub group: ProcGroupRef, pub start_ticks: Option<u64> }
// start_ticks：POSIX 取 /proc/<pid>/stat 第 22 欄；Windows 取 CreationTime FILETIME
pub enum ProcGroupRef { Pgid(i32), WinTree }   // Windows 終止時以 Toolhelp32 的 parent 鏈加 CreationTime 驗證整棵樹
pub trait ProcessTunnel: Send + Sync {
    /// stdin=NUL，stdout 與 stderr 都導到 log 檔（append）。POSIX setsid；Windows CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW。
    /// 全程不使用 pipe（BC-05：拿掉 POSIX-only 的轉接依賴）
    fn spawn_detached(&self, argv: &[String], log: &Path) -> PalResult<TunnelProc>;
    /// POSIX kill(pid,0) 加 start_ticks 比對；Windows OpenProcess＋GetExitCodeProcess＋CreationTime。
    /// Windows 上絕不可用 os.kill(pid,0)：在 Windows 那等於終止目標行程
    fn alive(&self, p: &TunnelProc) -> bool;
    fn terminate(&self, p: &TunnelProc, grace: Duration) -> PalResult<()>;   // SIGTERM → 每 0.1s 檢查 → SIGKILL ｜ TerminateProcess(tree)
    fn stderr_tail(&self, log: &Path, max: usize) -> String;
    /// 無 ControlMaster 平台（Win32-OpenSSH）在 readiness 改走另一條路徑（§7.7）
    fn supports_control_master(&self) -> bool;
}
pub trait ProcessCtl: Send + Sync {
    fn spawn_daemon(&self, argv: &[String], env: &[(String,String)], detached: bool) -> PalResult<ChildRef>;
    // POSIX：start_new_session、stdout/stderr→DEVNULL、stdin 繼承；Windows：DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP
    fn poll(&self, c: &ChildRef) -> Option<i32>;
    fn call_foreground(&self, argv: &[String], env: &[(String,String)]) -> PalResult<i32>;   // subprocess.call
    /// Stage 1A 委派：POSIX 用 execve（行程被取代，stdio、exit code、訊號天然一致）；
    /// Windows 用 CreateProcess 繼承 handle，加入 Job(KILL_ON_JOB_CLOSE)，SetConsoleCtrlHandler(NULL,TRUE) 後等待並回傳子行程 exit code
    fn exec_or_wait(&self, argv: &[String], env: &[(String,String)]) -> !;
}
pub struct DaemonProc { pub pid: u32, pub socket: Option<String> }
pub trait ProcScan: Send + Sync {
    /// POSIX：/proc/*/cmdline，判定規則逐字照 multi_open.py:27-62，--socket 解析照 :65-83，依 pid 排序。
    /// Windows（SC-17）：Toolhelp32 列出 image 名為 serialwrapd.exe 的行程，command line 取自
    /// NtQueryInformationProcess(ProcessCommandLineInformation)，--socket 解析規則相同
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure>;
    /// 只掃 serialwrapd 行程的 fd（含自身，RPC-D-10 保留）；Windows 回 status=Unsupported
    fn serialwrapd_tty_holders(&self, tty_paths: &[String]) -> (IndexMapLike<String, u32>, HolderScanStatus);
}
pub trait SignalWait: Send + Sync {
    /// daemon：SIGINT/SIGTERM（以 signalfd 或 sigwait thread）｜ SetConsoleCtrlHandler（CTRL_C/CTRL_BREAK/CLOSE），再加 Task Scheduler 的 /End
    fn wait_stop(&self, extra: crossbeam_channel::Receiver<()>);
}
```

---

## 5. `sw-core`：Actor 模型與安全骨架

### 5.1 結構

每個 session 由下列元件組成：

- **`SessionCore`**（`session_core.rs`）：sans-IO 狀態機，介面為 `fn on_event(&mut self, ev: Event, now: Mono) -> Effects`。它持有對外狀態、世代、lease、quiet gate、命令佇列（arbiter）、DeferredBuffer 帳本、drain 狀態。L1 測試直接餵事件序列並斷言 `Effects`，不需要 thread。
- **`SessionActor`**（`actor.rs`）：執行 `SessionCore` 的 thread `serialwrap-session-<COM>`。以 `select_biased!` 從 mailbox 收事件，執行 `Effects`（送 TxJob、啟停 worker、排 timer、回覆 RPC、寫 WAL META、要求 StateStore 持久化）。`Effects` 的執行不得阻塞；需要等待的工作一律交給下列 thread。
- **`IoWorker`**（`uart_bridge.rs`）：每個 `conn_epoch` 一組，包含：
  - **Reader thread**：read → `WalWriter::append(RX)` → 推送到 ConsoleHub → `UartRxChunk` 送進 data lane。
  - **TxWriter thread**（`tx_writer.rs`）：唯一呼叫 `SerialWriter::write_gated` 的地方（一律帶 `FenceSubmitGate`，§5.4）。
- **`OpRunner`**：執行前景長操作的 thread，包括 command execute、file transfer、self-test probe、recover 送按鍵與等待、login FSM、force recover 輪詢。一個 session 同時只有一個前景槽（`ForegroundSlot`），對應 Python 的 `_ForegroundOperation`。OpRunner 送 TX 只能向 actor 取得 `TxPermit`，再交給 TxWriter；等待 RX 使用共享的 `RxWindow`（`Arc<Mutex<RxWindow>> + Condvar`，語意照 06 §4.2 的絕對偏移與 `_rx_trimmed`）。
- **`ConsoleHub`**（`human_console.rs`、`telnet_console.rs`）：每 session 一個 thread，處理 PTY master／TCP socket 的入出向；每個 console 有自己的出向 ring（§5.10）。

全域元件：

- **`SessionManager`**（`session_manager.rs`）：全域 actor thread。負責 device delta、rank（#100）、session 建立與 rematerialize、selector 解析、alias、binding、pin，並把 RPC 路由到各 SessionActor。
- **`StateStore`**（`state_store.rs`）：單一寫入者 thread，原子寫入 `state.json` 並回報結果。
- **`WalWriter`**（`wal.rs`）：Mutex 保護的同步 append（§5.8）。
- **`EventEngine`**：matcher thread 一條（queue 1024）、dispatcher pool 8。

### 5.2 Mailbox（`mailbox.rs`）

| lane | 容量 | 內容 | 滿載策略 |
|---|---|---|---|
| `control` | 256 | RPC 請求、`DeviceAttached/Detached`、`DeviceRelease/Attach`、`SessionRecover*`、`CmdSubmit/Cancel`、`Interactive*`、`Console*`、`WorkerStoppedReceipt`、`WorkerDrainTimeout`、`UartIoError`、`UartTxCompleted`、timer、`Flash*`、`LogCaptureControl`、`FileTransfer`、`TransportStallDetected` | 送端最多等 `rpc_timeout`；逾時的 RPC 請求回 `TIMEOUT` 由 client 處理（daemon 不新增碼）。內部事件送端（worker、timer）一律阻塞等待，不得丟棄 |
| `data` | 1024 個 chunk（每個 ≤8192 B） | `UartRxChunk{epoch, wal_seq, bytes}`、`ConsoleInputBytes{client_lifetime, owner_gen_at_capture, bytes}` | Reader 阻塞等待（背壓傳到 OS buffer）。WAL 在進 lane **之前**就已落盤，背壓不會丟失 RX 證據 |

- 優先序：`select_biased!` 先取 control。但只要 control 內含有依 RX 順序判定的事件，就必須先把同一 `epoch` 中、序號在它之前的 data 處理完。實作方式：每個 RX chunk 帶 `rx_seq`；控制事件若依賴 RX，帶 `after_rx_seq`，actor 處理到該序號前把它暫存。這保證規格 §5.3「不得先處理後段 prompt」。
- 有界處理延遲的目標值（規格 §5.3 要求在 Slice 2.1 前凍結，量測方法由 P5 計畫定義）：control 事件從進 lane 到處理完成 p99 ≤ 20 ms；RX flood（20000 B/10s 的 10 倍）期間仍 ≤ 50 ms。

### 5.3 世代（`generations.rs`）

| 名稱 | 遞增的唯一線性化位置 | 隔離對象 |
|---|---|---|
| `conn_epoch: u64` | `SerialBackend::open_exclusive` 成功、建立 IoWorker 的那一刻（全域單調）。對外的 `bridge_generation` 另依 Python 規則維護（attach 成功才 +1） | 舊 worker 的 completion、receipt、RX；TxPermit |
| `fsm_gen: u64` | (a) boot banner arm quiet；(b) 啟動一次 login／probe／ready-reconfirm run；(c) quiet 被清除 | login step timer、quiet timer、`ReadyProbeVerified` |
| `owner_gen: u64` | owner 變更：lease 開或關、human raw 授予或撤銷、suspend depth 0→1 或 1→0、soft preempt、self-heal grant | DeferredBuffer 回放、console 輸入、lease 到期 |

- 事件攜帶建立時的世代；actor 比對不符就丟棄狀態轉移，但 I/O 證據仍保存（§5.6）。
- 對外的 `drain_timeout_epoch` 取值為 `bridge_generation`，以便與 Python 對照（`SC-04`）。

### 5.4 單一 TX 寫入者與 fence（`tx_writer.rs`）

```rust
pub struct TxPermit { pub op_id: OpId, pub epoch: u64, pub owner_gen: u64, pub class: TxClass,
                      pub source: String, pub cmd_id: Option<String>, pub log: bool }
pub enum TxClass { Flash, FlashProbe, LeaseOwner, HumanRaw, HumanReplay, LoginProbe, ReadyReconfirm, Command,
                   FileTransfer, SystemRecoverKey, SelfTestProbe, InteractiveOpenCmd }
pub struct FenceState { pub active_epoch: Option<u64>, pub owner_gen: u64, pub frozen: bool /* drain phase1／release */,
    pub quiet_active: bool, pub reconfirm_pending: bool, pub flash_mode: FlashMode /* Off | Probe(probe_id) | Flashing */,
    pub wal_unhealthy: bool, pub cancelled: SmallSet<OpId>, pub in_flight: Option<InFlight> }
pub struct Fence(Mutex<FenceState>);
```

三個檢查位置（規格 §5.2.2）：

1. **Admission**：RPC handler 送 `CmdSubmit` 給 actor，`SessionCore` 依 §5.3 表與 SC-01／02／03 檢查狀態、gate、容量（`SC-06`），再加 `_resolve_session_id` 的 Python 順序。
2. **出列**：actor 把命令交給 OpRunner 前重驗 epoch、owner、quiet／reconfirm、release／recovery、cancel。
3. **OS 提交時（授權與提交同一臨界區，R0-F1）**：TxWriter 出列時先以 `Fence::admit` 做一次快速拒絕（被拒者不呼叫 PAL；通過**不算**授權）。真正的授權發生在 PAL 每次 OS 提交的 `SubmitGate::submit`（§4.2）內：`FenceSubmitGate { fence, permit }` 取 `Fence` 鎖 → 重跑 `Fence::admit`（`permit.epoch == active_epoch`、`!frozen`、class 對應的 gate 規則、世代規則（`owner_gen`／`fsm_gen`／`input_epoch`）、`op_id ∉ cancelled`）→ 通過才在**仍持鎖**時呼叫非阻塞的 `os_submit`（POSIX 一次 `write(2)`；Windows 發出 overlapped `WriteFile`）→ 提交有交出 bytes（`Accepted(n>0)`／`Pending`）時於同一臨界區設 `in_flight = Some(op_id, len)` → 放鎖。完成等待（POLLOUT、`GetOverlappedResult`）一律在鎖外。POSIX 部分寫入後的下一段再經 gate 重驗；撤銷後的尾段不交 OS（結果 `Completed(k)`＋`cancelled`，k=0 時 `NotSubmitted`，原因寫入 `TxReport.reject`）。寫入結束後取鎖清 `in_flight`，並把 `TxResult` 交給 WalWriter（只記錄確認的 bytes，`SC-07`），最後送出 `UartTxCompleted`。**禁止**「鎖內驗證、放鎖、再呼叫 OS write」的寫法：兩者之間的 Quiet／換手／drain 會讓失效世代的 bytes 被寫出（規格 §5.2.2「讀一次旗標，過一段時間再 write」）。

   actor 的任何失效操作（drain、release、quiet arm、lease close／expiry、soft preempt、flash freeze、`input_epoch++`）都在**同一把鎖**內修改 `FenceState`，因此與 OS 提交互斥，形成可測試的線性化邊界：
   - 失效先取得鎖 → 之後每一次 `gate.submit` 都看到新狀態，尚未提交的 bytes 一律不交 OS（zero-TX 只對這類操作斷言，規格 §5.2.2 末段）。
   - 提交先取得鎖 → 失效呼叫阻塞到該次**非阻塞**提交返回（微秒級；不等完成），該段 bytes 已交 OS，記為在途（`in_flight`）並照實記錄，不宣稱可撤回。
   - 鎖不跨完成等待：寫入卡在完成等待（D-state、RTS/CTS 卡住）時，失效操作不被阻塞，Phase 1 仍可立即進行並以 `CancelToken` 收斂（§5.5）。

TxClass 與 gate 規則（規格 §5.2.1）：

| class | 擋下條件 |
|---|---|
| `Command`／`FileTransfer` | `quiet_active`、`reconfirm_pending`；source 以 `human:` 開頭時另見 `DF-02` |
| `LoginProbe`／`ReadyReconfirm`／`SystemRecoverKey`／`SelfTestProbe` | quiet、`wal_unhealthy`（停止自動 TX，規格 §5.4） |
| `HumanRaw` | `owner_gen` 不符時改走 deferred |
| `LeaseOwner`／`InteractiveOpenCmd` | 不受 quiet 阻擋；`permit.owner_gen != owner_gen`（lease close／expiry／soft preempt／換手後的舊 lease 世代）→ `OwnerMismatch`。此世代檢查在每次 `gate.submit` 臨界區內重跑（R0-F1） |
| `Flash` | 只在 `FlashMode::Flashing` 時放行 |
| `FlashProbe` | 只在對應的 `Probe(probe_id)` 時放行 |
| 其他所有 class | `flash_mode != Off` |

TxWriter 的 job queue 容量 64；滿時 OpRunner 阻塞等待（只有 OpRunner 會送 job）。

### 5.5 Two-Phase Drain Barrier（`drain.rs`）

- **觸發**：`DeviceRelease`、`SessionRecover(force)`、`UartIoError`、`DeviceDetached`（裝置移除或改 path）、`clear`／`bind` 的 detach、daemon shutdown。
- **去重**：`pending_stop: Option<StopOp{ id, kind, waiters: Vec<Reply> }>`。重複的 release 或 recover 加入同一個 StopOp 的 waiters，不會再啟動另一組關閉流程。

**Phase 1**（actor 內同步、在 fence 鎖內）：

1. `active_epoch=None`，`frozen=true`。
2. state 設為 `RECOVERING`（release 也一樣）。Python 現況是 DETACHED→ATTACHING；改為 RECOVERING 屬 `SC-04`。
3. 尚未提交的 TX job 標 `NotSubmitted`。
4. arbiter 中 `accepted` 的命令 → `FLUSHED_BY_RECOVERY`（shutdown 用 `FLUSHED_BY_SHUTDOWN`）。
5. DeferredBuffer 整段清空，`owner_gen++`（BC-02）。
6. 依 Python `_detach_session_locked` 契約關閉 lease 與 capture。
7. 對 reader 呼叫 `IoWaker::wake()`，對 writer 呼叫 `CancelToken::cancel()`。
8. 排定 `WorkerDrainTimeout{epoch, stop_id}`，3000 ms 後觸發。

**Phase 2**（IoWorker 內）：

1. reader 回傳 `Woken`；Windows 先 CancelIoEx，再 GetOverlappedResult，已讀到的 bytes 照常落 WAL。
2. writer 完成在途寫入，取得 `TxResult`。
3. WalWriter 記錄所有可確認的 TX；記錄失敗的部分收集到 `unrecorded`。
4. `CloseToken::close` → `CloseReceipt`（POSIX 先 `TIOCNXCL` 解除獨占再關 fd，見 §4.2；否則 PTY master 仍開啟時 PROBE→正式 bridge 重開、release 後外部工具開啟都會 EBUSY）。
5. 送出 `WorkerStoppedReceipt { epoch, stop_id, tx_outcomes: Vec<(OpId, TxResult)>, unrecorded: Vec<EvidenceGap>, wal_error: Option<PalError>, handle_closed: bool }`。`tx_outcomes` 只含該 epoch **尚未結算**的 op（§5.7 TX 帳本結算協定；P5 實作為 `TxLedger`），缺項等同未提交；已結算 op 遺留的 WAL 證據缺口仍列入 `unrecorded`（有上限，溢位以合併計數交回，不靜默丟棄）。

規則：

- 未確認 I/O 收斂或 `handle_closed=false` 時**不得**送出成功回條，改送 `handle_closed=false` 並附錯誤，actor 維持 stalled。
- WAL 不可寫**不阻止**關閉 handle（規格 §5.2.4），但回條必須列出未落盤的證據。

**actor 收到回條後**：

1. 驗證 `epoch` 與 `stop_id`。
2. 套用 BC-03 對在途命令的處置（§5.7）。
3. 依 StopOp 種類收尾：
   - release：StateStore 持久化 released（§5.9）；成功才 `RELEASED` 並回覆所有 waiters。
   - recover／io_error：裝置在 → `ATTACHING` 並啟動 attach；不在 → `DETACHED`。
   - shutdown：回報給 daemon_main。

**逾時**（收到 `WorkerDrainTimeout` 時 StopOp 仍未完成）：

- 維持 `RECOVERING`，設 `drain_stalled=true`、`drain_timeout_epoch=<bridge_generation>`。
- release 的 waiters 收到 `{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":true,"drain_timeout_epoch":N,"session":…}`（`SC-04`，owner 確認項 4）。
- 回條遲到時仍完成同一個 StopOp。之後再次呼叫 `device release`，依實際結果回 `already_released`；若仍在 drain，同樣回 `SESSION_RECOVERING`。
- 回條到達前，**不得**對同一端點再次呼叫 `open_exclusive`；manager 的 attach 路徑會檢查 `drain_in_progress(by_id)`。

### 5.6 過期事件

- 舊 epoch 的 `UartRxChunk` 已經落 WAL，actor 只丟棄其狀態效應：不 fan-out 給登入 FSM，也不更新 RX 計數。
- 舊 epoch 的 `UartTxCompleted`：WAL 已由 TxWriter 寫入，actor 只把結果記到原 op_id 的命令紀錄。
- 舊的 `fsm_gen` 或 `owner_gen` timer：直接丟棄。

### 5.7 TX 結果處置（BC-03）與命令紀錄

- 每個 op 累計 `confirmed_bytes` 與 `unknown: bool`。
- **TX 帳本結算／退休（R1-F8）**：累計放在 TxWriter 的 epoch 帳本，只保留**未結算** op，不得隨連線存活時間無界增長。
  - 只有 actor 能結算：op 到達終態處理點（命令紀錄已 `complete`，或其完成事件已套用：`OpFinished`、`LoginFinished`、lease close／expiry、human TX 結算等）後恰送一次 `SettleTx{epoch, op_id}`；executor 只送給現行 epoch 的 TxWriter（非阻塞 `try_send`，容量 256；滿時該 op 留在帳本，屬過度保留、不失證據）。
  - drain 的 `aborted_op` 與尚未終態的 op **永不結算**，因此 settle 與 drain 不論誰先到達 TxWriter，BC-03 映射所需的在途證據都在 receipt 中；TxWriter 停止前先套用已到達的 settle。帳本缺項的 op 映射同 `confirmed_bytes=0`、非 unknown。
  - 結算時該 op 的 WAL 證據缺口移入有界的未解決集合（256，溢位合併最舊者為 `{gaps, bytes, first_op, last_op}` 計數）；單一 op 的缺口上限 16，溢位同樣合併。
  - 未結算 op 達 256：**不淘汰任何在途項目**；新 op 的 job 不交 OS（`NotSubmitted`＋`Frozen`、不建項目），並以 `UartIoError{reason:"TX_LEDGER_SATURATED"}` 走既有 IoError drain 收斂（新 epoch 帳本歸零）。
- recovery、release、drain 中止在途命令時，命令紀錄依下列規則處置：
  - `confirmed_bytes ≥ 1` → `status:"error"`、`error_code:"COMMAND_ABORTED_IN_FLIGHT"`（`retryable:false`）。
  - 所有寫入都是 `NotSubmitted` 或 `Completed(0)` → `FLUSHED_BY_RECOVERY`。
  - 有 `Unknown` 且 `confirmed_bytes=0` → `SEND_FAILED`（沿用既有映射，見 `ERR-7.3-9`）。
- 任何情況都不自動重送。
- 其餘命令紀錄規則逐字照 arbiter.py：`ok:true` 回應中的 `error_code` 不寫入紀錄；cancel 只標記，不中止；history 上限 512，依 `done_at` 字串淘汰。pending 計數規則見 `SC-06`。

### 5.8 WAL Writer（BC-01，`wal.rs`＋`WalFs`）

- `WalWriter { inner: Mutex<WalInner> }`。`WalInner` 持有常駐的 `wal: Box<dyn WalFile>`、`mirror: Box<dyn WalFile>`、`seq`、`file_first_seq`、`last_identity_check_mono`、health 計數。
- `append(com, dir, source, payload, cmd_id, loss_flag, meta) -> WalAppend{seq, ok}` 的步驟：
  1. 鎖外計算 b64 與 crc32。
  2. 鎖內 `rotate_if_needed`。
  3. `seq += 1`，取 `mono_ns` 與 `wall_us`。
  4. 序列化。
  5. `wal.write_all_flush(line)`，再 `mirror.write_all_flush(printable)`。WAL 先寫，任一失敗**只重試失敗的那個檔**（`DF-17`）。
  6. 失敗時設 `loss_flag`、遞增 `write_failures`、記錄 `last_write_error`。
- **unlink 偵測**：每次 append 時，若距上次檢查 ≥ 50 ms，比對 `path_identity(wal_path) != wal.identity()` 或 `wal.unlinked()`。命中則：
  1. 標記失效。
  2. 保存舊 identity 與 seq 範圍 `[file_first_seq, seq]`。
  3. 關閉自身 handle。
  4. 有界重試重開，最多 3 次，間隔 10／50／200 ms。
  5. 成功後先寫 META `WAL_RECREATED_AFTER_EXTERNAL_UNLINK`（§3.8），再寫原紀錄。
  6. 重開失敗時 `health.healthy=false`，fence 設 `wal_unhealthy=true`（停止自動 TX），記錄仍以 `loss_flag` 回傳。
- **rotate／reset**：同一把鎖內依序 flush、`sync()`、rename（handle 仍開著，兩平台都可以，因為 handle 帶 share-delete），成功才關閉舊 handle 並開新檔，最後 `sync_dir`。
  - rename 失敗時沿用舊 handle，該筆紀錄帶 `rotation_failed:true`。
  - 同名歸檔加 `.N` 後綴（`DF-03`）。
  - reset 用相同機制，條件 `> 0`，seq 歸 0。
- **shutdown**：flush 加 sync 後關閉（`SC-11`）。
- **啟動**：掃描現行檔，取最大合法 `seq`（與 Python 相同），並記下第一筆的 seq 作為 `file_first_seq`。
- **熱路徑**：每筆 2 次 write syscall，無 fsync，對應規格 §1.2 的 p50 目標。

### 5.9 Release／Attach 的持久化順序（規格 §6.2）

- **release**：Phase 1 → Phase 2 回條（`handle_closed=true`）→ `StateStore::commit(released += {sid: {by_id, released_by, released_at, reason}})`。
  - 原子寫加 fsync 成功後，記憶體才轉 `RELEASED`、回覆 ok。
  - 寫入失敗時記憶體仍標 `RELEASED`（阻擋自動 attach 與自動 TX），回 framing 層 `EXCEPTION`（含 message、無 version），與 Python 相同（`RPC-H-11`）。
- **重啟**：以最後有效的 `state.json` 為準。動態 session 的 released 項目在 init 時保留（`DF-04`）。`_mark_missing_devices` 不得把 RELEASED 降成 DETACHED（`DF-04`）。
- **attach**（`SC-05`）：
  1. 確認沒有 drain 在進行。
  2. 非 force 時做 holder 預檢：`pids` 非空 → `DEVICE_STILL_HELD{pids, holder_unknown:false}`。`force` 只略過這個**開檔前**快照，**不得**略過已識別的外部 holder（規格 §6.2、BC-04）：步驟 3 的 `open_exclusive` 鎖後重掃（§4.2）對 force 一樣執行。
  3. **同步** `open_exclusive`：
     - `Held`（含鎖後重掃識別到的外部 holder，例如未取 flock 的 minicom）或 `AccessDenied` → `DEVICE_STILL_HELD{pids, holder_unknown}`，session 維持 `RELEASED`、不啟動 I/O、UART 零 TX；
     - `NotFound` → `DEVICE_NOT_PRESENT`；
     - 其他錯誤 → 照 Python 非同步失敗的結果（session `DETACHED`、`last_error="ATTACH_FAILED:<kind>"`），RPC 回 ok。
  4. 成功時：清除 released、`StateStore::commit`、state `ATTACHING`、回 ok，login 流程非同步進行。

### 5.10 有界資源（規格 §5.4；Slice 2.1 前由 P5 實測凍結）

| 資源 | 上限 | 滿載處置 |
|---|---|---|
| control／data lane | 256／1024 chunk | §5.2 |
| TxWriter job queue | 64 | OpRunner 阻塞 |
| TX 帳本未結算 op（§5.7） | 256 | 新 op 不提交、不建項目，`UartIoError{TX_LEDGER_SATURATED}` → IoError drain；不淘汰在途證據 |
| TX 帳本未解決證據缺口／單一 op 缺口 | 256／16 | 合併最舊者為計數（`gaps`、`bytes`），隨 receipt 交回 |
| TX settle 佇列 | 256 | `try_send` 失敗時該 op 留在帳本（`settle_dropped` 計數） |
| arbiter pending（`CMD_PENDING_MAX`） | 256 | `SESSION_QUEUE_FULL`（`SC-06` 計數） |
| arbiter heap | 等於 pending 上限（admission 保證） | — |
| history | 512 | 淘汰最舊已完成者 |
| BackgroundCapture | 4194304 字元×64 | 截頭（至少留 1 段）；淘汰已結束者 |
| RxWindow | 131072 字元 | 截頭，`_rx_trimmed` 累計 |
| RX 速率視窗 | 4096 項 | 丟最舊 |
| DeferredBuffer | 每 client 262144 B | **整段清空**（BC-02），log WARNING |
| console line buffer | 16385 B | 丟棄整行，log WARNING（`SC-16`） |
| console 出向 ring | 每 console 1 MiB | drop-oldest（`SC-15`） |
| WAL 待寫 | 0（同步 append） | 失敗時 `loss_flag`；不可寫時停止自動 TX |
| matcher queue | 1024 | drop-oldest 加 `event_dropped` |
| dispatcher | 8，每條 rule 1 | 丟新加 `event_dropped` |
| RPC 連線 | 128 | 超過時 acceptor 暫停 accept（kernel backlog 承接） |
| RPC 請求行 | 65536 B | 斷線不回應（`RPC-H-02`） |
| client 回應累積 | 16 MiB | 視同 `INVALID_RESPONSE`（Python 無限制；Rust 加上限，不影響正常回應） |
| interactive tombstone | 每 session 16 | FIFO |
| reprobe／attach thread | 每 session 或 by_id 1 | inflight 去重 |

### 5.11 Quiet Gate、Lease 與 DeferredBuffer

- **Quiet gate**（`quiet_gate.rs`）：逐字照 05 §7，包括 arm／clear 點、只清 quiet 不清 pending、`_boot_quiet_blocks_probe` 等。
  - submit-time 與 execute-time 都回 `retry_after_s`（`SC-03`）：quiet 中為剩餘秒數（1 位小數）；只剩 pending 時為 `5.0`。
  - `READY_UNCONFIRMED` 不帶 `retry_after_s`。
- **Lease**（`lease.rs`）：沿用 Python 規則，加上 actor 每 1s tick 主動處理到期（`SC-13`），到期後保留 tombstone。`interactive_status` 仍會 touch，不在重試白名單內。
- **DeferredBuffer**（`deferred.rs`）：

  ```rust
  struct Entry {
      client_id: String,
      client_lifetime: u64,
      owner_gen: u64,
      bytes: Vec<u8>,
  }
  ```

  - 回放時只送 `owner_gen` 等於目前上下文、且 client 仍存活的項目。
  - mailbox 中尚未處理、但帶舊 `owner_gen` 的 `ConsoleInputBytes` 一律丟棄（BC-02 測項）。
  - 沒有發生上下文失效時，照 #53 的正常回放流程。

---

## 6. `sw-daemon`：組裝、framing、分派

### 6.1 啟動與停止（`daemon_main.rs`）

順序逐字照 02 §3.2：

1. `ensure_runtime_dirs`。
2. `load_profiles`；結果為空時 stderr 印 `serialwrapd: no profiles loaded`。
3. Windows device backend 時注入 `windows-default-passthrough` template（依 capability 判斷）。
4. `instance_lock.acquire`；失敗時 stderr `serialwrapd: DAEMON_ALREADY_RUNNING`，exit 2。
5. 建 Service；`StateLoadError` 時 exit 3。
6. 取得 OD-11 allowlist 判定（`SC-19`）；違規時 stderr `serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: <reason>`，exit 2。
7. 依序 initial scan → rank → watcher.start（`DF-14`）→ bootstrap attach → flash endpoint。
8. 綁定 RPC，再寫入 config.yaml 的 `socket_path`；寫入失敗印 stderr，視為非致命。
9. `signals.wait_stop`。

停止順序：

1. server.stop（P8 起拆為 `stop_accept`，handler join 移到 Phase 1 之後）。
   - 1a. `manager.begin_shutdown()`：stopping gate。之後熱插 Delta／Tick reattach、`RequestAttach`、bootstrap、bind／clear／recover／device.attach 重開一律不開 UART、不建 session 或 worker；已越過 gate 的在途 attach 納入第 2 步的 drain 集合（R1-F2）。
   - 1b. `watcher.stop()`：停 DeviceWatcher thread 並 join；channel 殘留的 Delta 由 gate 丟棄（P1 順序：watcher 在 drain 前停止）。
   - 1c. `engine.close_intake()`：matcher 停止、dispatcher 不再 spawn handler；已在跑的 handler 與 drain 並行（P7 起）。
   - P8 起 flash endpoint stop 位於 1c 之後、第 2 步之前（P8 Task 12）。
2. 所有 session 依 Phase 1／2 drain，總上限 5 s（`SC-11`）。
3. 對所有 session `FLUSHED_BY_SHUTDOWN`。
   - 3a. `engine.stop(第 2 步的共用期限)`：等仍在跑的 handler；逾時以 `HandlerSpawner` 終止整個 process group／Job，再最多 100 ms 回收；未回收者列入 `unreaped`。必在第 4 步前返回，handler 行程樹不得活過 WAL close 與 lock.release（P7 Task 15／16-wire、P8 Task 12）。
4. WAL sync 並關閉。
5. lock.release。
6. 全部收斂 → exit 0；任一步未收斂（flash endpoint 未 join、RPC handler 逾時未結束、drain stalled、event handler 未回收、WAL sync 失敗）→ stderr `serialwrapd: shutdown incomplete: <原因以 "; " 串接>`，exit 1，不宣稱優雅停止。各步驟皆有界等待，失敗不阻擋後續步驟，只累積到最終 exit code（P5 Task 19、P8 Task 12；SC-11 細化，DF 候選；Python 基線此情境 exit code 不改，不屬 A/B 比較範圍）。

其他規定：

- 未攔截的 panic：exit 1。停機未收斂亦 exit 1（見停止順序第 6 步），讓 `Restart=on-failure`／Task Scheduler RestartOnFailure 能辨識異常停機。
- logging 輸出到 stderr，格式 `%(message)s`，只輸出 WARNING 以上，訊息模板逐字照 10 §G-01（`GAP-G-01`）。
- 無參數執行時，所有路徑都由 env 決定（regression ThrowawayDaemon 依賴此行為）。
- 執行檔名固定為 `serialwrapd`，以符合 multi_open 的偵測規則。

**Stage 1 launcher 模式**（`launcher.rs`）：`serialwrapd` 啟動時讀 release manifest（§10）。若 `components.daemon.implementation == "python"`：

- POSIX：`execve(daemon.entry + argv)`；entry 指向 `<bundle>/py/serialwrapd.py`，argv 尾段以 `serialwrapd.py` 結尾，符合 multi_open 規則 2。
- Windows：以 Job Object 承載 Python 子行程並等待，exit code 照傳。

這樣同一個執行檔名在 Stage 1（啟動 Python）與 Stage 2（Rust 本體）都成立，不需要任何 cfg。

### 6.2 Framing（`framing.rs`，對應 `serve_connection`）

每條連線一個 thread，在同一連線內逐行依序處理（允許 pipelining）。

1. 讀到 `\n` 為止；內容超過上限時**關閉連線、不回任何 bytes**。上限邊界由 Stage 0 擷取 asyncio `readuntil` 的實測值（`RPC-H-02`）。
2. 以 Python `bytes.strip()` 的字元集 `b" \t\n\r\x0b\x0c"` 去頭尾；空行略過。
3. `decode_utf8_replace` → `pyjson::loads`。失敗回 `{"ok": false, "error_code": "INVALID_JSON"}\n`（`RPC_EARLY`，有空格、無 id、無 version）。深度超過上限時關閉連線。
4. 不是 object → `{"ok": false, "error_code": "INVALID_REQUEST"}\n`。
5. `id` 取原值，缺省為 null；`method` 不是 str → `{"id":…,"ok":false,"error_code":"INVALID_METHOD"}`（compact，無 version）。
6. `params` 不是 object → 當成 `{}`。
7. `method=="daemon.stop"` → `{"id":…,"ok":true,"stopping":true}`，並觸發 stop。
   `method=="daemon.handoff_stop"`（SC-11 交接停止，R1-F1；P8 Task 14 實作，之前的 Rust artifact 落到第 8 步回 `METHOD_NOT_FOUND`，CLI 據此 fail-closed 拒絕更新）→ 呼叫 `sw_core::admission::AdmissionGate::freeze_if_idle(reason)`：閒置 → `{"id":…,"ok":true,"stopping":true,"handoff_token":<32 hex>}`（無 version）寫出後觸發與 `daemon.stop` 相同的 stop；忙碌 → `{"id":…,"ok":false,"error_code":"FLASHING_BUSY"|"SESSION_BUSY","busy":[{com,state,foreground_busy,outstanding_commands,interactive_session_id,recovering,ops}]}`，不凍結、不停止。`AdmissionGate` 是所有新操作的共同 admission 邊界（單一 `Mutex`，葉鎖，持有時不得送 actor 訊息或做 I/O）：service 分派在把任何「`side_effects` 非空且 `retryable_readonly==false`」的方法交給 SessionActor／CommandRegistry 之前 `admit(kind, com)`（ticket 持有到該操作終態：命令紀錄終態、file 傳輸返回、lease close／expire），flash endpoint 在 sync-probe 寫第一個 byte 前 `admit("flash", com)`（被拒則端點沉默、不進 FLASHING），SessionActor 在狀態轉移時同步發布 `recovering`／`flashing`／`lease` 旗標；`freeze_if_idle` 與 `admit` 同鎖，故操作要嘛在凍結前取得 ticket（交接回忙碌），要嘛在凍結後被拒（`FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`，零 TX、零狀態改寫）。SC-11 停止序 Phase 1 的 `TransferAbort`／撤銷前景命令／flash pump 停止是**中止**，只有經交接 ok 的停止才保證沒有可被中止的不可安全中止操作。
8. 其餘交給 `service.rpc`。
   - handler 回 `Err(PyErr)` 或 panic 時，回 `{"id":…,"ok":false,"error_code":"EXCEPTION","message":…}`，無 version。
   - 正常時 `resp={"id":id}`，再依序併入 result；result 若含 `id` 會覆寫值，但位置仍在第一個。
9. 以 `RPC_WIRE` 序列化。client 斷線不取消 handler；沒有請求逾時。

**保留的 Python quirk**（對照調和表）：

| 行為 | 調和表 ID |
|---|---|
| 超長行斷線不回應 | `RPC-H-02` |
| NaN 接受並回送 | `RPC-H-03` |
| early 回應格式不一致 | `RPC-H-04` |
| params 非 object 時靜默改為 `{}` | `RPC-H-06` |
| `EXCEPTION`、`INVALID_METHOD`、`daemon.stop` 回應無 version | `ERR-7.1-g` |
| result 的 `id` 覆寫 | 本節第 8 步 |
| 重複 key 取後者 | §3.3 |

**修正或改變的行為**：

| 行為 | 調和表 ID | 說明 |
|---|---|---|
| 阻塞 method 拖住所有連線 | `RPC-H-12` | Rust 沒有單一 event loop，屬內部實作差異 |
| 例外訊息文字 | `RPC-H-08` | `EXCEPTION.message` 非契約 |

### 6.3 分派器（`service.rs`）

```rust
pub fn rpc(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr> {
    let mut r = self.dispatch(method, params)?;
    r.setdefault("version", PyValue::Str(self.version.clone()));
    Ok(r)
}
fn dispatch(&self, method: &str, p: &PyDict) -> Result<PyDict, PyErr> {
    match method {
        "health.ping" => Ok(pydict!{"ok"=>true, "pong"=>true}),
        "health.status" => self.health(),
        "mcu.patterns" => …,
        // … 共 53 個分支，順序與 service.py:705-1174 相同，每個分支內的強制轉型照 02 §7 逐行對照 …
        _ => Ok(pydict!{"ok"=>false, "error_code"=>"METHOD_NOT_FOUND", "method"=>method}),
    }
}
```

- 這是平面 `match`，不使用 registry 或 macro 生成，對應 CLAUDE.md「RPC 路由是平面 if/elif」。
- 所有 session 類 method 經 `SessionManager::route(selector)` 轉成 actor request，並以有界 reply channel 等待。等待上限：一般 method 60 s，`LONG_RPC_METHODS` 與 file 類 600 s。逾時時回 framing `EXCEPTION`（`message="internal timeout"`）。這條路徑在正常情況下不可達，屬防禦用途。
- `health.status` 的 key 順序照 02 §7.1，額外欄位：
  - `test_device_allowlist`：只在啟用時出現（`SC-19`）。
  - Windows 的 `multi_open` 由 `ProcScan` 提供（`SC-17`）。

---

## 7. `sw-cli`

### 7.1 結構與輸出

- `main.rs` 流程：
  1. 取得 `effective_argv`。
  2. argparse 相容層 `parse`。
  3. 處理 JSON 邊界：只對 `connect`／`benches` 生效，`_json_boundary_command_from_argv` 逐字移植，包括 `--socket connect` 的 quirk。
  4. 依 `args.cmd` 平面分派。
  5. 回傳 exit code。exit code 總表逐字照 01 §2.4。
- `output.rs`：`print_json`（`CLI_STDOUT`）與 `format_err_line`，後者格式為 `serialwrap: {ctx }failed: {code|message|UNKNOWN_ERROR}[: msg][（hint: …）]`，用全形括號；另有 `warn_version_mismatch`，訊息與輸出順序照 01 §1.2 與 §6.15。
- stdout、stderr 一律寫 UTF-8 bytes，等同 `force_utf8_stdio`。CLI 不安裝訊號 handler：Ctrl-C 走預設行為（shell 看到 130）；`BrokenPipe` 時 exit 1、不印 traceback（`GAP-G-08`）。
- 20 個頂層群組依 parser 註冊順序：daemon、device、session、alias、cmd、stream、log、file、wal、mcu、remote、connect、benches、event、supervision-mode、service、setup、doctor、profile、skill。子命令、旗標、dest、預設值與 params 組裝逐字照 01 §6，`cli_spec.rs` 以宣告式表格描述。新增的 `daemon restart`、`remote stop`、`supervision-mode set`、`setup --rollback` 見 SC-20～23。

### 7.2 argparse 相容層（`argparse/`）

- 支援的功能：
  - positional、`nargs='*'`；
  - action：store、store_true、count、append、version、help；
  - `type=int` 與 `type=float`，錯誤訊息如 `argument --retries: invalid int value: 'x'`；
  - choices，訊息如 `argument action: invalid choice: 'x' (choose from 'start', 'stop', 'restart', 'status')`；
  - required；mutually exclusive group（含 required）；
  - subparsers：`required=True`、`metavar="<group>"`／`"<command>"`、`title`；
  - `allow_abbrev`：唯一前綴縮寫；有歧義時訊息為 `ambiguous option: --s could match --socket, …`；
  - `--opt=value`、`-vv`、`--` 分隔。
- help 與 usage 移植 CPython 3.12 的 `HelpFormatter` 與 `RawTextHelpFormatter`：
  - 寬度取 `COLUMNS` env，其次 `terminal_columns()`，皆無時用 80，再減 2；
  - `max_help_position=24`；
  - 長度以 code point 計，CJK 也算 1，與 Python `len()` 相同；
  - 段落標題為 `options:`；
  - epilog 原文輸出。
- 錯誤一律走 stderr：`usage: …\n{prog}: error: {msg}\n`，exit 2。
- golden：`scripts/rust/capture_cli_help.py` 以 Python 3.12 對每個 parser 路徑擷取 `--help`（`COLUMNS=80` 與 `120`）以及約 60 個錯誤情境，存入 `contracts/cli_help/`。Rust 單元測試與 `tests/contract_blackbox` 都比對這份 golden。基線 Python 版本固定 3.12（CI 版本）。

### 7.3 Endpoint 解析、client、trace

- `endpoint_resolve.rs` 移植 01 §4：解析優先序、dangling fallback 與通知文字、`_EndpointResolutionError` 的 exit 1。
- `client.rs` 移植 01 §3：
  - 每次呼叫開新連線；請求 key 順序為 `id`、`method`、`params`；
  - `recv(65536)` 累積到出現換行；
  - 錯誤分類與 message 照 Python `str(OSError)`，由 PAL `py_message` 提供；
  - 白名單重試、退避、TIMEOUT enrich；
  - 回應不是合法 JSON 時回 `INVALID_RESPONSE`（`DF-06`）。
- `cli_trace.rs` 移植 01 §5：key 依插入序、`endpoint_id` 用 sha256、層級解析與 `SERIALWRAP_LOG_LEVEL`。

### 7.4 Stage 1A 委派（`delegate.rs`）

委派集合（1A 未承接）：`daemon`、`service`、`supervision-mode`、`remote`、`connect`、`benches`、`setup`、`doctor`。

1A 原生承接：其餘 12 個群組全部，包含 `session console-attach/console-detach`（純 RPC，`CLI-11.08`）、`profile test`（用 pyre）、`skill`（內嵌 `SKILL.md` 與 `SKILL_WINDOWS.md`，以 `include_str!` 取得，sha256 golden 照 09 §6），以及 `--bench` 解析（只讀 `benches.state.json`）。

**Python entry 的取得**：

- 只從 release manifest 的 `components.python_cli_delegate.entry` 取得絕對 argv，例如 `["<bundle>/py/bin/python3","-I","-m","sw_core.cli"]`；Windows 為 `["<bundle>\\py\\python.exe","-I","-m","sw_core.cli"]`。
- manifest 的位置：
  - POSIX：`current_exe()` 的 realpath 的上一層目錄中的 `release-manifest.json`；
  - Windows：`bin\current-release.json` 指向 `versions\<v>\release-manifest.json`（§10.2）。
- 找不到 manifest 或 entry 不是絕對路徑時：stdout `{"error_code":"INTERNAL_ERROR","message":"delegation target unavailable: <reason>","ok":false}`，stderr 鏡像，exit 1。
- **絕不搜尋 PATH**。開發與測試以 `scripts/rust/mkbundle.sh --dev` 建立含 manifest 的暫時 bundle。

**遞迴防護**：

- 委派時設 `SERIALWRAP_DELEGATE_DEPTH=<n+1>` 與 `SERIALWRAP_DELEGATE_ARGV_SHA=<sha256(argv)>`。
- Rust CLI 在 depth ≥ 2 或 argv sha 與本次相同時拒絕再委派，錯誤形狀同上，message 為 `delegation recursion detected`。
- 合法的 Rust→Python→Rust 鏈（例如 Python setup 內部執行 `serialwrap daemon stop`）depth 為 1，不受影響。

**保真要求**：

- argv 原樣傳遞（含全域選項）；env 繼承，僅加上述兩個變數。
- stdin、stdout、stderr 直接繼承，不經 pipe。
- exit code 原樣回傳；POSIX 以 `execve` 取代行程，訊號語意天然一致；Windows 見 §4.9。
- Rust 端不重送、不重試、不自行加逾時。
- 委派前**不做任何輸出**。

### 7.5 重試白名單

`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`。

只有這 11 個 method 在 `--retries` 下重試，且只在 transient 錯誤碼 `{TIMEOUT, SOCKET_ERROR, EMPTY_RESPONSE}` 時重試。`session.interactive_status` 與所有 event、file、log 類 method **永不重試**。event dispatch 不轉送 `--retries`，與 Python 相同。

### 7.6 Stage 1B 承接

`doctor_cmd.rs`、`setup_cmd.rs`、`service`、`supervision-mode`、`remote_tunnel.rs`、`connect`、`benches`、`daemon start/stop/restart/status` 全部改為原生，行為逐字照 01 §6.11～6.19、§7 與 07 §5～§9。調和表的 DF 與 SC 項目在 Python Stage 0 先改，Rust 照修正後的基線承接。新增功能：

| 項目 | 內容 | 調和表 |
|---|---|---|
| `windows-task`／`windows-service` 模式 | — | `SC-24` |
| `setup --rollback` 與版本目錄 | — | `SC-23` |
| `supervision-mode set` | — | `SC-22` |
| `daemon restart` | — | `SC-20` |
| `remote stop` | `close` 的別名 | `SC-21` |
| Windows on-demand 首次需求自動啟動 | — | `SC-18` |
| doctor `version_pairing` 檢查 | — | `SC-25` |
| setup 物化 `serialwrap-minicom` 等 wrapper | 原樣複製 `.sh` 資產，只在 POSIX 物化；Windows 不物化 | — |

### 7.7 remote（BC-05）

POSIX 路徑逐字照 07 §8：argv、identity、registry、`ControlMaster`／`ControlPath`、`ssh -O check`、role probe、`_bind_is_loopback_only`。

Windows（`ProcessTunnel::supports_control_master()==false`）的差異：

- **readiness**：
  - connect：直接對本地 forward 做 `health.ping`（2.0 s）；
  - expose + tcp：另開一條 ssh 連線執行 `ss -ltnH 'sport = :<port>'`，不使用 ControlPath；
  - expose + `--remote-socket`：只要行程存活即視為 ready，與 POSIX 相同。
- **registry 鎖**：改用 `FsOps::file_lock_exclusive`（LockFileEx）。
- **存活判定**：`ProcessTunnel::alive`，不用 `os.kill`。
- **停止**：terminate tree。
- argv 不帶 ControlMaster 相關的 3 個 `-o` 選項；identity 計算仍包含使用者的 `ssh_opts`。

connect 與 benches 是否一併納入 Windows 支援，為 `SC-27`（owner 確認項）。

---

## 8. 跨 Phase 的 Python 相容要點（實作者常漏）

- regex 一律走 pyre（§3.6），不得直接用 `regex` crate 的語法。
- YAML 一律走 yaml11（§3.7）。
- 時間：WAL 的 `wall_ts` 是 UTC ISO 字串，microsecond 為 0 時省略小數；`wal.reset` 與 rotate 後綴用 UTC `%Y%m%d-%H%M%S`；log capture 檔名用本地時間 `%y%m%d-%H%M%S`；event 的 `ts`、`matched_at`、`last_fire_ts` 用 wall ms。
- 計數單位：字元與 bytes 混用的規則見 09 §9 第 5 點。
- `next_reprobe_at` 輸出 monotonic 原值，Python 也是如此。
- 集合順序不定的回應（例如 `event.reload`）：契約定為集合，Rust 輸出升冪（`RPC-H-13`），比較器以集合比對。

---

## 9. 測試架構

### 9.1 Rust L1

- 各 crate 的 `#[cfg(test)]` 單元測試與 `tests/` 整合測試。
  - `SessionCore` 以事件序列配合 FakeClock 覆蓋 §5.3 狀態表全部列、BC-02、BC-03、drain、quiet。
  - TxWriter 以 FakeSerial（可注入部分寫入、Unknown、阻塞、取消失敗，以及「授權後、OS 提交前暫停」`PauseBeforeSubmit`、「提交臨界區內暫停」`PauseInsideSubmit`、「部分提交後 EAGAIN 暫停」`AcceptThenWouldBlock`）覆蓋 fence 線性化：在授權通過後、OS 提交前觸發 Quiet／drain／lease 換手（`owner_gen++`）必須零 TX（`os_submits()==0`、`take_tx()` 為空、無 TX WAL）；提交臨界區內的失效呼叫必須阻塞到提交返回；每次 OS 提交時 Fence 鎖必為持有狀態（06 Task 9 `authorized_then_*_before_os_submit_is_zero_tx`、`os_submit_always_inside_fence_critical_section`）。
  - WalWriter 以 FakeWalFs（可注入 unlink、replace、寫入失敗、rename 失敗）覆蓋。
- PAL conformance suite：`sw_pal::conformance::{serial_suite, wal_fs_suite, lock_suite, rpc_suite, console_suite}`。同一套斷言分別跑在 fakes、POSIX PTY、Windows COM pair 上。在 fakes 上通過**不得**計為原生 gate。
- 資源標記：需要特定資源的測試命名為 `cap_<resource>__<name>` 並加 `#[ignore = "cap:<resource>"]`。resource 取值：`posix_pty`、`windows_com`、`physical_uart`、`supervisor_runtime`、`interactive_desktop`、`process_rpc`。執行時 runner 以 `-- --ignored cap_<resource>__` 選取，被略過的會顯示為 ignored，**不會**算成 pass。`memory_only` 測試不加 ignore。
- 跨 crate 的 Python golden（pyjson、pycompat、pyre、yaml11、argparse）：`contracts/golden/*.ndjson` 由 pytest 以 python3 產生並 check-in；Rust 測試只讀，不呼叫 Python。

### 9.2 黑箱契約：`tests/contract_blackbox/`

- 這是 Python pytest，**不 import `sw_core`**。`conftest.py` 在 sessionfinish 時斷言 `"sw_core" not in sys.modules`，`test_no_product_import.py` 另以 AST 掃描整個目錄。
- 受測 artifact 由 env 選定：

  | env | 預設值（Python 基線） |
  |---|---|
  | `SERIALWRAP_CLI_EXE` | repo 的 `./serialwrap` |
  | `SERIALWRAP_DAEMON_EXE` | `tests/contract_blackbox/bin/py-serialwrapd`（stdlib wrapper，執行 `python3 <repo>/serialwrapd.py "$@"`） |
  | `SERIALWRAP_AB_LABEL` | 報告標籤 |

  兩個 exe 都必須是可執行檔；Rust 端為 `target/<triple>/release/serialwrap{,d}`。產品從不讀這些變數，名稱也已列入 `tests/conftest.py` 的 pop 清單。
- 輔助工具：
  - `stubd.py`：stdlib NDJSON 假 daemon，依腳本回覆並錄下收到的請求，用於驗證 CLI 送出的 params 與 framing。
  - `faketarget.py`：PTY 假 DUT，沿用 func-test 的 FakeTarget 行為模型，不依賴產品碼。
- 每個案例的隔離目錄沿用 conftest 的 9 個 env 加 5 個 pop。
- 斷言範圍：stdout 位元組、stderr 行、exit code、RPC wire、WAL 檔、`state.json`。
- `pytest tests/` 預設對 Python 基線執行，因此 Python 必須先滿足全部絕對契約；改指向 Rust artifact 時執行同一批測試。

### 9.3 既有 pytest 與套件改指 Rust artifact（A/B）

| 套件（盤點 08） | 改法 | 用途 |
|---|---|---|
| `tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py`（BB-proc，共 9 項） | 讀 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`，預設值不變 | Stage 1A／1B 換 CLI；Slice 2.x 換 daemon |
| `tests/test_bootloader_recovery.py::test_cli_parser_has_allow_attached_flag` | 同上，只換 CLI | help 契約 |
| WB-cli 的 36 檔（01 §1.5 C） | 不改原檔，把其中的 I/O 契約（stdout／stderr／exit／spawn argv）**移植**成 `contract_blackbox/test_cli_*.py`，配 stubd | CLI A/B |
| `func-test/`（DaemonHarness、cli_client） | 讀同兩個 env；補齊 WAL／CONFIG／EVENTS／LOG 隔離（`TST-D-8`） | L1.5 |
| `tools/docker/uart_harness.py`、`remote_lab.py` | 容器內讀同兩個 env（Linux artifact 掛入容器） | remote 拓樸 |
| `regression/`（TestPilot） | testbed `serialwrap_exe` 指向 bundle 的 `bin/serialwrap`；sibling `bin/serialwrapd` 為 launcher | L2 |
| `realhw/`、`reliability/` | 新增 `serialwrap_exe` 設定與 #154 版本 gate（`TST-D-10`） | L3 |

A/B 程序照規格 §15.9：先停 A 並確認資源釋放，才啟動 B；兩輪都用新的模擬器實例。比較器只用 `contracts/` 中的正規化規則（例如 `EXCEPTION.message`、集合順序），不得另寫寬鬆比對。

### 9.4 Windows 原生測試：WSL interop

**`scripts/rust/win-runner.sh <exe> [args…]`**：作為 cargo 的 Windows target runner，也供 win-test.sh 使用。

1. 以 `cmd.exe /c echo %LOCALAPPDATA%` 加 `wslpath -u` 求出 `/mnt/c/Users/<u>/AppData/Local`。
2. 建立 staging 目錄 `…/Temp/sw-rust-test/<run_id>/`，`run_id = $(date +%y%m%d-%H%M%S)-$$`。
3. 把 exe 與 `$SW_WIN_EXTRA_FILES` 複製進去。工作目錄**必須**在 Windows 本機磁碟，避免 9P／UNC 改變檔案分享與鎖的語意。
4. `cd` 到 staging 目錄。
5. 匯出 `WSLENV=SW_TEST_CAPS:SW_TEST_COM_PAIR:SW_TEST_SEED:RUST_BACKTRACE:RUST_TEST_THREADS:SERIALWRAP_*/u`，並把 `TMP` 與 `TEMP` 設為 staging 內的 `tmp`（以 `/p` 轉換）。
6. 以 `timeout ${SW_WIN_TIMEOUT:-900}` 執行 `./<exe>.exe "$@"`，exit code 原樣回傳。
7. `SW_WIN_KEEP=1` 時保留 staging，否則刪除。

**`scripts/rust/win-test.sh [--caps windows_com,…] [--package …] [cargo test args…]`**：

1. 執行 `check_cfg_boundary.py`。
2. `cargo test --target x86_64-pc-windows-gnu --no-run --message-format=json`，從輸出取出所有 `executable`。
3. 逐一以 win-runner.sh 執行：先跑一般測試；`--caps` 指定的資源再以 `-- --ignored cap_<res>__` 跑一次。
4. 彙總為 `target/win-test-report/<run_id>.json`，欄位為 `exe`、`caps`、`passed`、`failed`、`ignored`、`exit`。
5. 任何一個 exe 失敗即 exit 1。

**COM pair**：`SW_TEST_COM_PAIR=COMa:COMb` 由擁有者在 Spike 1／OD-08 後提供。沒有提供時，`windows_com` 測試保持 ignored，並在報告中標為「證據不足」，不是 PASS。

**Windows 黑箱**：Windows Python 3.12 以 `py -3.12 -m pytest tests/contract_blackbox`，搭配 `SERIALWRAP_CLI_EXE=…\serialwrap.exe`、`SERIALWRAP_DAEMON_EXE=…\serialwrapd.exe`，同樣在 Windows 本機目錄執行。入口腳本為 `scripts/rust/win-blackbox.ps1`，由 P3 建立。

**正式 gate**：

- 在 Windows 自架 runner 原生 build。
- Linux 端執行 `cargo test --workspace`、`python3 -m pytest -q tests/`、`check_cfg_boundary.py`。

---

## 10. Release manifest 與版本配對（規格 §9.5、§12.1）

### 10.1 `release-manifest.json`

位於每個 `versions/<bundle_version>/`，以 `DISK_STABLE` 的 Indent(2) 變體輸出（`sort_keys`，尾端換行）。schema 為 `contracts/release_manifest.schema.json`。

```json
{
  "schema_version": 1,
  "bundle_version": "0.4.0",
  "stage": "1A",
  "created_at": "2026-12-01T03:00:00+00:00",
  "target": "x86_64-pc-windows-gnu",
  "git_revision": "<40 hex>",
  "contract_revision": "<git tree sha of contracts/>",
  "components": {
    "cli":    {"implementation": "rust",   "version": "0.4.0", "git_revision": "…", "path": "bin/serialwrap.exe",  "sha256": "…"},
    "daemon": {"implementation": "python", "version": "0.4.0", "git_revision": "…",
               "launcher": {"path": "bin/serialwrapd.exe", "sha256": "…"},
               "entry": ["py/python.exe", "-I", "py/serialwrapd.py"],
               "runtime": {"path": "py/python.exe", "python_version": "3.12.x", "sha256": "…"},
               "package": {"name": "serialwrap", "version": "0.4.0", "wheel_sha256": "…", "tree_sha256": "…"}},
    "python_cli_delegate": {"entry": ["py/python.exe", "-I", "-m", "sw_core.cli"], "tree_sha256": "…"},
    "assets": {"skill_sha256": "…", "skill_windows_sha256": "…", "profiles_sha256": "…"},
    "supervisor": {"default_mode": "windows-task", "definition_sha256": "…"}
  },
  "pairing": {"rule": "exact", "cli_version": "0.4.0", "daemon_version": "0.4.0"}
}
```

- entry 內的相對路徑以 manifest 所在目錄為基準，執行前轉成絕對路徑。
- `tree_sha256` 是 sha256 of sorted `"<relpath>\0<sha256>\n"`，涵蓋所有部署的 Python 檔，以補足單看 `python.exe` hash 的不足（規格 §9.5）。
- Stage 2 起 `daemon.implementation` 改為 `"rust"`，`entry` 移除。
- 同目錄另有 `SHA256SUMS`。
- 建置由 `scripts/rust/mkbundle.sh --target <triple> --stage <s> [--dev]` 負責：組出 Python runtime（Linux 用 venv；Windows 用 embeddable 3.12 加 `pip install --target`），安裝本 repo wheel，產生 manifest。

### 10.2 安裝佈局與穩定入口

| 平台 | 版本目錄 | 穩定入口 |
|---|---|---|
| Linux | `${XDG_DATA_HOME:-~/.local/share}/serialwrap/versions/<v>/` | `~/.local/bin/serialwrap`、`~/.local/bin/serialwrapd` 為 symlink，指向 `versions/<v>/bin/*` |
| Windows | `%LOCALAPPDATA%\Programs\serialwrap\versions\<v>\` | `bin\serialwrap.exe`、`bin\serialwrapd.exe` 為**複本**，另有 `bin\current-release.json` 記錄 `{"bundle_version","dir"}`；不依賴 symlink 權限 |

切換流程：先把新複本寫成 `*.new`，再把舊檔 rename 成 `*.old.<ts>`（執行中的 exe 允許 rename），最後把 `*.new` rename 成正式名稱。至少保留 Current 與 Previous。rollback 流程依規格 §12.1 七步驟（`SC-23`）。第 2 步的交接安全檢查（Flash、FileTransfer、活躍寫入與其他不可安全中止操作）沒有覆寫：update／rollback 帶 `--force` 時仍拒絕，且不停止監管、不送 `daemon.stop`，因為停止 daemon 會中止傳輸或 flash pump，而 DUT 上已發生的部分燒寫無法撤銷。CLI 端預檢只做提早拒絕；第 3 步停止一律經 daemon 原子交接 `daemon.handoff_stop`（§6.2 第 7 步：busy 判定＋凍結新操作＋token 同一臨界區），update／rollback 路徑不送一般 `daemon.stop`；交接被拒或結果不明時不硬停、不切換（R1-F1，P4 Task 12、P1 T08、P8 Task 14）。

中斷復原（`install-journal.json`，P4 Task 12）：journal 必須自帶回到完整 Previous 組合所需的原始資料，不依賴行程記憶體或尚未完成的備份。第 3 步停用監管之前，先把原始穩定入口（symlink 目標或複本 sha256）、`current-release.json` 全文、`config.yaml` 複本、監管快照（mode、endpoint、enabled、running、匯出的已註冊內容）與 daemon 快照（是否健康在跑、identity、各 session 的 `attached_real_path`／`com`／`state`、重新啟動路徑）寫入 `install-journal.snapshot/` 與 journal，每次寫入都 fsync 並同步目錄。第 5 步拆成三個持久化階段：`backup_started`（只寫 `*.partial`）→ `backup_complete`（`manifest.json` 最後寫入、目錄改名提交，manifest sha256 記入 journal）→ `entries_switching`。備份提交前不切換任何入口、設定或監管。復原依階段處理：(1) 尚未停用：只刪 journal；(2) 已停用或已停止、尚未切換：不讀備份，依快照還原 enabled 與 running，確認舊行程與 tty holder 已釋放後才重新啟動舊版；(3) 已開始切換：先驗證備份 manifest 與每個檔案，通過後以快照還原入口與 current，再執行第 7 步。任一還原失敗都保留 journal（位元組不變），而且不繼續本次 update 或 rollback。

### 10.3 版本配對 gate

- 同一 bundle 內，CLI 的 `--version` 輸出 `serialwrap <bundle_version>`，daemon 的 `health.status.version` 也等於 `bundle_version`（Python 端讀 bundle 內 wheel 的 VERSION）。兩者是同一個 repo `VERSION`，不得只改字串湊成一致。
- 檢查點：
  1. 既有 L2／L3 preflight 的 #154 exact 比對維持不變，並擴及 realhw 與 reliability（`TST-D-10`）。
  2. CLI 每次 RPC 的 mismatch 警告維持 Python 行為：只警告、不阻擋。
  3. 新增 doctor check `version_pairing`（非 advisory，`SC-25`）：比對 manifest `pairing`、自身 `--version`、`daemon status.version`、manifest 的 `components.*.sha256` 與實際檔案 sha256。不一致時 `ok:false`，detail 列出差異。
  4. `setup` 與 `rollback` 在啟動新版本後必須驗證配對；失敗時回到完整的 Previous 組合。
- `binary_sha256` 不出現在 RPC 回應中，只存在於 manifest（規格 §9.5）。
