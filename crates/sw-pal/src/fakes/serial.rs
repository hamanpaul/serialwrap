//! 序列埠記憶體替身（arch §4.2；Slice 2.1 Task 1）。

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crate::traits::{
    CancelToken, CloseReceipt, CloseToken, DevicePath, HolderReport, HolderScanStatus, OpenError,
    PalError, PalResult, ReadError, ReadOutcome, SerialBackend, SerialConfig, SerialHandle,
    SerialReader,
    SerialWriter, SubmitGate, SubmitRevoked, SubmitStep, TxOutcome, TxResult, UartExclusive,
};

#[derive(Debug, Clone)]
enum Atom {
    Literal(char),
    Any,
    Class(Vec<(char, char)>, bool),
}

impl Atom {
    fn matches(&self, c: char) -> bool {
        match self {
            Atom::Literal(lit) => *lit == c,
            Atom::Any => c != '\n',
            Atom::Class(ranges, negated) => {
                let in_class = ranges.iter().any(|(s, e)| c >= *s && c <= *e);
                in_class != *negated
            }
        }
    }
}

#[derive(Debug, Clone)]
enum Quantifier {
    Once,
    ZeroOrMore,
    OneOrMore,
    Exact(usize),
}

#[derive(Debug, Clone)]
struct Piece {
    atom: Atom,
    quant: Quantifier,
}

/// 簡易正規表達式解析器（供 `on_tx_match` 的 nonce probe 比對使用）。
///
/// 支援子集：`^`, `$`, `.`, `[a-z]`, `[^a-z]`, `\d`, `\w`, `\s`, `*`, `+`, `{n}`, 以及非捕獲性括號 `(...)`。
/// 不支援（遇到即 panic）：`|`, `?`, `(?...)`, `{m,n}`, `\b`, `\B`, `\S`, `\D`, `\W`, `[[:...:]]`, 以及 lazy 量詞。
#[derive(Debug, Clone)]
struct SimpleRegex {
    start_anchored: bool,
    end_anchored: bool,
    pieces: Vec<Piece>,
}

impl SimpleRegex {
    fn parse(pattern: &str) -> Self {
        let chars: Vec<char> = pattern.chars().collect();
        let mut idx = 0;
        let mut start_anchored = false;
        let mut end_anchored = false;

        // 檢查不支援的 regex 語法
        for &c in &chars {
            if c == '|' || c == '?' {
                panic!("on_tx_match: unsupported regex syntax '{c}' in pattern '{pattern}'");
            }
        }

        if idx < chars.len() && chars[idx] == '^' {
            start_anchored = true;
            idx += 1;
        }

        let mut pieces: Vec<Piece> = Vec::new();

        while idx < chars.len() {
            let c = chars[idx];
            if c == '$' && idx + 1 == chars.len() {
                end_anchored = true;
                break;
            }
            if c == '(' {
                if idx + 1 < chars.len() && chars[idx + 1] == '?' {
                    panic!("on_tx_match: unsupported regex group '(?...' in pattern '{pattern}'");
                }
                idx += 1;
                continue;
            }
            if c == ')' {
                idx += 1;
                continue;
            }
            let atom = if c == '.' {
                idx += 1;
                Atom::Any
            } else if c == '[' {
                idx += 1;
                if idx + 1 < chars.len() && chars[idx] == ':' {
                    panic!("on_tx_match: unsupported POSIX bracket class '[[:' in pattern '{pattern}'");
                }
                let mut negated = false;
                if idx < chars.len() && chars[idx] == '^' {
                    negated = true;
                    idx += 1;
                }
                let mut ranges = Vec::new();
                while idx < chars.len() && chars[idx] != ']' {
                    let start = chars[idx];
                    idx += 1;
                    if idx + 1 < chars.len() && chars[idx] == '-' && chars[idx + 1] != ']' {
                        idx += 1;
                        let end = chars[idx];
                        idx += 1;
                        ranges.push((start, end));
                    } else {
                        ranges.push((start, start));
                    }
                }
                if idx < chars.len() && chars[idx] == ']' {
                    idx += 1;
                }
                Atom::Class(ranges, negated)
            } else if c == '\\' {
                idx += 1;
                if idx < chars.len() {
                    let esc = chars[idx];
                    idx += 1;
                    match esc {
                        'd' => Atom::Class(vec![('0', '9')], false),
                        'w' => Atom::Class(
                            vec![('a', 'z'), ('A', 'Z'), ('0', '9'), ('_', '_')],
                            false,
                        ),
                        's' => Atom::Class(
                            vec![(' ', ' '), ('\t', '\t'), ('\r', '\r'), ('\n', '\n')],
                            false,
                        ),
                        'b' | 'B' | 'S' | 'D' | 'W' => {
                            panic!("on_tx_match: unsupported escape '\\{esc}' in pattern '{pattern}'");
                        }
                        other => Atom::Literal(other),
                    }
                } else {
                    Atom::Literal('\\')
                }
            } else {
                idx += 1;
                Atom::Literal(c)
            };

            let mut quant = Quantifier::Once;
            if idx < chars.len() {
                if chars[idx] == '*' {
                    quant = Quantifier::ZeroOrMore;
                    idx += 1;
                } else if chars[idx] == '+' {
                    quant = Quantifier::OneOrMore;
                    idx += 1;
                } else if chars[idx] == '{' {
                    let mut count_str = String::new();
                    idx += 1;
                    while idx < chars.len() && chars[idx] != '}' {
                        count_str.push(chars[idx]);
                        idx += 1;
                    }
                    if idx < chars.len() && chars[idx] == '}' {
                        idx += 1;
                    }
                    if count_str.contains(',') {
                        panic!("on_tx_match: unsupported range quantifier '{{{count_str}}}' in pattern '{pattern}'");
                    }
                    if let Ok(n) = count_str.parse::<usize>() {
                        quant = Quantifier::Exact(n);
                    } else {
                        panic!("on_tx_match: invalid quantifier '{{{count_str}}}' in pattern '{pattern}'");
                    }
                }
            }
            if idx < chars.len() && chars[idx] == '?' {
                panic!("on_tx_match: unsupported lazy quantifier in pattern '{pattern}'");
            }
            pieces.push(Piece { atom, quant });
        }

        Self {
            start_anchored,
            end_anchored,
            pieces,
        }
    }

