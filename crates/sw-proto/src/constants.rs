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

// ── Slice 2.3（P7 Task 0，計畫 08）────────────────────────────────────────────────────────────
/// force recover 的輪詢次數（`session_manager._force_recover`：`for _ in range(10)`）。
pub const FORCE_RECOVER_POLLS: u32 = 10;
/// force recover 的每次輪詢間隔秒數（`_force_recover`：`time.sleep(1.0)`）。
pub const FORCE_RECOVER_POLL_S: f64 = 1.0;

// ── Slice 2.2（P6 Task 1，計畫 07）────────────────────────────────────────────────────────────
// arch §3.1 列為 sw-proto 常數、但 P3／P5 尚未定義者（數值照 `sw_core/constants.py`、`uart_io.py`、`session_manager.py`）。
/// soft preempt／ATTACHED 救援 lease 上限秒數（`sw_core/constants.py::MAX_RECOVERY_LEASE_S`）。
pub const MAX_RECOVERY_LEASE_S: f64 = 120.0;
/// bootloader prompt 判定取用的 RX 尾端位元組數（`sw_core/constants.py::BOOTLOADER_RX_TAIL_BYTES`）。
pub const BOOTLOADER_RX_TAIL_BYTES: usize = 512;
/// human「近期活躍」視窗（`sw_core/constants.py::HUMAN_ACTIVE_WINDOW_S`）。
pub const HUMAN_ACTIVE_WINDOW_S: f64 = 60.0;
/// human lease peer-loss grace（`sw_core/constants.py::_HUMAN_PEER_GRACE_S`；07 D-09 hub 掃描週期）。
pub const HUMAN_PEER_GRACE_S: f64 = 3.0;
/// 無 peer 的 PTY console 被 reap 前的 grace（`uart_io.py::_STALE_CONSOLE_GRACE_S`）。
pub const STALE_CONSOLE_GRACE_S: f64 = 2.0;
/// ATTACHED 狀態 human console lease 的 timeout（`session_manager.py::_ATTACHED_CONSOLE_LEASE_TIMEOUT_S`）。
pub const ATTACHED_CONSOLE_LEASE_TIMEOUT_S: f64 = 86400.0;
/// DeferredBuffer 每 client 上限（bytes；BC-02 超過時刪除該 client 整段，07 D-07；`sw_core/constants.py`）。
/// P6 Task 1 自 sw-core `resources` 移入（比照 `CMD_HISTORY_MAX` 先例），sw-core 以 `pub use` 再匯出。
pub const DEFERRED_INPUT_MAX_BYTES: usize = 262_144;
/// 每 session interactive tombstone 上限（FIFO；arch §5.10）。sw-core `resources::INTERACTIVE_TOMBSTONES_MAX` 為其再匯出。
pub const INTERACTIVE_TOMBSTONE_MAX: usize = 16;

