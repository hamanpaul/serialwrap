//! serial conformance（arch §9.1、計畫 Task 2 Step 1 表）：同一套斷言跑在 fakes、POSIX PTY、Windows COM pair 上。
//!
//! 每個子檢查的失敗訊息都帶列名（`[roundtrip_tx]` 等）。T2 測試作者交付；實作者不得改寫斷言。

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use super::PeerEnd;
use crate::traits::{
    CancelToken, CloseToken, DevicePath, IoWaker, PalErrorKind, ReadOutcome, SerialBackend,
    SerialConfig, SerialReader, SerialWriter, SubmitGate, SubmitRevoked, SubmitStep, TxOutcome,
};

type Pair<'a> = &'a dyn Fn() -> (DevicePath, Box<dyn PeerEnd>);

type Parts = (
    Box<dyn SerialReader>,
    Box<dyn SerialWriter>,
    Arc<dyn IoWaker>,
    CloseToken,
);

fn cfg() -> SerialConfig {
    SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)
}

fn open_split(be: &dyn SerialBackend, dev: &DevicePath, tag: &str) -> Parts {
    let h = be
        .open_exclusive(dev, &cfg())
        .unwrap_or_else(|e| panic!("[{tag}] open_exclusive 失敗: {e:?}"));
    h.split()
}

/// 寫入 `hello\n`：`Completed(6)`、無 error，peer 讀到 `hello\n`。
fn roundtrip_tx(be: &dyn SerialBackend, pair: Pair) {
    let (dev, mut peer) = pair();
    let (r, mut w, waker, close) = open_split(be, &dev, "roundtrip_tx");
    let cancel = CancelToken::new(waker);
    let res = w.write(b"hello\n", &cancel);
    assert_eq!(res.outcome, TxOutcome::Completed(6), "[roundtrip_tx] outcome");
    assert!(res.error.is_none(), "[roundtrip_tx] error {:?}", res.error);
    assert_eq!(
        peer.read_available(Duration::from_millis(500)),
        b"hello\n".to_vec(),
        "[roundtrip_tx] peer 應讀到 hello\\n"
    );
    close.close(r, w);
}

/// peer 寫 `\x00\xff\r\n`：`read` 回 `Data(4)` 且內容相同（raw，不做 CR/LF 轉換）。
fn roundtrip_rx(be: &dyn SerialBackend, pair: Pair) {
    let (dev, mut peer) = pair();
    let (mut r, w, _waker, close) = open_split(be, &dev, "roundtrip_rx");
    peer.write(b"\x00\xff\r\n");
    let mut buf = [0u8; 16];
    match r.read(&mut buf, Some(Duration::from_millis(500))) {
        Ok(ReadOutcome::Data(4)) => {
            assert_eq!(&buf[..4], b"\x00\xff\r\n", "[roundtrip_rx] 內容不符")
        }
        other => panic!("[roundtrip_rx] 應為 Data(4)，實際 {other:?}"),
    }
    close.close(r, w);
}

/// 無資料 `read(Some(200ms))` → `TimedOut`，耗時 ∈ [180, 400] ms。
fn read_timeout(be: &dyn SerialBackend, pair: Pair) {
    let (dev, _peer) = pair();
    let (mut r, w, _waker, close) = open_split(be, &dev, "read_timeout");
    let mut buf = [0u8; 8];
    let t0 = Instant::now();
    let out = r.read(&mut buf, Some(Duration::from_millis(200)));
    let el = t0.elapsed().as_millis();
    assert!(
        matches!(out, Ok(ReadOutcome::TimedOut)),
        "[read_timeout] 應為 TimedOut，實際 {out:?}"
    );
    assert!(
        (180..=400).contains(&el),
        "[read_timeout] 耗時 {el} ms 不在 [180, 400]"
    );
    close.close(r, w);
}

/// `close` 回 `closed:true`、`excl_cleared:true`、`error:None`；peer 保持開啟，之後同一路徑可再次 `open_exclusive`。
fn close_once(be: &dyn SerialBackend, pair: Pair) {
    let (dev, _peer) = pair();
    let (r, w, _waker, close) = open_split(be, &dev, "close_once");
    let rc = close.close(r, w);
    assert!(
        rc.closed && rc.excl_cleared && rc.error.is_none(),
        "[close_once] receipt {rc:?}"
    );
    let (r2, w2, _waker2, close2) = open_split(be, &dev, "close_once(reopen)");
    close2.close(r2, w2);
}

/// peer 關閉後 `read` 回 `Ok(Eof)`（呼叫端不得當成 I/O error，UIO-12.10-d KEEP）。
fn eof_not_bridge_down(be: &dyn SerialBackend, pair: Pair) {
    let (dev, peer) = pair();
    let (mut r, w, _waker, close) = open_split(be, &dev, "eof_not_bridge_down");
    drop(peer);
    let mut buf = [0u8; 8];
    match r.read(&mut buf, Some(Duration::from_millis(500))) {
        Ok(ReadOutcome::Eof) => {}
        other => panic!("[eof_not_bridge_down] peer 關閉後應為 Ok(Eof)，實際 {other:?}"),
    }
    close.close(r, w);
}

