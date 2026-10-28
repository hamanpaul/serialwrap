//! P5-T17：RPC framing（`serve_connection`）與 `RpcServer` acceptor／停止流程（arch §6.2）。
//!
//! 位元組表逐字對照 Python `sw_core/rpc_posix.py::serve_connection`；真 socket 案例一律走 TCP loopback
//! （經 `sw_pal::native().rpc_listen`），使同一份測試在 POSIX 與 Windows 皆可執行（測試碼不得用平台 cfg）。
#![cfg(feature = "test-fakes")]

mod support;

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use support::serde_free;
use sw_daemon::framing::*;
use sw_pal::fakes::mem_stream::MemStream;
use sw_pal::traits::{EndpointSpec, ListenOptions, PalResult, RpcListener, RpcServerStream};
use sw_proto::pyjson::{PyDict, PyValue};

struct H(Mutex<Vec<String>>);
impl RpcHandler for H {
    fn call(&self, m: &str, p: &PyDict) -> Result<PyDict, sw_proto::pycompat::PyErr> {
        self.0.lock().unwrap().push(m.to_string());
        match m {
            "boom" => Err(sw_proto::pycompat::PyErr::value_error("kaboom")),
            "echo" => {
                let mut d = PyDict::new();
                d.insert("ok", PyValue::Bool(true));
                d.insert("params", PyValue::Dict(p.clone()));
                d.insert("id", PyValue::from("x"));
                Ok(d)
            }
            // 以下為 Task 17 表列案例所需的追加方法（不影響上方位元組表）。
            "slow" => {
                std::thread::sleep(Duration::from_millis(500));
                let mut d = PyDict::new();
                d.insert("ok", PyValue::Bool(true));
                Ok(d)
            }
            "block" => {
                std::thread::sleep(Duration::from_millis(200));
                self.0.lock().unwrap().push("block-done".into());
                let mut d = PyDict::new();
                d.insert("ok", PyValue::Bool(true));
                Ok(d)
            }
            "panic" => panic!("x"),
            "big" => {
                let mut d = PyDict::new();
                d.insert("ok", PyValue::Bool(true));
                d.insert("blob", PyValue::Str("y".repeat(256 * 1024)));
                Ok(d)
            }
            // 審查 F06：遠大於 loopback 收送緩衝的單筆回應（stop 總期限案例）。
            "huge" => {
                let mut d = PyDict::new();
                d.insert("ok", PyValue::Bool(true));
                d.insert("blob", PyValue::Str("z".repeat(48 * 1024 * 1024)));
                Ok(d)
            }
            _ => {
                let mut d = PyDict::new();
                d.insert("ok", PyValue::Bool(true));
                Ok(d)
            }
        }
    }
    fn request_stop(&self) {
        self.0.lock().unwrap().push("<stop>".into());
    }
}

fn new_h() -> Arc<H> {
    Arc::new(H(Mutex::new(vec![])))
}

fn run(input: &[u8]) -> (Vec<u8>, Vec<String>) {
    let (mut a, b) = MemStream::pair();
    let h = new_h();
    let h2 = h.clone();
    let j = std::thread::spawn(move || serve_connection(Box::new(b), &*h2, &ConnStop::new()));
    a.write_all(input).unwrap();
    a.close_write();
    let mut out = Vec::new();
    a.read_to_end(&mut out).unwrap();
    j.join().unwrap();
    let calls = h.0.lock().unwrap().clone();
    (out, calls)
}

#[test]
fn framing_byte_table() {
    let cases: &[(&[u8], &str)] = &[
        (b"not json\n", "{\"ok\": false, \"error_code\": \"INVALID_JSON\"}\n"),
        (b"[1,2]\n", "{\"ok\": false, \"error_code\": \"INVALID_REQUEST\"}\n"),
        (b"{\"id\":7}\n", "{\"id\":7,\"ok\":false,\"error_code\":\"INVALID_METHOD\"}\n"),
        (b"{\"id\":1,\"method\":\"boom\"}\n", "{\"id\":1,\"ok\":false,\"error_code\":\"EXCEPTION\",\"message\":\"kaboom\"}\n"),
        (b"{\"id\":2,\"method\":\"echo\",\"params\":[1]}\n", "{\"id\":\"x\",\"ok\":true,\"params\":{}}\n"),
        (b"{\"id\":3,\"method\":\"echo\",\"params\":{\"x\":NaN}}\n", "{\"id\":\"x\",\"ok\":true,\"params\":{\"x\":NaN}}\n"),
        (b"\n\r\n  \t\n", ""),
        (b"\xff\xfe{\"id\":1}\n", "{\"ok\": false, \"error_code\": \"INVALID_JSON\"}\n"),
        (b"{\"id\":4,\"method\":\"daemon.stop\"}\n", "{\"id\":4,\"ok\":true,\"stopping\":true}\n"),
        (b"{\"id\":5,\"method\":\"echo\",\"params\":{\"a\":1,\"a\":2}}\n", "{\"id\":\"x\",\"ok\":true,\"params\":{\"a\":2}}\n"),
    ];
    for (input, expect) in cases {
        assert_eq!(String::from_utf8(run(input).0).unwrap(), *expect, "input={:?}", String::from_utf8_lossy(input));
    }
}

