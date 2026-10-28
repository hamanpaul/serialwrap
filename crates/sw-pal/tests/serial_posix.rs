//! Slice 2.1 Task 2（規格 §6.2、arch §4.2）：POSIX 序列埠獨占開啟與 PTY 行為的失敗測試（TDD 紅燈）。
//!
//! 需要 PTY 資源的測試標記 `#[ignore = "cap:posix_pty"]`，以 `-- --ignored cap_posix_pty__` 執行；
//! fakes conformance 不需資源，預設執行。實作者必須讓這裡的測試依規格通過，不得改寫斷言。
// sw-pal 是唯一允許平台 cfg 的 crate
#![cfg(all(unix, feature = "test-fakes"))]
// 測試名沿用計畫的 `cap_posix_pty__*` 命名（雙底線即為計畫指定的分隔），不套用 snake_case 警告。
#![allow(non_snake_case)]

use std::{
    thread,
    time::{Duration, Instant},
};
use sw_pal::{conformance::posix_pty_pair, *};

fn cfg() -> SerialConfig {
    SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)
}

/// `Result<Box<dyn SerialHandle>, _>` 無法直接 `{:?}`（handle 為 trait object），此處只用來產生斷言訊息。
fn show_open(r: &Result<Box<dyn SerialHandle>, OpenError>) -> String {
    match r {
        Ok(_) => "Ok(<SerialHandle>)".to_string(),
        Err(e) => format!("{e:?}"),
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__second_open_in_process_is_held() {
    let (dev, _peer) = posix_pty_pair();
    let pal = sw_pal::native();
    let _h = pal.serial.open_exclusive(&dev, &cfg()).expect("first open");
    match pal.serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert!(
                rep.holder_unknown,
                "self pid excluded → pids empty → holder_unknown"
            )
        }
        other => panic!("expected Held, got {}", show_open(&other)),
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_plain_open_after_tiocexcl_is_ebusy() {
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    } // 報告計為證據不足
    let (dev, _peer) = posix_pty_pair();
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open_exclusive");
    let err = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&dev.0)
        .expect_err("TIOCEXCL 之後的一般 open 應失敗");
    assert_eq!(err.raw_os_error(), Some(libc::EBUSY));
}

/// 規格 §6.2「拒絕已識別的外部衝突」：外部程式先以普通 open（不 flock、不設 TIOCEXCL）持有，
/// 本端 open→flock→TIOCEXCL 都會成功，必須靠鎖後 holder 重掃拒絕；被拒時不得改 termios、不得送任何 byte。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_plain_open_before_daemon_is_held() {
    let (dev, mut peer) = posix_pty_pair();
    let child = sw_pal::conformance::spawn_plain_holder(&dev); // 子程序 open(O_RDWR|O_NOCTTY) 後 sleep；不 flock、不 TIOCEXCL
    let before = sw_pal::conformance::tcgetattr_snapshot(&dev);
    match sw_pal::native().serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert_eq!(rep.pids, vec![child.id()]);
            assert!(!rep.holder_unknown);
        }
        other => panic!(
            "expected Held for plain external holder, got {}",
            show_open(&other)
        ),
    }
    assert_eq!(
        sw_pal::conformance::tcgetattr_snapshot(&dev),
        before,
        "被拒時不得改寫外部工具的 termios"
    );
    assert_eq!(
        peer.read_available(Duration::from_millis(500)),
        Vec::<u8>::new(),
        "被拒時 UART 零 TX"
    );
    drop(child); // kill + wait
    // peer（PTY master）仍開啟：被拒路徑若只 close(fd) 未 TIOCNXCL，旗標殘留，此處會得 Held(EBUSY)（R0-F3）
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("holder 結束後可取得");
}

