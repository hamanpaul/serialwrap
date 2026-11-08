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
use std::sync::Mutex;

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

#[derive(Default)]
struct GateInner {
    tickets: BTreeMap<u64, (String, Option<String>)>,
    flags: BTreeMap<String, BTreeSet<AdmissionFlag>>,
    next_seq: u64,
    frozen: bool,
    token: Option<String>,
    /// 只記錄由 `freeze_if_idle` 新建立之凍結的 token；`freeze()` 建立或接手者不計入。
    idle_token: Option<String>,
}

/// 共同 admission 邊界（見模組說明）。
pub struct AdmissionGate {
    inner: Mutex<GateInner>,
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
        }
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
        g.frozen = true;
        // shutdown 凍結接手後，handoff 的失敗路徑不得再以先前 token 撤銷它。
        g.idle_token = None;
        if g.token.is_none() {
            g.token = Some(new_token());
        }
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
        true
    }

    /// 閒置（無 ticket、無旗標）時凍結並發 token；已凍結回既有 token（`created=false`）；否則回忙碌、不凍結。
    pub fn freeze_if_idle(&self, _reason: &str) -> FreezeOutcome {
        let mut g = self.lock();
        if g.frozen {
            // 已凍結：本次呼叫不是建立者，之後的 thaw 不得撤銷該凍結。
            g.idle_token = None;
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
        FreezeOutcome::Frozen { token, created: true }
    }

    /// 目前未歸還的 ticket 數（觀測用）。
    pub fn live_tickets(&self) -> usize {
        self.lock().tickets.len()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, GateInner> {
        self.inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
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
