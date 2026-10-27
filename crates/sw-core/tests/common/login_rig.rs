//! P7 Task 3（08-slice23-login-events.md）：`LoginRig` —— 以 `RxShared`＋虛擬時鐘＋腳本化 tx 閉包組出 `LoginIo`。
//!
//! DSL 與 `scripts/rust/p7_fake_bridge.py`（`ScriptedBridge`／`VirtualClock`）逐條相同：
//! - 虛擬時鐘自 `BASE` 起（`virtual_elapsed_s` = 現在 − `BASE`）；`initial_rx` 於 t=0 出現；`idle_s` 為開始前已靜默秒數。
//! - `sleep(d)`＝時鐘推進 `d` 後遞送期間到期的排程 RX（RX 時刻記為推進後的現在，同 `VirtualClock.advance_to`）。
//! - tx：記錄 `(source, payload, log)`，依第一個符合的 reaction 附加 RX（`{nonce}`＝最近一次 nonce；`after_s` 排程）。
//! - `rx_bytes_10s`：`RxShared::rx_stats` 固定回此值（`ScriptedBridge.rx_stats`）。
//!
//! 偏離計畫原文：計畫把本檔放在 `tests/support/login_rig.rs`；`support/mod.rs` 屬 P5（06）測試支援、仍在修訂，
//! 故改放 P7 的 `tests/common/`（同為整合測試共用輔助）。`FenceReject::StaleFsmGen` 依 Task 0 核對註記改用 `FsmMismatch`。
#![allow(dead_code)]

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use sw_core::login_fsm::LoginIo;
use sw_core::tx_writer::{FenceReject, OpId, TxClass, TxReport};
use sw_core::uart_bridge::{RunCancel, RxShared};
use sw_pal::traits::{Clock, TxOutcome, TxResult};
use sw_proto::pyjson::PyValue;

/// 虛擬時鐘原點（ns）；保留空間給 `idle_s`（開始前的靜默）。
pub const BASE: u64 = 1_000_000_000_000;

#[derive(Clone, Debug)]
enum On {
    Eq,
    Prefix,
    Secret,
    Any,
}

#[derive(Clone, Debug)]
struct Reaction {
    on: On,
    matcher: String,
    rx: String,
    after_s: Option<f64>,
}

/// 時間一律以 `f64` 秒（自 `BASE` 起）累加，與 Python `VirtualClock`（`now += d`）的浮點誤差逐位相同；
/// `mono_ns` 取 `BASE + floor(now * 1e9)`。
#[derive(Default)]
struct ClockState {
    now: f64,
    pending: Vec<(f64, u64, String)>,
    seq: u64,
    cancel_at: Option<f64>,
}

fn to_ns(t: f64) -> u64 {
    BASE + (t * 1e9) as u64
}

/// 腳本化虛擬時鐘（`VirtualClock`）：`sleep` 推進並遞送到期 RX。
pub struct ScriptClock {
    st: Mutex<ClockState>,
    rx: Arc<RxShared>,
    rx_seq: AtomicU64,
    cancel: Mutex<Option<RunCancel>>,
    pub sleeps: Mutex<Vec<Duration>>,
}

impl ScriptClock {
    fn new(rx: Arc<RxShared>) -> Arc<ScriptClock> {
        Arc::new(ScriptClock {
            st: Mutex::new(ClockState::default()),
            rx,
            rx_seq: AtomicU64::new(0),
            cancel: Mutex::new(None),
            sleeps: Mutex::new(Vec::new()),
        })
    }

    fn now(&self) -> u64 {
        to_ns(self.st.lock().unwrap().now)
    }

    fn now_s(&self) -> f64 {
        self.st.lock().unwrap().now
    }

    fn append_now(&self, text: &str) {
        let now = self.now();
        let seq = self.rx_seq.fetch_add(1, Ordering::SeqCst) + 1;
        self.rx.append(text.as_bytes(), now, 1, seq);
    }

    fn schedule(&self, after_s: f64, text: String) {
        let mut st = self.st.lock().unwrap();
        st.seq += 1;
        let at = st.now + after_s;
        let seq = st.seq;
        st.pending.push((at, seq, text));
        st.pending
            .sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
    }

