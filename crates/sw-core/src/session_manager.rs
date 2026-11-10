//! P5-T16：`SessionManager`（裝置、rank #100、動態 session、selector、管理操作）。
//!
//! Python 對照：`sw_core/session_manager.py`（`SessionManager.__init__`、`prepare_dynamic_rank`、`_session_from_template`、
//! `update_devices`、`_spawn_attach`、`_attach_by_id`、`_attach_by_id_dynamic`、`_reresolve_profile_on_reattach`、
//! `_rematerialize_profile_locked`、`bind_session`／`pin_session`／`clear_session`、alias 管理、`shutdown_drain`）。
//!
//! 架構（plan 06 Task 16 Step 3）：
//! - 每個 session 一條 `SessionActor`（`SessionCore` sans-IO 狀態機），效果由本模組的 `ManagerExec` 執行
//!   （開埠、IoWorker、登入 FSM、state.json 寫入、WAL META、通知）。
//! - manager 本身的可變狀態（session 鏡像、裝置快照、rank 預配、inflight attach、stopping gate、interactive 索引）
//!   集中在 `Inner`，以單一 `Mutex` 保護；**持 `Inner` 鎖時不等待任何 actor 回覆**（executor 的開埠 gate 也取此鎖）。
//! - 全域 thread `serialwrap-session-manager` 依序處理 `WatcherEvent`（Delta → `update_devices`、Tick → `on_tick`）與
//!   actor 的 `ManagerNote`（READY 後 sticky 持久化、drain 後移除 interactive 索引）。
//! - 動態裝置（尚無 session）與需要再解析 profile 的既有 session（#181）由 per-by_id attach worker
//!   （`serialwrap-attach-<by_id>`）處理：**先獨占開埠一次**，PROBE 偵測直接在該 handle 上做，命中後把同一 handle
//!   原樣交給正式 bridge（Python P1 T10 凍結「PROBE 與正式 bridge 共用同一預開 port」，parity #51）。

use std::collections::{BTreeMap, HashMap, HashSet};
use std::hash::{BuildHasher, Hasher};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, Sender};
use indexmap::IndexMap;
use sw_pal::Capabilities;
use sw_pal::traits::{
    CancelToken, Clock, CloseReceipt, CloseToken, ConsoleTransportFactory, DeviceInfo, DevicePath,
    FsOps, IoWaker, OpenError, PalError, PalErrorKind, PalResult, ReadOutcome, SerialBackend,
    SerialConfig, SerialHandle, SerialReader, SerialWriter, SubmitGate, SysEnv, TxOutcome, TxResult,
    UartExclusive, WatcherEvent,
};
use sw_proto::config_types::{LoadResult, ProfileTemplate, SessionProfile, UartProfile};
use sw_proto::pycompat::{PyErr, clean_text, iso_utc_now};
use sw_proto::pydict;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{PyFlags, compile};

use crate::actor::{ActorHandle, EffectExecutor, spawn_actor};
use crate::alias_registry::AliasRegistry;
use crate::arbiter::CommandRegistry;
use crate::generations::EpochAllocator;
use crate::mailbox::{ControlEvent, DataEvent, DetachReason, Mailbox, OpenFailure, OpenedBridge};
use crate::op_runner::{OpCtx, OpOutcome, OpRunner, OpSpec};
use crate::resources::{
    DRAIN_TIMEOUT_MS, MAILBOX_CONTROL_CAP, MAILBOX_DATA_CHUNKS_CAP, RX_WINDOW_MAX_CHARS,
};
#[cfg(feature = "test-fakes")]
use crate::session_core::SessionState;
use crate::session_core::{
    CoreDeps, Effect, ManagerNote, OpGrant, SessionCore, SessionRequest, UnconfirmedCloses,
};
use crate::state_store::{
    lone_surrogate_encode_error, serialize_state, LoadedState, ReleasedRow, StateDelta, StateModel, StateStore,
};
use crate::tx_writer::{Fence, HandoffFreeze, OpId, TxClass, TxJob, TxPermit, TxWriterHandle, spawn_tx_writer};
use crate::self_test::SelfTestProbes;
use crate::uart_bridge::{IoWorker, IoWorkerArgs, RxShared};
use crate::wal::{WalAppendReq, WalHealthFanout, WalWriter};

/// P8 Task 6：`file.push`／`file.pull` 的回覆等待上限（arch §6.3 的 600 s）。shutdown 時 drain Phase 1 取消傳輸等待，
/// 引擎盡快返回，不以此上限阻塞停止序。
const FILE_TRANSFER_REPLY_TIMEOUT: Duration = Duration::from_secs(600);

/// P8 Task 9：`SessionManager::send_control` 對 control lane 的送出等待上限（lane 有界；滿時不無限阻塞 flash 路徑）。
const FLASH_CONTROL_SEND_WAIT: Duration = Duration::from_secs(2);

/// P8 Task 8：flash 偵測候選（`collect_flash_candidates_and_mark` 的輸出；Task 9 `detect_mcu_line` 消費）。
/// 欄位對照 Python 候選 dict `{com, by_id, real_path, command_capable}`；`real_path` 查無裝置時為 `None`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlashCandidate {
    pub com: String,
    pub by_id: Option<String>,
    pub real_path: Option<String>,
    pub command_capable: bool,
}

/// P8 Task 12：drain Phase 1 已送出、等待回條的集合（[`SessionManager::shutdown_phase1`] 的結果，交給
/// [`SessionManager::shutdown_phase2`]）；`deadline` 為步驟 3～5 共用的期限。
pub struct ShutdownDrain {
    deadline: Instant,
    waits: Vec<(String, Option<Receiver<Result<PyDict, PyErr>>>)>,
    inflight: Vec<String>,
}

/// `SessionManager::new` 的注入依賴（plan 06 Task 16 Produces）。
pub struct ManagerDeps {
    pub serial: Arc<dyn SerialBackend>,
    pub uart_excl: Arc<dyn UartExclusive>,
    pub console: Arc<dyn ConsoleTransportFactory>,
    pub caps: Capabilities,
    pub wal: Arc<WalWriter>,
    pub registry: Arc<CommandRegistry>,
    pub state: StateStore,
    pub loaded: LoadedState,
    pub load: LoadResult,
    pub clock: Arc<dyn Clock>,
    pub env: Arc<dyn SysEnv>,
    pub fs: Arc<dyn FsOps>,
    pub warn: Arc<dyn Fn(String) + Send + Sync>,
}

/// 動態 COM 排序鍵（#100）：`(by_id, by_path 或空字串)`。`DeviceInfo` 無 by_path，故第二欄恆空（與 Python 同）。
pub fn device_sort_key(by_id: &str, by_path: Option<&str>) -> (String, String) {
    (by_id.to_string(), by_path.unwrap_or("").to_string())
}

/// actor 請求的一般等待上限（manager 內部查詢）。
const QUERY_TIMEOUT: Duration = Duration::from_secs(5);
/// 動態偵測的 probe 等待（`detect_template(probe_timeout_s=3.0)`）。
const DETECT_PROBE: Duration = Duration::from_secs(3);
/// 既有 attach op 取消後的等待上限（Python `bind_session` 的 `DRAIN_TIMEOUT_MS / 1000`，#26）。
const ATTACH_OP_WAIT: Duration = Duration::from_millis(DRAIN_TIMEOUT_MS);
/// manager thread 單次等待事件的上限（期間持有事件處理鎖）。
const PUMP_POLL: Duration = Duration::from_millis(20);
/// 偵測 PROBE 的 WAL com。
const PROBE_COM: &str = "PROBE";

/// 預開（PROBE 用過、尚未交給正式 bridge）的 UART handle。
struct Preopened {
    real_path: String,
    parts: HandleParts,
}

/// `SerialHandle::split` 的四件組；可原樣重組成 `SerialHandle` 交給 `IoWorker::start`（同一 OS handle）。
pub struct HandleParts {
    pub reader: Box<dyn SerialReader>,
    pub writer: Box<dyn SerialWriter>,
    pub waker: Arc<dyn IoWaker>,
    pub close: CloseToken,
}

impl HandleParts {
    pub fn split(handle: Box<dyn SerialHandle>) -> HandleParts {
        let (reader, writer, waker, close) = handle.split();
        HandleParts {
            reader,
            writer,
            waker,
            close,
        }
    }

    /// 關閉（未交給正式 bridge 時；回條供呼叫端判斷）。
    pub fn close(self) -> CloseReceipt {
        self.close.close(self.reader, self.writer)
    }
}

/// `CloseReceipt` 失敗時的錯誤文字（Python `_finish_attach_op` 告警中的 `exc`，`session_manager.py:4326-4331`）。
fn close_error_text(receipt: &CloseReceipt) -> String {
    receipt
        .error
        .as_ref()
        .map(|error| error.py_message.clone())
        .unwrap_or_else(|| "close 回條為 closed:false 且未附錯誤".to_string())
}

/// Python `_finish_attach_op` 的預開 handle 關閉失敗告警（`session_manager.py:4326-4331`）。
fn preopened_close_warning(by_id: &str, error: &str) -> String {
    format!("預開 UART handle 關閉失敗（by_id={by_id}）：{error}")
}

/// 佔位交接回應以退休前快照改寫狀態時，比照 Python state setter（`origin/main:sw_core/session_manager.py:617-620`）：
/// 狀態改變即寫入 `last_state_change_at`。
fn set_public_state(public: &mut PyDict, state: &str, changed_at: String) {
    if public.get("state").and_then(PyValue::as_str) != Some(state) {
        public.insert("last_state_change_at", PyValue::from(changed_at));
    }
    public.insert("state", PyValue::from(state));
}

/// 同上，比照 Python last_error setter（`:603-610`）：值改變即清除 `last_error_detail`。
fn set_public_last_error(public: &mut PyDict, value: PyValue) {
    if public.get("last_error") != Some(&value) {
        public.insert("last_error_detail", PyValue::Null);
    }
    public.insert("last_error", value);
}

/// 重組的 handle：`split()` 直接交回原四件組（PROBE 與正式 bridge 共用同一預開 port，#51）。
struct ReassembledHandle(HandleParts);

impl SerialHandle for ReassembledHandle {
    fn split(
        self: Box<Self>,
    ) -> (
        Box<dyn SerialReader>,
        Box<dyn SerialWriter>,
        Arc<dyn IoWaker>,
        CloseToken,
    ) {
        let parts = self.0;
        (parts.reader, parts.writer, parts.waker, parts.close)
    }

    fn set_baud(&self, _baud: u32) -> PalResult<()> {
        Ok(())
    }

    fn is_alive(&self) -> bool {
        self.0.close.is_alive()
    }
}

/// actor executor 與 manager 共享的 per-session I/O 狀態。
struct SessionIo {
    sid: Mutex<String>,
    profile: Mutex<SessionProfile>,
    preopened: Mutex<Option<Preopened>>,
    /// P6 Task 10：RPC thread 預建、等待 core `ConsoleAdopt`／`ConsoleDiscard` 的 PTY（token → PtyPair）。
    pending_pty: Mutex<HashMap<u64, sw_pal::PtyPair>>,
    next_pty_token: AtomicU64,
}

/// manager 的 session 鏡像（selector 解析、rank、管理操作用；狀態本身以 actor 為準）。
struct Entry {
    sid: String,
    profile: SessionProfile,
    profile_source: String,
    /// OD-63：原 template 已不存在的 RELEASED session 佔位；不對外公開、不寫入 state.json。
    #[cfg_attr(not(feature = "test-fakes"), allow(dead_code))]
    released_placeholder: bool,
    actor: Arc<ActorHandle>,
    /// 與 actor 共用的 Fence（P8 Task 8：manager 於 `Inner` 鎖內寫入 `flash_critical`）。
    fence: Arc<Fence>,
    io: Arc<SessionIo>,
    /// 與 executor 共用的各 epoch IoWorker（P8 Task 9：`tx_for`／`line_control_for` 以 Fence 的 `active_epoch` 取值）。
    workers: Arc<Mutex<HashMap<u64, IoWorker>>>,
    #[cfg(feature = "test-fakes")]
    history: Arc<Mutex<Vec<String>>>,
}

static NEXT_ATTACH_OP_TOKEN: AtomicU64 = AtomicU64::new(1);

/// Python `sw_core/session_manager.py:412-429` 的 `_AttachOp` 對照：
///
/// | `token/session_id/by_id/bridge_generation/real_path`；建立 `:3495-3503` | `token` 在此；by_id 是 `Inner::inflight` 的鍵，session／generation／real path 由呼叫端與快照持有。`attach_released_placeholder` 登錄作業後才開埠。 | `wp510_b_concurrent_placeholder_attach_returns_session_recovering` |
/// | `port/port_lock`；開埠與存入 op `:3504-3509` | `preopened` mutex／`HandleParts` 保存同步開埠的 handle；入口 `attach_released_placeholder` → `retire_released_placeholder_locked` → `spawn_attach_worker` → `attach_dynamic`。 | `wp510_rpc_bind_then_device_attach_replaces_placeholder` |
/// | 開埠完成與 `port_transferred`；有效性檢查 `:3572-3615`、正式交接 `:4374-4383` | 開埠回來後重驗 stopping、取消、by_id 與 real path；有效則把 handle 移交動態 session，之後由正式 bridge 擁有。 | `wp510_rpc_bind_then_device_attach_replaces_placeholder` |
/// | 晚到開埠；開埠返回後失效及 `_finish_attach_op` `:3572-3615`、`:4320-4333` | 取消／裝置改指／shutdown 後才返回的 handle 不送 TX，嘗試 close；成功才清 op，`closed:false` 留住 inflight。 | `wp510_b_handoff_invalidated_during_open_closes_handle`; `wp510_lifecycle_shutdown_cancel_fail_close_keeps_placeholder_blocked`; `wp510_lifecycle_device_path_change_cancel_fail_close_keeps_placeholder_blocked` |
/// | `cancelled`；裝置改指 `:4031-4043`、bind `:3683-3691`、shutdown `:1175-1190`、release `:3117-3125` | 共用原子取消旗標；各取消者都阻止晚到 handle 交接，並由同一 close 回條決定是否可清除 op。 | `wp510_b_handoff_invalidated_during_open_closes_handle`; `shutdown_includes_inflight_attach`; `wp510_review_release_cancels_placeholder_attach_before_retirement`; `bind_with_inflight_attach_op_cancels_waits_3000ms` |
/// | `done`；交接完成 `:4374-4383`、作業收尾 `:4320-4365`，release 等待 `:3192-3204` | 沒有獨立 done 欄位；`inflight` 項目存活代表未完成，條件變數喚醒等待者。close 成功後移除 op，close 失敗不會誤報完成。 | `wp510_review_late_placeholder_attach_completion_clears_drain_stall`; `wp510_rpc_failed_close_keeps_placeholder_drain_stalled` |
/// | `probe_bridge`；註冊 `:4367-4372`，取消排空：shutdown `:1186-1193`、release `:3126-3131`、bind `:3686-3691`、裝置改指 `:4039-4043` | 不另建 PROBE bridge；`attach_dynamic`／`reattach_existing` 以同一預開 handle 偵測。PROBE 的 TX gate 是作業持有的 `probe_fence`／`probe_cancel`：所有取消者經 `AttachOp::cancel()` 在 fence 鎖內凍結並取消（`uart_io.py:908-918`），已越過 `still_valid()` 的 PROBE 於提交點重驗即拒絕（`:623-627`）、零 TX。 | `wp510_rpc_bind_then_device_attach_replaces_placeholder`; `wp510_tx_shutdown_after_still_valid_before_placeholder_probe_submit_sends_zero_tx`; `wp510_tx_device_path_change_before_hotplug_probe_submit_sends_zero_tx`; `wp510_rpc_shutdown_before_placeholder_probe_submit_sends_zero_tx` |
/// | `release_request/release_deadline`；開始計時 `:3084-3086`、第一次設定及重用 `:3117-3125`，等待與逾期處理 `:3192-3204` | 第一個取得 op 的 release 把 `(source, reason)` 寫入 `release_request`（佔位已是 RELEASED，reason 沿用 session 既有值）；進 manager lock 前記錄候選期限，第一個取得 op 的 release 寫入共享欄位。重複／並行 release 重用第一次期限，逾期即回 stalled。 | `wp510_review_release_cancels_placeholder_attach_before_retirement`; `wp510_lifecycle_repeated_release_uses_expired_attach_deadline`; `wp510_lifecycle_concurrent_releases_share_first_attach_deadline`; `wp510_review4_release_cancel_fills_null_release_metadata_and_persists` |
/// | `bind_requested`；設定與等待 `:3683-3697` | 無獨立欄位；一般 bind 以取消旗標和共用 inflight 排空。OD-63 佔位 bind 依裁決早退，不能取消或改動作業。 | `bind_with_inflight_attach_op_cancels_waits_3000ms`; `wp510_b_bind_placeholder_precedes_inflight_cancellation` |
/// | close 成功／失敗；`_finish_attach_op` `:4320-4365` | close 成功才移除 inflight；任一取消原因的 `closed:false` 都保留阻擋並以 by_id 與 `CloseReceipt.error` 告警（`:4322-4332`），release 後續維持 `DRAIN_STALLED`。release 收尾由 actor 的 `PlaceholderAttachSettled` 補齊 `released_by`／`released_at` 並持久化（`:4341-4363`），失敗告警後由 release 重試（`:3206-3209`）。 | `wp510_review_late_placeholder_attach_completion_clears_drain_stall`; `wp510_rpc_failed_close_keeps_placeholder_drain_stalled`; `wp510_lifecycle_shutdown_cancel_fail_close_keeps_placeholder_blocked`; `wp510_lifecycle_device_path_change_cancel_fail_close_keeps_placeholder_blocked`; `wp510_review4_failed_preopened_close_warns_with_by_id_and_error`; `wp510_review4_release_cancel_persist_enospc_fails_release_then_retries` |
/// | shutdown 取消及排空 `:1175-1239` | shutdown 取消作業並等待 `Inner::inflight` 清除；仍有 close:false 的作業回報未 drained。末段比照 `:1231-1239` 重新收集排空期間才保留的作業。 | `shutdown_includes_inflight_attach`; `wp510_lifecycle_shutdown_cancel_fail_close_keeps_placeholder_blocked`; `wp510_review5_shutdown_before_bridge_takes_preopened_fail_close_reports_stalled` |
/// | 執行緒建立；`_spawn_attach` `:4147-4150`，佔位交接同步呼叫 `:3633-3638` | `prepare_attach_worker` 先建立、放行前不碰作業；佔位交接在開埠與退休前取得 worker，`Builder::spawn()` 失敗即告警並回 RPC 錯誤（`can't start new thread`），佔位與持久化阻擋不變。自動 attach 無 RPC，告警後收尾作業。 | `wp510_review5_handoff_worker_spawn_failure_returns_error_and_keeps_placeholder`; `wp510_review5_oserror_handoff_worker_spawn_failure_returns_error_and_keeps_placeholder` |
///
/// 生命週期：建立 → 開埠完成並交接，或取消後晚到並關閉；關閉成功才移除作業，`closed:false` 一律保留作業。release 找到同一作業後取消並等待首次 deadline；期限到期回 `DRAIN_STALLED`。shutdown 取消並排空同一 inflight 集合。
/// Rust 用作業 token 與 actor 的 `PlaceholderAttachStalled/Settled` 保存 stall 診斷，對應 Python 在 manager lock 下設定 `drain_stalled` 與 `drain_timeout_epoch`。
///
/// TX 來源盤點（wp510 第三次整包審查 High）：佔位交接期間與交接後、進入正式 bridge 前，可能送出 TX 的來源與其取消權：
///
/// | TX 來源 | Fence／CancelToken 來源 | shutdown／release／bind／裝置改指時是否撤銷 |
/// |---|---|---|
/// | 動態偵測 PROBE 的 `\n`（`attach_dynamic` → `detect_template` → `probe_send`） | 作業的 `probe_fence`、`probe_cancel`（`AttachOp::new` 配置，`probe_send` 登錄 token） | 是：四個取消者皆呼叫 `AttachOp::cancel()`，凍結 fence 並取消 token；提交點以 `Frozen` 拒絕。 |
/// | fallback 再解析 PROBE 的 `\n`（`reattach_existing` → `detect_template`） | 同上（同一 attach 作業） | 是（同上）。 |
/// | ready probe／login／`post_login_cmd`（正式 bridge 的 `start_login`） | session `Entry.fence`（`create_session_locked`），TxWriter 的 cancel 屬 `IoWorker` | 正式 bridge 才有；`open_bridge` 在取預開 handle 前後各檢查 stopping，stopping 時關閉 handle、零 TX；開埠後由 actor 的 `Shutdown`／`DeviceDetached`／`Release` drain 凍結 session fence。不屬交接窗口。 |
/// | human console 回放與按鍵（`human_tx`） | session `Entry.fence`；需該 epoch 的 IoWorker（`active_epoch`） | 正式 bridge 前沒有 worker，`human_tx` 回 `NotSubmitted`；之後同 session drain。 |
/// | 事件引擎 | 不送 UART TX（`event_engine` 無 `TxJob` 提交點） | 不適用。 |
/// | DETACHED-rebind（`rebind_detached` → `DeviceAttached`） | 經 actor 正式 bridge（session fence） | 佔位交接為 `dynamic_only`，不走此路徑。 |
///
/// UART handle 關閉回條盤點（wp510 第五次整包審查 High）：佔位 attach、交接到正式 bridge 接手之間，所有可能持有
/// handle 的位置與 `closed:false` 的處理。原則比照 Python：作業仍在表內時 `_finish_attach_op` 關閉失敗即保留作業、
/// 不設 `done`（`origin/main:sw_core/session_manager.py:4322-4332`），shutdown 列為 stalled（`:1231-1239`），release
/// 等待 `done` 逾期回 DRAIN_STALLED（`:3192-3204`）；後續一般開埠錯誤不得清除 stall 而回成功。
///
/// | 持有位置 | `closed:false` 的處理 | shutdown 排空判定 | release 排空判定 |
/// |---|---|---|---|
/// | ① 佔位 `device.attach` 開埠在途（作業已登錄 `inflight`，`open_exclusive` 尚未返回） | 返回後失效：`AttachOp::close_parts` 記 `close_failed`，`finish_attach_op` 保留作業 | `attach:<by_id>` 為 false | 佔位 release 等同一作業至期限回 DRAIN_STALLED |
/// | ② 退休後、worker 接手前（`AttachOp::preopened`） | worker 收尾經 `close_preopened_with_receipt` → `finish_attach_op` 保留；退休落盤失敗時作業以 `parked` 留著、不關閉（Python `_preopened_ports`），shutdown 收尾關閉並依回條保留 | 同上 | 舊佔位已退休、正式 session 尚未建立；作業留在 `inflight`，parked 者由之後的 attach／bind 接手（於同一臨界區移入交接表）或拔除時關閉；已於記憶體改綁到該 by_id 的 session 經 release 取出並關閉（Python `:3108-3111`）；以該 by_id 為舊綁定的 bind 重試時由 `close_parked_for_bind` 關閉。這三處 Python 都先取出作業：關閉未確認只告警、不保留、不登記阻擋（wp510 第 14 項） |
/// | ③ attach worker 偵測中（`attach_dynamic`／`reattach_existing` 的區域 `HandleParts`） | 一律經 `AttachOp::close_parts`，worker 結束時 `finish_attach_op` 保留 | 同上 | 一般 session 的 release 遇 `close_failed` 作業回 DRAIN_STALLED |
/// | ④ 交給 session、正式 bridge 接手前（`SessionIo::preopened`，由交接表 `Inner::handoff` 追蹤），`open_bridge` 於 stopping、configure 失敗、路徑不符時關閉；取消者（release、bind、shutdown、裝置改指）在 actor 接手前認領交接列，handle 移入在途表的關閉中作業後代為關閉（`close_claimed_handoff`） | `close_before_bridge`／`close_claimed_handoff` 告警並 `retain_unconfirmed_close`（作業留在 `inflight`）；`open_bridge` 回 `OpenFailure::CloseUnconfirmed`，actor 的 pending stop 保持 stalled（`pending_open_close_unconfirmed`），不交 `finish_pending_open_stop` | 末段重收 `attach:<by_id>` 為 false；session 的 shutdown 等待者不回覆亦為 false | pending release 回 DRAIN_STALLED；release 代為關閉者等待保留作業至期限回 DRAIN_STALLED；之後的 release 經 `close_failed` 作業回 DRAIN_STALLED |
/// | ⑤ `open_bridge` 開埠返回時已 stopping（重組的預開或新開 handle） | 同 ④ | 同 ④ | 同 ④ |
/// | ⑥ 正式 bridge 接手後（`IoWorker` 擁有） | `WorkerStoppedReceipt.handle_closed:false` → stop 標 stalled（`worker_stopped`） | session 為 false | DRAIN_STALLED |
/// | ⑥' 開埠成功但 actor 不接手而退休的 bridge（`retire_opened_bridge`；例如 bind 等待已取走的交接列逾時設 RECOVERING 之後開埠才成功） | 退休 bridge 的回條 `handle_closed:false` → 關閉中作業保留（`close_failed`）並登記未確認關閉（`retired_open_close_receipt`） | 不列入（同 ⑥ 以外的退休 epoch） | release 等配對且 `handle_closed:true` 的回條才完成 RELEASED；`false` 回 DRAIN_STALLED，之後的 release 至期限回 DRAIN_STALLED |
/// | ⑦ parked 作業被拔除／改指（`update_devices`）、bind 收尾後的殘留 `SessionIo::preopened` | `close_session_preopened` 只告警：Python 先自 `_preopened_ports` 取出作業再關閉（`:4045-4063`、`:3776-3800`），失敗不保留 | 不列入（同 Python） | 不列入（同 Python） |
/// | ⑧ PROBE bridge | Rust 不另建 PROBE bridge，PROBE 直接用 ③ 的同一 handle | 見 ③ | 見 ③ |
///
/// 回歸：`wp510_review5_shutdown_before_bridge_takes_preopened_fail_close_reports_stalled`（④ shutdown）、
/// `wp510_review5_release_waiting_bridge_open_fail_close_reports_drain_stalled`（④⑤ release）、
/// `wp510_review5_configure_failure_fail_close_keeps_handle_in_drain`（④ configure 失敗，無 stop 在途）、
/// `wp510_review6_finish_attach_op_rereads_close_failed_under_lock`（④ 與 attach worker 收尾交錯：`finish_attach_op`
/// 於 manager 鎖內重讀 `close_failed`，不以取鎖前的快照移除已被 `retain_unconfirmed_close` 標記的作業）、
/// `wp510_review12_rpc_release_waits_retired_bridge_close_after_bind_stall`、
/// `wp510_review12_rpc_release_retired_bridge_close_failure_reports_drain_stalled`（⑥'，sw-daemon；sw-core 另有
/// `wp510_review12_*` 六案涵蓋關閉回條先到、裝置改指後新開埠失敗／成功、RELEASED 上 device.attach 落盤失敗）。
///
/// 未確認關閉的全域阻擋（wp510 第 13 項）：上表各位置的 `closed:false` 一律登記到 `UnconfirmedCloses`（Python
/// `_draining_by_ids`，`origin/main:sw_core/session_manager.py:1286-1348`；安全回條才移除 `:1346-1368`，Rust 無遲到回條，
/// 登記後持續到 daemon 結束）。登記來源：
///
/// | 來源 | Rust 登記點 | Python 對應 |
/// |---|---|---|
/// | ①②③ attach 作業關閉未確認 | `AttachOp::mark_close_failed`（`close_parts`、`finish_attach_op` 帶入的回條） | `_finish_attach_op` 關閉失敗保留作業 `:4320-4332` |
/// | ④⑤ 正式 bridge 接手前關閉未確認（含取消者代為關閉交接列的 handle） | `close_before_bridge`／`close_claimed_handoff` → `retain_unconfirmed_close` | `bridge.start()` 失敗經 `_track_attach_bridge_stop` 納入 `_draining_by_ids` `:5185-5207`、`:1286-1324`；`_attach_by_id` 中止時 `_finish_attach_op` 關閉失敗，作業留在 `_active_attach_ops` `:4322-4332` |
/// | ⑥ 正式 bridge 回條 `handle_closed:false` | actor `worker_stopped` | session drain 留在 `_draining_by_ids` `:2774-2775`、`:2918-2919` |
/// | ⑥' 退休 bridge 回條 `handle_closed:false` | actor `retired_open_close_receipt` | 被取消的作業 `_finish_attach_op` 關閉失敗，留在 `_active_attach_ops` `:4322-4332` |
/// | ⑦ 不登記：Python 已先自表取出的作業（parked 作業的 release／bind／拔除，bind 收尾殘留） | `close_session_preopened` 只告警 | 先移出表再關閉 `:3108-3111`、`:3680-3681`、`:3776-3800`、`:4045-4063`；`_finish_attach_op` 只告警 `:4322-4332` |
///
/// 會開啟 UART 的入口與阻擋結果（皆為零開埠嘗試、零 TX）：
///
/// | 入口 | Rust 路徑與檢查點 | Python 對應 | 阻擋結果 | 回歸 |
/// |---|---|---|---|---|
/// | `device.attach`（一般 session） | `Service::rpc` → `route_with_selector` → actor `attach_request`（stopping 之後第一個分支） | `attach_device` `:3410-3417` | SESSION_RECOVERING＋`drain_stalled`／`drain_timeout_epoch`＋session | `wp510_review7_rpc_device_attach_after_unconfirmed_close_returns_session_recovering` |
/// | `device.attach`（OD-63 佔位） | `route_inner` → `attach_released_placeholder`（inflight 或集合） | 同上 | SESSION_RECOVERING | `wp510_b_concurrent_placeholder_attach_returns_session_recovering` |
/// | `session.attach` | actor `attach_request`（無 bridge 時，在途表分流之前） | `attach_session` `:3933-3944`、`:3999-4000` → `_spawn_attach` `:4113-4114` | `{"ok":true}`＋ATTACHING、last_error=None | `wp510_review7_rpc_session_attach_after_unconfirmed_close_does_not_reopen` |
/// | `session.recover`（無 bridge） | actor `recover_request` 的 Reattach 計畫 | `recover_session` `:7970-7976` | REATTACH＋ATTACHING | `wp510_review7_rpc_session_recover_after_unconfirmed_close_does_not_reopen` |
/// | `session.clear`（無 bridge） | actor `request_reattach` → `Effect::Reattach` → `spawn_attach_with` | `clear_session` `:3058-3082` | `{"ok":true}`＋ATTACHING | `wp510_review7_rpc_session_clear_after_unconfirmed_close_does_not_reopen` |
/// | `session.bind`（改綁到該 by_id） | manager `bind` → `DeviceOnline` → actor `device_attached(bind_online)` | `bind_session` `:3789-3812`、`_complete_drain` `:2955-2988` | `{"ok":true}`＋ATTACHING、last_error=None | `wp510_review7_rpc_bind_to_unconfirmed_close_by_id_does_not_reopen` |
/// | hotplug／watcher（added／changed）、bootstrap | `update_devices`／`bootstrap_attach` → `spawn_attach_with` | `update_devices` `:4081-4082`、`bootstrap_attach` → `_spawn_attach` `:4113-4114` | 早退、不改狀態 | `wp510_review7_hotplug_after_unconfirmed_close_does_not_reopen` |
/// | `DeviceAttached`（watcher 對既有 session、`rebind_detached`、`reattach_existing` 的 `send_attached`） | actor `device_attached` | `_attach_by_id` `:4847-4848` | 早退、不改狀態 | `wp510_review7_device_attached_blocked_by_unconfirmed_close` |
/// | attach worker 的 PROBE 開埠（`attach_dynamic`／`reattach_existing`） | `gated_open` 與開埠後的 `still_valid` | `_preopen_dynamic_attach_port` `:4258`、`:4299`；`_attach_by_id_dynamic` `:5523` | 不開埠；開埠後才登記者關閉 handle、不建 session | `wp510_review7_rpc_bind_to_unconfirmed_close_by_id_does_not_reopen`（交接 PROBE 被拔除後的保留） |
/// | reprobe 自動 attach（DETACHED 逾時類） | actor `reprobe_tick` 的 Attach | `_prepare_reprobe_locked` `:2248-2252` → `reconcile_readiness` `:2513-2515` | ATTACHING＋last_error=None（已記 attempt） | `wp510_review7_detached_reprobe_attach_blocked_by_unconfirmed_close` |
/// | drain 收尾再 attach（io_error、裝置改指、開埠中改指） | actor `reopen_known_device` | `_complete_drain` `:2936-2988`、`_spawn_attach` finally `:4126-4145` | io_error→ATTACHING；改指→DETACHED＋DEVICE_REBOUND_REQUIRED | `wp510_review7_io_error_reopen_blocked_by_unconfirmed_close`; `wp510_review7_rebound_reopen_blocked_by_unconfirmed_close` |
/// | 開埠執行（actor 發出 `OpenBridge` 之後才登記） | `OpenCtx::open_bridge`：取預開 handle 前與開埠返回後各檢查一次 | `_attach_by_id` `:4933-4944`；`attach_device` 開埠後重驗 `:3576-3588`、`:3614-3631` | `OpenFailure::CloseBlocked`：device.attach→SESSION_RECOVERING、session.attach→ATTACHING、其餘維持已寫入狀態 | `wp510_review7_open_bridge_gate_rejects_late_unconfirmed_close`; `wp510_review7_open_gate_close_blocked_maps_per_entry` |
///
/// ## 四張表對照（wp510 第 14 項；`origin/main:sw_core/session_manager.py`）
///
/// Python 以四張表追蹤每個預開 handle 從開啟、交接，到正式 bridge 接手或確認關閉的整段生命週期；Rust 以在途表
/// `Inner::inflight`（含 `parked`）、交接表 `Inner::handoff` 與 `UnconfirmedCloses` 對應。handle 離開一張表時一定在
/// 同一個 manager 臨界區內進入下一張表（或已確認關閉），release／shutdown／bind 在任何時點都查得到。
///
/// | Python 表 | 進入 | 移出 | 讀者 | Rust 對應 |
/// |---|---|---|---|---|
/// | `_attach_ops`（開埠在途） | `attach_device` `:3502`；`_preopen_auto_attach_port` `:4191`；`_preopen_dynamic_attach_port` `:4277` | OSError `:3532-3533`；轉 `_preopened_ports` `:3592-3593`；失效 `:3611-3612`；`:4202-4203`、`:4238-4239`、`:4244-4245`；`:4287-4288`、`:4306-4307`、`:4309-4310`；bind 無 bridge `:3777`；`_finish_attach_op` `:4336-4338` | freeze `:1156`；shutdown `:1181-1185`、`:1219-1239`；`_attach_inflight_draining_locked` `:3394`；`attach_device` `:3428-3432`、`:3459`；release `:3104-3107`；bind `:3676-3678`；`attach_session` `:3888-3891`、`:3959-3962`；`update_devices` `:4031-4034`；`:4168-4176`、`:4260-4263`；passthrough 升級 `:4510-4513` | `inflight` 的作業（`attach_released_placeholder` 登錄後開埠；worker 的 `gated_open`） |
/// | `_preopened_ports`（已開埠、沒有 worker） | `attach_device` 成功 `:3592`（佔位交接 `:3608`）；存檔失敗時留著 `:3633-3637` | release `:3110`；bind `:3681`、`:3778`；`update_devices` `:4052`；`_attach_by_id` 轉 active `:4852-4854`；`_finish_attach_op` `:4336-4338` | shutdown 未消費者直接收尾 `:1219-1227`；`attach_device` `:3430`、`:3459`；`attach_session` `:3891`、`:3962`；`_attach_by_id` `:4913`；`:4168`、`:4260`；`:4513`、`:5535`、`:5674` | `inflight` 中 `parked` 的作業（佔位退休落盤失敗）；交給 worker 前的 `AttachOp::preopened` |
/// | `_active_attach_ops`（attach 執行緒持有：偵測、建 bridge） | `_attach_by_id` `:4852-4854`；`:4238`；`:4306` | `_handoff_attach_op_locked` `:4381-4382`；`_finish_attach_op` `:4336-4338`（關閉失敗不移除 `:4322-4332`） | shutdown `:1181-1185`、`:1219-1239`；`:3394`；`attach_device` `:3431`；release `:3104-3107`（取消並等待 `:3117-3131`、`:3188-3204`）；bind `:3676-3697`；`attach_session` `:3889`、`:3960`；`update_devices` 取消 `:4035-4043`、`:4050`；`_register_probe_bridge` `:4369`；`:4511`、`:5433`、`:5535`、`:5674` | worker 持有期間：`inflight` 的作業；worker 把 handle 交給 session 後：交接表 `handoff`（`HandoffEntry`，actor 的 `OpenBridge` 是持有者；`open_bridge` 取走時 `taken`、收尾 `settle_handoff`）；取消者認領未取走的交接列後：`inflight` 的關閉中作業（`claim_handoff_for_close_locked` 於同一臨界區移入，`close_claimed_handoff` 確認關閉才移出，對應 Python 作業留在 `_active_attach_ops` 至 `_finish_attach_op` 完成） |
/// | `_draining_by_ids`（bridge 排空未取得安全回條） | `_begin_attach_bridge_stop_locked` `:1324`；`_begin_drain_locked` `:2774-2775` | `_complete_attach_bridge_stop` `:1350`、`:1368`；`_complete_drain` `:2918-2919` | shutdown `:1195`、`:1260-1263`；`:3284`、`:3320`；`:3390`；`attach_device` `:3410`、`:3469`；`_spawn_attach` `:4113`、`:4137`；`:4174`、`:4258`、`:4299`；`:4416`、`:4505`；`_attach_by_id` `:4847`、`:4938`、`:4991`、`:5014` | `UnconfirmedCloses`：正式 bridge 回條 `handle_closed:false`（actor `worker_stopped`）與正式 bridge 接手前關閉未確認（`close_before_bridge`）；另含 Python 作業因關閉失敗而留在表內的情形（`mark_close_failed`；Rust actor 不讀 manager 表，以同一集合阻擋開埠入口） |
///
/// 消費者在各時點的行為：
///
/// | 時點（handle 在哪） | release | shutdown | bind（舊 by_id） | 裝置拔除／改指 |
/// |---|---|---|---|---|
/// | 開埠在途／worker 持有（`inflight`，非 parked） | 取消並等待作業收尾，期限到回 DRAIN_STALLED（`release_attach_handles`） | 取消並等待，未收尾列 stalled | 取消並等待，逾時 SESSION_RECOVERING | 取消；worker 收尾關閉 |
/// | parked（`inflight`，`parked`） | 取出並關閉；未確認只告警、立即 DRAIN_STALLED | 不取出直接關閉；未確認保留並列 stalled | 取出並關閉（`close_parked_for_bind`）；未確認只告警，照常改綁 | 取出並關閉；未確認只告警 |
/// | 交給 session、actor 尚未接手（`handoff`，未 `taken`） | 認領並留撤銷標記（排入的 `open_bridge` 回 `CloseBlocked`、不另開新 handle；標記交 actor，無開埠在途即移除、開埠在途則於其 `BridgeOpened` 終態移除，不隨 release 返回或逾時移除），handle 移入關閉中作業並寫入第一個 release 的請求與期限；鎖內排入 `ReleaseAttachPending`（Python 設 RECOVERING `:3133-3139`）；專用執行緒關閉，release 照在途作業等待：確認關閉後交 actor 完成 RELEASED，期限到回 DRAIN_STALLED、遲到的關閉由 `PlaceholderAttachSettled` 完成 RELEASED；未確認保留 | session 排空後認領並關閉；未確認保留並列 stalled | 認領並關閉後重試；未確認保留，逾時 SESSION_RECOVERING | 認領並關閉；未確認保留 |
/// | 交接列的關閉中作業（`inflight`，取消者正在關閉） | 取得同一作業、沿用首次期限等待（`:3104-3107`、`:3117-3125`），確認關閉前不回成功 | 等待，未收尾列 stalled | 等待，逾時 SESSION_RECOVERING | 已取消，由關閉者收尾 |
/// | `open_bridge` 已取走（`handoff`，`taken`） | 交 actor（開埠在途的 pending stop） | 等待 `settle_handoff` | 等待 `settle_handoff`，逾時 SESSION_RECOVERING | 交 actor 的 `DeviceDetached` |
///
/// 回歸：`wp510_review9_rpc_release_before_bind_device_online_closes_adopted_handle`（第七次審查 High）、
/// `wp510_review9_rpc_release_before_bind_device_online_close_unconfirmed_drain_stalled`、
/// `wp510_review9_rpc_shutdown_before_bind_device_online_closes_adopted_handle`、
/// `wp510_review9_rpc_shutdown_before_bind_device_online_close_unconfirmed_reports_stalled`、
/// `wp510_review9_rpc_rebind_before_bind_device_online_closes_adopted_handle`、
/// `wp510_review9_rpc_release_with_parked_handle_of_bound_session_closes_it`、
/// `wp510_review9_rpc_release_before_bridge_takes_dynamic_handoff_closes_without_reopen`、
/// `wp510_review8_rpc_bind_retry_parked_close_unconfirmed_keeps_block`（第七次審查 Medium）、
/// `wp510_review9_release_during_existing_session_redetect_cancels_and_waits`（sw-core）、
/// `wp510_review10_rpc_second_release_waits_handoff_close_in_progress`、
/// `wp510_review10_rpc_releases_drain_stalled_while_handoff_close_blocked`（第八次審查 High P8-e25b69c-1）、
/// `wp510_review10_rpc_release_after_revoked_bridge_open_reported_completes`（撤銷標記先於 `Release` 回報）、
/// `wp510_review10_rpc_release_timeout_then_late_redetect_finish_completes_release`、
/// `wp510_review10_late_redetect_finish_after_release_timeout_completes_release`（sw-core；第八次審查 High P8-e25b69c-2）、
/// `wp510_review11_rpc_release_timeout_keeps_revoke_until_bridge_open_reports`、
/// `wp510_review11_open_failure_after_release_stalled_lets_late_settle_release`、
/// `wp510_review11_open_success_after_release_stalled_lets_late_settle_release`（sw-core；第九次審查 High P8-8463b22-1）。
#[derive(Clone)]
struct AttachOp {
    token: u64,
    /// 作業所屬 by_id；`close_parts` 關閉未確認時據此登記 `unconfirmed`。
    by_id: String,
    /// 全 daemon 共用的未確認關閉集合（`Shared::unconfirmed_closes`）。
    unconfirmed: Arc<UnconfirmedCloses>,
    cancelled: Arc<AtomicBool>,
    /// `device.release` 專用：在途佔位開埠由 release 撤銷，收尾需清除對應 actor stall。
    release_requested: Arc<AtomicBool>,
    /// 同一 op 的重複／並行 release 共用首次排空期限（Python `_AttachOp.release_deadline`）。
    release_deadline: Arc<Mutex<Option<Instant>>>,
    /// 第一個取得 op 的 release 請求 `(source, reason)`（Python `_AttachOp.release_request`，`session_manager.py:3117-3122`）；
    /// 收尾時交 actor：已 RELEASED（佔位）以 `released_by or source` 補齊，一般 session 於 release 逾時後的遲到收尾以此
    /// 完成 RELEASED（`:4340-4363`）。
    release_request: Arc<Mutex<Option<(String, PyValue)>>>,
    /// `close()` 的失敗回條必須跨 worker 收尾保留，不能因取消原因不同而移除 inflight。
    close_failed: Arc<AtomicBool>,
    /// 尚未告警的關閉錯誤（`CloseReceipt.error`）；`finish_attach_op` 以 by_id 告警後清除（Python
    /// `_finish_attach_op` `session_manager.py:4322-4332`）。
    close_error: Arc<Mutex<Option<String>>>,
    /// clear 收尾的再 attach（`Effect::Reattach`）：session 已是 ATTACHING 但未開埠，仍須再解析（審查 F62）。
    /// 在途 op 與 clear 重疊時由 `spawn_attach_with` OR 進來，避免該次再解析遺失（審查 F98）。
    after_clear: Arc<AtomicBool>,
    /// OD-63：佔位交接略過 DETACHED-rebind，只走 template 動態偵測。
    dynamic_only: bool,
    /// PROBE／device.attach 已獨占開啟的 handle；交給偵測後再由正式 bridge 接手。
    preopened: Arc<Mutex<Option<Preopened>>>,
    /// 未消費的預開作業（Python 留在 `_preopened_ports`、沒有 worker）：佔位退休落盤失敗後 `_spawn_attach` 未執行
    /// （`origin/main:sw_core/session_manager.py:3633-3637`）。之後由 `spawn_attach_with`／bind 取走 handle 接手
    /// （`_attach_by_id` `:4852-4854`），裝置拔除或改指時關閉（`update_devices` `:4045-4063`），shutdown 收尾關閉
    /// （`shutdown_drain` `:1218-1227`）；以該 by_id 為舊綁定的 bind 先關閉再重試（`bind_session` `:3680-3694`）。
    /// 取用者以 `swap(false)` 認領，只會有一個。
    parked: Arc<AtomicBool>,
    /// PROBE 的提交 Fence（Python `_AttachOp.probe_bridge` 的 TX gate，`session_manager.py:4367-4372`）：作業建立即配置，
    /// 取消者於 fence 鎖內凍結；已越過 `still_valid()` 的 PROBE 於提交點重驗即拒絕（`uart_io.py:623-627`）。
    probe_fence: Arc<Fence>,
    /// PROBE TxWriter 的 CancelToken（Python `begin_drain` 的 `_tx_cancel.set()`，`uart_io.py:908-918`）；
    /// `probe_send` 登錄，取消者一併撤銷進行中的寫入。
    probe_cancel: Arc<Mutex<Option<CancelToken>>>,
}

