//! sw-pal 記憶體替身自測（P4-T1）；Slice 2.1 Task 1 追加 FakeClock／FakeSerial／FakeWalFs／MemStream 自測。
#![cfg(feature = "test-fakes")]
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use sw_pal::fakes::*;
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::mem_stream::MemStream;
use sw_pal::fakes::serial::{FakeSerialBackend, FakeWriteStep};
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::{
    CancelToken, Clock, DevicePath, HolderReport, HolderScanStatus, OpenError, PalError,
    PalErrorKind, ReadError, ReadOutcome, SerialBackend, SerialConfig, SubmitGate,
    SubmitRevoked, SubmitStep, TxOutcome, WalFs,
};
use sw_pal::SysEnv;

#[test]
fn fake_sysenv_records_calls_and_scripted_rc() {
    let env = FakeSysEnv::builder()
        .command(&["systemctl", "--user", "is-active", "serialwrap"], 3, "inactive\n", "")
        .build();
    let out = env
        .run(
            &[
                "systemctl".into(),
                "--user".into(),
                "is-active".into(),
                "serialwrap".into(),
            ],
            None,
        )
        .unwrap();
    assert_eq!((out.rc, out.stdout.as_str()), (3, "inactive\n"));
    let out2 = env
        .run(&["loginctl".into(), "enable-linger".into(), "alice".into()], None)
        .unwrap();
    assert_eq!(out2.rc, 0);
    assert_eq!(env.calls().len(), 2);
}

#[test]
fn fake_platform_unconfigured_component_panics_not_real_os() {
    let pf = platform().build();
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        pf.tunnel.supports_control_master()
    }));
    assert!(r.is_err());
}

/// 收集 panic 訊息（無 panic 回 `None`）。
fn panic_text(f: impl FnOnce()) -> Option<String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f))
        .err()
        .map(|payload| {
            payload
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| payload.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_default()
        })
}

/// P5 Task 0 雙審修正：原生 PAL 元件不得出現在替身組裝點；未設定即 panic 並帶元件名稱。
#[test]
fn unconfigured_native_components_panic_not_real_os() {
    use sw_pal::console_pty::ConsoleTransportFactory;
    use sw_pal::lock::InstanceLockFactory;
    use sw_pal::traits::{DeviceSelection, EndpointSpec, ListenOptions, RpcListenerFactory, SignalWait};

    let pf = platform().build();
    let sel = DeviceSelection {
        by_id_dir: std::path::PathBuf::from("/nonexistent/by-id"),
        by_path_dir: std::path::PathBuf::from("/nonexistent/by-path"),
        exclude_coms: Vec::new(),
        test_allowlist: None,
    };
    let ep = EndpointSpec::Unix(std::ffi::OsString::from("/nonexistent/serialwrapd.sock"));
    let opts = ListenOptions {
        mode: 0o660,
        group: None,
        ensure_dir_mode: None,
    };
    let cases: [(&str, Option<String>); 5] = [
        ("devices", panic_text(|| {
            let _ = (pf.devices)(sel);
        })),
        ("instance_lock", panic_text(|| {
            let _ = pf.instance_lock.acquire(Path::new("/nonexistent/lock"), &ep);
        })),
        ("rpc_listen", panic_text(|| {
            let _ = pf.rpc_listen.bind(&ep, &opts);
        })),
        ("console", panic_text(|| {
            let _ = pf.console.create_pty();
        })),
        ("signals", panic_text(|| {
            let _ = pf.signals.install();
        })),
    ];
    for (name, msg) in cases {
        let msg = msg.unwrap_or_else(|| panic!("{name} 未 panic：可能走到原生實作"));
        assert!(
            msg.contains(&format!("fake component not configured: {name}")),
            "{name}: {msg}"
        );
    }
}

#[test]
fn p3_alias_still_compiles() {
    let _: fn() -> sw_pal::ClientPlatform = sw_pal::native_client;
}

// ---------------------------------------------------------------------------
// Slice 2.1 Task 1：PAL test-fakes（FakeClock／FakeSerialBackend／FakeWalFs／MemStream）自測。
// 紅燈階段：替身行為尚未實作，以下測試應於執行期因 `unimplemented!`（slice21 T1 ...）失敗。
// ---------------------------------------------------------------------------