    fn advance_to(&self, target: f64) {
        let (due, cancel_now) = {
            let mut st = self.st.lock().unwrap();
            if target > st.now {
                st.now = target;
            }
            let now = st.now;
            let mut due = Vec::new();
            while st.pending.first().is_some_and(|(at, _, _)| *at <= now) {
                due.push(st.pending.remove(0).2);
            }
            let cancel_now = st.cancel_at.is_some_and(|at| at <= now);
            if cancel_now {
                st.cancel_at = None;
            }
            (due, cancel_now)
        };
        for text in due {
            self.append_now(&text);
        }
        if cancel_now {
            if let Some(c) = self.cancel.lock().unwrap().as_ref() {
                c.trigger();
            }
        }
    }
}

impl Clock for ScriptClock {
    fn mono_ns(&self) -> u64 {
        self.now()
    }
    fn sleep(&self, d: Duration) {
        self.sleeps.lock().unwrap().push(d);
        let target = self.now_s() + d.as_secs_f64();
        self.advance_to(target);
    }
    /// `ScriptedBridge.wait_for_regex` 的推進：直接跳到「下一筆排程 RX／取消／期限」中最早者。
    fn wait_until_or_cancel(&self, deadline_ns: u64, cancel: &crossbeam_channel::Receiver<()>) -> bool {
        loop {
            if cancel.try_recv().is_ok() {
                return false;
            }
            if self.now() >= deadline_ns {
                return true;
            }
            let mut deadline_s = (deadline_ns - BASE) as f64 / 1e9;
            while to_ns(deadline_s) < deadline_ns {
                deadline_s = f64::from_bits(deadline_s.to_bits() + 1);
            }
            let target = {
                let st = self.st.lock().unwrap();
                let mut t = deadline_s;
                if let Some((at, _, _)) = st.pending.first() {
                    t = t.min(*at);
                }
                if let Some(at) = st.cancel_at {
                    t = t.min(at);
                }
                t
            };
            self.advance_to(target);
        }
    }
    fn wall_unix_us(&self) -> i64 {
        0
    }
    fn local_utc_offset_s(&self) -> i32 {
        0
    }
}

struct Inner {
    clock: Arc<ScriptClock>,
    reactions: Vec<Reaction>,
    nonces: Vec<String>,
    nonce_i: AtomicU64,
    last_nonce: Mutex<String>,
    sent: Mutex<Vec<(String, Vec<u8>, bool)>>,
    attempts: AtomicU64,
    reject_after: Option<(usize, FenceReject)>,
    partial_on: Option<usize>,
}

impl Inner {
    fn react(&self, secret: bool, cmd: &str) {
        for r in &self.reactions {
            let hit = if secret {
                matches!(r.on, On::Secret)
            } else {
                match r.on {
                    On::Eq => cmd == r.matcher,
                    On::Prefix => cmd.starts_with(&r.matcher),
                    On::Any => true,
                    On::Secret => false,
                }
            };
            if !hit {
                continue;
            }
            let rx = r.rx.replace("{nonce}", &self.last_nonce.lock().unwrap());
            match r.after_s.filter(|s| *s != 0.0) {
                Some(after) => self.clock.schedule(after, rx),
                None => self.clock.append_now(&rx),
            }
            return;
        }
    }

    fn tx(&self, payload: &[u8], _class: TxClass, source: &str, log: bool) -> TxReport {
        let n = self.attempts.fetch_add(1, Ordering::SeqCst) as usize + 1;
        let report = |outcome: TxOutcome, reject: Option<FenceReject>| TxReport {
            op_id: OpId(n as u64),
            epoch: 1,
            result: TxResult {
                outcome,
                error: None,
            },
            wal: None,
            reject,
        };
        if let Some((k, reject)) = self.reject_after {
            if n > k {
                return report(TxOutcome::NotSubmitted, Some(reject));
            }
        }
        self.sent
            .lock()
            .unwrap()
            .push((source.to_string(), payload.to_vec(), log));
        if self.partial_on == Some(n) {
            return report(TxOutcome::Completed(1.min(payload.len())), None);
        }
        let text = String::from_utf8_lossy(payload);
        let cmd = text.strip_suffix('\n').unwrap_or(&text).to_string();
        self.react(source == "system:secret", &cmd);
        report(TxOutcome::Completed(payload.len()), None)
    }

