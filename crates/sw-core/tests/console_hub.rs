//! P6 Task 9：每 session 一條不阻塞的 ConsoleHub thread（07 D-05、D-12、D-17、D-18）。
#![cfg(feature = "test-fakes")]
mod support;

use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use support::hub::*;
use sw_core::human_console::hub::*;
use sw_core::human_console::out_ring::OutItem;
use sw_core::human_console::{ConsoleTransportKind, ScanSource};
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_pal::fakes::console::FakeIo;
use sw_pal::ConsoleKind;
use sw_proto::constants::{telnet::TELNET_GREETING, CONSOLE_HUB_RX_QUEUE, CONSOLE_OUT_RING_BYTES};

const IAC: u8 = 0xFF;
const WILL: u8 = 0xFB;
const DONT: u8 = 0xFE;
const NAWS: u8 = 31;

fn connected_cid(t: &HubHarness) -> String {
    t.control_events()
        .iter()
        .find_map(|e| match e {
            ControlEvent::ConsoleConnected { client_id, .. } => Some(client_id.clone()),
            _ => None,
        })
        .expect("ConsoleConnected")
}

fn input_events(t: &HubHarness) -> Vec<(String, Vec<u8>)> {
    t.data_events()
        .into_iter()
        .filter_map(|e| match e {
            DataEvent::ConsoleInputBytes { client_id, bytes, .. } => Some((client_id, bytes)),
            _ => None,
        })
        .collect()
}

// ---------------------------------------------------------------- 計畫內示例

#[test]
fn greeting_written_before_any_fanout_bytes() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let client = t.fake.connect(); // fake accept
    t.settle(); // accept 前到達的 RX 不屬於此 client（不回放），先確定已 accept
    for _ in 0..50 {
        assert!(t.reader_push(b"RXDATA"));
    }
    t.settle();
    let got = client.written();
    assert_eq!(&got[..12], &TELNET_GREETING[..]);
    assert!(got[12..].starts_with(b"RXDATA"));
    assert!(matches!(
        &t.control_events()[..],
        [ControlEvent::ConsoleConnected { kind: ConsoleTransportKind::TcpTelnet, .. }]
    ));
}

#[test]
fn slow_console_does_not_block_reader_or_other_console() {
    let t = HubHarness::pty("COM0");
    let slow = t.adopt_pty_with(FakeIo::stalled()); // try_write 永遠 Ok(0)
    let fast = t.adopt_pty_with(FakeIo::unbounded());
    let start = Instant::now();
    for _ in 0..384 {
        t.reader_push(&[b'x'; 8192]); // 3 MiB；reader 側一律 try_send
    }
    assert!(start.elapsed() < Duration::from_millis(200), "reader 不得因 console 背壓阻塞");
    t.settle();
    assert_eq!(
        fast.written_len(),
        384 * 8192 - t.status().rx_dropped_chunks.load(Ordering::Relaxed) as usize * 8192
    );
    assert!(t.ring_display_bytes(&slow.client_id()) <= CONSOLE_OUT_RING_BYTES);
}

#[test]
fn full_mailbox_pauses_reading_without_blocking_hub() {
    // D-17(1)
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c = t.fake.connect();
    t.settle();
    t.block_mailbox(true); // data lane try_send 一律 Full
    for _ in 0..200 {
        c.push_input(b"k");
        std::thread::sleep(Duration::from_millis(1));
    }
    let start = Instant::now();
    t.hub.close_all(); // critical 旁路仍被處理
    t.settle();
    assert!(start.elapsed() < Duration::from_millis(500));
    assert!(c.closed());
    t.block_mailbox(false);
    t.settle();
    // pending 上限 64（CONSOLE_HUB_PENDING_EVENTS）；harness 的 data lane 為 rendezvous，block 期間一律 Full。
    assert!(t.data_events().len() <= 64, "pending 有界：{}", t.data_events().len());
    assert!(t.warnings().iter().any(|w| w.contains("session mailbox 已滿")));
}