/// 寫前已 cancel：`Completed(0)`、`error.kind == Cancelled`，peer 讀不到任何 byte。
fn cancel_before_write(be: &dyn SerialBackend, pair: Pair) {
    let (dev, mut peer) = pair();
    let (r, mut w, waker, close) = open_split(be, &dev, "cancel_before_write");
    let cancel = CancelToken::new(waker);
    cancel.cancel();
    let res = w.write(b"never\n", &cancel);
    assert_eq!(res.outcome, TxOutcome::Completed(0), "[cancel_before_write] outcome");
    assert_eq!(
        res.error.as_ref().map(|e| e.kind),
        Some(PalErrorKind::Cancelled),
        "[cancel_before_write] error {:?}",
        res.error
    );
    assert_eq!(
        peer.read_available(Duration::from_millis(200)),
        Vec::<u8>::new(),
        "[cancel_before_write] peer 不得收到任何 byte"
    );
    close.close(r, w);
}

/// 永遠撤銷的 gate（R0-F1）：不得呼叫 `os_submit`。
struct RevokeGate {
    calls: AtomicUsize,
}

impl SubmitGate for RevokeGate {
    fn submit(
        &self,
        _os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Err(SubmitRevoked)
    }
}

/// 放行並計數：記錄 gate 呼叫次數與 `os_submit` 閉包實際執行次數（R0-F1）。
struct CountGate {
    calls: AtomicUsize,
    os_runs: AtomicUsize,
}

impl SubmitGate for CountGate {
    fn submit(
        &self,
        os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let step = os_submit();
        self.os_runs.fetch_add(1, Ordering::SeqCst);
        Ok(step)
    }
}

/// 永遠回 `Err(SubmitRevoked)` 的 gate 寫 `zz\n`：`NotSubmitted`、無 error、gate 恰呼叫 1 次、peer 於 300 ms 內讀不到任何 byte。
fn gate_revoked_never_reaches_os(be: &dyn SerialBackend, pair: Pair) {
    let (dev, mut peer) = pair();
    let (r, mut w, waker, close) = open_split(be, &dev, "gate_revoked_never_reaches_os");
    let cancel = CancelToken::new(waker);
    let gate = RevokeGate {
        calls: AtomicUsize::new(0),
    };
    let res = w.write_gated(b"zz\n", &cancel, &gate);
    assert_eq!(
        res.outcome,
        TxOutcome::NotSubmitted,
        "[gate_revoked_never_reaches_os] outcome"
    );
    assert!(
        res.error.is_none(),
        "[gate_revoked_never_reaches_os] error {:?}",
        res.error
    );
    assert_eq!(
        gate.calls.load(Ordering::SeqCst),
        1,
        "[gate_revoked_never_reaches_os] gate 應被呼叫恰 1 次"
    );
    assert_eq!(
        peer.read_available(Duration::from_millis(300)),
        Vec::<u8>::new(),
        "[gate_revoked_never_reaches_os] peer 於 300 ms 內不得讀到任何 byte"
    );
    close.close(r, w);
}

/// 計數 gate 放行寫 64 KiB：`Completed(65536)`、無 error；`os_submit` 閉包至少執行一次；peer 收齊完整 bytes。
/// 計畫要求的「閉包次數等於 PAL 實際 syscall／WriteFile 次數」需要 PAL 內 debug 計數器（尚未定義 seam），
/// 目前未在此斷言；實作者須補上該 seam 後再加入相等斷言。
fn gate_wraps_every_os_submit(be: &dyn SerialBackend, pair: Pair) {
    let (dev, mut peer) = pair();
    let (r, mut w, waker, close) = open_split(be, &dev, "gate_wraps_every_os_submit");
    let cancel = CancelToken::new(waker);
    let gate = CountGate {
        calls: AtomicUsize::new(0),
        os_runs: AtomicUsize::new(0),
    };
    let payload = vec![b'Z'; 65536];
    let mut got: Vec<u8> = Vec::new();
    let res = thread::scope(|s| {
        // 寫端與讀端必須並行：PTY buffer 遠小於 64 KiB
        let j = s.spawn(|| w.write_gated(&payload, &cancel, &gate));
        let deadline = Instant::now() + Duration::from_secs(10);
        while got.len() < payload.len() && Instant::now() < deadline {
            got.extend(peer.read_available(Duration::from_millis(100)));
        }
        j.join().expect("writer thread")
    });
    assert_eq!(
        res.outcome,
        TxOutcome::Completed(65536),
        "[gate_wraps_every_os_submit] outcome"
    );
    assert!(
        res.error.is_none(),
        "[gate_wraps_every_os_submit] error {:?}",
        res.error
    );
    assert!(
        gate.os_runs.load(Ordering::SeqCst) >= 1,
        "[gate_wraps_every_os_submit] os_submit 閉包應至少執行一次"
    );
    assert_eq!(
        got.len(),
        payload.len(),
        "[gate_wraps_every_os_submit] peer 應收齊 65536 bytes"
    );
    assert!(
        got.iter().all(|&b| b == b'Z'),
        "[gate_wraps_every_os_submit] peer 收到的 bytes 內容不符"
    );
    close.close(r, w);
}

/// serial conformance 全套（順序即計畫表列順序）。
pub fn serial_suite(backend: Arc<dyn SerialBackend>, pair: Pair) {
    let be: &dyn SerialBackend = &*backend;
    roundtrip_tx(be, pair);
    roundtrip_rx(be, pair);
    read_timeout(be, pair);
    close_once(be, pair);
    eof_not_bridge_down(be, pair);
    cancel_before_write(be, pair);
    gate_revoked_never_reaches_os(be, pair);
    gate_wraps_every_os_submit(be, pair);
}
