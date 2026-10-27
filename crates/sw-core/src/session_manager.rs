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
use crate::session_core::{CoreDeps, Effect, ManagerNote, OpGrant, SessionCore, SessionRequest};
use crate::state_store::{LoadedState, ReleasedRow, StateDelta, StateModel, StateStore};
use crate::tx_writer::{Fence, OpId, TxClass, TxJob, TxPermit, spawn_tx_writer};
use crate::uart_bridge::{IoWorker, IoWorkerArgs, RxShared};
use crate::wal::{WalAppendReq, WalHealthFanout, WalWriter};

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
struct HandleParts {
    reader: Box<dyn SerialReader>,
    writer: Box<dyn SerialWriter>,
    waker: Arc<dyn IoWaker>,
    close: CloseToken,
}

impl HandleParts {
    fn split(handle: Box<dyn SerialHandle>) -> HandleParts {
        let (reader, writer, waker, close) = handle.split();
        HandleParts {
            reader,
            writer,
            waker,
            close,
        }
    }

    /// 關閉（未交給正式 bridge 時；回條供呼叫端判斷）。
    fn close(self) -> CloseReceipt {
        self.close.close(self.reader, self.writer)
    }
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
    actor: Arc<ActorHandle>,
    #[cfg_attr(not(feature = "test-fakes"), allow(dead_code))]
    fence: Arc<Fence>,
    io: Arc<SessionIo>,
    #[cfg(feature = "test-fakes")]
    history: Arc<Mutex<Vec<String>>>,
}

/// 進行中的 attach worker（per by_id 去重；`cancelled` 由裝置變更／bind／shutdown 設定）。
#[derive(Clone)]
struct AttachOp {
    cancelled: Arc<AtomicBool>,
}