/// R0-F3：TIOCEXCL 是 tty 物件旗標，不隨 fd 關閉清除；PTY master 持續開啟時，CloseToken 必須先 TIOCNXCL 才關 fd，
/// 否則 PROBE→close→正式 open 與 release→外部工具 open 都會 EBUSY。本測試在修正前必定 FAIL（第一次 reopen 即 EBUSY）。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__probe_close_reopen_with_master_open() {
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    }
    let (dev, mut peer) = posix_pty_pair(); // peer 持有 master 直到測試結束
    for round in 0..3 {
        // 模擬 PROBE bridge → 正式 bridge → 再一次 recover
        let h = sw_pal::native()
            .serial
            .open_exclusive(&dev, &cfg())
            .unwrap_or_else(|e| panic!("round {round}: reopen failed {e:?}"));
        let (r, w, _waker, close) = h.split();
        let rc = close.close(r, w);
        assert!(
            rc.closed && rc.excl_cleared && rc.error.is_none(),
            "round {round}: {rc:?}"
        );
        // 外部一般 open（不經 serialwrap）也必須成功：證明旗標已解除，而不只是同 backend 的路徑碰巧可用
        let ext = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&dev.0)
            .unwrap_or_else(|e| {
                panic!(
                    "round {round}: plain open after close must not be EBUSY, got {:?}",
                    e.raw_os_error()
                )
            });
        drop(ext);
    }
    assert_eq!(
        peer.read_available(Duration::from_millis(100)),
        Vec::<u8>::new(),
        "open／close 循環不得寫出任何 byte"
    );
}

/// R0-F3：open_exclusive 在 TIOCEXCL 成功之後失敗（termios／eventfd），失敗返回前必須 TIOCNXCL。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__open_failure_after_tiocexcl_clears_excl() {
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    }
    let (dev, _peer) = posix_pty_pair();
    // test-fakes only：TIOCEXCL 成功後立即回注入的錯誤（模擬 termios 失敗）
    let be = sw_pal::serial::posix::PosixSerialBackend::with_post_excl_fault(PalError::io(
        5,
        "injected tcsetattr failure",
    ));
    match be.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Other(e)) => assert_eq!(e.kind, PalErrorKind::Io),
        other => panic!("expected Other(Io), got {}", show_open(&other)),
    }
    match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&dev.0)
    {
        Ok(f) => drop(f),
        Err(e) => panic!("失敗路徑殘留 TIOCEXCL：errno={:?}", e.raw_os_error()),
    }
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("失敗後可重新取得");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__blocked_write_cancel_returns_partial_completed() {
    let (dev, mut peer) = posix_pty_pair();
    peer.stop_reading(); // 讓 PTY buffer 塞滿
    let h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open");
    let (_r, mut w, waker, _c) = h.split();
    let tok = CancelToken::new(waker);
    let t2 = tok.clone();
    let payload = vec![b'A'; 1 << 20];
    let j = thread::spawn(move || w.write(&payload, &t2));
    thread::sleep(Duration::from_millis(200));
    let t0 = Instant::now();
    tok.cancel();
    let res = j.join().expect("writer thread");
    assert!(
        t0.elapsed() < Duration::from_millis(100),
        "cancel must wake poll(POLLOUT)"
    );
    match res.outcome {
        TxOutcome::Completed(n) => assert!(n > 0 && n < (1 << 20)),
        o => panic!("{o:?}"),
    }
    assert_eq!(
        res.error.expect("cancel error").kind,
        PalErrorKind::Cancelled
    );
    peer.resume_reading();
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__reader_woken_within_50ms() {
    let (dev, _peer) = posix_pty_pair();
    let h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open");
    let (mut r, _w, waker, _c) = h.split();
    let j = thread::spawn(move || {
        let mut b = [0u8; 64];
        r.read(&mut b, None)
    });
    thread::sleep(Duration::from_millis(50));
    let t0 = Instant::now();
    waker.wake();
    assert_eq!(
        j.join().expect("reader thread").expect("read"),
        ReadOutcome::Woken
    );
    assert!(t0.elapsed() < Duration::from_millis(50));
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__conformance_suite() {
    sw_pal::conformance::serial_suite(
        sw_pal::native().serial.clone(),
        &|| {
            let (d, p) = posix_pty_pair();
            (d, Box::new(p) as Box<dyn PeerEnd>)
        },
        sw_pal::conformance::SerialPairCaps {
            eof_on_peer_close: true,
        },
    );
}

/// 鎖後重掃 status 非 ok、pids 空（`PostLockVerdict::AcceptUnverified`）的 holder 診斷替身。
struct UnknownStatusHolders;

impl UartExclusive for UnknownStatusHolders {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: HolderScanStatus::Unknown,
        }
    }
}

