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

/// 07 Task 10 `attach_request_timeout_closes_pty`：對 actor 的請求逾時時 RPC handler 回 timeout 類錯誤，預建 PTY 隨
/// handler 丟棄（fd 不洩漏）；core 之後才處理到的 `ConsoleAttach` 在 executor 找不到 PTY，不留下幽靈 console 與 lease。
/// 計畫原文以「actor 卡住」觸發 `SendTimeoutError`；卡住 actor 的 fault hook 須改 P5 的 `actor.rs`（SPEC-GAP，見
/// p6-fix-responses），本測試以 0 逾時走同一個 handler 逾時出口（`ActorHandle::request` 的 send／recv 逾時皆回
/// `internal timeout`，`console_attach` 之後一律移除暫存 PTY）。
#[test]
fn attach_request_timeout_closes_pty() {
    let r = ServiceRig::with_console_backend("pty");
    let factory = r.console.as_ref().unwrap();
    let before = factory.created().len();
    let err = r
        .mgr
        .console_attach("COM0", None, Duration::ZERO)
        .expect_err("0 逾時：handler 不等 actor 回覆");
    assert_eq!(err.message, "internal timeout");
    assert_eq!(r.mgr.pending_pty_count(), 0, "逾時後暫存 PTY 已移除");
    let created = factory.created();
    assert_eq!(created.len(), before + 1, "handler 先建 PTY 再送請求");
    let (_, io) = created[before].clone();
    wait_for("逾時的 PTY 被 drop（fd 關閉）", || io.closed());
    // core 處理遲到的請求後，executor 回報 adopt 失敗，core 移除該 console 與其 lease（executor 註解「含其 owner／lease」；
    // 否則幽靈 `human:<cid>` lease 使之後 attach 的 console 不再取得 lease 與 raw ownership）。
    wait_for("不留幽靈 console", || {
        let l = r.call("session.console_list", r#"{"selector":"COM0"}"#);
        matches!(l.get("consoles"), Some(PyValue::List(v)) if v.len() == 1)
    });
    wait_for("不留幽靈 lease", || {
        r.session("COM0").unwrap().get("interactive_session_id") == Some(&PyValue::Null)
    });
    let ok = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(ok.get("interactive_owner"), Some(&PyValue::Bool(true)), "之後的 attach 照常：{}", j(&ok));
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
    // 編譯期內嵌（cargo_l1_results.py 以 repo 根為 cwd 執行測試執行檔，執行期相對路徑會失敗）。
    let scope = include_str!("../../../tests/acceptance/slice21_scope.yaml");
    let still = support::deferred_methods(scope, &["2.1", sw_core::SLICE]);
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

/// Task 11 黑箱 `test_peer_loss_grace_then_teardown` 的 L1 回歸：human lease 的 console 失去 peer 逾 grace 後，
/// `interactive_open` 前的 RPC ConsoleScan 讓 refresh 拆除該 lease（Python `_refresh_interactive_locked` 即時掃描）。
#[test]
fn interactive_open_scans_peers_before_refresh() {
    let r = ServiceRig::with_console_backend("pty");
    let a = attach(&r, r#"{"selector":"COM0"}"#);
    let vtty = s(&a, "vtty");
    let fake = &r.console.as_ref().unwrap().fake;
    fake.set_held(Some([vtty.clone()].into_iter().collect()));
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
    let dev = r.device(REAL_A);
    let _ = dev.take_tx();
    io.push_input(b"a"); // human active
    let mut seen = Vec::new();
    wait_for("按鍵到 UART", || {
        seen.extend(dev.take_tx());
        seen.contains(&b'a')
    });
    let first = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#);
    assert_eq!(s(&first, "error_code"), "SESSION_INTERACTIVE_BUSY", "{}", j(&first));
    fake.set_held(Some(Default::default())); // minicom 關閉
    let _ = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#); // 記下 peer 消失
    r.clock.advance(Duration::from_millis(3500));
    let ok = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#);
    assert_eq!(ok.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&ok));
    assert!(ok.get("soft_preempted").is_none(), "peer 消失逾 grace → 拆除後一般開啟：{}", j(&ok));
}

/// Task 11 黑箱 `test_soft_preempt_restore_and_replay` 的 L1 回歸：open 時 human console 尚無 peer（minicom 稍後才開），
/// soft preempt 期間的按鍵仍於 close 後回放到 UART。
#[test]
fn soft_preempt_replays_when_peer_appears_later() {
    let r = ServiceRig::with_console_backend("pty");
    let fake = &r.console.as_ref().unwrap().fake;
    fake.set_held(Some(Default::default()));
    let a = attach(&r, r#"{"selector":"COM0"}"#);
    // attach 後先取一次 session 摘要：held 為空的快照使 peer=false（等同 Python「minicom 尚未開啟」），
    // 本測試因此只依賴 close 前的即時掃描（審查 bd2f989 Low：原版在 attach 時 peer_present=true，不是獨立回歸）。
    let _ = r.call("session.get_state", r#"{"selector":"COM0"}"#);
    let o = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#);
    assert_eq!(o.get("soft_preempted"), Some(&PyValue::Bool(true)), "{}", j(&o));
    fake.set_held(Some([s(&a, "vtty")].into_iter().collect()));
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
    let dev = r.device(REAL_A);
    let _ = dev.take_tx();
    io.push_input(b"Q");
    std::thread::sleep(Duration::from_millis(200));
    let iid = s(&o, "interactive_id");
    let c = r.call("session.interactive_close", &format!(r#"{{"interactive_id":"{iid}"}}"#));
    assert_eq!(c.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&c));
    let mut seen = Vec::new();
    wait_for("close 後回放 Q", || {
        seen.extend(dev.take_tx());
        seen.contains(&b'Q')
    });
}

/// Medium（1fb9612）：`interactive_open` 的 SESSION_NOT_READY 回原樣 selector（Python `session_manager.py:6822` 起）。
#[test]
fn interactive_open_not_ready_keeps_raw_selector() {
    let r = ServiceRig::with_console_backend("pty");
    r.mgr.force_session_state("COM0", SessionState::Attached);
    let resp = r.call("session.interactive_open", r#"{"alias":"t+1"}"#);
    assert_eq!(
        j(&resp),
        format!(r#"{{"ok":false,"error_code":"SESSION_NOT_READY","selector":"t+1","version":"{}"}}"#, r.version)
    );
}

/// Medium（02fb068）：計畫 `attach_selector_alias_chain`——alias 選取 session。
#[test]
fn attach_selector_alias_chain() {
    let r = ServiceRig::with_console_backend("pty");
    let resp = attach(&r, r#"{"alias":"t+1"}"#);
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&resp));
    assert_eq!(s(&resp, "client_id").len(), 12);
}

/// Medium（02fb068）：計畫 `attach_not_ready_states` 的 DETACHED／RELEASED 兩態。
#[test]
fn attach_not_ready_detached_and_released() {
    let r = ServiceRig::with_console_backend("pty");
    let want = format!(r#"{{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0","version":"{}"}}"#, r.version);
    for st in [SessionState::Detached, SessionState::Released] {
        r.mgr.force_session_state("COM0", st);
        let before = r.side_effect_fingerprint().2;
        assert_eq!(j(&attach(&r, r#"{"selector":"COM0"}"#)), want, "{st:?}");
        assert_eq!(r.side_effect_fingerprint().2, before);
    }
}

// ---------------------------------------------------------------- TCP console（SC-28 tcp 後端；R3-2）

fn is_loopback_endpoint(v: &str) -> bool {
    v.strip_prefix("127.0.0.1:")
        .is_some_and(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
}

fn session_row(r: &ServiceRig) -> PyDict {
    let list = r.call("session.list", "{}");
    match list.get("sessions") {
        Some(PyValue::List(rows)) => match &rows[0] {
            PyValue::Dict(d) => d.clone(),
            other => panic!("{other:?}"),
        },
        other => panic!("{other:?}"),
    }
}

/// 07 Task 10 `attach_tcp_shape`（`uart_io.py:2578-2590`、`session_manager.py:6748-6756`）：回端點、不開 lease。
#[test]
fn attach_tcp_shape() {
    let r = ServiceRig::with_console_backend("tcp");
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(
        keys(&resp),
        ["ok", "client_id", "label", "vtty", "endpoint", "transport", "protocol", "session", "version"],
        "{}",
        j(&resp)
    );
    let ep = s(&resp, "endpoint");
    assert!(is_loopback_endpoint(&ep), "{}", j(&resp));
    let session = match resp.get("session") {
        Some(PyValue::Dict(d)) => d.clone(),
        other => panic!("{other:?}"),
    };
    let want = format!(
        r#"{{"ok":true,"client_id":null,"label":null,"vtty":"{ep}","endpoint":"{ep}","transport":"tcp","protocol":"telnet","session":{},"version":"{}"}}"#,
        j(&session),
        r.version
    );
    assert_eq!(j(&resp), want);
    assert_eq!(session.get("interactive_session_id"), Some(&PyValue::Null), "TCP attach 不開 lease");
    assert_eq!(s(&session, "vtty"), ep, "vtty 快取為 None 時設為端點（D-20）");
    let labeled = attach(&r, r#"{"selector":"COM0","label":"x"}"#);
    assert_eq!(s(&labeled, "label"), "x");
    assert_eq!(s(&labeled, "endpoint"), ep, "同一 bridge 端點不變");
}

/// 07 Task 10 `session_public_console_fields_tcp`：tcp 後端 SessionPublic 恆有 `console_endpoint`（最後一欄）。
#[test]
fn session_public_console_fields_tcp() {
    let r = ServiceRig::with_console_backend("tcp");
    let row = session_row(&r);
    let k = keys(&row);
    assert_eq!(k.last().map(String::as_str), Some("console_endpoint"), "{}", j(&row));
    assert_eq!(k[k.len() - 2], "drain_timeout_epoch");
    assert!(is_loopback_endpoint(&s(&row, "console_endpoint")), "{}", j(&row));
    assert_eq!(j(&row).contains(r#""console_count":0"#), true, "TCP 無哨兵 primary：{}", j(&row));
}

/// TCP client 連入：首個取得 raw owner（`on_stream_accept`）、先收 greeting、console_list 的 vtty 為端點。
#[test]
fn tcp_client_accept_gets_raw_owner_and_greeting() {
    let r = ServiceRig::with_console_backend("tcp");
    let ep = s(&session_row(&r), "console_endpoint");
    let fake = &r.console.as_ref().unwrap().fake;
    let c1 = fake.connect();
    wait_for("TCP client 登記", || {
        matches!(r.call("session.console_list", r#"{"selector":"COM0"}"#).get("consoles"),
            Some(PyValue::List(rows)) if rows.len() == 1)
    });
    let list = r.call("session.console_list", r#"{"selector":"COM0"}"#);
    let row = match list.get("consoles") {
        Some(PyValue::List(rows)) => match &rows[0] {
            PyValue::Dict(d) => d.clone(),
            other => panic!("{other:?}"),
        },
        other => panic!("{other:?}"),
    };
    assert_eq!(s(&row, "vtty"), ep, "{}", j(&row));
    assert_eq!(row.get("interactive_owner"), Some(&PyValue::Bool(true)), "{}", j(&row));
    wait_for("greeting", || c1.written().len() >= 12);
    assert_eq!(&c1.written()[..12], &sw_proto::constants::telnet::TELNET_GREETING[..]);
    let dev = r.device(REAL_A);
    let _ = dev.take_tx();
    c1.push_input(b"ab");
    let mut seen = Vec::new();
    wait_for("raw bytes 到 UART", || {
        seen.extend(dev.take_tx());
        seen.windows(2).any(|w| w == b"ab")
    });
}

/// release 後 listener 停止、`console_endpoint` 為 null（`uart_io.py:1113-1115`）。
#[test]
fn tcp_release_clears_console_endpoint() {
    let r = ServiceRig::with_console_backend("tcp");
    assert!(is_loopback_endpoint(&s(&session_row(&r), "console_endpoint")));
    let rel = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&rel));
    let row = session_row(&r);
    assert_eq!(row.get("console_endpoint"), Some(&PyValue::Null), "{}", j(&row));
}

/// 07 Task 12 `telnet_listener_ephemeral_per_bridge`（Linux 可驗部分）：bridge 重建後 listener 換新 port。
#[test]
fn tcp_listener_ephemeral_per_bridge() {
    let r = ServiceRig::with_console_backend("tcp");
    let first = s(&session_row(&r), "console_endpoint");
    assert!(is_loopback_endpoint(&first));
    let c = r.call("session.clear", r#"{"selector":"COM0"}"#);
    assert_eq!(c.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&c));
    let mut second = String::new();
    wait_for("新 bridge 的端點", || {
        r.clock.advance(Duration::from_millis(250));
        let row = session_row(&r);
        second = s(&row, "console_endpoint");
        is_loopback_endpoint(&second) && second != first && matches!(s(&row, "state").as_str(), "READY" | "ATTACHED")
    });
    let resp = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(s(&resp, "endpoint"), second);
}

// ---------------------------------------------------------------- 07 Task 10 表列（R3-9 補齊）

/// `list_consoles`／`detach_console`：bridge 不存在（release 後）→ `SESSION_NOT_READY{selector}`（`session_manager.py:6778`、`:6800`）。
#[test]
fn list_and_detach_without_bridge() {
    let r = ServiceRig::with_console_backend("pty");
    let rel = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&rel));
    let want = format!(r#"{{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0","version":"{}"}}"#, r.version);
    let before = r.side_effect_fingerprint().2;
    assert_eq!(j(&r.call("session.console_list", r#"{"selector":"COM0"}"#)), want);
    assert_eq!(j(&r.call("session.console_detach", r#"{"selector":"COM0","client_id":"abc"}"#)), want);
    assert_eq!(r.side_effect_fingerprint().2, before, "零 TX");
}

/// `service.py:1094` `float(params.get("timeout_s") or 60.0)` 先於 selector 檢查：轉型失敗為 framing EXCEPTION。
#[test]
fn open_coercion_float_error_before_invalid_args() {
    let r = ServiceRig::with_console_backend("pty");
    let err = r.call_err("session.interactive_open", r#"{"timeout_s":"x"}"#);
    assert_eq!(err.message, "could not convert string to float: 'x'", "{err:?}");
}

/// `timeout_s:0` 為 falsy → 預設 60.0：59 s 時仍 active，61 s 後 `INTERACTIVE_EXPIRED`。
#[test]
fn open_timeout_zero_uses_default() {
    let r = ServiceRig::with_console_backend("pty");
    let o = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent","timeout_s":0}"#);
    assert_eq!(o.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&o));
    let iid = s(&o, "interactive_id");
    let status = |r: &ServiceRig| r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}"}}"#));
    r.clock.advance(Duration::from_secs(30));
    assert_eq!(status(&r).get("ok"), Some(&PyValue::Bool(true)));
    // status 觸碰把到期延到 30+60＝90 s；再推進 61 s（91 s）越過（若誤用 0 s，第一次 status 即到期）
    r.clock.advance(Duration::from_secs(61));
    let st = status(&r);
    assert_eq!(s(&st, "error_code"), "INTERACTIVE_EXPIRED", "{}", j(&st));
}

/// `allow_attached = bool(params.get("allow_attached", False))`：字串 `"false"` 為 truthy，走 ATTACHED 救援分支。
#[test]
fn open_allow_attached_string_false_is_truthy() {
    let r = ServiceRig::with_console_backend("pty");
    r.mgr.force_session_state("COM0", SessionState::Attached);
    let plain = r.call("session.interactive_open", r#"{"selector":"COM0","allow_attached":false}"#);
    assert_eq!(s(&plain, "error_code"), "SESSION_NOT_READY", "{}", j(&plain));
    assert!(plain.get("error_detail").is_none(), "未帶 allow_attached 不判 bootloader：{}", j(&plain));
    let truthy = r.call("session.interactive_open", r#"{"selector":"COM0","allow_attached":"false"}"#);
    assert_eq!(s(&truthy, "error_code"), "SESSION_NOT_READY", "{}", j(&truthy));
    assert_eq!(s(&truthy, "error_detail"), "NOT_BOOTLOADER", "字串 false 為 truthy：{}", j(&truthy));
}

/// OD-37（擁有者裁決 2026-10-28；DF-20，Python 不改）：release → 重新 attach 後，以 release 前取得的舊
/// interactive_id（agent lease 與 human console lease 各一）呼叫 interactive_send，Rust 回 `INTERACTIVE_NOT_FOUND`
/// （帶 interactive_id）且零 TX。Python 現行仍照送該 bytes（舊 lease 未隨 release 失效），此為登記的刻意差異。
#[test]
fn od37_send_with_pre_release_id_after_reattach_not_found_zero_tx() {
    for human in [false, true] {
        let r = ServiceRig::with_console_backend("pty");
        let old = if human {
            let a = attach(&r, r#"{"selector":"COM0"}"#);
            s(&a, "interactive_session_id")
        } else {
            let o = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#);
            assert_eq!(o.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&o));
            s(&o, "interactive_id")
        };
        assert!(!old.is_empty(), "human={human}");
        let rel = r.call("device.release", r#"{"selector":"COM0"}"#);
        assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&rel));
        let att = r.call("device.attach", r#"{"selector":"COM0"}"#);
        assert_eq!(att.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&att));
        wait_for("重新 attach 後 bridge 就緒", || {
            r.clock.advance(Duration::from_millis(250));
            matches!(s(&session_row(&r), "state").as_str(), "READY" | "ATTACHED")
        });
        let dev = r.device(REAL_A);
        std::thread::sleep(Duration::from_millis(100));
        let _ = dev.take_tx();
        let send = r.call(
            "session.interactive_send",
            &format!(r#"{{"interactive_id":"{old}","data":"x\n"}}"#),
        );
        assert_eq!(
            j(&send),
            format!(
                r#"{{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":"{old}","version":"{}"}}"#,
                r.version
            ),
            "human={human}"
        );
        std::thread::sleep(Duration::from_millis(100));
        assert!(!dev.take_tx().contains(&b'x'), "human={human}：零 TX");
    }
}

/// 接線＋端到端（review-convergence-rules §5；sol P6-e9b5c61-1）：daemon 級（真 SessionManager＋Service＋ConsoleHub＋
/// human TX pump）兩個重疊的 `session.self_test` probe 各自 suspend／resume human——S1 的 probe 寫入停在提交前、
/// S2 隨後提交，期間 c1 鍵入 `k` 不得上 UART；兩個 probe 完成後 `k` 回放到 UART、之後按鍵直送。
/// 修正前共用旗標使一次 resume 遺失，`k` 永不回放（等待逾時）。
#[test]
fn overlapping_self_tests_replay_human_input_end_to_end() {
    use sw_pal::fakes::serial::FakeWriteStep;
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
    let dev = r.device(REAL_A);
    let _ = dev.take_tx();
    dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    std::thread::scope(|scope| {
        let s1 = scope.spawn(|| r.call("session.self_test", r#"{"selector":"COM0"}"#));
        assert!(dev.wait_paused(Duration::from_secs(5)), "S1 probe 寫入停在提交前");
        let s2 = scope.spawn(|| r.call("session.self_test", r#"{"selector":"COM0"}"#));
        std::thread::sleep(Duration::from_millis(300)); // S2 已進 core（suspend）並排在 TX writer 後
        io.push_input(b"k");
        std::thread::sleep(Duration::from_millis(300));
        assert!(!dev.take_tx().contains(&b'k'), "probe 期間 human 按鍵不得上 UART");
        dev.release_pause();
        let (a, b) = (s1.join().unwrap(), s2.join().unwrap());
        assert_eq!(a.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&a));
        assert_eq!(b.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&b));
    });
    let mut seen = Vec::new();
    wait_for("兩個 probe 完成後回放 k", || {
        seen.extend(dev.take_tx());
        seen.contains(&b'k')
    });
    io.push_input(b"z");
    wait_for("resume 後按鍵直送", || {
        seen.extend(dev.take_tx());
        seen.contains(&b'z')
    });
}

/// 接線＋端到端（§5）：core 的 `Effect::ConsoleOut` 經 manager executor 交給 hub 寫到該 console——第二個 console
/// 走 line-buffer，按鍵的本地回顯（`uart_io.py` line mode echo）出現在該 console 的輸出、不上 UART。
#[test]
fn second_console_local_echo_reaches_its_console() {
    let r = ServiceRig::with_console_backend("pty");
    attach(&r, r#"{"selector":"COM0"}"#);
    let second = attach(&r, r#"{"selector":"COM0"}"#);
    assert_eq!(second.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&second));
    let (_, io2) = r.console.as_ref().unwrap().created()[2].clone();
    let dev = r.device(REAL_A);
    let _ = dev.take_tx();
    io2.push_input(b"ab");
    wait_for("第二個 console 的本地回顯", || String::from_utf8_lossy(&io2.written()).contains("ab"));
    std::thread::sleep(Duration::from_millis(100));
    assert!(!dev.take_tx().windows(2).any(|w| w == b"ab"), "line-buffer 未送出前不上 UART");
}

/// 接線＋端到端（§5）：release（BeginDrain）停止 TCP console listener——之後連入的 client 不被 accept
/// （無 Telnet greeting；Python `uart_io.py:1113-1115` 關閉 listener）。
#[test]
fn tcp_release_stops_listener_accepting() {
    let r = ServiceRig::with_console_backend("tcp");
    assert!(is_loopback_endpoint(&s(&session_row(&r), "console_endpoint")));
    let rel = r.call("device.release", r#"{"selector":"COM0"}"#);
    assert_eq!(rel.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&rel));
    let late = r.console.as_ref().unwrap().fake.connect();
    std::thread::sleep(Duration::from_millis(400));
    assert!(late.written().is_empty(), "release 後 listener 已停止，不得 accept 並送 greeting");
}

/// 接線＋端到端（§5；07 D-09）：`console_attach` 前的 RPC ConsoleScan——既有 human console 的 peer 消失並經
/// refresh 記下 `peer_lost_at`、逾 grace 後：attach 當下 minicom 已重開 → 即時掃描判定 peer 存在、既有 human lease
/// 保留；仍未重開 → attach 的 refresh 拆除該 lease（Python `attach_console` → `_refresh_interactive_locked` 即時
/// `console_has_external_peer`，`session_manager.py` peer-loss grace）。
#[test]
fn console_attach_rescans_peers_before_refresh() {
    for reopened in [true, false] {
        let r = ServiceRig::with_console_backend("pty");
        let a = attach(&r, r#"{"selector":"COM0"}"#);
        let iid = s(&a, "interactive_session_id");
        let fake = &r.console.as_ref().unwrap().fake;
        fake.set_held(Some([s(&a, "vtty")].into_iter().collect()));
        let (_, io) = r.console.as_ref().unwrap().created()[1].clone();
        let dev = r.device(REAL_A);
        let _ = dev.take_tx();
        io.push_input(b"a"); // human active（agent open 回 BUSY 而非 soft preempt）
        let mut seen = Vec::new();
        wait_for("按鍵到 UART", || {
            seen.extend(dev.take_tx());
            seen.contains(&b'a')
        });
        fake.set_held(Some(Default::default())); // minicom 關閉
        let busy = r.call("session.interactive_open", r#"{"selector":"COM0","owner":"agent"}"#); // refresh 記下 peer 消失
        assert_eq!(s(&busy, "error_code"), "SESSION_INTERACTIVE_BUSY", "{}", j(&busy));
        r.clock.advance(Duration::from_millis(3500));
        if reopened {
            fake.set_held(Some([s(&a, "vtty")].into_iter().collect())); // attach 前 minicom 重開
        }
        let b = attach(&r, r#"{"selector":"COM0"}"#);
        assert_eq!(b.get("ok"), Some(&PyValue::Bool(true)), "{}", j(&b));
        let st = r.call("session.interactive_status", &format!(r#"{{"interactive_id":"{iid}"}}"#));
        if reopened {
            assert_eq!(st.get("ok"), Some(&PyValue::Bool(true)), "peer 已回來，既有 human lease 不得被拆：{}", j(&st));
        } else {
            assert_ne!(st.get("ok"), Some(&PyValue::Bool(true)), "peer 逾 grace 未回，attach 的 refresh 須拆除：{}", j(&st));
            assert!(!s(&b, "interactive_session_id").is_empty(), "拆除後新 console 取得 human lease：{}", j(&b));
        }
    }
}