const TTY: &str = "/dev/ttyFAKE0";
const WAL: &str = "/wal/raw.wal.ndjson";
const IO_ERR: &str = "[Errno 5] Input/output error";
const NOSPC: &str = "[Errno 28] No space left on device";

fn cfg() -> SerialConfig {
    SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)
}

fn dev_path() -> DevicePath {
    DevicePath(TTY.into())
}

/// 永遠撤銷的 gate（R0-F1）：`os_submit` 不得被呼叫。
struct RevokeAll;

impl SubmitGate for RevokeAll {
    fn submit(
        &self,
        _os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        Err(SubmitRevoked)
    }
}

/// 第一次放行 `os_submit`，之後撤銷（R0-F1 尾段停止測試）。
struct AcceptFirstThenRevoke(AtomicUsize);

impl SubmitGate for AcceptFirstThenRevoke {
    fn submit(
        &self,
        os_submit: &mut dyn FnMut() -> SubmitStep,
    ) -> Result<SubmitStep, SubmitRevoked> {
        if self.0.fetch_add(1, Ordering::SeqCst) == 0 {
            Ok(os_submit())
        } else {
            Err(SubmitRevoked)
        }
    }
}

#[test]
fn partial_write_then_error_reports_confirmed_bytes_only() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![FakeWriteStep::AcceptThenError(
        3,
        PalError::io(5, IO_ERR),
    )]);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let res = w.write(b"0123456789", &CancelToken::new(waker));
    assert_eq!(res.outcome, TxOutcome::Completed(3));
    assert!(res.error.is_some());
    assert_eq!(dev.take_tx(), b"012");
}

#[test]
fn block_until_cancel_returns_bytes_before_cancel() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![FakeWriteStep::BlockUntilCancel {
        accept_before_cancel: 4,
    }]);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    let t2 = tok.clone();
    let j = thread::spawn(move || w.write(b"abcdefgh", &t2));
    let start = std::time::Instant::now();
    while dev.os_submits() == 0 {
        if start.elapsed() > Duration::from_secs(5) {
            panic!("os_submits not reached in time");
        }
        thread::yield_now();
    }
    tok.cancel();
    let res = j.join().unwrap();
    assert_eq!(res.outcome, TxOutcome::Completed(4));
    assert_eq!(res.error.unwrap().kind, PalErrorKind::Cancelled);
}

#[test]
fn second_open_while_held_is_held_with_unknown_holder() {
    let be = FakeSerialBackend::new();
    be.add_device(TTY);
    let _h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    match be.open_exclusive(&dev_path(), &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert!(rep.pids.is_empty());
            assert!(rep.holder_unknown);
        }
        Err(other) => panic!("expected Held, got {other:?}"),
        Ok(_) => panic!("expected Held, got an open handle"),
    }
}

#[test]
fn close_requires_reader_and_writer_and_counts_once() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (r, w, _waker, close) = h.split();
    let rc = close.close(r, w);
    // fake 不模擬殘留獨占旗標；殘留情境由 Task 2 原生 PTY 測試驗證（R0-F3）。
    assert!(rc.closed && rc.excl_cleared && rc.error.is_none());
    assert_eq!(
        (dev.open_count(), dev.close_count(), dev.is_open()),
        (1, 1, false)
    );
}

#[test]
fn fake_clock_sleep_wakes_on_advance_and_wall_jump_keeps_mono() {
    let c = FakeClock::at(1_000, 1_760_000_000_000_000);
    let c2: Arc<dyn Clock> = c.clone();
    let j = thread::spawn(move || {
        c2.sleep(Duration::from_millis(500));
        c2.mono_ns()
    });
    let start = std::time::Instant::now();
    while c.sleeps_s().is_empty() {
        if start.elapsed() > Duration::from_secs(5) {
            panic!("sleeper did not register in time");
        }
        thread::yield_now();
    }
    c.set_wall_us(1_759_996_400_000_000);
    c.advance(Duration::from_millis(500));
    assert_eq!(j.join().unwrap(), 1_000 + 500_000_000);
}

