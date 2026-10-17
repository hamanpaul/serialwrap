//! 可腳本化的假 RPC 連線。

use crate::traits::{
    kind_from_errno, timed_out, EndpointSpec, PalError, PalErrorKind, PalResult, RpcConnector,
    RpcStream, SockTimeout,
};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Clone, Debug)]
pub enum StreamStep {
    /// 一次 recv 回傳整段。
    Reply(Vec<u8>),
    Chunk(Vec<u8>),
    Eof,
    RecvErr(PalError),
    /// 推進 FakeClock。
    Delay(Duration),
}

#[derive(Clone, Debug)]
pub enum FakeConn {
    Refuse(PalError),
    /// connect 回 kind=TimedOut。
    Timeout,
    Stream(Vec<StreamStep>),
}

impl FakeConn {
    /// kind 依 code 映射（111→ConnectionRefused、110→TimedOut…）。
    pub fn refuse_errno(code: i32, py_message: &str) -> FakeConn {
        let strerror = py_message
            .strip_prefix(&format!("[Errno {code}] "))
            .unwrap_or(py_message)
            .to_string();
        FakeConn::Refuse(PalError::new(
            kind_from_errno(code),
            Some(code),
            None,
            strerror,
            py_message,
        ))
    }
    pub fn stream(steps: Vec<StreamStep>) -> FakeConn {
        FakeConn::Stream(steps)
    }
    /// kind=Io、os_code=Some(code)、py_message 原樣。
    pub fn gai_error(code: i32, py_message: &str) -> PalError {
        let strerror = py_message
            .strip_prefix(&format!("[Errno {code}] "))
            .unwrap_or(py_message);
        PalError::new(PalErrorKind::Io, Some(code), None, strerror, py_message)
    }
}

/// 假連線器會依序消費連線腳本，並記錄輸入、傳送資料與解析要求。
pub struct ScriptedConnector {
    pub script: Mutex<VecDeque<FakeConn>>,
    pub sent: Arc<Mutex<Vec<Vec<u8>>>>,
    pub timeouts: Mutex<Vec<SockTimeout>>,
    pub endpoints: Mutex<Vec<EndpointSpec>>,
    pub clock: Arc<super::clock::FakeClock>,
    pub af_unix: bool,
    resolve_count: AtomicUsize,
    resolve_failures: Mutex<BTreeMap<String, PalError>>,
}

impl ScriptedConnector {
    pub fn new(script: Vec<FakeConn>, clock: Arc<super::clock::FakeClock>, af_unix: bool) -> Self {
        Self {
            script: Mutex::new(script.into()),
            sent: Arc::new(Mutex::new(Vec::new())),
            timeouts: Mutex::new(Vec::new()),
            endpoints: Mutex::new(Vec::new()),
            clock,
            af_unix,
            resolve_count: AtomicUsize::new(0),
            resolve_failures: Mutex::new(BTreeMap::new()),
        }
    }

    pub fn sent(&self) -> Vec<Vec<u8>> {
        self.sent.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
    pub fn connect_count(&self) -> usize {
        self.endpoints
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .len()
    }
    pub fn timeouts_s(&self) -> Vec<f64> {
        self.timeouts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|timeout| match timeout {
                SockTimeout::Blocking(value) => value.as_secs_f64(),
                SockTimeout::NonBlocking => 0.0,
            })
            .collect()
    }
    pub fn resolve_count(&self) -> usize {
        self.resolve_count.load(Ordering::SeqCst)
    }
    pub fn fail_resolve(&self, host: &str, e: PalError) {
        self.resolve_failures
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .insert(host.to_string(), e);
    }
}

impl RpcConnector for ScriptedConnector {
    fn af_unix_available(&self) -> bool {
        self.af_unix
    }

    fn connect(
        &self,
        endpoint: &EndpointSpec,
        timeout: SockTimeout,
    ) -> PalResult<Box<dyn RpcStream>> {
        self.endpoints
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(endpoint.clone());
        self.timeouts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(timeout);
        let connection = self
            .script
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .pop_front()
            .unwrap_or_else(|| {
                FakeConn::Refuse(PalError::plain(
                    PalErrorKind::Unsupported,
                    "scripted connector has no remaining connection",
                ))
            });
        match connection {
            FakeConn::Refuse(error) => Err(error),
            FakeConn::Timeout => Err(timed_out()),
            FakeConn::Stream(steps) => Ok(Box::new(FakeStream {
                steps: steps.into(),
                pending: VecDeque::new(),
                sent: Arc::clone(&self.sent),
                clock: Arc::clone(&self.clock),
            })),
        }
    }