impl AttachOp {
    fn new(
        by_id: &str,
        unconfirmed: &Arc<UnconfirmedCloses>,
        after_clear: bool,
        dynamic_only: bool,
        preopened: Option<Preopened>,
    ) -> AttachOp {
        AttachOp {
            token: NEXT_ATTACH_OP_TOKEN.fetch_add(1, Ordering::Relaxed),
            by_id: by_id.to_string(),
            unconfirmed: Arc::clone(unconfirmed),
            cancelled: Arc::new(AtomicBool::new(false)),
            release_requested: Arc::new(AtomicBool::new(false)),
            release_deadline: Arc::new(Mutex::new(None)),
            release_request: Arc::new(Mutex::new(None)),
            close_failed: Arc::new(AtomicBool::new(false)),
            close_error: Arc::new(Mutex::new(None)),
            after_clear: Arc::new(AtomicBool::new(after_clear)),
            dynamic_only,
            preopened: Arc::new(Mutex::new(preopened)),
            parked: Arc::new(AtomicBool::new(false)),
            probe_fence: Fence::new(),
            probe_cancel: Arc::new(Mutex::new(None)),
        }
    }

    /// 撤銷作業（裝置改指、bind、shutdown、release 共用）：先發布取消旗標，再於 PROBE Fence 鎖內凍結、
    /// 取消已登錄的 PROBE 寫入（Python 各取消者 `op.cancelled.set()` 後對 `op.probe_bridge.begin_drain()`）。
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        self.probe_fence.with(|f| f.frozen = true);
        if let Some(token) = lock(&self.probe_cancel).as_ref() {
            token.cancel();
        }
    }

    fn take_preopened(&self) -> Option<Preopened> {
        lock(&self.preopened).take()
    }

    fn close_preopened_with_receipt(&self) -> Option<CloseReceipt> {
        self.take_preopened().map(|pre| self.close_parts(pre.parts))
    }

    fn close_parts(&self, parts: HandleParts) -> CloseReceipt {
        let receipt = parts.close();
        if !receipt.closed {
            self.mark_close_failed(close_error_text(&receipt));
        }
        receipt
    }

    /// 關閉未確認：保留作業（`finish_attach_op`）並登記 by_id 的未確認關閉阻擋（Python `_finish_attach_op` 關閉失敗即回
    /// False、作業留在表內，`origin/main:sw_core/session_manager.py:4320-4332`）。
    fn mark_close_failed(&self, error: String) {
        lock(&self.close_error).get_or_insert(error);
        self.close_failed.store(true, Ordering::SeqCst);
        self.unconfirmed.insert(&self.by_id);
    }
}

/// 交接表（`Inner::handoff`）的一列：預開 handle 已自在途作業交給 session 的 `SessionIo::preopened`，等待 actor 的
/// `OpenBridge` 由正式 bridge 接手（見 `AttachOp` 文件的四張表對照）。在途表移出與本列寫入在同一個 manager 臨界區
/// 內完成，release／shutdown／bind／裝置改指在任何時點都查得到 handle。
struct HandoffEntry {
    token: u64,
    io: Arc<SessionIo>,
    /// handle 開啟時的實體路徑；`open_bridge` 只接手同路徑的 handle（路徑不同者留給改指處理）。
    real_path: String,
    /// `open_bridge` 已取走 handle、正交給正式 bridge 或關閉。不論是否取走，Python 的作業都在 `_active_attach_ops`，
    /// 由 `_attach_by_id`／`_attach_by_id_dynamic` 持有至 `_handoff_attach_op_locked`（`:4374-4383`）或
    /// `_finish_attach_op`（`:4320-4365`）；Rust 的「執行緒」是 actor 的 `OpenBridge`。未取走時 actor 可能已忽略
    /// `DeviceAttached`，取消者（release、bind、shutdown、裝置改指）代替中止的 attach 直接關閉；關閉未確認時同
    /// `_finish_attach_op` 保留作業（作業未被取出，`:4322-4332`）。
    taken: bool,
    /// release 已取走並關閉 handle（Python 取消作業後 `_attach_by_id` 見 `cancelled` 中止、不開埠，`:4933-4944`、
    /// `:4986-4996`）：同 session、同路徑的下一次 `open_bridge` 不開埠並移除本列。標記綁定被取消的開埠，不隨 release
    /// RPC 返回而移除：actor 處理 `ReleaseAttachPending` 時無開埠在途即移除，開埠在途則於其 `BridgeOpened` 終態移除
    /// （`Effect::ClearRevokedHandoff`）；沒有 actor 接手時於 release 結束移除。
    revoked: bool,
}

/// `Shared::claim_handoff_locked` 的結果。
enum HandoffClaim {
    /// 無交接列（或已被 release 撤銷）。
    None,
    /// `open_bridge` 已取走 handle，取消者須等待其收尾（`Shared::settle_handoff`）。
    Taken,
    /// actor 尚未接手：handle 已自 session 取出、交接列已處理，由取消者代替中止的 attach 在鎖外關閉
    /// （`Shared::close_before_bridge`，關閉未確認即保留作業）。
    Unclaimed { pre: Option<Preopened>, token: u64 },
}

/// `Shared::claim_handoff_for_close_locked` 的結果。
enum HandoffCloseClaim {
    /// 無交接列（或已被 release 撤銷）。
    None,
    /// `open_bridge` 已取走 handle，取消者須等待其收尾（`Shared::settle_handoff`）。
    Taken,
    /// actor 尚未接手：交接列已處理，handle 已移入關閉中作業（`closing`，無 handle 時為 `None`），由取消者以
    /// `Shared::close_claimed_handoff` 在鎖外關閉並收尾。
    Unclaimed { closing: Option<AttachOp>, token: u64 },
}

/// `Shared::take_for_bridge` 的結果。
enum BridgeTake {
    /// 交接已被 release 撤銷：不開埠（`OpenFailure::CloseBlocked`）。
    Revoked,
    /// 取得的預開 handle（無則另行開埠）。
    Handle(Option<Preopened>),
}

/// 只供測試（test-fakes）：綁定 by_id 的一次性暫停點。
#[cfg(feature = "test-fakes")]
type ByIdTestHook = (String, Box<dyn FnOnce() + Send>);

/// 已建立、尚未放行的 attach worker 執行緒（`Shared::prepare_attach_worker`）。
struct PreparedAttachWorker {
    go: Sender<()>,
}

impl PreparedAttachWorker {
    /// 放行 worker 執行 attach 本體並於結束時收尾作業。
    fn start(self) {
        let _ = self.go.send(());
    }
}

/// Python `threading.Thread.start()` 無法建立執行緒時的 `RuntimeError` 訊息（RPC 邊界的 `str(exc)`）。
const THREAD_START_FAILED: &str = "can't start new thread";

#[derive(Default)]
struct Inner {
    /// session_id → 鏡像（插入順序＝建立順序，selector 的 tiebreak 與 DETACHED-rebind 候選都依此）。
    sessions: IndexMap<String, Entry>,
    devices: BTreeMap<String, DeviceInfo>,
    /// startup 預配 COM（#100）：by_id → COM。
    pending_com: HashMap<String, String>,
    /// by_id → 進行中的 attach 作業（Python `_attach_ops`／`_preopened_ports`（`parked`）／`_active_attach_ops`）。
    inflight: HashMap<String, AttachOp>,
    /// by_id → 已交給 session、等待正式 bridge 接手的預開 handle（見 `HandoffEntry`）。
    handoff: HashMap<String, HandoffEntry>,
    /// SC-11 stopping gate。
    stopping: bool,
    /// interactive_id → session_id。
    interactive: HashMap<String, String>,
    /// 動態偵測當時的 real_path（sticky 的 TOCTOU 比對；#95 HIGH#2）。
    probe_paths: HashMap<String, String>,
    /// P8 Task 8：flash-critical 的 COM（Python `_flash_critical_coms`；#83 RACE-2 final）。
    flash_critical: HashSet<String>,
    /// P8 Task 14：SC-11 交接凍結成立中（Python `_handoff_frozen`）：新建 session 的 fence 承接交接凍結。
    handoff_frozen: bool,
    /// P8 Task 14：`begin_shutdown` 已呼叫（交接回滾只還原自己設的 stopping gate，不撤銷 shutdown 的）。
    shutdown_started: bool,
    /// S2-PAR-wp510r R1: 保管帳本基礎設施
    #[allow(dead_code)]
    pub custody: crate::custody::CustodyLedger,
    #[allow(dead_code)]
    pub prepared: HashMap<u64, PreparedAttachWorker>,
    #[allow(dead_code)]
    pub deliverer: crate::custody::Deliverer,
}

/// P8 Task 14：偵測 PROBE 的 fence（每次 `probe_send` 新建）與交接凍結旗標，同一把鎖內登記與讀取：
/// probe 要嘛在凍結前登記（由 `freeze_for_handoff` 凍結並等收斂），要嘛在凍結後建立即帶凍結。上限＝同時在途的
/// attach worker 數（每個 worker 一次只送一筆 PROBE）。
#[derive(Default)]
struct HandoffProbes {
    frozen: bool,
    live: Vec<Arc<Fence>>,
}

impl Inner {
    fn session_by_id(&self, by_id: &str) -> Option<&Entry> {
        self.sessions
            .values()
            .find(|e| e.profile.device_by_id == by_id)
    }

    fn resolve(&self, selector: &str) -> Option<&Entry> {
        if let Some(e) = self.sessions.get(selector) {
            return Some(e);
        }
        self.sessions
            .values()
            .find(|e| e.profile.com == selector || e.profile.alias == selector)
    }

    fn device_path(&self, by_id: &str) -> Option<&str> {
        self.devices.get(by_id).map(|d| d.real_path.as_str())
    }

    /// `_next_dynamic_com`：既有 session 與預配號以外的最小空號。
    fn next_dynamic_com(&self) -> String {
        let mut used: HashSet<&str> = self
            .sessions
            .values()
            .map(|e| e.profile.com.as_str())
            .collect();
        used.extend(self.pending_com.values().map(String::as_str));
        let mut i = 0usize;
        while used.contains(format!("COM{i}").as_str()) {
            i += 1;
        }
        format!("COM{i}")
    }
}

/// manager 與各 executor／worker 共用的依賴與狀態。
struct Shared {
    serial: Arc<dyn SerialBackend>,
    uart_excl: Arc<dyn UartExclusive>,
    /// console 後端：哨兵 PTY 工廠（審查 F43）與 P6 Task 9／10 的 PTY 建立、ConsoleHub 的 mux。
    console: Arc<dyn ConsoleTransportFactory>,
    wal: Arc<WalWriter>,
    registry: Arc<CommandRegistry>,
    clock: Arc<dyn Clock>,
    env: Arc<dyn SysEnv>,
    fs: Arc<dyn FsOps>,
    warn: Arc<dyn Fn(String) + Send + Sync>,
    store: StateStore,
    model: Mutex<StateModel>,
    fanout: Arc<WalHealthFanout>,
    epochs: Arc<EpochAllocator>,
    templates: Vec<ProfileTemplate>,
    max_sessions: usize,
    inner: Mutex<Inner>,
    cv: Condvar,
    note_tx: Sender<ManagerNote>,
    /// 全 daemon 共用的 background capture 索引（審查 F28）。
    bg_index: Arc<crate::capture::BackgroundIndex>,
    /// 未確認關閉的 by_id 集合（Python `_draining_by_ids` 對照；manager 與各 actor 共用，見 `UnconfirmedCloses`）。
    unconfirmed_closes: Arc<UnconfirmedCloses>,
    closed: AtomicBool,
    /// state.json 持久化觀察者（P5-T19 #3／#4／#68 的 Stage 0 測試鉤子；未設定時不動作）。
    persist_observer: Mutex<Option<Arc<dyn PersistObserver>>>,
    /// 只供測試（test-fakes）：偵測 PROBE 的 Fence 以 frozen 建立（驗證 PROBE TX 經 Fence 時零 TX）。
    probe_tx_frozen: AtomicBool,
    /// P8 Task 14：在途 PROBE fence 與交接凍結旗標（見 [`HandoffProbes`]）。
    handoff_probes: Mutex<HandoffProbes>,
    /// P7 Task 16-wire：RX 觀察者（Python `add_rx_observer`）；每個 session bridge 的 reader 建立時取快照。
    rx_observers: Mutex<Vec<Arc<dyn crate::uart_bridge::RxObserver>>>,
    /// P6（sol 補審 #2）：human line mode basename 的路徑風格（`caps.path_flavor`）。
    path_flavor: sw_pal::PalPathFlavor,
    /// 只供測試（test-fakes）：偵測 PROBE 的 Fence 建立後、交給 TxWriter 提交前呼叫（重現「健康快照之後才發布
    /// WAL unhealthy」的競態；sol-backfill p5 #6）。
    #[cfg(feature = "test-fakes")]
    probe_pre_submit_hook: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    /// 只供測試（test-fakes）：佔位 release 取得在途 op 後、取消標記前的競態暫停點。
    #[cfg(feature = "test-fakes")]
    placeholder_release_cancel_hook: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    /// 只供測試（test-fakes）：`alias.set` 已更新 actor、尚未持久化前的競態暫停點（佔位退休交錯）。
    #[cfg(feature = "test-fakes")]
    alias_pre_commit_hook: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    /// 只供測試（test-fakes）：`open_bridge` 取 session 預開 handle 之前的一次性暫停點（重現預開 handle 交給
    /// session 後、正式 bridge 接手前的 shutdown 交錯）。
    #[cfg(feature = "test-fakes")]
    open_bridge_pre_take_hook: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    /// 只供測試（test-fakes）：`open_bridge` 已取走 session 預開 handle（交接列 `taken`）、開埠尚未返回時的一次性暫停點
    /// （重現 bind 等待已取走的交接列逾時、之後開埠才成功的交錯；wp510 第十次整包審查 High P8-14fe592-1）。
    #[cfg(feature = "test-fakes")]
    open_bridge_post_take_hook: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    /// 只供測試（test-fakes）：指定 by_id 的下一次 `finish_attach_op` 取得 manager 鎖之前的一次性暫停點（重現 attach
    /// worker 收尾與 `open_bridge` 的 `retain_unconfirmed_close` 交錯；wp510 P8-fdc1413-1）。
    #[cfg(feature = "test-fakes")]
    finish_attach_op_pre_lock_hook: Mutex<Option<ByIdTestHook>>,
    /// 只供測試（test-fakes）：下一次建立 attach worker 執行緒時模擬 `Builder::spawn()` 失敗（執行緒資源耗盡）。
    #[cfg(feature = "test-fakes")]
    fail_next_attach_spawn: AtomicBool,
    /// 只供測試（test-fakes）：`session.bind` 已把改綁目標的未消費預開 handle 交給 session、送出 `DeviceOnline` 之前的
    /// 一次性暫停點（重現 actor 處理 `DeviceOnline` 前插入的 release／shutdown／bind；wp510 第七次整包審查 High）。
    #[cfg(feature = "test-fakes")]
    bind_pre_device_online_hook: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    /// 只供測試（test-fakes）：累計啟動過的 session actor 與 attach worker thread 數（`spawned_worker_count`）。
    #[cfg(feature = "test-fakes")]
    spawned_workers: std::sync::atomic::AtomicUsize,
    /// 只供測試（test-fakes）：覆寫 session 公開查詢期限，以可控期限重現 handoff 的 actor timeout。
    #[cfg(feature = "test-fakes")]
    query_timeout_override: Mutex<Option<Duration>>,
    /// 只供測試（test-fakes）：覆寫傳檔 RPC 等待期限，驗證 actor timeout 後仍由操作持有 admission。
    #[cfg(feature = "test-fakes")]
    file_transfer_timeout_override: Mutex<Option<Duration>>,
    /// 載入的 state.json 含孤立 surrogate（`LoadedState::lone_surrogates`；啟動存檔前檢查）。
    lone_surrogates: bool,
    /// 啟動存檔重現的 Python 未捕捉例外末行（`_save_state` 的 `UnicodeEncodeError`）；daemon 據此 exit 1。
    startup_error: Mutex<Option<String>>,
    /// S2-PAR-wp510r R1: 回收者命令通道與執行緒
    #[allow(dead_code)]
    pub reaper_cmd_tx: Sender<crate::custody::ReaperCmd>,
    #[allow(dead_code)]
    pub reaper: Mutex<Option<crate::custody::ReaperThread>>,
}

/// PROBE 的 writer 交給 TxWriter thread 期間留在 `HandleParts` 的占位（任何提交一律 NotSubmitted）。
struct DetachedWriter;

impl SerialWriter for DetachedWriter {
    fn write_gated(&mut self, _data: &[u8], _cancel: &CancelToken, _gate: &dyn SubmitGate) -> TxResult {
        TxResult {
            outcome: TxOutcome::NotSubmitted,
            error: None,
        }
    }
}

/// state.json 持久化觀察者（P5-T19，parity #3／#4／#68）。
///
/// 對應 Python `_save_state()` 尾端的 `_test_kill_after_save_hook(False)` 與 release 持久化專用入口
/// `_save_release_state()`（commit 前暫停點 → 落盤 → 落盤後 kill 鉤子）。是否生效（僅隔離實例）由實作者
/// （daemon 組裝點）決定，sw-core 只提供呼叫點。
pub trait PersistObserver: Send + Sync {
    /// release 持久化 commit 之前（Python `_test_release_persist_point()`）；`Err` 時不 commit、release 持久化失敗
    /// （Python 暫停點的 `os.makedirs`／寫檔例外會穿出 `_save_release_state`，審查 F85）。
    fn before_release_commit(&self) -> Result<(), PalError>;
    /// 任一 state.json commit 成功之後；`release` 為 true 表示本次為 release 持久化
    /// （Python 先以 `False` 呼叫一次 kill 鉤子，release 持久化再以 `True` 呼叫一次）。
    fn after_commit(&self, release: bool);
}

/// 全域 session 管理者（plan 06 Task 16）。
pub struct SessionManager {
    shared: Arc<Shared>,
    watcher_tx: Sender<WatcherEvent>,
    watcher_rx: Receiver<WatcherEvent>,
    note_rx: Receiver<ManagerNote>,
    pump_lock: Arc<Mutex<()>>,
    _console: Arc<dyn ConsoleTransportFactory>,
    _caps: Capabilities,
}

/// console／interactive_open 查無 session（Python `{"ok":False,"error_code":"SESSION_NOT_READY","selector":selector}`）。
fn console_not_ready(selector: &str) -> PyDict {
    pydict! { "ok" => false, "error_code" => "SESSION_NOT_READY", "selector" => selector.to_string() }
}

/// core 的 `SESSION_NOT_READY` 帶 session 的 COM；Python 回原樣的 RPC selector。
fn with_raw_selector(mut result: PyDict, selector: &str) -> PyDict {
    if result.get("error_code").and_then(PyValue::as_str) == Some("SESSION_NOT_READY") && result.get("selector").is_some() {
        result.insert("selector", PyValue::from(selector.to_string()));
    }
    result
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn pyint_usize(value: &PyInt, default: usize) -> usize {
    match value {
        PyInt::Small(n) => usize::try_from(*n).unwrap_or(default),
        PyInt::Big(n) => n.parse().unwrap_or(default),
    }
}

fn pyint_u32(value: &PyInt, default: u32) -> u32 {
    match value {
        PyInt::Small(n) => u32::try_from(*n).unwrap_or(default),
        PyInt::Big(n) => n.parse().unwrap_or(default),
    }
}

fn pyint_u8(value: &PyInt, default: u8) -> u8 {
    match value {
        PyInt::Small(n) => u8::try_from(*n).unwrap_or(default),
        PyInt::Big(n) => n.parse().unwrap_or(default),
    }
}

fn pyint_i128(value: &PyInt) -> i128 {
    match value {
        PyInt::Small(n) => *n,
        PyInt::Big(n) => n.parse().unwrap_or(i128::MAX),
    }
}

fn serial_config(uart: &UartProfile) -> SerialConfig {
    SerialConfig::from_uart_profile(
        pyint_u32(&uart.baud, 115_200),
        pyint_u8(&uart.data_bits, 8),
        &uart.parity,
        pyint_u8(&uart.stop_bits, 1),
        &uart.flow_control,
        uart.xonxoff,
    )
}

/// 隨機位元組（process 隨機種子的 SipHash＋遞增計數；uuid4／nonce 用，不涉及平台 API）。
fn fill_random(buf: &mut [u8]) {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    for chunk in buf.chunks_mut(8) {
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
        let bytes = h.finish().to_le_bytes();
        chunk.copy_from_slice(&bytes[..chunk.len()]);
    }
}

/// `uuid4().hex[:8]`（ready_probe nonce）。
fn nonce_hex8() -> String {
    let mut b = [0u8; 4];
    fill_random(&mut b);
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// 開埠失敗的 Python 例外類名（`ATTACH_FAILED:<class>`）。
fn open_error_class(e: &OpenError) -> &'static str {
    match e {
        OpenError::Held(_) => "DeviceHeldError",
        OpenError::AccessDenied { .. } => "PermissionError",
        OpenError::NotFound(_) => "FileNotFoundError",
        OpenError::Other(err) => err.py_exc_name(),
    }
}

fn open_failure(e: OpenError) -> OpenFailure {
    match e {
        OpenError::Held(holder) => OpenFailure::Held(holder),
        OpenError::AccessDenied { holder, .. } => OpenFailure::AccessDenied(holder),
        OpenError::NotFound(_) => OpenFailure::NotFound,
        OpenError::Other(err) => OpenFailure::Other(err),
    }
}

/// Python `attach_device` 對不可見裝置的回應。
fn device_not_present_reply(selector: &str, by_id: &str) -> PyDict {
    pydict! {
        "ok" => false,
        "error_code" => "DEVICE_NOT_PRESENT",
        "selector" => selector.to_string(),
        "device_by_id" => by_id.to_string()
    }
}

/// Python `attach_device` 對外部 holder／OS 拒絕獨占的回應。
fn device_still_held_reply(selector: &str, pids: Vec<u32>) -> PyDict {
    let holder_unknown = pids.is_empty();
    pydict! {
        "ok" => false,
        "error_code" => "DEVICE_STILL_HELD",
        "pids" => PyValue::List(pids.into_iter().map(|pid| PyValue::Int(PyInt::Small(pid as i128))).collect()),
        "holder_unknown" => holder_unknown,
        "selector" => selector.to_string()
    }
}

/// Python attach 在途分支的完整回應；頂層 drain 欄位取自同一份 session 快照。
fn session_recovering_reply(session: PyDict) -> PyDict {
    let drain_stalled = session
        .get("drain_stalled")
        .cloned()
        .unwrap_or(PyValue::Bool(false));
    let drain_timeout_epoch = session
        .get("drain_timeout_epoch")
        .cloned()
        .unwrap_or(PyValue::Null);
    pydict! {
        "ok" => false,
        "error_code" => "SESSION_RECOVERING",
        "drain_stalled" => drain_stalled,
        "drain_timeout_epoch" => drain_timeout_epoch,
        "session" => session
    }
}

/// SC-11：stopping 後 RPC 路徑的重開（Python `RuntimeError("daemon stopping")`，framing 轉 `EXCEPTION`）。
fn stopping_error() -> PyErr {
    PyErr::value_error("daemon stopping")
}

fn commit_error(e: &PalError) -> PyErr {
    PyErr::value_error(e.py_message.clone())
}

/// 由 template 物化 session profile（`_session_from_template` 的 `SessionProfile(...)`）。
fn profile_from_template(
    tpl: &ProfileTemplate,
    com: &str,
    act_no: i128,
    alias: &str,
    by_id: &str,
) -> SessionProfile {
    SessionProfile {
        com: com.to_string(),
        act_no: PyInt::Small(act_no),
        alias: alias.to_string(),
        device_by_id: by_id.to_string(),
        template: tpl.clone(),
    }
}

/// Python `ProfileTemplate(profile_name=..., platform="passthrough", ready_probe="")`（其餘為 dataclass 預設值）。
fn minimal_passthrough_template(name: &str) -> ProfileTemplate {
    ProfileTemplate {
        profile_name: name.to_string(),
        platform: "passthrough".to_string(),
        prompt_regex: r"(?m)^root@prplOS:.*# ".to_string(),
        login_regex: r"(?mi)^login:\\s*$".to_string(),
        password_regex: r"(?mi)^password:\\s*$".to_string(),
        post_login_cmd: String::new(),
        ready_probe: String::new(),
        username: None,
        user_env: None,
        pass_env: None,
        env_file: None,
        console_unlock_env: None,
        console_unlock_idle_s: 5.0,
        timeout_s: 10.0,
        quiet_window_s: 2.0,
        hard_timeout_s: 60.0,
        log_dir: None,
        uart: UartProfile::default(),
        bootloader_prompts: Vec::new(),
        boot_done_regex: String::new(),
        max_console_line_chars: None,
    }
}

/// `AliasRegistry.set_for_session` 產生的列。
fn alias_row(sid: &str, alias: &str, now_iso: &str) -> PyDict {
    let mut reg = AliasRegistry::default();
    reg.set_for_session(sid, alias, now_iso);
    reg.dump().shift_remove(alias).unwrap_or_default()
}

/// `{"ok":false,"error_code":"FLASHING_BUSY","selector":<com>,"session":…}`（Python clear／bind 的鍵序）。
fn flashing_busy(public: PyDict) -> PyDict {
    let com = public.get("com").cloned().unwrap_or(PyValue::Null);
    pydict! {
        "ok" => false,
        "error_code" => "FLASHING_BUSY",
        "selector" => com,
        "session" => public
    }
}

fn session_of(d: &PyDict) -> Option<PyDict> {
    match d.get("session") {
        Some(PyValue::Dict(s)) => Some(s.clone()),
        _ => None,
    }
}

fn str_of(d: &PyDict, key: &str) -> Option<String> {
    d.get(key).and_then(|v| v.as_str()).map(str::to_string)
}

fn is_ok(d: &PyDict) -> bool {
    d.get("ok") == Some(&PyValue::Bool(true))
}

/// 持有 manager lock 時只把 safety request 排入 actor mailbox；回覆須於解鎖後等待。
fn enqueue_safety_request(
    actor: &ActorHandle,
    req: SessionRequest,
    timeout: Duration,
) -> Result<Receiver<Result<PyDict, PyErr>>, PyErr> {
    let (reply, rx) = crossbeam_channel::bounded(1);
    actor
        .safety
        .send_timeout(ControlEvent::Request { req, reply }, timeout)
        .map_err(|error| match error {
            crossbeam_channel::SendTimeoutError::Timeout(_) => {
                PyErr::value_error("internal timeout")
            }
            crossbeam_channel::SendTimeoutError::Disconnected(_) => {
                PyErr::value_error("session actor stopped")
            }
        })?;
    Ok(rx)
}

/// 等待先前已排入 actor mailbox 的 request 回覆。
fn await_actor_reply(
    rx: Receiver<Result<PyDict, PyErr>>,
    timeout: Duration,
) -> Result<PyDict, PyErr> {
    match rx.recv_timeout(timeout) {
        Ok(result) => result,
        Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
            Err(PyErr::value_error("internal timeout"))
        }
        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
            Err(PyErr::value_error("session actor stopped"))
        }
    }
}

