//! console backend conformance（P6 Task 8）：同一套斷言跑在 fakes、POSIX PTY／TCP、Windows TCP 上。
//! 在 fakes 上通過不得計為原生 gate（OD-08）。需要對端連線的案例（peer EOF、NODELAY、Readable）在
//! `tests/console_conformance.rs` 以原生 socket 另測，本 suite 只放與對端實作無關的斷言。
use std::collections::HashSet;
use std::time::Duration;

use crate::console_mux::{MuxEntry, MuxEvent};
use crate::console_pty::ConsoleTransportFactory;
use crate::ConsoleKind;

/// 對某個 `ConsoleTransportFactory` 執行 console conformance（mux waker、PTY 基本契約、TCP listener）。
pub fn console_suite(factory: &dyn ConsoleTransportFactory) {
    // 1. mux：waker 連呼叫只回一次 Woken，讀乾後再等待為空。
    let mut mux = factory.new_mux().expect("new_mux");
    let w = mux.waker();
    w.wake();
    w.wake();
    assert_eq!(mux.wait(Duration::from_secs(1)).expect("wait"), vec![MuxEvent::Woken]);
    assert!(mux.wait(Duration::from_millis(20)).expect("wait").is_empty());

    // 2. PTY（只在 kind=Pty 時）：slave_ref 與 slave_path 一致、has_external_peer 依 held 判定、可加入 mux。
    if factory.kind() == ConsoleKind::Pty {
        let pair = factory.create_pty().expect("create_pty");
        assert!(!pair.slave_path.is_empty());
        assert_eq!(pair.slave_ref.path, pair.slave_path);
        assert!(pair.master.has_external_peer(None), "held 未知時視為仍有 peer（不誤剪）");
        let other: HashSet<String> = ["/nonexistent/pts/0".to_string()].into();
        assert!(!pair.master.has_external_peer(Some(&other)));
        let keep = pair.slave_keepalive;
        mux.add_io(1, pair.master).expect("add_io(pty)");
        assert!(matches!(mux.remove(1), Some(MuxEntry::Io(_))));
        drop(keep);
    }

    // 3. TCP listener：兩平台、兩種 kind 都可建立；每次 bind 取得不同 ephemeral port；無連線時 accept 為 None。
    let mut l1 = factory.bind_tcp_console().expect("bind_tcp_console");
    let l2 = factory.bind_tcp_console().expect("bind_tcp_console");
    for ep in [l1.endpoint(), l2.endpoint()] {
        let (host, port) = ep.rsplit_once(':').expect("host:port");
        assert_eq!(host, "127.0.0.1");
        assert_ne!(port.parse::<u16>().expect("port"), 0);
    }
    assert_ne!(l1.endpoint(), l2.endpoint());
    assert!(l1.accept(Duration::ZERO).expect("accept").is_none());
    mux.add_listener(2, l2).expect("add_listener");
    let evs = mux.wait(Duration::from_millis(20)).expect("wait");
    assert!(!evs.contains(&MuxEvent::Acceptable(2)), "{evs:?}");
    assert!(mux.accept(2).expect("accept").is_none());
    assert!(matches!(mux.remove(2), Some(MuxEntry::Listener(_))));
}