    fn resolve_tcp(&self, host: &str, _port: u16) -> PalResult<()> {
        self.resolve_count.fetch_add(1, Ordering::SeqCst);
        self.resolve_failures
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(host)
            .cloned()
            .map_or(Ok(()), Err)
    }
}

struct FakeStream {
    steps: VecDeque<StreamStep>,
    pending: VecDeque<u8>,
    sent: Arc<Mutex<Vec<Vec<u8>>>>,
    clock: Arc<super::clock::FakeClock>,
}

impl RpcStream for FakeStream {
    fn send_all(&mut self, data: &[u8]) -> PalResult<()> {
        self.sent
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(data.to_vec());
        Ok(())
    }

    fn recv(&mut self, buffer: &mut [u8]) -> PalResult<usize> {
        loop {
            if !self.pending.is_empty() {
                let count = buffer.len().min(self.pending.len());
                for slot in &mut buffer[..count] {
                    *slot = self.pending.pop_front().expect("待讀資料數量已確認");
                }
                return Ok(count);
            }
            match self.steps.pop_front() {
                Some(StreamStep::Reply(bytes) | StreamStep::Chunk(bytes)) => {
                    self.pending.extend(bytes);
                }
                Some(StreamStep::Eof) | None => return Ok(0),
                Some(StreamStep::RecvErr(error)) => return Err(error),
                Some(StreamStep::Delay(delay)) => self.clock.advance(delay),
            }
            if buffer.is_empty() {
                return Ok(0);
            }
        }
    }
}

/// 請求感知的假 RPC 連線（P4-T1 替身）：依 `(endpoint, method)` 回應，連線以 `connect` 次數計。
///
/// endpoint 鍵：`unix://<path>` 或 `tcp://<host>:<port>`。未設定任何回應的 endpoint 一律 `ConnectionRefused`
/// （與計畫「預設所有 endpoint refused」一致）。回應行以 `\n` 結尾；同一 `(endpoint, method)` 的 `respond_seq`
/// 依序回應，用盡後重複最後一筆。
#[derive(Debug, Default)]
pub struct FakeRpcConnector {
    state: Arc<Mutex<RpcState>>,
    af_unix: bool,
}

#[derive(Debug, Default)]
struct RpcState {
    responses: BTreeMap<(String, String), VecDeque<String>>,
    refused: BTreeMap<String, PalErrorKind>,
    refuse_from_connect: BTreeMap<String, usize>,
    refuse_first: BTreeMap<String, usize>,
    spawn_gate: BTreeMap<String, Arc<std::sync::atomic::AtomicUsize>>,
    stop_after: BTreeMap<(String, String), ()>,
    stopped: BTreeSet<String>,
    connects: BTreeMap<String, usize>,
    live: BTreeSet<String>,
    fail_after_send: BTreeSet<(String, String)>,
    recv_timeout: BTreeSet<(String, String)>,
    requests: Vec<(String, String, String)>,
    request_seq: Vec<u64>,
}

/// 把 `EndpointSpec` 轉為鍵字串（`unix://<path>`／`tcp://<host>:<port>`）。
pub fn endpoint_key(ep: &EndpointSpec) -> String {
    match ep {
        EndpointSpec::Unix(p) => canonical_endpoint(&p.to_string_lossy()),
        EndpointSpec::Tcp { host, port } => format!("tcp://{host}:{port}"),
    }
}

/// 測試端 endpoint 字串的正規化：`/path`、`unix:///path` 與 `unix:/path` 皆視為 `unix:///path`。
pub fn canonical_endpoint(raw: &str) -> String {
    if let Some(rest) = raw.strip_prefix("unix://") {
        return format!("unix:///{}", rest.trim_start_matches('/'));
    }
    if let Some(rest) = raw.strip_prefix("unix:") {
        return format!("unix:///{}", rest.trim_start_matches('/'));
    }
    if raw.contains("://") {
        return raw.to_string();
    }
    format!("unix:///{}", raw.trim_start_matches('/'))
}