// ---------------------------------------------------------------- 表列

#[test]
fn telnet_reply_sent_even_in_flash() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c = t.fake.connect();
    t.settle();
    c.push_input(&[IAC, WILL, NAWS]);
    t.settle();
    assert!(c.written().ends_with(&[IAC, DONT, NAWS]));
    assert!(input_events(&t).is_empty(), "filter 後無資料 → 不送 ConsoleInputBytes");
}

#[test]
fn telnet_input_filtered_before_lane() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c = t.fake.connect();
    t.settle();
    c.push_input(b"ls\r\n");
    t.settle();
    let cid = connected_cid(&t);
    assert_eq!(input_events(&t), vec![(cid, b"ls\r".to_vec())]);
}

#[test]
fn input_tagged_with_epoch_snapshot_before_wait() {
    let t = HubHarness::pty("COM0");
    let c = t.adopt_pty_with(FakeIo::unbounded());
    t.hub.input_epoch.store(7, Ordering::Release);
    t.settle();
    t.stall(); // hub 已取快照、停在本輪處理（尚未讀取）
    c.push_input(b"a");
    t.hub.input_epoch.store(8, Ordering::Release);
    t.unstall();
    t.settle();
    let epochs: Vec<u64> = t
        .data_events()
        .into_iter()
        .filter_map(|e| match e {
            DataEvent::ConsoleInputBytes { input_epoch, .. } => Some(input_epoch),
            _ => None,
        })
        .collect();
    assert_eq!(epochs, vec![7], "快照在 mux.wait 之前（D-17(5)）");
}

#[test]
fn display_escaped_for_telnet_only() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let tcp = t.fake.connect();
    let pty = t.adopt_pty_with(FakeIo::unbounded());
    t.settle();
    t.reader_push(&[0xFF, 0x01]);
    t.settle();
    assert!(tcp.written().ends_with(&[0xFF, 0xFF, 0x01]));
    assert_eq!(pty.written(), vec![0xFF, 0x01]);
}

#[test]
fn echo_out_escaped_for_telnet() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c = t.fake.connect();
    t.settle();
    let cid = connected_cid(&t);
    t.hub
        .cmd
        .try_send(HubCmd::Out { client_id: cid, item: OutItem::Display { bytes: vec![0xFF], iac_escaped: false } })
        .unwrap();
    t.settle();
    assert!(c.written().ends_with(&[0xFF, 0xFF]));
}

#[test]
fn tcp_eof_emits_disconnected_on_data_lane() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c = t.fake.connect();
    t.settle();
    c.push_input(b"ab\r");
    c.hangup();
    t.settle();
    let cid = connected_cid(&t);
    let evs = t.data_events();
    assert!(matches!(&evs[..], [
        DataEvent::ConsoleInputBytes { client_id: a, bytes, .. },
        DataEvent::ConsoleDisconnected { client_id: b, .. },
    ] if *a == cid && *b == cid && bytes == b"ab\r"));
}

#[test]
fn pty_io_error_emits_disconnected() {
    let t = HubHarness::pty("COM0");
    let c = t.adopt_pty_with(FakeIo::failing_read());
    t.settle();
    let cid = c.client_id();
    assert!(t
        .data_events()
        .iter()
        .any(|e| matches!(e, DataEvent::ConsoleDisconnected { client_id, .. } if *client_id == cid)));
}

#[test]
fn peer_scan_every_3s() {
    let t = HubHarness::pty("COM0");
    t.settle();
    let mut times = Vec::new();
    for _ in 0..9 {
        t.advance(Duration::from_secs(1));
        t.settle();
    }
    for e in t.control_events() {
        if let ControlEvent::ConsoleScan { scan, source: ScanSource::Hub } = e {
            times.push(scan.taken_at);
        }
    }
    let base = t.start_mono();
    let rel: Vec<u64> = times.iter().map(|t| (t - base) / 1_000_000_000).collect();
    assert_eq!(rel, vec![3, 6, 9]);
}