#[test]
fn pipelined_requests_answer_in_order() {
    let (out, calls) = run(b"{\"id\":1,\"method\":\"a\"}\n{\"id\":2,\"method\":\"b\"}\n");
    assert_eq!(String::from_utf8(out).unwrap(), "{\"id\":1,\"ok\":true}\n{\"id\":2,\"ok\":true}\n");
    assert_eq!(calls, vec!["a", "b"]);
}

#[test]
fn over_limit_line_closes_without_reply() {
    let golden: serde_free::Golden = serde_free::load("contracts/golden/rpc/line_limit.json"); // {"max_ok_bytes":N,"first_close_bytes":N+1}
    let ok = format!("{{\"id\":1,\"method\":\"a\",\"params\":{{\"p\":\"{}\"}}}}", "x".repeat(golden.max_ok_bytes - 39));
    assert_eq!(ok.len(), golden.max_ok_bytes);
    assert!(!run(format!("{ok}\n").as_bytes()).0.is_empty());
    let bad = format!("{{\"id\":1,\"method\":\"a\",\"params\":{{\"p\":\"{}\"}}}}", "x".repeat(golden.first_close_bytes - 39));
    let (out, calls) = run(format!("{bad}\n{{\"id\":2,\"method\":\"a\"}}\n").as_bytes());
    assert!(out.is_empty() && calls.is_empty());
}

// ---------------------------------------------------------------------------
// 追加位元組斷言（Python 基線行為補強）
// ---------------------------------------------------------------------------

/// asyncio `readline` 於 EOF 回傳殘行：無 `\n` 的最後一行仍被處理。
#[test]
fn trailing_line_without_newline_at_eof_is_processed() {
    let (out, calls) = run(b"{\"id\":9,\"method\":\"a\"}");
    assert_eq!(String::from_utf8(out).unwrap(), "{\"id\":9,\"ok\":true}\n");
    assert_eq!(calls, vec!["a"]);
}

/// golden `crlf_max_ok_bytes`：`\r` 計入 `\n` 前的內容長度。
#[test]
fn crlf_line_limit_counts_carriage_return() {
    let golden = serde_free::load("contracts/golden/rpc/line_limit.json");
    let body = |n: usize| format!("{{\"id\":1,\"method\":\"a\",\"params\":{{\"p\":\"{}\"}}}}", "x".repeat(n - 39));
    // 內容 max_ok_bytes-1 + "\r" = max_ok_bytes → 仍回應。
    let ok = body(golden.max_ok_bytes - 1);
    assert!(!run(format!("{ok}\r\n").as_bytes()).0.is_empty());
    // 內容 max_ok_bytes + "\r" → 超限關閉。
    let bad = body(golden.max_ok_bytes);
    assert!(run(format!("{bad}\r\n").as_bytes()).0.is_empty());
}

/// 無 `\n` 的累積緩衝超過上限即關閉（asyncio `LimitOverrunError`），即使之後補上 `\n` 也不處理。
#[test]
fn over_limit_partial_line_closes_before_newline() {
    let (mut a, b) = MemStream::pair();
    let h = new_h();
    let h2 = h.clone();
    let j = std::thread::spawn(move || serve_connection(Box::new(b), &*h2, &ConnStop::new()));
    a.write_all(&vec![b'x'; MAX_LINE_BYTES + 1]).unwrap();
    // 連線應在收到 `\n` 前即結束。
    let deadline = Instant::now() + Duration::from_secs(5);
    while !j.is_finished() {
        assert!(Instant::now() < deadline, "超限殘行未關閉連線");
        std::thread::sleep(Duration::from_millis(5));
    }
    j.join().unwrap();
    assert!(h.0.lock().unwrap().is_empty());
}

