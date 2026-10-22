//! 記憶體雙向 pipe（P5 Task 1 骨架；RPC server 端 `RpcServerStream` 的 impl 由 Task 0c 補上）。

use std::collections::VecDeque;
use std::io::{self, Read, Write};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crate::traits::{PalResult, RpcServerStream};

#[derive(Debug)]
struct PipeState {
    buf: VecDeque<u8>,
    write_closed: bool,
    read_closed: bool,
    read_timeout: Option<Duration>,
}

#[derive(Debug)]
struct Pipe {
    state: Mutex<PipeState>,
    cond: Condvar,
}

impl Pipe {
    fn new() -> Self {
        Self {
            state: Mutex::new(PipeState {
                buf: VecDeque::new(),
                write_closed: false,
                read_closed: false,
                read_timeout: None,
            }),
            cond: Condvar::new(),
        }
    }
}

/// 雙向記憶體 pipe 的一端。
pub struct MemStream {
    incoming: Arc<Pipe>,
    outgoing: Arc<Pipe>,
}

impl MemStream {
    pub fn pair() -> (MemStream, MemStream) {
        let a_to_b = Arc::new(Pipe::new());
        let b_to_a = Arc::new(Pipe::new());

        let a = MemStream {
            incoming: Arc::clone(&b_to_a),
            outgoing: Arc::clone(&a_to_b),
        };
        let b = MemStream {
            incoming: a_to_b,
            outgoing: b_to_a,
        };
        (a, b)
    }

    /// 關閉本端寫方向；對端讀回 0（EOF）。
    pub fn close_write(&mut self) {
        let mut state = self.outgoing.state.lock().unwrap_or_else(|e| e.into_inner());
        state.write_closed = true;
        self.outgoing.cond.notify_all();
    }
}

impl Read for MemStream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        let mut state = self.incoming.state.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if !state.buf.is_empty() {
                let n = std::cmp::min(buf.len(), state.buf.len());
                for b in &mut buf[..n] {
                    *b = state.buf.pop_front().unwrap();
                }
                return Ok(n);
            }
            if state.write_closed || state.read_closed {
                return Ok(0);
            }
            if let Some(timeout) = state.read_timeout {
                let (guard, res) = self
                    .incoming
                    .cond
                    .wait_timeout(state, timeout)
                    .unwrap_or_else(|e| e.into_inner());
                state = guard;
                if res.timed_out() && state.buf.is_empty() && !state.write_closed && !state.read_closed {
                    return Err(io::Error::new(io::ErrorKind::TimedOut, "read timed out"));
                }
            } else {
                state = self.incoming.cond.wait(state).unwrap_or_else(|e| e.into_inner());
            }
        }
    }
}

impl Write for MemStream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let mut state = self.outgoing.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.write_closed || state.read_closed {
            return Err(io::Error::new(io::ErrorKind::BrokenPipe, "write closed"));
        }
        state.buf.extend(buf);
        self.outgoing.cond.notify_all();
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl RpcServerStream for MemStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        let mut state = self.incoming.state.lock().unwrap_or_else(|e| e.into_inner());
        state.read_closed = true;
        self.incoming.cond.notify_all();
        Ok(())
    }

    fn set_read_timeout(&mut self, d: Option<Duration>) -> PalResult<()> {
        let mut state = self.incoming.state.lock().unwrap_or_else(|e| e.into_inner());
        state.read_timeout = d;
        Ok(())
    }
}

impl Drop for MemStream {
    fn drop(&mut self) {
        self.close_write();
        let mut state = self.incoming.state.lock().unwrap_or_else(|e| e.into_inner());
        state.read_closed = true;
        self.incoming.cond.notify_all();
    }
}