/// P5-T18 parity-amendments-2026-10-26 #11：開檔回 AcceptUnverified 時登記為 holder_unknown 裝置，
/// close 後移除；鎖後重掃 ok（Accept）不登記（Python `serial_port._set_holder_unknown`）。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__accept_unverified_registers_unknown_holder_device() {
    use std::sync::Arc;
    let (dev, _peer) = posix_pty_pair();
    let key = std::fs::canonicalize(&dev.0)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| dev.0.clone());
    let be = sw_pal::serial::posix::PosixSerialBackend::with_uart_exclusive(Arc::new(
        UnknownStatusHolders,
    ));
    let h = be.open_exclusive(&dev, &cfg()).expect("AcceptUnverified 仍開檔成功");
    assert!(
        sw_pal::serial::unknown_holder_devices().contains(&key),
        "AcceptUnverified 應登記 {key}"
    );
    let (r, w, _waker, token) = h.split();
    let _ = token.close(r, w);
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&key),
        "close 後應移除 {key}"
    );

    let be = sw_pal::serial::posix::PosixSerialBackend::with_uart_exclusive(Arc::new(
        sw_pal::conformance::NoHolders,
    ));
    let h = be.open_exclusive(&dev, &cfg()).expect("Accept 開檔");
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&key),
        "鎖後重掃 ok 不登記"
    );
    drop(h);
}

#[test]
fn fakes_conformance_suite() {
    let be = sw_pal::fakes::serial::FakeSerialBackend::new();
    sw_pal::conformance::serial_suite(
        be.clone(),
        &sw_pal::conformance::fake_pair_factory(be.clone()),
        sw_pal::conformance::SerialPairCaps {
            eof_on_peer_close: true,
        },
    );
}

/// `spawn_plain_holder` 的子程序入口（僅 `S21_CHILD=plain_holder` 時作用）：普通 open（不 flock、不 TIOCEXCL），
/// 回報 READY 後阻塞於 stdin，直到父程序關閉 stdin 或 kill。預設（未 `--include-ignored`）不執行。
#[test]
#[ignore = "helper:plain_holder_child"]
fn plain_holder_child() {
    use std::io::{Read, Write};
    use std::os::unix::fs::OpenOptionsExt;
    if std::env::var("S21_CHILD").as_deref() != Ok("plain_holder") {
        return;
    }
    let dev = std::env::var("S21_DEV").expect("S21_DEV");
    let _f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY)
        .open(&dev)
        .expect("plain open");
    println!("S21_READY");
    std::io::stdout().flush().ok();
    let mut b = [0u8; 1];
    let _ = std::io::stdin().read(&mut b);
}

// ---- parity-amendments-2026-10-26 #62／#63／#65：開啟序 errno 映射逐字照 Python `serial_port.py:373-387,429-435` ----

use sw_pal::serial::posix::{open_sequence, OpenOp, TtyOpenOps};

#[derive(Default)]
struct FakeOps {
    ops: Vec<OpenOp>,
    open_err: Option<i32>,
    flock_err: Option<i32>,
    probe: Option<HolderReport>,
}

impl TtyOpenOps for FakeOps {
    fn open(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Open);
        self.open_err.map_or(Ok(()), Err)
    }
    fn flock(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Flock);
        self.flock_err.map_or(Ok(()), Err)
    }
    fn excl(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Excl);
        Ok(())
    }
    fn probe_holders(&mut self) -> HolderReport {
        self.ops.push(OpenOp::ProbeHolders);
        self.probe.clone().unwrap_or(HolderReport {
            pids: vec![],
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        })
    }
    fn termios(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Termios(Ok(())));
        Ok(())
    }
    fn nxcl(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Nxcl);
        Ok(())
    }
    fn close_fd(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::CloseFd);
        Ok(())
    }
    fn warn(&mut self, _line: &str) {}
}