    fn is_match(&self, text: &str) -> bool {
        let chars: Vec<char> = text.chars().collect();
        if self.start_anchored {
            self.match_from(&chars, 0)
        } else {
            for start in 0..=chars.len() {
                if self.match_from(&chars, start) {
                    return true;
                }
            }
            false
        }
    }

    fn match_from(&self, chars: &[char], start: usize) -> bool {
        self.match_rec(&self.pieces, chars, 0, start)
    }

    fn match_rec(&self, pieces: &[Piece], chars: &[char], pi: usize, ci: usize) -> bool {
        if pi == pieces.len() {
            if self.end_anchored {
                return ci == chars.len();
            } else {
                return true;
            }
        }

        let piece = &pieces[pi];
        match piece.quant {
            Quantifier::Once => {
                if ci < chars.len() && piece.atom.matches(chars[ci]) {
                    self.match_rec(pieces, chars, pi + 1, ci + 1)
                } else {
                    false
                }
            }
            Quantifier::Exact(n) => {
                if ci + n <= chars.len() && chars[ci..ci + n].iter().all(|c| piece.atom.matches(*c))
                {
                    self.match_rec(pieces, chars, pi + 1, ci + n)
                } else {
                    false
                }
            }
            Quantifier::OneOrMore => {
                if ci >= chars.len() || !piece.atom.matches(chars[ci]) {
                    return false;
                }
                let mut matched_len = 0;
                while ci + matched_len < chars.len() && piece.atom.matches(chars[ci + matched_len]) {
                    matched_len += 1;
                }
                for k in (1..=matched_len).rev() {
                    if self.match_rec(pieces, chars, pi + 1, ci + k) {
                        return true;
                    }
                }
                false
            }
            Quantifier::ZeroOrMore => {
                let mut matched_len = 0;
                while ci + matched_len < chars.len() && piece.atom.matches(chars[ci + matched_len]) {
                    matched_len += 1;
                }
                for k in (0..=matched_len).rev() {
                    if self.match_rec(pieces, chars, pi + 1, ci + k) {
                        return true;
                    }
                }
                false
            }
        }
    }
}