#[test]
fn unknown_step_reports_unknown() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![FakeWriteStep::Unknown {
        confirmed: 0,
        error: PalError::io(5, IO_ERR),
    }]);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let res = w.write(b"abc", &CancelToken::new(waker));
    assert_eq!(res.outcome, TxOutcome::Unknown { confirmed: 0 });
    assert!(dev.take_tx().is_empty());
}

#[test]
fn deny_access_maps_to_access_denied() {
    let be = FakeSerialBackend::new();
    be.add_device(TTY);
    be.deny_access(TTY);
    match be.open_exclusive(&dev_path(), &cfg()) {
        Err(OpenError::AccessDenied { holder, .. }) => assert_eq!(
            holder,
            HolderReport {
                pids: vec![],
                holder_unknown: true,
                status: HolderScanStatus::Unsupported,
            }
        ),
        Err(other) => panic!("expected AccessDenied, got {other:?}"),
        Ok(_) => panic!("expected AccessDenied, got an open handle"),
    }
}

#[test]
fn removed_device_read_errors() {
    let be = FakeSerialBackend::new();
    be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (mut r, _w, _waker, _close) = h.split();
    be.remove_device(TTY);
    let mut buf = [0u8; 16];
    match r.read(&mut buf, Some(Duration::from_millis(200))) {
        Err(ReadError::Read(e)) => assert_eq!(e.py_message, IO_ERR),
        Err(other) => panic!("expected read error, got {other:?}"),
        Ok(out) => panic!("expected read error after remove_device, got {out:?}"),
    }
}

#[test]
fn reader_woken_by_waker() {
    let be = FakeSerialBackend::new();
    be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (mut r, _w, waker, _close) = h.split();
    let w2 = Arc::clone(&waker);
    let j = thread::spawn(move || {
        thread::sleep(Duration::from_millis(20));
        w2.wake();
    });
    let mut buf = [0u8; 8];
    let out = r.read(&mut buf, Some(Duration::from_secs(1)));
    j.join().unwrap();
    assert_eq!(out.unwrap(), ReadOutcome::Woken);
}

#[test]
fn fail_close_reports_error() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (r, w, _waker, close) = h.split();
    dev.fail_close(PalError::io(5, IO_ERR));
    let rc = close.close(r, w);
    assert!(!rc.closed && !rc.excl_cleared && rc.error.is_some());
}

#[test]
fn wal_fs_external_unlink_keeps_handle() {
    let fs = FakeWalFs::new();
    let p = Path::new(WAL);
    let file = fs.open_append(p).unwrap();
    fs.external_unlink(p);
    assert_eq!(file.unlinked(), Ok(true));
    assert_eq!(fs.path_identity(p), Ok(None));
}

#[test]
fn wal_fs_pending_delete_with_external_reader() {
    let fs = FakeWalFs::new();
    let p = Path::new(WAL);
    let file = fs.open_append(p).unwrap();
    let old = file.identity().unwrap();
    let ext = fs.hold_external_reader(p);
    fs.external_unlink(p);
    assert_eq!(fs.path_identity(p), Ok(Some(old)));
    match fs.open_append(p) {
        Err(e) => assert_eq!(e.kind, PalErrorKind::AccessDenied),
        Ok(_) => panic!("open_append must be denied while pending-delete"),
    }
    drop(ext);
    assert!(fs.open_append(p).is_ok());
}

#[test]
fn wal_fs_partial_write_reports_sent_and_keeps_prefix() {
    let fs = FakeWalFs::new();
    let p = Path::new(WAL);
    let mut file = fs.open_append(p).unwrap();
    fs.fail_next_write_partial(p, 4, PalError::io(28, NOSPC));
    let e = file.write_all_flush(b"abcdefgh\n").unwrap_err();
    assert_eq!(e.sent, 4);
    assert_eq!(e.error.py_message, NOSPC);
    assert_eq!(fs.contents(p), Some(b"abcd".to_vec()));
    file.write_all_flush(b"efgh\n").unwrap();
    assert_eq!(fs.contents(p), Some(b"abcdefgh\n".to_vec()));
    assert_eq!(
        fs.write_calls(p),
        vec![b"abcdefgh\n".to_vec(), b"efgh\n".to_vec()]
    );
    fs.fail_next_writes(p, 1, PalError::io(28, NOSPC));
    let e2 = file.write_all_flush(b"ijkl\n").unwrap_err();
    assert_eq!(e2.sent, 0);
    assert_eq!(fs.contents(p), Some(b"abcdefgh\n".to_vec()));
}

