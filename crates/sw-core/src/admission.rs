//! P8 Task 14：所有新操作的共同 admission 邊界（arch §6.2 第 7 步；Python `sw_core/admission.py` `AdmissionGate`）。
//!
//! 單一 `Mutex`（葉鎖）：持有時不得送 actor 訊息、不得做 I/O、不得再取其他鎖。`admit` 與 `freeze_if_idle` 同鎖，
//! 故任一操作要嘛在凍結前取得 ticket（交接回忙碌），要嘛在凍結後被拒（`FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`）。
//!
//! - ticket：一項尚未完成的有副作用操作（RPC 方法、命令紀錄、flash sync-probe）；持有到該操作終態才 `done`。
//! - 旗標：`recovering`／`flashing`／`lease`，由發布者在狀態轉移時同步寫入（Python `set_flag`）。
//! - `freeze()`：daemon 一般關機的凍結，不做 busy 判定；之後交接失敗的撤銷（`thaw`）不得解除它。
//! - `freeze_if_idle(reason)`：無 ticket 且無旗標時凍結並發 32 hex token；否則回忙碌、不凍結。
//! - `thaw(token)`：只撤銷由 `freeze_if_idle` **新建立**且 token 相符的凍結（TX fence 逾時、交接未成立）。

use std::collections::hash_map::RandomState;
use std::collections::{BTreeMap, BTreeSet};
use std::hash::{BuildHasher, Hasher};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// 一項尚未完成的有副作用操作（Python `Ticket(kind, com, seq)`）。只能經 [`AdmissionGate::done`] 歸還。
#[derive(Debug, PartialEq, Eq)]
pub struct Ticket {
    kind: String,
    com: Option<String>,
    seq: u64,
}

impl Ticket {
    /// 操作種類（RPC method 名稱、`command.submit`、`flash.sync_probe`）。
    pub fn kind(&self) -> &str {
        &self.kind
    }

    /// 操作所屬 COM；無法解析時為 `None`（忙碌列以 `"*"` 呈現）。
    pub fn com(&self) -> Option<&str> {
        self.com.as_deref()
    }

    /// 取得順序（同一 gate 內單調遞增）。
    pub fn seq(&self) -> u64 {
        self.seq
    }
}

/// 跨 actor request timeout 仍代表執行中操作的 ticket 所有權。
///
/// RPC 等待者離開後，此物件由 actor 的 pending operation 繼續持有；操作到達終態或被終止時，最後一個 owner drop 才歸還票據。
pub struct AdmissionTicket {
    gate: Arc<AdmissionGate>,
    ticket: Option<Ticket>,
}

impl std::fmt::Debug for AdmissionTicket {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AdmissionTicket")
            .field("ticket", &self.ticket)
            .finish_non_exhaustive()
    }
}

impl AdmissionTicket {
    pub fn new(gate: Arc<AdmissionGate>, ticket: Ticket) -> AdmissionTicket {
        AdmissionTicket { gate, ticket: Some(ticket) }
    }
}

impl Drop for AdmissionTicket {
    fn drop(&mut self) {
        if let Some(ticket) = self.ticket.take() {
            self.gate.done(ticket);
        }
    }
}

/// session 狀態旗標（Python `set_flag` 的 `"flashing"`／`"recovering"`／`"lease"`）。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum AdmissionFlag {
    Flashing,
    Lease,
    Recovering,
}

impl AdmissionFlag {
    pub fn as_str(&self) -> &'static str {
        match self {
            AdmissionFlag::Flashing => "flashing",
            AdmissionFlag::Lease => "lease",
            AdmissionFlag::Recovering => "recovering",
        }
    }
}

/// 單一 COM 的忙碌內容（`ops` 為排序去重的 ticket kind；`flags` 依 [`AdmissionFlag`] 排序）。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct BusyCom {
    pub ops: Vec<String>,
    pub flags: Vec<AdmissionFlag>,
}

/// `freeze_if_idle` 的忙碌結果（Python `{"by_com": {...}, "flashing": bool}`）。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Busy {
    /// COM → 忙碌內容（依 COM 排序；`com=None` 的 ticket 列於 `"*"`）。
    pub by_com: BTreeMap<String, BusyCom>,
    /// 任一 COM 有 `flashing` 旗標，或任一 ticket kind 以 `flash.` 開頭（交接回 `FLASHING_BUSY`）。
    pub flashing: bool,
}

