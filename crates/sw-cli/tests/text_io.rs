//! 文字輸出寫入語意回歸測試（P3-T11 修補）：CRLF 單次寫入、`err_str` 寫後 flush、stdout／stderr 合流順序，
//! 以及 8192 位元組邊界的 BrokenPipe 時機（對應 Python `sys.stdout` 文字層的 flush 行為，Q-06、Q-20）。

use std::cell::RefCell;
use std::io::{self, BufWriter, Write};
use std::rc::Rc;

use sw_cli::ctx::TextIo;
use sw_cli::output::{mirror_err, print_json};
use sw_proto::pydict;
use sw_proto::pyjson::PyDict;

/// 共用位元組槽：stdout 與 stderr 寫入同一處，模擬合流（`2>&1`）後的先後順序。
#[derive(Clone, Default)]
struct Shared(Rc<RefCell<Vec<u8>>>);

impl Write for Shared {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.borrow_mut().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// 記錄每次 `write` 收到的位元組數與 `flush` 次數。
#[derive(Default)]
struct Recorder {
    calls: Vec<usize>,
    flushes: usize,
    data: Vec<u8>,
}

impl Write for Recorder {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.calls.push(buf.len());
        self.data.extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.flushes += 1;
        Ok(())
    }
}

/// 任何寫入與 flush 都回 BrokenPipe（讀端已關閉）。
struct BrokenPipeWriter;

impl Write for BrokenPipeWriter {
    fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
        Err(io::Error::from(io::ErrorKind::BrokenPipe))
    }

    fn flush(&mut self) -> io::Result<()> {
        Err(io::Error::from(io::ErrorKind::BrokenPipe))
    }
}

/// `{"m":"xxx…"}` 形式、序列化後恰為 `total` 位元組的 dict（不含換行）。
fn body_of_len(total: usize) -> PyDict {
    let overhead = "{\"m\":\"\"}".len();
    pydict! { "m" => "x".repeat(total - overhead) }
}

fn body_text(total: usize) -> String {
    format!("{{\"m\":\"{}\"}}", "x".repeat(total - "{\"m\":\"\"}".len()))
}

/// 錯誤回應（`ok` 為假），用於 stderr 錯誤行。
fn failing_resp() -> PyDict {
    pydict! {
        "ok" => false,
        "error_code" => "E",
    }
}

#[test]
fn err_str_crlf_single_write_then_flush() {
    let mut out = Vec::new();
    let mut err = Recorder::default();
    let mut io = TextIo::new(&mut out, &mut err, "\r\n");
    io.err_str("a\nb\n").unwrap();
    assert_eq!(err.data, b"a\r\nb\r\n");
    assert_eq!(err.calls, vec![6], "err_str 應以單次 write 寫出整段");
    assert_eq!(err.flushes, 1, "err_str 寫入後必須 flush（Q-06）");
}

#[test]
fn mirror_err_crlf_is_single_write() {
    let mut out = Vec::new();
    let mut err = Recorder::default();
    let mut io = TextIo::new(&mut out, &mut err, "\r\n");
    mirror_err(&mut io, &failing_resp(), Some("c")).unwrap();
    assert_eq!(err.data, b"serialwrap: c failed: E\r\n");
    assert_eq!(
        err.calls.len(),
        1,
        "錯誤行應以單次 write 寫出（Python 單次 sys.stderr.write）"
    );
}

#[test]
fn crlf_print_json_is_single_translated_write() {
    let mut out = Recorder::default();
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\r\n");
    print_json(&mut io, &body_of_len(8190)).unwrap();
    assert_eq!(
        out.calls,
        vec![8192],
        "CRLF 下 8190 位元組本體加換行應一次寫出 8192 位元組"
    );
}

#[test]
fn crlf_8192_boundary_fails_at_write_time() {
    // 與 `stdout_writer(false)` 相同容量的緩衝層；底層為已關閉的 pipe。
    let mut out = BufWriter::with_capacity(8192, BrokenPipeWriter);
    let mut err = Vec::new();
    let mut io = TextIo::new(&mut out, &mut err, "\r\n");
    let res = print_json(&mut io, &body_of_len(8190));
    let kind = res.as_ref().err().map(io::Error::kind);
    assert_eq!(
        kind,
        Some(io::ErrorKind::BrokenPipe),
        "轉換後 8192 位元組應於寫入當下失敗（Python exit 1），不得延後到 flush"
    );
}

#[test]
fn crlf_small_output_defers_error_to_flush() {
    // 轉換後 100 位元組以下留在緩衝，寫入成功、於結束 flush 才失敗（Python exit 120）。
    let mut out = BufWriter::with_capacity(8192, BrokenPipeWriter);
    let mut err = Vec::new();
    {
        let mut io = TextIo::new(&mut out, &mut err, "\r\n");
        assert!(print_json(&mut io, &body_of_len(100)).is_ok());
    }
    assert!(out.flush().is_err());
}

#[test]
fn merged_stream_order_crlf_8190_matches_python() {
    // Python 在 8192 邊界直接寫出 stdout，之後才寫 stderr，因此合流後 stdout 本體（含 CRLF）先於錯誤行。
    let sink = Shared::default();
    let mut out = BufWriter::with_capacity(8192, sink.clone());
    let mut err = sink.clone();
    {
        let mut io = TextIo::new(&mut out, &mut err, "\r\n");
        print_json(&mut io, &body_of_len(8190)).unwrap();
        mirror_err(&mut io, &failing_resp(), Some("c")).unwrap();
    }
    out.flush().unwrap();

    let mut expected = format!("{}\r\n", body_text(8190)).into_bytes();
    expected.extend_from_slice(b"serialwrap: c failed: E\r\n");
    assert_eq!(*sink.0.borrow(), expected);
}
