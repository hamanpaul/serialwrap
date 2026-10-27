//! P6 Task 6：UART 寫入卡住時 actor 仍即時回應（07 D-15(5)；06 `ActorRig`，真 actor thread）。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::{Duration, Instant};

use support::*;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_core::session_core::SessionRequest;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_proto::pyjson::PyValue;

fn ready_with_owner() -> ActorRig {
    let rig = ActorRig::new(SHELL_PROFILE).ready();
    rig.send_control(ControlEvent::ConsoleConnected {
        client_id: "c1".into(),
        client_lifetime: 1,
        label: "127.0.0.1:50000".into(),
        vtty: "127.0.0.1:50000".into(),
        kind: ConsoleTransportKind::TcpTelnet,
    });
    let _ = rig.get_state(); // 同一 control sender FIFO：回覆前 ConsoleConnected 已處理
    rig
}

fn key(rig: &ActorRig, b: u8) {
    let epoch = rig.fence.with(|f| f.input_epoch);
    rig.data_sender()
        .send(DataEvent::ConsoleInputBytes {
            client_id: "c1".into(),
            client_lifetime: 1,
            input_epoch: epoch,
            bytes: vec![b],
        })
        .unwrap();
}

#[test]
fn blocked_uart_does_not_freeze_actor() {
    let rig = ready_with_owner();
    rig.dev.script_write(vec![FakeWriteStep::BlockForever]);
    for i in 0..100u8 {
        key(&rig, b'a' + (i % 26));
        if i == 0 {
            rig.dev.wait_blocked(); // 第一個按鍵確實以 HumanRaw 進到 OS write 並卡住
        }
        let t = Instant::now();
        let _ = rig.get_state();
        assert!(t.elapsed() < Duration::from_millis(50), "第 {i} 次 get_state 逾 50 ms");
        assert!(rig.tx_ledger_gauge().live_ops <= 1, "同時最多 1 筆 human job 在 TxWriter");
    }
    rig.dev.release_block();
}

#[test]
fn release_reaches_recovering_while_uart_blocked() {
    let rig = ready_with_owner();
    rig.dev.script_write(vec![FakeWriteStep::BlockForever]);
    for i in 0..100u8 {
        key(&rig, b'a' + (i % 26));
    }
    rig.dev.wait_blocked();
    let _ = rig.get_state();
    let _rx = rig.request_async(SessionRequest::Release { source: "agent".into(), reason: PyValue::Null });
    let deadline = Instant::now() + Duration::from_millis(3500);
    loop {
        let st = rig.get_state();
        if st.get("state").and_then(PyValue::as_str) == Some("RECOVERING") {
            break;
        }
        assert!(Instant::now() < deadline, "3.5 s 內應為 RECOVERING：{st:?}");
        std::thread::sleep(Duration::from_millis(5));
    }
    rig.dev.release_block();
}
