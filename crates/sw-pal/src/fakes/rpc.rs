//! 可腳本化的假 RPC 連線。

use crate::traits::{
    kind_from_errno, timed_out, Clock, EndpointSpec, PalError, PalErrorKind, PalResult,
    RpcConnector, RpcStream, SockTimeout,
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
    post_relaunch_responses: BTreeMap<(String, String), VecDeque<String>>,
    refused: BTreeMap<String, PalErrorKind>,
    refuse_from_connect: BTreeMap<String, usize>,
    refused_connect_labels: BTreeMap<String, String>,
    refused_connect_events: Vec<(u64, String)>,
    refuse_first: BTreeMap<String, usize>,
    spawn_gate: BTreeMap<String, Arc<std::sync::atomic::AtomicUsize>>,
    spawn_refusal: BTreeMap<String, Arc<std::sync::atomic::AtomicUsize>>,
    stop_after: BTreeMap<(String, String), ()>,
    stop_after_nth: BTreeMap<(String, String), usize>,
    method_calls: BTreeMap<(String, String), usize>,
    stopped: BTreeSet<String>,
    relaunch_after: BTreeMap<(String, String), VecDeque<u32>>,
    relaunched_pid: BTreeMap<String, u32>,
    revive_after_spawn: BTreeMap<String, (Arc<std::sync::atomic::AtomicUsize>, u32, usize)>,
    delayed_responses: BTreeMap<(String, String), (Arc<super::clock::FakeClock>, u64, String)>,
    connects: BTreeMap<String, usize>,
    connect_timeouts_s: Vec<f64>,
    supervisor_hold: Option<Arc<std::sync::atomic::AtomicBool>>,
    ignore_supervisor_hold: bool,
    live: BTreeSet<String>,
    fail_after_send: BTreeSet<(String, String)>,
    recv_timeout: BTreeSet<(String, String)>,
    recv_timeout_once_after: BTreeMap<(String, String), usize>,
    requests: Vec<(String, String, String)>,
    request_seq: Vec<u64>,
    relaunch_templates: BTreeMap<(String, String), (BTreeSet<u32>, String)>,
}