/// 自 NDJSON 請求行取出 `method` 與 `params`（params 為envelope 之外的 JSON 物件文字）。
fn parse_request(line: &str) -> (String, String) {
    let method = line
        .find("\"method\":\"")
        .map(|i| {
            let rest = &line[i + "\"method\":\"".len()..];
            rest.split('"').next().unwrap_or("").to_string()
        })
        .unwrap_or_default();
    let params = line
        .find("\"params\":")
        .map(|i| {
            let rest = line[i + "\"params\":".len()..].trim_end();
            let rest = rest.strip_suffix('}').unwrap_or(rest);
            rest.to_string()
        })
        .unwrap_or_default();
    (method, params)
}

impl FakeRpcConnector {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Arc::new(Mutex::new(RpcState::default())),
            af_unix: true,
        })
    }

    /// `(ep, method)` 的單一回應行（取代既有設定，endpoint 變為存活）。
    pub fn respond(&self, ep: &str, method: &str, line: &str) {
        self.respond_seq(ep, method, &[line]);
    }

    /// `(ep, method)` 的依序回應行（取代既有設定，endpoint 變為存活）。
    pub fn respond_seq(&self, ep: &str, method: &str, lines: &[&str]) {
        let ep = canonical_endpoint(ep);
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.live.insert(ep.clone());
        st.responses.insert(
            (ep, method.to_string()),
            lines.iter().map(|s| s.to_string()).collect(),
        );
    }

    /// 之後對 `ep` 的所有連線以 `kind` 拒絕。
    pub fn refuse_with(&self, ep: &str, kind: PalErrorKind) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refused
            .insert(canonical_endpoint(ep), kind);
    }

    /// `ep` 目前為止的連線次數（含被拒絕者）。與 `refuse_from_connect` 同一計數。
    pub fn connect_count(&self, ep: &str) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .connects
            .get(&canonical_endpoint(ep))
            .copied()
            .unwrap_or(0)
    }

    /// `ep` 的前 `n` 次連線以 `ConnectionRefused` 拒絕，之後的連線照常（「refused 後 ok」）。
    pub fn refuse_first(&self, ep: &str, n: usize) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refuse_first
            .insert(canonical_endpoint(ep), n);
    }

    /// `ep` 在計數器為 0 前一律 `ConnectionRefused`（「第一次 spawn 之後才回 ok」，計數器由 `FakeProcessCtl::spawn_counter` 提供）。
    pub fn gate_on_spawn(&self, ep: &str, counter: Arc<std::sync::atomic::AtomicUsize>) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .spawn_gate
            .insert(canonical_endpoint(ep), counter);
    }

    /// `(ep, method)` 送出後，`ep` 進入停止狀態（之後連線一律 refused，`is_stopped` 為真）。
    pub fn stop_after_send(&self, ep: &str, method: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .stop_after
            .insert((canonical_endpoint(ep), method.to_string()), ());
    }

    /// `ep` 是否已因 `stop_after_send` 進入停止狀態（不推進任何計數）。
    pub fn is_stopped(&self, ep: &str) -> bool {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .stopped
            .contains(&canonical_endpoint(ep))
    }

    /// `ep` 的第 `n` 次連線（1 起算，含）起以 `ConnectionRefused` 拒絕。
    pub fn refuse_from_connect(&self, ep: &str, n: usize) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refuse_from_connect
            .insert(canonical_endpoint(ep), n);
    }

    /// `(ep, method)` 送出後連線即結束（recv 得 EOF，對應 EMPTY_RESPONSE）。
    pub fn fail_after_send(&self, ep: &str, method: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .fail_after_send
            .insert((canonical_endpoint(ep), method.to_string()));
    }

    /// `(ep, method)` 的 recv 逾時。
    pub fn recv_timeout(&self, ep: &str, method: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .recv_timeout
            .insert((canonical_endpoint(ep), method.to_string()));
    }

    /// 依送出順序回傳 `(endpoint, method, params JSON)`。
    pub fn requests(&self) -> Vec<(String, String, String)> {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .requests
            .clone()
    }

    /// 同 `requests`，回傳 `(事件序號, method)`（事件合併用）。
    pub fn request_events(&self) -> Vec<(u64, String)> {
        let st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.request_seq
            .iter()
            .copied()
            .zip(st.requests.iter().map(|(_, method, _)| method.clone()))
            .collect()
    }
}

