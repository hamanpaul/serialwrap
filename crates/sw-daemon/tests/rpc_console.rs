//! P6 Task 10：console RPC 表面（`session.console_attach`／`console_detach`／`console_list`）與 LeaseIndex 路由。
//!
//! in-process `ServiceRig`（真 `SessionManager`＋`Service`）；`ServiceRig::with_console_backend("pty")` 以
//! `RigPtyFactory` 替身建 PTY，產品 executor 經 ConsoleHub 與 human TX pump 接上 UART。
#![cfg(feature = "test-fakes")]
mod support;

use std::time::{Duration, Instant};

use support::*;
use sw_core::session_core::SessionState;
use sw_proto::pyjson::{dumps, PyDict, PyValue, RPC_WIRE};

fn j(d: &PyDict) -> String {
    dumps(&PyValue::Dict(d.clone()), &RPC_WIRE)
}

fn keys(d: &PyDict) -> Vec<String> {
    d.0.keys().map(|k| k.to_string()).collect()
}

fn s(d: &PyDict, k: &str) -> String {
    d.get(k).and_then(PyValue::as_str).unwrap_or_default().to_string()
}

fn attach(r: &ServiceRig, params: &str) -> PyDict {
    r.call("session.console_attach", params)
}

fn wait_for(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn attach_invalid_args() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, "{}");
    assert_eq!(j(&resp), format!(r#"{{"ok":false,"error_code":"INVALID_ARGS","version":"{}"}}"#, r.version));
}

#[test]
fn unknown_selector_shapes() {
    let r = ServiceRig::with_console_backend("pty");
    let want = format!(r#"{{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM9","version":"{}"}}"#, r.version);
    for (m, p) in [
        ("session.console_attach", r#"{"selector":"COM9"}"#),
        ("session.console_detach", r#"{"selector":"COM9","client_id":"abc"}"#),
        ("session.console_list", r#"{"selector":"COM9"}"#),
        ("session.interactive_open", r#"{"selector":"COM9"}"#),
    ] {
        assert_eq!(j(&r.call(m, p)), want, "{m}");
    }
}

#[test]
fn attach_selector_com_chain() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"com":"COM0"}"#);
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
}

#[test]
fn attach_label_coerced() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"selector":"COM0","label":5}"#);
    assert_eq!(s(&resp, "label"), "5");
}

#[test]
fn attach_label_falsy_uses_client_id() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"selector":"COM0","label":""}"#);
    assert_eq!(s(&resp, "label"), s(&resp, "client_id"));
}

#[test]
fn attach_pty_shape_first() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(
        keys(&resp),
        ["ok", "client_id", "label", "vtty", "interactive_session_id", "interactive_owner", "session", "version"],
        "{}",
        j(&resp)
    );
    let cid = s(&resp, "client_id");
    assert_eq!(cid.len(), 12);
    assert!(cid.chars().all(|c| c.is_ascii_hexdigit()));
    assert_eq!(s(&resp, "interactive_session_id").len(), 32);
    assert_eq!(resp.get("interactive_owner"), Some(&PyValue::Bool(true)));
    let created = r.console.as_ref().unwrap().created();
    assert_eq!(s(&resp, "vtty"), created[0].0);
}

#[test]
fn attach_pty_shape_second() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&resp), ["ok", "client_id", "label", "vtty", "session", "version"], "{}", j(&resp));
}

#[test]
fn attach_then_status_has_no_false_not_found() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    let iid = s(&resp, "interactive_session_id");
    let st = r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}"}}"#));
    assert_eq!(st.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&st));
}

