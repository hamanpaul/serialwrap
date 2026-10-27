//! RPC 連線 framing 與 acceptor／連線 thread（arch §6.2，對應 Python `serve_connection`；P5-T17）。
//!
//! 行為逐項對齊 `sw_core/rpc_posix.py::serve_connection`：
//! - 逐行讀取（`asyncio.StreamReader.readline`，limit 65536）：`\n` 之前的內容超過 [`MAX_LINE_BYTES`] →
//!   不回應並關閉連線（含同連線後續請求）；EOF 前的殘行（無 `\n`）仍視為一行處理。
//! - `bytes.strip()` 後為空略過；`decode("utf-8", errors="replace")` → `json.loads`；
//!   `JSONDecodeError` → `RPC_EARLY` 格式的 `INVALID_JSON`；非 dict → `INVALID_REQUEST`；
//!   深度超限（Python `RecursionError`）→ 例外逸出、不回應並關閉。
//! - `method` 非 str → `INVALID_METHOD`；`params` 非 dict → `{}`；handler 例外／panic → `EXCEPTION`。
//! - 回應 `{"id": id}` 依序併入 result（`RPC_WIRE` 格式）；`daemon.stop` 先寫回應再 `request_stop()`
//!   （Python `_rpc_after_write` 位於 `finally`：寫失敗也會呼叫）。
//! - 同連線依序處理（允許 pipelining）。

use std::io::{ErrorKind, Read};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use sw_pal::traits::{RpcListener, RpcServerStream};
use sw_proto::pycompat::{decode_utf8_replace, PyErr};
use sw_proto::pyjson::{self, JsonError, PyDict, PyValue, RPC_EARLY, RPC_WIRE};

/// 分派介面（由 `service.rs` 的 `ServiceHandler` 實作）。
pub trait RpcHandler: Send + Sync {
    fn call(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr>;
    fn request_stop(&self);
}

/// 請求行上限（內容不含 `\n`；超過斷線不回應，RPC-H-02；`contracts/golden/rpc/line_limit.json` 的 `max_ok_bytes`）。
pub const MAX_LINE_BYTES: usize = 65_536;

/// 連線數上限：達上限時 acceptor 暫停 accept（不拒絕、不斷線；NC-08，Python 無上限）。
pub const MAX_CONNECTIONS: usize = 128;

/// stop 時用來喚醒讀迴圈的讀取逾時（Produces ②）。
const STOP_POLL: Duration = Duration::from_millis(100);

/// acceptor 每次 accept 的有界等待。
const ACCEPT_POLL: Duration = Duration::from_millis(50);

/// 連線停止旗標與「本連線是否正在 handler 內」（stop 時的分級關閉依據）。
pub struct ConnStop {
    stopping: AtomicBool,
    in_handler: AtomicBool,
}

impl ConnStop {
    /// 建立停止旗標（測試與 `RpcServer` 共用同一個實例）。
    pub fn new() -> ConnStop {
        ConnStop {
            stopping: AtomicBool::new(false),
            in_handler: AtomicBool::new(false),
        }
    }

    /// 要求本連線結束：閒置／半行時讀迴圈醒來即結束；handler 內的請求寫完回應後結束。
    pub fn request(&self) {
        self.stopping.store(true, Ordering::SeqCst);
    }

    /// 是否已要求停止。
    pub fn is_stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    /// 本連線目前是否正在處理一筆請求（handler 呼叫到回應寫出為止）。
    pub fn in_handler(&self) -> bool {
        self.in_handler.load(Ordering::SeqCst)
    }
}

impl Default for ConnStop {
    fn default() -> Self {
        ConnStop::new()
    }
}

/// 單行處理結果。
enum LineOutcome {
    /// 繼續讀下一行。
    Continue,
    /// 關閉連線（寫失敗或 Python 例外逸出）。
    Close,
}

/// Python `bytes.strip()`（`b" \t\n\r\x0b\x0c"`）。
fn trim_py_bytes(b: &[u8]) -> &[u8] {
    let is_ws = |c: &u8| matches!(*c, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c);
    let start = b.iter().position(|c| !is_ws(c)).unwrap_or(b.len());
    let end = b.iter().rposition(|c| !is_ws(c)).map_or(start, |i| i + 1);
    &b[start..end]
}

fn early_error(code: &str) -> Vec<u8> {
    let mut d = PyDict::new();
    d.insert("ok", PyValue::Bool(false));
    d.insert("error_code", PyValue::from(code));
    let mut out = pyjson::dumps_dict(&d, &RPC_EARLY).into_bytes();
    out.push(b'\n');
    out
}

fn wire(d: &PyDict) -> Vec<u8> {
    let mut out = pyjson::dumps_dict(d, &RPC_WIRE).into_bytes();
    out.push(b'\n');
    out
}

fn write_line(stream: &mut dyn RpcServerStream, bytes: &[u8]) -> bool {
    stream.write_all(bytes).is_ok() && stream.flush().is_ok()
}

/// panic payload → Python `str(exc)` 對應字串。
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "panic".to_string()
    }
}