/// 寫入腳本（依序消耗；耗盡後恢復 `Accept(全部)`）。
#[derive(Debug, Clone)]
pub enum FakeWriteStep {
    /// 接受前 n bytes 後回 `Completed(累計)`。
    Accept(usize),
    /// 接受 n bytes 後回 `Completed(n)` 加 error。
    AcceptThenError(usize, PalError),
    /// 回 `TxOutcome::Unknown { confirmed }` 加 error。
    Unknown { confirmed: usize, error: PalError },
    /// 經 gate 提交後（已交 OS、in_flight 已設）於 gate 外阻塞直到 `CancelToken::cancel`，回 `Completed(accept_before_cancel)`。
    BlockUntilCancel { accept_before_cancel: usize },
    /// 經 gate 提交後於 gate 外阻塞直到 `FakeDevice::release_block`（模擬 D-state；不持 Fence 鎖）。
    BlockForever,
    /// 0 byte + error。
    Error(PalError),
    /// R0-F1 線性化測試用：`write_gated` 進入後、第一次 `gate.submit` 之前暫停（不持鎖）。
    PauseBeforeSubmit,
    /// R0-F1：gate 已重驗通過、`os_submit` 閉包內（持 Fence 鎖）暫停。
    PauseInsideSubmit,
    /// R0-F1：第一次 `gate.submit` 接受 n bytes，隨後回 WouldBlock 並於 gate 外暫停；`release_pause` 後經 gate 提交剩餘。
    AcceptThenWouldBlock(usize),
}

/// 裝置內部狀態。
pub(crate) struct FakeDeviceInner {
    rx: Mutex<VecDeque<u8>>,
    tx: Mutex<Vec<u8>>,
    steps: Mutex<VecDeque<FakeWriteStep>>,
    cond: Condvar,
    cond_mutex: Mutex<()>,
    open: AtomicBool,
    opens: AtomicUsize,
    closes: AtomicUsize,
    writes: AtomicUsize,
    os_submits: AtomicUsize,
    blocked_release: AtomicBool,
    close_err: Mutex<Option<PalError>>,
    removed: AtomicBool,
    external_hold: Mutex<Option<Vec<u32>>>,
    access_denied: AtomicBool,
    woken: AtomicBool,
    peer_closed: AtomicBool,
    eof_reads: AtomicUsize,
    read_errors: AtomicUsize,
    on_os_submit_hook: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    paused: Mutex<bool>,
    paused_cond: Condvar,
    pause_released: Mutex<bool>,
    pause_released_cond: Condvar,
    tx_replies: Mutex<Vec<(Vec<u8>, Vec<u8>, bool)>>,
    tx_matches: Mutex<Vec<(SimpleRegex, Box<dyn Fn(&[u8]) -> Vec<u8> + Send + Sync>)>>,
    tx_match_cursor: Mutex<usize>,
}

impl FakeDeviceInner {
    fn new() -> Self {
        Self {
            rx: Mutex::new(VecDeque::new()),
            tx: Mutex::new(Vec::new()),
            steps: Mutex::new(VecDeque::new()),
            cond: Condvar::new(),
            cond_mutex: Mutex::new(()),
            open: AtomicBool::new(false),
            opens: AtomicUsize::new(0),
            closes: AtomicUsize::new(0),
            writes: AtomicUsize::new(0),
            os_submits: AtomicUsize::new(0),
            blocked_release: AtomicBool::new(false),
            close_err: Mutex::new(None),
            removed: AtomicBool::new(false),
            external_hold: Mutex::new(None),
            access_denied: AtomicBool::new(false),
            woken: AtomicBool::new(false),
            peer_closed: AtomicBool::new(false),
            eof_reads: AtomicUsize::new(0),
            read_errors: AtomicUsize::new(0),
            on_os_submit_hook: Mutex::new(None),
            paused: Mutex::new(false),
            paused_cond: Condvar::new(),
            pause_released: Mutex::new(false),
            pause_released_cond: Condvar::new(),
            tx_replies: Mutex::new(Vec::new()),
            tx_matches: Mutex::new(Vec::new()),
            tx_match_cursor: Mutex::new(0),
        }
    }
}