/// `freeze_if_idle` 的結果。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FreezeOutcome {
    /// 已凍結；`created` 為 true 表示本次呼叫新建立凍結（之後可由 `thaw(token)` 撤銷）。
    Frozen { token: String, created: bool },
    /// 有 ticket 或旗標：不凍結。
    Busy(Busy),
}

/// 交接 admission 凍結的生命週期；`Pending` 可由 TX fence 失敗回滾，`Permanent` 不可撤銷。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HandoffFreezeState {
    Open,
    Pending,
    Permanent,
}

#[derive(Default)]
struct GateInner {
    tickets: BTreeMap<u64, (String, Option<String>)>,
    flags: BTreeMap<String, BTreeSet<AdmissionFlag>>,
    next_seq: u64,
    frozen: bool,
    token: Option<String>,
    /// 只記錄由 `freeze_if_idle` 新建立之凍結的 token；`freeze()` 建立或接手者不計入。
    idle_token: Option<String>,
    /// 凍結累計時間（已完成的凍結區間累計）。
    frozen_accum: Duration,
    /// 凍結歷史區間（以 (start, end) 記錄已結算的凍結區間，保證可按任意時刻精確截斷）。
    frozen_intervals: Vec<(Instant, Instant)>,
    /// 目前 Pending 凍結開始的時間點；非 Pending 時為 None。
    pending_since: Option<Instant>,
}

/// 凍結閘門的時間與狀態一致快照。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GateSnapshot {
    /// 取樣時刻。
    pub now: Instant,
    /// 取樣時的交接凍結狀態。
    pub freeze_state: HandoffFreezeState,
    /// 截至取樣時刻的累計凍結時間（含已結算與進行中區間，且精確截斷至該時刻）。
    pub frozen_total: Duration,
}

#[cfg(feature = "test-fakes")]
type TimeSource = Arc<dyn Fn() -> Instant + Send + Sync>;

/// 共同 admission 邊界（見模組說明）。
pub struct AdmissionGate {
    inner: Mutex<GateInner>,
    handoff_settled: Condvar,
    #[cfg(feature = "test-fakes")]
    time_source: Option<TimeSource>,
}

impl Default for AdmissionGate {
    fn default() -> Self {
        AdmissionGate::new()
    }
}

impl AdmissionGate {
    pub fn new() -> AdmissionGate {
        AdmissionGate {
            inner: Mutex::new(GateInner::default()),
            handoff_settled: Condvar::new(),
            #[cfg(feature = "test-fakes")]
            time_source: None,
        }
    }

    /// 建立注入自訂時鐘來源的 AdmissionGate（僅供 test-fakes 使用）。
    #[cfg(feature = "test-fakes")]
    pub fn with_clock<F>(time_source: F) -> AdmissionGate
    where
        F: Fn() -> Instant + Send + Sync + 'static,
    {
        AdmissionGate {
            inner: Mutex::new(GateInner::default()),
            handoff_settled: Condvar::new(),
            time_source: Some(Arc::new(time_source)),
        }
    }

    /// 建立注入共用時鐘來源的 AdmissionGate（僅供 test-fakes 使用）。
    #[cfg(feature = "test-fakes")]
    pub fn with_time_source(time_source: Arc<dyn Fn() -> Instant + Send + Sync>) -> AdmissionGate {
        AdmissionGate {
            inner: Mutex::new(GateInner::default()),
            handoff_settled: Condvar::new(),
            time_source: Some(time_source),
        }
    }

    fn now(&self) -> Instant {
        #[cfg(feature = "test-fakes")]
        if let Some(ref ts) = self.time_source {
            return ts();
        }
        Instant::now()
    }

    /// 是否已凍結（shutdown 或交接）。
    pub fn frozen(&self) -> bool {
        self.lock().frozen
    }

    /// 取得 ticket；已凍結回 `None`（呼叫端回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`，零副作用）。
    pub fn admit(&self, kind: &str, com: Option<&str>) -> Option<Ticket> {
        let mut g = self.lock();
        if g.frozen {
            return None;
        }
        g.next_seq += 1;
        let seq = g.next_seq;
        g.tickets.insert(seq, (kind.to_string(), com.map(str::to_string)));
        Some(Ticket {
            kind: kind.to_string(),
            com: com.map(str::to_string),
            seq,
        })
    }

