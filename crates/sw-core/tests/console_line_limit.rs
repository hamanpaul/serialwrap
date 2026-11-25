//! OD-111：Rust-only 行為，`command.submit` 遵守 session profile 的 `max_console_line_chars`（console 單行上限）；
//! Python 基線不實作此限制，以下斷言只驗證 Rust 契約：
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
fn rpc_submit_uses_profile_after_set_profile() {
    // rematerialize 以 SetProfile 更新 actor 後，後續 RPC 必須使用現行 profile 的上限。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let reply = h
        .rpc(SessionRequest::SetProfile {
            profile: profile_of(LIMITED_PROFILE),
            profile_source: "yaml-target".into(),
        })
        .expect("SetProfile 回覆");
    assert_eq!(reply["ok"], PyValue::Bool(true));

    h.to_ready();
    let before = h.tx_effect_count();
    let (reply, fx) = h.rpc_full(SessionRequest::Submit(agent_submit(&"A".repeat(21))));
    let r = reply.expect("submit 回覆");
    assert_eq!(r["error_code"], PyValue::from("CMD_TOO_LONG"));
    assert_eq!(r["limit"], PyValue::from(20i64));
    assert!(started_commands(&fx).is_empty(), "上限須在入列前使用現行 profile 判定");
    assert_eq!(h.tx_effect_count(), before, "拒絕後零 TX");
}

#[test]
fn zero_or_negative_line_limit_rejects_nonempty_command() {
    // 正常 YAML loader 僅接受正整數；這裡鎖住 arbiter 防線，避免非正值被當成未設上限。
    for limit in [0, -1] {
        let mut profile = profile_of(LIMITED_PROFILE);
        profile.template.max_console_line_chars = Some(sw_proto::pyjson::PyInt::Small(limit));
        let mut h = CoreHarness::new_with_profile(profile);
        h.to_ready();
        let before = h.tx_effect_count();
        let (reply, fx) = h.rpc_full(SessionRequest::Submit(agent_submit("A")));
        let r = reply.expect("submit 回覆");
        assert_eq!(r["error_code"], PyValue::from("CMD_TOO_LONG"), "limit={limit}");
        assert_eq!(r["limit"], PyValue::from(limit as i64), "limit={limit}");
        assert!(started_commands(&fx).is_empty(), "limit={limit}: 不得入列");
        assert_eq!(h.tx_effect_count(), before, "limit={limit}: 零 TX");
    }
}

#[test]
fn human_console_line_is_not_limited() {
    // human console 的整行輸入走 line editor → `CommandRegistry::admit`（不帶 profile 上限），照常提交。
    let mut h = CoreHarness::new(LIMITED_PROFILE);
    h.to_ready();
    h.connect_stream_console("c1", ConsoleTransportKind::TcpTelnet);
    h.connect_stream_console("c2", ConsoleTransportKind::TcpTelnet);
    // BC-23：第二個 TCP console 的 line 會被第一個 raw owner 的 lease 擋下；先明確關閉 lease，
    // 再驗證 human line 路徑不套用 profile 上限。
    let interactive_id = h
        .core
        .active_lease()
        .expect("第一個 TCP console 建立 human lease")
        .interactive_id
        .clone();
    let closed = h
        .rpc(SessionRequest::InteractiveClose { interactive_id })
        .expect("interactive-close 回覆");
    assert_eq!(closed["ok"], PyValue::Bool(true));
    assert!(h.core.active_lease().is_none(), "line 測試不應保留 raw owner lease");
    let line = "A".repeat(30);
    let fx = h.input("c2", format!("{line}\r").as_bytes());
    assert_eq!(started_commands(&fx), vec![line]);
    assert!(
        !h.warns().iter().any(|w| w.contains("CMD_TOO_LONG")),
        "human console 不得被 profile 上限拒絕：{:?}",
        h.warns()
    );
}