#[test]
fn mem_stream_roundtrip() {
    use std::io::{Read, Write};
    let (mut a, mut b) = MemStream::pair();
    a.write_all(b"x\n").unwrap();
    let mut buf = [0u8; 2];
    b.read_exact(&mut buf).unwrap();
    assert_eq!(&buf, b"x\n");
    a.close_write();
    let mut rest = [0u8; 8];
    assert_eq!(b.read(&mut rest).unwrap(), 0);
}

#[test]
fn on_tx_reply_pushes_rx_once() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.on_tx_reply(b"echo hi\n", b"hi\r\n# ");
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (mut r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    w.write(b"echo hi\n", &tok);
    let mut buf = [0u8; 64];
    match r.read(&mut buf, Some(Duration::from_secs(1))) {
        Ok(ReadOutcome::Data(n)) => assert_eq!(&buf[..n], b"hi\r\n# "),
        other => panic!("expected reply bytes, got {other:?}"),
    }
    w.write(b"echo hi\n", &tok);
    assert_eq!(
        r.read(&mut buf, Some(Duration::from_millis(100))),
        Ok(ReadOutcome::TimedOut)
    );
}

#[test]
fn on_tx_match_echoes_nonce() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.on_tx_match(
        r"^echo __READY__([0-9a-f]{8})$",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let nonce = text
                .trim_end()
                .trim_start_matches("echo __READY__")
                .to_string();
            format!("__READY__{nonce}\r\n# ").into_bytes()
        }),
    );
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (mut r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    w.write(b"echo __READY__ab12cd34\n", &tok);
    let mut buf = [0u8; 128];
    match r.read(&mut buf, Some(Duration::from_secs(1))) {
        Ok(ReadOutcome::Data(n)) => {
            let text = String::from_utf8_lossy(&buf[..n]).into_owned();
            assert!(text.contains("__READY__ab12cd34"), "reply was {text:?}");
        }
        other => panic!("expected reply, got {other:?}"),
    }
}

#[test]
fn fake_clock_subscribe_notified_on_advance() {
    let c = FakeClock::new();
    let rx = c.subscribe();
    c.advance(Duration::from_millis(1));
    assert!(rx.recv_timeout(Duration::from_millis(100)).is_ok());
}

#[test]
fn fake_writer_revoking_gate_never_submits() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    let res = w.write_gated(b"abc", &tok, &RevokeAll);
    assert_eq!(res.outcome, TxOutcome::NotSubmitted);
    assert!(res.error.is_none());
    assert_eq!(dev.os_submits(), 0);
    assert!(dev.take_tx().is_empty());
    assert_eq!(dev.write_calls(), 1);
}

#[test]
fn fake_revoke_after_partial_stops_tail() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![FakeWriteStep::AcceptThenWouldBlock(2)]);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    let j = thread::spawn(move || {
        let gate = AcceptFirstThenRevoke(AtomicUsize::new(0));
        w.write_gated(b"abcdef", &tok, &gate)
    });
    assert!(dev.wait_paused(Duration::from_secs(5)));
    dev.release_pause();
    let res = j.join().unwrap();
    assert_eq!(res.outcome, TxOutcome::Completed(2));
    assert_eq!(res.error.unwrap().kind, PalErrorKind::Cancelled);
    assert_eq!(dev.take_tx(), b"ab");
    assert_eq!(dev.os_submits(), 1);
}