impl Shared {
    fn query_timeout(&self) -> Duration {
        #[cfg(feature = "test-fakes")]
        if let Some(timeout) = *lock(&self.query_timeout_override) {
            return timeout;
        }
        QUERY_TIMEOUT
    }

    fn file_transfer_reply_timeout(&self) -> Duration {
        #[cfg(feature = "test-fakes")]
        if let Some(timeout) = *lock(&self.file_transfer_timeout_override) {
            return timeout;
        }
        FILE_TRANSFER_REPLY_TIMEOUT
    }

    fn now_iso(&self) -> String {
        iso_utc_now(self.clock.wall_unix_us())
    }

    fn warn(&self, msg: String) {
        (self.warn)(msg);
    }

    /// 關閉 Python 已先自表中取出的未消費預開 handle（release `:3108-3111`、bind `:3680-3681`／`:3776-3778`、裝置改指
    /// `:4045-4063`），回傳是否確認關閉；`closed:false` 時比照 `_finish_attach_op`（`origin/main:sw_core/session_manager.py:4322-4332`）
    /// 以 by_id 與 `CloseReceipt.error` 告警。作業已不在任何表內，關閉失敗不保留作業、不登記阻擋（`_draining_by_ids`
    /// 只由 bridge 排空寫入 `:1324`、`:2774-2775`）。
    fn close_session_preopened(&self, by_id: &str, pre: Preopened) -> bool {
        let receipt = pre.parts.close();
        if !receipt.closed {
            self.warn(preopened_close_warning(by_id, &close_error_text(&receipt)));
        }
        receipt.closed
    }

    /// 鎖內把預開 handle 交給 session（`SessionIo::preopened`）並寫入交接表；呼叫端在把 handle 移出在途作業的同一
    /// 臨界區內呼叫（Python `_attach_by_id` 於同一把鎖內 `_preopened_ports.pop` → `_active_attach_ops[by_id] = op`，
    /// `origin/main:sw_core/session_manager.py:4852-4854`）。回傳被取代、需由呼叫端在鎖外關閉的舊 handle。
    fn place_handoff_locked(
        inner: &mut Inner,
        by_id: &str,
        io: &Arc<SessionIo>,
        pre: Preopened,
    ) -> Vec<Preopened> {
        let real_path = pre.real_path.clone();
        let mut displaced: Vec<Preopened> = lock(&io.preopened).replace(pre).into_iter().collect();
        displaced.extend(Self::register_handoff_locked(inner, by_id, io, real_path));
        displaced
    }

    /// 為已放進 `io.preopened` 的 handle 寫入交接表；同一 by_id 或同一 session 的舊列（未取走者）連同其 handle 一併取出。
    fn register_handoff_locked(
        inner: &mut Inner,
        by_id: &str,
        io: &Arc<SessionIo>,
        real_path: String,
    ) -> Vec<Preopened> {
        let mut displaced = Vec::new();
        let stale: Vec<String> = inner
            .handoff
            .iter()
            .filter(|(key, entry)| {
                !entry.taken && (key.as_str() == by_id || Arc::ptr_eq(&entry.io, io))
            })
            .map(|(key, _)| key.clone())
            .collect();
        for key in stale {
            if let Some(entry) = inner.handoff.remove(&key) {
                if !entry.revoked && !Arc::ptr_eq(&entry.io, io) {
                    displaced.extend(lock(&entry.io.preopened).take());
                }
            }
        }
        inner.handoff.insert(
            by_id.to_string(),
            HandoffEntry {
                token: NEXT_ATTACH_OP_TOKEN.fetch_add(1, Ordering::Relaxed),
                io: io.clone(),
                real_path,
                taken: false,
                revoked: false,
            },
        );
        displaced
    }

    /// 取消者認領 by_id 的交接列（release／bind／shutdown／裝置改指）。未取走者取出 handle 由呼叫端以
    /// `close_before_bridge` 關閉；`revoke` 為真（release）時保留撤銷標記，令已排入的 `open_bridge` 不開埠，否則移除該列。
    fn claim_handoff_locked(inner: &mut Inner, by_id: &str, revoke: bool) -> HandoffClaim {
        let Some(entry) = inner.handoff.get_mut(by_id) else {
            return HandoffClaim::None;
        };
        if entry.revoked {
            return HandoffClaim::None;
        }
        if entry.taken {
            return HandoffClaim::Taken;
        }
        let pre = lock(&entry.io.preopened).take();
        let token = entry.token;
        if revoke {
            entry.revoked = true;
        } else {
            inner.handoff.remove(by_id);
        }
        HandoffClaim::Unclaimed { pre, token }
    }

    /// 取消者（release、bind、shutdown、裝置改指）認領交接列，並在同一臨界區把取出的 handle 移入在途表的關閉中作業，
    /// 讓 handle 在關閉確認前一直查得到（wp510 第八次整包審查 High P8-e25b69c-1）。
    /// Python 的作業此時在 `_active_attach_ops`，由持有作業的 attach 中止後 `_finish_attach_op` 關閉，確認關閉才移出
    /// （`origin/main:sw_core/session_manager.py:4320-4338`）；其間的 release 取得同一作業並等待（`:3104-3107`、
    /// `:3117-3125`、`:3188-3204`）。同 by_id 已有其他作業時不覆蓋（該作業仍擋住 release／shutdown／bind），關閉未確認
    /// 改標記該作業（`close_claimed_handoff`）。
    fn claim_handoff_for_close_locked(
        &self,
        inner: &mut Inner,
        by_id: &str,
        revoke: bool,
    ) -> HandoffCloseClaim {
        match Self::claim_handoff_locked(inner, by_id, revoke) {
            HandoffClaim::None => HandoffCloseClaim::None,
            HandoffClaim::Taken => HandoffCloseClaim::Taken,
            HandoffClaim::Unclaimed { pre, token } => {
                let closing = pre.map(|pre| {
                    let op = AttachOp::new(by_id, &self.unconfirmed_closes, false, false, Some(pre));
                    op.cancel();
                    if !inner.inflight.contains_key(by_id) {
                        inner.inflight.insert(by_id.to_string(), op.clone());
                    }
                    op
                });
                HandoffCloseClaim::Unclaimed { closing, token }
            }
        }
    }

    /// 關閉 `claim_handoff_for_close_locked` 取出的 handle 並收尾關閉中作業（Python `_finish_attach_op`
    /// `origin/main:sw_core/session_manager.py:4320-4365`）：確認關閉才移出在途表，release 請求過者由 actor 收尾
    /// （`PlaceholderAttachSettled`）；`closed:false` 以 by_id 與錯誤告警、保留作業並登記未確認關閉（`:4322-4332`）。
    /// 作業未能登錄（同 by_id 已有其他作業）時由 `retain_unconfirmed_close` 改標記該作業。
    fn close_claimed_handoff(&self, by_id: &str, op: &AttachOp) {
        let failed = op
            .close_preopened_with_receipt()
            .is_some_and(|receipt| !receipt.closed);
        if failed {
            self.retain_unconfirmed_close(by_id);
        }
        self.finish_attach_op(by_id, op, None);
    }

    /// 移除 release 留下的撤銷標記（同 token 才移除）：actor 確定被取消的開埠不會再取用（`Effect::ClearRevokedHandoff`，
    /// 見 `SessionRequest::ReleaseAttachPending`），或沒有 actor 接手時於 release 結束。
    fn clear_revoked_handoff(&self, by_id: &str, token: u64) {
        let mut inner = lock(&self.inner);
        if inner
            .handoff
            .get(by_id)
            .is_some_and(|entry| entry.revoked && entry.token == token)
        {
            inner.handoff.remove(by_id);
        }
    }

    /// `open_bridge` 取預開 handle：於 manager 鎖內查該 session 的交接列，同路徑者取走並標為 `taken`（Python 作業由
    /// `_attach_by_id` 持有於 `_active_attach_ops`）；已被 release 撤銷者不開埠。查無交接列時沿用 `io.preopened`。
    fn take_for_bridge(&self, io: &Arc<SessionIo>, dev: &str) -> BridgeTake {
        let mut inner = lock(&self.inner);
        let key = inner
            .handoff
            .iter()
            .find(|(_, entry)| Arc::ptr_eq(&entry.io, io))
            .map(|(key, _)| key.clone());
        let Some(key) = key else {
            return BridgeTake::Handle(lock(&io.preopened).take());
        };
        let entry = inner.handoff.get_mut(&key).expect("剛查到的交接列");
        if entry.real_path != dev {
            // 路徑不同（裝置改指或過期的開埠）：handle 留在交接表，由改指或取消者處理；本次另行開埠。
            return BridgeTake::Handle(None);
        }
        if entry.revoked {
            inner.handoff.remove(&key);
            return BridgeTake::Revoked;
        }
        if entry.taken {
            return BridgeTake::Handle(None);
        }
        let pre = lock(&io.preopened).take();
        if pre.is_some() {
            entry.taken = true;
        } else {
            inner.handoff.remove(&key);
        }
        BridgeTake::Handle(pre)
    }

    /// `open_bridge` 收尾：正式 bridge 已接手（Python `_handoff_attach_op_locked` `:4374-4383`）或 handle 已關閉
    /// （關閉未確認者已由 `retain_unconfirmed_close` 先移入在途表），移除該 session 已取走的交接列並喚醒等待者。
    fn settle_handoff(&self, io: &Arc<SessionIo>) {
        let mut inner = lock(&self.inner);
        inner
            .handoff
            .retain(|_, entry| !(entry.taken && Arc::ptr_eq(&entry.io, io)));
        drop(inner);
        self.cv.notify_all();
    }

    /// `open_bridge` 在正式 bridge 接手前關閉 handle（預開或剛開啟者）；回傳是否確認關閉。對應 Python `_attach_by_id`
    /// 的中止路徑：預開作業已移入 `_active_attach_ops`（`:4852-4854`），`_finish_attach_op` 關閉失敗即回 False 並保留
    /// 作業（`:4322-4332`）。`closed:false` 時告警並經 `retain_unconfirmed_close` 讓該 by_id 持續參與排空判定與阻擋。
    fn close_before_bridge(&self, by_id: &str, parts: HandleParts) -> bool {
        let receipt = parts.close();
        if receipt.closed {
            return true;
        }
        self.warn(preopened_close_warning(by_id, &close_error_text(&receipt)));
        self.retain_unconfirmed_close(by_id);
        false
    }

    /// 未確認關閉的 handle 以 `close_failed` 作業留在 `inflight`（Python 作業留在 `_active_attach_ops`、不設 `done`）：
    /// shutdown 末段列為 `attach:<by_id>` stalled（`:1231-1239`）、release 回 DRAIN_STALLED（`:3192-3204`），
    /// 自動 attach、佔位交接與 bind 皆被同一 inflight 項目擋住。同一 by_id 已有作業時改標記該作業，令其收尾時保留。
    /// Rust 的 handle 只關閉一次，阻擋持續到 daemon 結束。
    fn retain_unconfirmed_close(&self, by_id: &str) {
        let mut inner = lock(&self.inner);
        self.unconfirmed_closes.insert(by_id);
        match inner.inflight.get(by_id) {
            Some(op) => op.close_failed.store(true, Ordering::SeqCst),
            None => {
                let op = AttachOp::new(by_id, &self.unconfirmed_closes, false, false, None);
                op.cancel();
                op.close_failed.store(true, Ordering::SeqCst);
                inner.inflight.insert(by_id.to_string(), op);
            }
        }
        drop(inner);
        self.cv.notify_all();
    }

    fn template_by_name(&self, name: &str) -> Option<ProfileTemplate> {
        self.templates
            .iter()
            .find(|t| t.profile_name == name)
            .cloned()
    }

    /// `_default_passthrough_template`：優先非 command-capable 的 passthrough。
    fn default_passthrough_template(&self) -> Option<ProfileTemplate> {
        self.templates
            .iter()
            .find(|t| t.platform == "passthrough" && !t.command_capable())
            .or_else(|| self.templates.iter().find(|t| t.platform == "passthrough"))
            .cloned()
    }

    /// state.json 同步寫入（`StateModel::apply` → `StateStore` 單一寫入者）；失敗回 `Err`。
    /// 快照於 model 鎖內建立並排入 store（last-writer-wins 依提交順序）。
    fn commit_sync(&self, f: impl FnOnce(&mut StateModel)) -> Result<(), PalError> {
        self.commit_sync_kind(f, false)
    }

    /// 同 `commit_sync`；`release` 為 true 時為 release 持久化（觀察者的暫停點與 kill 鉤子，P5-T19 #3／#4）。
    fn commit_sync_kind(
        &self,
        f: impl FnOnce(&mut StateModel),
        release: bool,
    ) -> Result<(), PalError> {
        let observer = lock(&self.persist_observer).clone();
        if release {
            if let Some(o) = &observer {
                o.before_release_commit()?;
            }
        }
        let (tx, rx) = crossbeam_channel::bounded(1);
        {
            let mut model = lock(&self.model);
            f(&mut model);
            self.store.commit(model.snapshot(), tx);
        }
        let result = rx.recv().unwrap_or_else(|_| {
            Err(PalError::plain(
                PalErrorKind::BrokenPipe,
                "state store 已停止",
            ))
        });
        if result.is_ok() {
            if let Some(o) = &observer {
                o.after_commit(false);
                if release {
                    o.after_commit(true);
                }
            }
        }
        result
    }

    /// 只改記憶體 model（下一次寫入才落盤；Python 只改 `self._aliases` 等而不 `_save_state` 的路徑）。
    fn model_apply(&self, delta: StateDelta) {
        lock(&self.model).apply(delta);
    }

    fn released_by_ids(&self) -> HashSet<String> {
        lock(&self.model).released_by_ids()
    }

    fn model_snapshot(&self) -> LoadedState {
        lock(&self.model).snapshot()
    }

    fn actor_of(&self, sid: &str) -> Option<Arc<ActorHandle>> {
        lock(&self.inner).sessions.get(sid).map(|e| e.actor.clone())
    }