// 07 Task 1 Produces（值即契約；`contracts/resource_limits.yaml` 07 D-18 列的 Rust 鏡像）。
/// line editor 緩衝上限（SC-16、07 D-06）。
pub const CONSOLE_LINE_BUFFER_MAX: usize = 16385;
/// 每 console 出向 ring 的 Display 上限（SC-15、07 D-05）。
pub const CONSOLE_OUT_RING_BYTES: usize = 1_048_576;
/// 每 console 尚未寫出的 Control（greeting、協商回覆）上限；超過即關閉該 console（07 D-05、SC-31(c)）。
pub const CONSOLE_CONTROL_MAX_BYTES: usize = 65_536;
/// reader→hub 顯示佇列（chunk 數，07 D-18）。
pub const CONSOLE_HUB_RX_QUEUE: usize = 256;
/// actor→hub 控制佇列（07 D-18）。
pub const CONSOLE_HUB_CTRL_QUEUE: usize = 64;
/// hub→mailbox 暫存（07 D-17）。
pub const CONSOLE_HUB_PENDING_EVENTS: usize = 64;
/// hub 每輪每 channel 最多處理筆數（07 D-17）。
pub const CONSOLE_HUB_LOOP_QUOTA: usize = 64;
/// 每 session Telnet console 上限（07 D-18、SC-31(d)）。
pub const TELNET_CONSOLES_PER_SESSION: usize = 32;
/// human TX lane backlog 上限（07 D-15）。
pub const HUMAN_TX_BACKLOG_MAX_BYTES: usize = 262_144;
/// com0com reader→mux 有界 channel（07 D-04、D-18；PAL 不依賴 sw-proto，實作端另以相同值定義並由測試比對）。
pub const COM0COM_READER_CHANNEL: usize = 64;
/// `session.interactive_open` 預設 timeout（`service.py:843`）。
pub const DEFAULT_INTERACTIVE_TIMEOUT_S: f64 = 60.0;
/// `session.interactive_status` 預設 screen_chars（`service.py:860`）。
pub const DEFAULT_SCREEN_CHARS: i128 = 2048;
/// human console line 命令的 timeout（`service.py:313-322`）。
pub const HUMAN_CONSOLE_CMD_TIMEOUT_S: f64 = 30.0;
/// human console line 命令的 priority（與 `SubmitReq.priority: i128` 同型別）。
pub const HUMAN_CONSOLE_CMD_PRIORITY: i128 = 100;
/// 視為互動式全螢幕程式的命令 basename（`service.py::_HUMAN_INTERACTIVE_COMMANDS`，維持字母序）。
pub const HUMAN_INTERACTIVE_COMMANDS: [&str; 18] = [
    "alsamixer", "btop", "htop", "less", "menuconfig", "more", "most", "nano", "nmtui", "screen", "tig", "tmux",
    "top", "vi", "view", "vim", "vimdiff", "watch",
];
/// `sudo` 後會吃掉下一個 token 的選項（`service.py::_human_console_mode`）。
pub const SUDO_VALUE_OPTS: [&str; 8] = ["-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t"];
/// `interactive_send` 的 `encoding=key` 對照表（`session_manager.py::_encode_interactive_payload` 的 `key_map`）。
pub const INTERACTIVE_KEY_MAP: [(&str, &[u8]); 9] = [
    ("enter", b"\n"),
    ("tab", b"\t"),
    ("escape", b"\x1b"),
    ("ctrl-c", b"\x03"),
    ("ctrl-d", b"\x04"),
    ("up", b"\x1b[A"),
    ("down", b"\x1b[B"),
    ("right", b"\x1b[C"),
    ("left", b"\x1b[D"),
];
/// 哨兵 primary console 的 label。
pub const CONSOLE_PRIMARY_LABEL: &str = "primary";
/// console `client_id` 的 hex 長度（`uuid4().hex[:12]`）。
pub const CONSOLE_CLIENT_ID_HEX: usize = 12;

/// Telnet 常數（`sw_core/telnet_console.py`；上游未定義，本 Phase 唯一定義處）。
pub mod telnet {
    pub const IAC: u8 = 0xFF;
    pub const SE: u8 = 0xF0;
    pub const SB: u8 = 0xFA;
    pub const WILL: u8 = 0xFB;
    pub const WONT: u8 = 0xFC;
    pub const DO: u8 = 0xFD;
    pub const DONT: u8 = 0xFE;
    pub const OPT_BINARY: u8 = 0x00;
    pub const OPT_ECHO: u8 = 0x01;
    pub const OPT_SGA: u8 = 0x03;
    /// `IAC WILL ECHO, IAC WILL SGA, IAC DO SGA, IAC WILL BINARY`（12 bytes）。
    pub const TELNET_GREETING: [u8; 12] = [0xFF, 0xFB, 0x01, 0xFF, 0xFB, 0x03, 0xFF, 0xFD, 0x03, 0xFF, 0xFB, 0x00];
}