fn open_with(open_err: Option<i32>, flock_err: Option<i32>) -> Result<(), OpenError> {
    let mut ops = FakeOps {
        open_err,
        flock_err,
        ..Default::default()
    };
    open_sequence(&mut ops, "/dev/ttyUSB0")
}

/// #62：只有 `ENOENT`→`NotFound`、`EBUSY`→`Held`；`EACCES`／`EPERM`／`ENODEV`／`ENXIO` 原樣 → `Other`，
/// 且 `Other` 的 Python 例外類別名與 Python `raise` 的 OSError 子類別相同（session `ATTACH_FAILED:<名>`）。
#[test]
fn open_errno_maps_like_python() {
    assert!(matches!(
        open_with(Some(libc::ENOENT), None),
        Err(OpenError::NotFound(_))
    ));
    match open_with(Some(libc::EBUSY), None) {
        Err(OpenError::Held(rep)) => assert!(rep.holder_unknown, "pids 空時 holder_unknown"),
        other => panic!("EBUSY 應為 Held，實際 {other:?}"),
    }
    for (code, py_name) in [
        (libc::EACCES, "PermissionError"),
        (libc::EPERM, "PermissionError"),
        (libc::ENODEV, "OSError"),
        (libc::ENXIO, "OSError"),
    ] {
        match open_with(Some(code), None) {
            Err(OpenError::Other(e)) => {
                assert_eq!(e.os_code, Some(code), "errno {code}");
                assert_eq!(e.py_exc_name(), py_name, "errno {code}");
            }
            other => panic!("errno {code} 應為 Other，實際 {other:?}"),
        }
    }
}

/// #63：flock 失敗 errno ∈ {EACCES, EAGAIN, EWOULDBLOCK} → `Held`；其他 → `Other`；兩者皆先 close(fd)、不 TIOCNXCL。
#[test]
fn flock_eacces_is_held() {
    for code in [libc::EACCES, libc::EAGAIN, libc::EWOULDBLOCK] {
        let mut ops = FakeOps {
            flock_err: Some(code),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/ttyUSB0") {
            Err(OpenError::Held(_)) => {}
            other => panic!("flock errno {code} 應為 Held，實際 {other:?}"),
        }
        assert!(ops.ops.contains(&OpenOp::CloseFd));
        assert!(!ops.ops.contains(&OpenOp::Nxcl));
    }
    assert!(matches!(
        open_with(None, Some(libc::EBADF)),
        Err(OpenError::Other(_))
    ));
}

/// #65：衝突路徑（EBUSY／flock 失敗）在掃描無 pids 時 `holder_unknown:true`（`_conflict_holder_report`），
/// 掃描到 pids 時 `holder_unknown:false`。
#[test]
fn conflict_holder_unknown_when_no_pids() {
    for (open_err, flock_err) in [(Some(libc::EBUSY), None), (None, Some(libc::EWOULDBLOCK))] {
        let mut ops = FakeOps {
            open_err,
            flock_err,
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/ttyUSB0") {
            Err(OpenError::Held(rep)) => {
                assert!(rep.pids.is_empty());
                assert!(
                    rep.holder_unknown,
                    "status ok 但 pids 空 → 仍須 holder_unknown"
                );
            }
            other => panic!("應為 Held，實際 {other:?}"),
        }
        let mut ops = FakeOps {
            open_err,
            flock_err,
            probe: Some(HolderReport {
                pids: vec![4242],
                holder_unknown: false,
                status: HolderScanStatus::Permission,
            }),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/ttyUSB0") {
            Err(OpenError::Held(rep)) => {
                assert_eq!(rep.pids, vec![4242]);
                assert!(!rep.holder_unknown);
            }
            other => panic!("應為 Held，實際 {other:?}"),
        }
    }
}