#[test]
fn listener_restarted_with_new_port_per_bridge() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let first = t.status().endpoint.lock().unwrap().clone().unwrap();
    let c = t.fake.connect();
    t.settle();
    t.hub.cmd.try_send(HubCmd::StopListener).unwrap();
    t.settle();
    assert!(!t.status().listener_up.load(Ordering::Acquire));
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let second = t.status().endpoint.lock().unwrap().clone().unwrap();
    assert_ne!(first, second);
    assert!(!c.closed(), "既有 TCP client 保留");
    let vtty = t
        .control_events()
        .iter()
        .find_map(|e| match e {
            ControlEvent::ConsoleConnected { vtty, .. } => Some(vtty.clone()),
            _ => None,
        })
        .unwrap();
    assert_eq!(vtty, first, "其 vtty 仍為舊 endpoint 字串（UIO-12.8-c）");
}

#[test]
fn consoles_survive_conn_epoch_change() {
    let t = HubHarness::pty("COM0");
    let c1 = t.adopt_pty_with(FakeIo::unbounded());
    t.settle();
    // 06 的 epoch 變更只換 reader（hub 不隨 IoWorker 重建）：舊 reader 的 tap 丟棄、新 reader 取新的 tap。
    t.reader_push(b"old");
    t.settle();
    let new_reader_tap = t.hub.tap();
    assert!(new_reader_tap.push(b"new", &|_line: &str| {}));
    t.settle();
    assert!(!c1.closed());
    assert_eq!(c1.written(), b"oldnew".to_vec());
    assert!(
        t.data_events().iter().all(|e| !matches!(e, DataEvent::ConsoleDisconnected { .. })),
        "epoch 變更不產生 console 斷線"
    );
    // 端到端（真 epoch／IoWorker 重建、slave_path 不變）見 sw-daemon rpc_console::consoles_survive_bridge_rebuild。
}

#[test]
fn close_all_on_release() {
    let t = HubHarness::pty("COM0");
    let a = t.adopt_pty_with(FakeIo::unbounded());
    let b = t.adopt_pty_with(FakeIo::unbounded());
    t.settle();
    t.hub.close_all();
    t.settle();
    assert!(a.closed() && b.closed());
}

#[test]
fn critical_close_when_cmd_queue_full() {
    let t = HubHarness::pty("COM0");
    let c1 = t.adopt_pty_with(FakeIo::unbounded());
    t.settle();
    let cid = c1.client_id();
    t.stall();
    let mut sent = 0;
    while t.hub.cmd.try_send(HubCmd::Out { client_id: cid.clone(), item: OutItem::Display { bytes: b"z".to_vec(), iac_escaped: false } }).is_ok() {
        sent += 1;
    }
    assert!(sent >= 1);
    t.hub.close(&cid);
    t.unstall();
    t.settle();
    assert!(c1.closed(), "c1 仍被關閉");
}

#[test]
fn no_peer_pty_display_cleared_after_grace() {
    let t = HubHarness::pty("COM0");
    let c = t.adopt_pty_with(FakeIo::stalled());
    t.settle();
    t.fake.set_held(Some(Default::default())); // 無 holder
    t.reader_push(b"display");
    t.settle();
    assert!(t.ring_display_bytes(&c.client_id()) > 0);
    t.advance(Duration::from_millis(3000)); // peer scan（attach 後已過 2.0 s grace）
    t.settle();
    assert_eq!(t.ring_display_bytes(&c.client_id()), 0, "D-05：Display 清空");
}

#[test]
fn control_overflow_closes_console() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let deaf = t.fake.connect_with(FakeIo::stalled());
    let other = t.fake.connect();
    t.settle();
    for _ in 0..30_000 {
        deaf.push_input(&[IAC, WILL, NAWS]);
    }
    t.settle();
    assert!(deaf.closed());
    assert!(t.data_events().iter().any(|e| matches!(e, DataEvent::ConsoleDisconnected { .. })));
    assert_eq!(t.warnings().iter().filter(|w| w.contains("協商回覆累積超過 65536 bytes")).count(), 1);
    assert!(!other.closed());
}