    /// 歸還 ticket（操作已到終態）。
    pub fn done(&self, ticket: Ticket) {
        self.lock().tickets.remove(&ticket.seq);
    }

    /// 發布 COM 的狀態旗標（Python `set_flag(com, flag, on)`）。
    pub fn set_flag(&self, com: &str, flag: AdmissionFlag, on: bool) {
        let mut g = self.lock();
        let flags = g.flags.entry(com.to_string()).or_default();
        if on {
            flags.insert(flag);
        } else {
            flags.remove(&flag);
        }
        if flags.is_empty() {
            g.flags.remove(com);
        }
    }

    /// daemon 一般關機的凍結：停止接受新操作，不做 busy 判定；接手後交接失敗的 `thaw` 不得解除。
    pub fn freeze(&self) {
        let mut g = self.lock();
        let was_pending = g.frozen && g.idle_token.is_some();
        g.frozen = true;
        // shutdown 凍結接手後，handoff 的失敗路徑不得再以先前 token 撤銷它。
        g.idle_token = None;
        if was_pending {
            // Pending -> Permanent：結算後清除
            if let Some(since) = g.pending_since.take() {
                let now = self.now();
                let dur = now.saturating_duration_since(since);
                g.frozen_accum += dur;
                g.frozen_intervals.push((since, now));
            }
        }
        if g.token.is_none() {
            g.token = Some(new_token());
        }
        self.handoff_settled.notify_all();
    }

    /// 撤銷由 `freeze_if_idle` 新建立、token 相符的凍結；其餘情形不動作並回 false。
    pub fn thaw(&self, token: &str) -> bool {
        let mut g = self.lock();
        if !g.frozen || g.token.as_deref() != Some(token) || g.idle_token.as_deref() != Some(token) {
            return false;
        }
        g.frozen = false;
        g.token = None;
        g.idle_token = None;
        // Pending -> Open：把 now - pending_since 加進 frozen_accum，清除 pending_since
        if let Some(since) = g.pending_since.take() {
            let now = self.now();
            let dur = now.saturating_duration_since(since);
            g.frozen_accum += dur;
            g.frozen_intervals.push((since, now));
        }
        self.handoff_settled.notify_all();
        true
    }

    /// 凍結是否仍是尚未成立的交接嘗試；查詢與 token 狀態共用同一把 gate lock。
    pub fn handoff_freeze_state(&self) -> HandoffFreezeState {
        let g = self.lock();
        handoff_state(&g)
    }

    /// 等待可撤銷的交接嘗試結束；供有界的 actor 暫存佇列滿載時回壓。
    pub fn wait_handoff_settled(&self) -> HandoffFreezeState {
        let g = self.lock();
        let g = self
            .handoff_settled
            .wait_while(g, |state| state.frozen && state.idle_token.is_some())
            .unwrap_or_else(|error| error.into_inner());
        handoff_state(&g)
    }

    /// 將成功完成 TX fence 的交接凍結轉為永久；之後 `thaw` 不得撤銷。
    pub fn commit_handoff(&self, token: &str) -> bool {
        let mut g = self.lock();
        if !g.frozen || g.token.as_deref() != Some(token) || g.idle_token.as_deref() != Some(token) {
            return false;
        }
        g.idle_token = None;
        // Pending -> Permanent：同樣結算後清除
        if let Some(since) = g.pending_since.take() {
            let now = self.now();
            let dur = now.saturating_duration_since(since);
            g.frozen_accum += dur;
            g.frozen_intervals.push((since, now));
        }
        self.handoff_settled.notify_all();
        true
    }