fn check_tx_triggers_and_matches(inner: &FakeDeviceInner) {
    let mut to_push = Vec::new();
    {
        let tx = inner.tx.lock().unwrap_or_else(|e| e.into_inner());
        let mut replies = inner.tx_replies.lock().unwrap_or_else(|e| e.into_inner());
        for (trigger, reply, pushed) in replies.iter_mut() {
            if !*pushed && trigger.len() <= tx.len() {
                if tx.windows(trigger.len()).any(|w| w == trigger.as_slice()) {
                    *pushed = true;
                    to_push.extend_from_slice(reply);
                }
            }
        }

        let mut cursor = inner.tx_match_cursor.lock().unwrap_or_else(|e| e.into_inner());
        if *cursor < tx.len() {
            let uninspected = &tx[*cursor..];
            if let Some(last_nl) = uninspected.iter().rposition(|&b| b == b'\n') {
                let complete_lines = &uninspected[..=last_nl];
                let matches = inner.tx_matches.lock().unwrap_or_else(|e| e.into_inner());
                for line in complete_lines.split_inclusive(|&b| b == b'\n') {
                    let line_str = String::from_utf8_lossy(line);
                    let trimmed = line_str.trim_end_matches(&['\r', '\n'][..]);
                    for (regex, reply_fn) in matches.iter() {
                        if regex.is_match(&line_str) || regex.is_match(trimmed) {
                            to_push.extend_from_slice(&reply_fn(line));
                        }
                    }
                }
                *cursor += last_nl + 1;
            }
        }
    }

    if !to_push.is_empty() {
        let _g = inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        let mut rx = inner.rx.lock().unwrap_or_else(|e| e.into_inner());
        rx.extend(to_push);
        drop(rx);
        inner.cond.notify_all();
    }
}

fn do_pause(inner: &FakeDeviceInner) {
    let mut p = inner.paused.lock().unwrap_or_else(|e| e.into_inner());
    *p = true;
    inner.paused_cond.notify_all();
    drop(p);

    let mut r = inner.pause_released.lock().unwrap_or_else(|e| e.into_inner());
    while !*r {
        r = inner
            .pause_released_cond
            .wait(r)
            .unwrap_or_else(|e| e.into_inner());
    }
    *r = false;

    let mut p = inner.paused.lock().unwrap_or_else(|e| e.into_inner());
    *p = false;
}

/// 記憶體序列埠後端：以路徑鍵控的虛擬裝置表。
pub struct FakeSerialBackend {
    devices: Mutex<HashMap<String, Arc<FakeDeviceInner>>>,
}

impl FakeSerialBackend {
    pub fn new() -> Arc<FakeSerialBackend> {
        Arc::new(FakeSerialBackend {
            devices: Mutex::new(HashMap::new()),
        })
    }

    pub fn add_device(&self, path: &str) -> FakeDevice {
        let mut devs = self.devices.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(inner) = devs.get(path) {
            inner.removed.store(false, Ordering::SeqCst);
            FakeDevice {
                inner: Arc::clone(inner),
            }
        } else {
            let inner = Arc::new(FakeDeviceInner::new());
            devs.insert(path.to_string(), Arc::clone(&inner));
            FakeDevice { inner }
        }
    }

    /// 下次 `open_exclusive` → `Held(HolderReport{pids, holder_unknown: pids.is_empty()})`。
    pub fn hold_externally(&self, path: &str, pids: Vec<u32>) {
        let dev = self.add_device(path);
        *dev.inner.external_hold.lock().unwrap_or_else(|e| e.into_inner()) = Some(pids);
    }

    /// → `AccessDenied{holder: pids [] / holder_unknown true / Unsupported}`。
    pub fn deny_access(&self, path: &str) {
        let dev = self.add_device(path);
        dev.inner.access_denied.store(true, Ordering::SeqCst);
    }

    /// → 之後 open 回 `NotFound`；已開 handle 的 read 回 `Err(kind=Io, "[Errno 5] Input/output error")`。
    pub fn remove_device(&self, path: &str) {
        let devs = self.devices.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(inner) = devs.get(path) {
            inner.removed.store(true, Ordering::SeqCst);
            let _g = inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
            inner.cond.notify_all();
        }
    }
}

impl SerialBackend for FakeSerialBackend {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        _cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        let devs = self.devices.lock().unwrap_or_else(|e| e.into_inner());
        let inner = match devs.get(&dev.0) {
            Some(i) => Arc::clone(i),
            None => {
                return Err(OpenError::NotFound(PalError::not_found(
                    "[Errno 2] No such file or directory",
                )))
            }
        };
        drop(devs);