#[derive(Default)]
struct Inner {
    /// session_id → 鏡像（插入順序＝建立順序，selector 的 tiebreak 與 DETACHED-rebind 候選都依此）。
    sessions: IndexMap<String, Entry>,
    devices: BTreeMap<String, DeviceInfo>,
    /// startup 預配 COM（#100）：by_id → COM。
    pending_com: HashMap<String, String>,
    /// by_id → 進行中的 attach worker。
    inflight: HashMap<String, AttachOp>,
    /// SC-11 stopping gate。
    stopping: bool,
    /// interactive_id → session_id。
    interactive: HashMap<String, String>,
    /// 動態偵測當時的 real_path（sticky 的 TOCTOU 比對；#95 HIGH#2）。
    probe_paths: HashMap<String, String>,
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
    closed: AtomicBool,
    /// state.json 持久化觀察者（P5-T19 #3／#4／#68 的 Stage 0 測試鉤子；未設定時不動作）。
    persist_observer: Mutex<Option<Arc<dyn PersistObserver>>>,
    /// 只供測試（test-fakes）：偵測 PROBE 的 Fence 以 frozen 建立（驗證 PROBE TX 經 Fence 時零 TX）。
    probe_tx_frozen: AtomicBool,
    /// P6 Task 9／10：console 後端（PTY 建立、ConsoleHub 的 mux）。
    console: Arc<dyn ConsoleTransportFactory>,
    /// P6（sol 補審 #2）：human line mode basename 的路徑風格（`caps.path_flavor`）。
    path_flavor: sw_pal::PalPathFlavor,
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
    /// release 持久化 commit 之前（Python `_test_release_persist_point()`）。
    fn before_release_commit(&self);
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

/// SC-11：stopping 後 RPC 路徑的重開（Python `RuntimeError("daemon stopping")`，framing 轉 `EXCEPTION`）。
fn stopping_error() -> PyErr {
    PyErr::value_error("daemon stopping")
}

fn commit_error(e: &PalError) -> PyErr {
    PyErr::value_error(format!("state.json commit failed: {e:?}"))
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

impl Shared {
    fn now_iso(&self) -> String {
        iso_utc_now(self.clock.wall_unix_us())
    }

    fn warn(&self, msg: String) {
        (self.warn)(msg);
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
                o.before_release_commit();
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
            .request(SessionRequest::GetState, QUERY_TIMEOUT)
            .ok()?;
        let mut s = session_of(&r)?;
        // alias 以 manager 鏡像為準（`alias.set` 只改 manager 側 profile；重啟後由 YAML／template 重算）。
        if let Some(e) = lock(&self.inner).sessions.get(sid) {
            s.insert("alias", PyValue::from(e.profile.alias.clone()));
        }
        Some(s)
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
        released: Option<&ReleasedRow>,
        detached_error: Option<String>,
        preopened: Option<Preopened>,
        sid_override: Option<&str>,
    ) -> String {
        let sid = sid_override
            .map(str::to_string)
            .unwrap_or_else(|| format!("{}:{}", profile.template.profile_name, profile.com));
        let fence = Fence::new();
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
        let exec = ManagerExec {
            shared: self.clone(),
            io: io.clone(),
            rx,
            fence: fence.clone(),
            data: mailbox.data_sender(),
            workers: HashMap::new(),
            runners: HashMap::new(),
            hub,
            pumps: HashMap::new(),
            listener_epoch: None,
        };
        let actor = spawn_actor(core, mailbox, Box::new(exec), self.clock.clone());
        inner.sessions.insert(
            sid.clone(),
            Entry {
                sid: sid.clone(),
                profile,
                profile_source: profile_source.to_string(),
                actor: Arc::new(actor),
                fence,
                io,
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
            WatcherEvent::Tick => {}
            WatcherEvent::ScanFailed(e) => self.warn(format!("裝置掃描失敗：{e:?}")),
        }
    }

    fn update_devices(self: &Arc<Self>, snapshot: &[(String, DeviceInfo)]) {
        let new: BTreeMap<String, DeviceInfo> = snapshot.iter().cloned().collect();
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
                    op.cancelled.store(true, Ordering::SeqCst);
                }
            }
            // #100 M2/3a：離線裝置的預配 COM 釋出（#28）。
            let online: HashSet<String> = inner.devices.keys().cloned().collect();
            inner.pending_com.retain(|b, _| online.contains(b));
            prev
        };
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

    /// `_detach_by_id`：未交給正式 bridge 的預開 handle 立即關閉（#27），並通知 actor drain。
    fn detach_by_id(&self, by_id: &str, reason: DetachReason) {
        let target = {
            let inner = lock(&self.inner);
            inner
                .session_by_id(by_id)
                .map(|e| (e.sid.clone(), e.io.clone()))
        };
        let Some((sid, io)) = target else {
            return;
        };
        if let Some(pre) = lock(&io.preopened).take() {
            let _ = pre.parts.close();
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
        let released = self.released_by_ids();
        let pins = self.model_snapshot().profile_pins;
        let mut inner = lock(&self.inner);
        if inner.stopping {
            drop(inner);
            self.warn(format!("shutdown 中，略過 attach {by_id}"));
            return;
        }
        if released.contains(by_id) || inner.inflight.contains_key(by_id) {
            return;
        }
        let Some(real) = inner.device_path(by_id).map(str::to_string) else {
            return;
        };
        if let Some(e) = inner.session_by_id(by_id) {
            let pin_differs = pins
                .get(by_id)
                .and_then(|p| self.template_by_name(p))
                .is_some_and(|t| t.profile_name != e.profile.template.profile_name);
            let needs_reresolve = e.profile_source != "yaml-target"
                && (pin_differs || (e.profile_source == "fallback" && !self.templates.is_empty()));
            if !needs_reresolve {
                let actor = e.actor.clone();
                drop(inner);
                let _ = actor.control.send(ControlEvent::DeviceAttached {
                    by_id: by_id.to_string(),
                    real_path: real,
                });
                return;
            }
        }
        let op = AttachOp {
            cancelled: Arc::new(AtomicBool::new(false)),
        };
        inner.inflight.insert(by_id.to_string(), op.clone());
        drop(inner);
        let me = self.clone();
        let key = by_id.to_string();
        let spawned = std::thread::Builder::new()
            .name(format!("serialwrap-attach-{by_id}"))
            .spawn(move || {
                let _ = catch_unwind(AssertUnwindSafe(|| me.attach_worker(&key, &op)));
                lock(&me.inner).inflight.remove(&key);
                me.cv.notify_all();
            });
        if spawned.is_err() {
            lock(&self.inner).inflight.remove(by_id);
            self.cv.notify_all();
        }
    }

    /// attach worker 本體（`_attach_by_id`）：既有 session → #181 再解析；否則 DETACHED-rebind 或動態偵測。
    fn attach_worker(self: &Arc<Self>, by_id: &str, op: &AttachOp) {
        let existing = lock(&self.inner)
            .session_by_id(by_id)
            .map(|e| e.sid.clone());
        if let Some(sid) = existing {
            self.reattach_existing(&sid, by_id, op);
            return;
        }
        // DETACHED-rebind fallback：DETACHED 且其 by_id 離線的 session，取 act_no 最小者。
        let candidates: Vec<(String, i128)> = {
            let inner = lock(&self.inner);
            inner
                .sessions
                .values()
                .filter(|e| !inner.devices.contains_key(&e.profile.device_by_id))
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
                self.rebind_detached(&sid, by_id);
            }
            return;
        }
        if self.templates.is_empty() {
            return;
        }
        if lock(&self.inner).sessions.len() >= self.max_sessions {
            self.warn(format!(
                "已達 max_sessions={}，忽略裝置 {by_id}",
                self.max_sessions
            ));
            return;
        }
        self.attach_dynamic(by_id, op);
    }

    /// DETACHED-rebind：改綁 session 到新 by_id，寫 binding override，然後 attach。
    fn rebind_detached(self: &Arc<Self>, sid: &str, by_id: &str) {
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
        {
            let mut inner = lock(&self.inner);
            if let Some(e) = inner.sessions.get_mut(sid) {
                e.profile.device_by_id = by_id.to_string();
                lock(&e.io.profile).device_by_id = by_id.to_string();
            }
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
            if inner.stopping {
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

    /// gate 條件：未 stopping、op 未取消、裝置仍在線且 real_path 未變。
    fn still_valid(&self, by_id: &str, real: &str, op: &AttachOp) -> bool {
        let inner = lock(&self.inner);
        !inner.stopping
            && !op.cancelled.load(Ordering::SeqCst)
            && inner.device_path(by_id) == Some(real)
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
        let handle = match self.gated_open(by_id, &real, op) {
            None => return,
            Some(Ok(h)) => h,
            Some(Err(e)) => {
                self.record_dynamic_open_failure(by_id, &real, &e, tpl.as_ref(), source);
                return;
            }
        };
        let mut parts = HandleParts::split(handle);
        if !self.still_valid(by_id, &real, op) {
            // 已越過 gate、open 返回時已 stopping／取消：立即關閉（零 TX）。
            let _ = parts.close();
            return;
        }
        if tpl.is_none() {
            if let Some(t) = self.detect_template(&mut parts) {
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
            let _ = parts.close();
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
            let _ = parts.close();
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
    fn probe_send(&self, parts: &mut HandleParts, payload: &[u8]) {
        let fence = Fence::new();
        let frozen = self.probe_tx_frozen.load(Ordering::SeqCst);
        let wal_unhealthy = self.fanout.current();
        fence.with(|f| {
            f.active_epoch = Some(0);
            f.frozen = frozen;
            f.wal_unhealthy = wal_unhealthy;
        });
        let writer = std::mem::replace(&mut parts.writer, Box::new(DetachedWriter));
        let (control_tx, _control_rx) = crossbeam_channel::unbounded();
        let cancel = CancelToken::new(parts.waker.clone());
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
            },
            payload: payload.to_vec(),
            reply: reply_tx,
        };
        if handle.jobs.send(job).is_ok() {
            let _ = handle.wait_report(&reply_rx);
        }
        stop.send();
        if let Ok((writer, _ledger)) = join.join() {
            parts.writer = writer;
        }
    }

    /// `detect_template`：送 `\n`、等待 3.0 s（Clock）、以 RX 快照依序比對候選模板的 prompt（再以 login regex 備選）。
    /// 候選排除 passthrough 與 Slice 2.1 不支援能力（`console_unlock_env`、`post_login_cmd`）。RX／TX 以 `com=PROBE` 寫 WAL。
    fn detect_template(&self, parts: &mut HandleParts) -> Option<ProfileTemplate> {
        let mut buf = vec![0u8; 8192];
        // `clear_rx_buffer`：丟棄開埠後已到的舊資料（仍寫 WAL）。
        let _ = self.probe_drain(parts, &mut buf);
        self.probe_send(parts, b"\n");
        self.clock.sleep(DETECT_PROBE);
        let bytes = self.probe_drain(parts, &mut buf);
        let snapshot = clean_text(&String::from_utf8_lossy(&bytes));
        let mut login_candidate: Option<ProfileTemplate> = None;
        for tpl in &self.templates {
            if tpl.platform == "passthrough"
                || tpl
                    .console_unlock_env
                    .as_deref()
                    .is_some_and(|v| !v.is_empty())
                || !tpl.post_login_cmd.is_empty()
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
        let Some(public) = self.session_public(sid) else {
            return;
        };
        let state = str_of(&public, "state").unwrap_or_default();
        if state == "RELEASED" || self.released_by_ids().contains(by_id) {
            return;
        }
        if state != "DETACHED" {
            // 已有 bridge（或 attach／drain 進行中）：不再解析（再開 PROBE 會 two-reader），交給 actor 判斷。
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
            let _ = parts.close();
            return;
        }
        let detected = self.detect_template(&mut parts);
        if !self.still_valid(by_id, &real, op) {
            let _ = parts.close();
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
        let io = lock(&self.inner)
            .sessions
            .get(&sid_now)
            .map(|e| e.io.clone());
        match io {
            Some(io) => {
                *lock(&io.preopened) = Some(Preopened {
                    real_path: real.clone(),
                    parts,
                });
                lock(&self.inner).probe_paths.insert(sid_now, real);
                self.send_attached(by_id);
            }
            None => {
                let _ = parts.close();
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
            ManagerNote::SessionReady { sid } => self.maybe_persist_sticky(&sid),
            ManagerNote::SessionDetached { sid, .. } => {
                lock(&self.inner).interactive.retain(|_, v| *v != sid);
            }
            ManagerNote::InteractiveOpened {
                sid,
                interactive_id,
            } => {
                lock(&self.inner).interactive.insert(interactive_id, sid);
            }
            ManagerNote::InteractiveClosed { interactive_id } => {
                lock(&self.inner).interactive.remove(&interactive_id);
            }
            ManagerNote::StickyCandidate { .. } => {}
        }
    }

    /// `_maybe_persist_sticky`：只有 READY 的正向偵測（`detected`）且 real_path 與偵測當時相同才寫入。
    fn maybe_persist_sticky(&self, sid: &str) {
        let (by_id, name) = {
            let inner = lock(&self.inner);
            let Some(e) = inner.sessions.get(sid) else {
                return;
            };
            if e.profile_source != "detected" {
                return;
            }
            let Some(probe) = inner.probe_paths.get(sid) else {
                return;
            };
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
            let mut tpl = self
                .template_by_name(&template_name)
                .or_else(|| self.template_by_name(&sid_profile));
            if tpl.is_none() && (sid_profile == "passthrough" || sid_profile == "others-template") {
                // 舊 state.json 不保存 template 內容；只對無命令能力的通用 passthrough 名稱使用最小 profile。
                tpl = Some(minimal_passthrough_template(&sid_profile));
            }
            let Some(tpl) = tpl else {
                self.warn(format!(
                    "released session 的 template 不存在（session={sid}、profile={template_name}），保留持久化阻擋"
                ));
                continue;
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
        if let Err(e) = self.commit_sync(|_| {}) {
            self.warn(format!("state.json 寫入失敗（啟動存檔）：{e:?}"));
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
    workers: HashMap<u64, IoWorker>,
    runners: HashMap<u64, OpRunner>,
    /// P6 Task 9：該 session 的 ConsoleHub（RX 分流、console I/O）。
    hub: Option<Arc<crate::human_console::hub::HubHandle>>,
    /// P6 Task 6（07 D-15(5)）：每個 epoch 一條 human TX pump。
    pumps: HashMap<u64, crate::human_console::human_tx::HumanTxPump>,
    /// TCP console listener 所屬的 bridge epoch（該 epoch `BeginDrain` 時停止，ephemeral port，UIO-12.8-c）。
    listener_epoch: Option<u64>,
}

impl ManagerExec {
    fn com(&self) -> String {
        lock(&self.io.profile).com.clone()
    }

    /// P6 Task 9 步驟 5：PTY 後端且有 hub 時預建哨兵 PTY（隨 `OpenedBridge` 交給 core 同一事件登記；TCP／無 hub 不建，06 §7.2）。
    fn make_sentinel(&mut self) -> Option<crate::session_core::PtyInfo> {
        if self.hub.is_none() || self.shared.console.kind() != sw_pal::ConsoleKind::Pty {
            return None;
        }
        let pair = match self.shared.console.create_pty() {
            Ok(pair) => pair,
            Err(e) => {
                self.shared.warn(format!(
                    "哨兵 primary console 建立失敗（com={}）：{}",
                    self.com(),
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
    fn bind_console_listener(&mut self, epoch: u64) -> Option<String> {
        let hub = self.hub.as_ref()?;
        if self.shared.console.kind() != sw_pal::ConsoleKind::TcpTelnet {
            return None;
        }
        let listener = match self.shared.console.bind_tcp_console() {
            Ok(listener) => listener,
            Err(e) => {
                self.shared.warn(format!(
                    "console listener 建立失敗（com={}）：{}",
                    self.com(),
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
                self.com()
            ));
            return None;
        }
        hub.waker.wake();
        self.listener_epoch = Some(epoch);
        Some(endpoint)
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
            if let Some(worker) = self.workers.get(&epoch) {
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

    /// `OpenBridge`：stopping gate → 預開 handle（同路徑則原樣交接，#51）或 `open_exclusive` → IoWorker。
    fn open_bridge(
        &mut self,
        dev: DevicePath,
        cfg: SerialConfig,
        precheck_holders: bool,
        control: &Sender<ControlEvent>,
    ) -> Result<OpenedBridge, OpenFailure> {
        let stopped = || OpenFailure::Other(PalError::plain(PalErrorKind::Io, "daemon stopping"));
        if lock(&self.shared.inner).stopping {
            if let Some(pre) = lock(&self.io.preopened).take() {
                let _ = pre.parts.close();
            }
            return Err(stopped());
        }
        let pre = lock(&self.io.preopened).take();
        let handle: Box<dyn SerialHandle> = match pre {
            Some(pre) if pre.real_path == dev.0 => Box::new(ReassembledHandle(pre.parts)),
            other => {
                if let Some(pre) = other {
                    let _ = pre.parts.close();
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
            // open 返回時已進 stopping：不交給 actor（零 TX），立即關閉。
            let _ = HandleParts::split(handle).close();
            return Err(stopped());
        }
        let epoch = self.shared.epochs.next();
        let worker = IoWorker::start(
            handle,
            IoWorkerArgs {
                com: self.com(),
                epoch,
                wal: self.shared.wal.clone(),
                rx: self.rx.clone(),
                fence: self.fence.clone(),
                sentinel: None,
                hub: self.hub.as_ref().map(|h| h.tap()),
                data: self.data.clone(),
                control: control.clone(),
                clock: self.shared.clock.clone(),
            },
        );
        let sentinel = self.make_sentinel();
        let console_endpoint = self.bind_console_listener(epoch);
        Ok(OpenedBridge {
            epoch,
            worker,
            real_path: dev.0,
            sentinel,
            console_endpoint,
        })
    }

    /// `StartOp`：交給該 epoch 的 `OpRunner`（Task 14）；無法啟動時以 `SendFailed` 結束 op，避免 actor 永久等待。
    fn start_op(&mut self, grant: OpGrant, spec: OpSpec, control: &Sender<ControlEvent>) {
        let epoch = grant.epoch;
        let op_id = grant.op_id;
        let ran = catch_unwind(AssertUnwindSafe(|| {
            if !self.runners.contains_key(&epoch) {
                let worker = self.workers.get(&epoch)?;
                let ctx = OpCtx {
                    com: self.com(),
                    sid: lock(&self.io.sid).clone(),
                    profile: lock(&self.io.profile).clone(),
                    rx: self.rx.clone(),
                    tx: worker.tx().clone(),
                    wal: self.shared.wal.clone(),
                    clock: self.shared.clock.clone(),
                    control: control.clone(),
                    rng: Arc::new(fill_random),
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
        let Some(worker) = self.workers.get(&epoch) else {
            return;
        };
        let handle = worker.tx().clone();
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
                let io = crate::login_fsm::LoginIo {
                    rx: &rx,
                    tx: &send,
                    clock: &*clock,
                    nonce: &nonce,
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
                let result = self.open_bridge(dev, cfg, precheck_holders, tx);
                let _ = tx.send(ControlEvent::BridgeOpened {
                    epoch_hint: 0,
                    result,
                });
            }
            Effect::AdoptBridge { epoch, worker, .. } => {
                self.workers.insert(epoch, worker);
            }
            Effect::BeginDrain { stop_id, epoch } => {
                self.runners.remove(&epoch);
                if let Some(pump) = self.pumps.remove(&epoch) {
                    pump.stop();
                }
                if let Some(worker) = self.workers.remove(&epoch) {
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
                let result = self.shared.commit_sync_kind(|m| m.apply(delta), release);
                let _ = tx.send(ControlEvent::StateCommitted { commit_id, result });
            }
            Effect::Notify(note) => {
                let _ = self.shared.note_tx.send(note);
            }
            Effect::Warn(msg) => self.shared.warn(msg),
            Effect::SettleTx { epoch, op_id } => {
                if let Some(worker) = self.workers.get(&epoch) {
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
                    // 不留下幽靈 console（審查 1fb9612 Medium）。
                    self.shared.warn(format!(
                        "console 註冊失敗（com={} client_id={client_id}）：PTY 不存在或 hub 佇列已滿",
                        self.com()
                    ));
                    let _ = self.data.try_send(DataEvent::ConsoleDisconnected { client_id, client_lifetime });
                }
            }
            Effect::ConsoleDiscard { token } => {
                lock(&self.io.pending_pty).remove(&token);
            }
            // P6 Task 6（07 D-15(5)）：交給 per-epoch pump（try_send，actor 永不阻塞）；送不出去以 NotSubmitted 回報。
            Effect::HumanTx { tag, permit, payload } => self.human_tx(tag, permit, payload, tx),
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
        let max_sessions = pyint_usize(&load.max_sessions, 16);
        let shared = Arc::new(Shared {
            serial,
            uart_excl,
            wal,
            registry,
            clock,
            env,
            fs,
            warn,
            store: state,
            model: Mutex::new(StateModel::new(loaded)),
            fanout,
            epochs: EpochAllocator::new(),
            templates: load.templates.clone(),
            max_sessions,
            inner: Mutex::new(Inner::default()),
            cv: Condvar::new(),
            note_tx,
            closed: AtomicBool::new(false),
            persist_observer: Mutex::new(None),
            probe_tx_frozen: AtomicBool::new(false),
            console: console.clone(),
            path_flavor: caps.path_flavor,
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

    /// 將 session 請求路由到對應 actor 並等待回覆。
    pub fn route(
        &self,
        sid: &str,
        req: SessionRequest,
        timeout: Duration,
    ) -> Result<PyDict, PyErr> {
        self.pump();
        let stopping = lock(&self.shared.inner).stopping;
        match &req {
            SessionRequest::Bind { by_id } => return self.bind(sid, by_id),
            SessionRequest::Clear => return self.clear(sid, timeout),
            SessionRequest::Recover { .. } | SessionRequest::DeviceAttach { .. }
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
            SessionRequest::GetState | SessionRequest::Activity | SessionRequest::InteractiveOpen { .. }
        ) {
            // 07 D-09：session 摘要與 interactive_open（Python `_refresh_interactive_locked` 即時掃 peer）前先更新 console peer 判定。
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

    /// `clear_session`：RELEASED 早退；stopping → `Err("daemon stopping")`；否則 drain(CLEARED) 後重新 attach。
    fn clear(&self, sid: &str, timeout: Duration) -> Result<PyDict, PyErr> {
        let Some(public) = self.shared.session_public(sid) else {
            return self.shared.request(sid, SessionRequest::Clear, timeout);
        };
        let by_id = str_of(&public, "device_by_id").unwrap_or_default();
        if str_of(&public, "state").as_deref() == Some("FLASHING") {
            // FLASHING 不得 detach bridge（#69 r5）；Python 鍵序 ok／error_code／selector／session。
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
            // 無 bridge 的 clear：裝置在線時重新 attach（Python `clear_session` 的 `_spawn_attach`）。
            self.shared.send_attached(&by_id);
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
        self.pump();
        let deadline = Instant::now() + ATTACH_OP_WAIT;
        if let Some(sid) = self.resolve(selector) {
            if !lock(&self.shared.inner).stopping {
                if let Some(public) = self.shared.session_public(&sid) {
                    if str_of(&public, "state").as_deref() == Some("FLASHING") {
                        // FLASHING 不得 rebind（Python 於 DEVICE_ALREADY_BOUND 之前判定）。
                        return Ok(flashing_busy(public));
                    }
                }
            }
        }
        let (sid, io) = loop {
            let inner = lock(&self.shared.inner);
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
                op.cancelled.store(true, Ordering::SeqCst);
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
        // 未交給正式 bridge 的預開 handle（舊 by_id）關閉。
        if let Some(pre) = lock(&io.preopened).take() {
            let _ = pre.parts.close();
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
            // Python：裝置在線 → ATTACHING、last_error None，並 spawn attach。
            if let Some(PyValue::Dict(s)) = result.0.get_mut("session") {
                s.insert("state", PyValue::from("ATTACHING"));
                s.insert("last_error", PyValue::Null);
            }
            self.shared.send_attached(&by_id);
        }
        Ok(result)
    }

    /// #26：attach op 取消後 3000 ms 內未收斂（Python 設 RECOVERING＋drain_stalled 並回 SESSION_RECOVERING）。
    fn attach_op_stalled_reply(&self, sid: &str) -> PyDict {
        let mut s = self.shared.session_public(sid).unwrap_or_default();
        let epoch = s.get("bridge_generation").cloned().unwrap_or(PyValue::Null);
        s.insert("state", PyValue::from("RECOVERING"));
        s.insert("drain_stalled", PyValue::from(true));
        s.insert("drain_timeout_epoch", epoch.clone());
        pydict! {
            "ok" => false,
            "error_code" => "SESSION_RECOVERING",
            "drain_stalled" => true,
            "drain_timeout_epoch" => epoch,
            "session" => s
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
    pub fn alias_set(&self, sid: &str, alias: &str) -> Result<PyDict, PyErr> {
        {
            let mut inner = lock(&self.shared.inner);
            let Some(e) = inner.sessions.get_mut(sid) else {
                return Ok(pydict! {
                    "ok" => false,
                    "error_code" => "SESSION_NOT_FOUND",
                    "session_id" => sid.to_string()
                });
            };
            e.profile.alias = alias.to_string();
            lock(&e.io.profile).alias = alias.to_string();
        }
        let row = alias_row(sid, alias, &self.shared.now_iso());
        let name = alias.to_string();
        self.shared
            .commit_sync(move |m| {
                m.apply(StateDelta::AliasSet {
                    alias: name,
                    row: Some(row),
                })
            })
            .map_err(|e| commit_error(&e))?;
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

    /// DeviceWatcher → manager 的事件送端（Delta 轉 `update_devices`、Tick 轉 `on_tick`）。
    pub fn watcher_sender(&self) -> Sender<WatcherEvent> {
        self.watcher_tx.clone()
    }

    /// 只供測試（test-fakes）：之後的偵測 PROBE 以凍結的 Fence 送出（驗證 PROBE TX 經 Fence：零 TX）。
    #[cfg(feature = "test-fakes")]
    pub fn freeze_probe_tx_for_test(&self, frozen: bool) {
        self.shared.probe_tx_frozen.store(frozen, Ordering::SeqCst);
    }

    /// 設定 state.json 持久化觀察者（P5-T19 #3／#4／#68；daemon 組裝點於 `new()` 之後、任何 attach 之前呼叫）。
    pub fn set_persist_observer(&self, observer: Arc<dyn PersistObserver>) {
        *lock(&self.shared.persist_observer) = Some(observer);
    }

    /// SC-11 stopping gate：冪等設定 `stopping=true`；之後任何自動或 RPC 路徑的 attach 都不開啟裝置。
    pub fn begin_shutdown(&self) {
        lock(&self.shared.inner).stopping = true;
    }

    /// 先隱含呼叫 `begin_shutdown`；回傳每個 drain 目標是否在 `budget` 內收斂。
    /// drain 集合＝有 bridge（或 attach／drain 進行中）的 session ∪ inflight attach worker（`attach:<by_id>`）。
    pub fn shutdown(&self, budget: Duration) -> Vec<(String, bool)> {
        self.begin_shutdown();
        let deadline = Instant::now() + budget;
        let (entries, inflight): (Vec<(String, Arc<ActorHandle>)>, Vec<String>) = {
            let inner = lock(&self.shared.inner);
            for op in inner.inflight.values() {
                op.cancelled.store(true, Ordering::SeqCst);
            }
            (
                inner
                    .sessions
                    .values()
                    .map(|e| (e.sid.clone(), e.actor.clone()))
                    .collect(),
                inner.inflight.keys().cloned().collect(),
            )
        };
        let mut waits: Vec<(String, Option<Receiver<Result<PyDict, PyErr>>>)> = Vec::new();
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
            waits.push((sid, sent.then_some(rx)));
        }
        let mut out = Vec::new();
        for (sid, rx) in waits {
            let drained = rx.is_some_and(|rx| {
                rx.recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    .is_ok_and(|r| r.is_ok())
            });
            out.push((sid, drained));
        }
        for by_id in inflight {
            let mut inner = lock(&self.shared.inner);
            while inner.inflight.contains_key(&by_id) {
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
            let drained = !inner.inflight.contains_key(&by_id);
            out.push((format!("attach:{by_id}"), drained));
        }
        out
    }

    /// 只供測試（test-fakes）：session 的 fence `wal_unhealthy`（`None`＝查無 session）。
    #[cfg(feature = "test-fakes")]
    pub fn session_fence_wal_unhealthy(&self, selector: &str) -> Option<bool> {
        let inner = lock(&self.shared.inner);
        let e = inner.resolve(selector)?;
        Some(e.fence.with(|f| f.wal_unhealthy))
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