fn exception_result(message: String) -> PyDict {
    let mut d = PyDict::new();
    d.insert("ok", PyValue::Bool(false));
    d.insert("error_code", PyValue::from("EXCEPTION"));
    d.insert("message", PyValue::Str(message));
    d
}

fn handle_line(
    raw: &[u8],
    stream: &mut dyn RpcServerStream,
    handler: &dyn RpcHandler,
) -> LineOutcome {
    let line = trim_py_bytes(raw);
    if line.is_empty() {
        return LineOutcome::Continue;
    }
    let text = decode_utf8_replace(line);
    let obj = match pyjson::loads(&text) {
        Ok(v) => v,
        Err(JsonError::Decode(_)) => {
            return if write_line(stream, &early_error("INVALID_JSON")) {
                LineOutcome::Continue
            } else {
                LineOutcome::Close
            };
        }
        // Python `RecursionError` 不是 `JSONDecodeError`：例外逸出 serve_connection → 不回應並關閉。
        Err(JsonError::Recursion) => return LineOutcome::Close,
    };
    let PyValue::Dict(obj) = obj else {
        return if write_line(stream, &early_error("INVALID_REQUEST")) {
            LineOutcome::Continue
        } else {
            LineOutcome::Close
        };
    };
    let req_id = obj.get("id").cloned().unwrap_or(PyValue::Null);
    let mut resp = PyDict::new();
    resp.insert("id", req_id);
    let method = match obj.get("method") {
        Some(PyValue::Str(m)) => m.clone(),
        _ => {
            resp.insert("ok", PyValue::Bool(false));
            resp.insert("error_code", PyValue::from("INVALID_METHOD"));
            return if write_line(stream, &wire(&resp)) {
                LineOutcome::Continue
            } else {
                LineOutcome::Close
            };
        }
    };
    if method == "daemon.stop" {
        // Python：handler 回 `{"ok":True,"stopping":True,"_rpc_after_write":stop}`；寫回應後於 finally 呼叫 stop。
        resp.insert("ok", PyValue::Bool(true));
        resp.insert("stopping", PyValue::Bool(true));
        let written = write_line(stream, &wire(&resp));
        handler.request_stop();
        return if written {
            LineOutcome::Continue
        } else {
            LineOutcome::Close
        };
    }
    let params = match obj.get("params") {
        Some(PyValue::Dict(p)) => p.clone(),
        _ => PyDict::new(),
    };
    let result = match catch_unwind(AssertUnwindSafe(|| handler.call(&method, &params))) {
        Ok(Ok(d)) => d,
        Ok(Err(e)) => exception_result(e.message),
        Err(payload) => exception_result(panic_message(payload.as_ref())),
    };
    resp.update(&result);
    if write_line(stream, &wire(&resp)) {
        LineOutcome::Continue
    } else {
        LineOutcome::Close
    }
}

/// 處理單一連線：逐 byte 累積到 `\n`（arch §6.2 九步）。
pub fn serve_connection(mut stream: Box<dyn RpcServerStream>, handler: &dyn RpcHandler, stop: &ConnStop) {
    let mut pending: Vec<u8> = Vec::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        // 先處理緩衝區內已完整的行（pipelining）。
        while let Some(pos) = pending.iter().position(|&b| b == b'\n') {
            if pos > MAX_LINE_BYTES {
                return;
            }
            if stop.is_stopping() {
                return;
            }
            let line: Vec<u8> = pending.drain(..=pos).collect();
            stop.in_handler.store(true, Ordering::SeqCst);
            let outcome = handle_line(&line[..pos], stream.as_mut(), handler);
            stop.in_handler.store(false, Ordering::SeqCst);
            if matches!(outcome, LineOutcome::Close) {
                return;
            }
        }
        // asyncio readuntil：尚無分隔符且緩衝超過 limit → LimitOverrunError → 關閉。
        if pending.len() > MAX_LINE_BYTES {
            return;
        }
        if stop.is_stopping() {
            return;
        }
        match stream.read(&mut buf) {
            Ok(0) => {
                // EOF：殘行（無 `\n`）仍處理一次（asyncio readline 於 EOF 回傳部分資料）。
                if !pending.is_empty() {
                    let line = std::mem::take(&mut pending);
                    stop.in_handler.store(true, Ordering::SeqCst);
                    let _ = handle_line(&line, stream.as_mut(), handler);
                    stop.in_handler.store(false, Ordering::SeqCst);
                }
                return;
            }
            Ok(n) => pending.extend_from_slice(&buf[..n]),
            Err(e) if matches!(e.kind(), ErrorKind::Interrupted | ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                continue;
            }
            Err(_) => return,
        }
    }
}

struct ConnEntry {
    handle: JoinHandle<()>,
    stop: Arc<ConnStop>,
}

/// RPC acceptor（連線上限 128；每連線 thread 與停止旗標）。
pub struct RpcServer {
    stopping: Arc<AtomicBool>,
    acceptor: Option<JoinHandle<()>>,
    conns: Arc<Mutex<Vec<ConnEntry>>>,
}