        if inner.removed.load(Ordering::SeqCst) {
            return Err(OpenError::NotFound(PalError::not_found(
                "[Errno 2] No such file or directory",
            )));
        }

        if inner.access_denied.load(Ordering::SeqCst) {
            return Err(OpenError::AccessDenied {
                os: PalError::io(13, "[Errno 13] Permission denied"),
                holder: HolderReport {
                    pids: vec![],
                    holder_unknown: true,
                    status: HolderScanStatus::Unsupported,
                },
            });
        }

        if let Some(pids) = inner
            .external_hold
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            let unknown = pids.is_empty();
            return Err(OpenError::Held(HolderReport {
                pids,
                holder_unknown: unknown,
                status: HolderScanStatus::Ok,
            }));
        }

        inner.woken.store(false, Ordering::SeqCst);
        inner.peer_closed.store(false, Ordering::SeqCst);

        if inner.open.swap(true, Ordering::SeqCst) {
            return Err(OpenError::Held(HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Ok,
            }));
        }

        inner.opens.fetch_add(1, Ordering::SeqCst);
        let open_guard = Arc::new(OpenGuard {
            inner: Arc::clone(&inner),
            closed_via_token: AtomicBool::new(false),
        });
        Ok(Box::new(FakeSerialHandle { inner, open_guard }))
    }
}

/// 句柄釋放防護：未呼叫 `CloseToken::close` 即 drop 時自動清除占用（不計入 closes）。
pub(crate) struct OpenGuard {
    inner: Arc<FakeDeviceInner>,
    closed_via_token: AtomicBool,
}

impl Drop for OpenGuard {
    fn drop(&mut self) {
        if !self.closed_via_token.load(Ordering::SeqCst) {
            self.inner.open.store(false, Ordering::SeqCst);
        }
    }
}

/// 單一虛擬裝置的操作面（與 `FakeSerialBackend` 共享狀態）。
#[derive(Clone)]
pub struct FakeDevice {
    inner: Arc<FakeDeviceInner>,
}

impl FakeDevice {
    /// 注入 RX bytes（reader 可讀）。
    pub fn push_rx(&self, bytes: &[u8]) {
        let _g = self.inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        let mut rx = self.inner.rx.lock().unwrap_or_else(|e| e.into_inner());
        rx.extend(bytes);
        drop(rx);
        self.inner.cond.notify_all();
    }

    /// 模擬對端關閉（reader 讀完剩餘資料後回 `Ok(ReadOutcome::Eof)`）。
    pub fn close_peer(&self) {
        self.inner.peer_closed.store(true, Ordering::SeqCst);
        let _g = self.inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        self.inner.cond.notify_all();
    }

    /// Fake reader 已觀察到的 EOF 次數（供同步測試使用）。
    pub fn eof_reads(&self) -> usize {
        self.inner.eof_reads.load(Ordering::SeqCst)
    }

    /// Fake reader 已觀察到的讀取錯誤次數（供同步測試使用）。
    pub fn read_errors(&self) -> usize {
        self.inner.read_errors.load(Ordering::SeqCst)
    }

    /// 模擬裝置拔除；既有 handle 仍開啟，但後續讀取回錯誤。
    pub fn remove(&self) {
        self.inner.removed.store(true, Ordering::SeqCst);
        let _g = self.inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        self.inner.cond.notify_all();
    }

    /// 只含已「確認寫出」的 bytes；讀後清空。
    pub fn take_tx(&self) -> Vec<u8> {
        let mut tx = self.inner.tx.lock().unwrap_or_else(|e| e.into_inner());
        let out = tx.clone();
        tx.clear();
        *self
            .inner
            .tx_match_cursor
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = 0;
        out
    }

    /// 依序消耗寫入腳本；耗盡後恢復 `Accept(全部)`。
    pub fn script_write(&self, steps: Vec<FakeWriteStep>) {
        let mut s = self.inner.steps.lock().unwrap_or_else(|e| e.into_inner());
        s.extend(steps);
    }

    pub fn release_block(&self) {
        self.inner.blocked_release.store(true, Ordering::SeqCst);
        let _g = self.inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        self.inner.cond.notify_all();
    }

    /// 之後 `close` 回 `closed:false, excl_cleared:false, error:Some(err)`。
    pub fn fail_close(&self, err: PalError) {
        *self.inner.close_err.lock().unwrap_or_else(|e| e.into_inner()) = Some(err);
    }

