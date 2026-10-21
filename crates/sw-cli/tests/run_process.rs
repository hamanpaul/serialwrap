//! P3-T14 run_process / run_process_with 錯誤處理與 exit code 契約測試。
use std::io::{self, Write};
use std::sync::{Arc, Mutex};
use sw_cli::run_process_with;

#[derive(Clone, Default)]
struct SharedBuffer(Arc<Mutex<Vec<u8>>>);

impl Write for SharedBuffer {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

struct FailOnWrite;

impl Write for FailOnWrite {
    fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
        Err(io::Error::new(io::ErrorKind::BrokenPipe, "broken pipe"))
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

struct FailOnFlush;

impl Write for FailOnFlush {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::Other, "flush failed"))
    }
}

#[test]
fn test_run_process_panic() {
    let out = SharedBuffer::default();
    let err = SharedBuffer::default();
    let code = run_process_with(
        &|_, _| panic!("boom"),
        Box::new(out.clone()),
        Box::new(err.clone()),
    );
    assert_eq!(code, 1);
    let stdout_bytes = out.0.lock().unwrap().clone();
    let stderr_bytes = err.0.lock().unwrap().clone();
    assert!(stdout_bytes.is_empty(), "panic 時 stdout 不得寫入任何內容");
    let stderr_str = String::from_utf8_lossy(&stderr_bytes);
    assert!(
        stderr_str.contains("serialwrap: internal error: boom"),
        "stderr 應輸出 internal error 訊息，實際為: {stderr_str}"
    );
}

#[test]
fn test_run_process_write_fail() {
    let err = SharedBuffer::default();
    let code = run_process_with(
        &|out, _| {
            let _ = out.write_all(b"test output");
            0
        },
        Box::new(FailOnWrite),
        Box::new(err),
    );
    assert_eq!(code, 1);
}

#[test]
fn test_run_process_flush_fail() {
    let err = SharedBuffer::default();
    let code = run_process_with(
        &|out, _| {
            let _ = out.write_all(b"test output");
            0
        },
        Box::new(FailOnFlush),
        Box::new(err),
    );
    assert_eq!(code, 120);
}
