#![cfg(feature = "test-fakes")]

mod common;
mod support;

use support::*;
use sw_core::session_core::SessionRequest;
use sw_proto::pyjson::PyValue;

fn self_test_req() -> SessionRequest {
    SessionRequest::SelfTest {
        timeout_s: 2.0,
        strict_human_lock: false,
    }
}

fn open_interactive(owner: &str) -> SessionRequest {
    SessionRequest::InteractiveOpen {
        owner: owner.to_string(),
        timeout_s: 30.0,
        command: String::new(),
        allow_attached: false,
    }
}

fn close_interactive(id: &str) -> SessionRequest {
    SessionRequest::InteractiveClose {
        interactive_id: id.to_string(),
    }
}

#[test]
fn foreground_command_busy_self_test_is_passive() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let cmd_id = h.submit_ok("sleep 20");
    h.wait_running(&cmd_id);
    let before_tx = h.dev.write_calls();
    let before_state = h.get_state();

    let r = h.request(self_test_req());
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["classification"], PyValue::from("SESSION_BUSY"));
    assert_eq!(r["recommended_action"], PyValue::from("wait"));
    assert!(r.get("probe_ok").is_none(), "busy self-test 不應帶 probe_ok: {r:?}");
    assert_eq!(h.dev.write_calls(), before_tx, "前景忙碌時不得新增 probe TX");
    assert_eq!(
        h.get_state()["ready_reconfirm_pending"],
        before_state["ready_reconfirm_pending"],
        "前景忙碌時不應改變 ready_reconfirm_pending"
    );
}

#[test]
fn background_capture_busy_self_test_is_passive() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"sleep 30", b"root@t:/# ");
    let cmd_id = h.submit_with("sleep 30", |req| {
        req.mode = "background".into();
        req.timeout_s = 30.0;
    });
    h.wait_running(&cmd_id);

    let before_tx = h.dev.write_calls();
    let before_state = h.get_state();
    let r = h.request(self_test_req());

    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["classification"], PyValue::from("SESSION_BUSY"));
    assert_eq!(r["recommended_action"], PyValue::from("wait"));
    assert!(r.get("probe_ok").is_none(), "active background capture 不應產生 probe_ok: {r:?}");
    assert_eq!(h.dev.write_calls(), before_tx, "背景 capture 活動時不得新增 probe TX");
    assert_eq!(
        h.get_state()["ready_reconfirm_pending"],
        before_state["ready_reconfirm_pending"],
        "背景 capture 活動時不應改變 ready_reconfirm_pending"
    );
}

#[test]
fn agent_lease_busy_self_test_is_passive() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let r = h.request(open_interactive("agent:a"));
    let interactive_id = r["interactive_id"].as_str().unwrap().to_string();
    let before_tx = h.dev.write_calls();
    let before_state = h.get_state();

    let r = h.request(self_test_req());
    assert_eq!(r["ok"], PyValue::Bool(true));
    assert_eq!(r["classification"], PyValue::from("SESSION_INTERACTIVE_BUSY"));
    assert_eq!(r["recommended_action"], PyValue::from("wait"));
    assert!(r.get("probe_ok").is_none(), "agent lease 活動時不應有 probe_ok: {r:?}");
    assert_eq!(h.dev.write_calls(), before_tx, "agent lease 活動時不得新增 probe TX");
    assert_eq!(
        h.get_state()["ready_reconfirm_pending"],
        before_state["ready_reconfirm_pending"],
        "agent lease 活動時不應改變 ready_reconfirm_pending"
    );

    let close = h.request(close_interactive(&interactive_id));
    assert_eq!(close["ok"], PyValue::Bool(true));
}
