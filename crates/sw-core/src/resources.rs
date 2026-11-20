//! 有界資源常數（Slice 2.1 Task 0／Task 11；`contracts/resource_limits.yaml` 的 Rust 鏡像）。
//!
//! 數值改動等同改契約：須同步 `contracts/resource_limits.yaml` 與 `tests/test_slice21_scope.py::test_resource_limits_contract_values`。
//! `CMD_PENDING_MAX`／`DEFAULT_WAL_ROTATE_BYTES`／`CMD_HISTORY_MAX` 位於 `sw_proto::constants`（P5-T0 補記，見該檔）；本檔不重複定義，`CMD_HISTORY_MAX` 僅 `pub use` 再匯出。
//! TX 帳本上限位於 `tx_writer`（`TX_LEDGER_*`、`TX_OP_WAL_GAPS_MAX`、`TX_SETTLE_QUEUE_MAX`）。

/// Actor control lane 容量（訊息數）；RPC 送端逾時回 `timeout`（`block_sender`）。
pub const MAILBOX_CONTROL_CAP: usize = 256;
/// Actor data lane 容量（chunk 數）。
pub const MAILBOX_DATA_CHUNKS_CAP: usize = 1024;
/// 單一 data chunk 上限（bytes）。
pub const MAILBOX_DATA_CHUNK_BYTES: usize = 8192;
/// Mailbox stash 上限（訊息數）。
pub const MAILBOX_STASH_MAX: usize = 16;
/// TxWriter job queue 容量。
pub const TX_WRITER_JOBS_CAP: usize = 64;
/// 命令 history 上限（淘汰最舊已完成者）；定義於 `sw_proto::constants`，此處僅再匯出。
pub use sw_proto::constants::CMD_HISTORY_MAX;
/// BackgroundCapture 數量上限（淘汰已結束者）。
pub const BG_CAPTURE_MAX_COUNT: usize = 64;
/// BackgroundCapture 單筆字元上限（截頭，至少留 1 段）。
pub const BG_CAPTURE_MAX_CHARS: usize = 4_194_304;
/// RxWindow 字元上限（截頭，`_rx_trimmed` 累計）。
pub const RX_WINDOW_MAX_CHARS: usize = 131_072;
/// RX 速率視窗項目上限（溢位合併最舊兩筆）。
pub const RX_RATE_WINDOW_ENTRIES: usize = 4096;
/// DeferredBuffer 每 client 上限（bytes；整段清空，BC-02）。
/// P6 Task 1：定義移至 `sw_proto::constants`（比照 `CMD_HISTORY_MAX`），此處只再匯出。
pub use sw_proto::constants::DEFERRED_INPUT_MAX_BYTES;
/// RPC 連線上限（超過時 acceptor 暫停 accept）。
pub const RPC_CONNECTIONS_MAX: usize = 128;
/// RPC 請求行上限（bytes；超過斷線不回應，RPC-H-02）。
pub const RPC_REQUEST_LINE_BYTES: usize = 65_536;
/// 每 session interactive tombstone 上限（FIFO）。
/// P6 Task 1：定義移至 `sw_proto::constants::INTERACTIVE_TOMBSTONE_MAX`，此處以 06 名稱再匯出。
pub use sw_proto::constants::INTERACTIVE_TOMBSTONE_MAX as INTERACTIVE_TOMBSTONES_MAX;
/// Two-Phase Drain 完成時限（毫秒；逾時 → RECOVERING＋drain_stalled，SC-04）。
pub const DRAIN_TIMEOUT_MS: u64 = 3000;