    pub fn open_count(&self) -> usize {
        self.inner.opens.load(Ordering::SeqCst)
    }

    pub fn close_count(&self) -> usize {
        self.inner.closes.load(Ordering::SeqCst)
    }

    pub fn is_open(&self) -> bool {
        self.inner.open.load(Ordering::SeqCst)
    }

    /// `write_gated`／`write` 被呼叫的次數（TxWriter 出列快速拒絕的零 TX 斷言用）。
    pub fn write_calls(&self) -> usize {
        self.inner.writes.load(Ordering::SeqCst)
    }

    /// `os_submit` 閉包實際執行（bytes 交給假 OS）的次數；gate 撤銷不計。
    pub fn os_submits(&self) -> usize {
        self.inner.os_submits.load(Ordering::SeqCst)
    }

    /// 每次 `os_submit` 閉包內（持 Fence 鎖時）呼叫；鎖持有斷言用。
    pub fn on_os_submit(&self, hook: Box<dyn Fn() + Send + Sync>) {
        *self
            .inner
            .on_os_submit_hook
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(hook);
    }

    /// 等 writer 抵達 `Pause*`／`AcceptThenWouldBlock` 的暫停點。
    pub fn wait_paused(&self, timeout: Duration) -> bool {
        let start = std::time::Instant::now();
        let mut p = self.inner.paused.lock().unwrap_or_else(|e| e.into_inner());
        while !*p {
            let elapsed = start.elapsed();
            if elapsed >= timeout {
                return false;
            }
            let (guard, res) = self
                .inner
                .paused_cond
                .wait_timeout(p, timeout - elapsed)
                .unwrap_or_else(|e| e.into_inner());
            p = guard;
            if res.timed_out() && !*p {
                return false;
            }
        }
        true
    }

    pub fn release_pause(&self) {
        let mut r = self
            .inner
            .pause_released
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        *r = true;
        self.inner.pause_released_cond.notify_all();
    }

    /// 已確認 TX 累積緩衝出現 `trigger` 時推一次 `reply`。
    pub fn on_tx_reply(&self, trigger: &[u8], reply: &[u8]) {
        self.inner
            .tx_replies
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((trigger.to_vec(), reply.to_vec(), false));
        check_tx_triggers_and_matches(&self.inner);
    }

    /// `pattern` 對每個已確認 TX 行比對；`reply` 取得命中的行（random nonce probe 用）。
    pub fn on_tx_match(
        &self,
        pattern: &str,
        reply: Box<dyn Fn(&[u8]) -> Vec<u8> + Send + Sync>,
    ) {
        let regex = SimpleRegex::parse(pattern);
        self.inner
            .tx_matches
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((regex, reply));
        check_tx_triggers_and_matches(&self.inner);
    }
}

/// 替身的 `UartExclusive`（Task 1 Produces）：`report` 由測試預先注入，`probe_holders` 回傳對應報告。
pub struct FakeUartExclusive {
    pub report: Mutex<HashMap<String, HolderReport>>,
}

impl UartExclusive for FakeUartExclusive {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        self.report
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(real_path)
            .cloned()
            .unwrap_or(HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Ok,
            })
    }
}

struct FakeSerialHandle {
    inner: Arc<FakeDeviceInner>,
    open_guard: Arc<OpenGuard>,
}

impl SerialHandle for FakeSerialHandle {
    fn split(
        self: Box<Self>,
    ) -> (
        Box<dyn SerialReader>,
        Box<dyn SerialWriter>,
        Arc<dyn crate::traits::IoWaker>,
        CloseToken,
    ) {
        let reader = Box::new(FakeSerialReader {
            inner: Arc::clone(&self.inner),
            _open_guard: Arc::clone(&self.open_guard),
        });
        let writer = Box::new(FakeSerialWriter {
            inner: Arc::clone(&self.inner),
            _open_guard: Arc::clone(&self.open_guard),
        });
        let waker = Arc::new(FakeIoWaker {
            inner: Arc::clone(&self.inner),
        });
        let close_token = CloseToken::new(Box::new(FakeCloseImpl {
            inner: Arc::clone(&self.inner),
            open_guard: Arc::clone(&self.open_guard),
        }));
        (reader, writer, waker, close_token)
    }

    fn set_baud(&self, _baud: u32) -> PalResult<()> {
        Ok(())
    }

