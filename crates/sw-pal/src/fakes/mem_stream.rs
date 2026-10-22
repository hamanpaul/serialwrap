//! 記憶體雙向 pipe（P5 Task 1 骨架；RPC server 端 `RpcServerStream` 的 impl 由 Task 0c 補上）。

use std::io::{self, Read, Write};
use std::time::Duration;

use crate::traits::{PalResult, RpcServerStream};

/// 雙向記憶體 pipe 的一端。
pub struct MemStream {
    _private: (),
}

impl MemStream {
    pub fn pair() -> (MemStream, MemStream) {
        s21_todo!("T1", "MemStream::pair")
    }

    /// 關閉本端寫方向；對端讀回 0（EOF）。
    pub fn close_write(&mut self) {
        s21_todo!("T1", "MemStream::close_write")
    }
}

impl Read for MemStream {
    fn read(&mut self, _buf: &mut [u8]) -> io::Result<usize> {
        s21_todo!("T1", "MemStream::read")
    }
}

impl Write for MemStream {
    fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
        s21_todo!("T1", "MemStream::write")
    }

    fn flush(&mut self) -> io::Result<()> {
        s21_todo!("T1", "MemStream::flush")
    }
}

impl RpcServerStream for MemStream {
    fn shutdown_read(&mut self) -> PalResult<()> {
        s21_todo!("T1", "MemStream::shutdown_read")
    }

    fn set_read_timeout(&mut self, _d: Option<Duration>) -> PalResult<()> {
        s21_todo!("T1", "MemStream::set_read_timeout")
    }
}