struct FakeRpcStream {
    state: Arc<Mutex<RpcState>>,
    key: String,
    reply: Option<Vec<u8>>,
    last_method: Option<String>,
    inbuf: VecDeque<u8>,
}

impl RpcConnector for FakeRpcConnector {
    fn af_unix_available(&self) -> bool {
        self.af_unix
    }

    fn connect(&self, endpoint: &EndpointSpec, _timeout: SockTimeout) -> PalResult<Box<dyn RpcStream>> {
        let key = endpoint_key(endpoint);
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let count = {
            let c = st.connects.entry(key.clone()).or_insert(0);
            *c += 1;
            *c
        };
        if let Some(kind) = st.refused.get(&key).copied() {
            return Err(PalError::plain(kind, "fake endpoint refused"));
        }
        if st.refuse_from_connect.get(&key).is_some_and(|n| count >= *n) {
            return Err(PalError::plain(PalErrorKind::ConnectionRefused, "fake endpoint gone"));
        }
        if st.refuse_first.get(&key).is_some_and(|n| count <= *n) {
            return Err(PalError::plain(PalErrorKind::ConnectionRefused, "fake endpoint refused (first connects)"));
        }
        if st.stopped.contains(&key) {
            return Err(PalError::plain(PalErrorKind::ConnectionRefused, "fake endpoint stopped"));
        }
        if st
            .spawn_gate
            .get(&key)
            .is_some_and(|c| c.load(std::sync::atomic::Ordering::SeqCst) == 0)
        {
            return Err(PalError::plain(PalErrorKind::ConnectionRefused, "fake endpoint not spawned yet"));
        }
        if !st.live.contains(&key) {
            return Err(PalError::plain(PalErrorKind::ConnectionRefused, "fake endpoint not configured"));
        }
        drop(st);
        Ok(Box::new(FakeRpcStream {
            state: Arc::clone(&self.state),
            key,
            reply: None,
            last_method: None,
            inbuf: VecDeque::new(),
        }))
    }

    fn resolve_tcp(&self, _host: &str, _port: u16) -> PalResult<()> {
        Ok(())
    }
}

impl RpcStream for FakeRpcStream {
    fn send_all(&mut self, data: &[u8]) -> PalResult<()> {
        let text = String::from_utf8_lossy(data).into_owned();
        for line in text.lines().filter(|l| !l.trim().is_empty()) {
            let (method, params) = parse_request(line);
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            st.requests
                .push((self.key.clone(), method.clone(), params));
            st.request_seq.push(super::next_event_seq());
            let reply = {
                let slot = (self.key.clone(), method.clone());
                match st.responses.get_mut(&slot) {
                    Some(queue) if queue.len() > 1 => queue.pop_front(),
                    Some(queue) => queue.front().cloned(),
                    None => None,
                }
            };
            self.reply = reply.map(|r| format!("{r}\n").into_bytes());
            if st.stop_after.contains_key(&(self.key.clone(), method.clone())) {
                st.stopped.insert(self.key.clone());
            }
            self.last_method = Some(method);
        }
        Ok(())
    }

    fn recv(&mut self, buf: &mut [u8]) -> PalResult<usize> {
        if let Some(method) = &self.last_method {
            let st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let slot = (self.key.clone(), method.clone());
            if st.recv_timeout.contains(&slot) {
                return Err(crate::traits::timed_out());
            }
            if st.fail_after_send.contains(&slot) {
                return Ok(0);
            }
        }
        if self.inbuf.is_empty() {
            if let Some(bytes) = self.reply.take() {
                self.inbuf.extend(bytes);
            }
        }
        if self.inbuf.is_empty() {
            return Ok(0);
        }
        let n = buf.len().min(self.inbuf.len());
        for slot in &mut buf[..n] {
            *slot = self.inbuf.pop_front().unwrap_or(0);
        }
        Ok(n)
    }
}
