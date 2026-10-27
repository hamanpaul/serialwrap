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
    // created[0] 為 bridge 起來時建立的哨兵 primary（uart_io.py `_create_console_client("primary")`）。
    assert_eq!(s(&resp, "vtty"), created[1].0);
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
    assert_eq!(labels, ["a", "b", "primary"], "含哨兵 primary");
}

#[test]
fn console_input_reaches_uart_and_rx_fans_out() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
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
fn bridge_creates_internal_primary_sentinel() {
    let r = ServiceRig::with_console_backend("pty");
    let sentinel = r.console.as_ref().unwrap().created();
    assert_eq!(sentinel.len(), 1, "bridge 起來時建立一個哨兵 PTY");
    let st = r.session("COM0").unwrap();
    assert_eq!(s(&st, "vtty"), sentinel[0].0, "session.vtty_path = bridge.vtty_path（哨兵 primary）");
    assert_eq!(st.get("console_count"), Some(&PyValue::from(1i64)));
    let l = r.call("session.console_list", r#"{"selector":"COM0"}"#);
    let Some(PyValue::List(rows)) = l.get("consoles") else { panic!("consoles") };
    assert_eq!(rows.len(), 1);
    let PyValue::Dict(row) = &rows[0] else { panic!("row") };
    assert_eq!(s(row, "label"), "primary");
    assert_eq!(row.get("interactive_owner"), Some(&PyValue::Bool(false)));
}

#[test]
fn session_public_vtty_is_cached() {
    let r = ServiceRig::with_console_backend("pty");
    let primary = r.session("COM0").map(|st| s(&st, "vtty")).unwrap();
    let a = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(s(&session_of(&a), "vtty"), primary, "已有 vtty（哨兵）時 attach 不覆寫");
    attach(&r, r#"{"selector":"COM0"}"#);
    let st = r.session("COM0").unwrap();
    assert_eq!(s(&st, "vtty"), primary, "vtty 不隨後續 console 改變（D-20）");
}

#[test]
fn session_public_console_fields_pty() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    attach(&r, r#"{"selector":"COM0"}"#);
    let st = r.session("COM0").unwrap();
    assert_eq!(st.get("console_count"), Some(&PyValue::from(3i64)), "哨兵＋2 個 human：{}", j(&st));
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
    assert_eq!(labels, ["a", "primary"], "非 primary、逾 2 s、無 peer 者被 prune（RPC 前 ConsoleScan）");
}

#[test]
fn attach_interactive_owner_false_when_fg_busy() {
    let r = ServiceRig::with_console_backend("pty");
    // DUT 不回 prompt：命令一直在前景。
    let sub = r.call("command.submit", r#"{"selector":"COM0","cmd":"sleep 99","timeout_s":30}"#);
    let cmd_id = s(&sub, "cmd_id");
    wait_for("前景命令開始執行", || {
        let g = r.call("command.get", &format!(r#"{{"cmd_id":"{cmd_id}"}}"#));
        matches!(g.get("command"), Some(PyValue::Dict(c)) if s(c, "status") == "running")
    });
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(resp.get("interactive_owner"), Some(&PyValue::Bool(false)), "{}", j(&resp));
    assert_eq!(s(&resp, "interactive_session_id").len(), 32, "lease 仍開");
}

#[test]
fn detach_unknown_client_still_closes_own_lease() {
    let r = ServiceRig::with_console_backend("pty");
    let a = attach(&r, r#"{"selector":"COM0"}"#);
    let cid = s(&a, "client_id");
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
    io.hangup();
    wait_for("console 斷線後被移除", || {
        let l = r.call("session.console_list", r#"{"selector":"COM0"}"#);
        matches!(l.get("consoles"), Some(PyValue::List(v)) if v.len() == 1)
    });
    let d = r.call("session.console_detach", &format!(r#"{{"selector":"COM0","client_id":"{cid}"}}"#));
    assert_eq!(s(&d, "error_code"), "CONSOLE_NOT_FOUND");
    let st = r.session("COM0").unwrap();
    assert_eq!(st.get("interactive_session_id"), Some(&PyValue::Null), "ERR-7.5-b：仍關閉自己的 human lease");
}

#[test]
fn status_screen_chars_coerced_and_negative() {
    let r = ServiceRig::with_console_backend("pty");
    let a = attach(&r, r#"{"selector":"COM0"}"#);
    let iid = s(&a, "interactive_session_id");
    r.device(REAL_A).push_rx(b"0123456789abcdefghij");
    wait_for("RX 進視窗", || {
        s(&r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}","screen_chars":"10"}}"#)), "screen")
            .ends_with("abcdefghij")
    });
    let st = r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}","screen_chars":"10"}}"#));
    assert_eq!(s(&st, "screen"), "abcdefghij", "screen 最多 10 字元");
    let all = r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}","screen_chars":100000}}"#));
    let neg = r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}","screen_chars":-3}}"#));
    let full = s(&all, "screen");
    let expect: String = full.chars().skip(3).collect();
    assert_eq!(s(&neg, "screen"), expect, "負值＝去掉開頭 3 字元（text[3:]，H-08）");
}

#[test]
fn status_not_in_retry_whitelist() {
    use sw_proto::rpc_types::Method;
    assert!(!Method::parse("session.interactive_status").is_retryable_readonly());
    assert!(Method::parse("session.console_list").is_retryable_readonly());
}

#[test]
fn send_routes_by_lease_index() {
    let r = ServiceRig::two_shells();
    let o0 = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent:a"}"#);
    let o1 = r.call("session.interactive_open", r#"{"selector":"COM1","owner":"agent:b"}"#);
    let id1 = s(&o1, "interactive_id");
    assert!(!s(&o0, "interactive_id").is_empty() && !id1.is_empty(), "{} {}", j(&o0), j(&o1));
    let _ = r.device(REAL_A).take_tx();
    let _ = r.device(REAL_B).take_tx();
    let resp = r.call("session.interactive_send", &format!(r#"{{"interactive_id":"{id1}","data":"zz"}}"#));
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    let mut b = Vec::new();
    wait_for("COM1 收到 zz", || {
        b.extend(r.device(REAL_B).take_tx());
        b.windows(2).any(|w| w == b"zz")
    });
    assert!(!r.device(REAL_A).take_tx().windows(2).any(|w| w == b"zz"), "COM0 不得收到");
}

/// R2-9（17270a4）：bridge 重建（新 epoch、新 IoWorker）後 console 保留、vtty 不變，新 reader 的 RX 仍到該 console。
#[test]
fn consoles_survive_bridge_rebuild() {
    let r = ServiceRig::with_console_backend("pty");
    let a = attach(&r, r#"{"selector":"COM0","label":"h1"}"#);
    let cid = s(&a, "client_id");
    let vtty = s(&a, "vtty");
    let gen0 = r.session("COM0").unwrap().get("bridge_generation").cloned();
    let c = r.call("session.clear", r#"{"selector":"COM0"}"#);
    assert_eq!(c.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&c));
    // 重新 attach 的等待依 FakeClock 推進；新 epoch 的 bridge 掛上即可（clear 後 READY／ATTACHED 皆屬 06 行為）。
    wait_for("bridge 以新 epoch 重建", || {
        r.clock.advance(Duration::from_millis(250));
        r.session("COM0").is_some_and(|st| {
            st.get("bridge_generation").cloned() != gen0
                && matches!(s(&st, "state").as_str(), "READY" | "ATTACHED")
        })
    });
    let l = r.call("session.console_list", r#"{"selector":"COM0"}"#);
    let Some(PyValue::List(rows)) = l.get("consoles") else { panic!("consoles：{}", j(&l)) };
    let row = rows
        .iter()
        .find_map(|v| match v {
            PyValue::Dict(d) if s(d, "client_id") == cid => Some(d.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("h1 仍在：{}", j(&l)));
    assert_eq!(s(&row, "vtty"), vtty, "slave_path 不變");
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
    assert!(!io.closed(), "未關閉");
    r.device(REAL_A).push_rx(b"after-rebuild");
    wait_for("新 reader 的 RX 寫到 h1", || {
        String::from_utf8_lossy(&io.written()).contains("after-rebuild")
    });
}
