//! P7 審查 sol-backfill p7-20261028 #3：事件分類旗標（`BridgeQueries::active_cmd_id_for` 經 fence 讀取的
//! `foreground_busy`）只計前景命令。Python `active_cmd_id_for` 回 `"foreground" if session.foreground_busy`，
//! 而 `interactive_send`／`interactive_open`（無命令）不經 `_begin_foreground_operation_locked`，TX 進行中
//! `foreground_busy` 仍為 False：`spontaneous` 規則觸發、`command_output` 不觸發。
#![cfg(feature = "test-fakes")]

mod common;
mod support;

use support::*;
use sw_core::session_core::{Effect, SessionRequest};

fn published(h: &CoreHarness) -> bool {
    h.fence.with(|f| f.foreground_busy)
}

fn has_start_op(effects: &[Effect]) -> bool {
    effects.iter().any(|e| matches!(e, Effect::StartOp { .. }))
}

#[test]
fn interactive_send_tx_in_flight_is_not_foreground_for_events() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let open = h
        .rpc(SessionRequest::InteractiveOpen {
            owner: "agent:a".into(),
            timeout_s: 30.0,
            command: String::new(),
            allow_attached: false,
        })
        .expect("interactive_open");
    let id = open["interactive_id"].as_str().expect("interactive_id").to_string();
    assert!(!published(&h), "無命令的 interactive_open 不是前景命令");
    // interactive_send 的 TX 尚未完成（OpFinished 未投遞）時收到 RX。
    let (_reply, effects) = h.rpc_deferred(SessionRequest::InteractiveSend {
        interactive_id: id,
        data: "ls".into(),
        encoding: "plain".into(),
    });
    assert!(has_start_op(&effects), "interactive_send 應起 TX op");
    assert!(!published(&h), "interactive send TX 期間 active_cmd_id_for 須為 None（Python foreground_busy False）");
    h.rx(b"hit\n");
    assert!(!published(&h), "RX 後仍不得分類為 foreground");
}

#[test]
fn foreground_command_in_flight_is_foreground_for_events() {
    // 對照：前景命令 TX／等待期間 Python `foreground_busy` 為 True → `command_output` scope。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (resp, effects) = h.rpc_full(SessionRequest::Submit(agent_submit("echo hi")));
    resp.expect("submit");
    assert!(has_start_op(&effects), "前景命令應出列");
    assert!(published(&h), "前景命令進行中 active_cmd_id_for 須為 \"foreground\"");
    h.rx(b"hit\n");
    assert!(published(&h));
}