    fn is_alive(&self) -> bool {
        self.inner.open.load(Ordering::SeqCst)
    }
}

struct FakeIoWaker {
    inner: Arc<FakeDeviceInner>,
}

impl crate::traits::IoWaker for FakeIoWaker {
    fn wake(&self) {
        self.inner.woken.store(true, Ordering::SeqCst);
        let _g = self.inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        self.inner.cond.notify_all();
    }
}

struct FakeSerialReader {
    inner: Arc<FakeDeviceInner>,
    _open_guard: Arc<OpenGuard>,
}

impl SerialReader for FakeSerialReader {
    fn read(
        &mut self,
        buf: &mut [u8],
        timeout: Option<Duration>,
    ) -> Result<ReadOutcome, ReadError> {
        let start = std::time::Instant::now();
        let mut guard = self.inner.cond_mutex.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if self.inner.removed.load(Ordering::SeqCst) {
                self.inner.read_errors.fetch_add(1, Ordering::SeqCst);
                return Err(ReadError::Read(PalError::io(
                    5,
                    "[Errno 5] Input/output error",
                )));
            }
            if self.inner.woken.swap(false, Ordering::SeqCst) {
                return Ok(ReadOutcome::Woken);
            }
            {
                let mut rx = self.inner.rx.lock().unwrap_or_else(|e| e.into_inner());
                if !rx.is_empty() {
                    let n = std::cmp::min(buf.len(), rx.len());
                    for b in &mut buf[..n] {
                        *b = rx.pop_front().unwrap();
                    }
                    return Ok(ReadOutcome::Data(n));
                }
            }
            if self.inner.peer_closed.load(Ordering::SeqCst) {
                self.inner.eof_reads.fetch_add(1, Ordering::SeqCst);
                return Ok(ReadOutcome::Eof);
            }
            match timeout {
                Some(d) => {
                    let elapsed = start.elapsed();
                    if elapsed >= d {
                        return Ok(ReadOutcome::TimedOut);
                    }
                    let remaining = d - elapsed;
                    let (next_guard, _) = self
                        .inner
                        .cond
                        .wait_timeout(guard, remaining)
                        .unwrap_or_else(|e| e.into_inner());
                    guard = next_guard;
                }
                None => {
                    guard = self.inner.cond.wait(guard).unwrap_or_else(|e| e.into_inner());
                }
            }
        }
    }
}

struct FakeCloseImpl {
    inner: Arc<FakeDeviceInner>,
    open_guard: Arc<OpenGuard>,
}

impl crate::traits::CloseImpl for FakeCloseImpl {
    fn is_alive(&self) -> bool {
        self.inner.open.load(Ordering::SeqCst)
    }

    fn close(self: Box<Self>) -> CloseReceipt {
        self.inner.closes.fetch_add(1, Ordering::SeqCst);
        if let Some(err) = self
            .inner
            .close_err
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            self.open_guard.closed_via_token.store(true, Ordering::SeqCst);
            CloseReceipt {
                closed: false,
                excl_cleared: false,
                error: Some(err),
            }
        } else {
            self.open_guard.closed_via_token.store(true, Ordering::SeqCst);
            self.inner.open.store(false, Ordering::SeqCst);
            CloseReceipt {
                closed: true,
                excl_cleared: true,
                error: None,
            }
        }
    }
}

struct FakeSerialWriter {
    inner: Arc<FakeDeviceInner>,
    _open_guard: Arc<OpenGuard>,
}