fn record_refused_connect(st: &mut RpcState, key: &str) {
    if let Some(method) = st.refused_connect_labels.remove(key) {
        st.refused_connect_events
            .push((super::next_event_seq(), method));
    }
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

    /// 讀取目前設定的第一筆回應，供情境替身在切換後保留原 daemon 語意。
    pub fn configured_response(&self, ep: &str, method: &str) -> Option<String> {
        let key = (canonical_endpoint(ep), method.to_string());
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .responses
            .get(&key)
            .and_then(|queue| queue.front().cloned())
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

    /// 設定 endpoint 重新拉起後的回應，不覆蓋目前 daemon 的回應。
    pub fn respond_after_relaunch(&self, ep: &str, method: &str, line: &str) {
        let key = (canonical_endpoint(ep), method.to_string());
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.live.insert(key.0.clone());
        let response = line.to_string();
        if let Some(queue) = state.post_relaunch_responses.get_mut(&key) {
            if queue.len() > 1 {
                if let Some(first) = queue.front_mut() {
                    *first = response;
                }
                return;
            }
        }
        state
            .post_relaunch_responses
            .insert(key, VecDeque::from([response]));
    }

    /// 依序設定重新拉起後的 RPC 回應；用盡後重複最後一筆。
    pub fn respond_after_relaunch_seq(&self, ep: &str, method: &str, lines: &[&str]) {
        let key = (canonical_endpoint(ep), method.to_string());
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.live.insert(key.0.clone());
        state
            .post_relaunch_responses
            .insert(key, lines.iter().map(|line| line.to_string()).collect());
    }

    /// 之後對 `ep` 的所有連線以 `kind` 拒絕。
    pub fn refuse_with(&self, ep: &str, kind: PalErrorKind) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refused
            .insert(canonical_endpoint(ep), kind);
    }

    /// 清除 endpoint 的固定拒絕狀態，供「先無法連線、稍後新 daemon 出現」替身使用。
    pub fn clear_refusal(&self, ep: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refused
            .remove(&canonical_endpoint(ep));
    }

    /// 將已停止的 endpoint 重新標記為存活，用於模擬監管器在中途重新拉起 daemon。
    pub fn revive_endpoint(&self, ep: &str) {
        let ep = canonical_endpoint(ep);
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.stopped.remove(&ep);
        state.refused.remove(&ep);
        state.live.insert(ep);
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

    /// 記錄 RPC connect 收到的逾時值，供固定 timeout 契約測試斷言。
    pub fn timeouts_s(&self) -> Vec<f64> {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .connect_timeouts_s
            .clone()
    }

    /// 接上假監管 hold 狀態，讓 restart-on-failure 模型只在 hold 失效時重拉。
    pub fn set_supervisor_hold_signal(&self, held: Arc<std::sync::atomic::AtomicBool>) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .supervisor_hold = Some(held);
    }

    /// 模擬監管服務忽略 hold（R5-F1 的反例測試）。
    pub fn set_ignore_supervisor_hold(&self, ignore: bool) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .ignore_supervisor_hold = ignore;
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

    /// spawn 前允許舊 daemon 回應，spawn 後模擬新 daemon RPC 無法連線。
    pub fn refuse_after_spawn(&self, ep: &str, counter: Arc<std::sync::atomic::AtomicUsize>) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .spawn_refusal
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

    /// 清除 `(ep, method)` 的一般送出即停止設定。
    pub fn clear_stop_after_send(&self, ep: &str, method: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .stop_after
            .remove(&(canonical_endpoint(ep), method.to_string()));
    }

    /// 第 `n` 次成功送出 `(ep, method)` 後才停止 endpoint。
    pub fn stop_after_nth_send(&self, ep: &str, method: &str, n: usize) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .stop_after_nth
            .insert((canonical_endpoint(ep), method.to_string()), n);
    }

    /// `(ep, method)` 停止後，依序由 supervisor 以指定 PID 重新拉起 daemon。
    pub fn relaunch_after_send(&self, ep: &str, method: &str, pids: &[u32]) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .relaunch_after
            .insert(
                (canonical_endpoint(ep), method.to_string()),
                pids.iter().copied().collect(),
            );
    }

    /// `ep` 停止後，於每次後續成功 spawn 時恢復連線並以新 PID 回報。
    pub fn revive_after_spawn(
        &self,
        ep: &str,
        counter: Arc<std::sync::atomic::AtomicUsize>,
        pid: u32,
    ) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let observed = counter.load(std::sync::atomic::Ordering::SeqCst);
        state
            .revive_after_spawn
            .insert(canonical_endpoint(ep), (counter, pid, observed));
    }

    /// 從 FakeClock 指定時間起，`(ep, method)` 回傳不同的 response line。
    pub fn respond_at(
        &self,
        ep: &str,
        method: &str,
        clock: Arc<super::clock::FakeClock>,
        at: Duration,
        line: &str,
    ) {
        let key = (canonical_endpoint(ep), method.to_string());
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.live.insert(key.0.clone());
        state.delayed_responses.insert(
            key,
            (
                clock,
                at.as_nanos().min(u64::MAX as u128) as u64,
                line.to_string(),
            ),
        );
    }

    /// 監管或其他行程重新拉起 `pids` 中的 daemon 時，`(ep, method)` 以 `template`（`{pid}` 代換為目前 PID）回應，
    /// 優先於 `respond_after_relaunch`（OD-48：重拉的 0.3.x daemon 以自身 PID 回應）。
    pub fn respond_relaunch_template(&self, ep: &str, method: &str, pids: &[u32], template: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .relaunch_templates
            .insert(
                (canonical_endpoint(ep), method.to_string()),
                (pids.iter().copied().collect(), template.to_string()),
            );
    }

    /// `(ep, method)` 已收到的請求數（不推進任何計數）。
    pub fn request_count(&self, ep: &str, method: &str) -> usize {
        let key = canonical_endpoint(ep);
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .requests
            .iter()
            .filter(|request| request.0 == key && request.1 == method)
            .count()
    }

    /// `ep` 是否已因 `stop_after_send` 進入停止狀態（不推進任何計數）。
    pub fn is_stopped(&self, ep: &str) -> bool {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .stopped
            .contains(&canonical_endpoint(ep))
    }

    /// 目前由監管替身重新拉起的 daemon PID（供 fake ProcScan 同步行程快照）。
    pub fn relaunched_pid(&self, ep: &str) -> Option<u32> {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .relaunched_pid
            .get(&canonical_endpoint(ep))
            .copied()
    }

    /// 模擬 service manager 強制終止 endpoint 對應的 daemon。
    pub fn stop_endpoint(&self, ep: &str) {
        let ep = canonical_endpoint(ep);
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.stopped.insert(ep.clone());
        state.relaunched_pid.remove(&ep);
    }

    /// `ep` 的第 `n` 次連線（1 起算，含）起以 `ConnectionRefused` 拒絕。
    pub fn refuse_from_connect(&self, ep: &str, n: usize) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refuse_from_connect
            .insert(canonical_endpoint(ep), n);
    }

    /// 將 endpoint 下一次失敗的連線標記為指定 RPC 的 gone 事件（測試事件序用）。
    pub fn refused_connect_as(&self, ep: &str, method: &str) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refused_connect_labels
            .insert(canonical_endpoint(ep), method.to_string());
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

    /// 第 `successful_calls + 1` 次 `(ep, method)` recv 單次逾時；之後請求恢復使用已配置回應。
    pub fn recv_timeout_once_after(&self, ep: &str, method: &str, successful_calls: usize) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .recv_timeout_once_after
            .insert(
                (canonical_endpoint(ep), method.to_string()),
                successful_calls,
            );
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

    /// 同 `request_events`，僅回傳明確標記的 refused RPC gone 事件。
    pub fn refused_connect_events(&self) -> Vec<(u64, String)> {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .refused_connect_events
            .clone()
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

    fn connect(
        &self,
        endpoint: &EndpointSpec,
        timeout: SockTimeout,
    ) -> PalResult<Box<dyn RpcStream>> {
        let key = endpoint_key(endpoint);
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.connect_timeouts_s.push(match timeout {
            SockTimeout::Blocking(value) => value.as_secs_f64(),
            SockTimeout::NonBlocking => 0.0,
        });
        let count = {
            let c = st.connects.entry(key.clone()).or_insert(0);
            *c += 1;
            *c
        };
        if let Some((counter, pid, observed)) = st.revive_after_spawn.get(&key).cloned() {
            let spawned = counter.load(std::sync::atomic::Ordering::SeqCst);
            if spawned > observed {
                st.stopped.remove(&key);
                st.relaunched_pid.insert(key.clone(), pid);
                if let Some((_, _, last_observed)) = st.revive_after_spawn.get_mut(&key) {
                    *last_observed = spawned;
                }
            }
        }
        if let Some(kind) = st.refused.get(&key).copied() {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(kind, "fake endpoint refused"));
        }
        if st
            .spawn_refusal
            .get(&key)
            .is_some_and(|counter| counter.load(std::sync::atomic::Ordering::SeqCst) > 0)
        {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(
                PalErrorKind::ConnectionRefused,
                "fake endpoint refused after spawn",
            ));
        }
        if st
            .refuse_from_connect
            .get(&key)
            .is_some_and(|n| count >= *n)
        {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(
                PalErrorKind::ConnectionRefused,
                "fake endpoint gone",
            ));
        }
        if st.refuse_first.get(&key).is_some_and(|n| count <= *n) {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(
                PalErrorKind::ConnectionRefused,
                "fake endpoint refused (first connects)",
            ));
        }
        if st.stopped.contains(&key) {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(
                PalErrorKind::ConnectionRefused,
                "fake endpoint stopped",
            ));
        }
        if st
            .spawn_gate
            .get(&key)
            .is_some_and(|c| c.load(std::sync::atomic::Ordering::SeqCst) == 0)
        {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(
                PalErrorKind::ConnectionRefused,
                "fake endpoint not spawned yet",
            ));
        }
        if !st.live.contains(&key) {
            record_refused_connect(&mut st, &key);
            return Err(PalError::plain(
                PalErrorKind::ConnectionRefused,
                "fake endpoint not configured",
            ));
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
            st.requests.push((self.key.clone(), method.clone(), params));
            st.request_seq.push(super::next_event_seq());
            let slot = (self.key.clone(), method.clone());
            let method_call = {
                let count = st.method_calls.entry(slot.clone()).or_insert(0);
                *count += 1;
                *count
            };
            let reply = {
                let dynamic_pid = st.relaunched_pid.get(&self.key).copied();
                let templated = dynamic_pid.and_then(|pid| {
                    st.relaunch_templates
                        .get(&slot)
                        .filter(|(pids, _)| pids.contains(&pid))
                        .map(|(_, template)| template.replace("{pid}", &pid.to_string()))
                });
                if templated.is_some() {
                    templated
                } else if let Some(pid) = dynamic_pid.filter(|_| method == "health.ping") {
                    Some(format!(r#"{{"ok":true,"pid":{pid},"version":"test"}}"#))
                } else if dynamic_pid.is_some() {
                    st.post_relaunch_responses
                        .get_mut(&slot)
                        .and_then(|queue| {
                            if queue.len() > 1 {
                                queue.pop_front()
                            } else {
                                queue.front().cloned()
                            }
                        })
                        .or_else(|| {
                            st.responses.get_mut(&slot).and_then(|queue| {
                                if queue.len() > 1 {
                                    queue.pop_front()
                                } else {
                                    queue.front().cloned()
                                }
                            })
                        })
                        .or_else(|| {
                            Some(format!(
                                r#"{{"ok":true,"pid":{},"version":"test"}}"#,
                                dynamic_pid.unwrap_or_default()
                            ))
                        })
                } else if let Some((clock, at, response)) = st.delayed_responses.get(&slot) {
                    if clock.mono_ns() >= *at {
                        Some(response.clone())
                    } else {
                        st.responses.get_mut(&slot).and_then(|queue| {
                            if queue.len() > 1 {
                                queue.pop_front()
                            } else {
                                queue.front().cloned()
                            }
                        })
                    }
                } else {
                    st.responses.get_mut(&slot).and_then(|queue| {
                        if queue.len() > 1 {
                            queue.pop_front()
                        } else {
                            queue.front().cloned()
                        }
                    })
                }
            };
            let accepted = reply
                .as_deref()
                .is_none_or(|line| !line.contains("\"ok\":false"));
            self.reply = reply.map(|r| format!("{r}\n").into_bytes());
            if accepted
                && (st.stop_after.contains_key(&slot)
                    || st
                        .stop_after_nth
                        .get(&slot)
                        .is_some_and(|n| *n == method_call))
            {
                let held = st
                    .supervisor_hold
                    .as_ref()
                    .is_some_and(|signal| signal.load(std::sync::atomic::Ordering::SeqCst));
                let suppress_relaunch = held && !st.ignore_supervisor_hold;
                let pid = if suppress_relaunch {
                    None
                } else {
                    st.relaunch_after
                        .get_mut(&slot)
                        .and_then(VecDeque::pop_front)
                };
                if let Some(pid) = pid {
                    st.stopped.remove(&self.key);
                    st.relaunched_pid.insert(self.key.clone(), pid);
                } else {
                    st.stopped.insert(self.key.clone());
                    st.relaunched_pid.remove(&self.key);
                }
            }
            self.last_method = Some(method);
        }
        Ok(())
    }

    fn recv(&mut self, buf: &mut [u8]) -> PalResult<usize> {
        if let Some(method) = &self.last_method {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let slot = (self.key.clone(), method.clone());
            if st.recv_timeout.contains(&slot) {
                return Err(crate::traits::timed_out());
            }
            let call_count = st
                .requests
                .iter()
                .filter(|request| request.0 == slot.0 && request.1 == slot.1)
                .count();
            if st
                .recv_timeout_once_after
                .get(&slot)
                .is_some_and(|successful_calls| call_count > *successful_calls)
            {
                st.recv_timeout_once_after.remove(&slot);
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