#[test]
fn fake_writer_revoking_gate_with_error_and_unknown() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![
        FakeWriteStep::Error(PalError::io(5, IO_ERR)),
        FakeWriteStep::Unknown {
            confirmed: 2,
            error: PalError::io(5, IO_ERR),
        },
    ]);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);

    // Revoking gate: Error step returns NotSubmitted
    let res1 = w.write_gated(b"abc", &tok, &RevokeAll);
    assert_eq!(res1.outcome, TxOutcome::NotSubmitted);
    assert!(res1.error.is_none());
    assert_eq!(dev.os_submits(), 0);
    assert!(dev.take_tx().is_empty());

    // Revoking gate: Unknown step returns NotSubmitted
    let res2 = w.write_gated(b"abc", &tok, &RevokeAll);
    assert_eq!(res2.outcome, TxOutcome::NotSubmitted);
    assert!(res2.error.is_none());
    assert_eq!(dev.os_submits(), 0);
    assert!(dev.take_tx().is_empty());

    // Count gate: Unknown { confirmed: 0 } calls the gate exactly once
    struct CountGate(AtomicUsize);
    impl SubmitGate for CountGate {
        fn submit(
            &self,
            os_submit: &mut dyn FnMut() -> SubmitStep,
        ) -> Result<SubmitStep, SubmitRevoked> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Ok(os_submit())
        }
    }
    dev.script_write(vec![FakeWriteStep::Unknown {
        confirmed: 0,
        error: PalError::io(5, IO_ERR),
    }]);
    let gate = CountGate(AtomicUsize::new(0));
    let res3 = w.write_gated(b"abc", &tok, &gate);
    assert_eq!(res3.outcome, TxOutcome::Unknown { confirmed: 0 });
    assert_eq!(gate.0.load(Ordering::SeqCst), 1);
    assert_eq!(dev.os_submits(), 1);
}

#[test]
fn on_tx_match_handles_chunked_write_and_no_premature_reply() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.on_tx_match(
        r"^echo __READY__([0-9a-f]{8})$",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line);
            let nonce = text
                .trim_end()
                .trim_start_matches("echo __READY__")
                .to_string();
            format!("__READY__{nonce}\r\n# ").into_bytes()
        }),
    );
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (mut r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);

    // 第一筆不含換行：不得提前回覆
    w.write(b"echo __READY__1234", &tok);
    let mut buf = [0u8; 128];
    assert_eq!(
        r.read(&mut buf, Some(Duration::from_millis(50))),
        Ok(ReadOutcome::TimedOut)
    );

    // 第二筆補上剩餘內容與換行：此時才觸發完整行比對並回覆
    w.write(b"5678\n", &tok);
    match r.read(&mut buf, Some(Duration::from_secs(1))) {
        Ok(ReadOutcome::Data(n)) => {
            let text = String::from_utf8_lossy(&buf[..n]);
            assert!(text.contains("__READY__12345678"), "got {text:?}");
        }
        other => panic!("expected reply after full line, got {other:?}"),
    }
}

#[test]
fn on_tx_match_panics_on_unsupported_regex() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    assert!(std::panic::catch_unwind(|| {
        dev.on_tx_match("foo|bar", Box::new(|_| Vec::new()));
    })
    .is_err());
    assert!(std::panic::catch_unwind(|| {
        dev.on_tx_match(r"\bword\b", Box::new(|_| Vec::new()));
    })
    .is_err());
    assert!(std::panic::catch_unwind(|| {
        dev.on_tx_match("colou?r", Box::new(|_| Vec::new()));
    })
    .is_err());
}

#[test]
fn fail_close_leaves_held() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (r, w, _waker, close) = h.split();
    dev.fail_close(PalError::io(5, IO_ERR));
    let rc = close.close(r, w);
    assert!(!rc.closed && !rc.excl_cleared);
    // 關閉失敗時独占狀態不釋放，後續 open_exclusive 必須回 Held
    match be.open_exclusive(&dev_path(), &cfg()) {
        Err(OpenError::Held(_)) => {}
        Err(e) => panic!("expected Held after failed close, got Err({e:?})"),
        Ok(_) => panic!("expected Held after failed close, got Ok"),
    }
}

#[test]
fn drop_without_close_clears_open() {
    let be = FakeSerialBackend::new();
    be.add_device(TTY);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    drop(h);
    // 未呼叫 CloseToken::close 直接 drop 時，OpenGuard 自動釋放
    assert!(be.open_exclusive(&dev_path(), &cfg()).is_ok());

    // split 後各端未呼叫 close 亦同
    let h2 = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (r, w, _waker, close) = h2.split();
    drop(r);
    drop(w);
    drop(close);
    assert!(be.open_exclusive(&dev_path(), &cfg()).is_ok());
}