#[test]
fn attach_not_ready_states() {
    let r = ServiceRig::with_console_backend("pty");
    let want = format!(r#"{{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0","version":"{}"}}"#, r.version);
    for st in [SessionState::Recovering, SessionState::Flashing, SessionState::Attaching] {
        r.mgr.force_session_state("COM0", st);
        let before = r.side_effect_fingerprint().2;
        assert_eq!(j(&attach(&r, r#"{"selector":"COM0"}"#)), want, "{st:?}");
        assert_eq!(r.side_effect_fingerprint().2, before, "拒絕零 UART 寫入");
    }
}

#[test]
fn attach_rejected_discards_pty() {
    let r = ServiceRig::with_console_backend("pty");
    r.mgr.force_session_state("COM0", SessionState::Recovering);
    attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(r.mgr.pending_pty_count(), 0, "拒絕時預建的 PTY 已丟棄");
}

#[test]
fn detach_missing_client_id() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = r.call("session.console_detach", r#"{"selector":"COM0"}"#);
    assert_eq!(j(&resp), format!(r#"{{"ok":false,"error_code":"INVALID_ARGS","version":"{}"}}"#, r.version));
}

#[test]
fn detach_unknown_client() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = r.call("session.console_detach", r#"{"selector":"COM0","client_id":"nope"}"#);
    assert_eq!(
        j(&resp),
        format!(r#"{{"ok":false,"error_code":"CONSOLE_NOT_FOUND","client_id":"nope","version":"{}"}}"#, r.version)
    );
}

#[test]
fn detach_ok_closes_own_lease() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    let cid = s(&resp, "client_id");
    let d = r.call("session.console_detach", &format!(r#"{{"selector":"COM0","client_id":"{cid}"}}"#));
    assert_eq!(keys(&d), ["ok", "client_id", "session", "version"], "{}", j(&d));
    let session = match d.get("session") {
        Some(PyValue::Dict(x)) => x.clone(),
        _ => panic!("session"),
    };
    assert_eq!(session.get("interactive_session_id"), Some(&PyValue::Null));
}

#[test]
fn list_sorted_by_label_then_id() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0","label":"b"}"#);
    attach(&r, r#"{"selector":"COM0","label":"a"}"#);
    let l = r.call("session.console_list", r#"{"selector":"COM0"}"#);
    assert_eq!(keys(&l), ["ok", "consoles", "session", "version"]);
    let Some(PyValue::List(rows)) = l.get("consoles") else { panic!("consoles") };
    let labels: Vec<String> = rows
        .iter()
        .map(|v| match v {
            PyValue::Dict(d) => {
                assert_eq!(keys(d), ["client_id", "label", "vtty", "interactive_owner"]);
                s(d, "label")
            }
            _ => panic!("row"),
        })
        .collect();
    assert_eq!(labels, ["a", "b"]);
}

#[test]
fn console_input_reaches_uart_and_rx_fans_out() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    let (_, io) = r.console.as_ref().unwrap().created()[0].clone();
    let dev = r.device(REAL_A);
    let _ = dev.take_tx();
    io.push_input(b"x");
    let mut seen = Vec::new();
    wait_for("human raw 按鍵寫到 UART", || {
        seen.extend(dev.take_tx());
        seen.contains(&b'x')
    });
    dev.push_rx(b"hello-from-dut");
    wait_for("RX 分流到 console", || {
        String::from_utf8_lossy(&io.written()).contains("hello-from-dut")
    });
}

#[test]
fn console_methods_no_longer_deferred() {
    let r = ServiceRig::with_console_backend("pty");
    let scope = std::fs::read_to_string("../../tests/acceptance/slice21_scope.yaml").unwrap();
    let still = support::deferred_methods(&scope, &["2.1", sw_core::SLICE]);
    assert!(still.iter().all(|m| !m.starts_with("session.console_")), "{still:?}");
    for m in ["session.console_attach", "session.console_detach", "session.console_list"] {
        let resp = r.call(m, r#"{"selector":"COM0","client_id":"x"}"#);
        assert_ne!(s(&resp, "error_code"), "METHOD_NOT_FOUND", "{m}");
    }
    for m in still {
        let resp = r.call(&m, "{}");
        assert_eq!(s(&resp, "error_code"), "METHOD_NOT_FOUND", "{m}");
    }
}

fn session_of(d: &PyDict) -> PyDict {
    match d.get("session") {
        Some(PyValue::Dict(x)) => x.clone(),
        _ => panic!("session：{}", j(d)),
    }
}

#[test]
fn session_public_vtty_is_cached() {
    let r = ServiceRig::with_console_backend("pty");
    let a = attach(&r, r#"{"selector":"COM0"}"#);
    let h1 = s(&a, "vtty");
    assert_eq!(s(&session_of(&a), "vtty"), h1, "首個 console 設定 vtty");
    let b = attach(&r, r#"{"selector":"COM0"}"#);
    assert_ne!(s(&b, "vtty"), h1);
    let st = r.session("COM0").unwrap();
    assert_eq!(s(&st, "vtty"), h1, "vtty 不隨第二個 console 改變（D-20）");
}

#[test]
fn session_public_console_fields_pty() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    attach(&r, r#"{"selector":"COM0"}"#);
    let st = r.session("COM0").unwrap();
    assert_eq!(st.get("console_count"), Some(&PyValue::from(2i64)), "{}", j(&st));
    assert!(st.get("console_endpoint").is_none(), "PTY 後端無 console_endpoint 鍵");
}

#[test]
fn list_prunes_stale_without_peer() {
    let r = ServiceRig::with_console_backend("pty");
    let a = attach(&r, r#"{"selector":"COM0","label":"a"}"#);
    attach(&r, r#"{"selector":"COM0","label":"b"}"#);
    let held: std::collections::HashSet<String> = [s(&a, "vtty")].into_iter().collect();
    r.console.as_ref().unwrap().fake.set_held(Some(held));
    r.clock.advance(Duration::from_millis(2100));
    let l = r.call("session.console_list", r#"{"selector":"COM0"}"#);
    let Some(PyValue::List(rows)) = l.get("consoles") else { panic!("consoles") };
    let labels: Vec<String> = rows
        .iter()
        .map(|v| match v {
            PyValue::Dict(d) => s(d, "label"),
            _ => panic!("row"),
        })
        .collect();
    assert_eq!(labels, ["a"], "非 primary、逾 2 s、無 peer 者被 prune（RPC 前 ConsoleScan）");
}