#[test]
fn telnet_accept_paused_at_32() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let clients: Vec<_> = (0..33).map(|_| t.fake.connect()).collect();
    t.settle();
    for c in &clients[..32] {
        assert!(c.written().starts_with(&TELNET_GREETING));
    }
    assert!(clients[32].written().is_empty(), "第 33 個未被 accept");
    assert_eq!(t.warnings().iter().filter(|w| w.contains("console 數已達 32")).count(), 1);
    t.hub.close(&connected_cid(&t));
    t.settle();
    assert!(clients[32].written().starts_with(&TELNET_GREETING), "關閉一個後被 accept");
}

#[test]
fn hub_rx_queue_full_drops_and_counts() {
    let t = HubHarness::pty("COM0");
    t.settle();
    t.stall();
    for _ in 0..300 {
        t.reader_push(b"x");
    }
    assert_eq!(t.status().rx_dropped_chunks.load(Ordering::Relaxed), (300 - CONSOLE_HUB_RX_QUEUE) as u64);
    assert_eq!(t.reader_warnings().iter().filter(|w| w.contains("fan-out 佇列已滿")).count(), 1);
    t.unstall();
    t.settle();
}

#[test]
fn loop_quota_bounds_drain() {
    let t = HubHarness::pty("COM0");
    let c = t.adopt_pty_with(FakeIo::unbounded());
    t.settle();
    t.stall();
    for _ in 0..CONSOLE_HUB_RX_QUEUE {
        t.reader_push(b"y");
    }
    t.hub.close_all();
    let loops0 = t.loops();
    t.unstall();
    let deadline = Instant::now() + Duration::from_secs(2);
    while !c.closed() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(t.loops() - loops0 <= 2, "close_all 在 2 輪迴圈內生效（D-17(2)）");
}

#[test]
fn com0com_open_failure_logs_and_continues() {
    let t = HubHarness::tcp("COM0");
    t.fake.fail_next_com0com();
    t.hub.cmd.try_send(HubCmd::OpenCom0com { port: "COM40".into() }).unwrap();
    t.settle();
    assert!(t.warnings().iter().any(|w| w.contains("serial console 開啟失敗") && w.contains("port=COM40")));
    t.hub.cmd.try_send(HubCmd::OpenCom0com { port: "COM41".into() }).unwrap();
    t.settle();
    assert_eq!(t.fake.com0com_opened().len(), 1, "hub 續行");
    assert!(t.control_events().iter().any(|e| matches!(e,
        ControlEvent::ConsoleConnected { kind: ConsoleTransportKind::Com0com, label, vtty, .. }
            if label == "serial:COM41" && vtty == "COM41")));
}

#[test]
fn vtty_alive_semantics() {
    let s = HubStatus::new();
    assert!(s.vtty_alive(Some("/dev/pts/1"), &|_| true, ConsoleKind::Pty));
    assert!(!s.vtty_alive(Some("/dev/pts/1"), &|_| false, ConsoleKind::Pty));
    assert!(!s.vtty_alive(None, &|_| true, ConsoleKind::Pty));
    s.listener_up.store(true, Ordering::Release);
    assert!(s.vtty_alive(None, &|_| false, ConsoleKind::TcpTelnet));
    s.listener_up.store(false, Ordering::Release);
    assert!(!s.vtty_alive(None, &|_| true, ConsoleKind::TcpTelnet));
}

