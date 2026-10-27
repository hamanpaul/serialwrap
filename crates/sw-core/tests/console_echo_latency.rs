//! P6 Task 9：按鍵至回顯延遲與輸入洪流下的 control 延遲（`ActorRig`＋ConsoleHub＋FakeSerial loopback）。
//!
//! 規格 §1.2 SLO 的 in-process 回歸門檻；實體 115200 loopback 屬 BLOCKED-OWNER #7。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::{Duration, Instant};

use support::*;

fn p99(mut v: Vec<Duration>) -> Duration {
    v.sort();
    v[(v.len() * 99 / 100).min(v.len() - 1)]
}

#[test]
fn console_keystroke_echo_p99_under_10ms() {
    let rig = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let c1 = rig.attach_pty_console("c1");
    rig.dev.set_loopback(true); // DUT 端收到的每個 byte 立即回送為 RX
    std::thread::sleep(Duration::from_millis(50));
    let mut lat = Vec::new();
    for i in 0..500u32 {
        let before = c1.written_len();
        let key = [b'a' + (i % 26) as u8];
        let t = Instant::now();
        c1.push_input(&key);
        let deadline = t + Duration::from_secs(2);
        loop {
            let w = c1.written();
            if w.len() > before && w[before..].contains(&key[0]) {
                break;
            }
            assert!(Instant::now() < deadline, "第 {i} 個按鍵未回顯");
            std::thread::yield_now();
        }
        lat.push(t.elapsed());
        std::thread::sleep(Duration::from_millis(5));
    }
    let p = p99(lat);
    assert!(p < Duration::from_millis(10), "按鍵→回顯 p99={p:?}");
}

#[test]
fn console_input_flood_keeps_control_latency() {
    let rig = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let c1 = rig.attach_pty_console("c1");
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let flood = {
        let c1 = c1.clone();
        let stop = stop.clone();
        std::thread::spawn(move || {
            // 約 1 MiB/s：每 8 ms 8 KiB
            let chunk = vec![b'f'; 8192];
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                c1.push_input(&chunk);
                std::thread::sleep(Duration::from_millis(8));
            }
        })
    };
    let mut lat = Vec::new();
    for _ in 0..100 {
        let t = Instant::now();
        let _ = rig.get_state();
        lat.push(t.elapsed());
        std::thread::sleep(Duration::from_millis(10));
    }
    stop.store(true, std::sync::atomic::Ordering::Relaxed);
    flood.join().unwrap();
    let p = p99(lat);
    assert!(p <= Duration::from_millis(20), "輸入洪流下 get_state p99={p:?}");
}