fn reap(conns: &Mutex<Vec<ConnEntry>>) {
    let mut list = conns.lock().unwrap_or_else(|e| e.into_inner());
    let mut i = 0;
    while i < list.len() {
        if list[i].handle.is_finished() {
            let entry = list.swap_remove(i);
            let _ = entry.handle.join();
        } else {
            i += 1;
        }
    }
}

impl RpcServer {
    pub fn start(listener: Box<dyn RpcListener>, handler: Arc<dyn RpcHandler>) -> RpcServer {
        let stopping = Arc::new(AtomicBool::new(false));
        let conns: Arc<Mutex<Vec<ConnEntry>>> = Arc::new(Mutex::new(Vec::new()));
        let active = Arc::new(AtomicUsize::new(0));
        let acceptor = {
            let stopping = Arc::clone(&stopping);
            let conns = Arc::clone(&conns);
            std::thread::Builder::new()
                .name("rpc-acceptor".into())
                .spawn(move || {
                    let mut listener = listener;
                    while !stopping.load(Ordering::SeqCst) {
                        reap(&conns);
                        if active.load(Ordering::SeqCst) >= MAX_CONNECTIONS {
                            // NC-08：達上限暫停 accept（不拒絕、不斷線），任一連線結束後恢復。
                            std::thread::sleep(Duration::from_millis(10));
                            continue;
                        }
                        let mut stream = match listener.accept(ACCEPT_POLL) {
                            Ok(Some(s)) => s,
                            Ok(None) => continue,
                            Err(_) => {
                                std::thread::sleep(ACCEPT_POLL);
                                continue;
                            }
                        };
                        // 讀逾時讓讀迴圈定期醒來檢查停止旗標（Produces ②）。
                        let _ = stream.set_read_timeout(Some(STOP_POLL));
                        let stop = Arc::new(ConnStop::new());
                        let handler = Arc::clone(&handler);
                        let thread_stop = Arc::clone(&stop);
                        let thread_active = Arc::clone(&active);
                        active.fetch_add(1, Ordering::SeqCst);
                        let spawned = std::thread::Builder::new()
                            .name("rpc-conn".into())
                            .spawn(move || {
                                struct Release(Arc<AtomicUsize>);
                                impl Drop for Release {
                                    fn drop(&mut self) {
                                        self.0.fetch_sub(1, Ordering::SeqCst);
                                    }
                                }
                                let _release = Release(thread_active);
                                serve_connection(stream, &*handler, &thread_stop);
                            });
                        match spawned {
                            Ok(handle) => conns
                                .lock()
                                .unwrap_or_else(|e| e.into_inner())
                                .push(ConnEntry { handle, stop }),
                            Err(_) => {
                                active.fetch_sub(1, Ordering::SeqCst);
                            }
                        }
                    }
                    // ① 停 accept 並 close listener。
                    listener.close();
                })
                .expect("spawn rpc acceptor thread")
        };
        RpcServer {
            stopping,
            acceptor: Some(acceptor),
            conns,
        }
    }

    /// 回傳 handler 是否全部收斂（arch §6.2 停止流程 ①～④）。
    ///
    /// ① 停 accept 並 close listener；② 對全部連線設停止旗標（讀逾時 100 ms 讓閒置／半行的讀迴圈醒來即結束）；
    /// ③ handler 內的連線把該筆回應寫完後結束（寫逾時 2 s 由 PAL 設定，逾時放棄）；④ 在 `budget` 內 join 全部連線 thread。
    /// 逾 `budget` 仍未結束的 thread 不再等待（detach），回傳 `false`。
    pub fn stop(mut self, budget: Duration) -> bool {
        let deadline = Instant::now() + budget;
        self.stopping.store(true, Ordering::SeqCst);
        if let Some(acceptor) = self.acceptor.take() {
            let _ = acceptor.join();
        }
        let entries: Vec<ConnEntry> = std::mem::take(&mut *self.conns.lock().unwrap_or_else(|e| e.into_inner()));
        for entry in &entries {
            entry.stop.request();
        }
        let mut remaining = entries;
        loop {
            let mut still = Vec::new();
            for entry in remaining {
                if entry.handle.is_finished() {
                    let _ = entry.handle.join();
                } else {
                    still.push(entry);
                }
            }
            remaining = still;
            if remaining.is_empty() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

impl Drop for RpcServer {
    fn drop(&mut self) {
        // 未呼叫 stop 就被 drop：至少停掉 acceptor 並關閉 listener，避免 thread 外洩持有 socket。
        self.stopping.store(true, Ordering::SeqCst);
        if let Some(acceptor) = self.acceptor.take() {
            let _ = acceptor.join();
        }
        for entry in self.conns.lock().unwrap_or_else(|e| e.into_inner()).iter() {
            entry.stop.request();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::trim_py_bytes;

    #[test]
    fn trim_matches_python_strip() {
        assert_eq!(trim_py_bytes(b" \t\x0b\x0cab c\r\n"), b"ab c");
        assert_eq!(trim_py_bytes(b"\r\n \t"), b"");
        assert_eq!(trim_py_bytes(b""), b"");
    }
}
