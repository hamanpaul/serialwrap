//! 常數定義（P3-T7）。
//!
//! 數值照 `rust-architecture.md` §3.1，並以 `contracts/golden/constants.json`（由
//! `scripts/rust/goldens/gen_constants.py` 以 AST 自 Python 來源抽出）逐項比對（`tests/constants_sync.rs`）。

/// `DEFAULT_TCP_PORT` 的預設原始字串（`SERIALWRAP_TCP_PORT` 未設時）。
pub const DEFAULT_TCP_PORT_RAW_DEFAULT: &str = "48700";

/// RPC TCP 僅允許的 loopback 主機（`sw_core/constants.py::LOOPBACK_TCP_HOSTS`）。
pub const LOOPBACK_TCP_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];

/// client 端可安全重送的唯讀方法白名單（`sw_core/client.py::RETRYABLE_READONLY_METHODS`）。
pub const RETRYABLE_READONLY_METHODS: [&str; 11] = [
    "health.ping",
    "health.status",
    "session.list",
    "session.get_state",
    "session.activity",
    "session.console_list",
    "session.log_status",
    "device.list",
    "alias.list",
    "mcu.patterns",
    "mcu.status",
];

/// 重試退避基底秒數（`RETRY_BACKOFF_BASE_S`）。
pub const RETRY_BACKOFF_BASE_S: f64 = 0.5;
/// 重試退避上限秒數（`RETRY_BACKOFF_MAX_S`）。
pub const RETRY_BACKOFF_MAX_S: f64 = 5.0;

/// 可重試的暫態錯誤碼（`TRANSIENT_ERROR_CODES`）。
pub const TRANSIENT_ERROR_CODES: [&str; 3] = ["TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE"];

/// TIMEOUT 探測單次逾時秒數（`PROBE_TIMEOUT_S`）。
pub const PROBE_TIMEOUT_S: f64 = 1.0;
/// TIMEOUT 探測總期限秒數（`PROBE_DEADLINE_S`）。
pub const PROBE_DEADLINE_S: f64 = 2.5;
/// 探測使用的方法（`_PROBE_METHODS`）。
pub const PROBE_METHODS: [&str; 2] = ["health.ping", "health.status"];

/// 單次 `recv` 區塊大小（`RECV_CHUNK`）。
pub const RECV_CHUNK: usize = 65536;

/// client 回應累積上限（arch §5.10 由 16 MiB 修訂為 256 MiB；Rust-only 上限，Python 無上限，見 plan §0.7）。
pub const CLIENT_RESPONSE_MAX_BYTES: usize = 256 * 1024 * 1024;

/// 一般 RPC 逾時秒數（`cli.DEFAULT_RPC_TIMEOUT_S`）。
pub const DEFAULT_RPC_TIMEOUT_S: f64 = 5.0;
/// 長 RPC 逾時下限秒數（`cli.LONG_RPC_TIMEOUT_FLOOR_S`）。
pub const LONG_RPC_TIMEOUT_FLOOR_S: f64 = 45.0;
/// 使用長逾時的方法（`cli.LONG_RPC_METHODS`）。
pub const LONG_RPC_METHODS: [&str; 8] = [
    "session.recover",
    "session.attach",
    "session.self_test",
    "session.console_attach",
    "device.release",
    "device.attach",
    "session.clear",
    "session.bind",
];

/// endpoint 存活探測逾時秒數（`cli.ENDPOINT_PROBE_TIMEOUT_S`）。
pub const ENDPOINT_PROBE_TIMEOUT_S: f64 = 0.2;

/// 系統級 socket 路徑（`constants.SYSTEM_SOCKET`）。
pub const SYSTEM_SOCKET: &str = "/run/serialwrap/serialwrapd.sock";

/// completion regex 最大位元組數（`command_completion.COMPLETION_REGEX_MAX_BYTES`）。
pub const COMPLETION_REGEX_MAX_BYTES: usize = 1024;
/// 命令警告位元組門檻（`arbiter.CMD_WARN_BYTES`）。
pub const CMD_WARN_BYTES: i64 = 4096;
/// 命令拒絕位元組門檻（`arbiter.CMD_REJECT_BYTES`）。
pub const CMD_REJECT_BYTES: i64 = 16384;

/// 檔案傳輸預設分塊大小（`file_transfer.FILE_DEFAULT_CHUNK_SIZE`）。
pub const FILE_DEFAULT_CHUNK_SIZE: i64 = 512;
/// 檔案傳輸 ACK 模式（`file_transfer.FILE_ACK_MODES`）。
pub const FILE_ACK_MODES: [&str; 3] = ["auto", "echo", "none"];

/// 委派深度環境變數名（arch §7.4；防止 Rust→Python→Rust 無限委派）。
pub const DELEGATE_DEPTH_ENV: &str = "SERIALWRAP_DELEGATE_DEPTH";
/// 委派 argv 雜湊環境變數名（arch §7.4）。
pub const DELEGATE_ARGV_SHA_ENV: &str = "SERIALWRAP_DELEGATE_ARGV_SHA";

/// 數值型 log level 的最大位數（`cli_trace._MAX_NUMERIC_LOG_LEVEL_DIGITS`）。
pub const MAX_NUMERIC_LOG_LEVEL_DIGITS: usize = 4300;

/// 終端機欄寬環境變數名（argparse `help_width` 讀取；`shutil.get_terminal_size` 的 `COLUMNS`）。
pub const ENV_COLUMNS: &str = "COLUMNS";
/// `--endpoint` 的 argparse metavar（`cli.py` 的 `metavar="ENDPOINT"`）。
pub const METAVAR_ENDPOINT: &str = "ENDPOINT";
/// `--bench` 的 argparse metavar（`cli.py` 的 `metavar="CODE"`）。
pub const METAVAR_CODE: &str = "CODE";

// Slice 2.1 P5-T0 補記（上游 P3-T7 漏列；數值照 `rust-architecture.md` §3.1 與 `sw_core/constants.py`）：
/// 每 session 進行中（accepted／running）命令的硬上限（`sw_core/constants.py::CMD_PENDING_MAX`，SC-06）。
pub const CMD_PENDING_MAX: usize = 256;
/// WAL rotate 門檻（位元組；`sw_core/constants.py::DEFAULT_WAL_ROTATE_BYTES = 64 * 1024 * 1024`，條件 `size >= R`）。
pub const DEFAULT_WAL_ROTATE_BYTES: u64 = 67_108_864;
/// 命令 history 上限（`sw_core/constants.py::CMD_HISTORY_MAX`；淘汰最舊已完成者，arch §5.10）。
/// Task 8 Consumes 自本模組；sw-core `resources` 以 `pub use` 再匯出，不另定義。
pub const CMD_HISTORY_MAX: usize = 512;
/// passthrough 安靜升 READY 的安靜秒數（`sw_core/constants.py::PASSTHROUGH_READY_QUIET_S`，SC-35；arch §5.12）。
pub const PASSTHROUGH_READY_QUIET_S: f64 = 30.0;