// ---------------------------------------------------------------------------
// 計畫 Task 17 表列案例
// ---------------------------------------------------------------------------

#[test]
fn daemon_stop_calls_request_stop_and_continues_reading() {
    let (out, calls) = run(b"{\"id\":1,\"method\":\"daemon.stop\"}\n{\"id\":2,\"method\":\"a\"}\n");
    assert_eq!(String::from_utf8(out).unwrap(), "{\"id\":1,\"ok\":true,\"stopping\":true}\n{\"id\":2,\"ok\":true}\n");
    assert_eq!(calls, vec!["<stop>", "a"]);
    assert_eq!(calls.iter().filter(|c| *c == "<stop>").count(), 1);
}

#[test]
fn client_disconnect_does_not_cancel_handler() {
    let (mut a, b) = MemStream::pair();
    let h = new_h();
    let h2 = h.clone();
    let j = std::thread::spawn(move || serve_connection(Box::new(b), &*h2, &ConnStop::new()));
    a.write_all(b"{\"id\":1,\"method\":\"block\"}\n").unwrap();
    std::thread::sleep(Duration::from_millis(50));
    drop(a); // handler 阻塞期間 client 關閉
    j.join().expect("寫回失敗不得 panic");
    let calls = h.0.lock().unwrap().clone();
    assert_eq!(calls, vec!["block", "block-done"]);
}

/// 記錄已寫出位元組的 stream 包裝（供判定 `request_stop` 呼叫時回應是否已寫出）。
struct RecStream {
    inner: MemStream,
    written: Arc<Mutex<Vec<u8>>>,
}
impl Read for RecStream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        self.inner.read(buf)
    }
}
impl Write for RecStream {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.written.lock().unwrap().extend_from_slice(&buf[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}
impl RpcServerStream for RecStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        self.inner.shutdown_read()
    }
    fn set_read_timeout(&mut self, d: Option<Duration>) -> PalResult<()> {
        self.inner.set_read_timeout(d)
    }
}

struct StopSnapshot {
    written: Arc<Mutex<Vec<u8>>>,
    at_stop: Mutex<Vec<Vec<u8>>>,
    calls: Mutex<Vec<String>>,
}
impl RpcHandler for StopSnapshot {
    fn call(&self, m: &str, _p: &PyDict) -> Result<PyDict, sw_proto::pycompat::PyErr> {
        self.calls.lock().unwrap().push(m.to_string());
        if m == "slow" {
            std::thread::sleep(Duration::from_millis(500));
        }
        let mut d = PyDict::new();
        d.insert("ok", PyValue::Bool(true));
        Ok(d)
    }
    fn request_stop(&self) {
        self.calls.lock().unwrap().push("<stop>".into());
        self.at_stop.lock().unwrap().push(self.written.lock().unwrap().clone());
    }
}

#[test]
fn pipelined_stop_after_long_call() {
    let (mut a, b) = MemStream::pair();
    let written = Arc::new(Mutex::new(Vec::new()));
    let h = Arc::new(StopSnapshot { written: written.clone(), at_stop: Mutex::new(vec![]), calls: Mutex::new(vec![]) });
    let h2 = h.clone();
    let stream = RecStream { inner: b, written };
    let j = std::thread::spawn(move || serve_connection(Box::new(stream), &*h2, &ConnStop::new()));
    a.write_all(b"{\"id\":1,\"method\":\"slow\"}\n{\"id\":2,\"method\":\"daemon.stop\"}\n").unwrap();
    a.close_write();
    let mut out = Vec::new();
    a.read_to_end(&mut out).unwrap();
    j.join().unwrap();
    assert_eq!(String::from_utf8(out).unwrap(), "{\"id\":1,\"ok\":true}\n{\"id\":2,\"ok\":true,\"stopping\":true}\n");
    assert_eq!(*h.calls.lock().unwrap(), vec!["slow", "<stop>"]);
    let at_stop = h.at_stop.lock().unwrap();
    assert_eq!(at_stop.len(), 1);
    // request_stop 呼叫時，slow 與 stop 兩筆回應皆已寫出（stop 回應先寫、後呼叫 request_stop）。
    assert_eq!(
        String::from_utf8(at_stop[0].clone()).unwrap(),
        "{\"id\":1,\"ok\":true}\n{\"id\":2,\"ok\":true,\"stopping\":true}\n"
    );
}