    fn request(&self, sid: &str, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr> {
        match self.actor_of(sid) {
            Some(actor) => actor.request(req, timeout),
            None => Ok(pydict! {
                "ok" => false,
                "error_code" => "SESSION_NOT_FOUND",
                "selector" => sid.to_string()
            }),
        }
    }

    /// 07 D-09：RPC handler thread 取 `/proc` 持有者快照，經同一條 control sender 先於 request 送
    /// `ConsoleScan{source: Rpc}`（FIFO 保證 core 處理 request 前已更新 peer 判定）。lane 滿時略過。
    fn send_console_scan(&self, sid: &str) {
        let Some(actor) = self.actor_of(sid) else {
            return;
        };
        let scan = crate::human_console::ConsoleScanSnapshot {
            held: self.console.held_slave_paths(),
            taken_at: self.clock.mono_ns(),
        };
        let _ = actor.control.try_send(ControlEvent::ConsoleScan {
            scan,
            source: crate::human_console::ScanSource::Rpc,
        });
    }

    /// actor 的 `session` 公開欄位（GetState）。
    fn session_public(&self, sid: &str) -> Option<PyDict> {
        self.send_console_scan(sid);
        let actor = self.actor_of(sid)?;
        let r = actor
            .request(SessionRequest::GetState, self.query_timeout())
            .ok()?;
        let mut s = session_of(&r)?;
        // alias 以 manager 鏡像為準（`alias.set` 只改 manager 側 profile；重啟後由 YAML／template 重算）。
        if let Some(e) = lock(&self.inner).sessions.get(sid) {
            s.insert("alias", PyValue::from(e.profile.alias.clone()));
        }
        Some(s)
    }

    fn session_public_checked(&self, sid: &str) -> Result<PyDict, PyErr> {
        self.send_console_scan(sid);
        let actor = self
            .actor_of(sid)
            .ok_or_else(|| PyErr::value_error(format!("session actor unavailable: {sid}")))?;
        let r = actor.request(SessionRequest::GetState, self.query_timeout())?;
        let mut s = session_of(&r)
            .ok_or_else(|| PyErr::value_error(format!("session state unavailable: {sid}")))?;
        if let Some(e) = lock(&self.inner).sessions.get(sid) {
            s.insert("alias", PyValue::from(e.profile.alias.clone()));
        }
        Ok(s)
    }

    fn session_state(&self, sid: &str) -> Option<String> {
        self.session_public(sid).and_then(|s| str_of(&s, "state"))
    }

    fn send_control(&self, sid: &str, ev: ControlEvent) {
        if let Some(actor) = self.actor_of(sid) {
            let _ = actor.control.send(ev);
        }
    }

    /// 建立 session（actor＋executor）並登錄到鏡像；須持 `Inner` 鎖呼叫（`inner` 即該鎖內容）。
    #[allow(clippy::too_many_arguments)]
    fn create_session_locked(
        self: &Arc<Self>,
        inner: &mut Inner,
        profile: SessionProfile,
        profile_source: &str,
        released_placeholder: bool,
        released: Option<&ReleasedRow>,
        detached_error: Option<String>,
        preopened: Option<Preopened>,
        sid_override: Option<&str>,
    ) -> String {
        let sid = sid_override
            .map(str::to_string)
            .unwrap_or_else(|| format!("{}:{}", profile.template.profile_name, profile.com));
        let fence = Fence::new();
        // P8 Task 8：重建的 session 承接該 COM 既有的 flash-critical 標記。
        let flash_critical = inner.flash_critical.contains(&profile.com);
        // P8 Task 14：交接凍結期間新建的 session（在途 attach 越過 gate 後才建立）同樣零 TX。
        let handoff_frozen = inner.handoff_frozen;
        fence.with(|f| {
            f.flash_critical = flash_critical;
            f.handoff_frozen = handoff_frozen;
        });
        self.fanout.register(fence.clone());
        let rx = RxShared::new(RX_WINDOW_MAX_CHARS, self.clock.mono_ns());
        let clock = self.clock.clone();
        let fs = self.fs.clone();
        let input_epoch_pub = Arc::new(AtomicU64::new(0));
        let deps = CoreDeps {
            fence: fence.clone(),
            registry: self.registry.clone(),
            wal: self.wal.clone(),
            rx: rx.clone(),
            epochs: self.epochs.clone(),
            wall_iso: Arc::new(move || iso_utc_now(clock.wall_unix_us())),
            rng: Arc::new(fill_random),
            env: self.env.clone(),
            read_env_file: Arc::new(move |p: &str| -> Option<std::io::Result<String>> {
                match fs.read(Path::new(p)) {
                    Ok(bytes) => Some(Ok(String::from_utf8_lossy(&bytes).into_owned())),
                    Err(e) if e.kind == PalErrorKind::NotFound => None,
                    Err(e) => Some(Err(std::io::Error::other(format!("{e:?}")))),
                }
            }),
            // P6（07 D-02）：human console 擷取時讀的 input_epoch（與 ConsoleHub 共用）。
            input_epoch_pub: input_epoch_pub.clone(),
            bg_index: self.bg_index.clone(),
            unconfirmed_closes: self.unconfirmed_closes.clone(),
        };
        let mut core = SessionCore::new(profile.clone(), profile_source, deps);
        core.set_path_flavor(self.path_flavor);
        core.set_tcp_console(self.console.kind() == sw_pal::ConsoleKind::TcpTelnet);
        if core.session_id() != sid {
            core.init_session_id(sid.clone());
        }
        if let Some(row) = released {
            let text = |v: &PyValue| match v {
                PyValue::Str(s) => Some(s.clone()),
                PyValue::Null => None,
                other => Some(format!("{other:?}")),
            };
            core.init_released(
                text(&row.released_by),
                text(&row.released_at),
                row.reason.clone(),
            );
        }
        if let Some(err) = detached_error {
            core.init_detached_error(err, self.now_iso());
        }
        #[cfg(feature = "test-fakes")]
        let history = {
            let history = Arc::new(Mutex::new(Vec::new()));
            let sink = history.clone();
            core.set_last_error_observer(Box::new(move |v: &str| lock(&sink).push(v.to_string())));
            history
        };
        let handoff_path = preopened.as_ref().map(|pre| pre.real_path.clone());
        let io = Arc::new(SessionIo {
            sid: Mutex::new(sid.clone()),
            profile: Mutex::new(profile.clone()),
            preopened: Mutex::new(preopened),
            pending_pty: Mutex::new(HashMap::new()),
            next_pty_token: AtomicU64::new(0),
        });
        let mailbox = Mailbox::new(MAILBOX_CONTROL_CAP, MAILBOX_DATA_CHUNKS_CAP);
        // P6 Task 9：每 session 一個 ConsoleHub（mux 不可用的平台不建立；Windows WSAPoll 屬 LOCAL-WINDOWS-TODO）。
        let hub = crate::human_console::hub::spawn_hub_with(
            profile.com.clone(),
            self.console.clone(),
            mailbox.control_sender(),
            mailbox.data_sender(),
            self.clock.clone(),
            input_epoch_pub,
        )
        .ok()
        .map(Arc::new);
        // agent log 檔名以平台 Clock 的本地時間組成（Python `time.strftime` 本地時間；審查 F34）。
        core.set_capture_env(crate::capture::CaptureEnv {
            clock: Some(self.clock.clone()),
            ..Default::default()
        });
        let workers: Arc<Mutex<HashMap<u64, IoWorker>>> = Arc::new(Mutex::new(HashMap::new()));
        {
            // self-test 的外部觀測（Python `bridge.snapshot()` 與 RELEASED 的 `/proc` holder 掃描）。
            let snapshot_workers = workers.clone();
            core.set_self_test_probes(SelfTestProbes {
                holders: Some(self.uart_excl.clone()),
                vtty_alive: None,
                path_exists: Some({
                    let console = self.console.clone();
                    Arc::new(move |path: &str| console.slave_path_exists(path))
                }),
                bridge_snapshot: Some(Arc::new(move |epoch: u64| {
                    lock(&snapshot_workers).get(&epoch).map(IoWorker::snapshot)
                })),
                templates: Arc::new(self.templates.clone()),
            });
        }
        let exec = ManagerExec {
            shared: self.clone(),
            io: io.clone(),
            rx,
            fence: fence.clone(),
            data: mailbox.data_sender(),
            workers: workers.clone(),
            runners: HashMap::new(),
            hub,
            pumps: HashMap::new(),
            listener_epoch: None,
            open_jobs: JobQueue::new("open"),
            persist_jobs: JobQueue::new("persist"),
            opened_rx: Arc::new(Mutex::new(HashMap::new())),
            opened_listener: Arc::new(Mutex::new(HashSet::new())),
        };
        let actor = spawn_actor(core, mailbox, Box::new(exec), self.clock.clone());
        if let Some(real_path) = handoff_path {
            // dynamic 偵測沿用的 PROBE handle 交給新 session：與 `attach_dynamic` 的作業同在本臨界區內寫入交接表
            // （Python `_attach_by_id_dynamic` 改寫作業 `session_id` 後仍留在 `_active_attach_ops`，
            // `origin/main:sw_core/session_manager.py:5644-5647`）。同一 by_id 的舊列（理論上不存在）就地關閉並告警。
            for stale in
                Self::register_handoff_locked(inner, &profile.device_by_id, &io, real_path)
            {
                self.close_session_preopened(&profile.device_by_id, stale);
            }
        }
        #[cfg(feature = "test-fakes")]
        self.spawned_workers.fetch_add(1, Ordering::SeqCst);
        inner.sessions.insert(
            sid.clone(),
            Entry {
                sid: sid.clone(),
                profile,
                profile_source: profile_source.to_string(),
                released_placeholder,
                actor: Arc::new(actor),
                fence,
                io,
                workers,
                #[cfg(feature = "test-fakes")]
                history,
            },
        );
        sid
    }

    /// `_session_from_template`：預配 COM（#100）優先，否則最小空號；act_no＝目前 session 數＋1。
    fn session_from_template_locked(
        self: &Arc<Self>,
        inner: &mut Inner,
        tpl: &ProfileTemplate,
        by_id: &str,
        source: &str,
        detached_error: Option<String>,
        preopened: Option<Preopened>,
    ) -> String {
        let com = inner
            .pending_com
            .remove(by_id)
            .unwrap_or_else(|| inner.next_dynamic_com());
        let act_no = inner.sessions.len() as i128 + 1;
        let alias = format!("{}+{act_no}", tpl.profile_name);
        let profile = profile_from_template(tpl, &com, act_no, &alias, by_id);
        let sid = self.create_session_locked(
            inner,
            profile,
            source,
            false,
            None,
            detached_error,
            preopened,
            None,
        );
        self.model_apply(StateDelta::AliasSet {
            alias: alias.clone(),
            row: Some(alias_row(&sid, &alias, &self.now_iso())),
        });
        sid
    }

    // -----------------------------------------------------------------------
    // 裝置變更與 attach
    // -----------------------------------------------------------------------

    fn handle_watcher(self: &Arc<Self>, ev: WatcherEvent) {
        match ev {
            WatcherEvent::Delta { snapshot, .. } => self.update_devices(&snapshot),
            // Python `Service._on_watcher_tick`：每輪先非阻塞服務 WAL 延後輪替（OD-19；無新流量時 append 不會發生）。
            // readiness reconcile 屬後續 Slice，不在 2.1 範圍。
            WatcherEvent::Tick => {
                self.wal.tick();
            }
            WatcherEvent::ScanFailed(e) => self.warn(format!("裝置掃描失敗：{e:?}")),
        }
    }

    fn update_devices(self: &Arc<Self>, snapshot: &[(String, DeviceInfo)]) {
        let new: BTreeMap<String, DeviceInfo> = snapshot.iter().cloned().collect();
        let mut unconsumed: Vec<(String, AttachOp)> = Vec::new();
        let prev = {
            let mut inner = lock(&self.inner);
            if inner.stopping {
                // SC-11：stopping 後熱插不 open、不建 session（自動路徑只記 log）。
                let added: Vec<String> = new
                    .keys()
                    .filter(|k| !inner.devices.contains_key(*k))
                    .cloned()
                    .collect();
                drop(inner);
                for by_id in added {
                    self.warn(format!("shutdown 中，略過 attach {by_id}"));
                }
                return;
            }
            let prev = std::mem::replace(&mut inner.devices, new.clone());
            // 裝置離線／改指：使進行中的 attach op 失效（#27）。
            for (by_id, op) in &inner.inflight {
                let invalid = match (inner.devices.get(by_id), prev.get(by_id)) {
                    (Some(cur), Some(old)) => cur.real_path != old.real_path,
                    (None, _) => true,
                    (Some(_), None) => false,
                };
                if invalid {
                    op.cancel();
                    if op.parked.swap(false, Ordering::SeqCst) {
                        unconsumed.push((by_id.clone(), op.clone()));
                    }
                }
            }
            // Python `update_devices` 先把未消費的 `_preopened_ports` 作業移出表、再關閉（關閉失敗只告警，不保留阻擋；
            // `origin/main:sw_core/session_manager.py:4045-4063`）。
            for (by_id, _) in &unconsumed {
                inner.inflight.remove(by_id);
            }
            // #100 M2/3a：離線裝置的預配 COM 釋出（#28）。
            let online: HashSet<String> = inner.devices.keys().cloned().collect();
            inner.pending_com.retain(|b, _| online.contains(b));
            prev
        };
        if !unconsumed.is_empty() {
            for (by_id, op) in unconsumed {
                if let Some(pre) = op.take_preopened() {
                    self.close_session_preopened(&by_id, pre);
                }
            }
            self.cv.notify_all();
        }
        // Python `_mark_missing_devices_locked`（審查 F60）：與 Python 同在 detach／attach 之前標記。
        let missing: Vec<String> = {
            let inner = lock(&self.inner);
            inner
                .sessions
                .values()
                .filter(|e| {
                    !e.profile.device_by_id.is_empty() && !new.contains_key(&e.profile.device_by_id)
                })
                .map(|e| e.sid.clone())
                .collect()
        };
        for sid in &missing {
            self.send_control(sid, ControlEvent::DeviceMissing);
        }
        let changed: Vec<String> = prev
            .iter()
            .filter(|(k, v)| new.get(*k).is_some_and(|n| n.real_path != v.real_path))
            .map(|(k, _)| k.clone())
            .collect();
        let removed: Vec<String> = prev
            .keys()
            .filter(|k| !new.contains_key(*k))
            .cloned()
            .collect();
        let added: Vec<String> = new
            .keys()
            .filter(|k| !prev.contains_key(*k))
            .cloned()
            .collect();
        for by_id in &removed {
            self.detach_by_id(by_id, DetachReason::Removed);
        }
        for by_id in &changed {
            self.detach_by_id(by_id, DetachReason::Rebound);
        }
        for by_id in added.iter().chain(changed.iter()) {
            self.spawn_attach(by_id);
        }
    }

    /// `_detach_by_id`：未交給正式 bridge 的預開 handle 立即關閉（#27），並通知 actor drain。交接表的 handle 在 Python
    /// 屬 `_active_attach_ops` 的作業：`update_devices` 取消（`origin/main:sw_core/session_manager.py:4035-4043`），由持有
    /// 作業的 attach 中止並 `_finish_attach_op`，關閉未確認即保留（`:4322-4332`）；未取走者由此代為關閉，已取走者由開埠
    /// 路徑與 actor 的 `DeviceDetached` 收尾。
    fn detach_by_id(&self, by_id: &str, reason: DetachReason) {
        let (claimed, target) = {
            let mut inner = lock(&self.inner);
            let claimed = self.claim_handoff_for_close_locked(&mut inner, by_id, false);
            let target = inner
                .session_by_id(by_id)
                .map(|e| (e.sid.clone(), e.io.clone()));
            (claimed, target)
        };
        if let HandoffCloseClaim::Unclaimed { closing, .. } = claimed {
            self.cv.notify_all();
            if let Some(op) = closing {
                self.close_claimed_handoff(by_id, &op);
            }
        }
        let Some((sid, io)) = target else {
            return;
        };
        if let Some(pre) = lock(&io.preopened).take() {
            self.close_session_preopened(by_id, pre);
        }
        self.send_control(
            &sid,
            ControlEvent::DeviceDetached {
                by_id: by_id.to_string(),
                reason,
            },
        );
    }

    /// `_spawn_attach`：stopping gate、released／inflight 去重；需要再解析或尚無 session 時起 attach worker。
    fn spawn_attach(self: &Arc<Self>, by_id: &str) {
        self.spawn_attach_with(by_id, false, false, None);
    }

    /// `spawn_attach` 本體；`after_clear` 見 `AttachOp::after_clear`。
    fn spawn_attach_with(
        self: &Arc<Self>,
        by_id: &str,
        after_clear: bool,
        dynamic_only: bool,
        mut preopened: Option<Preopened>,
    ) {
        let released = self.released_by_ids();
        let pins = self.model_snapshot().profile_pins;
        let mut inner = lock(&self.inner);
        if inner.stopping {
            drop(inner);
            if let Some(pre) = preopened {
                let _ = pre.parts.close();
            }
            self.warn(format!("shutdown 中，略過 attach {by_id}"));
            return;
        }
        // wp510 第 13 項：未確認關閉的 by_id 不再 attach（Python `_spawn_attach` 見 `_draining_by_ids` 即早退，
        // `origin/main:sw_core/session_manager.py:4113-4114`）。
        if self.unconfirmed_closes.contains(by_id) {
            drop(inner);
            if let Some(pre) = preopened {
                let _ = pre.parts.close();
            }
            return;
        }
        // OD-63：佔位標記是自動 attach 的權威防線；即使 state.json 的 RELEASED 列已遺失也不可重掛。
        if inner
            .session_by_id(by_id)
            .is_some_and(|e| e.released_placeholder)
        {
            drop(inner);
            if let Some(pre) = preopened {
                let _ = pre.parts.close();
            }
            return;
        }
        if released.contains(by_id) {
            drop(inner);
            if let Some(pre) = preopened {
                let _ = pre.parts.close();
            }
            return;
        }
        if inner
            .inflight
            .get(by_id)
            .is_some_and(|op| op.parked.swap(false, Ordering::SeqCst))
        {
            // 未消費的預開作業（佔位退休落盤失敗留下）：Python `_attach_by_id` 先自 `_preopened_ports` 取走作業，以同一
            // handle 續行 attach（`origin/main:sw_core/session_manager.py:4852-4854`）。handle 在本臨界區內交給新作業或
            // session 的交接列，不離開 manager 的追蹤。
            let parked = inner.inflight.remove(by_id).and_then(|op| op.take_preopened());
            if let Some(pre) = preopened.take() {
                let _ = pre.parts.close();
            }
            preopened = parked;
        }
        if let Some(op) = inner.inflight.get(by_id) {
            if after_clear {
                op.after_clear.store(true, Ordering::SeqCst);
            }
            drop(inner);
            if let Some(pre) = preopened {
                let _ = pre.parts.close();
            }
            return;
        }
        let Some(real) = inner.device_path(by_id).map(str::to_string) else {
            drop(inner);
            if let Some(pre) = preopened {
                let _ = pre.parts.close();
            }
            return;
        };
        if let Some(e) = inner.session_by_id(by_id) {
            let pin_differs = pins
                .get(by_id)
                .and_then(|p| self.template_by_name(p))
                .is_some_and(|t| t.profile_name != e.profile.template.profile_name);
            let needs_reresolve = e.profile_source != "yaml-target"
                && (pin_differs || (e.profile_source == "fallback" && !self.templates.is_empty()));
            if !needs_reresolve || preopened.is_some() {
                let actor = e.actor.clone();
                let io = e.io.clone();
                let mut to_close = Vec::new();
                if let Some(pre) = preopened {
                    // 接手的預開 handle 交給 session 的正式 bridge（Python `_attach_by_id` 以 `preopened_op.port` 建
                    // bridge，`:4982`）：移出在途表與寫入交接表在同一臨界區（`:4852-4854`）；路徑已改變者關閉。
                    // 只有未消費的預開作業會帶 handle 進來。
                    if pre.real_path == real {
                        to_close = Self::place_handoff_locked(&mut inner, by_id, &io, pre);
                    } else {
                        to_close.push(pre);
                    }
                }
                drop(inner);
                for pre in to_close {
                    self.close_session_preopened(by_id, pre);
                }
                self.cv.notify_all();
                let _ = actor.control.send(ControlEvent::DeviceAttached {
                    by_id: by_id.to_string(),
                    real_path: real,
                });
                return;
            }
        }
        let op = AttachOp::new(
            by_id,
            &self.unconfirmed_closes,
            after_clear,
            dynamic_only,
            preopened.map(|pre| Preopened {
                real_path: real.clone(),
                parts: pre.parts,
            }),
        );
        inner.inflight.insert(by_id.to_string(), op.clone());
        drop(inner);
        self.spawn_attach_worker(by_id, op);
    }

    /// 啟動已登錄於 `inflight` 的 attach op（自動 attach 路徑）。執行緒建立失敗沒有 RPC 可回報：告警留下診斷後
    /// 關閉預開 handle 並收尾作業（關閉失敗仍保留阻擋）。
    fn spawn_attach_worker(self: &Arc<Self>, by_id: &str, op: AttachOp) {
        match self.prepare_attach_worker(by_id, &op) {
            Ok(worker) => worker.start(),
            Err(_) => {
                let close_receipt = op.close_preopened_with_receipt();
                self.finish_attach_op(by_id, &op, close_receipt);
            }
        }
    }

    /// 先建立 attach worker 執行緒、放行（`PreparedAttachWorker::start`）前不執行 attach 本體也不碰作業；drop 未放行的
    /// worker 即令執行緒直接結束。Python `_spawn_attach` 的 `worker.start()` 例外會穿出呼叫端（`device.attach` 經
    /// `origin/main:sw_core/session_manager.py:3633-3638` 同步呼叫，例外見 `:4147-4150`），因此建立失敗以 by_id 與
    /// OS 錯誤告警並回傳錯誤，由呼叫端決定 RPC 回應與還原。
    fn prepare_attach_worker(
        self: &Arc<Self>,
        by_id: &str,
        op: &AttachOp,
    ) -> std::io::Result<PreparedAttachWorker> {
        let (go, go_rx) = crossbeam_channel::bounded::<()>(1);
        let me = self.clone();
        let key = by_id.to_string();
        let worker_op = op.clone();
        #[cfg(feature = "test-fakes")]
        let injected = self.fail_next_attach_spawn.swap(false, Ordering::SeqCst);
        #[cfg(not(feature = "test-fakes"))]
        let injected = false;
        let spawned = if injected {
            Err(std::io::Error::from(std::io::ErrorKind::WouldBlock))
        } else {
            std::thread::Builder::new()
                .name(format!("serialwrap-attach-{by_id}"))
                .spawn(move || {
                    if go_rx.recv().is_err() {
                        return;
                    }
                    let _ = catch_unwind(AssertUnwindSafe(|| me.attach_worker(&key, &worker_op)));
                    let close_receipt = worker_op.close_preopened_with_receipt();
                    me.finish_attach_op(&key, &worker_op, close_receipt);
                })
        };
        match spawned {
            Ok(_) => {
                // 只計實際建立的 attach worker 執行緒；建立失敗不計（審查 P8X-8b5cbe4-2）。
                #[cfg(feature = "test-fakes")]
                self.spawned_workers.fetch_add(1, Ordering::SeqCst);
                Ok(PreparedAttachWorker { go })
            }
            Err(error) => {
                self.warn(format!("attach worker 建立失敗（by_id={by_id}）：{error}"));
                Err(error)
            }
        }
    }

    /// attach worker 本體（`_attach_by_id`）：既有 session → #181 再解析；否則 DETACHED-rebind 或動態偵測。
    fn attach_worker(self: &Arc<Self>, by_id: &str, op: &AttachOp) {
        let existing = {
            let inner = lock(&self.inner);
            match inner.session_by_id(by_id) {
                Some(e) if e.released_placeholder => return,
                Some(e) => Some(e.sid.clone()),
                None => None,
            }
        };
        if let Some(sid) = existing {
            self.reattach_existing(&sid, by_id, op);
            return;
        }
        if !op.dynamic_only {
            // DETACHED-rebind fallback：DETACHED 且其 by_id 離線的 session，取 act_no 最小者。
            let candidates: Vec<(String, i128)> = {
                let inner = lock(&self.inner);
                inner
                    .sessions
                    .values()
                    .filter(|e| {
                        !e.released_placeholder
                            && !inner.devices.contains_key(&e.profile.device_by_id)
                    })
                    .map(|e| (e.sid.clone(), pyint_i128(&e.profile.act_no)))
                    .collect()
            };
            let mut detached: Vec<(String, i128)> = candidates
                .into_iter()
                .filter(|(sid, _)| self.session_state(sid).as_deref() == Some("DETACHED"))
                .collect();
            detached.sort_by_key(|(_, act)| *act);
            if let Some((sid, _)) = detached.into_iter().next() {
                if !op.cancelled.load(Ordering::SeqCst) {
                    self.rebind_detached(&sid, by_id, op);
                }
                return;
            }
        }
        if self.templates.is_empty() {
            let _ = op.close_preopened_with_receipt();
            return;
        }
        if lock(&self.inner).sessions.len() >= self.max_sessions {
            self.warn(format!(
                "已達 max_sessions={}，忽略裝置 {by_id}",
                self.max_sessions
            ));
            let _ = op.close_preopened_with_receipt();
            return;
        }
        self.attach_dynamic(by_id, op);
    }

    /// DETACHED-rebind：改綁 session 到新 by_id，寫 binding override，然後 attach。作業帶有預開 handle（接手的未消費
    /// 預開作業）時交給該 session 的正式 bridge（Python `_attach_by_id` 改綁後沿用 `preopened_op`，
    /// `origin/main:sw_core/session_manager.py:4857-4866`、`:4982`）。
    fn rebind_detached(self: &Arc<Self>, sid: &str, by_id: &str, op: &AttachOp) {
        let r = self.request(
            sid,
            SessionRequest::Bind {
                by_id: by_id.to_string(),
            },
            QUERY_TIMEOUT,
        );
        if !r.as_ref().is_ok_and(is_ok) {
            return;
        }
        let displaced = {
            let mut inner = lock(&self.inner);
            let io = inner.sessions.get_mut(sid).map(|e| {
                e.profile.device_by_id = by_id.to_string();
                lock(&e.io.profile).device_by_id = by_id.to_string();
                e.io.clone()
            });
            match io {
                // 作業仍在在途表（worker 收尾才移除），handle 於同一臨界區寫入交接表；已取消者留在作業內由 worker
                // 收尾關閉（Python `_attach_by_id` 見 `cancelled` 中止並 `_finish_attach_op`，`:4933-4944`）。
                Some(io) if !op.cancelled.load(Ordering::SeqCst) => match op.take_preopened() {
                    Some(pre) => Self::place_handoff_locked(&mut inner, by_id, &io, pre),
                    None => Vec::new(),
                },
                _ => Vec::new(),
            }
        };
        for pre in displaced {
            self.close_session_preopened(by_id, pre);
        }
        if let Err(e) = self.commit_sync(|m| {
            m.apply(StateDelta::Binding {
                sid: sid.to_string(),
                by_id: Some(by_id.to_string()),
            })
        }) {
            self.warn(format!("state.json 寫入失敗（binding {sid}）：{e:?}"));
        }
        self.send_attached(by_id);
    }

    /// 對 by_id 的 session 送 `DeviceAttached`（裝置在線且未 stopping 時）。
    fn send_attached(&self, by_id: &str) {
        let target = {
            let inner = lock(&self.inner);
            if inner.stopping
                || inner
                    .session_by_id(by_id)
                    .is_some_and(|e| e.released_placeholder)
            {
                None
            } else {
                match (inner.session_by_id(by_id), inner.device_path(by_id)) {
                    (Some(e), Some(real)) => Some((e.actor.clone(), real.to_string())),
                    _ => None,
                }
            }
        };
        if let Some((actor, real)) = target {
            let _ = actor.control.send(ControlEvent::DeviceAttached {
                by_id: by_id.to_string(),
                real_path: real,
            });
        }
    }

    /// 開埠 gate（R1-F2）：持 manager 鎖檢查 stopping／取消／裝置路徑，通過後才 `open_exclusive`
    /// （PROBE 以 `UartProfile` 預設值開啟）。`None`＝gate 拒絕（未呼叫 open）。
    fn gated_open(
        &self,
        by_id: &str,
        real: &str,
        op: &AttachOp,
    ) -> Option<Result<Box<dyn SerialHandle>, OpenError>> {
        if !self.still_valid(by_id, real, op) {
            return None;
        }
        Some(self.serial.open_exclusive(
            &DevicePath(real.to_string()),
            &serial_config(&UartProfile::default()),
        ))
    }

    /// gate 條件：未 stopping、op 未取消、by_id 無未確認關閉、裝置仍在線且 real_path 未變（Python
    /// `_preopen_dynamic_attach_port`／`_attach_by_id_dynamic` 的 `_draining_by_ids` 檢查，
    /// `origin/main:sw_core/session_manager.py:4258`、`:4299`、`:5523`）。
    fn still_valid(&self, by_id: &str, real: &str, op: &AttachOp) -> bool {
        let inner = lock(&self.inner);
        !inner.stopping
            && !op.cancelled.load(Ordering::SeqCst)
            && !self.unconfirmed_closes.contains(by_id)
            && inner.device_path(by_id) == Some(real)
    }

    /// OD-63：以 device.attach 的獨占開埠取得佔位裝置，再將同一 handle 交給 dynamic 偵測與正式 bridge。
    fn attach_released_placeholder(
        self: &Arc<Self>,
        sid: &str,
        force: bool,
        request_selector: Option<&str>,
    ) -> Result<PyDict, PyErr> {
        let (by_id, real, selector, uart, already_inflight) = {
            let inner = lock(&self.inner);
            if inner.stopping {
                return Err(stopping_error());
            }
            let Some(entry) = inner.sessions.get(sid) else {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "selector" => request_selector.unwrap_or(sid).to_string()
                });
            };
            if !entry.released_placeholder {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "selector" => request_selector.unwrap_or(sid).to_string()
                });
            }
            let by_id = entry.profile.device_by_id.clone();
            let selector = request_selector
                .unwrap_or(entry.profile.com.as_str())
                .to_string();
            let Some(real) = inner.device_path(&by_id).map(str::to_string) else {
                return Ok(device_not_present_reply(&selector, &by_id));
            };
            (
                by_id.clone(),
                real,
                selector,
                entry.profile.template.uart.clone(),
                // Python `attach_device` 先檢查 `_draining_by_ids`（`origin/main:sw_core/session_manager.py:3410-3417`）。
                inner.inflight.contains_key(&by_id) || self.unconfirmed_closes.contains(&by_id),
            )
        };
        let Some(mut public) = self.session_public(sid) else {
            return Ok(pydict! {
                "ok" => false,
                "error_code" => "SESSION_NOT_FOUND",
                "selector" => request_selector.unwrap_or(sid).to_string()
            });
        };
        if already_inflight {
            return Ok(session_recovering_reply(public));
        }

        if !force {
            let holder = self.uart_excl.probe_holders(&real);
            if !holder.pids.is_empty() {
                return Ok(device_still_held_reply(&selector, holder.pids));
            }
        }

        let op = AttachOp::new(&by_id, &self.unconfirmed_closes, false, true, None);
        {
            let mut inner = lock(&self.inner);
            if inner.stopping {
                return Err(stopping_error());
            }
            let still_placeholder = inner.sessions.get(sid).is_some_and(|entry| {
                entry.released_placeholder && entry.profile.device_by_id == by_id
            });
            if !still_placeholder {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "selector" => selector
                });
            }
            if inner.inflight.contains_key(&by_id) || self.unconfirmed_closes.contains(&by_id) {
                return Ok(session_recovering_reply(public));
            }
            if inner.device_path(&by_id) != Some(real.as_str()) {
                return Ok(device_not_present_reply(&selector, &by_id));
            }
            inner.inflight.insert(by_id.clone(), op.clone());
        }

        // wp510 第五次整包審查 Medium：交接用的 attach worker 先於開埠與退休建立。Python 在退休並存檔後才
        // `_spawn_attach`，`worker.start()` 例外穿出 RPC（`origin/main:sw_core/session_manager.py:3633-3638`、
        // `:4147-4150`）；這裡在任何副作用前取得執行緒，建立失敗即回 RPC 錯誤，佔位與持久化阻擋原樣保留，
        // 不會出現新舊 session 皆不存在。未放行的 worker 於各提前返回路徑 drop 後自行結束。
        let worker = match self.prepare_attach_worker(&by_id, &op) {
            Ok(worker) => worker,
            Err(_) => {
                self.finish_attach_op(&by_id, &op, None);
                return Err(PyErr {
                    message: THREAD_START_FAILED.to_string(),
                });
            }
        };

        let handle = match self
            .serial
            .open_exclusive(&DevicePath(real.clone()), &serial_config(&uart))
        {
            Ok(handle) => handle,
            Err(OpenError::Held(holder)) | Err(OpenError::AccessDenied { holder, .. }) => {
                self.finish_attach_op(&by_id, &op, None);
                return Ok(device_still_held_reply(&selector, holder.pids));
            }
            Err(OpenError::NotFound(_)) => {
                self.finish_attach_op(&by_id, &op, None);
                return Ok(device_not_present_reply(&selector, &by_id));
            }
            Err(OpenError::Other(error)) => {
                let retired = {
                    let mut inner = lock(&self.inner);
                    Self::retire_released_placeholder_locked(
                        &mut inner,
                        sid,
                        &by_id,
                        &real,
                        &op,
                        None,
                    )
                };
                let Some(retired) = retired else {
                    self.finish_attach_op(&by_id, &op, None);
                    let inner = lock(&self.inner);
                    if inner.device_path(&by_id) != Some(real.as_str()) {
                        return Ok(device_not_present_reply(&selector, &by_id));
                    }
                    drop(inner);
                    let current = self.session_public(sid).unwrap_or(public);
                    return Ok(session_recovering_reply(current));
                };

                // Python `:3535-3537`：`session.state = "DETACHED"`（setter 寫入時間）→ last_error → detached_at。
                set_public_state(&mut public, "DETACHED", self.now_iso());
                set_public_last_error(
                    &mut public,
                    PyValue::from(format!("ATTACH_FAILED:{}", error.py_exc_name())),
                );
                public.insert("detached_at", PyValue::from(self.now_iso()));
                public.insert("released_by", PyValue::Null);
                public.insert("released_at", PyValue::Null);
                public.insert("released_reason", PyValue::Null);

                // Python `:3554-3556`：先 `_finish_attach_op`，`_save_state()` 例外穿出 RPC；記憶體中的退休不回滾，
                // 其後的 `_spawn_attach`（`:3557-3558`）不執行（未放行的 worker 於返回時 drop）。
                let persisted = self.persist_placeholder_retirement(sid);
                if persisted.is_err() {
                    self.finish_attach_op(&by_id, &op, None);
                }
                self.stop_retired_placeholder_actor(sid, &retired);
                persisted?;
                // Python `_spawn_attach(by_id, dynamic_only=True)` 會以原 COM 重試 dynamic。
                // 保留目前 inflight op，避免 watcher 在退休與重試之間搶先建立一般 attach。
                worker.start();
                return Ok(pydict! { "ok" => true, "session" => public });
            }
        };
        let mut parts = Some(HandleParts::split(handle));
        let retired = {
            let mut inner = lock(&self.inner);
            Self::retire_released_placeholder_locked(
                &mut inner,
                sid,
                &by_id,
                &real,
                &op,
                Some(&mut parts),
            )
        };
        let Some(retired) = retired else {
            let close_receipt = parts.map(|parts| op.close_parts(parts));
            self.finish_attach_op(&by_id, &op, close_receipt);
            let inner = lock(&self.inner);
            if inner.device_path(&by_id) != Some(real.as_str()) {
                return Ok(device_not_present_reply(&selector, &by_id));
            }
            drop(inner);
            let current = self.session_public(sid).unwrap_or(public);
            return Ok(session_recovering_reply(current));
        };

        // Python `:3596-3602`：清 released 欄位 → `session.state = "ATTACHING"`（`:3599`，setter 寫入時間）→ last_error。
        set_public_state(&mut public, "ATTACHING", self.now_iso());
        set_public_last_error(&mut public, PyValue::Null);
        public.insert("released_by", PyValue::Null);
        public.insert("released_at", PyValue::Null);
        public.insert("released_reason", PyValue::Null);

        if let Err(error) = self.persist_placeholder_retirement(sid) {
            // Python `:3633`：`_save_state()` 例外穿出 RPC，鎖內已完成的退休（`:3590-3607`）不回滾，已開的 UART 作業
            // 留在 `_preopened_ports`、不執行 `_spawn_attach`（`:3634-3637`）。作業以 parked 留在 `inflight`；與
            // shutdown／拔除同在 manager 鎖內判定，已 stopping、已取消或裝置已改變時照 Python 的收尾直接關閉
            // （`shutdown_drain` `:1218-1227`、`update_devices` `:4045-4063`）。
            let park = {
                let inner = lock(&self.inner);
                let park = !inner.stopping
                    && !op.cancelled.load(Ordering::SeqCst)
                    && inner.device_path(&by_id) == Some(real.as_str());
                if park {
                    op.parked.store(true, Ordering::SeqCst);
                }
                park
            };
            if !park {
                let close_receipt = op.close_preopened_with_receipt();
                self.finish_attach_op(&by_id, &op, close_receipt);
            }
            self.stop_retired_placeholder_actor(sid, &retired);
            return Err(error);
        }
        self.stop_retired_placeholder_actor(sid, &retired);

        worker.start();
        Ok(pydict! { "ok" => true, "session" => public })
    }

    /// 結束指定 attach op；不會移除同一 by_id 後續登錄的新 op。
    fn finish_attach_op(&self, by_id: &str, op: &AttachOp, close_receipt: Option<CloseReceipt>) {
        if let Some(receipt) = close_receipt.as_ref().filter(|receipt| !receipt.closed) {
            // 呼叫端直接帶入的失敗回條（未經 `close_parts`）同樣留下錯誤內容並登記未確認關閉。
            op.mark_close_failed(close_error_text(receipt));
        }
        // Python `_finish_attach_op`（`session_manager.py:4322-4332`）：關閉失敗先以 by_id 與例外告警，
        // 再保留阻擋；每次失敗的關閉只告警一次（Rust 的 handle 只關閉一次）。`close_error` 只隨 `close_failed`
        // 一起寫入，告警與否不影響下方的保留判定。
        if let Some(error) = lock(&op.close_error).take() {
            self.warn(preopened_close_warning(by_id, &error));
        }
        #[cfg(feature = "test-fakes")]
        {
            let hook = {
                let mut slot = lock(&self.finish_attach_op_pre_lock_hook);
                match slot.as_ref() {
                    Some((key, _)) if key == by_id => slot.take().map(|(_, hook)| hook),
                    _ => None,
                }
            };
            if let Some(hook) = hook {
                hook();
            }
        }
        let mut inner = lock(&self.inner);
        if inner
            .inflight
            .get(by_id)
            .is_some_and(|current| Arc::ptr_eq(&current.cancelled, &op.cancelled))
        {
            // Python `_finish_attach_op` 對任何取消原因都在 close 失敗時保留作業；不能只在 release
            // 分支檢查，否則 shutdown／裝置改指會遺失仍可能持有 UART 的阻擋。
            // 必須在 manager 鎖內重讀：`open_bridge` 的 `retain_unconfirmed_close` 於同一把鎖內把仍在表內的作業
            // 標為未確認關閉；取鎖前的快照會漏掉該標記而移除作業（wp510 P8-fdc1413-1）。鎖內讀取使兩者線性化：
            // 標記先發生則此處保留，移除先發生則 retain 另登錄新的阻擋作業。
            if op.close_failed.load(Ordering::SeqCst) {
                return;
            }
            if op.release_requested.load(Ordering::SeqCst) {
                if let Some(actor) = inner.session_by_id(by_id).map(|entry| entry.actor.clone()) {
                    // Python `_finish_attach_op` 的 release 收尾（`:4340-4363`）：source／reason 取第一個 release 請求。
                    let (release_source, release_reason) = lock(&op.release_request)
                        .clone()
                        .unwrap_or_else(|| ("cli".to_string(), PyValue::Null));
                    if let Err(error) = enqueue_safety_request(
                        &actor,
                        SessionRequest::PlaceholderAttachSettled {
                            token: op.token,
                            release_source,
                            release_reason,
                        },
                        QUERY_TIMEOUT,
                    ) {
                        // actor 無法確認清除時保留 inflight，讓後續操作維持 recovering 阻擋。
                        self.warn(format!(
                            "placeholder attach 收尾通知失敗（by_id={by_id} token={}）：{}",
                            op.token, error.message
                        ));
                        return;
                    }
                }
            }
            inner.inflight.remove(by_id);
        }
        drop(inner);
        self.cv.notify_all();
    }

    /// 鎖內退休已獨占開埠的佔位，將同一 handle 留給 dynamic-only attach worker；回傳已移出索引的佔位 entry。
    /// Python `_retire_released_placeholder_locked`（`origin/main:sw_core/session_manager.py:3640-3653`）與
    /// `_pending_com[by_id] = com`（`:3606`、`:3548`）：之後的存檔失敗不回滾（`:3633`、`:3556`）。
    fn retire_released_placeholder_locked(
        inner: &mut Inner,
        sid: &str,
        by_id: &str,
        real: &str,
        op: &AttachOp,
        parts: Option<&mut Option<HandleParts>>,
    ) -> Option<Entry> {
        let valid = !inner.stopping
            && !op.cancelled.load(Ordering::SeqCst)
            && inner.device_path(by_id) == Some(real)
            && inner
                .inflight
                .get(by_id)
                .is_some_and(|current| Arc::ptr_eq(&current.cancelled, &op.cancelled))
            && inner.sessions.get(sid).is_some_and(|entry| {
                entry.released_placeholder && entry.profile.device_by_id == by_id
            });
        if !valid {
            return None;
        }

        let entry = inner.sessions.shift_remove(sid).expect("已驗證佔位 entry");
        inner
            .pending_com
            .insert(by_id.to_string(), entry.profile.com.clone());
        inner.probe_paths.remove(sid);
        inner.interactive.retain(|_, owner| owner != sid);
        if let Some(parts) = parts {
            *lock(&op.preopened) = Some(Preopened {
                real_path: real.to_string(),
                parts: parts.take().expect("成功開埠後須交付預開 handle"),
            });
        }
        Some(entry)
    }

    /// 持久化移除舊佔位的 released、binding 與 aliases，保留 pin／detected。記憶體 model 先套用再存檔；存檔失敗
    /// 不還原（Python `_retire_released_placeholder_locked` 已改記憶體，`_save_state()` 例外不回滾，
    /// `origin/main:sw_core/session_manager.py:3640-3653`、`:1445-1471`），下一次成功存檔即寫入退休結果。
    fn persist_placeholder_retirement(&self, sid: &str) -> Result<(), PyErr> {
        let old_sid = sid.to_string();
        let (tx, rx) = crossbeam_channel::bounded(1);
        let mut model = lock(&self.model);
        {
            model.apply(StateDelta::Unreleased {
                sid: old_sid.clone(),
            });
            model.apply(StateDelta::Binding {
                sid: old_sid.clone(),
                by_id: None,
            });
            let old_aliases: Vec<String> = model
                .snapshot()
                .aliases
                .into_iter()
                .filter(|(_, row)| {
                    row.get("session_id").and_then(PyValue::as_str) == Some(old_sid.as_str())
                })
                .map(|(alias, _)| alias)
                .collect();
            for alias in old_aliases {
                model.apply(StateDelta::AliasSet { alias, row: None });
            }
        }
        self.store.commit(model.snapshot(), tx);
        let result = rx.recv().unwrap_or_else(|_| {
            Err(PalError::plain(
                PalErrorKind::BrokenPipe,
                "state store 已停止",
            ))
        });
        result.map_err(|error| commit_error(&error))
    }

    /// 舊 actor 已自 manager 索引退休；只在 manager 鎖外通知並等待它退出。
    fn stop_retired_placeholder_actor(&self, sid: &str, entry: &Entry) {
        let (reply, rx) = crossbeam_channel::bounded(1);
        if entry
            .actor
            .control
            .send_timeout(ControlEvent::Shutdown { reply }, QUERY_TIMEOUT)
            .is_err()
            || rx.recv_timeout(QUERY_TIMEOUT).is_err()
        {
            self.warn(format!("RELEASED 佔位 actor 未能及時停止（session={sid}）"));
        }
    }

    /// `_attach_by_id_dynamic`：pin > sticky > detect > fallback；PROBE 與正式 bridge 共用同一預開 handle。
    fn attach_dynamic(self: &Arc<Self>, by_id: &str, op: &AttachOp) {
        let Some(real) = lock(&self.inner).device_path(by_id).map(str::to_string) else {
            return;
        };
        let snap = self.model_snapshot();
        let mut tpl: Option<ProfileTemplate> = None;
        let mut source = "";
        if let Some(t) = snap
            .profile_pins
            .get(by_id)
            .and_then(|p| self.template_by_name(p))
        {
            tpl = Some(t);
            source = "pin";
        }
        if tpl.is_none() {
            if let Some(t) = snap
                .profile_detected
                .get(by_id)
                .and_then(|p| self.template_by_name(p))
            {
                tpl = Some(t);
                source = "sticky";
            }
        }
        let mut parts = match op.take_preopened() {
            Some(pre) if pre.real_path == real => pre.parts,
            Some(pre) => {
                let _ = op.close_parts(pre.parts);
                return;
            }
            None => {
                let handle = match self.gated_open(by_id, &real, op) {
                    None => return,
                    Some(Ok(h)) => h,
                    Some(Err(e)) => {
                        self.record_dynamic_open_failure(by_id, &real, &e, tpl.as_ref(), source);
                        return;
                    }
                };
                HandleParts::split(handle)
            }
        };
        if !self.still_valid(by_id, &real, op) {
            // 已越過 gate、open 返回時已 stopping／取消：立即關閉（零 TX）。
            let _ = op.close_parts(parts);
            return;
        }
        if tpl.is_none() {
            if let Some(t) = self.detect_template(op, &mut parts) {
                tpl = Some(t);
                source = "detected";
            }
        }
        if tpl.is_none() {
            tpl = self.default_passthrough_template();
            source = "fallback";
        }
        let Some(tpl) = tpl else {
            // 無任何可用模板：未交接的 PROBE handle 關閉（`_discard_preopened_port`）。
            let _ = op.close_parts(parts);
            return;
        };
        let mut parts = Some(parts);
        let actor = {
            let mut inner = lock(&self.inner);
            let valid = !inner.stopping
                && !op.cancelled.load(Ordering::SeqCst)
                && inner.device_path(by_id) == Some(real.as_str())
                && inner.sessions.len() < self.max_sessions
                && inner.session_by_id(by_id).is_none();
            if valid {
                let preopened = parts.take().map(|parts| Preopened {
                    real_path: real.clone(),
                    parts,
                });
                let sid = self
                    .session_from_template_locked(&mut inner, &tpl, by_id, source, None, preopened);
                inner.probe_paths.insert(sid.clone(), real.clone());
                inner.sessions.get(&sid).map(|e| e.actor.clone())
            } else {
                None
            }
        };
        if let Some(parts) = parts {
            let _ = op.close_parts(parts);
        }
        if let Some(actor) = actor {
            let _ = actor.control.send(ControlEvent::DeviceAttached {
                by_id: by_id.to_string(),
                real_path: real,
            });
        }
    }

    /// `_record_dynamic_open_failure`：動態開埠失敗保留可診斷的 DETACHED session。
    fn record_dynamic_open_failure(
        self: &Arc<Self>,
        by_id: &str,
        real: &str,
        err: &OpenError,
        tpl: Option<&ProfileTemplate>,
        source: &str,
    ) {
        if self.released_by_ids().contains(by_id) {
            return;
        }
        let mut inner = lock(&self.inner);
        if inner.stopping
            || inner.device_path(by_id) != Some(real)
            || inner.session_by_id(by_id).is_some()
            || inner.sessions.len() >= self.max_sessions
        {
            return;
        }
        let (selected, src) = match tpl {
            Some(t) => (Some(t.clone()), source),
            None => (self.default_passthrough_template(), "fallback"),
        };
        let Some(selected) = selected else {
            return;
        };
        self.session_from_template_locked(
            &mut inner,
            &selected,
            by_id,
            src,
            Some(format!("ATTACH_FAILED:{}", open_error_class(err))),
            None,
        );
    }

    /// PROBE bridge 的 `send`：經 TxWriter／Fence（單一寫入者、OS 提交於 Fence 鎖內重驗），TX WAL 由 TxWriter 以
    /// `com=PROBE` 寫入（Python PROBE bridge 的 send 同樣走 TX gate 與 WAL）。writer 於送出後原樣交回 `parts`，
    /// 維持 #51「PROBE 與正式 bridge 共用同一預開 handle」。自動 TX（`LoginProbe` 類）：WAL 不健康時由 Fence 拒絕、零 TX。
    /// 回傳是否完整送出（Python `send_command` 未拋例外；被拒或未完整時 `detect_template` 的呼叫端視為偵測失敗）。
    ///
    /// Fence 與 CancelToken 屬於所在 attach 作業（`AttachOp::probe_fence`／`probe_cancel`），shutdown、release、bind
    /// 與裝置改指經 `AttachOp::cancel()` 撤銷；凍結只會由取消者設定，這裡不得解凍（wp510 第三次整包審查 High）。
    fn probe_send(&self, op: &AttachOp, parts: &mut HandleParts, payload: &[u8]) -> bool {
        let fence = op.probe_fence.clone();
        let frozen = self.probe_tx_frozen.load(Ordering::SeqCst);
        let wal_unhealthy = self.fanout.current();
        fence.with(|f| {
            f.active_epoch = Some(0);
            // Python `_register_probe_bridge` 拒絕已取消作業：取消旗標在 fence 鎖內再讀一次。
            f.frozen = f.frozen || frozen || op.cancelled.load(Ordering::SeqCst);
            f.wal_unhealthy = wal_unhealthy;
        });
        // P8 Task 14：登記與讀取交接凍結在同一把鎖內（見 `HandoffProbes`）。
        {
            let mut probes = lock(&self.handoff_probes);
            probes.live.push(fence.clone());
            if probes.frozen {
                fence.with(|f| f.handoff_frozen = true);
            }
        }
        let registered = fence.clone();
        // sol-backfill p5 #6：比照 session fence 向全域 fanout 註冊——快照讀取之後才發布的 unhealthy 也會送達；
        // 註冊協定與通知串行化，取得的必為最新狀態（Python 於送出時才讀共用 WAL unhealthy）。
        self.fanout.register(fence.clone());
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = lock(&self.probe_pre_submit_hook).as_ref() {
            hook();
        }
        let writer = std::mem::replace(&mut parts.writer, Box::new(DetachedWriter));
        let (control_tx, _control_rx) = crossbeam_channel::unbounded();
        let cancel = CancelToken::new(parts.waker.clone());
        {
            // 先登錄再重讀旗標：取消者若早於登錄，這裡自行取消；晚於登錄則由 `AttachOp::cancel()` 取消。
            let mut slot = lock(&op.probe_cancel);
            *slot = Some(cancel.clone());
            if op.cancelled.load(Ordering::SeqCst) {
                cancel.cancel();
            }
        }
        let (handle, stop, join) = spawn_tx_writer(
            PROBE_COM.to_string(),
            writer,
            cancel,
            fence,
            self.wal.clone(),
            0,
            control_tx,
        );
        let (reply_tx, reply_rx) = crossbeam_channel::bounded(1);
        let job = TxJob {
            permit: TxPermit {
                op_id: OpId(1),
                epoch: 0,
                owner_gen: 0,
                fsm_gen: 0,
                class: TxClass::LoginProbe,
                source: "system".to_string(),
                cmd_id: None,
                log: true,
                input_epoch: None,
                replay_hold: None,
                probe_id: None,
            },
            payload: payload.to_vec(),
            reply: reply_tx,
            aggregate: None,
        };
        let sent = handle.jobs.send(job).is_ok()
            && handle.wait_report(&reply_rx).is_some_and(|report| {
                matches!(report.result.outcome, TxOutcome::Completed(n) if n >= payload.len())
            });
        stop.send();
        if let Ok((writer, _ledger)) = join.join() {
            parts.writer = writer;
        }
        *lock(&op.probe_cancel) = None;
        lock(&self.handoff_probes).live.retain(|f| !Arc::ptr_eq(f, &registered));
        sent
    }

    /// `detect_template`：送 `\n`、等待 3.0 s（Clock）、以 RX 快照依序比對候選模板的 prompt（再以 login regex 備選）。
    /// 候選排除 passthrough 與 `console_unlock_env`（Python `detect_template`）。RX／TX 以 `com=PROBE` 寫 WAL。
    fn detect_template(&self, op: &AttachOp, parts: &mut HandleParts) -> Option<ProfileTemplate> {
        let mut buf = vec![0u8; 8192];
        // `clear_rx_buffer`：丟棄開埠後已到的舊資料（仍寫 WAL）。
        let _ = self.probe_drain(parts, &mut buf);
        if !self.probe_send(op, parts, b"\n") {
            // Python：`send_command("")` 拋例外 → 呼叫端 `except: detected = None`，不睡、不比對（審查 F71）。
            return None;
        }
        self.clock.sleep(DETECT_PROBE);
        let bytes = self.probe_drain(parts, &mut buf);
        let snapshot = clean_text(&String::from_utf8_lossy(&bytes));
        let mut login_candidate: Option<ProfileTemplate> = None;
        for tpl in &self.templates {
            // `login_fsm.detect_template`：只略過 passthrough 與 console_unlock_env（P7 Task 3 退役 Slice 2.1 能力閘，
            // post_login_cmd 模板恢復為候選）。
            if tpl.platform == "passthrough"
                || tpl
                    .console_unlock_env
                    .as_deref()
                    .is_some_and(|v| !v.is_empty())
            {
                continue;
            }
            match compile(&tpl.prompt_regex, PyFlags(0)) {
                Ok(re) if re.search(&snapshot).is_some() => return Some(tpl.clone()),
                Ok(_) => {}
                Err(_) => continue,
            }
            if login_candidate.is_none() {
                if let Ok(re) = compile(&tpl.login_regex, PyFlags(0)) {
                    if re.search(&snapshot).is_some() {
                        login_candidate = Some(tpl.clone());
                    }
                }
            }
        }
        login_candidate
    }

    /// 讀完目前可讀的 PROBE RX（零等待），逐塊寫 WAL（com=PROBE），回傳讀到的位元組。
    fn probe_drain(&self, parts: &mut HandleParts, buf: &mut [u8]) -> Vec<u8> {
        let mut out = Vec::new();
        while let Ok(ReadOutcome::Data(n)) = parts.reader.read(buf, Some(Duration::ZERO)) {
            if n == 0 {
                break;
            }
            let data = &buf[..n.min(buf.len())];
            let _ = self.wal.append(WalAppendReq {
                com: PROBE_COM,
                dir: "RX",
                source: "device",
                payload: data,
                cmd_id: None,
                loss_flag: false,
                meta: None,
            });
            out.extend_from_slice(data);
        }
        out
    }

    /// 既有 session 重新 attach 前的 profile 再解析（#181 `_reresolve_profile_on_reattach`）。
    fn reattach_existing(self: &Arc<Self>, sid: &str, by_id: &str, op: &AttachOp) {
        if lock(&self.inner)
            .sessions
            .get(sid)
            .is_some_and(|e| e.released_placeholder)
        {
            return;
        }
        let Some(public) = self.session_public(sid) else {
            return;
        };
        let state = str_of(&public, "state").unwrap_or_default();
        if state == "RELEASED" || self.released_by_ids().contains(by_id) {
            return;
        }
        if state != "DETACHED" && !(op.after_clear.load(Ordering::SeqCst) && state == "ATTACHING") {
            // 已有 bridge（或 attach／drain 進行中）：不再解析（再開 PROBE 會 two-reader），交給 actor 判斷。
            // clear 收尾的 ATTACHING 尚未開埠（bridge 已關），與 Python `_attach_by_id` 同樣先再解析。
            self.send_attached(by_id);
            return;
        }
        let (source, cur_name) = {
            let inner = lock(&self.inner);
            match inner.sessions.get(sid) {
                Some(e) => (
                    e.profile_source.clone(),
                    e.profile.template.profile_name.clone(),
                ),
                None => return,
            }
        };
        let pins = self.model_snapshot().profile_pins;
        if let Some(pinned) = pins.get(by_id).and_then(|p| self.template_by_name(p)) {
            // 可解析的 pin 為最高優先：改用它並繞過偵測（契約即「繞過偵測」）。
            if pinned.profile_name != cur_name {
                self.rematerialize(sid, &pinned, "pin");
            }
            self.send_attached(by_id);
            return;
        }
        // 無法解析的 pin 視同無 pin；只有 fallback 再給一次偵測機會（boot quiet 中不偵測，#130）。
        let quiet = !matches!(
            public.get("boot_quiet_remaining_s"),
            Some(PyValue::Null) | None
        );
        if source != "fallback" || self.templates.is_empty() || quiet {
            self.send_attached(by_id);
            return;
        }
        let Some(real) = lock(&self.inner).device_path(by_id).map(str::to_string) else {
            return;
        };
        let handle = match self.gated_open(by_id, &real, op) {
            None => return,
            Some(Ok(h)) => h,
            Some(Err(_)) => {
                // 開埠失敗：交給 actor 的正式 attach 路徑回報（ATTACH_FAILED:*）。
                self.send_attached(by_id);
                return;
            }
        };
        let mut parts = HandleParts::split(handle);
        if !self.still_valid(by_id, &real, op) {
            let _ = op.close_parts(parts);
            return;
        }
        let detected = self.detect_template(op, &mut parts);
        if !self.still_valid(by_id, &real, op) {
            let _ = op.close_parts(parts);
            return;
        }
        let mut sid_now = sid.to_string();
        if let Some(t) = detected {
            if t.profile_name != cur_name {
                if let Some(new_sid) = self.rematerialize(sid, &t, "detected") {
                    sid_now = new_sid;
                }
            }
        }
        let mut inner = lock(&self.inner);
        let io = inner.sessions.get(&sid_now).map(|e| e.io.clone());
        match io {
            // 作業仍在在途表（worker 收尾才移除），handle 於同一臨界區寫入交接表；已取消者不交出（Python
            // `_attach_by_id` 見 `cancelled` 中止並 `_finish_attach_op`，`origin/main:sw_core/session_manager.py:4933-4944`）。
            Some(io) if !op.cancelled.load(Ordering::SeqCst) => {
                let displaced = Self::place_handoff_locked(
                    &mut inner,
                    by_id,
                    &io,
                    Preopened {
                        real_path: real.clone(),
                        parts,
                    },
                );
                drop(inner);
                for pre in displaced {
                    self.close_session_preopened(by_id, pre);
                }
                // Python `_attach_by_id` 的再偵測不寫 sticky（`_maybe_persist_sticky` 只在動態 attach；審查 F65）。
                self.send_attached(by_id);
            }
            _ => {
                drop(inner);
                let _ = op.close_parts(parts);
            }
        }
    }

    /// `_rematerialize_profile_locked`（#31）：換 template、重算 session_id／自動 alias，搬移以 sid 為 key 的結構；
    /// 新 sid 已被佔用時不動作。回傳新 sid（未變更回 `None`）。
    fn rematerialize(
        self: &Arc<Self>,
        sid: &str,
        tpl: &ProfileTemplate,
        source: &str,
    ) -> Option<String> {
        let (old_profile, new_sid, alias) = {
            let inner = lock(&self.inner);
            let e = inner.sessions.get(sid)?;
            let old = e.profile.clone();
            let new_sid = format!("{}:{}", tpl.profile_name, old.com);
            if new_sid != sid && inner.sessions.contains_key(&new_sid) {
                return None;
            }
            let act = pyint_i128(&old.act_no);
            let auto_alias = format!("{}+{act}", old.template.profile_name);
            let alias = if old.alias == auto_alias {
                format!("{}+{act}", tpl.profile_name)
            } else {
                old.alias.clone()
            };
            (old, new_sid, alias)
        };
        let mut profile = old_profile.clone();
        profile.template = tpl.clone();
        profile.alias = alias.clone();
        let r = self.request(
            sid,
            SessionRequest::SetProfile {
                profile: profile.clone(),
                profile_source: source.to_string(),
            },
            QUERY_TIMEOUT,
        );
        if !r.as_ref().is_ok_and(is_ok) {
            return None;
        }
        // state.json 先落盤（alias 列搬移、binding key 搬移），再於鏡像改名：外部觀察到新 sid 時檔案已一致。
        let now = self.now_iso();
        let old_sid = sid.to_string();
        let target_sid = new_sid.clone();
        let old_alias = old_profile.alias.clone();
        if let Err(e) = self.commit_sync(move |m| {
            if target_sid != old_sid {
                if let Some(b) = m.snapshot().bindings.get(&old_sid).cloned() {
                    m.apply(StateDelta::Binding {
                        sid: old_sid.clone(),
                        by_id: None,
                    });
                    m.apply(StateDelta::Binding {
                        sid: target_sid.clone(),
                        by_id: Some(b),
                    });
                }
            }
            if alias != old_alias {
                m.apply(StateDelta::AliasSet {
                    alias: old_alias.clone(),
                    row: None,
                });
            }
            m.apply(StateDelta::AliasSet {
                alias: alias.clone(),
                row: Some(alias_row(&target_sid, &alias, &now)),
            });
        }) {
            self.warn(format!("state.json 寫入失敗（rematerialize {sid}）：{e:?}"));
        }
        {
            let mut inner = lock(&self.inner);
            let entries = std::mem::take(&mut inner.sessions);
            for (k, mut e) in entries {
                if k == sid {
                    e.sid = new_sid.clone();
                    e.profile = profile.clone();
                    e.profile_source = source.to_string();
                    *lock(&e.io.sid) = new_sid.clone();
                    *lock(&e.io.profile) = profile.clone();
                    inner.sessions.insert(new_sid.clone(), e);
                } else {
                    inner.sessions.insert(k, e);
                }
            }
            if let Some(p) = inner.probe_paths.remove(sid) {
                inner.probe_paths.insert(new_sid.clone(), p);
            }
            for v in inner.interactive.values_mut() {
                if v == sid {
                    *v = new_sid.clone();
                }
            }
        }
        Some(new_sid)
    }

    // -----------------------------------------------------------------------
    // 通知
    // -----------------------------------------------------------------------

    fn handle_note(&self, note: ManagerNote) {
        match note {
            ManagerNote::SessionReady { sid, via_attach } => {
                if via_attach {
                    self.maybe_persist_sticky(&sid);
                }
            }
            ManagerNote::SessionDetached { sid, .. } => {
                let mut inner = lock(&self.inner);
                inner.interactive.retain(|_, v| *v != sid);
                // 動態 attach 的偵測紀錄只屬該次 attach（審查 F65）：bridge 結束即失效。
                inner.probe_paths.remove(&sid);
            }
            ManagerNote::InteractiveOpened {
                sid,
                interactive_id,
            } => {
                lock(&self.inner).interactive.insert(interactive_id, sid);
            }
            ManagerNote::EvictCapture { com, cmd_id } => {
                // 內部 owner 身分：以 `profile.com` 精確比對，不走使用者 selector 規則（alias 可能與他人 COM 同名；
                // sol-backfill p5 #7）。
                let actor = lock(&self.inner)
                    .sessions
                    .values()
                    .find(|e| e.profile.com == com)
                    .map(|e| e.actor.clone());
                if let Some(actor) = actor {
                    let _ = actor.control.send(ControlEvent::EvictCapture { cmd_id });
                }
            }
            ManagerNote::InteractiveClosed { interactive_id } => {
                lock(&self.inner).interactive.remove(&interactive_id);
            }
            ManagerNote::StickyCandidate { .. } => {}
            // merge P7：P7 Task 0 宣告、Task 9（recover）才發出；接線隨 Task 9。
            ManagerNote::RequestAttach { .. } => {}
        }
    }

    /// `_maybe_persist_sticky`：只有動態 attach 本身到 READY 的正向偵測（`detected`）且 real_path 與偵測當時相同
    /// 才寫入；偵測紀錄只消費一次（Python 只在 `_attach_by_id_dynamic` 呼叫，審查 F65）。
    fn maybe_persist_sticky(&self, sid: &str) {
        let (by_id, name) = {
            let mut inner = lock(&self.inner);
            let Some(probe) = inner.probe_paths.remove(sid) else {
                return;
            };
            let Some(e) = inner.sessions.get(sid) else {
                return;
            };
            if e.profile_source != "detected" {
                return;
            }
            if inner.device_path(&e.profile.device_by_id) != Some(probe.as_str()) {
                return;
            }
            (
                e.profile.device_by_id.clone(),
                e.profile.template.profile_name.clone(),
            )
        };
        if self.model_snapshot().profile_detected.get(&by_id) == Some(&name) {
            return;
        }
        if let Err(e) = self.commit_sync(|m| {
            m.apply(StateDelta::Detected {
                by_id: by_id.clone(),
                profile: name.clone(),
            })
        }) {
            self.warn(format!(
                "state.json 寫入失敗（profile_detected {by_id}）：{e:?}"
            ));
        }
    }

    /// `SessionManager.__init__`：YAML targets（#186 override 清理）、RELEASED 實體化（含動態 session）、啟動存檔。
    fn bootstrap_sessions(self: &Arc<Self>, profiles: &[SessionProfile]) {
        let snap = self.model_snapshot();
        let unmaterialized: IndexMap<String, ReleasedRow> =
            lock(&self.model).unmaterialized_released().clone();
        let declared: HashSet<&str> = profiles
            .iter()
            .map(|p| p.device_by_id.as_str())
            .filter(|b| !b.is_empty())
            .collect();
        let now = self.now_iso();
        let mut inner = lock(&self.inner);
        for p in profiles {
            let sid = format!("{}:{}", p.template.profile_name, p.com);
            let mut override_by = snap.bindings.get(&sid).cloned();
            if let Some(o) = &override_by {
                // #186：override 指向另一 target 宣告的 by_id → 過期，刪除並改用自己的宣告值。
                if *o != p.device_by_id && declared.contains(o.as_str()) {
                    self.model_apply(StateDelta::Binding {
                        sid: sid.clone(),
                        by_id: None,
                    });
                    override_by = None;
                }
            }
            let by_id = override_by.unwrap_or_else(|| p.device_by_id.clone());
            if by_id.is_empty() {
                continue;
            }
            let mut profile = p.clone();
            profile.device_by_id = by_id;
            if !inner.sessions.contains_key(&sid) {
                let released = unmaterialized.get(&sid);
                self.create_session_locked(
                    &mut inner,
                    profile.clone(),
                    "yaml-target",
                    false,
                    released,
                    None,
                    None,
                    None,
                );
                if let Some(row) = released {
                    self.model_apply(StateDelta::Released {
                        sid: sid.clone(),
                        row: row.clone(),
                    });
                }
            }
            self.model_apply(StateDelta::AliasSet {
                alias: profile.alias.clone(),
                row: Some(alias_row(&sid, &profile.alias, &now)),
            });
        }
        for (sid, row) in &unmaterialized {
            if inner.sessions.contains_key(sid) {
                continue;
            }
            let Some(by_id) = row.by_id_str().map(str::to_string) else {
                self.warn(format!(
                    "released session 無法還原（session={sid}、缺少 by_id），保留持久化阻擋"
                ));
                continue;
            };
            let (sid_profile, sid_com) = match sid.rsplit_once(':') {
                Some((p, c)) => (p.to_string(), c.to_string()),
                None => (String::new(), sid.clone()),
            };
            let pinned = snap.profile_pins.get(&by_id).cloned();
            let detected = snap.profile_detected.get(&by_id).cloned();
            let template_name = pinned
                .clone()
                .or_else(|| detected.clone())
                .unwrap_or_else(|| sid_profile.clone());
            let tpl = self
                .template_by_name(&template_name)
                .or_else(|| self.template_by_name(&sid_profile));
            let released_placeholder =
                tpl.is_none() && sid_profile != "passthrough" && sid_profile != "others-template";
            let tpl = match tpl {
                Some(tpl) => tpl,
                None => {
                    // 舊 state.json 不保存 template 內容；通用名稱沿用最小 passthrough，其他缺失 template
                    // 以 RELEASED 佔位保存持久化阻擋，供後續 device.attach 解除（OD-63）。
                    if released_placeholder {
                        self.warn(format!(
                            "released session 的 template 不存在（session={sid}、profile={template_name}），保留持久化阻擋"
                        ));
                    }
                    minimal_passthrough_template(&sid_profile)
                }
            };
            if sid_com.is_empty() {
                self.warn(format!(
                    "released session 無法還原（session={sid}、缺少 COM），保留持久化阻擋"
                ));
                continue;
            }
            let aliases = lock(&self.model).snapshot().aliases;
            let alias = aliases
                .iter()
                .find(|(_, r)| r.get("session_id").and_then(PyValue::as_str) == Some(sid.as_str()))
                .map(|(name, _)| name.clone());
            let mut act_no: Option<i128> = None;
            if let Some(a) = &alias {
                if a.starts_with(&format!("{}+", tpl.profile_name)) {
                    let suffix = a.rsplit('+').next().unwrap_or("");
                    if !suffix.is_empty() && suffix.chars().all(|c| c.is_ascii_digit()) {
                        act_no = suffix.parse().ok();
                    }
                }
            }
            let act_no = act_no.unwrap_or_else(|| {
                let upper = sid_com.to_ascii_uppercase();
                match upper
                    .strip_prefix("COM")
                    .filter(|d| !d.is_empty() && d.chars().all(|c| c.is_ascii_digit()))
                {
                    Some(d) => d.parse::<i128>().map(|n| n + 1).unwrap_or(1),
                    None => inner.sessions.len() as i128 + 1,
                }
            });
            let alias = alias
                .filter(|a| !a.is_empty())
                .unwrap_or_else(|| format!("{}+{act_no}", tpl.profile_name));
            let source = if pinned.is_some() {
                "pin"
            } else if detected.is_some() {
                "detected"
            } else {
                "fallback"
            };
            let profile = profile_from_template(&tpl, &sid_com, act_no, &alias, &by_id);
            self.create_session_locked(
                &mut inner,
                profile,
                source,
                released_placeholder,
                Some(row),
                None,
                None,
                Some(sid),
            );
            self.model_apply(StateDelta::AliasSet {
                alias: alias.clone(),
                row: Some(alias_row(sid, &alias, &now)),
            });
            self.model_apply(StateDelta::Released {
                sid: sid.clone(),
                row: row.clone(),
            });
        }
        drop(inner);
        if self.lone_surrogates {
            // Python `__init__` 尾端 `_save_state`：payload 含孤立 surrogate 時 UTF-8 文字模式寫入拋
            // `UnicodeEncodeError`（未捕捉 → daemon rc 1）；temp 檔清除、原 state.json 不動（審查 47c7b25 #1）。
            let payload = String::from_utf8_lossy(&serialize_state(&self.model_snapshot())).into_owned();
            if let Some(msg) = lone_surrogate_encode_error(&payload) {
                *lock(&self.startup_error) = Some(msg);
                return;
            }
        }
        if let Err(e) = self.commit_sync(|_| {}) {
            // Python 建構子尾端 `_save_state()` 的 OSError（EACCES／ENOSPC 等）未捕捉 → daemon traceback、rc 1，
            // 尚未掃描／attach（sol-backfill p5 #4）；daemon 以 `startup_error()` 判定並以此末行結束。
            *lock(&self.startup_error) = Some(format!("{}: {}", e.py_exc_name(), e.py_message));
        }
    }
}