#[test]
fn mem_stream_write_to_closed_peer_fails() {
    use std::io::Write;
    let (mut a, b) = MemStream::pair();
    drop(b);
    let res = a.write(b"hello");
    assert_eq!(res.unwrap_err().kind(), std::io::ErrorKind::BrokenPipe);
}

#[test]
fn wal_fs_non_writable_dir_prevents_file_creation() {
    let fs = FakeWalFs::new();
    let dir = Path::new("/wal");
    fs.set_dir_writable(dir, false);
    let new_path = Path::new("/wal/new.wal");
    match fs.open_append(new_path) {
        Err(e) => assert_eq!(e.py_message, "[Errno 13] Permission denied"),
        Ok(_) => panic!("expected permission denied on new file in non-writable dir"),
    }

    // 已存在的檔案不受目錄不可寫影響（模擬唯讀目錄下已開啟/既有檔案追加）
    let existing = Path::new("/wal/existing.wal");
    fs.seed(existing, b"hello\n");
    assert!(fs.open_append(existing).is_ok());

    // 重新命名至不可寫目錄亦失敗
    let from = Path::new("/wal/existing.wal");
    let to = Path::new("/wal/archived.wal");
    let rres = fs.rename_noreplace_or_suffix(from, to);
    assert_eq!(rres.unwrap_err().py_message, "[Errno 13] Permission denied");
}

#[test]
fn clock_advance_saturates_at_max() {
    let c = FakeClock::at(u64::MAX - 5, 0);
    c.advance(Duration::from_nanos(10));
    assert_eq!(c.mono_ns(), u64::MAX);
}

/// 審查 F45：`release_block` 落在 `os_submits` 已增加、writer 尚未進入等待的空窗（`os_submit` 閉包內）
/// 時不得被吃掉；舊實作於 gate 之後才重設旗標，writer 永遠阻塞。
#[test]
fn block_forever_release_during_submit_window_is_not_lost() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![FakeWriteStep::BlockForever]);
    let hook_dev = dev.clone();
    dev.on_os_submit(Box::new(move || hook_dev.release_block()));
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    let (tx, rx) = std::sync::mpsc::channel();
    thread::spawn(move || {
        let _ = tx.send(w.write(b"abc", &tok));
    });
    let res = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("空窗內的 release_block 不得遺失");
    assert_eq!(res.outcome, TxOutcome::Completed(3));
}

/// 審查 F44：經 gate 提交後阻塞直到 cancel，才回 `Unknown { confirmed }`。
#[test]
fn block_until_cancel_then_unknown_reports_unknown_after_cancel() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.script_write(vec![FakeWriteStep::BlockUntilCancelThenUnknown {
        confirmed: 0,
        error: PalError::io(5, IO_ERR),
    }]);
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    let t2 = tok.clone();
    let j = thread::spawn(move || w.write(b"abcdefgh", &t2));
    let start = std::time::Instant::now();
    while dev.os_submits() == 0 {
        assert!(start.elapsed() < Duration::from_secs(5), "os_submits not reached in time");
        thread::yield_now();
    }
    thread::sleep(Duration::from_millis(30));
    assert!(!j.is_finished(), "cancel 前不得返回");
    tok.cancel();
    let res = j.join().unwrap();
    assert_eq!(res.outcome, TxOutcome::Unknown { confirmed: 0 });
    assert!(res.error.is_some());
    assert!(dev.take_tx().is_empty());
}

/// 審查 F46：`pause_next_close` 讓下一次 close 停在暫停點，`release_pause` 後才回條。
#[test]
fn pause_next_close_holds_close_until_released() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device(TTY);
    dev.pause_next_close();
    let h = be.open_exclusive(&dev_path(), &cfg()).unwrap();
    let (r, w, _waker, close) = h.split();
    let j = thread::spawn(move || close.close(r, w));
    assert!(dev.wait_paused(Duration::from_secs(5)), "close 應停在暫停點");
    assert!(!j.is_finished());
    dev.release_pause();
    let receipt = j.join().unwrap();
    assert!(receipt.closed);
    assert_eq!(dev.close_count(), 1);
}