#[test]
fn hub_mux_wait_error_backoff_20ms_then_rebuild() {
    // parity-amendments #37（`uart_io.py:1386-1390`）
    let t = HubHarness::pty("COM0");
    let c = t.adopt_pty_with(FakeIo::unbounded());
    t.settle();
    t.fake.fail_next_mux_waits(1);
    let start = Instant::now();
    t.reader_push(b"after");
    t.settle();
    assert!(start.elapsed() >= Duration::from_millis(20), "20 ms 退避");
    assert_eq!(c.written(), b"after".to_vec(), "不結束 hub、續行");
    t.hub.shutdown();
    t.join_within(Duration::from_secs(1));
}

#[test]
fn console_rx_handler_panic_isolated() {
    // parity-amendments #89：單一 console 處理 panic 寫 WARNING、其他 console 照常
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let bad = t.fake.connect();
    let good = t.fake.connect();
    t.settle();
    t.panic_next_input_handler();
    bad.push_input(b"boom");
    t.settle();
    good.push_input(b"ok");
    t.settle();
    assert!(t.warnings().iter().any(|w| w.contains("console RX handler 例外")));
    assert!(input_events(&t).iter().any(|(_, b)| b == b"ok"));
}

// ---------------------------------------------------------------------------
// p6-cloud 審查回歸（1f1fa67..4f710c3）
// ---------------------------------------------------------------------------

fn fill_pending(t: &HubHarness) -> sw_pal::fakes::console::FakeConsoleRef {
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c = t.fake.connect();
    t.settle();
    t.block_mailbox(true);
    for _ in 0..200 {
        c.push_input(b"k");
        std::thread::sleep(Duration::from_millis(1));
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while t.status().pending.load(Ordering::Acquire) < sw_proto::constants::CONSOLE_HUB_PENDING_EVENTS as u64 {
        assert!(Instant::now() < deadline, "pending 未滿");
        std::thread::sleep(Duration::from_millis(2));
    }
    c
}

/// R2-13（4f710c3）：pending 滿時暫停讀取不得讓 level-triggered poll 空轉（D-17(1)：10 ms 逾時重試）。
#[test]
fn full_pending_does_not_spin() {
    let t = HubHarness::tcp("COM0");
    let c = fill_pending(&t);
    c.push_input(b"more"); // fd 仍可讀
    let before = t.status().loops.load(Ordering::Acquire);
    std::thread::sleep(Duration::from_millis(300));
    let spins = t.status().loops.load(Ordering::Acquire) - before;
    assert!(spins <= 60, "300 ms 內 hub 迴圈 {spins} 輪（應以 ~10 ms 逾時重試，非空轉）");
    t.block_mailbox(false);
}

/// R2-14（4f710c3）：mailbox 長時間塞住時，週期掃描不得讓 pending 超過 64（D-18）。
#[test]
fn scan_does_not_grow_pending_beyond_cap() {
    let t = HubHarness::tcp("COM0");
    let _c = fill_pending(&t);
    for _ in 0..10 {
        t.clock.advance(Duration::from_secs(3)); // 每次推進觸發一次 peer scan
        t.hub.waker.wake();
        std::thread::sleep(Duration::from_millis(30));
    }
    let pending = t.status().pending.load(Ordering::Acquire);
    assert!(pending <= sw_proto::constants::CONSOLE_HUB_PENDING_EVENTS as u64, "pending={pending}");
    t.block_mailbox(false);
}

/// sol 補審 p6-20261028 High #2：`ConsoleConnected` 帶 hub 已處理的 close-all 世代（close_all 之後的 accept 為新世代）。
#[test]
fn console_connected_carries_processed_close_gen() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    t.settle();
    let c1 = t.fake.connect();
    t.settle();
    t.hub.close_all();
    t.settle();
    assert!(c1.closed());
    let _c2 = t.fake.connect();
    t.settle();
    let gens: Vec<u64> = t
        .control_events()
        .iter()
        .filter_map(|e| match e {
            ControlEvent::ConsoleConnected { close_gen, .. } => Some(*close_gen),
            _ => None,
        })
        .collect();
    assert_eq!(gens, vec![0, 1]);
}