// ---------------------------------------------------------------------------
// actor executor
// ---------------------------------------------------------------------------

/// 產品 executor：把 `SessionCore` 的效果落到 PAL（開埠、IoWorker、登入、state.json、WAL）。
struct ManagerExec {
    shared: Arc<Shared>,
    io: Arc<SessionIo>,
    rx: Arc<RxShared>,
    fence: Arc<Fence>,
    data: Sender<DataEvent>,
    /// 各 epoch 的 IoWorker；與 self-test 的 bridge 快照探針共用（審查 F32：`snapshot()` 的 running／serial_alive）。
    workers: Arc<Mutex<HashMap<u64, IoWorker>>>,
    runners: HashMap<u64, OpRunner>,
    /// P6 Task 9：該 session 的 ConsoleHub（RX 分流、console I/O）。
    hub: Option<Arc<crate::human_console::hub::HubHandle>>,
    /// P6 Task 6（07 D-15(5)）：每個 epoch 一條 human TX pump。
    pumps: HashMap<u64, crate::human_console::human_tx::HumanTxPump>,
    /// TCP console listener 所屬的 bridge epoch（該 epoch `BeginDrain` 時停止，ephemeral port，UIO-12.8-c）。
    listener_epoch: Option<u64>,
    /// 開埠工作者：`OpenBridge` 的同步開埠在 actor 外執行，完成以 `BridgeOpened` 回報（sol-backfill p5 #2）。
    open_jobs: JobQueue,
    /// 持久化工作者：`Persist` 的 commit 等待在 actor 外執行，完成以 `StateCommitted` 回報（同上）。
    persist_jobs: JobQueue,
    /// 開埠工作者建立、尚待 `AdoptBridge` 接手的 RX 視窗（epoch → 視窗；接手時成為本 executor 的 `rx`）。
    opened_rx: Arc<Mutex<HashMap<u64, Arc<RxShared>>>>,
    /// 開埠工作者已綁定 TCP console listener、尚待 `AdoptBridge` 接手的 epoch（P6：listener 與開埠同一工作）。
    opened_listener: Arc<Mutex<HashSet<u64>>>,
}

/// actor 外的單執行緒 FIFO 工作者（有界佇列；延遲建立，executor 卸除即結束）。
/// 佇列滿時 `submit` 回 `false`，呼叫端須立即以失敗結果回報，不得阻塞 actor。
struct JobQueue {
    name: &'static str,
    tx: Option<Sender<Box<dyn FnOnce() + Send>>>,
}

/// 每個 session 的開埠／持久化工作者佇列上限（actor 同時只會有一個 pending open；commit 依序完成）。
const JOB_QUEUE_CAP: usize = 16;

impl JobQueue {
    fn new(name: &'static str) -> JobQueue {
        JobQueue { name, tx: None }
    }

    fn submit(&mut self, job: Box<dyn FnOnce() + Send>) -> bool {
        if self.tx.is_none() {
            let (tx, rx) = crossbeam_channel::bounded::<Box<dyn FnOnce() + Send>>(JOB_QUEUE_CAP);
            let spawned = std::thread::Builder::new()
                .name(format!("serialwrap-{}", self.name))
                .spawn(move || {
                    for job in rx {
                        let _ = catch_unwind(AssertUnwindSafe(job));
                    }
                });
            if spawned.is_err() {
                return false;
            }
            self.tx = Some(tx);
        }
        self.tx.as_ref().is_some_and(|tx| tx.try_send(job).is_ok())
    }
}

/// 開埠所需、可移出 actor 的狀態。
struct OpenCtx {
    shared: Arc<Shared>,
    io: Arc<SessionIo>,
    fence: Arc<Fence>,
    data: Sender<DataEvent>,
    com: String,
    /// P6 Task 9：該 session 的 ConsoleHub（reader tap、哨兵／TCP listener 交給 hub）。
    hub: Option<Arc<crate::human_console::hub::HubHandle>>,
    /// 開埠工作者綁定 TCP console listener 的 epoch（與 executor 共用；`AdoptBridge` 接手為 `listener_epoch`）。
    opened_listener: Arc<Mutex<HashSet<u64>>>,
}

impl OpenCtx {
    /// P6 Task 9 步驟 5：PTY 後端且有 hub 時預建哨兵 PTY（隨 `OpenedBridge` 交給 core 同一事件登記；TCP／無 hub 不建，06 §7.2）。
    fn make_sentinel(&self) -> Option<crate::session_core::PtyInfo> {
        if self.hub.is_none() || self.shared.console.kind() != sw_pal::ConsoleKind::Pty {
            return None;
        }
        let pair = match self.shared.console.create_pty() {
            Ok(pair) => pair,
            Err(e) => {
                self.shared.warn(format!(
                    "哨兵 primary console 建立失敗（com={}）：{}",
                    self.com,
                    e.py_message
                ));
                return None;
            }
        };
        let token = self.io.next_pty_token.fetch_add(1, Ordering::Relaxed) + 1;
        let slave_path = pair.slave_path.clone();
        lock(&self.io.pending_pty).insert(token, pair);
        Some(crate::session_core::PtyInfo {
            token,
            slave_path,
            client_id: SessionCore::new_console_client_id(&fill_random),
        })
    }

    /// TCP console 後端：每次開埠綁一個新的 127.0.0.1 ephemeral listener 交給 hub（`uart_io.py` `start()` 於
    /// `not _pty_available()` 時 `_start_console_listener()`），回端點供 core 在 adopt 同一事件登記。
    fn bind_console_listener(&self, epoch: u64) -> Option<String> {
        let hub = self.hub.as_ref()?;
        if self.shared.console.kind() != sw_pal::ConsoleKind::TcpTelnet {
            return None;
        }
        let listener = match self.shared.console.bind_tcp_console() {
            Ok(listener) => listener,
            Err(e) => {
                self.shared.warn(format!(
                    "console listener 建立失敗（com={}）：{}",
                    self.com,
                    e.py_message
                ));
                return None;
            }
        };
        let endpoint = listener.endpoint();
        if hub
            .cmd
            .try_send(crate::human_console::hub::HubCmd::AdoptListener { listener })
            .is_err()
        {
            self.shared.warn(format!(
                "console listener 交給 hub 失敗（com={}）",
                self.com
            ));
            return None;
        }
        hub.waker.wake();
        lock(&self.opened_listener).insert(epoch);
        Some(endpoint)
    }

    /// `OpenBridge`：stopping gate → 預開 handle（同路徑則原樣交接，#51）或 `open_exclusive` → IoWorker。
    /// 預開 handle 經 `take_for_bridge` 於 manager 鎖內取得（交接列標為 `taken`），收尾一律 `settle_handoff`：
    /// 正式 bridge 接手、確認關閉，或關閉未確認時已先移入在途表（`retain_unconfirmed_close`）。
    fn open_bridge(
        &self,
        dev: DevicePath,
        cfg: SerialConfig,
        precheck_holders: bool,
        control: &Sender<ControlEvent>,
    ) -> Result<OpenedBridge, OpenFailure> {
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = lock(&self.shared.open_bridge_pre_take_hook).take() {
            hook();
        }
        let pre = match self.shared.take_for_bridge(&self.io, &dev.0) {
            // release 已撤銷交給本 session 的 handle（Python 作業 `cancelled`，`_attach_by_id` 中止、不開埠，
            // `origin/main:sw_core/session_manager.py:4933-4944`、`:4986-4996`）。
            BridgeTake::Revoked => return Err(OpenFailure::CloseBlocked),
            BridgeTake::Handle(pre) => pre,
        };
        #[cfg(feature = "test-fakes")]
        {
            // 先取出再執行，暫停期間不持有 hook 的鎖。
            let hook = lock(&self.shared.open_bridge_post_take_hook).take();
            if let Some(hook) = hook {
                hook();
            }
        }
        let result = self.open_bridge_with(dev, cfg, precheck_holders, control, pre);
        self.shared.settle_handoff(&self.io);
        result
    }

    fn open_bridge_with(
        &self,
        dev: DevicePath,
        cfg: SerialConfig,
        precheck_holders: bool,
        control: &Sender<ControlEvent>,
        pre: Option<Preopened>,
    ) -> Result<OpenedBridge, OpenFailure> {
        let stopped = || OpenFailure::Other(PalError::plain(PalErrorKind::Io, "daemon stopping"));
        let by_id = lock(&self.io.profile).device_by_id.clone();
        // 接手前關閉 handle（見 `AttachOp` 的未確認關閉盤點）：`closed:false` 以 `CloseUnconfirmed` 包住原失敗交給 actor，
        // manager 端由 `close_before_bridge` 保留阻擋作業。
        let close_or_fail = |parts: HandleParts, failure: OpenFailure| {
            if self.shared.close_before_bridge(&by_id, parts) {
                failure
            } else {
                OpenFailure::CloseUnconfirmed(Box::new(failure))
            }
        };
        if lock(&self.shared.inner).stopping {
            if let Some(pre) = pre {
                return Err(close_or_fail(pre.parts, stopped()));
            }
            return Err(stopped());
        }
        // wp510 第 13 項：actor 發出 `OpenBridge` 之後 by_id 才登記為未確認關閉（各入口的 actor 端檢查之後）：不開埠，
        // 未交接的預開 handle 一併關閉（Python `_attach_by_id` 開 bridge 前重驗 `_draining_by_ids` 即中止並經
        // `_finish_attach_op` 關閉，`origin/main:sw_core/session_manager.py:4933-4944`）。
        if self.shared.unconfirmed_closes.contains(&by_id) {
            if let Some(pre) = pre {
                return Err(close_or_fail(pre.parts, OpenFailure::CloseBlocked));
            }
            return Err(OpenFailure::CloseBlocked);
        }
        let handle: Box<dyn SerialHandle> = match pre {
            Some(pre) if pre.real_path == dev.0 => {
                // Python bridge 啟動時對預開 port 呼叫 `port.configure(self.profile)`：PROBE 以預設設定開啟，
                // 正式 bridge 前套用解析出的 template UART 設定（審查 F59）。後端未支援時沿用開啟時設定。
                match pre.parts.close.configure(&cfg) {
                    Ok(()) => {}
                    Err(error) if error.kind == PalErrorKind::Unsupported => {}
                    Err(error) => {
                        return Err(close_or_fail(pre.parts, OpenFailure::Other(error)));
                    }
                }
                Box::new(ReassembledHandle(pre.parts))
            }
            other => {
                if let Some(pre) = other {
                    // 舊 handle 未確認關閉時不另開新 handle（Python 作業取消即中止 attach 並經 `_finish_attach_op` 收尾，`:4939-4969`）。
                    let failure = OpenFailure::Other(PalError::plain(
                        PalErrorKind::Io,
                        "預開 UART handle 未確認關閉",
                    ));
                    if let failure @ OpenFailure::CloseUnconfirmed(_) = close_or_fail(pre.parts, failure) {
                        return Err(failure);
                    }
                }
                if precheck_holders {
                    let report = self.shared.uart_excl.probe_holders(&dev.0);
                    if !report.pids.is_empty() {
                        return Err(OpenFailure::Held(report));
                    }
                }
                self.shared
                    .serial
                    .open_exclusive(&dev, &cfg)
                    .map_err(open_failure)?
            }
        };
        if lock(&self.shared.inner).stopping {
            // open 返回時已進 stopping：不交給 actor（零 TX），立即關閉；未確認關閉同樣保留阻擋。
            return Err(close_or_fail(HandleParts::split(handle), stopped()));
        }
        if self.shared.unconfirmed_closes.contains(&by_id) {
            // 開埠期間 by_id 才登記為未確認關閉：Python `attach_device` 開埠後重驗 `_draining_by_ids`，以 `_finish_attach_op`
            // 關閉剛開的 handle（`origin/main:sw_core/session_manager.py:3576-3588`、`:3614-3631`）；不交給 actor（零 TX）。
            return Err(close_or_fail(HandleParts::split(handle), OpenFailure::CloseBlocked));
        }
        let epoch = self.shared.epochs.next();
        let observers = lock(&self.shared.rx_observers).clone();
        // Python 每次 attach 新建 `UARTBridge`：RX 視窗、速率、總量、最後 RX 時刻一律歸零（審查 F51）。
        let rx = RxShared::new(RX_WINDOW_MAX_CHARS, self.shared.clock.mono_ns());
        let worker = IoWorker::start_observed(
            handle,
            IoWorkerArgs {
                com: self.com.clone(),
                epoch,
                wal: self.shared.wal.clone(),
                rx: rx.clone(),
                fence: self.fence.clone(),
                // P6：哨兵 primary 由 `make_sentinel` 預建並經 hub 登記（取代 2.1 的 reader 哨兵；審查 F43／F30 的
                // console_count／vtty 由 core 的 human.consoles 計算）。
                sentinel: None,
                sentinel_keepalive: None,
                hub: self.hub.as_ref().map(|h| h.tap()),
                data: self.data.clone(),
                control: control.clone(),
                clock: self.shared.clock.clone(),
            },
            observers,
        );
        let sentinel = self.make_sentinel();
        let console_endpoint = self.bind_console_listener(epoch);
        Ok(OpenedBridge {
            epoch,
            worker,
            real_path: dev.0,
            sentinel,
            console_endpoint,
            rx,
        })
    }

}

