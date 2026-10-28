//! P6 Task 9：按鍵至回顯延遲與輸入洪流下的 control 延遲（`ActorRig`＋ConsoleHub＋FakeSerial loopback）。
//!
//! 規格 §1.2 SLO 的 in-process 回歸門檻；實體 115200 loopback 屬 BLOCKED-OWNER #7。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::{Duration, Instant};

use support::*;
use sw_core::session_core::SessionRequest;
use sw_proto::pyjson::PyValue;

fn p99(mut v: Vec<Duration>) -> Duration {
    v.sort();
    v[(v.len() * 99 / 100).min(v.len() - 1)]
}

/// 一個按鍵樣本：必須先經 UART TX（DUT 收到該 byte），console 才可接受回顯；本地回顯（line mode）不算。
/// 回 `Err` 表示 deadline 內 UART 未收到該按鍵或 console 未見回顯。
fn echo_sample(
    rig: &ActorRig,
    console: &sw_pal::fakes::console::FakeConsoleRef,
    key: u8,
    deadline: Duration,
) -> Result<Duration, String> {
    let before = console.written_len();
    let tx_before = rig.dev.tx_len();
    let t = Instant::now();
    console.push_input(&[key]);
    let limit = t + deadline;
    let mut uart_seen = false;
    loop {
        if !uart_seen && rig.dev.tx_since(tx_before).contains(&key) {
            uart_seen = true;
        }
        if uart_seen {
            let w = console.written();
            if w.len() > before && w[before..].contains(&key) {
                return Ok(t.elapsed());
            }
        }
        if Instant::now() >= limit {
            return Err(format!("uart_seen={uart_seen}：UART TX／loopback RX 未在 {deadline:?} 內完成"));
        }
        std::thread::yield_now();
    }
}

#[test]
fn console_keystroke_echo_p99_under_10ms() {
    let rig = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let c1 = rig.attach_pty_console("c1");
    rig.dev.set_loopback(true); // DUT 端收到的每個 byte 立即回送為 RX
    std::thread::sleep(Duration::from_millis(50));
    // sol 補審 Medium（17270a4）：先確認 c1 為 raw owner，量到的才是 UART loopback 而非 line editor 本地回顯。
    let list = rig.request(SessionRequest::ConsoleList);
    let rows = match list.get("consoles") {
        Some(PyValue::List(rows)) => rows.clone(),
        other => panic!("console_list：{other:?}"),
    };
    let owner_is_c1 = rows.iter().any(|row| match row {
        PyValue::Dict(d) => {
            d.get("client_id") == Some(&PyValue::from("c1")) && d.get("interactive_owner") == Some(&PyValue::Bool(true))
        }
        _ => false,
    });
    assert!(owner_is_c1, "c1 須為 raw owner：{rows:?}");
    let mut lat = Vec::new();
    for i in 0..500u32 {
        let key = b'a' + (i % 26) as u8;
        match echo_sample(&rig, &c1, key, Duration::from_secs(2)) {
            Ok(d) => lat.push(d),
            Err(e) => panic!("第 {i} 個按鍵：{e}"),
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let p = p99(lat);
    assert!(p < Duration::from_millis(10), "按鍵→回顯 p99={p:?}");
}

/// 負對照：line mode（第二個 console、非 raw owner）的本地回顯不經 UART，`echo_sample` 必須判為失敗——
/// 證明上面的 SLO 測試不會把本地回顯量成 DUT 回顯。
#[test]
fn console_echo_sample_rejects_local_echo() {
    let rig = ActorRig::new_with_hub(SHELL_PROFILE).ready();
    let _c1 = rig.attach_pty_console("c1");
    let c2 = rig.attach_pty_console("c2");
    rig.dev.set_loopback(true);
    std::thread::sleep(Duration::from_millis(50));
    let before = c2.written_len();
    let r = echo_sample(&rig, &c2, b'z', Duration::from_millis(300));
    assert!(r.is_err(), "line mode 本地回顯不得通過：{r:?}");
    assert!(c2.written()[before..].contains(&b'z'), "line editor 確實本地回顯（負對照前提）");
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
