//! #4：`command.submit` 遵守 session profile 的 `max_console_line_chars`（console 單行上限）。
//!
//! 斷言值以 Python 基線為準（`sw_core/arbiter.py` 的 `submit(max_console_line_chars=…)`、`sw_core/service.py`
//! 從 session profile 取上限；Python 測試 `tests/test_submit_console_line_limit.py::TestServiceUsesProfileLimit`）：
//! - RPC 路徑超過上限回 `CMD_TOO_LONG`（`limit` 為 profile 的值），零 TX、不入列；
//! - 等於上限照常執行；profile 未設時行為不變；
//! - human console 的整行輸入不經 service，不套用。

#![cfg(feature = "test-fakes")]

mod support;

use support::*;
use sw_core::human_console::ConsoleTransportKind;
use sw_core::op_runner::OpSpec;
use sw_core::session_core::{Effect, SessionRequest};
use sw_proto::pyjson::PyValue;

const LIMITED_PROFILE: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n    max_console_line_chars: 20\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";

fn started_commands(effects: &[Effect]) -> Vec<String> {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::StartOp {
                spec: OpSpec::Command(q),
                ..
            } => Some(q.req.command.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn rpc_submit_over_profile_limit_rejected_with_zero_tx() {
    let mut h = CoreHarness::new(LIMITED_PROFILE);
    h.to_ready();
    let before = h.tx_effect_count();
    let (reply, fx) = h.rpc_full(SessionRequest::Submit(agent_submit(&"A".repeat(21))));
    let r = reply.expect("submit 回覆");
    assert_eq!(r["ok"], PyValue::Bool(false));
    assert_eq!(r["error_code"], PyValue::from("CMD_TOO_LONG"));
    assert_eq!(r["cmd_length"], PyValue::from(21i64));
    assert_eq!(r["limit"], PyValue::from(20i64));
    assert!(
        r["hint"].as_str().unwrap_or("").contains("max_console_line_chars"),
        "{r:?}"
    );
    assert!(r.get("session").is_some(), "拒絕回應附 session（與 16 KB 上限同一路徑）");
    assert!(started_commands(&fx).is_empty(), "不得入列執行");
    assert_eq!(h.tx_effect_count(), before, "零 TX");
}

#[test]
fn rpc_submit_within_profile_limit_runs() {
    let mut h = CoreHarness::new(LIMITED_PROFILE);
    h.to_ready();
    let (reply, fx) = h.rpc_full(SessionRequest::Submit(agent_submit(&"A".repeat(20))));
    assert_eq!(reply.expect("submit 回覆")["ok"], PyValue::Bool(true));
    assert_eq!(started_commands(&fx), vec!["A".repeat(20)]);
}

#[test]
fn profile_without_limit_unchanged() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (reply, fx) = h.rpc_full(SessionRequest::Submit(agent_submit(&"A".repeat(600))));
    assert_eq!(reply.expect("submit 回覆")["ok"], PyValue::Bool(true));
    assert_eq!(started_commands(&fx), vec!["A".repeat(600)]);
}

#[test]
fn human_console_line_is_not_limited() {
    // human console 的整行輸入走 line editor → `CommandRegistry::admit`（不帶 profile 上限），照常提交。
    let mut h = CoreHarness::new(LIMITED_PROFILE);
    h.to_ready();
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    let line = "A".repeat(30);
    let fx = h.input("c2", format!("{line}\r").as_bytes());
    assert_eq!(started_commands(&fx), vec![line]);
    assert!(
        !h.warns().iter().any(|w| w.contains("CMD_TOO_LONG")),
        "human console 不得被 profile 上限拒絕：{:?}",
        h.warns()
    );
}