impl ManagerExec {
    fn com(&self) -> String {
        lock(&self.io.profile).com.clone()
    }

    fn human_tx(
        &mut self,
        tag: crate::human_console::human_tx::HumanTxTag,
        permit: TxPermit,
        payload: Vec<u8>,
        control: &Sender<ControlEvent>,
    ) {
        let epoch = permit.epoch;
        if !self.pumps.contains_key(&epoch) {
            if let Some(worker) = lock(&self.workers).get(&epoch) {
                let pump = crate::human_console::human_tx::HumanTxPump::spawn(
                    &self.com(),
                    worker.tx().clone(),
                    control.clone(),
                );
                self.pumps.insert(epoch, pump);
            }
        }
        let refused = match self.pumps.get(&epoch) {
            Some(pump) => pump.try_submit(tag.clone(), permit.clone(), payload).is_err(),
            None => true,
        };
        if refused {
            if self.pumps.contains_key(&epoch) {
                // core 保證同時最多一筆在途；PumpFull 屬不變式違反（07 Task 6 Step 3 第 9 點）。
                self.shared.warn(format!(
                    "human TX pump 已滿（不變式違反），以 NotSubmitted 回報（com={}）",
                    self.com()
                ));
            }
            let report = crate::tx_writer::TxReport {
                op_id: permit.op_id,
                epoch,
                result: TxResult {
                    outcome: TxOutcome::NotSubmitted,
                    error: None,
                },
                wal: None,
                reject: None,
            };
            let _ = control.try_send(ControlEvent::HumanTxCompleted { tag, report });
        }
    }

    /// `StartOp`：交給該 epoch 的 `OpRunner`（Task 14）；無法啟動時以 `SendFailed` 結束 op，避免 actor 永久等待。
    fn start_op(&mut self, grant: OpGrant, spec: OpSpec, control: &Sender<ControlEvent>) {
        let epoch = grant.epoch;
        let op_id = grant.op_id;
        let ran = catch_unwind(AssertUnwindSafe(|| {
            if !self.runners.contains_key(&epoch) {
                let tx = lock(&self.workers).get(&epoch)?.tx().clone();
                let ctx = OpCtx {
                    com: self.com(),
                    sid: lock(&self.io.sid).clone(),
                    profile: lock(&self.io.profile).clone(),
                    rx: self.rx.clone(),
                    tx,
                    wal: self.shared.wal.clone(),
                    clock: self.shared.clock.clone(),
                    control: control.clone(),
                    rng: Arc::new(fill_random),
                    registry: self.shared.registry.clone(),
                };
                self.runners.insert(epoch, OpRunner::spawn(ctx));
            }
            self.runners.get(&epoch)?.run(grant, spec);
            Some(())
        }));
        if !matches!(ran, Ok(Some(()))) {
            self.shared.warn(format!(
                "session {} 前景 op 無法啟動（op_id={op_id:?}），以 SEND_FAILED 結束",
                self.com()
            ));
            let _ = control.send(ControlEvent::OpFinished {
                op_id,
                epoch,
                outcome: OpOutcome::SendFailed,
                after_rx_seq: None,
            });
        }
    }

    fn start_login(
        &mut self,
        epoch: u64,
        fsm_gen: u64,
        plan: crate::login_fsm::LoginPlan,
        control: &Sender<ControlEvent>,
    ) {
        if matches!(
            plan,
            crate::login_fsm::LoginPlan::None
                | crate::login_fsm::LoginPlan::CredentialsUnresolved { .. }
        ) {
            return;
        }
        let Some(handle) = lock(&self.workers).get(&epoch).map(|worker| worker.tx().clone()) else {
            return;
        };
        let rx = self.rx.clone();
        let clock = self.shared.clock.clone();
        let profile = lock(&self.io.profile).clone();
        let env = self.shared.env.clone();
        let control = control.clone();
        let _ = std::thread::Builder::new()
            .name(format!("serialwrap-login-{}", profile.com))
            .spawn(move || {
                let send = crate::login_fsm::login_tx_sender(handle, epoch, 0, fsm_gen);
                let nonce = nonce_hex8;
                // merge P7（Task 3）：LoginIo 增 cancel／class（Task 7 起由 OpRunner 依 run 種類決定 class）。
                let cancel = crate::uart_bridge::RunCancel::new(&rx);
                let io = crate::login_fsm::LoginIo {
                    rx: &rx,
                    tx: &send,
                    clock: &*clock,
                    nonce: &nonce,
                    cancel: &cancel,
                    class: crate::tx_writer::TxClass::LoginProbe,
                };
                let Some(result) = crate::login_fsm::run_login_plan(&io, &profile, &plan, &*env)
                else {
                    return;
                };
                let (wm_epoch, wm_seq) = rx.watermark();
                let _ = control.send(ControlEvent::LoginFinished {
                    epoch,
                    fsm_gen,
                    result,
                    after_rx_seq: (wm_epoch == epoch).then_some(wm_seq),
                });
            });
    }
}

impl EffectExecutor for ManagerExec {
    fn execute(&mut self, e: Effect, tx: &Sender<ControlEvent>, safety: &Sender<ControlEvent>) {
        match e {
            Effect::Reply(reply, result) => {
                let _ = reply.try_send(result);
            }
            Effect::OpenBridge {
                dev,
                cfg,
                precheck_holders,
            } => {
                let ctx = OpenCtx {
                    shared: self.shared.clone(),
                    io: self.io.clone(),
                    fence: self.fence.clone(),
                    data: self.data.clone(),
                    com: self.com(),
                    hub: self.hub.clone(),
                    opened_listener: self.opened_listener.clone(),
                };
                let control = tx.clone();
                let opened_rx = self.opened_rx.clone();
                let opened_listener = self.opened_listener.clone();
                let submitted = self.open_jobs.submit(Box::new(move || {
                    let result = ctx.open_bridge(dev, cfg, precheck_holders, &control);
                    if let Ok(opened) = &result {
                        lock(&opened_rx).insert(opened.epoch, opened.rx.clone());
                    }
                    let sent = control.send(ControlEvent::BridgeOpened {
                        epoch_hint: 0,
                        result,
                    });
                    // actor 已結束（開埠期間 session 被移除）：沒有人會 adopt／drain，就地關閉以免 handle 懸置。
                    if let Err(crossbeam_channel::SendError(ControlEvent::BridgeOpened {
                        result: Ok(opened),
                        ..
                    })) = sent
                    {
                        lock(&opened_rx).remove(&opened.epoch);
                        lock(&opened_listener).remove(&opened.epoch);
                        opened
                            .worker
                            .finish_async(crate::drain::StopId(0), control.clone());
                    }
                }));
                if !submitted {
                    let _ = tx.send(ControlEvent::BridgeOpened {
                        epoch_hint: 0,
                        result: Err(OpenFailure::Other(PalError::plain(
                            PalErrorKind::Io,
                            "開埠工作者無法受理",
                        ))),
                    });
                }
            }
            Effect::AdoptBridge { epoch, worker, .. } => {
                if let Some(rx) = lock(&self.opened_rx).remove(&epoch) {
                    self.rx = rx;
                }
                if lock(&self.opened_listener).remove(&epoch) {
                    self.listener_epoch = Some(epoch);
                }
                lock(&self.workers).insert(epoch, worker);
            }
            Effect::BeginDrain { stop_id, epoch } => {
                lock(&self.opened_rx).remove(&epoch);
                // 未被接手即排空的 epoch 也要停掉開埠時綁定的 listener。
                if lock(&self.opened_listener).remove(&epoch) {
                    self.listener_epoch = Some(epoch);
                }
                self.runners.remove(&epoch);
                if let Some(pump) = self.pumps.remove(&epoch) {
                    pump.stop();
                }
                let worker = lock(&self.workers).remove(&epoch);
                if let Some(worker) = worker {
                    worker.finish_async(stop_id, safety.clone());
                }
                if self.listener_epoch == Some(epoch) {
                    self.listener_epoch = None;
                    if let Some(hub) = &self.hub {
                        if hub
                            .cmd
                            .try_send(crate::human_console::hub::HubCmd::StopListener)
                            .is_err()
                        {
                            self.shared.warn(format!(
                                "console listener 停止命令送不進 hub（com={}）",
                                self.com()
                            ));
                        }
                        hub.waker.wake();
                    }
                }
            }
            Effect::StartLogin {
                epoch,
                fsm_gen,
                plan,
            } => self.start_login(epoch, fsm_gen, plan, tx),
            Effect::StartOp { grant, spec } => self.start_op(grant, spec, tx),
            Effect::Schedule { .. } => {
                // actor 迴圈自行排程（dispatch 不會把 Schedule 交給 executor）。
            }
            Effect::WalMeta { cmd_id, meta } => {
                let com = self.com();
                let _ = self.shared.wal.append(WalAppendReq {
                    com: &com,
                    dir: "META",
                    source: "system",
                    payload: &[],
                    cmd_id: cmd_id.as_deref(),
                    loss_flag: false,
                    meta: Some(meta),
                });
            }
            Effect::Persist { commit_id, delta } => {
                let release = matches!(delta, StateDelta::Released { .. });
                // sol P5-5cc4623-1：delta 依 actor 順序同步套入 model；工作者只跑 release 暫停鉤子並提交「當下」快照。
                // 若延到工作者才套 delta，release 暫停期間完成的 bind（Unreleased＋新 binding）會被舊 Released 覆寫
                // （Python `_save_release_state` 暫停後重建當前快照，不回放已失效的 delta）。
                self.shared.model_apply(delta);
                let shared = self.shared.clone();
                let control = tx.clone();
                let submitted = self.persist_jobs.submit(Box::new(move || {
                    let result = shared.commit_sync_kind(|_| {}, release);
                    let _ = control.send(ControlEvent::StateCommitted { commit_id, result });
                }));
                if !submitted {
                    let _ = tx.send(ControlEvent::StateCommitted {
                        commit_id,
                        result: Err(PalError::plain(PalErrorKind::Io, "持久化工作者無法受理")),
                    });
                }
            }
            Effect::Notify(note) => {
                let _ = self.shared.note_tx.send(note);
            }
            Effect::Warn(msg) => self.shared.warn(msg),
            Effect::Reattach { by_id, .. } => {
                self.shared.spawn_attach_with(&by_id, true, false, None)
            }
            Effect::ClearRevokedHandoff { by_id, token } => {
                self.shared.clear_revoked_handoff(&by_id, token)
            }
            Effect::SettleTx { epoch, op_id } => {
                if let Some(worker) = lock(&self.workers).get(&epoch) {
                    let _ = worker.tx().settle(op_id);
                }
            }
            // P6 Task 10：全域 LeaseIndex（含 human lease、tombstone）；在 Reply 之前同步更新（core 保證排序）。
            Effect::LeaseIndex(op) => {
                let sid = lock(&self.io.sid).clone();
                let mut inner = lock(&self.shared.inner);
                match op {
                    crate::session_core::LeaseIndexOp::Insert { interactive_id } => {
                        inner.interactive.insert(interactive_id, sid);
                    }
                    crate::session_core::LeaseIndexOp::Remove { interactive_id } => {
                        if inner.interactive.get(&interactive_id) == Some(&sid) {
                            inner.interactive.remove(&interactive_id);
                        }
                    }
                }
            }
            // P6 Task 9：console I/O 交給 hub（未建 hub 的平台忽略）。
            Effect::ConsoleOut { client_id, item } => {
                if let Some(hub) = self.hub.as_ref() {
                    hub.out(client_id, item);
                }
            }
            Effect::ConsoleClose { client_id } => {
                if let Some(hub) = self.hub.as_ref() {
                    hub.close(&client_id);
                }
            }
            Effect::ConsoleCloseAll => {
                if let Some(hub) = self.hub.as_ref() {
                    hub.close_all();
                }
            }
            Effect::ConsoleAdopt {
                token,
                client_id,
                client_lifetime,
                label,
            } => {
                let pair = lock(&self.io.pending_pty).remove(&token);
                let adopted = match (self.hub.as_ref(), pair) {
                    (Some(hub), Some(pair)) => {
                        let sent = hub
                            .cmd
                            .try_send(crate::human_console::hub::HubCmd::AdoptPty {
                                client_id: client_id.clone(),
                                client_lifetime,
                                label,
                                pair,
                            })
                            .is_ok();
                        if sent {
                            hub.waker.wake();
                        }
                        sent
                    }
                    _ => false,
                };
                if !adopted {
                    // PTY 已不在（RPC 逾時已丟棄）或 hub 佇列滿：讓 core 移除這個沒有 fd 的 console（含其 owner／lease），
                    // 不留下幽靈 console（審查 1fb9612 Medium）。以 detach 語意（ERR-7.5-b：一併關閉自己的 human lease）
                    // 送 control lane；斷線語意（`ConsoleDisconnected`）會保留 lease，使之後 attach 的 console 取不到
                    // lease 與 raw ownership。此 console 從未交給 hub，data lane 上沒有它的輸入可被超車。
                    self.shared.warn(format!(
                        "console 註冊失敗（com={} client_id={client_id}）：PTY 不存在或 hub 佇列已滿",
                        self.com()
                    ));
                    let (reply, _ignored) = crossbeam_channel::bounded(1);
                    let detach = ControlEvent::Request {
                        req: SessionRequest::ConsoleDetach { client_id: client_id.clone() },
                        reply,
                    };
                    if tx.try_send(detach).is_err() {
                        let _ = self.data.try_send(DataEvent::ConsoleDisconnected { client_id, client_lifetime });
                    }
                }
            }
            Effect::ConsoleDiscard { token } => {
                lock(&self.io.pending_pty).remove(&token);
            }
            // P6 Task 6（07 D-15(5)）：交給 per-epoch pump（try_send，actor 永不阻塞）；送不出去以 NotSubmitted 回報。
            Effect::HumanTx { tag, permit, payload } => self.human_tx(tag, permit, payload, tx),
            // S2-PAR-wp510r R1: 輪詢保管帳本終態與 offer
            Effect::PollCustody { tokens: _ } => {}
        }
    }
}

// ---------------------------------------------------------------------------
// SessionManager 公開介面
// ---------------------------------------------------------------------------

impl SessionManager {
    pub fn new(deps: ManagerDeps) -> Arc<SessionManager> {
        let ManagerDeps {
            serial,
            uart_excl,
            console,
            caps,
            wal,
            registry,
            state,
            loaded,
            load,
            clock,
            env,
            fs,
            warn,
        } = deps;
        let fanout = WalHealthFanout::new();
        wal.set_health_sink(fanout.clone());
        let (note_tx, note_rx) = crossbeam_channel::unbounded();
        let (watcher_tx, watcher_rx) = crossbeam_channel::unbounded();
        let (reaper_cmd_tx, _reaper_cmd_rx) = crossbeam_channel::unbounded();
        let (_reaper_event_tx, reaper_event_rx) = crossbeam_channel::unbounded();
        let reaper_thread = crate::custody::ReaperThread::start(reaper_cmd_tx.clone(), reaper_event_rx);
        let max_sessions = pyint_usize(&load.max_sessions, 16);
        let shared = Arc::new(Shared {
            serial,
            uart_excl,
            console: console.clone(),
            wal,
            registry,
            clock,
            env,
            fs,
            warn,
            store: state,
            lone_surrogates: loaded.lone_surrogates,
            startup_error: Mutex::new(None),
            model: Mutex::new(StateModel::new(loaded)),
            fanout,
            epochs: EpochAllocator::new(),
            templates: load.templates.clone(),
            max_sessions,
            inner: Mutex::new(Inner::default()),
            cv: Condvar::new(),
            note_tx: note_tx.clone(),
            bg_index: {
                let index = crate::capture::BackgroundIndex::new();
                // 淘汰決定在 actor thread 內做出：只經通知通道交給 manager pump，不在 actor thread 取 manager 鎖。
                let tx = note_tx;
                index.set_remote_evictor(Arc::new(move |com: &str, cmd_id: &str| {
                    let _ = tx.send(ManagerNote::EvictCapture {
                        com: com.to_string(),
                        cmd_id: cmd_id.to_string(),
                    });
                }));
                index
            },
            unconfirmed_closes: UnconfirmedCloses::new(),
            closed: AtomicBool::new(false),
            persist_observer: Mutex::new(None),
            probe_tx_frozen: AtomicBool::new(false),
            handoff_probes: Mutex::new(HandoffProbes::default()),
            rx_observers: Mutex::new(Vec::new()),
            path_flavor: caps.path_flavor,
            #[cfg(feature = "test-fakes")]
            probe_pre_submit_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            placeholder_release_cancel_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            alias_pre_commit_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            open_bridge_pre_take_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            open_bridge_post_take_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            finish_attach_op_pre_lock_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            fail_next_attach_spawn: AtomicBool::new(false),
            #[cfg(feature = "test-fakes")]
            bind_pre_device_online_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            spawned_workers: std::sync::atomic::AtomicUsize::new(0),
            #[cfg(feature = "test-fakes")]
            query_timeout_override: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            file_transfer_timeout_override: Mutex::new(None),
            reaper_cmd_tx,
            reaper: Mutex::new(Some(reaper_thread)),
        });
        shared.bootstrap_sessions(&load.profiles);
        let mgr = Arc::new(SessionManager {
            shared: shared.clone(),
            watcher_tx,
            watcher_rx: watcher_rx.clone(),
            note_rx: note_rx.clone(),
            pump_lock: Arc::new(Mutex::new(())),
            _console: console,
            _caps: caps,
        });
        let pump_lock = mgr.pump_lock.clone();
        let _ = std::thread::Builder::new()
            .name("serialwrap-session-manager".to_string())
            .spawn(move || {
                while !shared.closed.load(Ordering::SeqCst) {
                    // 只等「有事件可讀」，不在等待期間持鎖（std Mutex 非公平，持鎖等待會餓死同步呼叫端的 pump）。
                    let mut sel = crossbeam_channel::Select::new();
                    sel.recv(&watcher_rx);
                    sel.recv(&note_rx);
                    if sel.ready_timeout(PUMP_POLL).is_err() {
                        continue;
                    }
                    let handled = {
                        let _guard = lock(&pump_lock);
                        drain_events(&shared, &watcher_rx, &note_rx)
                    };
                    if !handled {
                        // 事件已被同步呼叫端取走，或通道已斷線（manager 正在釋放）：短暫讓出。
                        std::thread::sleep(Duration::from_millis(1));
                    }
                }
            });
        mgr
    }

    /// 依序處理已排入的 watcher 事件與通知（manager thread 之外的同步呼叫點；與 manager thread 互斥、保序）。
    fn pump(&self) {
        let _guard = lock(&self.pump_lock);
        drain_events(&self.shared, &self.watcher_rx, &self.note_rx);
    }

    /// DF-14：於 lock 內依 `device_sort_key` 預先排定動態 COM（在 watcher 啟動前呼叫）。
    /// explicit YAML target／binding override／RELEASED／已有 session 的 by_id 不參與；已用 COM 跳過。
    pub fn prepare_dynamic_rank(&self, by_ids: &[String]) {
        let released = self.shared.released_by_ids();
        let snap = self.shared.model_snapshot();
        let mut inner = lock(&self.shared.inner);
        let mut used: HashSet<String> = inner
            .sessions
            .values()
            .map(|e| e.profile.com.clone())
            .collect();
        used.extend(inner.pending_com.values().cloned());
        let bound: HashSet<&String> = snap.bindings.values().collect();
        let mut ordered: Vec<&String> = by_ids
            .iter()
            .filter(|b| {
                !b.is_empty()
                    && !released.contains(*b)
                    && !bound.contains(b)
                    && !inner
                        .sessions
                        .values()
                        .any(|e| &e.profile.device_by_id == *b)
            })
            .collect();
        ordered.sort_by_key(|b| device_sort_key(b, None));
        let mut idx = 0usize;
        for b in ordered {
            if inner.pending_com.contains_key(b) {
                continue;
            }
            while used.contains(&format!("COM{idx}")) {
                idx += 1;
            }
            let com = format!("COM{idx}");
            inner.pending_com.insert(b.clone(), com.clone());
            used.insert(com);
            idx += 1;
        }
    }

    /// `com_for_by_id`（#20）：既有 session 優先，否則 startup 預配（pending）COM。
    pub fn com_for_by_id(&self, by_id: &str) -> Option<String> {
        let inner = lock(&self.shared.inner);
        if let Some(e) = inner.session_by_id(by_id) {
            return Some(e.profile.com.clone());
        }
        inner.pending_com.get(by_id).cloned()
    }

    /// 裝置快照變更（added／removed／changed）→ actor 事件與動態 session 建立。
    pub fn update_devices(&self, snapshot: &[(String, DeviceInfo)]) {
        self.pump();
        self.shared.update_devices(snapshot);
    }

    /// 啟動時對快照中所有裝置 attach（stopping 後不動作）。
    pub fn bootstrap_attach(&self) {
        self.pump();
        let keys: Vec<String> = {
            let inner = lock(&self.shared.inner);
            if inner.stopping {
                return;
            }
            inner.devices.keys().cloned().collect()
        };
        for by_id in keys {
            self.shared.spawn_attach(&by_id);
        }
    }

    /// selector → session_id（session_id 精確優先；否則依建立順序取第一個 `com == sel || alias == sel`）。
    pub fn resolve(&self, selector: &str) -> Option<String> {
        lock(&self.shared.inner)
            .resolve(selector)
            .map(|e| e.sid.clone())
    }

    /// OD-63：以 manager device snapshot 重新開啟缺失 template 的 RELEASED 佔位，交給 dynamic template 偵測。
    /// manager 層直接回歸入口；正式 RPC 透過 `route_with_selector` 保留呼叫者輸入。
    pub fn attach_released_placeholder(&self, sid: &str, force: bool) -> Result<PyDict, PyErr> {
        self.pump();
        self.shared.attach_released_placeholder(sid, force, None)
    }

    /// P8 Task 6：`file.push`／`file.pull` 的 session 路由（Python `_resolve_session_id(file_transfer=True)` 之後的
    /// `file_push`／`file_pull`）。查無 → `SESSION_NOT_FOUND`＋原 selector；其餘閘門在 actor 內原子判定。
    pub fn file_transfer(&self, req: crate::file_transfer::FileTransferRequest) -> Result<PyDict, PyErr> {
        match self.resolve(&req.selector) {
            Some(sid) => self.route(&sid, SessionRequest::FileTransfer(req), self.shared.file_transfer_reply_timeout()),
            None => Ok(pydict! {
                "ok" => false,
                "error_code" => "SESSION_NOT_FOUND",
                "selector" => req.selector,
            }),
        }
    }

    /// 傳檔 RPC 路由；ticket 由 pending op 持有至傳輸實際終態，actor reply timeout 不會提早解除交接 admission。
    pub fn file_transfer_with_admission(
        &self,
        req: crate::file_transfer::FileTransferRequest,
        admission: Option<Arc<crate::admission::AdmissionTicket>>,
    ) -> Result<PyDict, PyErr> {
        match self.resolve(&req.selector) {
            Some(sid) => {
                let request = match admission {
                    Some(admission) => SessionRequest::FileTransferAdmitted { req, admission },
                    None => SessionRequest::FileTransfer(req),
                };
                self.route(&sid, request, self.shared.file_transfer_reply_timeout())
            }
            None => Ok(pydict! {
                "ok" => false,
                "error_code" => "SESSION_NOT_FOUND",
                "selector" => req.selector,
            }),
        }
    }

    /// 將 session 請求路由到對應 actor 並等待回覆。
    pub fn route(
        &self,
        sid: &str,
        req: SessionRequest,
        timeout: Duration,
    ) -> Result<PyDict, PyErr> {
        self.route_inner(sid, req, timeout, None)
    }

    /// 保留 RPC 原始 selector 供需要回顯 selector 的專用路徑使用；actor 仍以 sid 路由。
    pub fn route_with_selector(
        &self,
        sid: &str,
        req: SessionRequest,
        timeout: Duration,
        selector: &str,
    ) -> Result<PyDict, PyErr> {
        self.route_inner(sid, req, timeout, Some(selector))
    }

    fn route_inner(
        &self,
        sid: &str,
        req: SessionRequest,
        timeout: Duration,
        request_selector: Option<&str>,
    ) -> Result<PyDict, PyErr> {
        self.pump();
        if let SessionRequest::DeviceAttach { force } = &req {
            let is_placeholder = lock(&self.shared.inner)
                .sessions
                .get(sid)
                .is_some_and(|entry| entry.released_placeholder);
            if is_placeholder {
                // OD-63：只讓 RELEASED 佔位的 device.attach 走專用交接；一般 session 仍經 actor 與下方 stopping gate。
                return self
                    .shared
                    .attach_released_placeholder(sid, *force, request_selector);
            }
        }
        let mut revoked_handoff: Option<(String, u64)> = None;
        if let SessionRequest::Release { source, reason } = &req {
            let settled = self.release_attach_handles(sid, source, reason, &mut revoked_handoff);
            if !matches!(settled, Ok(None)) {
                if let Some((by_id, token)) = revoked_handoff.take() {
                    self.shared.clear_revoked_handoff(&by_id, token);
                }
                return settled.map(|response| response.expect("非 None 的 release 收尾回應"));
            }
        }
        let result = self.route_after_release_settle(sid, req, timeout);
        if let Some((by_id, token)) = revoked_handoff {
            self.shared.clear_revoked_handoff(&by_id, token);
        }
        result
    }

    fn route_after_release_settle(
        &self,
        sid: &str,
        req: SessionRequest,
        timeout: Duration,
    ) -> Result<PyDict, PyErr> {
        let stopping = lock(&self.shared.inner).stopping;
        match &req {
            SessionRequest::Bind { by_id } => return self.bind(sid, by_id),
            SessionRequest::Clear => return self.clear(sid, timeout),
            SessionRequest::Recover { .. } if stopping && self.shared.actor_of(sid).is_some() => {
                // Python `recover_session`：RELEASED 判定（released 回應）先於 stopping 例外（審查 F68）。
                let released = self.shared.session_public(sid).is_some_and(|s| {
                    str_of(&s, "state").as_deref() == Some("RELEASED")
                        || self
                            .shared
                            .released_by_ids()
                            .contains(&str_of(&s, "device_by_id").unwrap_or_default())
                });
                if !released {
                    return Err(stopping_error());
                }
            }
            SessionRequest::DeviceAttach { .. }
                if stopping && self.shared.actor_of(sid).is_some() =>
            {
                return Err(stopping_error());
            }
            _ => {}
        }
        let is_open = matches!(req, SessionRequest::InteractiveOpen { .. });
        let is_release = matches!(req, SessionRequest::Release { .. });
        if matches!(
            req,
            SessionRequest::GetState
                | SessionRequest::Activity
                | SessionRequest::InteractiveOpen { .. }
                | SessionRequest::FileTransfer(_)
                | SessionRequest::FileTransferAdmitted { .. }
        ) {
            // 07 D-09：session 摘要與 interactive_open（Python `_refresh_interactive_locked` 即時掃 peer）前先更新 console peer 判定。
            // P8 Task 6：file.* 的 manager 層同樣先 `_refresh_interactive_locked`（human lease 的 peer 判定）。
            self.shared.send_console_scan(sid);
        }
        let result = self.shared.request(sid, req, timeout)?;
        if is_open && is_ok(&result) {
            if let Some(iid) = str_of(&result, "interactive_id") {
                lock(&self.shared.inner)
                    .interactive
                    .insert(iid, sid.to_string());
            }
        }
        if is_release && is_ok(&result) {
            lock(&self.shared.inner).interactive.retain(|_, v| v != sid);
        }
        Ok(result)
    }

    /// Python `release_device` 的作業收尾（`origin/main:sw_core/session_manager.py:3104-3111`、`:3117-3131`、
    /// `:3188-3204`）：依序查 by_id 的在途作業與交接列，直到兩者都查無才交給 actor 的 release 流程。
    ///
    /// - 在途作業、有 worker（`_active_attach_ops`／`_attach_ops`）：取消並記錄第一個 release 的來源與期限，等待作業
    ///   收尾（worker 見取消即關閉 handle）；期限到仍未收尾回 DRAIN_STALLED（`:3192-3204`）。
    /// - 在途作業、parked（`_preopened_ports` 未消費者，`:3108-3111`）：取出作業並直接關閉 handle。作業已不在任何表內，
    ///   `closed:false` 只告警、不保留、不登記阻擋（`_finish_attach_op` `:4322-4332`），立即回 DRAIN_STALLED＋
    ///   `drain_stalled:true`（`done` 未設定，`:3192-3204`）。
    /// - 交接列未取走（Python 作業仍在 `_active_attach_ops`，由 attach 持有）：認領交接列並留撤銷標記，令已排入的
    ///   `open_bridge` 不開埠；同一臨界區把 handle 移入關閉中作業、寫入本次 release 請求與期限，並排入帶撤銷標記 token 的
    ///   `ReleaseAttachPending`（Python 設 RECOVERING，`:3133-3139`）。標記由 actor 於被取消的開埠確定不會取用後移除
    ///   （Python 取消旗標隨作業保留，`:3125`），不隨本次 release 返回或逾時移除；沒有 actor 接手時才交呼叫端在 release
    ///   結束時移除（`revoked_handoff`）。之後由專用執行緒關閉、
    ///   release 照在途作業等待（`:3188-3204`）：確認關閉才移出作業；重複的 release 取得同一作業、沿用首次期限；關閉
    ///   未確認時作業保留（`retain_unconfirmed_close`），期限到回 DRAIN_STALLED。
    /// - 交接列已取走者交 actor（開埠在途的 pending stop 收尾）。
    fn release_attach_handles(
        &self,
        sid: &str,
        source: &str,
        reason: &PyValue,
        revoked_handoff: &mut Option<(String, u64)>,
    ) -> Result<Option<PyDict>, PyErr> {
        // 與 Python `release_device` 一樣，在取得 manager lock 前開始本次期限計時。
        let candidate_release_deadline = Instant::now() + ATTACH_OP_WAIT;
        enum Step {
            CloseParked {
                by_id: String,
                pre: Option<Preopened>,
                token: u64,
            },
            CloseHandoff {
                by_id: String,
                op: Option<AttachOp>,
                deadline: Instant,
                pending: Option<Receiver<Result<PyDict, PyErr>>>,
            },
            Wait {
                by_id: String,
                op: AttachOp,
                deadline: Instant,
            },
        }
        loop {
            let step = {
                let mut inner = lock(&self.shared.inner);
                let Some(entry) = inner.sessions.get(sid) else {
                    return Ok(None);
                };
                let by_id = entry.profile.device_by_id.clone();
                if by_id.is_empty() {
                    return Ok(None);
                }
                if let Some(op) = inner.inflight.get(&by_id).cloned() {
                    if op.parked.swap(false, Ordering::SeqCst) {
                        inner.inflight.remove(&by_id);
                        Step::CloseParked {
                            pre: op.take_preopened(),
                            token: op.token,
                            by_id,
                        }
                    } else {
                        // 與 `retire_released_placeholder_locked` 共用 manager lock：若 release 已取得 op，
                        // 後續的 attach 收尾就不能先通過退休檢查。
                        op.cancel();
                        op.release_requested.store(true, Ordering::SeqCst);
                        // Python `release_device` 只在第一個 release 請求寫入 `release_request`（`:3117-3122`）。
                        lock(&op.release_request)
                            .get_or_insert_with(|| (source.to_string(), reason.clone()));
                        let deadline = {
                            let mut stored = lock(&op.release_deadline);
                            *stored.get_or_insert(candidate_release_deadline)
                        };
                        #[cfg(feature = "test-fakes")]
                        if let Some(hook) =
                            lock(&self.shared.placeholder_release_cancel_hook).as_ref()
                        {
                            hook();
                        }
                        Step::Wait {
                            by_id,
                            op,
                            deadline,
                        }
                    }
                } else {
                    match self
                        .shared
                        .claim_handoff_for_close_locked(&mut inner, &by_id, true)
                    {
                        HandoffCloseClaim::Unclaimed { closing, token } => {
                            // 關閉中作業與 release 請求在同一臨界區寫入：之後的 release 取得同一作業、沿用本次期限
                            // （Python `:3117-3125`），確認關閉前不會回成功。
                            let mut pending = None;
                            if let Some(op) = closing.as_ref() {
                                op.release_requested.store(true, Ordering::SeqCst);
                                lock(&op.release_request)
                                    .get_or_insert_with(|| (source.to_string(), reason.clone()));
                                lock(&op.release_deadline).get_or_insert(candidate_release_deadline);
                                // Python `release_device` 找到作業即在同一把鎖內設 RECOVERING（`:3133-3139`）。於鎖內排入，
                                // 先於開埠 worker 撞到撤銷標記後回報的 `BridgeOpened`：actor 不會停在無開埠在途的
                                // ATTACHING，之後的 `Release` 也不會把它當成開埠在途而等待。
                                // 撤銷標記一併交 actor：其生命週期綁定被取消的開埠，不隨 release RPC 返回（含逾時回
                                // DRAIN_STALLED）而移除（wp510 第九次整包審查 High P8-8463b22-1；Python 取消旗標隨作業
                                // 保留，`:3125`）。
                                if let Some(actor) = inner.sessions.get(sid).map(|e| e.actor.clone()) {
                                    match enqueue_safety_request(
                                        &actor,
                                        SessionRequest::ReleaseAttachPending {
                                            by_id: by_id.clone(),
                                            handoff_token: token,
                                        },
                                        QUERY_TIMEOUT,
                                    ) {
                                        Ok(rx) => pending = Some(rx),
                                        Err(error) => self.shared.warn(format!(
                                            "release 進行中通知失敗（by_id={by_id}）：{}",
                                            error.message
                                        )),
                                    }
                                }
                            }
                            if pending.is_none() {
                                // 沒有 actor 接手（無 handle、無 actor 或通知失敗）：不會有該 session 的開埠再取用標記，
                                // 沿用 release 結束時移除。
                                *revoked_handoff = Some((by_id.clone(), token));
                            }
                            Step::CloseHandoff {
                                by_id,
                                op: closing,
                                deadline: candidate_release_deadline,
                                pending,
                            }
                        }
                        HandoffCloseClaim::Taken | HandoffCloseClaim::None => return Ok(None),
                    }
                }
            };
            match step {
                Step::CloseParked { by_id, pre, token } => {
                    self.shared.cv.notify_all();
                    let closed = pre.is_none_or(|pre| self.shared.close_session_preopened(&by_id, pre));
                    if !closed {
                        return self.release_close_unconfirmed_reply(sid, token);
                    }
                }
                Step::CloseHandoff {
                    by_id,
                    op,
                    deadline,
                    pending,
                } => {
                    self.shared.cv.notify_all();
                    if let Some(rx) = pending {
                        // 先確認 actor 已套用 RECOVERING，之後才可能送出 `Release`。
                        if let Err(error) = await_actor_reply(rx, QUERY_TIMEOUT) {
                            self.shared.warn(format!(
                                "release 進行中通知未確認（by_id={by_id}）：{}",
                                error.message
                            ));
                        }
                    }
                    if let Some(op) = op {
                        // Python 由持有作業的 attach 中止後關閉 handle，release 只取消並等待 `done` 至期限（`:3188-3204`）；
                        // Rust 的持有者（actor 的 `OpenBridge`）尚未接手，改由專用執行緒代為關閉，release 照在途作業等待。
                        self.spawn_handoff_closer(&by_id, &op);
                        if let Some(reply) =
                            self.wait_attach_op_for_release(sid, &by_id, &op, deadline)?
                        {
                            return Ok(Some(reply));
                        }
                    }
                }
                Step::Wait {
                    by_id,
                    op,
                    deadline,
                } => {
                    if let Some(reply) = self.wait_attach_op_for_release(sid, &by_id, &op, deadline)? {
                        return Ok(Some(reply));
                    }
                }
            }
        }
    }