#[test]
fn handler_panic_is_exception() {
    let (out, calls) = run(b"{\"id\":1,\"method\":\"panic\"}\n{\"id\":2,\"method\":\"a\"}\n");
    assert_eq!(
        String::from_utf8(out).unwrap(),
        "{\"id\":1,\"ok\":false,\"error_code\":\"EXCEPTION\",\"message\":\"x\"}\n{\"id\":2,\"ok\":true}\n"
    );
    assert_eq!(calls, vec!["panic", "a"]);
}

#[test]
fn deep_json_closes_connection() {
    let depth = sw_proto::pyjson::JSON_MAX_DEPTH + 10;
    let deep = format!("{{\"id\":1,\"method\":\"a\",\"params\":{{\"p\":{}{}}}}}", "[".repeat(depth), "]".repeat(depth));
    let (out, calls) = run(format!("{deep}\n{{\"id\":2,\"method\":\"a\"}}\n").as_bytes());
    assert!(out.is_empty(), "深度超限不得回應");
    assert!(calls.is_empty(), "同連線後續請求不得處理");
}

// ---------------------------------------------------------------------------
// RpcServer（真 socket：TCP loopback）
// ---------------------------------------------------------------------------

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn tcp_server(handler: Arc<dyn RpcHandler>) -> (RpcServer, u16) {
    let port = free_port();
    let ep = EndpointSpec::Tcp { host: "127.0.0.1".into(), port };
    let opts = ListenOptions { mode: 0o660, group: None, ensure_dir_mode: None };
    let listener = sw_pal::native().rpc_listen.bind(&ep, &opts).expect("bind tcp loopback");
    (RpcServer::start(listener, handler), port)
}

fn connect(port: u16) -> TcpStream {
    let s = TcpStream::connect(("127.0.0.1", port)).expect("connect");
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    s
}

fn read_line(s: &mut TcpStream) -> std::io::Result<String> {
    let mut out = Vec::new();
    let mut b = [0u8; 1];
    loop {
        let n = s.read(&mut b)?;
        if n == 0 {
            break;
        }
        out.push(b[0]);
        if b[0] == b'\n' {
            break;
        }
    }
    Ok(String::from_utf8(out).unwrap())
}

#[test]
fn idle_connection_does_not_block_stop() {
    let h = new_h();
    let (server, port) = tcp_server(h.clone());
    let _idle = connect(port);
    let mut half = connect(port);
    half.write_all(b"{\"id\":1,\"method\":\"a\"").unwrap();
    // 確認連線已被 accept（以第三條連線往返一行作屏障）。
    let mut probe = connect(port);
    probe.write_all(b"{\"id\":0,\"method\":\"ping\"}\n").unwrap();
    assert_eq!(read_line(&mut probe).unwrap(), "{\"id\":0,\"ok\":true}\n");
    std::thread::sleep(Duration::from_millis(100));
    let t0 = Instant::now();
    assert!(server.stop(Duration::from_secs(5)));
    assert!(t0.elapsed() < Duration::from_millis(500), "stop 耗時 {:?}", t0.elapsed());
    assert_eq!(*h.0.lock().unwrap(), vec!["ping"], "半行不得被處理");
}

#[test]
fn half_line_and_unread_response_do_not_block_stop() {
    let h = new_h();
    let (server, port) = tcp_server(h.clone());
    let mut c = connect(port);
    let mut reqs = Vec::new();
    for i in 0..100 {
        reqs.extend_from_slice(format!("{{\"id\":{i},\"method\":\"big\"}}\n").as_bytes());
    }
    c.write_all(&reqs).unwrap();
    c.write_all(b"{\"id\":999,\"method\":\"a\"").unwrap(); // 半行
    // 等到至少一筆 handler 完成且回應寫入塞滿 socket buffer。
    let deadline = Instant::now() + Duration::from_secs(5);
    while h.0.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "handler 未被呼叫");
        std::thread::sleep(Duration::from_millis(10));
    }
    std::thread::sleep(Duration::from_millis(200));
    let t0 = Instant::now();
    let _converged = server.stop(Duration::from_secs(5));
    assert!(t0.elapsed() < Duration::from_secs(3), "stop 耗時 {:?}", t0.elapsed());
    let calls = h.0.lock().unwrap().clone();
    assert!(!calls.is_empty() && calls.iter().all(|c| c == "big"), "handler 已完成的呼叫不被取消：{calls:?}");
    assert!(calls.len() < 100, "client 未讀回應時不應處理完全部請求");
    drop(c);
}

