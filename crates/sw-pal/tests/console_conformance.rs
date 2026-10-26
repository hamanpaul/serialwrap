//! P6 Task 8（07-slice22-console.md）：PAL console backends（PTY／TCP／ConsoleMux／com0com）conformance。
//!
//! 需要資源者依 arch §9.1 命名 `cap_<resource>__<name>` 並 `#[ignore = "cap:<resource>"]`；
//! `fake_console_suite` 只跑記憶體替身，不計原生 gate（OD-08）。
#![cfg(feature = "test-fakes")]
#![allow(non_snake_case)]

use std::io::{Read, Write};
use std::time::{Duration, Instant};

use sw_pal::console_mux::{select_console_kind, MuxEvent};
use sw_pal::fakes::console::{FakeConsoleFactory, FakeIo};
use sw_pal::{BackendKind, ConsoleIo, ConsoleKind, ConsoleListener, PalErrorKind, ReadOutcome};

fn tempfile_dir(tag: &str) -> std::path::PathBuf {
    // 每次呼叫建立唯一暫存目錄（pid＋計數器），測試結束不需清理
    static N: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let d = std::env::temp_dir().join(format!(
        "{tag}-{}-{}",
        std::process::id(),
        N.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// 建立 native listener＋一條已連線的 client／server 對。
fn tcp_pair() -> (
    Box<dyn ConsoleListener>,
    std::net::TcpStream,
    Box<dyn ConsoleIo>,
    String,
) {
    let p = sw_pal::native();
    let mut l = p.console.bind_tcp_console().unwrap();
    let c = std::net::TcpStream::connect(l.endpoint()).unwrap();
    let (io, peer) = sw_pal::testutil::wait_accept(&mut *l, Duration::from_secs(2));
    (l, c, io, peer)
}

/// 在 `deadline` 內重複 `wait`，直到出現符合條件的事件。
fn wait_for(
    mux: &mut dyn sw_pal::console_mux::ConsoleMux,
    deadline: Duration,
    pred: impl Fn(&MuxEvent) -> bool,
) -> Option<MuxEvent> {
    let start = Instant::now();
    while start.elapsed() < deadline {
        let evs = mux.wait(Duration::from_millis(100)).unwrap();
        if let Some(ev) = evs.into_iter().find(|e| pred(e)) {
            return Some(ev);
        }
    }
    None
}

// ---------------------------------------------------------------------------
// POSIX PTY（posix_pty）
// ---------------------------------------------------------------------------

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__pty_slave_is_raw_and_master_nonblocking() {
    // D-01：console PTY 不建 symlink。掃 PAL 實際可能落 symlink 的目錄（run dir／XDG runtime／temp／/dev／cwd），
    // 比對 create_pty 前後的快照，只看「新增且指向 slave_path」者（不受他人既有 symlink 干擾）。
    let dirs = sw_pal::testutil::d01_scan_dirs();
    let before = sw_pal::testutil::symlink_snapshot(&dirs);
    let p = sw_pal::native();
    let pair = p.console.create_pty().unwrap();
    assert!(sw_pal::testutil::is_dev_pts_path(&pair.slave_path));
    let t = sw_pal::testutil::tcgetattr_path(&pair.slave_path).unwrap();
    assert_eq!((t.iflag, t.oflag, t.lflag), (0, 0, 0));
    assert_eq!(
        t.cflag & (t.CREAD | t.CLOCAL | t.CS8),
        t.CREAD | t.CLOCAL | t.CS8
    );
    assert_eq!((t.vmin, t.vtime), (1, 0));
    assert!(sw_pal::testutil::master_is_nonblocking(&pair));
    let added = sw_pal::testutil::new_symlinks_to(&pair.slave_path, &dirs, &before);
    assert!(
        added.is_empty(),
        "D-01：PAL 不得為 console 建 symlink：{added:?}"
    );
}

/// L4：D-01 掃描必須在違規時紅——在掃描目錄（temp）植入一條指向目標的 symlink，偵測須報出；
/// 舊斷言只掃測試自建的空目錄，對同一違規恆真。
#[cfg(unix)]
#[test]
fn d01_symlink_scan_detects_planted_violation() {
    let dirs = sw_pal::testutil::d01_scan_dirs();
    let tmp = std::env::temp_dir();
    assert!(
        dirs.iter().any(|d| d == &tmp),
        "D-01 掃描須涵蓋 temp 目錄：{dirs:?}"
    );
    let work = tempfile_dir("sw-p6-d01");
    let target = work.join("fake-pts");
    std::fs::write(&target, b"").unwrap();
    let target = target.to_string_lossy().into_owned();
    let before = sw_pal::testutil::symlink_snapshot(&dirs);
    assert!(sw_pal::testutil::new_symlinks_to(&target, &dirs, &before).is_empty());
    let planted = tmp.join(format!("sw-p6-d01-link-{}", std::process::id()));
    let _ = std::fs::remove_file(&planted);
    std::os::unix::fs::symlink(&target, &planted).unwrap();
    let added = sw_pal::testutil::new_symlinks_to(&target, &dirs, &before);
    let _ = std::fs::remove_file(&planted);
    assert_eq!(added, vec![planted], "植入的 console symlink 必須被偵測到");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_close_does_not_eof_master() {
    // keepalive（06 §7.1）
    let p = sw_pal::native();
    let mut pair = p.console.create_pty().unwrap();
    let ext = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&pair.slave_path)
        .unwrap();
    drop(ext);
    let mut buf = [0u8; 16];
    assert!(matches!(
        pair.master
            .read(&mut buf, Some(Duration::from_millis(200)))
            .unwrap(),
        ReadOutcome::TimedOut
    ));
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__held_slave_paths_sees_external_holder() {
    let p = sw_pal::native();
    let pair = p.console.create_pty().unwrap();
    let held = p.console.held_slave_paths().expect("/proc 可讀");
    assert!(
        !held.contains(&pair.slave_path),
        "自身 pid 持有（keepalive）不算外部持有"
    );
    let f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&pair.slave_path)
        .unwrap();
    let mut child = std::process::Command::new("sleep")
        .arg("5")
        .stdin(std::process::Stdio::from(f))
        .spawn()
        .unwrap();
    let start = Instant::now();
    let mut seen = false;
    while start.elapsed() < Duration::from_secs(3) {
        if p.console
            .held_slave_paths()
            .is_some_and(|h| h.contains(&pair.slave_path))
        {
            seen = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let _ = child.kill();
    let _ = child.wait();
    assert!(seen, "子行程持有 slave 時集合須含 slave_path");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__try_write_full_returns_zero() {
    let p = sw_pal::native();
    let mut pair = p.console.create_pty().unwrap();
    let chunk = [b'x'; 8192];
    let mut zero = false;
    for _ in 0..4096 {
        match pair.master.try_write(&chunk) {
            Ok(0) => {
                zero = true;
                break;
            }
            Ok(_) => {}
            Err(e) => panic!("try_write 不得回 Err：{e:?}"),
        }
    }
    assert!(zero, "無人讀 slave 時最終須回 Ok(0)");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__has_external_peer_semantics() {
    let p = sw_pal::native();
    let pair = p.console.create_pty().unwrap();
    assert!(pair.master.has_external_peer(None));
    let other: std::collections::HashSet<String> = ["/dev/pts/999999".to_string()].into();
    assert!(!pair.master.has_external_peer(Some(&other)));
    let mine: std::collections::HashSet<String> = [pair.slave_path.clone()].into();
    assert!(pair.master.has_external_peer(Some(&mine)));
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__mux_mixed_pty_and_tcp() {
    let p = sw_pal::native();
    let mut mux = p.console.new_mux().unwrap();
    let pair = p.console.create_pty().unwrap();
    let keep = pair.slave_keepalive;
    let slave_path = pair.slave_path.clone();
    mux.add_io(1, pair.master).unwrap();
    let (_l, mut c, io, _peer) = tcp_pair();
    mux.add_io(2, io).unwrap();
    let mut ext = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&slave_path)
        .unwrap();
    ext.write_all(b"p").unwrap();
    assert_eq!(
        wait_for(&mut *mux, Duration::from_secs(2), |e| *e
            == MuxEvent::Readable(1)),
        Some(MuxEvent::Readable(1))
    );
    let mut buf = [0u8; 8];
    assert!(matches!(
        mux.read(1, &mut buf).unwrap(),
        ReadOutcome::Data(1)
    ));
    c.write_all(b"t").unwrap();
    assert_eq!(
        wait_for(&mut *mux, Duration::from_secs(2), |e| *e
            == MuxEvent::Readable(2)),
        Some(MuxEvent::Readable(2))
    );
    assert!(matches!(
        mux.read(2, &mut buf).unwrap(),
        ReadOutcome::Data(1)
    ));
    assert_eq!(buf[0], b't');
    drop(keep);
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__native_console_suite() {
    sw_pal::conformance::console_suite::console_suite(&*sw_pal::native().console);
}

// ---------------------------------------------------------------------------
// TCP（兩平台）
// ---------------------------------------------------------------------------

#[test]
fn tcp_console_binds_loopback_ephemeral_and_peer_eof() {
    // 兩平台都可跑（Windows 經 win-runner）
    let p = sw_pal::native();
    let mut l = p.console.bind_tcp_console().unwrap();
    let ep = l.endpoint();
    let (host, port) = ep.rsplit_once(':').unwrap();
    assert_eq!(host, "127.0.0.1");
    assert_ne!(port.parse::<u16>().unwrap(), 0);
    let c = std::net::TcpStream::connect(&ep).unwrap();
    let (mut io, peer) = sw_pal::testutil::wait_accept(&mut *l, Duration::from_secs(2));
    assert!(peer.starts_with("127.0.0.1:"));
    assert!(io.has_external_peer(None));
    drop(c);
    let mut buf = [0u8; 8];
    assert!(matches!(
        io.read(&mut buf, Some(Duration::from_secs(2))).unwrap(),
        ReadOutcome::Eof
    ));
}

#[test]
fn tcp_accepted_socket_has_nodelay() {
    let (_l, _c, io, _peer) = tcp_pair();
    assert!(sw_pal::testutil::nodelay(&*io));
}

/// parity-amendments-2026-10-26 #109（B:E-15）：accept 設 TCP_NODELAY；PAL 回傳的 peer 字串即 Python
/// `_accept_console_conn` 的 `label`（`f"{addr[0]}:{addr[1]}"`，client 端位址，非 `tcp://…`），listener
/// `endpoint()` 即 Python `_console_endpoint`（`f"{host}:{port}"`，hub 以它作 TCP console 的 `vtty`／`slave_path`，
/// `uart_io.py:1278-1305`）。hub 端的 label／vtty 組裝屬 Task 9。
#[test]
fn tcp_accept_sets_nodelay_and_label_shape() {
    let p = sw_pal::native();
    let mut l = p.console.bind_tcp_console().unwrap();
    let ep = l.endpoint();
    let c = std::net::TcpStream::connect(&ep).unwrap();
    let (io, peer) = sw_pal::testutil::wait_accept(&mut *l, Duration::from_secs(2));
    assert!(sw_pal::testutil::nodelay(&*io), "accept 後必須設 TCP_NODELAY");
    let local = c.local_addr().unwrap();
    assert_eq!(peer, format!("{}:{}", local.ip(), local.port()), "label 形狀與 Python 相同（client 的 ip:port）");
    let server = c.peer_addr().unwrap();
    assert_eq!(ep, format!("127.0.0.1:{}", server.port()), "endpoint 形狀與 Python 相同（host:port，無 scheme）");
    assert!(!peer.contains("://") && !ep.contains("://"));
}

#[test]
fn tcp_listener_reuseaddr() {
    let l = sw_pal::native().console.bind_tcp_console().unwrap();
    assert!(sw_pal::testutil::reuseaddr(&*l));
}

#[test]
fn tcp_try_write_never_blocks() {
    let (_l, _c, mut io, _peer) = tcp_pair();
    let chunk = vec![b'x'; 65536];
    for _ in 0..512 {
        let start = Instant::now();
        let n = io.try_write(&chunk).unwrap();
        assert!(
            start.elapsed() < Duration::from_millis(10),
            "try_write 須在 10 ms 內回傳"
        );
        if n == 0 {
            return;
        }
    }
    panic!("對端不讀時送出緩衝應在 32 MiB 內塞滿");
}

#[test]
fn tcp_wouldblock_maps_to_zero() {
    let (_l, _c, mut io, _peer) = tcp_pair();
    let chunk = vec![b'y'; 65536];
    let mut zero = false;
    for _ in 0..512 {
        match io.try_write(&chunk) {
            Ok(0) => {
                zero = true;
                break;
            }
            Ok(_) => {}
            Err(e) => panic!("WouldBlock 不得回 Err：{e:?}"),
        }
    }
    assert!(zero);
}

#[test]
fn winsock_error_message_format() {
    // Windows 由 caps 判斷（不用 cfg）：`[WinError N] `；POSIX：`[Errno N] `。
    let p = sw_pal::native();
    let e = sw_pal::testutil::tcp_connect_closed_port_error();
    if p.caps.native_backend == BackendKind::Win {
        assert!(e.py_message.starts_with("[WinError "), "{}", e.py_message);
    } else {
        assert!(e.py_message.starts_with("[Errno "), "{}", e.py_message);
    }
}

// ---------------------------------------------------------------------------
// ConsoleMux（兩平台）
// ---------------------------------------------------------------------------

#[test]
fn mux_waker_wakes_wait() {
    let p = sw_pal::native();
    let mut mux = p.console.new_mux().unwrap();
    let w = mux.waker();
    let start = Instant::now();
    let t = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(5));
        w.wake();
    });
    let evs = mux.wait(Duration::from_secs(5)).unwrap();
    assert!(
        start.elapsed() < Duration::from_millis(100),
        "{:?}",
        start.elapsed()
    );
    assert_eq!(evs, vec![MuxEvent::Woken]);
    t.join().unwrap();
}

#[test]
fn mux_reports_readable_and_hangup() {
    let p = sw_pal::native();
    let mut mux = p.console.new_mux().unwrap();
    let (_l, mut c, io, _peer) = tcp_pair();
    mux.add_io(7, io).unwrap();
    c.write_all(b"hi").unwrap();
    assert_eq!(
        wait_for(&mut *mux, Duration::from_secs(2), |e| *e
            == MuxEvent::Readable(7)),
        Some(MuxEvent::Readable(7))
    );
    let mut buf = [0u8; 8];
    assert!(matches!(
        mux.read(7, &mut buf).unwrap(),
        ReadOutcome::Data(2)
    ));
    assert_eq!(&buf[..2], b"hi");
    assert!(
        matches!(mux.read(7, &mut buf).unwrap(), ReadOutcome::TimedOut),
        "EAGAIN → TimedOut"
    );
    drop(c);
    let ev = wait_for(&mut *mux, Duration::from_secs(2), |e| {
        matches!(e, MuxEvent::Readable(7) | MuxEvent::Hangup(7))
    });
    match ev {
        Some(MuxEvent::Hangup(7)) => {}
        Some(MuxEvent::Readable(7)) => {
            assert!(matches!(mux.read(7, &mut buf).unwrap(), ReadOutcome::Eof))
        }
        other => panic!("預期 Hangup 或 Readable，得 {other:?}"),
    }
}

#[test]
fn mux_write_interest_toggles() {
    let p = sw_pal::native();
    let mut mux = p.console.new_mux().unwrap();
    let (_l, _c, io, _peer) = tcp_pair();
    mux.add_io(3, io).unwrap();
    mux.set_write_interest(3, true);
    assert_eq!(
        wait_for(&mut *mux, Duration::from_secs(1), |e| *e
            == MuxEvent::Writable(3)),
        Some(MuxEvent::Writable(3))
    );
    mux.set_write_interest(3, false);
    let evs = mux.wait(Duration::from_millis(100)).unwrap();
    assert!(!evs.contains(&MuxEvent::Writable(3)), "{evs:?}");
    assert!(mux.try_write(3, b"ok").unwrap() > 0);
}

#[test]
fn listener_readable_reports_acceptable() {
    let p = sw_pal::native();
    let mut mux = p.console.new_mux().unwrap();
    let l = p.console.bind_tcp_console().unwrap();
    let ep = l.endpoint();
    mux.add_listener(9, l).unwrap();
    let _c = std::net::TcpStream::connect(&ep).unwrap();
    assert_eq!(
        wait_for(&mut *mux, Duration::from_secs(2), |e| *e
            == MuxEvent::Acceptable(9)),
        Some(MuxEvent::Acceptable(9))
    );
    let (io, peer) = mux.accept(9).unwrap().expect("排隊中的連線");
    assert!(peer.starts_with("127.0.0.1:"));
    assert!(io.has_external_peer(None));
    assert!(mux.accept(9).unwrap().is_none(), "佇列已空 → None");
    assert!(matches!(
        mux.remove(9),
        Some(sw_pal::console_mux::MuxEntry::Listener(_))
    ));
}

#[test]
fn mux_handle_from_foreign_pal_rejected() {
    let p = sw_pal::native();
    let mut mux = p.console.new_mux().unwrap();
    let e = mux.add_io(1, Box::new(FakeIo::unbounded())).unwrap_err();
    assert_eq!(e.kind, PalErrorKind::Unsupported);
    // 反向：native io 加進 fake mux 亦拒絕。
    let fake = FakeConsoleFactory::new(ConsoleKind::TcpTelnet);
    let mut fmux = sw_pal::console_pty::ConsoleTransportFactory::new_mux(&fake).unwrap();
    let (_l, _c, io, _peer) = tcp_pair();
    assert_eq!(
        fmux.add_io(1, io).unwrap_err().kind,
        PalErrorKind::Unsupported
    );
}

// ---------------------------------------------------------------------------
// SC-28 選擇、com0com
// ---------------------------------------------------------------------------

#[test]
fn select_console_kind_table() {
    assert_eq!(select_console_kind(None, true), (ConsoleKind::Pty, None));
    assert_eq!(
        select_console_kind(Some("tcp"), true),
        (ConsoleKind::TcpTelnet, None)
    );
    assert_eq!(
        select_console_kind(Some(" PTY "), true),
        (ConsoleKind::Pty, None)
    );
    assert_eq!(
        select_console_kind(Some("pty"), false),
        (
            ConsoleKind::TcpTelnet,
            Some("SERIALWRAP_CONSOLE_BACKEND=pty 在此平台不支援，改用 tcp")
        )
    );
    assert_eq!(
        select_console_kind(Some("bogus"), true),
        (ConsoleKind::Pty, None)
    );
    assert_eq!(
        select_console_kind(Some(""), false),
        (ConsoleKind::TcpTelnet, None)
    );
    assert_eq!(
        select_console_kind(Some("AUTO"), false),
        (ConsoleKind::TcpTelnet, None)
    );
    assert_eq!(
        select_console_kind(Some("Tcp\n"), false),
        (ConsoleKind::TcpTelnet, None)
    );
}

#[test]
fn com0com_unsupported_on_posix() {
    // 只在 caps.com0com_console==false 的產物判定（不用 cfg）。
    let p = sw_pal::native();
    if p.caps.com0com_console {
        return;
    }
    let e = p
        .console
        .open_com0com_console("COM21")
        .err()
        .expect("須回 Err");
    assert_eq!(e.kind, PalErrorKind::Unsupported);
}

fn console_pair() -> (String, String) {
    let raw = std::env::var("SW_TEST_CONSOLE_PAIR")
        .expect("windows_com 測試需設定 SW_TEST_CONSOLE_PAIR=COMa:COMb（Console-side pair）");
    let (a, b) = raw
        .split_once(':')
        .expect("SW_TEST_CONSOLE_PAIR 格式須為 COMa:COMb");
    (a.to_string(), b.to_string())
}

fn open_peer(port: &str) -> std::fs::File {
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(format!(r"\\.\{port}"))
        .unwrap()
}

#[test]
#[ignore = "cap:windows_com"]
fn cap_windows_com__console_com0com_roundtrip() {
    let (a, b) = console_pair();
    let p = sw_pal::native();
    assert!(p.caps.com0com_console);
    let io = p.console.open_com0com_console(&a).unwrap();
    let mut mux = p.console.new_mux().unwrap();
    mux.add_io(1, io).unwrap();
    let mut peer = open_peer(&b);
    peer.write_all(b"hi").unwrap();
    assert_eq!(
        wait_for(&mut *mux, Duration::from_secs(2), |e| *e
            == MuxEvent::Readable(1)),
        Some(MuxEvent::Readable(1))
    );
    let mut buf = [0u8; 8];
    assert!(matches!(
        mux.read(1, &mut buf).unwrap(),
        ReadOutcome::Data(2)
    ));
    assert_eq!(&buf[..2], b"hi");
    assert_eq!(mux.try_write(1, b"ok").unwrap(), 2);
    let mut got = [0u8; 2];
    peer.read_exact(&mut got).unwrap();
    assert_eq!(&got, b"ok");
}

#[test]
#[ignore = "cap:windows_com"]
fn cap_windows_com__console_com0com_exclusive() {
    let (a, _b) = console_pair();
    let p = sw_pal::native();
    let _io = p.console.open_com0com_console(&a).unwrap();
    let second = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(format!(r"\\.\{a}"));
    let err = second.expect_err("share mode 0：第二次開啟須失敗");
    assert_eq!(err.raw_os_error(), Some(5), "ERROR_ACCESS_DENIED");
}

#[test]
#[ignore = "cap:windows_com"]
fn cap_windows_com__console_com0com_no_modem_forward() {
    let (a, b) = console_pair();
    let p = sw_pal::native();
    let io = p.console.open_com0com_console(&a).unwrap();
    let _peer = open_peer(&b);
    sw_pal::testutil::toggle_peer_modem_lines(&b);
    let calls = sw_pal::testutil::comm_api_calls(&*io);
    assert_eq!(calls.set_comm_state, 1, "僅開啟時一次");
    assert_eq!(calls.escape_comm_function, 0);
}

// ---------------------------------------------------------------------------
// 記憶體替身（memory_only；不計原生 gate）
// ---------------------------------------------------------------------------

#[test]
fn fake_console_suite() {
    sw_pal::conformance::console_suite::console_suite(&FakeConsoleFactory::new(ConsoleKind::Pty));
    sw_pal::conformance::console_suite::console_suite(&FakeConsoleFactory::new(
        ConsoleKind::TcpTelnet,
    ));
}

#[test]
fn fake_io_modes_and_refs() {
    use sw_pal::console_pty::ConsoleTransportFactory;
    let f = FakeConsoleFactory::new(ConsoleKind::TcpTelnet);
    let mut l = f.bind_tcp_console().unwrap();
    assert!(l.endpoint().starts_with("127.0.0.1:"));
    let r = f.connect();
    let (mut io, peer) = l
        .accept(Duration::from_millis(100))
        .unwrap()
        .expect("fake 連線");
    assert!(peer.starts_with("127.0.0.1:"));
    assert_eq!(io.try_write(b"abc").unwrap(), 3);
    assert_eq!(r.written(), b"abc");
    r.push_input(b"in");
    let mut buf = [0u8; 8];
    assert!(matches!(
        io.read(&mut buf, Some(Duration::from_millis(100))).unwrap(),
        ReadOutcome::Data(2)
    ));
    assert!(!r.closed());
    io.close();
    assert!(r.closed());

    let stalled = FakeIo::stalled();
    let sref = stalled.reference();
    let mut stalled: Box<dyn ConsoleIo> = Box::new(stalled);
    assert_eq!(stalled.try_write(b"x").unwrap(), 0);
    assert_eq!(sref.written_len(), 0);

    let mut n = FakeIo::accept_n(4);
    assert_eq!(n.try_write(b"abcdef").unwrap(), 4);
    assert_eq!(n.try_write(b"g").unwrap(), 0);

    let mut bad = FakeIo::failing_read();
    assert!(bad.read(&mut buf, Some(Duration::ZERO)).is_err());

    f.set_held(Some(["/fake/pts/1".to_string()].into()));
    assert!(f.held_slave_paths().unwrap().contains("/fake/pts/1"));
    f.fail_next_com0com();
    assert!(f.open_com0com_console("COM40").is_err());
    assert!(f.open_com0com_console("COM40").is_ok());
}
// ---------------------------------------------------------------------------
// console reaper 的 holder 身分判定（#40／#64，B:E-109；Python `TestUartBridgeHeldPathsNamespace` 5 案）
// ---------------------------------------------------------------------------
//
// 背景（#R2）：容器內獨立 devpts instance 的 `/dev/pts/N` readlink 字串與主機相同、`st_rdev`（136:N）
// 亦相同，但 `(st_dev, st_ino)` 不同。`held_slave_paths()` 只在「fd 實際指向的 inode」與「該字串於本
// daemon 所見的 inode」相同時才計入。以假 `/proc` ＋注入的 readlink／stat 固定此情境（真機依 pty 配號而定）。

#[cfg(unix)]
mod reaper_ns {
    use std::collections::HashMap;
    use std::io;
    use std::path::{Path, PathBuf};

    use sw_pal::tty_identity::TtyIdentity;

    // 主機 devpts（st_dev=0:60）與容器 devpts（0:140、0:148）：同名 /dev/pts/7 實為不同裝置。
    pub const HOST_DEV: u64 = 60;
    pub const CONTAINER_DEV_A: u64 = 140;
    pub const CONTAINER_DEV_B: u64 = 148;
    pub const PTS7_RDEV: u64 = (136 << 8) | 7;
    pub const TTY: &str = "/dev/pts/7";

    pub enum FakeStat {
        Id(TtyIdentity),
        /// 被 stat 即 panic（驗證未對該路徑做 stat）。
        MustNotStat,
    }

    /// 假 /proc：每條 fd 的 readlink 字串與 stat 身分各自可控（對照 Python `FakeProcNs`）。
    pub struct FakeProcNs {
        pub root: PathBuf,
        links: HashMap<PathBuf, String>,
        stats: HashMap<PathBuf, FakeStat>,
    }

    pub fn chr(dev: u64, ino: u64, rdev: u64) -> TtyIdentity {
        TtyIdentity {
            dev,
            ino,
            rdev,
            is_char_device: true,
        }
    }

    impl FakeProcNs {
        pub fn new() -> FakeProcNs {
            let root = super::tempfile_dir("sw-p6-fakeproc").join("proc");
            std::fs::create_dir_all(&root).unwrap();
            let mut fake = FakeProcNs {
                root,
                links: HashMap::new(),
                stats: HashMap::new(),
            };
            // 主機 tty 的身分（TTY 不存在於 CI 主機，故一律由假表提供）。
            fake.set_path(TTY, FakeStat::Id(chr(HOST_DEV, 10, PTS7_RDEV)));
            fake
        }

        pub fn add_fd(&mut self, pid: u32, fd: u32, link: &str, stat: FakeStat) {
            let fd_dir = self.root.join(pid.to_string()).join("fd");
            std::fs::create_dir_all(&fd_dir).unwrap();
            let path = fd_dir.join(fd.to_string());
            std::fs::write(&path, b"").unwrap();
            self.links.insert(path.clone(), link.to_string());
            self.stats.insert(path, stat);
        }

        pub fn set_path(&mut self, path: &str, stat: FakeStat) {
            self.stats.insert(PathBuf::from(path), stat);
        }

        pub fn held(&self) -> Option<std::collections::HashSet<String>> {
            let readlink = |p: &Path| -> io::Result<PathBuf> {
                self.links
                    .get(p)
                    .map(PathBuf::from)
                    .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
            };
            let stat = |p: &Path| -> io::Result<TtyIdentity> {
                match self.stats.get(p) {
                    Some(FakeStat::Id(id)) => Ok(*id),
                    Some(FakeStat::MustNotStat) => {
                        panic!(
                            "stat must not be called for non-/dev links: {}",
                            p.display()
                        )
                    }
                    None => Err(io::Error::from(io::ErrorKind::NotFound)),
                }
            };
            sw_pal::proc_scan::posix::held_tty_paths_with(&self.root, &readlink, &stat)
        }
    }
}

#[cfg(unix)]
#[test]
fn console_reaper_ignores_other_namespace_same_name_pts() {
    use reaper_ns::*;
    let mut ns = FakeProcNs::new();
    ns.add_fd(
        4411,
        3,
        TTY,
        FakeStat::Id(chr(CONTAINER_DEV_B, 10, PTS7_RDEV)),
    );
    let held = ns.held().expect("假 /proc 可列舉");
    assert!(
        !held.contains(TTY),
        "他 namespace 同名 pts（rdev 相同、inode 不同）不得計為 peer"
    );
}

#[cfg(unix)]
#[test]
fn console_reaper_counts_same_inode_pts() {
    use reaper_ns::*;
    let mut ns = FakeProcNs::new();
    ns.add_fd(4412, 3, TTY, FakeStat::Id(chr(HOST_DEV, 10, PTS7_RDEV)));
    let held = ns.held().expect("假 /proc 可列舉");
    assert!(held.contains(TTY));
}

#[cfg(unix)]
#[test]
fn console_reaper_counts_non_devpts_mknod_alias() {
    use reaper_ns::*;
    let uart = "/dev/ttyS0";
    let uart_rdev = (4 << 8) | 64;
    let mut ns = FakeProcNs::new();
    ns.set_path(uart, FakeStat::Id(chr(HOST_DEV, 11, uart_rdev)));
    ns.add_fd(
        4413,
        3,
        uart,
        FakeStat::Id(chr(CONTAINER_DEV_A, 55, uart_rdev)),
    );
    let held = ns.held().expect("假 /proc 可列舉");
    assert!(
        held.contains(uart),
        "非 devpts 字元裝置的 mknod 別名（inode 不同、rdev 相同）仍計為 held"
    );
}

#[cfg(unix)]
#[test]
fn console_reaper_does_not_stat_non_dev_links() {
    use reaper_ns::*;
    let mut ns = FakeProcNs::new();
    ns.add_fd(4414, 4, "pipe:[12345]", FakeStat::MustNotStat);
    let held = ns.held().expect("假 /proc 可列舉");
    assert!(!held.contains("pipe:[12345]"));
}

#[cfg(unix)]
#[test]
fn console_reaper_missing_proc_returns_none() {
    let root = tempfile_dir("sw-p6-noproc").join("no-such-proc");
    let readlink = |p: &std::path::Path| std::fs::read_link(p);
    let stat = |p: &std::path::Path| sw_pal::tty_identity::stat_identity(p);
    assert!(sw_pal::proc_scan::posix::held_tty_paths_with(&root, &readlink, &stat).is_none());
}

/// 真實檔案系統：`/dev/` 下但本 namespace 無法 stat 的 link（身分取不到）不計入，
/// 舊版以 readlink 字串直接收錄（對照 Python #R2：stat 失敗即略過該 fd）。
#[cfg(unix)]
#[test]
fn console_reaper_skips_dev_link_without_identity() {
    let root = tempfile_dir("sw-p6-realproc");
    let fd_dir = root.join("4415").join("fd");
    std::fs::create_dir_all(&fd_dir).unwrap();
    let ghost = "/dev/pts/987654321";
    std::os::unix::fs::symlink(ghost, fd_dir.join("3")).unwrap();
    let held = sw_pal::proc_scan::PosixProcScan::with_root(&root)
        .held_fd_targets()
        .expect("假 /proc 可列舉");
    assert!(!held.contains(ghost), "無身分的同名字串不得計為 peer");
}