    /// 以專用執行緒關閉 release 認領的交接 handle（`Shared::close_claimed_handoff`），release 不必卡在關閉本身、可於
    /// 首次期限回 DRAIN_STALLED（Python 的關閉由 attach 執行緒進行，`origin/main:sw_core/session_manager.py:4320-4332`）。
    /// 執行緒建立失敗時告警並於本執行緒直接關閉。
    fn spawn_handoff_closer(&self, by_id: &str, op: &AttachOp) {
        let shared = Arc::clone(&self.shared);
        let key = by_id.to_string();
        let closer_op = op.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("serialwrap-handoff-close-{by_id}"))
            .spawn(move || shared.close_claimed_handoff(&key, &closer_op));
        if let Err(error) = spawned {
            self.shared
                .warn(format!("交接 handle 關閉執行緒建立失敗（by_id={by_id}）：{error}"));
            self.shared.close_claimed_handoff(by_id, op);
        }
    }

    /// 等待 release 已取消的在途作業收尾；作業移除回 `None`，期限到回 DRAIN_STALLED（Python `:3192-3204`）。
    fn wait_attach_op_for_release(
        &self,
        sid: &str,
        by_id: &str,
        op: &AttachOp,
        deadline: Instant,
    ) -> Result<Option<PyDict>, PyErr> {
        let epoch = self
            .shared
            .session_public(sid)
            .and_then(|session| session.get("bridge_generation").and_then(PyValue::as_i64))
            .unwrap_or(0)
            .max(0) as u64;
        let stalled_reply = {
            let mut inner = lock(&self.shared.inner);
            loop {
                let same_op = inner
                    .inflight
                    .get(by_id)
                    .is_some_and(|current| Arc::ptr_eq(&current.cancelled, &op.cancelled));
                if !same_op {
                    return Ok(None);
                }
                let now = Instant::now();
                if now >= deadline {
                    let Some(actor) = inner.sessions.get(sid).map(|entry| entry.actor.clone())
                    else {
                        return Ok(None);
                    };
                    // 與完成收尾共用 manager lock，先排入 timeout 再允許 worker 排入同 token 的 settled。
                    break enqueue_safety_request(
                        &actor,
                        SessionRequest::PlaceholderAttachStalled {
                            token: op.token,
                            epoch,
                        },
                        QUERY_TIMEOUT,
                    )?;
                }
                let (next, _) = self
                    .shared
                    .cv
                    .wait_timeout(inner, deadline - now)
                    .unwrap_or_else(|e| e.into_inner());
                inner = next;
            }
        };
        let mut response = await_actor_reply(stalled_reply, QUERY_TIMEOUT)?;
        response.insert("error_code", PyValue::from("DRAIN_STALLED"));
        Ok(Some(response))
    }

    /// release 直接關閉的 parked handle 回 `closed:false`：Python 設 `drain_stalled`、`drain_timeout_epoch` 與 RECOVERING
    /// 後回 DRAIN_STALLED（`origin/main:sw_core/session_manager.py:3192-3204`）。
    fn release_close_unconfirmed_reply(&self, sid: &str, token: u64) -> Result<Option<PyDict>, PyErr> {
        let epoch = self
            .shared
            .session_public(sid)
            .and_then(|session| session.get("bridge_generation").and_then(PyValue::as_i64))
            .unwrap_or(0)
            .max(0) as u64;
        let Some(actor) = self.shared.actor_of(sid) else {
            return Ok(None);
        };
        let reply = enqueue_safety_request(
            &actor,
            SessionRequest::PlaceholderAttachStalled { token, epoch },
            QUERY_TIMEOUT,
        )?;
        let mut response = await_actor_reply(reply, QUERY_TIMEOUT)?;
        response.insert("error_code", PyValue::from("DRAIN_STALLED"));
        Ok(Some(response))
    }

    /// `clear_session`：RELEASED 早退；stopping → `Err("daemon stopping")`；否則 drain(CLEARED) 後重新 attach。
    fn clear(&self, sid: &str, timeout: Duration) -> Result<PyDict, PyErr> {
        let Some(public) = self.shared.session_public(sid) else {
            return self.shared.request(sid, SessionRequest::Clear, timeout);
        };
        let by_id = str_of(&public, "device_by_id").unwrap_or_default();
        if str_of(&public, "state").as_deref() == Some("FLASHING") || self.flash_critical_sid(sid) {
            // FLASHING 或 flash probe 臨界區不得 detach bridge（#69 r5；#83 RACE-2 final）；Python 鍵序 ok／error_code／selector／session。
            return Ok(flashing_busy(public));
        }
        if str_of(&public, "state").as_deref() == Some("RELEASED")
            || self.shared.released_by_ids().contains(&by_id)
        {
            return Ok(pydict! { "ok" => true, "released" => true, "session" => public });
        }
        if lock(&self.shared.inner).stopping {
            return Err(stopping_error());
        }
        let result = self.shared.request(sid, SessionRequest::Clear, timeout)?;
        let detached = session_of(&result)
            .and_then(|s| str_of(&s, "state"))
            .as_deref()
            == Some("DETACHED");
        if is_ok(&result) && detached && !by_id.is_empty() {
            // core 判定裝置不在線而 manager 鏡像有裝置（兩者不一致時）：仍經 `_spawn_attach` 再解析，不直接以舊
            // profile 開埠（審查 F98）。裝置確實不在線時 `spawn_attach` 為 no-op。
            self.shared.spawn_attach(&by_id);
        }
        Ok(result)
    }

    /// 依 session_id 排序的 session 公開欄位。
    pub fn list_sessions(&self) -> Vec<PyDict> {
        let mut sids: Vec<String> = lock(&self.shared.inner).sessions.keys().cloned().collect();
        sids.sort();
        sids.iter()
            .filter_map(|sid| self.shared.session_public(sid))
            .collect()
    }

    /// 交接專用完整 session 快照；任何 actor 查詢失敗都回傳該 session 的 COM，呼叫端必須 fail closed。
    pub fn list_sessions_checked(&self) -> Result<Vec<PyDict>, String> {
        let mut sessions: Vec<(String, String)> = lock(&self.shared.inner)
            .sessions
            .values()
            .map(|entry| (entry.sid.clone(), entry.profile.com.clone()))
            .collect();
        sessions.sort_by(|a, b| a.0.cmp(&b.0));
        sessions
            .into_iter()
            .map(|(sid, com)| self.shared.session_public_checked(&sid).map_err(|_| com))
            .collect()
    }

    /// Python `add_rx_observer`：之後建立的 session bridge reader 依序通知（daemon 於 bootstrap attach 前註冊）。
    pub fn add_rx_observer(&self, observer: Arc<dyn crate::uart_bridge::RxObserver>) {
        lock(&self.shared.rx_observers).push(observer);
    }

    /// `{by_id, real_path}`，依 by_id 排序。
    pub fn list_devices(&self) -> Vec<PyDict> {
        lock(&self.shared.inner)
            .devices
            .iter()
            .map(|(by_id, d)| {
                pydict! { "by_id" => by_id.clone(), "real_path" => d.real_path.clone() }
            })
            .collect()
    }

    /// `bind_session`：`state.json` 寫入失敗 → `Err`（framing 轉 EXCEPTION）。
    pub fn bind(&self, selector: &str, by_id: &str) -> Result<PyDict, PyErr> {
        let by_id = by_id.trim().to_string();
        if by_id.is_empty() {
            return Ok(pydict! { "ok" => false, "error_code" => "INVALID_ARGS" });
        }

        // OD-63：佔位在任何 pump、FLASHING／在途 op／stopping／衝突檢查與持久化前早退。
        let (sid, released_placeholder) = {
            let inner = lock(&self.shared.inner);
            let Some(e) = inner.resolve(selector) else {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "selector" => selector.to_string()
                });
            };
            (e.sid.clone(), e.released_placeholder)
        };
        if released_placeholder {
            let Some(public) = self.shared.session_public(&sid) else {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "selector" => selector.to_string()
                });
            };
            return Ok(pydict! {
                "ok" => true,
                "released" => true,
                "recommended_action" => "device_attach",
                "session" => public
            });
        }

        self.pump();
        let deadline = Instant::now() + ATTACH_OP_WAIT;
        if let Some(sid) = self.resolve(selector) {
            if !lock(&self.shared.inner).stopping {
                if let Some(public) = self.shared.session_public(&sid) {
                    if str_of(&public, "state").as_deref() == Some("FLASHING") || self.flash_critical_sid(&sid) {
                        // FLASHING 或 flash probe 臨界區不得 rebind（Python 於 DEVICE_ALREADY_BOUND 之前判定；#83 RACE-2 final）。
                        return Ok(flashing_busy(public));
                    }
                }
            }
        }
        let (sid, io) = loop {
            let mut inner = lock(&self.shared.inner);
            let Some(e) = inner.resolve(selector) else {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "selector" => selector.to_string()
                });
            };
            let sid = e.sid.clone();
            let old_by_id = e.profile.device_by_id.clone();
            let io = e.io.clone();
            // #26：舊 by_id 的 attach op 進行中 → 取消並等待（上限 3000 ms）後重試；逾時 → SESSION_RECOVERING。
            if let Some(op) = inner.inflight.get(&old_by_id).cloned() {
                op.cancel();
                if op.parked.swap(false, Ordering::SeqCst) {
                    // 未消費的預開作業沒有 worker，不可等待（審查 P8-503c341-1）：Python 自 `_preopened_ports` 取出後直接
                    // `_finish_attach_op` 關閉 handle，再遞迴重試 bind（`origin/main:sw_core/session_manager.py:3680-3694`、
                    // `:3727`）。鎖內認領、作業留在 `inflight` 直到關閉回條，期間其他入口視同在途作業而不開埠。
                    drop(inner);
                    self.close_parked_for_bind(&old_by_id, &op);
                    continue;
                }
                let now = Instant::now();
                if now >= deadline {
                    drop(inner);
                    return Ok(self.attach_op_stalled_reply(&sid));
                }
                drop(
                    self.shared
                        .cv
                        .wait_timeout(inner, deadline - now)
                        .unwrap_or_else(|e| e.into_inner()),
                );
                continue;
            }
            // 舊 by_id 的 handle 已交給本 session、等待正式 bridge 接手（`HandoffEntry`；Python 作業在
            // `_active_attach_ops`）：Python 取消並等待 `done`（`:3676-3693`），attach 中止後 `_finish_attach_op` 關閉
            // 再遞迴重試（`:3727`）。未取走者由此代為關閉後重試；關閉未確認時作業保留，下一輪照在途作業等待至期限回
            // SESSION_RECOVERING＋drain_stalled（`:3697-3711`）。已取走者（`open_bridge` 進行中）等待其收尾，上限同在途作業。
            match self
                .shared
                .claim_handoff_for_close_locked(&mut inner, &old_by_id, false)
            {
                HandoffCloseClaim::Unclaimed { closing, .. } => {
                    drop(inner);
                    self.shared.cv.notify_all();
                    if let Some(op) = closing {
                        self.shared.close_claimed_handoff(&old_by_id, &op);
                    }
                    continue;
                }
                HandoffCloseClaim::Taken => {
                    let now = Instant::now();
                    if now >= deadline {
                        drop(inner);
                        return Ok(self.attach_op_stalled_reply(&sid));
                    }
                    drop(
                        self.shared
                            .cv
                            .wait_timeout(inner, deadline - now)
                            .unwrap_or_else(|e| e.into_inner()),
                    );
                    continue;
                }
                HandoffCloseClaim::None => {}
            }
            if inner.stopping {
                return Err(stopping_error());
            }
            if let Some(other) = inner
                .sessions
                .values()
                .find(|o| o.sid != sid && o.profile.device_by_id == by_id)
            {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "DEVICE_ALREADY_BOUND",
                    "device_by_id" => by_id.clone(),
                    "session_id" => other.sid.clone()
                });
            }
            break (sid, io);
        };
        let mut result = self.shared.request(
            &sid,
            SessionRequest::Bind {
                by_id: by_id.clone(),
            },
            QUERY_TIMEOUT + ATTACH_OP_WAIT,
        )?;
        if !is_ok(&result) || result.get("already_bound") == Some(&PyValue::Bool(true)) {
            return Ok(result);
        }
        // 未交給正式 bridge 的預開 handle（舊 by_id）關閉：Python 自 `_preopened_ports` 取出後關閉、例外忽略
        // （`origin/main:sw_core/session_manager.py:3776-3778`、`:3796-3800`）。
        let old_by_id = lock(&io.profile).device_by_id.clone();
        let claimed = {
            let mut inner = lock(&self.shared.inner);
            Shared::claim_handoff_locked(&mut inner, &old_by_id, false)
        };
        if let HandoffClaim::Unclaimed { pre: Some(pre), .. } = claimed {
            self.shared.close_session_preopened(&old_by_id, pre);
        }
        if let Some(pre) = lock(&io.preopened).take() {
            self.shared.close_session_preopened(&old_by_id, pre);
        }
        let has_device = {
            let mut inner = lock(&self.shared.inner);
            if let Some(e) = inner.sessions.get_mut(&sid) {
                e.profile.device_by_id = by_id.clone();
                lock(&e.io.profile).device_by_id = by_id.clone();
            }
            inner.devices.contains_key(&by_id)
        };
        let commit_sid = sid.clone();
        let commit_by = by_id.clone();
        self.shared
            .commit_sync(move |m| {
                m.apply(StateDelta::Unreleased {
                    sid: commit_sid.clone(),
                });
                m.apply(StateDelta::Binding {
                    sid: commit_sid,
                    by_id: Some(commit_by),
                });
            })
            .map_err(|e| commit_error(&e))?;
        if has_device {
            // 改綁目標若有未消費的預開作業（佔位退休落盤失敗留下）：Python `bind_session` 收尾 `_spawn_attach`，
            // `_attach_by_id` 取走 `_preopened_ports` 的作業並以同一 handle 建 bridge（`origin/main:sw_core/session_manager.py:4852-4854`、
            // `:4982`），因此把 handle 交給本 session 的正式 bridge，不重新開埠。
            // 移出在途表與寫入交接表在同一臨界區（Python `:4852-4854`），release／shutdown／bind 在任何時點都查得到。
            let parked = {
                let mut inner = lock(&self.shared.inner);
                let claimed = inner
                    .inflight
                    .get(&by_id)
                    .is_some_and(|op| op.parked.swap(false, Ordering::SeqCst));
                let op = if claimed { inner.inflight.remove(&by_id) } else { None };
                op.map(|op| {
                    let current = inner.device_path(&by_id).map(str::to_string);
                    match op.take_preopened() {
                        Some(pre) if current.as_deref() == Some(pre.real_path.as_str()) => {
                            Shared::place_handoff_locked(&mut inner, &by_id, &io, pre)
                        }
                        Some(pre) => vec![pre],
                        None => Vec::new(),
                    }
                })
            };
            if let Some(to_close) = parked {
                for pre in to_close {
                    self.shared.close_session_preopened(&by_id, pre);
                }
                self.shared.cv.notify_all();
            }
            #[cfg(feature = "test-fakes")]
            {
                // 先放開鎖再執行：暫停期間其他 bind 仍須能讀取（一次性）暫停點。
                let hook = lock(&self.shared.bind_pre_device_online_hook).take();
                if let Some(hook) = hook {
                    hook();
                }
            }
            // Python：裝置在線 → 回覆前活 session 即 ATTACHING、last_error None，再 spawn attach。
            // 經請求通道的 DeviceOnline 套用並回覆活 session（審查 F70：不得只改回應副本）。
            let real = lock(&self.shared.inner).device_path(&by_id).map(str::to_string);
            if let Some(real_path) = real.filter(|_| !lock(&self.shared.inner).stopping) {
                let applied = self.shared.request(
                    &sid,
                    SessionRequest::DeviceOnline {
                        by_id: by_id.clone(),
                        real_path,
                    },
                    QUERY_TIMEOUT,
                );
                if let Some(PyValue::Dict(mut live)) =
                    applied.ok().and_then(|r| r.get("session").cloned())
                {
                    // alias 以 manager 鏡像為準（同 `session_public`）。
                    if let Some(e) = lock(&self.shared.inner).sessions.get(&sid) {
                        live.insert("alias", PyValue::from(e.profile.alias.clone()));
                    }
                    result.insert("session", PyValue::Dict(live));
                }
            }
        }
        Ok(result)
    }

    /// bind 重試前收尾舊 by_id 已認領的未消費預開作業（Python `bind_session` 的 `pending_attach_unconsumed` 分支，
    /// `origin/main:sw_core/session_manager.py:3680-3694`）：Python 已先把作業自 `_preopened_ports` 取出（`:3681`），
    /// `_finish_attach_op` 關閉失敗只告警並回 False（`:4322-4332`），不放回任何表、也不寫 `_draining_by_ids`（該表只由
    /// bridge 排空寫入 `:1324`、`:2774-2775`）。作業的 `session_id` 為 `dynamic:<by_id>`（`:3608`）查無 session，不回
    /// RECOVERING（`:3697-3700`）而遞迴重試（`:3727`），改綁照常 `_spawn_attach`（`:3810-3812`）。
    ///
    /// 作業在關閉期間留在 `inflight`（其他入口視同在途而不開埠），回條到後不論結果一律移除；關閉失敗只告警。
    fn close_parked_for_bind(&self, by_id: &str, op: &AttachOp) {
        if let Some(pre) = op.take_preopened() {
            self.shared.close_session_preopened(by_id, pre);
        }
        let mut inner = lock(&self.shared.inner);
        if inner
            .inflight
            .get(by_id)
            .is_some_and(|current| Arc::ptr_eq(&current.cancelled, &op.cancelled))
        {
            inner.inflight.remove(by_id);
        }
        drop(inner);
        self.shared.cv.notify_all();
    }

    /// #26：attach op 取消後 3000 ms 內未收斂（Python 設 RECOVERING＋drain_stalled 並回 SESSION_RECOVERING）。
    /// #26 逾時：Python `bind_session` 直接寫活 session（state RECOVERING、drain_stalled、drain_timeout_epoch＝
    /// attach op 的 bridge_generation），回應取自寫入後的 session（審查 F64：不得只改回應副本）。
    fn attach_op_stalled_reply(&self, sid: &str) -> PyDict {
        let epoch = self
            .shared
            .session_public(sid)
            .and_then(|s| s.get("bridge_generation").and_then(PyValue::as_i64))
            .unwrap_or(0)
            .max(0) as u64;
        match self
            .shared
            .request(sid, SessionRequest::MarkAttachStalled { epoch }, QUERY_TIMEOUT)
        {
            Ok(reply) => reply,
            Err(_) => pydict! {
                "ok" => false,
                "error_code" => "SESSION_RECOVERING",
                "drain_stalled" => true,
                "drain_timeout_epoch" => PyValue::from(epoch as i64),
            },
        }
    }

    /// `_resolve_device_key`：selector → (device_key, 是否為 yaml-target)。
    fn resolve_device_key(&self, selector: &str) -> Option<(String, bool)> {
        let inner = lock(&self.shared.inner);
        for e in inner.sessions.values() {
            if selector == e.sid || selector == e.profile.com || selector == e.profile.alias {
                return Some((
                    e.profile.device_by_id.clone(),
                    e.profile_source == "yaml-target",
                ));
            }
        }
        for e in inner.sessions.values() {
            if e.profile.device_by_id == selector {
                return Some((selector.to_string(), e.profile_source == "yaml-target"));
            }
        }
        if inner.devices.contains_key(selector)
            || selector.starts_with("/dev/serial/by-id/")
            || selector.starts_with("/dev/serial/by-path/")
        {
            return Some((selector.to_string(), false));
        }
        None
    }

    /// `pin_session`：不 reattach（下次 attach 由 #181 再解析套用）。
    pub fn pin(&self, selector: &str, profile: &str) -> Result<PyDict, PyErr> {
        if self.shared.template_by_name(profile).is_none() {
            return Ok(pydict! { "ok" => false, "error_code" => "UNKNOWN_PROFILE" });
        }
        let Some((key, explicit)) = self
            .resolve_device_key(selector)
            .filter(|(k, _)| !k.is_empty())
        else {
            return Ok(pydict! { "ok" => false, "error_code" => "DEVICE_NOT_FOUND" });
        };
        if explicit {
            return Ok(pydict! { "ok" => false, "error_code" => "PROFILE_IS_EXPLICIT" });
        }
        let device_key = key.clone();
        let name = profile.to_string();
        self.shared
            .commit_sync(move |m| {
                m.apply(StateDelta::Pin {
                    device_key,
                    profile: Some(name),
                })
            })
            .map_err(|e| commit_error(&e))?;
        Ok(pydict! { "ok" => true, "device_key" => key, "profile" => profile.to_string() })
    }

    pub fn unpin(&self, selector: &str) -> Result<PyDict, PyErr> {
        let Some((key, explicit)) = self
            .resolve_device_key(selector)
            .filter(|(k, _)| !k.is_empty())
        else {
            return Ok(pydict! { "ok" => false, "error_code" => "DEVICE_NOT_FOUND" });
        };
        if explicit {
            return Ok(pydict! { "ok" => false, "error_code" => "PROFILE_IS_EXPLICIT" });
        }
        let device_key = key.clone();
        self.shared
            .commit_sync(move |m| {
                m.apply(StateDelta::Pin {
                    device_key,
                    profile: None,
                })
            })
            .map_err(|e| commit_error(&e))?;
        Ok(pydict! { "ok" => true, "device_key" => key })
    }

    /// `alias.list`：`{"ok":true,"aliases":[...]}`（依 alias 字典序）。
    pub fn alias_list(&self) -> PyDict {
        let reg = AliasRegistry::load(self.shared.model_snapshot().aliases);
        let rows: Vec<PyValue> = reg.list_alias().into_iter().map(PyValue::Dict).collect();
        pydict! { "ok" => true, "aliases" => PyValue::List(rows) }
    }

    /// `set_alias_for_session`：manager 側 profile alias 立即生效；重啟後依 YAML／template 重算（WAL-D15 KEEP）。
    ///
    /// Python `session_manager.py:2612-2620` 的存在檢查、更新與 `_save_state()` 全在 manager lock 內；OD-63 佔位退休
    /// （`retire_released_placeholder_locked`，Python `:3640-3652`）同樣持 manager 鎖移除 entry。Rust 等 actor 確認時
    /// 不能持鎖，故提交前於 manager 鎖內重驗同一 entry（同一 actor）仍在，並持鎖完成持久化；退休後到的 alias
    /// 回 `SESSION_NOT_FOUND`，不寫回已退休 sid（wp510 第三次整包審查 Medium）。鎖序 inner → model，與
    /// `session_from_template_locked` 相同。
    pub fn alias_set(&self, sid: &str, alias: &str) -> Result<PyDict, PyErr> {
        let not_found = || {
            pydict! {
                "ok" => false,
                "error_code" => "SESSION_NOT_FOUND",
                "session_id" => sid.to_string()
            }
        };
        let actor = {
            let mut inner = lock(&self.shared.inner);
            let Some(e) = inner.sessions.get_mut(sid) else {
                return Ok(not_found());
            };
            e.profile.alias = alias.to_string();
            lock(&e.io.profile).alias = alias.to_string();
            e.actor.clone()
        };
        let still_same_session = |inner: &Inner| {
            inner
                .sessions
                .get(sid)
                .is_some_and(|e| Arc::ptr_eq(&e.actor, &actor))
        };
        // actor 持有的 profile 一併更新並等確認（GetState／submit 等回應中的 `session.alias`；sol-backfill p5 #3）。
        let applied = match actor.request(
            SessionRequest::SetAlias {
                alias: alias.to_string(),
            },
            QUERY_TIMEOUT,
        ) {
            Ok(applied) => applied,
            // 確認前 session 已退休（actor 隨之停止）：Python 持鎖時只會看到 session 不存在。
            Err(_) if !still_same_session(&lock(&self.shared.inner)) => return Ok(not_found()),
            Err(error) => return Err(error),
        };
        if !is_ok(&applied) {
            return Ok(applied);
        }
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = lock(&self.shared.alias_pre_commit_hook).as_ref() {
            hook();
        }
        let inner = lock(&self.shared.inner);
        if !still_same_session(&inner) {
            return Ok(not_found());
        }
        let row = alias_row(sid, alias, &self.shared.now_iso());
        let name = alias.to_string();
        let committed = self.shared.commit_sync(move |m| {
            m.apply(StateDelta::AliasSet {
                alias: name,
                row: Some(row),
            })
        });
        drop(inner);
        committed.map_err(|e| commit_error(&e))?;
        Ok(pydict! { "ok" => true, "session_id" => sid.to_string(), "alias" => alias.to_string() })
    }

    pub fn alias_assign(
        &self,
        by_id: &str,
        alias: &str,
        profile: Option<&str>,
    ) -> Result<PyDict, PyErr> {
        let mut reg = AliasRegistry::default();
        reg.assign_by_id(by_id, alias, profile, &self.shared.now_iso());
        let row = reg.dump().shift_remove(alias);
        let name = alias.to_string();
        self.shared
            .commit_sync(move |m| m.apply(StateDelta::AliasSet { alias: name, row }))
            .map_err(|e| commit_error(&e))?;
        Ok(
            pydict! { "ok" => true, "alias" => alias.to_string(), "device_by_id" => by_id.to_string() },
        )
    }

    /// `unassign_alias`：先寫 state.json（不論是否存在），再回報結果。
    pub fn alias_unassign(&self, alias: &str) -> Result<PyDict, PyErr> {
        let existed = self.shared.model_snapshot().aliases.contains_key(alias);
        let name = alias.to_string();
        self.shared
            .commit_sync(move |m| {
                m.apply(StateDelta::AliasSet {
                    alias: name,
                    row: None,
                })
            })
            .map_err(|e| commit_error(&e))?;
        if !existed {
            return Ok(pydict! {
                "ok" => false,
                "error_code" => "ALIAS_NOT_FOUND",
                "alias" => alias.to_string()
            });
        }
        Ok(pydict! { "ok" => true, "alias" => alias.to_string() })
    }

    /// P6 Task 10：RPC thread 預建、尚未被 core `ConsoleAdopt`／`ConsoleDiscard` 結算的 PTY 數（全部 session）。
    pub fn pending_pty_count(&self) -> usize {
        lock(&self.shared.inner)
            .sessions
            .values()
            .map(|e| lock(&e.io.pending_pty).len())
            .sum()
    }

    /// `attach_console`（`session_manager.py:6740-6773`）：PTY 後端先在 RPC thread 建 PTY（`create_pty`），
    /// 以 token 暫存後送 `ConsoleAttach{pty: Some(PtyInfo)}`；core 回 `ConsoleAdopt` 時交給 hub、`ConsoleDiscard`
    /// 時丟棄。request 失敗（逾時等）時 PTY 仍在暫存區者於此丟棄，不洩漏 fd。
    pub fn console_attach(&self, selector: &str, label: Option<String>, timeout: Duration) -> Result<PyDict, PyErr> {
        self.pump();
        let found = {
            let inner = lock(&self.shared.inner);
            inner.resolve(selector).map(|e| (e.sid.clone(), e.io.clone()))
        };
        let Some((sid, io)) = found else {
            return Ok(console_not_ready(selector));
        };
        let mut token = None;
        let pty = if self.shared.console.kind() == sw_pal::ConsoleKind::Pty {
            let pair = self
                .shared
                .console
                .create_pty()
                .map_err(|e| PyErr::value_error(e.py_message.clone()))?;
            let t = io.next_pty_token.fetch_add(1, Ordering::Relaxed) + 1;
            let info = crate::session_core::PtyInfo {
                token: t,
                slave_path: pair.slave_path.clone(),
                client_id: SessionCore::new_console_client_id(&fill_random),
            };
            lock(&io.pending_pty).insert(t, pair);
            token = Some(t);
            Some(info)
        } else {
            None
        };
        self.shared.send_console_scan(&sid);
        let result = self
            .shared
            .request(&sid, SessionRequest::ConsoleAttach { label, pty }, timeout);
        if let Some(t) = token {
            lock(&io.pending_pty).remove(&t);
        }
        Ok(with_raw_selector(result?, selector))
    }

    /// `detach_console`／`list_consoles`：查無 session 一律 `SESSION_NOT_READY{selector}`（Python `get_session` 為 None）。
    pub fn console_request(&self, selector: &str, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr> {
        self.pump();
        let Some(sid) = self.resolve(selector) else {
            return Ok(console_not_ready(selector));
        };
        self.shared.send_console_scan(&sid);
        Ok(with_raw_selector(self.shared.request(&sid, req, timeout)?, selector))
    }

    /// interactive_send／status／close 只帶 interactive_id，以 manager 的索引路由；查無 → `INTERACTIVE_NOT_FOUND`。
    pub fn route_interactive(
        &self,
        interactive_id: &str,
        req: SessionRequest,
        timeout: Duration,
    ) -> Result<PyDict, PyErr> {
        self.pump();
        let not_found = || {
            pydict! {
                "ok" => false,
                "error_code" => "INTERACTIVE_NOT_FOUND",
                "interactive_id" => interactive_id.to_string()
            }
        };
        let Some(sid) = lock(&self.shared.inner)
            .interactive
            .get(interactive_id)
            .cloned()
        else {
            return Ok(not_found());
        };
        let is_close = matches!(req, SessionRequest::InteractiveClose { .. });
        // 07 D-09：Python `_refresh_interactive_locked`／close 的 `console_has_external_peer` 皆即時掃描。
        self.shared.send_console_scan(&sid);
        let result = self.shared.request(&sid, req, timeout)?;
        if str_of(&result, "error_code").as_deref() == Some("INTERACTIVE_NOT_FOUND") {
            lock(&self.shared.inner).interactive.remove(interactive_id);
            return Ok(not_found());
        }
        if is_close && is_ok(&result) {
            lock(&self.shared.inner).interactive.remove(interactive_id);
        }
        Ok(result)
    }

    /// `new()` 時建立並 `wal.set_health_sink` 一次；每個 session fence 建立時 register。
    pub fn wal_health_fanout(&self) -> Arc<WalHealthFanout> {
        self.shared.fanout.clone()
    }

    /// DeviceWatcher Tick：Slice 2.1 無 readiness 自動重探（Phase 08），只依序處理已排入的事件。
    pub fn on_tick(&self) {
        self.pump();
    }

    /// 以注入的 `Clock` 產生 ISO 時間字串（service 層 `command.cancel` 的 `done_at`；Python `now_iso()`）。
    pub fn now_iso(&self) -> String {
        self.shared.now_iso()
    }

    /// `(sessions, devices)`。
    pub fn counts(&self) -> (usize, usize) {
        let inner = lock(&self.shared.inner);
        (inner.sessions.len(), inner.devices.len())
    }

    pub fn attached_real_paths(&self) -> Vec<String> {
        self.list_sessions()
            .iter()
            .filter_map(|s| str_of(s, "attached_real_path"))
            .collect()
    }

    /// `mcu.status` 用（02 §7.2；service `_flash_candidates`）。
    pub fn flash_candidates(&self) -> Vec<PyDict> {
        let devices: HashMap<String, String> = lock(&self.shared.inner)
            .devices
            .iter()
            .map(|(k, d)| (k.clone(), d.real_path.clone()))
            .collect();
        self.list_sessions()
            .into_iter()
            .filter(|s| s.get("command_capable") != Some(&PyValue::Bool(true)))
            .filter(|s| matches!(str_of(s, "state").as_deref(), Some("READY" | "ATTACHED")))
            .map(|s| {
                let by_id = s.get("device_by_id").cloned().unwrap_or(PyValue::Null);
                let real = by_id
                    .as_str()
                    .and_then(|b| devices.get(b).cloned())
                    .map(PyValue::from)
                    .unwrap_or(PyValue::Null);
                pydict! {
                    "com" => s.get("com").cloned().unwrap_or(PyValue::Null),
                    "by_id" => by_id,
                    "real_path" => real,
                    "command_capable" => false
                }
            })
            .collect()
    }

    /// P8 Task 8：**原子**收集 flash 偵測候選並標記 flash-critical（Python `collect_flash_candidates_and_mark`，
    /// `session_manager.py:3265`；#83 RACE-2 final）。候選＝非 command_capable、state ∈ {READY, ATTACHED}、無前景操作。
    ///
    /// session 狀態由各自的 actor 持有，manager 無法與 Python 一樣在一把鎖內同時讀快照與標記；改為**先標記、後取
    /// 快照**：每個非 command_capable 的 session 先在 `Inner` 鎖內標 flash-critical（同步寫入該 session 的 Fence），
    /// 之後到達的 clear／release／bind／recover 一律 `FLASHING_BUSY`；再向 actor 取快照，不符候選條件者撤回本次新增
    /// 的標記（既有標記屬其他偵測者，不動）。actor 的 `FlashSyncProbeStart` 仍會重驗全部條件（最終授權）。
    pub fn collect_flash_candidates_and_mark(&self) -> Vec<FlashCandidate> {
        self.pump();
        let entries: Vec<(String, String)> = lock(&self.shared.inner)
            .sessions
            .values()
            .filter(|e| !e.profile.template.command_capable())
            .map(|e| (e.sid.clone(), e.profile.com.clone()))
            .collect();
        let mut out = Vec::new();
        for (sid, com) in entries {
            let newly = self.mark_flash_critical(&com);
            let candidate = self.shared.session_public(&sid).filter(|s| {
                s.get("command_capable") != Some(&PyValue::Bool(true))
                    && matches!(str_of(s, "state").as_deref(), Some("READY" | "ATTACHED"))
                    && s.get("foreground_busy") != Some(&PyValue::Bool(true))
                    && s.get("drain_stalled") != Some(&PyValue::Bool(true))
            });
            match candidate {
                Some(public) => {
                    let by_id = str_of(&public, "device_by_id").filter(|b| !b.is_empty());
                    let real_path = by_id.as_ref().and_then(|b| {
                        lock(&self.shared.inner).devices.get(b).map(|d| d.real_path.clone())
                    });
                    out.push(FlashCandidate {
                        com,
                        by_id,
                        real_path,
                        command_capable: false,
                    });
                }
                None if newly => self.unmark_flash_critical(&com),
                None => {}
            }
        }
        out
    }

    /// 標記 COM 為 flash-critical（Python `mark_flash_critical`）；回 true 表示本次新增。
    fn mark_flash_critical(&self, com: &str) -> bool {
        let mut inner = lock(&self.shared.inner);
        let newly = inner.flash_critical.insert(com.to_string());
        for e in inner.sessions.values().filter(|e| e.profile.com == com) {
            e.fence.with(|f| f.flash_critical = true);
        }
        newly
    }

    /// P8 Task 8：解除 COM 的 flash-critical 標記（Python `unmark_flash_critical`）。
    pub fn unmark_flash_critical(&self, com: &str) {
        let mut inner = lock(&self.shared.inner);
        inner.flash_critical.remove(com);
        for e in inner.sessions.values().filter(|e| e.profile.com == com) {
            e.fence.with(|f| f.flash_critical = false);
        }
    }

    /// P8 Task 8：COM 是否在 flash-critical 臨界區（probe 窗口）。
    pub fn flash_critical(&self, com: &str) -> bool {
        lock(&self.shared.inner).flash_critical.contains(com)
    }

    /// P8 Task 9：COM 目前 bridge（Fence 的 `active_epoch`＝actor 的 `conn_epoch`）的 TxWriter；無 active epoch 或該
    /// epoch 的 worker 已收尾 → `None`。FlashCoordinator 以此送 `FlashProbe`／`Flash` TX（permit 仍由 Fence 驗 epoch）。
    pub fn tx_for(&self, com: &str) -> Option<TxWriterHandle> {
        let (epoch, workers) = self.active_workers(com)?;
        let w = lock(&workers);
        w.get(&epoch).map(|worker| worker.tx().clone())
    }

    /// P8 Task 9：COM 目前 bridge 的序列線控制（`/dev/ttyMCU` baud 鏡射）；取值規則同 [`SessionManager::tx_for`]。
    pub fn line_control_for(&self, com: &str) -> Option<Arc<dyn sw_pal::traits::SerialLineControl>> {
        let (epoch, workers) = self.active_workers(com)?;
        let w = lock(&workers);
        w.get(&epoch).map(IoWorker::line_control)
    }

    /// P8 Task 9：把 control 事件送進 COM 的 actor（flash 事件）；查無 session 或送出失敗（含 control lane 滿到
    /// `FLASH_CONTROL_SEND_WAIT` 仍送不進）回 false。不檢查 epoch：結束事件（Released／HolderGone）在 bridge 失效後仍須
    /// 送達以清 flash 狀態，epoch／probe_id 由 actor 驗證。
    pub fn send_control(&self, com: &str, ev: ControlEvent) -> bool {
        let actor = lock(&self.shared.inner)
            .sessions
            .values()
            .find(|e| e.profile.com == com)
            .map(|e| e.actor.clone());
        match actor {
            Some(actor) => actor.control.send_timeout(ev, FLASH_CONTROL_SEND_WAIT).is_ok(),
            None => false,
        }
    }

    /// COM 的 Fence `active_epoch`（＝actor 的 `conn_epoch`）與 worker 表；無 active epoch 回 `None`。
    fn active_workers(&self, com: &str) -> Option<(u64, Arc<Mutex<HashMap<u64, IoWorker>>>)> {
        let inner = lock(&self.shared.inner);
        let e = inner.sessions.values().find(|e| e.profile.com == com)?;
        let epoch = e.fence.with(|f| f.active_epoch)?;
        Some((epoch, e.workers.clone()))
    }

    /// P8 Task 9：經 manager 的 WARNING 出口寫一行（FlashCoordinator／FlashEndpoint 用）。
    pub fn warn(&self, msg: String) {
        self.shared.warn(msg);
    }

    /// 只供測試：COM 的 Fence（斷言 `flash_mode`、注入 freeze）。
    #[cfg(feature = "test-fakes")]
    pub fn fence_for_test(&self, com: &str) -> Option<Arc<Fence>> {
        lock(&self.shared.inner)
            .sessions
            .values()
            .find(|e| e.profile.com == com)
            .map(|e| e.fence.clone())
    }

    /// session_id 對應的 COM 是否 flash-critical。
    fn flash_critical_sid(&self, sid: &str) -> bool {
        let inner = lock(&self.shared.inner);
        inner
            .sessions
            .get(sid)
            .is_some_and(|e| inner.flash_critical.contains(&e.profile.com))
    }

    /// DeviceWatcher → manager 的事件送端（Delta 轉 `update_devices`、Tick 轉 `on_tick`）。
    pub fn watcher_sender(&self) -> Sender<WatcherEvent> {
        self.watcher_tx.clone()
    }

    /// 只供測試（test-fakes）：之後的偵測 PROBE 以凍結的 Fence 送出（驗證 PROBE TX 經 Fence：零 TX）。
    #[cfg(feature = "test-fakes")]
    pub fn freeze_probe_tx_for_test(&self, frozen: bool) {
        self.shared.probe_tx_frozen.store(frozen, Ordering::SeqCst);
    }

    /// 只供測試（test-fakes）：偵測 PROBE 的 Fence 建立後、提交前執行 `hook`（見 `probe_pre_submit_hook`）。
    #[cfg(feature = "test-fakes")]
    pub fn set_probe_pre_submit_hook_for_test(&self, hook: Box<dyn Fn() + Send + Sync>) {
        *lock(&self.shared.probe_pre_submit_hook) = Some(hook);
    }

    /// 只供測試（test-fakes）：設定佔位 release 取消前暫停點。
    #[cfg(feature = "test-fakes")]
    pub fn set_placeholder_release_cancel_hook_for_test(&self, hook: Box<dyn Fn() + Send + Sync>) {
        *lock(&self.shared.placeholder_release_cancel_hook) = Some(hook);
    }

    /// 只供測試（test-fakes）：設定 `open_bridge` 取 session 預開 handle 前的一次性暫停點。
    #[cfg(feature = "test-fakes")]
    pub fn set_open_bridge_pre_take_hook_for_test(&self, hook: Box<dyn FnOnce() + Send>) {
        *lock(&self.shared.open_bridge_pre_take_hook) = Some(hook);
    }

    /// 只供測試（test-fakes）：設定 `open_bridge` 已取走 session 預開 handle、開埠返回前的一次性暫停點。
    #[cfg(feature = "test-fakes")]
    pub fn set_open_bridge_post_take_hook_for_test(&self, hook: Box<dyn FnOnce() + Send>) {
        *lock(&self.shared.open_bridge_post_take_hook) = Some(hook);
    }

    /// 只供測試（test-fakes）：查詢 by_id 的保管帳本紀錄快照。
    #[cfg(feature = "test-fakes")]
    pub fn custody_for_test(&self, by_id: &str) -> Option<crate::custody::CustodySnapshot> {
        lock(&self.shared.inner).custody.get_custody(by_id).map(|c| c.snapshot())
    }

    /// 只供測試（test-fakes）：查詢 token 的終態。
    #[cfg(feature = "test-fakes")]
    pub fn terminal_for_test(&self, token: u64) -> Option<crate::custody::Terminal> {
        lock(&self.shared.inner).custody.terminals.get(&token).cloned()
    }

    /// 只供測試（test-fakes）：查詢 token 是否在 prepared 表中。
    #[cfg(feature = "test-fakes")]
    pub fn prepared_for_test(&self, token: u64) -> bool {
        lock(&self.shared.inner).prepared.contains_key(&token)
    }

    /// 只供測試（test-fakes）：查詢 sid 是否有尚未送達的 DeviceAttached 義務。
    #[cfg(feature = "test-fakes")]
    pub fn delivery_obligation_for_test(&self, sid: &str) -> bool {
        lock(&self.shared.inner).deliverer.obligations.contains_key(sid)
    }

    /// 只供測試（test-fakes）：查詢送達者對 sid 的嘗試次數。
    #[cfg(feature = "test-fakes")]
    pub fn delivery_attempts_for_test(&self, sid: &str) -> usize {
        lock(&self.shared.inner).deliverer.attempts(sid)
    }

    /// 只供測試（test-fakes）：查詢 session 已退休的 epoch 集合。
    #[cfg(feature = "test-fakes")]
    pub fn retired_drains_for_test(&self, sid: &str) -> Vec<u64> {
        let inner = lock(&self.shared.inner);
        if let Some(entry) = inner.sessions.get(sid) {
            if let Ok(res) = entry.actor.request(crate::session_core::SessionRequest::RetiredDrainsForTest, Duration::from_millis(500)) {
                if let Some(PyValue::List(items)) = res.get("epochs") {
                    return items.iter().filter_map(|v| match v {
                        PyValue::Int(PyInt::Small(n)) => Some(*n as u64),
                        _ => None,
                    }).collect();
                }
            }
        }
        Vec::new()
    }

    /// 只供測試（test-fakes）：設定下 n 次 actor spawn 注入失敗。
    #[cfg(feature = "test-fakes")]
    pub fn inject_actor_spawn_fail_for_test(&self, n: usize) {
        crate::actor::inject_actor_spawn_fail(n);
    }

    /// 只供測試（test-fakes）：設定 `by_id` 下一次 `finish_attach_op` 取得 manager 鎖之前的一次性暫停點。
    #[cfg(feature = "test-fakes")]
    pub fn set_finish_attach_op_pre_lock_hook_for_test(
        &self,
        by_id: &str,
        hook: Box<dyn FnOnce() + Send>,
    ) {
        *lock(&self.shared.finish_attach_op_pre_lock_hook) = Some((by_id.to_string(), hook));
    }

    /// 只供測試（test-fakes）：設定 `session.bind` 送出 `DeviceOnline` 之前的一次性暫停點（見 `bind_pre_device_online_hook`）。
    #[cfg(feature = "test-fakes")]
    pub fn set_bind_pre_device_online_hook_for_test(&self, hook: Box<dyn FnOnce() + Send>) {
        *lock(&self.shared.bind_pre_device_online_hook) = Some(hook);
    }

    /// 只供測試（test-fakes）：下一次建立 attach worker 執行緒時模擬 `Builder::spawn()` 回錯。
    #[cfg(feature = "test-fakes")]
    pub fn fail_next_attach_spawn_for_test(&self) {
        self.shared.fail_next_attach_spawn.store(true, Ordering::SeqCst);
    }

    /// 只供測試（test-fakes）：設定 `alias.set` 持久化前暫停點（見 `alias_pre_commit_hook`）。
    #[cfg(feature = "test-fakes")]
    pub fn set_alias_pre_commit_hook_for_test(&self, hook: Box<dyn Fn() + Send + Sync>) {
        *lock(&self.shared.alias_pre_commit_hook) = Some(hook);
    }

    /// 設定 state.json 持久化觀察者（P5-T19 #3／#4／#68；daemon 組裝點於 `new()` 之後、任何 attach 之前呼叫）。
    pub fn set_persist_observer(&self, observer: Arc<dyn PersistObserver>) {
        *lock(&self.shared.persist_observer) = Some(observer);
    }

    /// SC-11 stopping gate：冪等設定 `stopping=true`；之後任何自動或 RPC 路徑的 attach 都不開啟裝置。
    /// 建構子尾端啟動存檔重現的 Python 未捕捉例外末行（有值時 daemon 應印 traceback 並 exit 1）。
    pub fn startup_error(&self) -> Option<String> {
        lock(&self.shared.startup_error).clone()
    }

    pub fn begin_shutdown(&self) {
        let mut inner = lock(&self.shared.inner);
        inner.stopping = true;
        inner.shutdown_started = true;
    }

    /// P8 Task 14：交接停止成立前凍結背景來源與既有 bridge 的新 TX（Python `SessionManager.freeze_for_handoff`）。
    ///
    /// 1. stopping gate：熱插、re-attach、bootstrap 不再開 UART、不建 worker（`stopping`）；交接凍結期間新建的
    ///    session fence 承接凍結（`handoff_frozen`）。
    /// 2. 所有 session fence 與在途 PROBE fence 設 `handoff_frozen`：之後任何來源（重探、login、human raw、console 行、
    ///    flash）的新 TX 一律被拒（零 TX）；已經 gate 提交的寫入段須在 `per_fence` 內收斂。
    ///
    /// 回傳未收斂的 COM（空＝凍結成立）。未收斂且本次為新凍結時，撤銷本次建立的凍結（fence 的 `handoff_frozen`、
    /// stopping gate 還原為 shutdown 是否已開始）；shutdown／drain 的 `frozen` 不動。已凍結者保持凍結（冪等）。
    /// reader 與埠保持開啟；實際排空仍由停止序的 drain 完成。
    pub fn freeze_for_handoff(&self, per_fence: Duration) -> Vec<String> {
        let (was_frozen, fences) = {
            let mut inner = lock(&self.shared.inner);
            let was = inner.handoff_frozen;
            inner.handoff_frozen = true;
            inner.stopping = true;
            let fences: Vec<(String, Arc<Fence>)> = inner
                .sessions
                .values()
                .map(|e| (e.profile.com.clone(), e.fence.clone()))
                .collect();
            (was, fences)
        };
        let probes: Vec<Arc<Fence>> = {
            let mut probes = lock(&self.shared.handoff_probes);
            probes.frozen = true;
            probes.live.clone()
        };
        let mut unconverged: Vec<String> = Vec::new();
        // 旗標已設者（撤銷時須清除）；`Locked` 者旗標未設，撤銷時不取其 fence 鎖（OS 提交仍持鎖，避免交接回覆被拖住）。
        let mut flagged: Vec<&Arc<Fence>> = Vec::new();
        let mut locked: Vec<&Arc<Fence>> = Vec::new();
        let targets = fences
            .iter()
            .map(|(com, fence)| (com.as_str(), fence))
            .chain(probes.iter().map(|fence| (PROBE_COM, fence)));
        for (com, fence) in targets {
            match fence.freeze_for_handoff(Instant::now() + per_fence) {
                HandoffFreeze::Converged => flagged.push(fence),
                HandoffFreeze::InFlight => {
                    flagged.push(fence);
                    unconverged.push(com.to_string());
                }
                HandoffFreeze::Locked => {
                    locked.push(fence);
                    unconverged.push(com.to_string());
                }
            }
        }
        if was_frozen {
            // 已凍結者保持凍結：先前交接已成立，持鎖中的提交結束後也必須帶上旗標（等待以 OS 提交本身為界）。
            for fence in locked {
                fence.with(|st| st.handoff_frozen = true);
            }
        } else if !unconverged.is_empty() {
            // 交接未成立：撤銷本次新建立的凍結（SC-11：忙碌不凍結、不停止）。
            {
                let mut inner = lock(&self.shared.inner);
                inner.handoff_frozen = false;
                inner.stopping = inner.shutdown_started;
            }
            lock(&self.shared.handoff_probes).frozen = false;
            for fence in flagged {
                fence.thaw_handoff();
            }
        }
        unconverged.sort();
        unconverged.dedup();
        unconverged
    }

    /// P8 Task 14：selector（session_id／COM／alias）對應的 COM（admission ticket 的 COM 歸屬）。
    pub fn com_of(&self, selector: &str) -> Option<String> {
        lock(&self.shared.inner).resolve(selector).map(|e| e.profile.com.clone())
    }

    /// P8 Task 14：interactive_id 對應 session 的 COM（admission ticket 的 COM 歸屬；查無回 `None`）。
    pub fn interactive_com(&self, interactive_id: &str) -> Option<String> {
        let inner = lock(&self.shared.inner);
        let sid = inner.interactive.get(interactive_id)?;
        inner.sessions.get(sid).map(|e| e.profile.com.clone())
    }

    /// 先隱含呼叫 `begin_shutdown`；回傳每個 drain 目標是否在 `budget` 內收斂。
    /// drain 集合＝有 bridge（或 attach／drain 進行中）的 session ∪ inflight attach worker（`attach:<by_id>`）。
    /// 等同 [`SessionManager::shutdown_phase1`] 後立即 [`SessionManager::shutdown_phase2`]（daemon 停止序在兩者之間
    /// join RPC handler，P8 Task 12）。
    pub fn shutdown(&self, budget: Duration) -> Vec<(String, bool)> {
        let drain = self.shutdown_phase1(Instant::now() + budget);
        self.shutdown_phase2(drain)
    }

    /// 停止序步驟 3（P8 Task 12，arch §6.1）：隱含 `begin_shutdown`、取消 inflight attach，對每個需 drain 的 session
    /// 送出 Shutdown（actor 內同步執行 Phase 1：fence 凍結、取消進行中的檔案傳輸、排隊命令 `FLUSHED_BY_SHUTDOWN`），
    /// 再以同一 control lane 的查詢作屏障，返回時 Phase 1 已在每個仍存活的 actor 執行過。等待回條屬 Phase 2。
    pub fn shutdown_phase1(&self, deadline: Instant) -> ShutdownDrain {
        self.begin_shutdown();
        type ShutdownTargets = (
            Vec<(String, Arc<ActorHandle>)>,
            Vec<String>,
            Vec<(String, AttachOp)>,
        );
        let (entries, inflight, unconsumed): ShutdownTargets = {
            let inner = lock(&self.shared.inner);
            for op in inner.inflight.values() {
                op.cancel();
            }
            (
                inner
                    .sessions
                    .values()
                    .map(|e| (e.sid.clone(), e.actor.clone()))
                    .collect(),
                inner.inflight.keys().cloned().collect(),
                inner
                    .inflight
                    .iter()
                    .filter(|(_, op)| op.parked.swap(false, Ordering::SeqCst))
                    .map(|(by_id, op)| (by_id.clone(), op.clone()))
                    .collect(),
            )
        };
        // Python `shutdown_drain` 對未消費的 `_preopened_ports` 作業直接 `_finish_attach_op`（關閉 handle；關閉失敗保留作業、
        // 列為 stalled，`origin/main:sw_core/session_manager.py:1218-1227`、`:1231-1239`）。
        for (by_id, op) in unconsumed {
            let receipt = op.close_preopened_with_receipt();
            self.shared.finish_attach_op(&by_id, &op, receipt);
        }

        let mut waits: Vec<(String, Option<Receiver<Result<PyDict, PyErr>>>)> = Vec::new();
        let mut barriers: Vec<Arc<ActorHandle>> = Vec::new();
        for (sid, actor) in entries {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let state = actor
                .request(SessionRequest::GetState, remaining.min(QUERY_TIMEOUT))
                .ok()
                .and_then(|r| session_of(&r))
                .and_then(|s| str_of(&s, "state"));
            if matches!(state.as_deref(), Some("DETACHED" | "RELEASED")) {
                continue;
            }
            let (reply, rx) = crossbeam_channel::bounded(1);
            let sent = actor
                .control
                .send_timeout(ControlEvent::Shutdown { reply }, remaining)
                .is_ok();
            if sent {
                barriers.push(actor.clone());
            }
            waits.push((sid, sent.then_some(rx)));
        }
        // Phase 1 屏障：Shutdown 與查詢同走 control lane（FIFO），查詢有回應（或 actor 已結束）即表示 Phase 1 已執行。
        for actor in barriers {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let _ = actor.request(SessionRequest::GetState, remaining.min(QUERY_TIMEOUT));
        }
        ShutdownDrain {
            deadline,
            waits,
            inflight,
        }
    }

    /// 停止序步驟 5（P8 Task 12）：在 Phase 1 的同一期限內等待每個 drain 目標的回條；回傳是否收斂。
    pub fn shutdown_phase2(&self, drain: ShutdownDrain) -> Vec<(String, bool)> {
        let ShutdownDrain {
            deadline,
            waits,
            inflight,
        } = drain;
        let mut handoff_close: Vec<(String, AttachOp)> = Vec::new();
        let mut handoff_wait: Vec<String> = Vec::new();
        let mut out = Vec::new();
        for (sid, rx) in waits {
            // 只有 `ok:true` 的收斂回覆算排空；同一 stop 的 DRAIN_STALLED 回覆（release 併入的等待者）不算。
            let drained = rx.is_some_and(|rx| {
                rx.recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    .is_ok_and(|r| r.is_ok_and(|reply| is_ok(&reply)))
            });
            out.push((sid, drained));
        }
        let wait_attach = |by_id: &str| -> bool {
            let mut inner = lock(&self.shared.inner);
            while inner.inflight.contains_key(by_id) {
                let now = Instant::now();
                if now >= deadline {
                    break;
                }
                inner = self
                    .shared
                    .cv
                    .wait_timeout(inner, deadline - now)
                    .unwrap_or_else(|e| e.into_inner())
                    .0;
            }
            !inner.inflight.contains_key(by_id)
        };
        let mut reported: HashSet<String> = HashSet::new();
        for by_id in inflight {
            out.push((format!("attach:{by_id}"), wait_attach(&by_id)));
            reported.insert(by_id);
        }
        // 交給 session、尚未由正式 bridge 接手的 handle（`HandoffEntry`；Python 作業在 `_active_attach_ops`）：session 的
        // Shutdown 已讓排入的 `open_bridge` 收尾（stopping gate 關閉 handle）；仍未取走者（actor 已忽略 `DeviceAttached`，或
        // session 為 DETACHED／RELEASED 而未送 Shutdown）代替中止的 attach 關閉，關閉未確認即保留並由末段列為 stalled
        // （`origin/main:sw_core/session_manager.py:1175-1190`、`:1218-1239`、`_finish_attach_op` `:4322-4332`）；
        // 已取走者等待開埠收尾。
        {
            let mut inner = lock(&self.shared.inner);
            let mut keys: Vec<String> = inner.handoff.keys().cloned().collect();
            keys.sort();
            for by_id in keys {
                match self
                    .shared
                    .claim_handoff_for_close_locked(&mut inner, &by_id, false)
                {
                    HandoffCloseClaim::Unclaimed {
                        closing: Some(op), ..
                    } => handoff_close.push((by_id, op)),
                    HandoffCloseClaim::Taken => handoff_wait.push(by_id),
                    HandoffCloseClaim::Unclaimed { closing: None, .. } | HandoffCloseClaim::None => {}
                }
            }
        }
        for (by_id, op) in handoff_close {
            self.shared.close_claimed_handoff(&by_id, &op);
        }
        self.shared.cv.notify_all();
        for by_id in handoff_wait {
            if reported.contains(&by_id) {
                continue;
            }
            let settled = {
                let mut inner = lock(&self.shared.inner);
                while inner.handoff.get(&by_id).is_some_and(|entry| entry.taken) {
                    let now = Instant::now();
                    if now >= deadline {
                        break;
                    }
                    inner = self
                        .shared
                        .cv
                        .wait_timeout(inner, deadline - now)
                        .unwrap_or_else(|e| e.into_inner())
                        .0;
                }
                !inner.handoff.get(&by_id).is_some_and(|entry| entry.taken)
            };
            // 開埠收尾後若關閉未確認而保留作業，交由下方末段重收。
            if !settled || !lock(&self.shared.inner).inflight.contains_key(&by_id) {
                out.push((format!("attach:{by_id}"), settled));
                reported.insert(by_id);
            }
        }
        // Python `shutdown_drain` 末段（`origin/main:sw_core/session_manager.py:1231-1239`）重新收集仍未完成的作業：
        // session 排空期間才登記者（正式 bridge 接手前關閉 handle 失敗而保留的作業，見 `close_before_bridge`）同樣列入。
        let mut late: Vec<String> = lock(&self.shared.inner)
            .inflight
            .keys()
            .filter(|by_id| !reported.contains(*by_id))
            .cloned()
            .collect();
        late.sort();
        for by_id in late {
            out.push((format!("attach:{by_id}"), wait_attach(&by_id)));
        }
        out
    }

    /// 目前全部 session 的 id（排序；不查詢 actor，停機收尾時 actor 可能已結束）。
    pub fn session_ids(&self) -> Vec<String> {
        let mut sids: Vec<String> = lock(&self.shared.inner).sessions.keys().cloned().collect();
        sids.sort();
        sids
    }

    /// 只供測試（test-fakes）：manager 累計啟動過的 session actor 與 attach worker thread 數（P8 Task 12：
    /// stopping gate 之後不得再增加，Phase 1 之後的熱插或延後 reconcile 不得新增未納入 drain 的 worker）。
    #[cfg(feature = "test-fakes")]
    pub fn spawned_worker_count(&self) -> usize {
        self.shared.spawned_workers.load(Ordering::SeqCst)
    }

    /// 只供測試（test-fakes）：session 的 fence `wal_unhealthy`（`None`＝查無 session）。
    #[cfg(feature = "test-fakes")]
    pub fn session_fence_wal_unhealthy(&self, selector: &str) -> Option<bool> {
        let inner = lock(&self.shared.inner);
        let e = inner.resolve(selector)?;
        Some(e.fence.with(|f| f.wal_unhealthy))
    }

    /// 只供測試（test-fakes）：讀取 OD-63 RELEASED 佔位標記，不經 RPC 或持久化欄位公開。
    #[cfg(feature = "test-fakes")]
    pub fn released_placeholder(&self, selector: &str) -> Option<bool> {
        lock(&self.shared.inner)
            .resolve(selector)
            .map(|e| e.released_placeholder)
    }

    /// 只供測試（test-fakes）：直接登記 by_id 的未確認關閉（重現 actor 已發出 `OpenBridge` 之後才登記的交錯）。
    #[cfg(feature = "test-fakes")]
    pub fn mark_unconfirmed_close_for_test(&self, by_id: &str) {
        self.shared.unconfirmed_closes.insert(by_id);
    }

    /// 只供測試（test-fakes）：by_id 是否登記為未確認關閉。
    #[cfg(feature = "test-fakes")]
    pub fn unconfirmed_close_for_test(&self, by_id: &str) -> bool {
        self.shared.unconfirmed_closes.contains(by_id)
    }

    /// 只供測試（test-fakes）：注入可觀察的在途 attach 標記，驗證早退不取消也不等待。
    #[cfg(feature = "test-fakes")]
    pub fn insert_inflight_attach_for_test(&self, by_id: &str) -> Arc<AtomicBool> {
        let op = AttachOp::new(by_id, &self.shared.unconfirmed_closes, false, false, None);
        let cancelled = Arc::clone(&op.cancelled);
        lock(&self.shared.inner)
            .inflight
            .insert(by_id.to_string(), op);
        cancelled
    }

    /// 只供測試（test-fakes）：移除測試注入的在途 attach 標記並喚醒等待者。
    #[cfg(feature = "test-fakes")]
    pub fn remove_inflight_attach_for_test(&self, by_id: &str) {
        lock(&self.shared.inner).inflight.remove(by_id);
        self.shared.cv.notify_all();
    }

    /// 只供測試（test-fakes）：指定 by_id 是否有交給 session、尚未由正式 bridge 接手的預開 handle（`HandoffEntry`）。
    #[cfg(feature = "test-fakes")]
    pub fn has_handoff_for_test(&self, by_id: &str) -> bool {
        lock(&self.shared.inner).handoff.contains_key(by_id)
    }

    /// 只供測試（test-fakes）：檢查指定 by_id 的在途 attach 標記仍存在。
    #[cfg(feature = "test-fakes")]
    pub fn has_inflight_attach_for_test(&self, by_id: &str) -> bool {
        lock(&self.shared.inner).inflight.contains_key(by_id)
    }

    /// 只供測試（test-fakes）：移除 selector 對應的持久化 RELEASED 阻擋，保留佔位標記。
    #[cfg(feature = "test-fakes")]
    pub fn clear_released_block_for_test(&self, selector: &str) -> bool {
        let Some(sid) = self.resolve(selector) else {
            return false;
        };
        self.shared
            .commit_sync(move |m| m.apply(StateDelta::Unreleased { sid }))
            .is_ok()
    }

    /// 只供測試（test-fakes）：等待指定 by_id 的 attach worker 結束。
    #[cfg(feature = "test-fakes")]
    pub fn wait_attach_idle_for_test(&self, by_id: &str, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let mut inner = lock(&self.shared.inner);
        while inner.inflight.contains_key(by_id) {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            inner = self
                .shared
                .cv
                .wait_timeout(inner, deadline - now)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
        true
    }

    /// 只供測試（test-fakes）：session 的 `last_error` 變更歷史（依發生順序）。
    #[cfg(feature = "test-fakes")]
    pub fn last_error_history(&self, com: &str) -> Vec<String> {
        let inner = lock(&self.shared.inner);
        inner
            .resolve(com)
            .map(|e| lock(&e.history).clone())
            .unwrap_or_default()
    }

    /// 只供測試（test-fakes）：session 佇列的 `(len, physical_len)`（`None`＝查無 session）。
    #[cfg(feature = "test-fakes")]
    pub fn session_queue_lens(&self, selector: &str) -> Option<(i64, i64)> {
        let sid = self.resolve(selector)?;
        let d = self
            .shared
            .request(&sid, SessionRequest::QueueLensForTest, QUERY_TIMEOUT)
            .ok()?;
        Some((d.get("len")?.as_i64()?, d.get("physical_len")?.as_i64()?))
    }

    /// 只供測試（test-fakes）：直接設定 session 狀態（FLASHING／RECOVERING 等無法由 RPC 到達的狀態）。
    #[cfg(feature = "test-fakes")]
    pub fn force_session_state(&self, selector: &str, state: SessionState) {
        if let Some(sid) = self.resolve(selector) {
            let _ = self.shared.request(
                &sid,
                SessionRequest::ForceStateForTest(state),
                QUERY_TIMEOUT,
            );
        }
    }

    /// 只供測試（test-fakes）：覆寫 actor 公開查詢期限。
    #[cfg(feature = "test-fakes")]
    pub fn set_query_timeout_for_test(&self, timeout: Duration) {
        *lock(&self.shared.query_timeout_override) = Some(timeout);
    }

    /// 只供測試（test-fakes）：暫停下一筆 actor request，用來確定性重現 session 查詢逾時。
    #[cfg(feature = "test-fakes")]
    pub fn pause_next_request_for_test(
        &self,
        selector: &str,
    ) -> Option<(crossbeam_channel::Receiver<()>, crossbeam_channel::Sender<()>)> {
        let sid = self.resolve(selector)?;
        let actor = self.shared.actor_of(&sid)?;
        Some(actor.probe.pause_before_next("Request"))
    }

    /// 只供測試（test-fakes）：暫停下一個自動登入結果 dispatch，確定性安排交接凍結與結果回填的交錯。
    #[cfg(feature = "test-fakes")]
    pub fn pause_next_login_finished_for_test(
        &self,
        selector: &str,
    ) -> Option<(crossbeam_channel::Receiver<()>, crossbeam_channel::Sender<()>)> {
        let sid = self.resolve(selector)?;
        let actor = self.shared.actor_of(&sid)?;
        Some(actor.probe.pause_before_next("LoginFinished"))
    }

    /// 只供測試（test-fakes）：覆寫 file.push／file.pull actor 回覆期限。
    #[cfg(feature = "test-fakes")]
    pub fn set_file_transfer_timeout_for_test(&self, timeout: Duration) {
        *lock(&self.shared.file_transfer_timeout_override) = Some(timeout);
    }
}

/// 依序處理已排入的 watcher 事件與通知（呼叫端須持 `pump_lock`）；回傳是否處理過任何事件。
fn drain_events(
    shared: &Arc<Shared>,
    watcher_rx: &Receiver<WatcherEvent>,
    note_rx: &Receiver<ManagerNote>,
) -> bool {
    let mut any = false;
    loop {
        if let Ok(ev) = watcher_rx.try_recv() {
            shared.handle_watcher(ev);
            any = true;
            continue;
        }
        if let Ok(note) = note_rx.try_recv() {
            shared.handle_note(note);
            any = true;
            continue;
        }
        return any;
    }
}

impl Drop for SessionManager {
    fn drop(&mut self) {
        self.shared.closed.store(true, Ordering::SeqCst);
    }
}

/// Event Engine 查詢（`session_manager.py:1499-1519`）：讀 manager 鏡像與 actor 發布到 fence 的 `foreground_busy`，
/// 不經 actor mailbox。`active_cmd_id_for` 只回字面 `"foreground"`（EVT-D-E3 KEEP）。
impl crate::event_engine::BridgeQueries for SessionManager {
    fn active_cmd_id_for(&self, com: &str) -> Option<String> {
        let inner = lock(&self.shared.inner);
        let e = inner.sessions.values().find(|e| e.profile.com == com)?;
        e.fence
            .with(|f| f.foreground_busy)
            .then(|| "foreground".to_string())
    }

    fn profile_for(&self, com: &str) -> Option<String> {
        let inner = lock(&self.shared.inner);
        inner
            .sessions
            .values()
            .find(|e| e.profile.com == com)
            .map(|e| e.profile.template.profile_name.clone())
    }

    fn known_coms(&self) -> Vec<String> {
        let mut coms: Vec<String> = lock(&self.shared.inner)
            .sessions
            .values()
            .map(|e| e.profile.com.clone())
            .collect();
        coms.sort();
        coms
    }
}
