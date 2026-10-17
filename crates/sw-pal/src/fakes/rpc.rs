//! 可腳本化的假 RPC 連線。

use crate::traits::{
    kind_from_errno, timed_out, EndpointSpec, PalError, PalErrorKind, PalResult, RpcConnector,
    RpcStream, SockTimeout,
};
use std::collections::{BTreeMap, VecDeque};
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
