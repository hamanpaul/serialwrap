//! P3-T14 run_process / run_process_with 錯誤處理與 exit code 契約測試。
#[cfg(feature = "test-fakes")]
mod support;

use std::io::{self, Write};
use std::sync::{Arc, Mutex};
#[cfg(feature = "test-fakes")]
use support::TestEnv;
use sw_cli::ctx::TextIo;
use sw_cli::output::{print_json, warn_version_mismatch_with_fingerprint};
use sw_cli::run_process_with;
use sw_proto::pydict;
use sw_proto::pyjson::{dumps_dict, CLI_STDOUT};

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

#[cfg(feature = "test-fakes")]
#[test]
fn s1m241_version_uses_manifest_fingerprint() {
    let t = TestEnv::posix().with_bundle("0.4.0");
    let manifest_path = t.bundle_dir().join("release-manifest.json");
    let manifest = std::fs::read_to_string(&manifest_path).expect("manifest 應存在");
    let with_fingerprint = manifest.replace(
        "\"state_schema\":\"python-state-v0\"",
        "\"state_schema\":\"python-state-v0\",\"code_fingerprint\":\"0123456789ab\"",
    );
    assert_ne!(with_fingerprint, manifest, "fixture 必須含 daemon.state_schema 欄位");
    t.write_file(&manifest_path, with_fingerprint.as_bytes());

    let (code, stdout, stderr) = t.run(&["--version"]);

    assert_eq!(code, 0);
    assert_eq!(stdout, format!("serialwrap {} (code 0123456789ab)\n", sw_cli::CLI_VERSION));
    assert_eq!(stderr, "");
    assert!(t.calls().is_empty(), "--version 不得啟動 Python 子行程");
}

#[cfg(feature = "test-fakes")]
#[test]
fn s1m241_version_fingerprint_falls_back_without_manifest_or_field() {
    let without_manifest = TestEnv::posix();
    let (code, stdout, stderr) = without_manifest.run(&["--version"]);
    assert_eq!(code, 0);
    assert_eq!(stdout, format!("serialwrap {}\n", sw_cli::CLI_VERSION));
    assert_eq!(stderr, "");
    assert!(without_manifest.calls().is_empty());

    let without_field = TestEnv::posix().with_bundle("0.4.0");
    let (code, stdout, stderr) = without_field.run(&["--version"]);
    assert_eq!(code, 0);
    assert_eq!(stdout, format!("serialwrap {}\n", sw_cli::CLI_VERSION));
    assert_eq!(stderr, "");
    assert!(without_field.calls().is_empty());
}

#[test]
fn s1m241_fingerprint_warning_preserves_response_stdout_and_exit_code() {
    let resp = pydict! {
        "ok" => false,
        "error_code" => "TEST_ERROR",
        "version" => "0.3.2",
        "code_fingerprint" => "bbbbbbbbbbbb",
    };
    let before = dumps_dict(&resp, &CLI_STDOUT);
    let out = SharedBuffer::default();
    let err = SharedBuffer::default();

    let code = run_process_with(
        &|stdout, stderr| {
            let mut io = TextIo::new(stdout, stderr, "\n");
            print_json(&mut io, &resp).unwrap();
            warn_version_mismatch_with_fingerprint(
                &mut io,
                &resp,
                "0.3.2",
                Some("aaaaaaaaaaaa"),
            )
            .unwrap();
            17
        },
        Box::new(out.clone()),
        Box::new(err.clone()),
    );

    assert_eq!(code, 17);
    assert_eq!(String::from_utf8(out.0.lock().unwrap().clone()).unwrap(), format!("{before}\n"));
    assert_eq!(
        String::from_utf8(err.0.lock().unwrap().clone()).unwrap(),
        "serialwrap: client 程式碼 aaaaaaaaaaaa 與 daemon 程式碼 bbbbbbbbbbbb 不一致（daemon 可能仍在跑升級前的舊碼）；請執行 `serialwrap service restart`（on-demand 模式：`serialwrap daemon stop` 後 `serialwrap daemon start`）\n"
    );
    assert_eq!(dumps_dict(&resp, &CLI_STDOUT), before);
}