/// 模擬核心 send 緩衝：client 未讀時，寫入在短輪詢（≈ PAL 的 100 ms `SO_SNDTIMEO`）後回 `WouldBlock`；
/// `drain_after` 之後 client 開始讀，寫入即可前進（審查 F06）。
struct SlowReaderStream {
    input: std::io::Cursor<Vec<u8>>,
    out: Arc<Mutex<Vec<u8>>>,
    t0: Instant,
    drain_after: Duration,
}

impl Read for SlowReaderStream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let n = self.input.read(buf)?;
        if n == 0 {
            // EOF 前保持連線：讓 serve_connection 停在讀迴圈（讀逾時語意）。
            std::thread::sleep(Duration::from_millis(20));
            return Err(std::io::ErrorKind::WouldBlock.into());
        }
        Ok(n)
    }
}

impl Write for SlowReaderStream {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        if self.t0.elapsed() < self.drain_after {
            std::thread::sleep(Duration::from_millis(100));
            return Err(std::io::ErrorKind::WouldBlock.into());
        }
        let n = buf.len().min(4096);
        self.out.lock().unwrap().extend_from_slice(&buf[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl RpcServerStream for SlowReaderStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        Ok(())
    }
    fn set_read_timeout(&mut self, _d: Option<Duration>) -> PalResult<()> {
        Ok(())
    }
}

#[test]
fn slow_reader_large_response_is_not_truncated() {
    // 審查 F06（2fcdbbf）：Python `writer.drain()` 無逾時——client 超過 2 s 不讀，回應仍完整寫出，同連線後續
    // 請求照常處理。舊實作每次 send 套 2 s 寫逾時、`write_all` 遇逾時即關閉 → 截斷並斷線。
    let out = Arc::new(Mutex::new(Vec::new()));
    let stream = SlowReaderStream {
        input: std::io::Cursor::new(b"{\"id\":1,\"method\":\"big\"}\n{\"id\":2,\"method\":\"a\"}\n".to_vec()),
        out: out.clone(),
        t0: Instant::now(),
        drain_after: Duration::from_millis(2600),
    };
    let stop = Arc::new(ConnStop::new());
    let h = new_h();
    let j = {
        let stop = stop.clone();
        std::thread::spawn(move || serve_connection(Box::new(stream), &*h, &stop))
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if out.lock().unwrap().iter().filter(|&&b| b == b'\n').count() >= 2 {
            break;
        }
        assert!(Instant::now() < deadline, "回應未完整寫出（已寫 {} bytes）", out.lock().unwrap().len());
        std::thread::sleep(Duration::from_millis(20));
    }
    stop.request();
    j.join().unwrap();
    let text = String::from_utf8(out.lock().unwrap().clone()).unwrap();
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    assert_eq!(lines.len(), 2);
    assert!(lines[0].starts_with("{\"id\":1,\"ok\":true,\"blob\":\"") && lines[0].ends_with("\"}\n"));
    assert_eq!(lines[0].len(), 256 * 1024 + "{\"id\":1,\"ok\":true,\"blob\":\"\"}\n".len());
    assert_eq!(lines[1], "{\"id\":2,\"ok\":true}\n");
}

#[test]
fn stop_bounds_total_write_time_with_trickle_reader() {
    // 審查 F06（High）：stop 後對「正在寫的回應」套用總期限 2 s（停止時刻起算），不因 client 持續少量讀取
    // 而一再重設。舊實作每次部分寫出重新計時 → stop 用完 5 s budget 仍未收斂。
    let h = new_h();
    let (server, port) = tcp_server(h.clone());
    let mut c = connect(port);
    c.write_all(b"{\"id\":1,\"method\":\"huge\"}\n").unwrap();
    let done = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let reader = {
        let done = done.clone();
        let mut c2 = c.try_clone().unwrap();
        std::thread::spawn(move || {
            let mut buf = vec![0u8; 16 * 1024];
            while !done.load(Ordering::SeqCst) {
                match c2.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => std::thread::sleep(Duration::from_millis(200)),
                }
            }
        })
    };
    let deadline = Instant::now() + Duration::from_secs(5);
    while h.0.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "handler 未被呼叫");
        std::thread::sleep(Duration::from_millis(10));
    }
    std::thread::sleep(Duration::from_millis(300));
    let t0 = Instant::now();
    let converged = server.stop(Duration::from_secs(5));
    let took = t0.elapsed();
    done.store(true, Ordering::SeqCst);
    drop(c);
    let _ = reader.join();
    assert!(converged, "stop 未收斂（{took:?}）");
    assert!(took < Duration::from_millis(3500), "stop 耗時 {took:?}（總期限應為停止時刻 + 2 s）");
}