    fn nonce(&self) -> String {
        let i = self.nonce_i.fetch_add(1, Ordering::SeqCst) as usize;
        let raw = self
            .nonces
            .get(i)
            .cloned()
            .unwrap_or_else(|| format!("{i:08x}"));
        let padded = format!("{raw}{}", "0".repeat(32));
        let nonce: String = padded.chars().take(8).collect();
        *self.last_nonce.lock().unwrap() = nonce.clone();
        nonce
    }
}

type TxFn = dyn Fn(&[u8], TxClass, &str, bool) -> TxReport + Send + Sync;
type NonceFn = dyn Fn() -> String + Send + Sync;

/// 建構器（`LoginRig::new()…build()` 或 `LoginRig::from_dsl`）。
#[derive(Default)]
pub struct LoginRigBuilder {
    initial_rx: String,
    reactions: Vec<Reaction>,
    nonces: Vec<String>,
    rx_bytes_10s: Option<u64>,
    idle_s: f64,
    reject_after: Option<(usize, FenceReject)>,
    partial_on: Option<usize>,
    cancel_at_s: Option<f64>,
    class: Option<TxClass>,
}

impl LoginRigBuilder {
    pub fn initial_rx(mut self, s: &str) -> Self {
        self.initial_rx = s.to_string();
        self
    }
    fn push(mut self, on: On, m: &str, rx: &str, after_s: Option<f64>) -> Self {
        self.reactions.push(Reaction {
            on,
            matcher: m.to_string(),
            rx: rx.to_string(),
            after_s,
        });
        self
    }
    pub fn on_eq(self, m: &str, rx: &str) -> Self {
        self.push(On::Eq, m, rx, None)
    }
    pub fn on_eq_after(self, m: &str, rx: &str, after_s: f64) -> Self {
        self.push(On::Eq, m, rx, Some(after_s))
    }
    pub fn on_prefix(self, m: &str, rx: &str) -> Self {
        self.push(On::Prefix, m, rx, None)
    }
    pub fn on_secret(self, rx: &str) -> Self {
        self.push(On::Secret, "", rx, None)
    }
    pub fn on_any(self, rx: &str) -> Self {
        self.push(On::Any, "", rx, None)
    }
    pub fn nonces(mut self, n: &[&str]) -> Self {
        self.nonces = n.iter().map(|s| s.to_string()).collect();
        self
    }
    pub fn rx_bytes_10s(mut self, n: u64) -> Self {
        self.rx_bytes_10s = Some(n);
        self
    }
    /// 開始前已靜默的秒數（DSL `idle_s`；有 `initial_rx` 時最後 RX 時刻為 0）。
    pub fn idle_s(mut self, s: f64) -> Self {
        self.idle_s = s;
        self
    }
    /// 第 `n` 次之後的 TX 一律以 `reject` 拒絕（NotSubmitted，不記錄）。
    pub fn reject_after(mut self, n: usize, reject: FenceReject) -> Self {
        self.reject_after = Some((n, reject));
        self
    }
    /// 第 `n` 次 TX 回 `Completed(1)`（部分寫出）。
    pub fn partial_on(mut self, n: usize) -> Self {
        self.partial_on = Some(n);
        self
    }
    /// 虛擬時間 `t` 秒時觸發 `RunCancel`。
    pub fn cancel_at(mut self, t: f64) -> Self {
        self.cancel_at_s = Some(t);
        self
    }
    pub fn class(mut self, c: TxClass) -> Self {
        self.class = Some(c);
        self
    }

    pub fn build(self) -> LoginRig {
        let idle_ns = (self.idle_s.max(0.0) * 1e9).round() as u64;
        let rx = RxShared::new(131_072, BASE - idle_ns);
        if let Some(n) = self.rx_bytes_10s {
            rx.set_rx_stats_override(Some(n));
        }
        let clock = ScriptClock::new(Arc::clone(&rx));
        if !self.initial_rx.is_empty() {
            clock.append_now(&self.initial_rx);
        }
        let cancel = RunCancel::new(&rx);
        *clock.cancel.lock().unwrap() = Some(cancel.clone());
        if let Some(t) = self.cancel_at_s {
            clock.st.lock().unwrap().cancel_at = Some(t);
        }
        let inner = Arc::new(Inner {
            clock: Arc::clone(&clock),
            reactions: self.reactions,
            nonces: self.nonces,
            nonce_i: AtomicU64::new(0),
            last_nonce: Mutex::new(String::new()),
            sent: Mutex::new(Vec::new()),
            attempts: AtomicU64::new(0),
            reject_after: self.reject_after,
            partial_on: self.partial_on,
        });
        let tx_inner = Arc::clone(&inner);
        let nonce_inner = Arc::clone(&inner);
        LoginRig {
            rx,
            clock,
            cancel,
            class: self.class.unwrap_or(TxClass::LoginProbe),
            tx: Box::new(move |p, c, s, l| tx_inner.tx(p, c, s, l)),
            nonce: Box::new(move || nonce_inner.nonce()),
            inner,
        }
    }
}