    /// 閒置（無 ticket、無旗標）時凍結並發 token；已凍結回既有 token（`created=false`）；否則回忙碌、不凍結。
    pub fn freeze_if_idle(&self, _reason: &str) -> FreezeOutcome {
        let mut g = self.lock();
        if g.frozen {
            // 已凍結：本次呼叫不是建立者，之後的 thaw 不得撤銷該凍結。
            let was_pending = g.idle_token.is_some();
            g.idle_token = None;
            if was_pending {
                // Pending -> Permanent
                if let Some(since) = g.pending_since.take() {
                    let now = self.now();
                    let dur = now.saturating_duration_since(since);
                    g.frozen_accum += dur;
                    g.frozen_intervals.push((since, now));
                }
            }
            self.handoff_settled.notify_all();
            let token = g.token.get_or_insert_with(new_token).clone();
            return FreezeOutcome::Frozen { token, created: false };
        }
        let mut by_com: BTreeMap<String, (BTreeSet<String>, BTreeSet<AdmissionFlag>)> = BTreeMap::new();
        let mut flashing = false;
        for (kind, com) in g.tickets.values() {
            let key = com.clone().unwrap_or_else(|| "*".to_string());
            by_com.entry(key).or_default().0.insert(kind.clone());
            flashing |= kind.starts_with("flash.");
        }
        for (com, flags) in &g.flags {
            let row = by_com.entry(com.clone()).or_default();
            row.1.extend(flags.iter().copied());
            flashing |= flags.contains(&AdmissionFlag::Flashing);
        }
        if !by_com.is_empty() {
            return FreezeOutcome::Busy(Busy {
                by_com: by_com
                    .into_iter()
                    .map(|(com, (ops, flags))| {
                        (
                            com,
                            BusyCom {
                                ops: ops.into_iter().collect(),
                                flags: flags.into_iter().collect(),
                            },
                        )
                    })
                    .collect(),
                flashing,
            });
        }
        let token = new_token();
        g.frozen = true;
        g.token = Some(token.clone());
        g.idle_token = Some(token.clone());
        // Open -> Pending：記下 pending_since
        let now = self.now();
        g.pending_since = Some(now);
        FreezeOutcome::Frozen { token, created: true }
    }

    fn frozen_total_locked(&self, g: &GateInner, now: Instant) -> Duration {
        let mut total = Duration::ZERO;
        for &(start, end) in &g.frozen_intervals {
            if now <= start {
                continue;
            }
            let effective_end = if now < end { now } else { end };
            total = total.saturating_add(effective_end.saturating_duration_since(start));
        }
        if let Some(since) = g.pending_since {
            if now > since {
                total = total.saturating_add(now.saturating_duration_since(since));
            }
        }
        total
    }

    /// 取得截至指定時刻的總凍結時間（含已結算之歷史區間與進行中 Pending 區間，精確截斷至指定時刻）。
    /// 結果保證單調不遞減。
    pub fn frozen_total(&self, now: Instant) -> Duration {
        let g = self.lock();
        self.frozen_total_locked(&g, now)
    }

    /// 於鎖內取得當前時刻、交接凍結狀態與截至該時刻累計凍結時間的一致快照。
    pub fn snapshot(&self) -> GateSnapshot {
        let g = self.lock();
        let now = self.now();
        let freeze_state = handoff_state(&g);
        let frozen_total = self.frozen_total_locked(&g, now);
        GateSnapshot {
            now,
            freeze_state,
            frozen_total,
        }
    }

    /// 於鎖內取得指定時刻、當前交接凍結狀態與截至該指定時刻累計凍結時間的一致快照。
    pub fn snapshot_at(&self, now: Instant) -> GateSnapshot {
        let g = self.lock();
        let freeze_state = handoff_state(&g);
        let frozen_total = self.frozen_total_locked(&g, now);
        GateSnapshot {
            now,
            freeze_state,
            frozen_total,
        }
    }

    /// 目前未歸還的 ticket 數（觀測用）。
    pub fn live_tickets(&self) -> usize {
        self.lock().tickets.len()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, GateInner> {
        self.inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn handoff_state(g: &GateInner) -> HandoffFreezeState {
    if !g.frozen {
        HandoffFreezeState::Open
    } else if g.idle_token.is_some() {
        HandoffFreezeState::Pending
    } else {
        HandoffFreezeState::Permanent
    }
}

/// `uuid.uuid4().hex`（32 hex）。熵源與 `session_manager` 的 nonce 相同（`RandomState` 雜湊＋單調計數），不需平台 RNG。
fn new_token() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let mut fill = |buf: &mut [u8]| {
        for chunk in buf.chunks_mut(8) {
            let mut h = RandomState::new().build_hasher();
            h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
            let bytes = h.finish().to_le_bytes();
            chunk.copy_from_slice(&bytes[..chunk.len()]);
        }
    };
    sw_proto::pycompat::uuid4_hex(&mut fill)
}