#[test]
fn stop_waits_for_in_handler_connection() {
    let h = new_h();
    let (server, port) = tcp_server(h.clone());
    let mut c = connect(port);
    c.write_all(b"{\"id\":1,\"method\":\"slow\"}\n").unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while h.0.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "handler 未被呼叫");
        std::thread::sleep(Duration::from_millis(5));
    }
    let t0 = Instant::now();
    assert!(server.stop(Duration::from_secs(5)));
    assert!(t0.elapsed() >= Duration::from_millis(300), "stop 未等待 handler：{:?}", t0.elapsed());
    assert_eq!(read_line(&mut c).unwrap(), "{\"id\":1,\"ok\":true}\n");
}

/// 記憶體 listener：accept 從佇列取 server 端 MemStream，記錄已 accept 數。
struct MemListener {
    queue: Arc<Mutex<VecDeque<MemStream>>>,
    accepted: Arc<AtomicUsize>,
}
impl RpcListener for MemListener {
    fn accept(&mut self, timeout: Duration) -> PalResult<Option<Box<dyn RpcServerStream>>> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(s) = self.queue.lock().unwrap().pop_front() {
                self.accepted.fetch_add(1, Ordering::SeqCst);
                return Ok(Some(Box::new(s)));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
    fn close(self: Box<Self>) {}
}

#[test]
fn connection_cap_128_pauses_accept() {
    let queue = Arc::new(Mutex::new(VecDeque::new()));
    let accepted = Arc::new(AtomicUsize::new(0));
    let mut clients = Vec::new();
    for _ in 0..129 {
        let (a, b) = MemStream::pair();
        queue.lock().unwrap().push_back(b);
        clients.push(a);
    }
    let listener = MemListener { queue: queue.clone(), accepted: accepted.clone() };
    let server = RpcServer::start(Box::new(listener), new_h());
    let deadline = Instant::now() + Duration::from_secs(5);
    while accepted.load(Ordering::SeqCst) < 128 {
        assert!(Instant::now() < deadline, "前 128 條未被 accept");
        std::thread::sleep(Duration::from_millis(5));
    }
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(accepted.load(Ordering::SeqCst), 128, "第 129 條不得在前 128 條關閉前被 accept");
    assert_eq!(queue.lock().unwrap().len(), 1);
    drop(clients.remove(0)); // 關一條
    let deadline = Instant::now() + Duration::from_secs(5);
    while accepted.load(Ordering::SeqCst) < 129 {
        assert!(Instant::now() < deadline, "關一條後第 129 條未被接受");
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(server.stop(Duration::from_secs(5)));
}

/// parity #73（B:A-103；RECORD spec-reconciliation §7 NC-08，Python 無上限）：真 socket 上第 129 條連線
/// 暫停 accept——不拒絕、不斷線；任一連線關閉後恢復並得到回應。
#[test]
fn rpc_accept_paused_at_128_connections() {
    let h = new_h();
    let (server, port) = tcp_server(h.clone());
    let mut conns = Vec::new();
    for i in 0..128 {
        let mut c = connect(port);
        c.write_all(format!("{{\"id\":{i},\"method\":\"ping\"}}\n").as_bytes()).unwrap();
        assert_eq!(read_line(&mut c).unwrap(), format!("{{\"id\":{i},\"ok\":true}}\n"));
        conns.push(c);
    }
    let mut extra = TcpStream::connect(("127.0.0.1", port)).expect("第 129 條 connect 不得被拒絕");
    extra.write_all(b"{\"id\":129,\"method\":\"ping\"}\n").unwrap();
    extra.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let mut b = [0u8; 1];
    match extra.read(&mut b) {
        Ok(0) => panic!("第 129 條不得被斷線"),
        Ok(_) => panic!("第 129 條不得在上限內被服務"),
        Err(e) => assert!(
            matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut),
            "預期讀逾時，得到 {e:?}"
        ),
    }
    drop(conns.remove(0));
    extra.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    assert_eq!(read_line(&mut extra).unwrap(), "{\"id\":129,\"ok\":true}\n");
    drop(conns);
    drop(extra);
    assert!(server.stop(Duration::from_secs(5)));
}