pub struct LoginRig {
    pub rx: Arc<RxShared>,
    pub clock: Arc<ScriptClock>,
    pub cancel: RunCancel,
    class: TxClass,
    tx: Box<TxFn>,
    nonce: Box<NonceFn>,
    inner: Arc<Inner>,
}

fn dsl_str(v: &PyValue, k: &str) -> String {
    v.get(k).and_then(PyValue::as_str).unwrap_or("").to_string()
}

fn dsl_f64(v: Option<&PyValue>) -> f64 {
    match v {
        Some(PyValue::Float(f)) => *f,
        Some(other @ PyValue::Int(_)) => other.as_i64().unwrap_or(0) as f64,
        _ => 0.0,
    }
}

impl LoginRig {
    #[allow(clippy::new_ret_no_self)]
    pub fn new() -> LoginRigBuilder {
        // `p7_login_cases._c` 的預設 nonces。
        LoginRigBuilder::default().nonces(&["deadbeef"])
    }

    /// golden `case` 物件（`p7_fake_bridge` DSL）。
    pub fn from_dsl(case: &PyValue) -> LoginRig {
        let mut b = LoginRigBuilder {
            initial_rx: dsl_str(case, "initial_rx"),
            idle_s: dsl_f64(case.get("idle_s")),
            ..LoginRigBuilder::default()
        };
        if let Some(PyValue::List(rs)) = case.get("reactions") {
            for r in rs {
                let on = match dsl_str(r, "on").as_str() {
                    "eq" => On::Eq,
                    "prefix" => On::Prefix,
                    "secret" => On::Secret,
                    "any" => On::Any,
                    other => panic!("未知 reaction.on {other}"),
                };
                let after = dsl_f64(r.get("after_s"));
                b.reactions.push(Reaction {
                    on,
                    matcher: dsl_str(r, "match"),
                    rx: dsl_str(r, "rx"),
                    after_s: (after != 0.0).then_some(after),
                });
            }
        }
        if let Some(PyValue::List(ns)) = case.get("nonces") {
            b.nonces = ns
                .iter()
                .map(|n| n.as_str().expect("nonce").to_string())
                .collect();
        }
        b.rx_bytes_10s = Some(case.get("rx_bytes_10s").and_then(PyValue::as_i64).unwrap_or(0) as u64);
        b.build()
    }

    pub fn io(&self) -> LoginIo<'_> {
        LoginIo {
            rx: &self.rx,
            tx: &*self.tx,
            clock: &*self.clock,
            nonce: &*self.nonce,
            cancel: &self.cancel,
            class: self.class,
        }
    }

    pub fn sent_triples(&self) -> Vec<(String, Vec<u8>, bool)> {
        self.inner.sent.lock().unwrap().clone()
    }
    pub fn sent_sources(&self) -> Vec<String> {
        self.sent_triples().into_iter().map(|t| t.0).collect()
    }
    pub fn sent_payloads(&self) -> Vec<Vec<u8>> {
        self.sent_triples().into_iter().map(|t| t.1).collect()
    }
    pub fn sent_payload(&self, i: usize) -> Vec<u8> {
        self.sent_triples()[i].1.clone()
    }
    pub fn sent_logged(&self, i: usize) -> bool {
        self.sent_triples()[i].2
    }
    /// tx 閉包被呼叫的次數（含被拒者）。
    pub fn tx_attempts(&self) -> usize {
        self.inner.attempts.load(Ordering::SeqCst) as usize
    }
    pub fn virtual_elapsed_s(&self) -> f64 {
        self.clock.now_s()
    }
    pub fn sleeps_s(&self) -> Vec<f64> {
        self.clock
            .sleeps
            .lock()
            .unwrap()
            .iter()
            .map(Duration::as_secs_f64)
            .collect()
    }
}