impl SerialWriter for FakeSerialWriter {
    fn write_gated(&mut self, data: &[u8], cancel: &CancelToken, gate: &dyn SubmitGate) -> TxResult {
        self.inner.writes.fetch_add(1, Ordering::SeqCst);

        if cancel.is_cancelled() {
            return TxResult {
                outcome: TxOutcome::Completed(0),
                error: Some(PalError::cancelled()),
            };
        }

        let step = self
            .inner
            .steps
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .pop_front()
            .unwrap_or(FakeWriteStep::Accept(data.len()));

        match step {
            FakeWriteStep::PauseBeforeSubmit => {
                do_pause(&self.inner);
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(data);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = data.len();
                    SubmitStep::Accepted(data.len())
                });
                match submit_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: None,
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                }
            }
            FakeWriteStep::PauseInsideSubmit => {
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    do_pause(&self.inner);
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(data);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = data.len();
                    SubmitStep::Accepted(data.len())
                });
                match submit_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: None,
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                }
            }
            FakeWriteStep::AcceptThenWouldBlock(n) => {
                let to_accept = n.min(data.len());
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(&data[..to_accept]);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = to_accept;
                    SubmitStep::Accepted(to_accept)
                });
                if submit_res.is_err() {
                    return TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    };
                }

                do_pause(&self.inner);

                let remaining = &data[committed..];
                if remaining.is_empty() {
                    return TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: None,
                    };
                }
                let second_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(remaining);
                    check_tx_triggers_and_matches(&self.inner);
                    committed += remaining.len();
                    SubmitStep::Accepted(remaining.len())
                });
                match second_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: None,
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: Some(PalError::cancelled()),
                    },
                }
            }
            FakeWriteStep::Accept(n) => {
                let to_accept = n.min(data.len());
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(&data[..to_accept]);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = to_accept;
                    SubmitStep::Accepted(to_accept)
                });
                match submit_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: None,
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                }
            }
            FakeWriteStep::AcceptThenError(n, err) => {
                let to_accept = n.min(data.len());
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(&data[..to_accept]);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = to_accept;
                    SubmitStep::Accepted(to_accept)
                });
                match submit_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Completed(committed),
                        error: Some(err),
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                }
            }
            FakeWriteStep::Unknown { confirmed, error } => {
                let to_accept = confirmed.min(data.len());
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    if to_accept > 0 {
                        self.inner
                            .tx
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .extend_from_slice(&data[..to_accept]);
                        check_tx_triggers_and_matches(&self.inner);
                        SubmitStep::Accepted(to_accept)
                    } else {
                        SubmitStep::Pending
                    }
                });
                match submit_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Unknown { confirmed },
                        error: Some(error),
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                }
            }
            FakeWriteStep::BlockUntilCancel {
                accept_before_cancel,
            } => {
                let to_accept = accept_before_cancel.min(data.len());
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(&data[..to_accept]);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = to_accept;
                    SubmitStep::Accepted(to_accept)
                });
                if submit_res.is_err() {
                    return TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    };
                }
                let mut guard = self
                    .inner
                    .cond_mutex
                    .lock()
                    .unwrap_or_else(|e| e.into_inner());
                while !cancel.is_cancelled() {
                    guard = self
                        .inner
                        .cond
                        .wait_timeout(guard, Duration::from_millis(10))
                        .unwrap_or_else(|e| e.into_inner())
                        .0;
                }
                TxResult {
                    outcome: TxOutcome::Completed(committed),
                    error: Some(PalError::cancelled()),
                }
            }
            FakeWriteStep::BlockForever => {
                let mut committed = 0;
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    self.inner
                        .tx
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .extend_from_slice(data);
                    check_tx_triggers_and_matches(&self.inner);
                    committed = data.len();
                    SubmitStep::Accepted(data.len())
                });
                if submit_res.is_err() {
                    return TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    };
                }
                self.inner.blocked_release.store(false, Ordering::SeqCst);
                let mut guard = self
                    .inner
                    .cond_mutex
                    .lock()
                    .unwrap_or_else(|e| e.into_inner());
                while !self.inner.blocked_release.load(Ordering::SeqCst) {
                    guard = self
                        .inner
                        .cond
                        .wait_timeout(guard, Duration::from_millis(10))
                        .unwrap_or_else(|e| e.into_inner())
                        .0;
                }
                self.inner.blocked_release.store(false, Ordering::SeqCst);
                TxResult {
                    outcome: TxOutcome::Completed(committed),
                    error: None,
                }
            }
            FakeWriteStep::Error(err) => {
                let submit_res = gate.submit(&mut || {
                    self.inner.os_submits.fetch_add(1, Ordering::SeqCst);
                    if let Some(hook) = self
                        .inner
                        .on_os_submit_hook
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_ref()
                    {
                        hook();
                    }
                    SubmitStep::Failed(err.clone())
                });
                match submit_res {
                    Ok(_) => TxResult {
                        outcome: TxOutcome::Completed(0),
                        error: Some(err),
                    },
                    Err(SubmitRevoked) => TxResult {
                        outcome: TxOutcome::NotSubmitted,
                        error: None,
                    },
                }
            }
        }
    }
}
